-- Result grain: NYC-local pickup date x pickup location ID for source month 2026-09.
-- Metric: count of trips with a non-NULL pickup timestamp in the editable
-- inclusive/exclusive local date interval. NULL pickup IDs map to 0.
-- Use the same source month, dates, and unknown-zone rule in the notebook.
WITH
    toDate('2026-09-01') AS selected_month,
    toDateTime64('2026-09-01 00:00:00', 6, 'America/New_York') AS start_local,
    toDateTime64('2026-10-01 00:00:00', 6, 'America/New_York') AS end_local
SELECT
    toDate(t.pickup_datetime, 'America/New_York') AS pickup_date,
    ifNull(t.pickup_location_id, toInt32(0)) AS pickup_location_id,
    count() AS trip_count
FROM trips AS t
WHERE t.source_month = selected_month
  AND isNotNull(t.pickup_datetime)
  AND t.pickup_datetime >= start_local
  AND t.pickup_datetime < end_local
GROUP BY pickup_date, pickup_location_id
ORDER BY pickup_date, pickup_location_id;
