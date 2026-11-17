#!/usr/bin/env python3
"""Build three self-contained Plotly HTML charts from ClickHouse."""

from __future__ import annotations

import argparse
import os
from datetime import date
from pathlib import Path
from typing import Any

import clickhouse_connect
import pandas as pd
import plotly.graph_objects as go
from dotenv import load_dotenv
from plotly.subplots import make_subplots

from load_month import parse_month

ROOT = Path(__file__).resolve().parent
TIMEZONE = "America/New_York"


def _client() -> Any:
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


def _bounds(month: date) -> tuple[date, date, str, str]:
    next_month = (
        date(month.year + 1, 1, 1)
        if month.month == 12
        else date(month.year, month.month + 1, 1)
    )
    return (
        month,
        next_month,
        f"{month:%Y-%m-%d}",
        f"{next_month:%Y-%m-%d}",
    )


def _demand_heatmap(client: Any, month: date, output_dir: Path) -> Path:
    _start, _end, start_text, end_text = _bounds(month)
    source_month = f"{month:%Y-%m-%d}"
    rows = client.query(
        f"""
        WITH top_zones AS
        (
            SELECT ifNull(pickup_location_id, 0) AS location_id, count() AS trips
            FROM trips
            WHERE source_month = toDate('{source_month}')
              AND pickup_datetime >= toDateTime64(
                  '{start_text} 00:00:00', 6, '{TIMEZONE}'
              )
              AND pickup_datetime < toDateTime64(
                  '{end_text} 00:00:00', 6, '{TIMEZONE}'
              )
            GROUP BY location_id
            ORDER BY trips DESC, location_id
            LIMIT 20
        )
        SELECT
            ifNull(t.pickup_location_id, 0) AS location_id,
            if(z.location_id = 0, 'Unknown', z.borough) AS borough,
            if(z.location_id = 0, 'Unknown zone', z.zone) AS zone,
            toHour(t.pickup_datetime, '{TIMEZONE}') AS pickup_hour,
            count() AS trip_count
        FROM trips AS t
        ANY LEFT JOIN
        (
            SELECT location_id, any(borough) AS borough, any(zone) AS zone
            FROM taxi_zones
            GROUP BY location_id
        ) AS z ON t.pickup_location_id = z.location_id
        INNER JOIN top_zones AS busiest
            ON ifNull(t.pickup_location_id, 0) = busiest.location_id
        WHERE t.source_month = toDate('{source_month}')
          AND t.pickup_datetime >= toDateTime64(
              '{start_text} 00:00:00', 6, '{TIMEZONE}'
          )
          AND t.pickup_datetime < toDateTime64(
              '{end_text} 00:00:00', 6, '{TIMEZONE}'
          )
        GROUP BY location_id, borough, zone, pickup_hour
        ORDER BY location_id, pickup_hour
        """
    ).result_rows
    frame = pd.DataFrame(
        rows,
        columns=["location_id", "borough", "zone", "pickup_hour", "trip_count"],
    )
    output_path = output_dir / "demand_by_zone_and_hour.html"
    if frame.empty:
        figure = go.Figure()
        figure.update_layout(title="No trips found for the selected month")
    else:
        frame["zone_label"] = (
            frame["borough"]
            + " — "
            + frame["zone"]
            + " ("
            + frame["location_id"].astype(int).astype(str)
            + ")"
        )
        zone_order = (
            frame.groupby("zone_label", as_index=False)["trip_count"]
            .sum()
            .sort_values(["trip_count", "zone_label"], ascending=[False, True])
            ["zone_label"]
            .tolist()
        )
        pivot = frame.pivot_table(
            index="zone_label",
            columns="pickup_hour",
            values="trip_count",
            aggfunc="sum",
            fill_value=0,
        ).reindex(index=zone_order, columns=range(24), fill_value=0)
        figure = go.Figure(
            go.Heatmap(
                z=pivot.to_numpy(),
                x=list(range(24)),
                y=list(pivot.index),
                colorscale="Viridis",
                colorbar={"title": "Trips"},
                hovertemplate=(
                    "%{y}<br>NYC local hour %{x}:00<br>"
                    "Trips %{z}<extra></extra>"
                ),
            )
        )
        figure.update_layout(
            title=f"Pickup demand by zone and NYC local hour — {month:%Y-%m}",
            xaxis_title="Pickup hour (America/New_York)",
            yaxis_title="Busiest pickup zones",
            height=max(600, len(zone_order) * 28),
        )
    figure.write_html(output_path, include_plotlyjs=True, full_html=True)
    return output_path


def _daily_volume(client: Any, month: date, output_dir: Path) -> Path:
    start_date, end_date, _start_text, end_text = _bounds(month)
    window_start = (pd.Timestamp(start_date) - pd.Timedelta(days=6)).date()
    rows = client.query(
        f"""
        SELECT toDate(pickup_datetime, '{TIMEZONE}') AS pickup_date, count() AS trips
        FROM trips
        WHERE pickup_datetime >= toDateTime64(
                  '{window_start:%Y-%m-%d} 00:00:00', 6, '{TIMEZONE}'
              )
          AND pickup_datetime < toDateTime64(
                  '{end_text} 00:00:00', 6, '{TIMEZONE}'
              )
        GROUP BY pickup_date
        ORDER BY pickup_date
        """
    ).result_rows
    calendar = pd.date_range(window_start, end_date - pd.Timedelta(days=1), freq="D")
    frame = pd.DataFrame(rows, columns=["pickup_date", "trip_count"])
    if frame.empty:
        daily = pd.Series(0, index=calendar, dtype="int64")
    else:
        frame["pickup_date"] = pd.to_datetime(frame["pickup_date"])
        daily = (
            frame.set_index("pickup_date")["trip_count"]
            .reindex(calendar, fill_value=0)
            .astype("int64")
        )
    rolling = daily.rolling(window=7, min_periods=7).mean()
    display_mask = (daily.index.date >= start_date) & (daily.index.date < end_date)
    figure = go.Figure()
    figure.add_trace(
        go.Bar(x=daily.index[display_mask], y=daily.loc[display_mask], name="Daily trips")
    )
    figure.add_trace(
        go.Scatter(
            x=rolling.index[display_mask],
            y=rolling.loc[display_mask],
            name="Trailing 7-calendar-day average",
            mode="lines",
        )
    )
    figure.update_layout(
        title=f"Daily pickup trip volume — {month:%Y-%m}",
        xaxis_title="NYC local pickup date",
        yaxis_title="Trips",
        hovermode="x unified",
    )
    output_path = output_dir / "daily_trip_volume.html"
    figure.write_html(output_path, include_plotlyjs=True, full_html=True)
    return output_path


def _fare_tip_duration(client: Any, month: date, output_dir: Path) -> Path:
    _start, _end, start_text, end_text = _bounds(month)
    source_month = f"{month:%Y-%m-%d}"
    rows = client.query(
        f"""
        WITH eligible AS
        (
            SELECT
                if(z.location_id = 0, 'Unknown', z.borough) AS pickup_borough,
                t.fare_amount,
                t.tip_amount,
                t.payment_type_id,
                dateDiff(
                    'millisecond', t.pickup_datetime, t.dropoff_datetime
                ) / 1000.0 AS duration_seconds
            FROM trips AS t
            ANY LEFT JOIN
            (
                SELECT location_id, any(borough) AS borough
                FROM taxi_zones
                GROUP BY location_id
            ) AS z ON t.pickup_location_id = z.location_id
            WHERE t.source_month = toDate('{source_month}')
              AND t.pickup_datetime >= toDateTime64(
                  '{start_text} 00:00:00', 6, '{TIMEZONE}'
              )
              AND t.pickup_datetime < toDateTime64(
                  '{end_text} 00:00:00', 6, '{TIMEZONE}'
              )
              AND isNotNull(t.pickup_datetime)
              AND isNotNull(t.dropoff_datetime)
              AND t.dropoff_datetime > t.pickup_datetime
              AND t.dropoff_datetime <= t.pickup_datetime + INTERVAL 24 HOUR
        )
        SELECT
            pickup_borough,
            avgIf(fare_amount, isNotNull(fare_amount)) AS mean_fare_usd,
            avgIf(
                tip_amount, payment_type_id = 1 AND isNotNull(tip_amount)
            ) AS mean_recorded_card_tip_usd,
            quantileExact(0.5)(duration_seconds) AS median_duration_seconds
        FROM eligible
        GROUP BY pickup_borough
        ORDER BY pickup_borough
        """
    ).result_rows
    frame = pd.DataFrame(
        rows,
        columns=[
            "pickup_borough",
            "mean_fare_usd",
            "mean_recorded_card_tip_usd",
            "median_duration_seconds",
        ],
    )
    output_path = output_dir / "fare_tip_duration_by_borough.html"
    figure = make_subplots(specs=[[{"secondary_y": True}]])
    if frame.empty:
        figure.update_layout(title="No eligible trips found for the selected month")
    else:
        figure.add_trace(
            go.Bar(
                x=frame["pickup_borough"],
                y=frame["mean_fare_usd"],
                name="Mean fare (USD)",
            ),
            secondary_y=False,
        )
        figure.add_trace(
            go.Bar(
                x=frame["pickup_borough"],
                y=frame["mean_recorded_card_tip_usd"],
                name="Mean recorded card tip (USD)",
            ),
            secondary_y=False,
        )
        figure.add_trace(
            go.Scatter(
                x=frame["pickup_borough"],
                y=frame["median_duration_seconds"],
                name="Median eligible trip duration (seconds)",
                mode="lines+markers",
            ),
            secondary_y=True,
        )
        figure.update_layout(
            title=(
                f"Fare, recorded card tips, and trip duration "
                f"by pickup borough — {month:%Y-%m}"
            ),
            barmode="group",
            xaxis_title="Pickup borough",
            legend={"orientation": "h", "y": -0.25},
        )
        figure.update_yaxes(title_text="Mean amount (USD)", secondary_y=False)
        figure.update_yaxes(title_text="Median duration (seconds)", secondary_y=True)
    figure.write_html(output_path, include_plotlyjs=True, full_html=True)
    return output_path


def build_charts(month: date) -> list[Path]:
    output_dir = ROOT / "charts"
    output_dir.mkdir(parents=True, exist_ok=True)
    client = _client()
    try:
        return [
            _demand_heatmap(client, month, output_dir),
            _daily_volume(client, month, output_dir),
            _fare_tip_duration(client, month, output_dir),
        ]
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--month", type=parse_month, default=parse_month("2026-09"))
    args = parser.parse_args(argv)
    for chart_path in build_charts(args.month):
        print(chart_path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
