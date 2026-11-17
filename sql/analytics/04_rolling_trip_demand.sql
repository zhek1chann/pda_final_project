-- Result grain: one row per NYC-local calendar date in the selected month.
-- Display range: [2026-09-01, 2026-10-01); edit month_start below.
-- Demand: trips by pickup date. The rolling average uses seven calendar dates,
-- including the six dates immediately before the displayed month. Those dates
-- are read from their own source-month partitions, so load the preceding month.
WITH
    toDate('2026-09-01') AS month_start,
    addMonths(month_start, 1) AS month_end,
    calendar AS
    (
        SELECT addDays(addDays(month_start, -6), number) AS pickup_date
        FROM numbers(toUInt64(dateDiff('day', addDays(month_start, -6), month_end)))
    ),
    daily_counts AS
    (
        SELECT
            toDate(pickup_datetime, 'America/New_York') AS pickup_date,
            count() AS trip_count
        FROM trips
        WHERE pickup_datetime >= toDateTime64(
              addDays(month_start, -6), 6, 'America/New_York'
          )
          AND pickup_datetime < toDateTime64(month_end, 6, 'America/New_York')
        GROUP BY pickup_date
    ),
    complete_calendar AS
    (
        SELECT
            calendar.pickup_date,
            ifNull(daily_counts.trip_count, 0) AS trip_count
        FROM calendar
        LEFT JOIN daily_counts USING (pickup_date)
    )
SELECT
    pickup_date,
    trip_count,
    round(
        avg(trip_count) OVER
        (
            ORDER BY pickup_date
            ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
        ),
        2
    ) AS seven_calendar_day_average
FROM complete_calendar
WHERE pickup_date >= month_start
  AND pickup_date < month_end
ORDER BY pickup_date;
