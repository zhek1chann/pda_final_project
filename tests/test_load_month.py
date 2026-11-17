import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

import load_month
from trip_fields import FIELD_SPECS, iter_mapped_records, validate_source_schema


class FakeClickHouseClient:
    def __init__(self, reported_rows: int | None = None) -> None:
        self.reported_rows = reported_rows
        self.commands: list[str] = []
        self.inserted_rows: list[tuple] = []
        self.closed = False

    def command(self, query: str) -> None:
        self.commands.append(query)

    def insert(self, table: str, rows: list[tuple], column_names: list[str]) -> None:
        self.inserted_rows.extend(rows)

    def query(self, query: str) -> SimpleNamespace:
        row_count = (
            self.reported_rows
            if self.reported_rows is not None
            else len(self.inserted_rows)
        )
        return SimpleNamespace(result_rows=[(row_count,)])

    def close(self) -> None:
        self.closed = True


class LoadMonthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.parquet_path = Path(self.temp_dir.name) / "sample.parquet"
        pq.write_table(
            pa.table(
                {
                    "tpep_pickup_datetime": pa.array(
                        [datetime(2024, 1, 1, 0, 30), None],
                        type=pa.timestamp("us"),
                    ),
                    "tpep_dropoff_datetime": pa.array(
                        [datetime(2024, 1, 1, 0, 45), None],
                        type=pa.timestamp("us"),
                    ),
                    "PULocationID": pa.array([1, None], type=pa.int64()),
                    "DOLocationID": pa.array([2, None], type=pa.int64()),
                    "trip_distance": pa.array([1.25, None], type=pa.float64()),
                    "total_amount": pa.array([10.5, None], type=pa.float64()),
                }
            ),
            self.parquet_path,
        )
        self.environment = patch.dict(
            os.environ,
            {
                "CLICKHOUSE_DATABASE": "analytics",
                "CLICKHOUSE_USER": "test_user",
                "CLICKHOUSE_PASSWORD": "test_password",
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def run_load(
        self, client: FakeClickHouseClient
    ) -> tuple[int, tuple[str, ...]]:
        with (
            patch.object(load_month, "download_source", return_value=self.parquet_path),
            patch.object(load_month.clickhouse_connect, "get_client", return_value=client),
            patch.object(load_month, "_ensure_zone_lookup", return_value=265),
        ):
            return load_month.load_month(date(2024, 1, 1))

    def test_validated_load_replaces_month_and_preserves_nullable_values(self) -> None:
        client = FakeClickHouseClient()

        rows, optional_columns_missing = self.run_load(client)

        self.assertEqual(rows, 2)
        self.assertIn("VendorID", optional_columns_missing)
        self.assertIn("fare_amount", optional_columns_missing)
        self.assertIn("cbd_congestion_fee", optional_columns_missing)
        self.assertNotIn("trip_distance", optional_columns_missing)
        self.assertEqual(len(client.inserted_rows), 2)
        self.assertIsNone(client.inserted_rows[0][0])
        self.assertIsNone(client.inserted_rows[0][10])
        self.assertEqual(
            client.inserted_rows[0][1].strftime("%Y-%m-%d %H:%M %z"),
            "2024-01-01 00:30 -0500",
        )
        self.assertIsNone(client.inserted_rows[1][1])
        self.assertEqual(client.inserted_rows[1][-2], date(2024, 1, 1))
        replace_commands = [
            command for command in client.commands if "REPLACE PARTITION" in command
        ]
        self.assertEqual(len(replace_commands), 1)
        self.assertIn("PARTITION '2024-01-01'", replace_commands[0])
        self.assertTrue(client.closed)

    def test_row_count_mismatch_does_not_replace_published_partition(self) -> None:
        client = FakeClickHouseClient(reported_rows=1)

        with self.assertRaisesRegex(RuntimeError, "staging row count mismatch"):
            self.run_load(client)

        self.assertFalse(
            any("REPLACE PARTITION" in command for command in client.commands)
        )
        self.assertTrue(any("DROP TABLE IF EXISTS" in command for command in client.commands))
        self.assertTrue(client.closed)

    def test_airport_fee_source_name_variants_are_mapped(self) -> None:
        airport_fee_index = next(
            index
            for index, (_source, target, _kind) in enumerate(FIELD_SPECS)
            if target == "airport_fee"
        )
        for source_column in ("Airport_fee", "airport_fee"):
            with self.subTest(source_column=source_column):
                table = pa.table(
                    {
                        "tpep_pickup_datetime": pa.array(
                            [datetime(2024, 1, 1, 0, 30)], type=pa.timestamp("us")
                        ),
                        "tpep_dropoff_datetime": pa.array(
                            [datetime(2024, 1, 1, 0, 45)], type=pa.timestamp("us")
                        ),
                        "PULocationID": pa.array([1], type=pa.int64()),
                        "DOLocationID": pa.array([2], type=pa.int64()),
                        source_column: pa.array([1.75], type=pa.float64()),
                    }
                )
                batch = table.to_batches()[0]
                optional_columns_missing = validate_source_schema(table.schema)
                record = next(
                    iter_mapped_records(
                        batch, date(2024, 1, 1), "sample.parquet"
                    )
                )

                self.assertNotIn("airport_fee", optional_columns_missing)
                self.assertEqual(record[airport_fee_index], 1.75)


if __name__ == "__main__":
    unittest.main()
