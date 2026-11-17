"""Shared TLC Parquet field mapping and normalization."""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import pyarrow as pa

BATCH_SIZE = 65_536
NYC_TIMEZONE = ZoneInfo("America/New_York")

# Source name, warehouse name, value kind. Optional fields absent in a month
# become NULL; only the timestamps and location IDs are required by the model.
FIELD_SPECS = (
    ("VendorID", "vendor_id", "integer"),
    ("tpep_pickup_datetime", "pickup_datetime", "datetime"),
    ("tpep_dropoff_datetime", "dropoff_datetime", "datetime"),
    ("passenger_count", "passenger_count", "integer"),
    ("trip_distance", "trip_distance", "float"),
    ("RatecodeID", "rate_code_id", "integer"),
    ("store_and_fwd_flag", "store_and_fwd_flag", "string"),
    ("PULocationID", "pickup_location_id", "integer"),
    ("DOLocationID", "dropoff_location_id", "integer"),
    ("payment_type", "payment_type_id", "integer"),
    ("fare_amount", "fare_amount", "float"),
    ("tip_amount", "tip_amount", "float"),
    ("tolls_amount", "tolls_amount", "float"),
    ("extra", "extra", "float"),
    ("mta_tax", "mta_tax", "float"),
    ("improvement_surcharge", "improvement_surcharge", "float"),
    ("congestion_surcharge", "congestion_surcharge", "float"),
    ("airport_fee", "airport_fee", "float"),
    ("cbd_congestion_fee", "cbd_congestion_fee", "float"),
    ("request_source", "request_source", "string"),
    ("total_amount", "total_amount", "float"),
)
SOURCE_ALIASES = {"airport_fee": ("Airport_fee",)}
REQUIRED_SOURCE_COLUMNS = (
    "tpep_pickup_datetime",
    "tpep_dropoff_datetime",
    "PULocationID",
    "DOLocationID",
)
SOURCE_TO_TARGET = {source: target for source, target, _kind in FIELD_SPECS}
TARGET_COLUMNS = tuple(target for _source, target, _kind in FIELD_SPECS) + (
    "source_month",
    "source_file",
)
OPTIONAL_SOURCE_COLUMNS = tuple(
    source for source, _target, _kind in FIELD_SPECS
    if source not in REQUIRED_SOURCE_COLUMNS
)


def validate_source_schema(schema: pa.Schema) -> tuple[str, ...]:
    available = set(schema.names)
    missing = [column for column in REQUIRED_SOURCE_COLUMNS if column not in available]
    if missing:
        raise ValueError(
            "source Parquet file is missing required column(s): "
            + ", ".join(missing)
        )
    return tuple(
        source
        for source in OPTIONAL_SOURCE_COLUMNS
        if not any(
            candidate in available
            for candidate in (source, *SOURCE_ALIASES.get(source, ()))
        )
    )


def source_columns_for_schema(schema: pa.Schema) -> tuple[str, ...]:
    """Return recognized physical Parquet names in warehouse field order."""
    available = set(schema.names)
    columns = []
    for source, _target, _kind in FIELD_SPECS:
        matched = next(
            (
                candidate
                for candidate in (source, *SOURCE_ALIASES.get(source, ()))
                if candidate in available
            ),
            None,
        )
        if matched is not None:
            columns.append(matched)
    return tuple(columns)


def _nullable_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return int(value)


def _nullable_float(value: Any) -> float | None:
    if value is None:
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _nullable_datetime(value: Any, *, postgres: bool) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise ValueError(f"expected a Parquet timestamp, got {type(value).__name__}")
    if value.tzinfo is None:
        return value if postgres else value.replace(tzinfo=NYC_TIMEZONE)
    local_value = value.astimezone(NYC_TIMEZONE)
    return local_value.replace(tzinfo=None) if postgres else local_value


def _normalize(value: Any, kind: str, *, postgres: bool) -> Any:
    if kind == "integer":
        return _nullable_int(value)
    if kind == "float":
        return _nullable_float(value)
    if kind == "datetime":
        return _nullable_datetime(value, postgres=postgres)
    if kind == "string":
        if value is None:
            return None
        text = str(value).strip()
        return text or None
    raise ValueError(f"unsupported source field kind: {kind}")


def iter_mapped_records(
    batch: pa.RecordBatch,
    source_month: date,
    source_file: str,
    *,
    postgres: bool = False,
) -> Iterator[tuple[Any, ...]]:
    """Yield records in TARGET_COLUMNS order using memory bounded to one batch."""
    available = set(batch.schema.names)
    values_by_target: dict[str, list[Any]] = {}
    for source, target, _kind in FIELD_SPECS:
        matched = next(
            (
                candidate
                for candidate in (source, *SOURCE_ALIASES.get(source, ()))
                if candidate in available
            ),
            None,
        )
        values_by_target[target] = (
            batch.column(batch.schema.get_field_index(matched)).to_pylist()
            if matched is not None
            else [None] * batch.num_rows
        )

    for row_index in range(batch.num_rows):
        values = [
            _normalize(values_by_target[target][row_index], kind, postgres=postgres)
            for _source, target, kind in FIELD_SPECS
        ]
        values.extend((source_month, source_file))
        yield tuple(values)
