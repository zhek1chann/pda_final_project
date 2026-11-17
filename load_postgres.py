#!/usr/bin/env python3
"""Publish ClickHouse daily summaries to PostgreSQL."""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import clickhouse_connect
import psycopg
from dotenv import load_dotenv

from zone_lookup import download_zone_lookup, read_zone_rows

ROOT = Path(__file__).resolve().parent

DAILY_TRIPS_QUERY = """
SELECT
    toDate(pickup_datetime) AS pickup_date,
    count() AS trip_count,
    round(ifNull(sumIf(fare_amount, isNotNull(fare_amount)), 0.), 2)
        AS fare_amount_total,
    round(ifNull(sumIf(tip_amount, payment_type_id = 1), 0.), 2)
        AS recorded_card_tips_total,
    if(
        countIf(
            isNotNull(dropoff_datetime)
                AND dropoff_datetime > pickup_datetime
                AND dropoff_datetime <= addHours(pickup_datetime, 24)
        ) = 0,
        CAST(NULL, 'Nullable(Float64)'),
        round(
            quantileExactIf(0.5)(
                dateDiff('millisecond', pickup_datetime, dropoff_datetime) / 1000.,
                isNotNull(dropoff_datetime)
                    AND dropoff_datetime > pickup_datetime
                    AND dropoff_datetime <= addHours(pickup_datetime, 24)
            ),
            2
        )
    ) AS median_valid_trip_duration_seconds
FROM trips
WHERE isNotNull(pickup_datetime)
GROUP BY pickup_date
ORDER BY pickup_date
"""

DAILY_PICKUP_ZONES_QUERY = """
SELECT
    toDate(pickup_datetime) AS pickup_date,
    ifNull(pickup_location_id, toInt32(0)) AS pickup_location_id,
    count() AS trip_count
FROM trips
WHERE isNotNull(pickup_datetime)
GROUP BY pickup_date, pickup_location_id
ORDER BY pickup_date, pickup_location_id
"""


def _clickhouse_client() -> Any:
    load_dotenv(ROOT / ".env")
    database = os.getenv("CLICKHOUSE_DATABASE", "")
    username = os.getenv("CLICKHOUSE_USER", "")
    password = os.getenv("CLICKHOUSE_PASSWORD", "")
    if not database or not username or not password:
        raise ValueError(
            "set CLICKHOUSE_DATABASE, CLICKHOUSE_USER, and CLICKHOUSE_PASSWORD "
            "in the environment or project .env file"
        )
    return clickhouse_connect.get_client(
        host=os.getenv("CLICKHOUSE_HOST", "127.0.0.1"),
        port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
        username=username,
        password=password,
        database=database,
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
        name
        for name, value in (
            ("POSTGRES_DATABASE", settings["dbname"]),
            ("POSTGRES_USER", settings["user"]),
            ("POSTGRES_PASSWORD", settings["password"]),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            "set " + ", ".join(missing) + " in the environment or project .env file"
        )
    return psycopg.connect(**settings)


def _apply_schema(connection: psycopg.Connection[Any]) -> None:
    schema = (ROOT / "sql" / "postgres" / "schema.sql").read_text(
        encoding="utf-8"
    )
    for statement in schema.split(";"):
        if statement.strip():
            connection.execute(statement)
    connection.commit()


def _money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def publish_summaries() -> tuple[int, int, int]:
    clickhouse = _clickhouse_client()
    try:
        daily_trips = clickhouse.query(DAILY_TRIPS_QUERY).result_rows
        daily_zones = clickhouse.query(DAILY_PICKUP_ZONES_QUERY).result_rows
    finally:
        clickhouse.close()

    daily_trips = [
        (
            row[0],
            int(row[1]),
            _money(row[2]),
            _money(row[3]),
            _money(row[4]) if row[4] is not None else None,
        )
        for row in daily_trips
    ]
    daily_zones = [
        (row[0], int(row[1]), int(row[2]))
        for row in daily_zones
    ]
    daily_trip_total = sum(row[1] for row in daily_trips)
    daily_zone_total = sum(row[2] for row in daily_zones)
    if daily_trip_total != daily_zone_total:
        raise RuntimeError(
            "ClickHouse summary totals disagree: "
            f"daily_trips={daily_trip_total}, daily_pickup_zones={daily_zone_total}"
        )
    zone_rows = read_zone_rows(
        download_zone_lookup(ROOT / ".cache" / "tlc")
    )
    known_zone_ids = {row[0] for row in zone_rows}
    unmatched_zone_ids = sorted(
        {row[1] for row in daily_zones} - known_zone_ids - {0}
    )

    connection = _postgres_connection()
    try:
        _apply_schema(connection)
        with connection.transaction():
            with connection.cursor() as cursor:
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
                if unmatched_zone_ids:
                    cursor.executemany(
                        """
                        INSERT INTO reference.taxi_zones
                            (location_id, borough, zone, service_zone)
                        VALUES (%s, 'Unknown', 'Unmatched location ID ' || %s::TEXT,
                                'Unknown')
                        ON CONFLICT (location_id) DO NOTHING
                        """,
                        [(zone_id, zone_id) for zone_id in unmatched_zone_ids],
                    )
                cursor.execute(
                    "TRUNCATE analytics.daily_trips, "
                    "analytics.daily_pickup_zones"
                )
                if daily_trips:
                    cursor.executemany(
                        """
                        INSERT INTO analytics.daily_trips
                        (
                            pickup_date,
                            trip_count,
                            fare_amount_total,
                            recorded_card_tips_total,
                            median_valid_trip_duration_seconds
                        )
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        daily_trips,
                    )
                if daily_zones:
                    cursor.executemany(
                        """
                        INSERT INTO analytics.daily_pickup_zones
                            (pickup_date, pickup_location_id, trip_count)
                        VALUES (%s, %s, %s)
                        """,
                        daily_zones,
                    )
                cursor.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM analytics.daily_trips),
                        (SELECT COALESCE(SUM(trip_count), 0)
                         FROM analytics.daily_trips),
                        (SELECT COUNT(*) FROM analytics.daily_pickup_zones),
                        (SELECT COALESCE(SUM(trip_count), 0)
                         FROM analytics.daily_pickup_zones)
                    """
                )
                (
                    daily_trip_rows,
                    published_trip_total,
                    daily_zone_rows,
                    published_zone_total,
                ) = cursor.fetchone()
                if (
                    daily_trip_rows != len(daily_trips)
                    or daily_zone_rows != len(daily_zones)
                    or published_trip_total != daily_trip_total
                    or published_zone_total != daily_zone_total
                ):
                    raise RuntimeError(
                        "PostgreSQL summary verification failed: "
                        f"daily_trips={daily_trip_rows}/{len(daily_trips)} rows, "
                        f"daily_pickup_zones={daily_zone_rows}/{len(daily_zones)} "
                        "rows"
                    )
    finally:
        connection.close()

    return len(daily_trips), len(daily_zones), daily_trip_total


def main() -> int:
    try:
        daily_trip_rows, daily_zone_rows, trip_total = publish_summaries()
    except Exception as exc:
        print(f"Summary publication failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"daily_trips_rows={daily_trip_rows} "
        f"daily_pickup_zones_rows={daily_zone_rows} "
        f"pickup_trip_total={trip_total}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
