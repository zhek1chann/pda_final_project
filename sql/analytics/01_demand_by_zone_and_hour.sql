-- Result grain: pickup zone and NYC-local pickup hour for the selected source month.
-- Demand: number of trips. Unknown or unmatched pickup zones remain as "Unknown".
-- The source month is based on the TLC file; date boundaries are NYC-local timestamps.
-- Date range: [2026-09-01, 2026-10-01); edit the literals in the WITH clause.
WITH
    toDate('2026-09-01') AS selected_month,
    toDateTime64('2026-09-01 00:00:00', 6, 'America/New_York') AS start_local,
    toDateTime64('2026-10-01 00:00:00', 6, 'America/New_York') AS end_local
SELECT
    ifNull(t.pickup_location_id, 0) AS pickup_location_id,
    if(pickup_zone.location_id = 0, 'Unknown', pickup_zone.zone) AS pickup_zone,
    toHour(t.pickup_datetime, 'America/New_York') AS pickup_hour_local,
    count() AS trip_count
FROM trips AS t
ANY LEFT JOIN
(
    SELECT location_id, any(zone) AS zone
    FROM taxi_zones
    GROUP BY location_id
) AS pickup_zone
    ON t.pickup_location_id = pickup_zone.location_id
WHERE t.source_month = selected_month
  AND t.pickup_datetime >= start_local
  AND t.pickup_datetime < end_local
GROUP BY
    pickup_location_id,
    pickup_zone,
    pickup_hour_local
ORDER BY trip_count DESC, pickup_location_id, pickup_hour_local;
