-- Result grain: pickup borough for eligible trips in the selected source month.
-- Date range: NYC-local [2026-09-01, 2026-10-01); edit the literals below.
-- Eligible duration: positive pickup-to-dropoff interval no longer than 24 hours.
-- Recorded card tips include only payment_type = 1; cash tips are not reported by TLC.
-- Recorded tip rate = card tips / eligible positive fares for payment_type = 1.
WITH eligible_trips AS
(
    SELECT
        if(pickup_zone.location_id = 0, 'Unknown', pickup_zone.borough) AS pickup_borough,
        t.fare_amount,
        t.tip_amount,
        t.payment_type_id,
        dateDiff('millisecond', t.pickup_datetime, t.dropoff_datetime) / 1000.0
            AS duration_seconds
    FROM trips AS t
    ANY LEFT JOIN
    (
        SELECT location_id, any(borough) AS borough
        FROM taxi_zones
        GROUP BY location_id
    ) AS pickup_zone
        ON t.pickup_location_id = pickup_zone.location_id
    WHERE t.source_month = toDate('2026-09-01')
      AND t.pickup_datetime >= toDateTime64('2026-09-01 00:00:00', 6, 'America/New_York')
      AND t.pickup_datetime < toDateTime64('2026-10-01 00:00:00', 6, 'America/New_York')
      AND isNotNull(t.pickup_datetime)
      AND isNotNull(t.dropoff_datetime)
      AND t.dropoff_datetime > t.pickup_datetime
      AND t.dropoff_datetime <= t.pickup_datetime + INTERVAL 24 HOUR
)
SELECT
    pickup_borough,
    count() AS eligible_trip_count,
    round(avgIf(fare_amount, isNotNull(fare_amount)), 2) AS average_fare_amount,
    round(sumIf(tip_amount, payment_type_id = 1 AND isNotNull(tip_amount)), 2)
        AS recorded_card_tips_total,
    round(
        sumIf(tip_amount, payment_type_id = 1 AND isNotNull(tip_amount))
        / nullIf(
            sumIf(fare_amount, payment_type_id = 1 AND fare_amount > 0),
            0
        ),
        4
    ) AS recorded_card_tip_rate,
    round(quantileExact(0.5)(duration_seconds), 2) AS median_trip_duration_seconds
FROM eligible_trips
GROUP BY pickup_borough
ORDER BY eligible_trip_count DESC, pickup_borough;
