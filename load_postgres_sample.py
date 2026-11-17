#!/usr/bin/env python3
"""Optionally load a bounded TLC source-month sample into normalized PostgreSQL."""

from __future__ import annotations

import argparse
import os
import time
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pyarrow.parquet as pq
from dotenv import load_dotenv

from load_month import download_source, parse_month
from trip_fields import (
    BATCH_SIZE,
    FIELD_SPECS,
    TARGET_COLUMNS,
    iter_mapped_records,
    source_columns_for_schema,
    validate_source_schema,
)
from zone_lookup import download_zone_lookup, read_zone_rows

ROOT = Path(__file__).resolve().parent
MONEY_COLUMNS = {
    "fare_amount",
    "tip_amount",
    "tolls_amount",
    "extra",
    "mta_tax",
    "improvement_surcharge",
    "congestion_surcharge",
    "airport_fee",
    "cbd_congestion_fee",
    "total_amount",
}

STAGE_COLUMNS = ("source_row_number",) + TARGET_COLUMNS[:-2]
STAGE_TABLE_DDL = """
CREATE TEMP TABLE stage_trips
(
    source_row_number BIGINT NOT NULL,
    vendor_id SMALLINT,
    pickup_datetime TIMESTAMP WITHOUT TIME ZONE,
    dropoff_datetime TIMESTAMP WITHOUT TIME ZONE,
    passenger_count SMALLINT,
    trip_distance NUMERIC(14, 6),
    rate_code_id SMALLINT,
    store_and_fwd_flag TEXT,
    pickup_location_id INTEGER,
    dropoff_location_id INTEGER,
    payment_type_id SMALLINT,
    fare_amount NUMERIC(14, 2),
    tip_amount NUMERIC(14, 2),
    tolls_amount NUMERIC(14, 2),
    extra NUMERIC(14, 2),
    mta_tax NUMERIC(14, 2),
    improvement_surcharge NUMERIC(14, 2),
    congestion_surcharge NUMERIC(14, 2),
    airport_fee NUMERIC(14, 2),
    cbd_congestion_fee NUMERIC(14, 2),
    request_source TEXT,
    total_amount NUMERIC(14, 2)
) ON COMMIT DROP
"""

VENDORS = (
    (1, "Creative Mobile Technologies LLC"),
    (2, "VeriFone Inc."),
    (6, "Myle Technologies Inc"),
    (7, "Helix"),
)
RATE_CODES = (
    (1, "Standard rate"),
    (2, "JFK"),
    (3, "Newark"),
    (4, "Nassau or Westchester"),
    (5, "Negotiated fare"),
    (6, "Group ride"),
    (99, "Unknown or empty"),
)
PAYMENT_TYPES = (
    (0, "Flex Fare trip"),
    (1, "Credit card"),
    (2, "Cash"),
    (3, "No charge"),
    (4, "Dispute"),
    (5, "Unknown"),
    (6, "Voided trip"),
)


def _postgres_connection() -> psycopg.Connection[Any]:
    load_dotenv(ROOT / ".env")
    settings = {
        "host": os.getenv("POSTGRES_HOST", os.getenv("PGHOST", "127.0.0.1")),
        "port": int(os.getenv("POSTGRES_PORT", os.getenv("PGPORT", "5432"))),
        "dbname": os.getenv("POSTGRES_DATABASE", os.getenv("PGDATABASE", "")),
        "user": os.getenv("POSTGRES_USER", os.getenv("PGUSER", "")),
        "password": os.getenv("POSTGRES_PASSWORD", os.getenv("PGPASSWORD", "")),
    }
    missing = [
        env_name
        for env_name, setting in (
            ("POSTGRES_DATABASE", settings["dbname"]),
            ("POSTGRES_USER", settings["user"]),
            ("POSTGRES_PASSWORD", settings["password"]),
        )
        if not setting
    ]
    if missing:
        raise ValueError(
            "set " + ", ".join(missing) + " in the environment or project .env file"
        )
    return psycopg.connect(**settings)


def _normalize_postgres_record(record: tuple[Any, ...]) -> tuple[Any, ...]:
    result = list(record[:-2])
    for index, (_, target, _) in enumerate(FIELD_SPECS):
        value = result[index]
        if value is not None and target in MONEY_COLUMNS:
            result[index] = Decimal(str(value)).quantize(Decimal("0.01"))
        elif value is not None and target == "trip_distance":
            result[index] = Decimal(str(value)).quantize(Decimal("0.000001"))
    return tuple(result)


def _apply_schema(connection: psycopg.Connection[Any]) -> None:
    schema_sql = (ROOT / "sql" / "postgres" / "schema.sql").read_text(
        encoding="utf-8"
    )
    for statement in schema_sql.split(";"):
        if statement.strip():
            connection.execute(statement)
    connection.commit()


def _seed_reference_data(
    cursor: psycopg.Cursor[Any],
    zone_rows: list[tuple[int, str, str, str]],
) -> None:
    cursor.executemany(
        """
        INSERT INTO reference.taxi_zones
            (location_id, borough, zone, service_zone)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (location_id) DO UPDATE SET
            borough = EXCLUDED.borough,
            zone = EXCLUDED.zone,
            service_zone = EXCLUDED.service_zone
        """,
        zone_rows,
    )
    cursor.execute(
        """
        INSERT INTO reference.taxi_zones
            (location_id, borough, zone, service_zone)
        VALUES (0, 'Unknown', 'Missing or unknown location ID', 'Unknown')
        ON CONFLICT (location_id) DO NOTHING
        """
    )
    for table, id_column, label_column, rows in (
        ("vendors", "vendor_id", "vendor_name", VENDORS),
        ("rate_codes", "rate_code_id", "rate_description", RATE_CODES),
        ("payment_types", "payment_type_id", "payment_description", PAYMENT_TYPES),
    ):
        cursor.executemany(
            f"""
            INSERT INTO reference.{table} ({id_column}, {label_column})
            VALUES (%s, %s)
            ON CONFLICT ({id_column}) DO NOTHING
            """,
            rows,
        )

    for table, id_column, label_column, description in (
        ("vendors", "vendor_id", "vendor_name", "Unknown vendor code "),
        ("rate_codes", "rate_code_id", "rate_description", "Unknown rate code "),
        ("payment_types", "payment_type_id", "payment_description", "Unknown payment code "),
    ):
        cursor.execute(
            f"""
            INSERT INTO reference.{table} ({id_column}, {label_column})
            SELECT DISTINCT {id_column}, %s || {id_column}::TEXT
            FROM stage_trips
            WHERE {id_column} IS NOT NULL
            ON CONFLICT ({id_column}) DO NOTHING
            """,
            (description,),
        )

    cursor.execute(
        """
        INSERT INTO reference.taxi_zones
            (location_id, borough, zone, service_zone)
        SELECT unmatched_locations.location_id, 'Unknown',
               'Unmatched location ID ' || unmatched_locations.location_id::TEXT,
               'Unknown'
        FROM
        (
            SELECT pickup_location_id AS location_id
            FROM stage_trips
            WHERE pickup_location_id IS NOT NULL
            UNION
            SELECT dropoff_location_id AS location_id
            FROM stage_trips
            WHERE dropoff_location_id IS NOT NULL
        ) AS unmatched_locations
        LEFT JOIN reference.taxi_zones AS zones USING (location_id)
        WHERE zones.location_id IS NULL
        ON CONFLICT (location_id) DO NOTHING
        """
    )


def _verify_constraints(
    connection: psycopg.Connection[Any], month: date, expected_rows: int
) -> tuple[int, int]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM core.trips AS t
            JOIN etl.source_files AS sf
                ON sf.source_file_id = t.source_file_id
            WHERE sf.source_month = %s
            """,
            (month,),
        )
        loaded_rows = cursor.fetchone()[0]
        cursor.execute(
            """
            SELECT
                COUNT(*) FILTER (
                    WHERE t.source_file_id IS NOT NULL AND sf.source_file_id IS NULL
                )
                + COUNT(*) FILTER (
                    WHERE t.vendor_id IS NOT NULL AND v.vendor_id IS NULL
                )
                + COUNT(*) FILTER (
                    WHERE t.rate_code_id IS NOT NULL AND r.rate_code_id IS NULL
                )
                + COUNT(*) FILTER (
                    WHERE t.payment_type_id IS NOT NULL
                      AND p.payment_type_id IS NULL
                )
                + COUNT(*) FILTER (
                    WHERE t.pickup_location_id IS NOT NULL
                      AND pu.location_id IS NULL
                )
                + COUNT(*) FILTER (
                    WHERE t.dropoff_location_id IS NOT NULL
                      AND do_zone.location_id IS NULL
                )
            FROM core.trips AS t
            LEFT JOIN etl.source_files AS sf
                ON sf.source_file_id = t.source_file_id
            LEFT JOIN reference.vendors AS v
                ON v.vendor_id = t.vendor_id
            LEFT JOIN reference.rate_codes AS r
                ON r.rate_code_id = t.rate_code_id
            LEFT JOIN reference.payment_types AS p
                ON p.payment_type_id = t.payment_type_id
            LEFT JOIN reference.taxi_zones AS pu
                ON pu.location_id = t.pickup_location_id
            LEFT JOIN reference.taxi_zones AS do_zone
                ON do_zone.location_id = t.dropoff_location_id
            """
        )
        orphan_rows = cursor.fetchone()[0]
    if loaded_rows != expected_rows:
        raise RuntimeError(
            f"PostgreSQL source-file row count mismatch: "
            f"expected {expected_rows}, found {loaded_rows}"
        )
    if orphan_rows:
        raise RuntimeError(f"PostgreSQL foreign-key orphan check found {orphan_rows} rows")
    return loaded_rows, orphan_rows


def load_month(
    month: date, sample_rows: int
) -> tuple[int, tuple[str, ...], int]:
    if sample_rows < 1:
        raise ValueError("sample_rows must be positive")
    started_at = time.monotonic()
    source_url = (
        "https://d37ci6vzurychx.cloudfront.net/trip-data/"
        f"yellow_tripdata_{month:%Y-%m}.parquet"
    )

    connection = _postgres_connection()
    rows_read = 0
    load_attempt_id: int | None = None
    try:
        _apply_schema(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO etl.load_attempts
                    (source_month, source_url, status)
                VALUES (%s, %s, 'running')
                RETURNING load_attempt_id
                """,
                (month, source_url),
            )
            load_attempt_id = cursor.fetchone()[0]
        connection.commit()

        cache_dir = ROOT / ".cache" / "tlc"
        source_path = download_source(month, cache_dir)
        zone_path = download_zone_lookup(cache_dir)
        zone_rows = read_zone_rows(zone_path)
        parquet_file = pq.ParquetFile(source_path)
        optional_columns_missing = validate_source_schema(parquet_file.schema_arrow)
        expected_rows = parquet_file.metadata.num_rows
        expected_rows = min(expected_rows, sample_rows)
        if expected_rows == 0:
            raise ValueError(f"source Parquet file for {month:%Y-%m} has no rows")
        columns_to_read = source_columns_for_schema(parquet_file.schema_arrow)

        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute(STAGE_TABLE_DDL)
                copy_columns = ", ".join(STAGE_COLUMNS)
                with cursor.copy(
                    f"COPY stage_trips ({copy_columns}) FROM STDIN"
                ) as copy:
                    for batch in parquet_file.iter_batches(
                        batch_size=BATCH_SIZE,
                        columns=columns_to_read,
                    ):
                        remaining = expected_rows - rows_read
                        if remaining <= 0:
                            break
                        if batch.num_rows > remaining:
                            batch = batch.slice(0, remaining)
                        for row_offset, record in enumerate(
                            iter_mapped_records(
                                batch,
                                month,
                                source_path.name,
                                postgres=True,
                            )
                        ):
                            copy.write_row(
                                (rows_read + row_offset + 1,)
                                + _normalize_postgres_record(record)
                            )
                        rows_read += batch.num_rows

                cursor.execute("SELECT COUNT(*) FROM stage_trips")
                staged_rows = cursor.fetchone()[0]
                if rows_read != expected_rows or staged_rows != expected_rows:
                    raise RuntimeError(
                        f"staging row count mismatch for {month:%Y-%m}: "
                        f"expected {expected_rows}, read {rows_read}, staged {staged_rows}"
                    )

                _seed_reference_data(cursor, zone_rows)
                cursor.execute(
                    """
                    INSERT INTO etl.source_files
                    (
                        source_month, source_url, source_file_name,
                        rows_read, accepted_rows, rejected_rows, loaded_rows,
                        load_duration_seconds, status, loaded_at
                    )
                    VALUES (%s, %s, %s, %s, %s, 0, %s, %s, 'loaded', now())
                    ON CONFLICT (source_month) DO UPDATE SET
                        source_url = EXCLUDED.source_url,
                        source_file_name = EXCLUDED.source_file_name,
                        rows_read = EXCLUDED.rows_read,
                        accepted_rows = EXCLUDED.accepted_rows,
                        rejected_rows = EXCLUDED.rejected_rows,
                        loaded_rows = EXCLUDED.loaded_rows,
                        load_duration_seconds = EXCLUDED.load_duration_seconds,
                        status = EXCLUDED.status,
                        loaded_at = now()
                    RETURNING source_file_id
                    """,
                    (
                        month,
                        source_url,
                        source_path.name,
                        rows_read,
                        rows_read,
                        rows_read,
                        time.monotonic() - started_at,
                    ),
                )
                source_file_id = cursor.fetchone()[0]
                cursor.execute(
                    "DELETE FROM core.trips WHERE source_file_id = %s",
                    (source_file_id,),
                )
                trip_columns = ", ".join(TARGET_COLUMNS[:-2])
                cursor.execute(
                    f"""
                    INSERT INTO core.trips
                        (source_file_id, source_row_number, {trip_columns})
                    SELECT %s, source_row_number, {trip_columns}
                    FROM stage_trips
                    ORDER BY source_row_number
                    """,
                    (source_file_id,),
                )
                cursor.execute(
                    "SELECT COUNT(*) FROM core.trips WHERE source_file_id = %s",
                    (source_file_id,),
                )
                inserted_rows = cursor.fetchone()[0]
                if inserted_rows != rows_read:
                    raise RuntimeError(
                        f"PostgreSQL insert row count mismatch: "
                        f"expected {rows_read}, inserted {inserted_rows}"
                    )
                loaded_rows, foreign_key_orphans = _verify_constraints(
                    connection, month, rows_read
                )
                duration = time.monotonic() - started_at
                cursor.execute(
                    """
                    UPDATE etl.source_files
                    SET load_duration_seconds = %s
                    WHERE source_file_id = %s
                    """,
                    (duration, source_file_id),
                )
                cursor.execute(
                    """
                    UPDATE etl.load_attempts
                    SET status = 'loaded',
                        rows_read = %s,
                        rows_loaded = %s,
                        duration_seconds = %s,
                        completed_at = now()
                    WHERE load_attempt_id = %s
                    """,
                    (rows_read, loaded_rows, duration, load_attempt_id),
                )
    except Exception as exc:
        if load_attempt_id is not None:
            try:
                with connection.transaction():
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """
                            UPDATE etl.load_attempts
                            SET status = 'failed',
                                rows_read = %s,
                                rows_loaded = 0,
                                duration_seconds = %s,
                                error_message = %s,
                                completed_at = now()
                            WHERE load_attempt_id = %s
                            """,
                            (
                                rows_read,
                                time.monotonic() - started_at,
                                str(exc)[:4000],
                                load_attempt_id,
                            ),
                        )
            except Exception as logging_error:
                raise RuntimeError(
                    f"load failed with {exc}; recording the failed attempt also "
                    f"failed with {logging_error}"
                ) from exc
        raise
    finally:
        connection.close()

    return loaded_rows, optional_columns_missing, foreign_key_orphans


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--month", required=True, type=parse_month, help="month as YYYY-MM")
    parser.add_argument(
        "--sample-rows",
        type=int,
        default=100_000,
        help="maximum number of source rows to load (default: 100000)",
    )
    args = parser.parse_args(argv)
    if args.sample_rows < 1:
        parser.error("--sample-rows must be positive")
    started_at = time.monotonic()
    try:
        rows_loaded, optional_columns_missing, foreign_key_orphans = load_month(
            args.month, sample_rows=args.sample_rows
        )
    except Exception as exc:
        print(f"PostgreSQL load failed for {args.month:%Y-%m}: {exc}", file=os.sys.stderr)
        return 1

    elapsed = time.monotonic() - started_at
    print(
        f"source_month={args.month:%Y-%m} rows_read={rows_loaded} "
        f"accepted={rows_loaded} rejected=0 rows_loaded={rows_loaded} "
        f"foreign_key_orphans={foreign_key_orphans} elapsed_seconds={elapsed:.2f}"
    )
    if optional_columns_missing:
        print(
            "optional_source_columns_missing="
            + ",".join(optional_columns_missing)
            + " (stored as NULL)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
