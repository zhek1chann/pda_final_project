"""Download and read the official NYC TLC taxi zone lookup."""

from __future__ import annotations

import csv
import os
import tempfile
import urllib.request
from pathlib import Path

ZONE_LOOKUP_URL = "https://d37ci6vzurychx.cloudfront.net/misc/taxi_zone_lookup.csv"


def download_zone_lookup(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = cache_dir / "taxi_zone_lookup.csv"
    if destination.exists():
        return destination

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=cache_dir,
            prefix=".taxi_zone_lookup.",
            suffix=".part",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            with urllib.request.urlopen(ZONE_LOOKUP_URL, timeout=60) as response:
                while chunk := response.read(64 * 1024):
                    temporary_file.write(chunk)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, destination)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    return destination


def read_zone_rows(path: Path) -> list[tuple[int, str, str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as zone_file:
        reader = csv.DictReader(zone_file)
        required = {"LocationID", "Borough", "Zone", "service_zone"}
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(
                "taxi zone lookup is missing required column(s): "
                + ", ".join(sorted(missing))
            )
        return [
            (
                int(row["LocationID"]),
                row["Borough"],
                row["Zone"],
                row["service_zone"],
            )
            for row in reader
        ]
