#!/usr/bin/env python3
"""Load one official NYC TLC Yellow Taxi Parquet month into ClickHouse."""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
import time
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

import clickhouse_connect
import pyarrow.parquet as pq
from dotenv import load_dotenv

from trip_fields import (
    BATCH_SIZE,
    TARGET_COLUMNS,
    iter_mapped_records,
    source_columns_for_schema,
    validate_source_schema,
)
from zone_lookup import download_zone_lookup, read_zone_rows

IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
CLICKHOUSE_COLUMN_MIGRATIONS = (
    ("vendor_id", "Nullable(Int16)"),
    ("passenger_count", "Nullable(Int16)"),
    ("rate_code_id", "Nullable(Int16)"),
    ("store_and_fwd_flag", "Nullable(String)"),
    ("payment_type_id", "Nullable(Int16)"),
    ("fare_amount", "Nullable(Float64)"),
    ("tip_amount", "Nullable(Float64)"),
    ("tolls_amount", "Nullable(Float64)"),
    ("extra", "Nullable(Float64)"),
    ("mta_tax", "Nullable(Float64)"),
    ("improvement_surcharge", "Nullable(Float64)"),
    ("congestion_surcharge", "Nullable(Float64)"),
    ("airport_fee", "Nullable(Float64)"),
    ("cbd_congestion_fee", "Nullable(Float64)"),
    ("request_source", "Nullable(String)"),
    ("source_file", "String DEFAULT ''"),
)


def parse_month(value: str) -> date:
    """Parse a calendar month into its first day."""
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise argparse.ArgumentTypeError(
            f"invalid month {value!r}; expected YYYY-MM with a valid month"
        )
    return date.fromisoformat(f"{value}-01")


def _identifier(value: str, label: str) -> str:
    if not IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(f"{label} must contain only letters, digits, or underscores")
    return f"`{value}`"


def _ensure_zone_lookup(client: Any) -> int:
    zone_rows = read_zone_rows(
        download_zone_lookup(Path(__file__).resolve().parent / ".cache" / "tlc")
    )
    existing_rows = client.query("SELECT count() FROM taxi_zones").result_rows[0][0]
    if existing_rows != len(zone_rows):
        client.command("TRUNCATE TABLE taxi_zones")
        client.insert(
            "taxi_zones",
            zone_rows,
            column_names=["location_id", "borough", "zone", "service_zone"],
        )
    loaded_rows = client.query("SELECT count() FROM taxi_zones").result_rows[0][0]
    if loaded_rows != len(zone_rows):
        raise RuntimeError(
            f"taxi zone lookup row count mismatch: expected {len(zone_rows)}, "
            f"found {loaded_rows}"
        )
    return loaded_rows


def download_source(month: date, cache_dir: Path) -> Path:
    month_text = month.strftime("%Y-%m")
    filename = f"yellow_tripdata_{month_text}.parquet"
    url = f"https://d37ci6vzurychx.cloudfront.net/trip-data/{filename}"
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = cache_dir / filename
    if destination.exists():
        return destination

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=cache_dir,
            prefix=f".{filename}.",
            suffix=".part",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            with urllib.request.urlopen(url, timeout=60) as response:
                while chunk := response.read(1024 * 1024):
                    temporary_file.write(chunk)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, destination)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    return destination


def load_month(month: date) -> tuple[int, tuple[str, ...]]:
    cache_dir = Path(__file__).resolve().parent / ".cache" / "tlc"
    source_path = download_source(month, cache_dir)
    parquet_file = pq.ParquetFile(source_path)
    optional_columns_missing = validate_source_schema(parquet_file.schema_arrow)
    expected_rows = parquet_file.metadata.num_rows
    if expected_rows == 0:
        raise ValueError(f"source Parquet file for {month:%Y-%m} has no rows")
    columns_to_read = source_columns_for_schema(parquet_file.schema_arrow)

    load_dotenv(Path(__file__).resolve().parent / ".env")
    database_name = os.environ.get("CLICKHOUSE_DATABASE", "")
    if not database_name:
        raise ValueError(
            "set CLICKHOUSE_DATABASE in the environment or project .env file"
        )
    database = _identifier(database_name, "CLICKHOUSE_DATABASE")
    username = os.environ.get("CLICKHOUSE_USER")
    password = os.environ.get("CLICKHOUSE_PASSWORD")
    if not username or not password:
        raise ValueError(
            "set CLICKHOUSE_USER and CLICKHOUSE_PASSWORD in the environment or .env"
        )

    host = os.environ.get("CLICKHOUSE_HOST", "127.0.0.1")
    port = int(os.environ.get("CLICKHOUSE_PORT", "8123"))
    client = clickhouse_connect.get_client(
        host=host,
        port=port,
        username=username,
        password=password,
        database=database_name,
    )
    stage_table = f"trips_stage_{uuid4().hex}"
    stage = f"{database}.`{stage_table}`"
    target = f"{database}.`trips`"
    stage_created = False
    rows_read = 0

    try:
        schema_path = Path(__file__).resolve().with_name("schema.sql")
        for statement in schema_path.read_text(encoding="utf-8").split(";"):
            if statement.strip():
                client.command(statement)
        for column_name, column_type in CLICKHOUSE_COLUMN_MIGRATIONS:
            client.command(
                f"ALTER TABLE {target} ADD COLUMN IF NOT EXISTS "
                f"`{column_name}` {column_type}"
            )
        _ensure_zone_lookup(client)
        stage_created = True
        client.command(f"CREATE TABLE {stage} AS {target}")

        for batch in parquet_file.iter_batches(
            batch_size=BATCH_SIZE,
            columns=columns_to_read,
        ):
            rows = list(
                iter_mapped_records(
                    batch,
                    month,
                    source_path.name,
                )
            )
            client.insert(stage, rows, column_names=list(TARGET_COLUMNS))
            rows_read += len(rows)

        staged_rows = client.query(f"SELECT count() FROM {stage}").result_rows[0][0]
        if rows_read != expected_rows or staged_rows != expected_rows:
            raise RuntimeError(
                f"staging row count mismatch for {month:%Y-%m}: "
                f"expected {expected_rows}, read {rows_read}, staged {staged_rows}"
            )

        partition = month.isoformat()
        client.command(
            f"ALTER TABLE {target} REPLACE PARTITION '{partition}' FROM {stage}"
        )
        return staged_rows, optional_columns_missing
    finally:
        try:
            if stage_created:
                client.command(f"DROP TABLE IF EXISTS {stage}")
        finally:
            client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--month", required=True, type=parse_month, help="month as YYYY-MM")
    args = parser.parse_args(argv)

    started_at = time.monotonic()
    try:
        rows_loaded, optional_columns_missing = load_month(args.month)
    except Exception as exc:
        print(f"Load failed for {args.month:%Y-%m}: {exc}", file=sys.stderr)
        return 1

    elapsed = time.monotonic() - started_at
    print(
        f"source_month={args.month:%Y-%m} "
        f"rows_read={rows_loaded} accepted={rows_loaded} rejected=0 "
        f"rows_loaded={rows_loaded} elapsed_seconds={elapsed:.2f}"
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
