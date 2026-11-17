-- Result grain: one row per pickup zone for the selected source month.
-- ROW_NUMBER chooses a deterministic top-N; RANK preserves ties in trip demand.
-- Unknown pickup IDs are retained as 0 and labeled "Unknown".
-- Date range: NYC-local [2026-09-01, 2026-10-01); edit the literals below.
WITH zone_counts AS
(
    SELECT
        ifNull(t.pickup_location_id, 0) AS pickup_location_id,
        if(pickup_zone.location_id = 0, 'Unknown', pickup_zone.zone) AS pickup_zone,
        count() AS trip_count
    FROM trips AS t
    ANY LEFT JOIN
    (
        SELECT location_id, any(zone) AS zone
        FROM taxi_zones
        GROUP BY location_id
    ) AS pickup_zone
        ON t.pickup_location_id = pickup_zone.location_id
    WHERE t.source_month = toDate('2026-09-01')
      AND t.pickup_datetime >= toDateTime64('2026-09-01 00:00:00', 6, 'America/New_York')
      AND t.pickup_datetime < toDateTime64('2026-10-01 00:00:00', 6, 'America/New_York')
    GROUP BY pickup_location_id, pickup_zone
),
ranked_zones AS
(
    SELECT
        *,
        row_number() OVER
        (
            ORDER BY trip_count DESC, isNull(pickup_location_id), pickup_location_id
        ) AS row_number_by_demand,
        rank() OVER (ORDER BY trip_count DESC) AS demand_rank
    FROM zone_counts
)
SELECT
    pickup_location_id,
    pickup_zone,
    trip_count,
    row_number_by_demand,
    demand_rank
FROM ranked_zones
WHERE row_number_by_demand <= 20
ORDER BY row_number_by_demand;
