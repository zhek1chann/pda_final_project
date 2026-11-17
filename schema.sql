CREATE TABLE IF NOT EXISTS trips
(
    vendor_id Nullable(Int16),
    pickup_datetime Nullable(DateTime64(6, 'America/New_York')),
    dropoff_datetime Nullable(DateTime64(6, 'America/New_York')),
    passenger_count Nullable(Int16),
    trip_distance Nullable(Float64),
    rate_code_id Nullable(Int16),
    store_and_fwd_flag Nullable(String),
    pickup_location_id Nullable(Int32),
    dropoff_location_id Nullable(Int32),
    payment_type_id Nullable(Int16),
    fare_amount Nullable(Float64),
    tip_amount Nullable(Float64),
    tolls_amount Nullable(Float64),
    extra Nullable(Float64),
    mta_tax Nullable(Float64),
    improvement_surcharge Nullable(Float64),
    congestion_surcharge Nullable(Float64),
    airport_fee Nullable(Float64),
    cbd_congestion_fee Nullable(Float64),
    request_source Nullable(String),
    total_amount Nullable(Float64),
    source_month Date,
    source_file String
)
ENGINE = MergeTree
PARTITION BY source_month
ORDER BY
(
    source_month,
    ifNull(
        pickup_datetime,
        toDateTime64('1970-01-01 00:00:00', 6, 'America/New_York')
    ),
    ifNull(pickup_location_id, toInt32(0))
);

CREATE TABLE IF NOT EXISTS taxi_zones
(
    location_id Int32,
    borough String,
    zone String,
    service_zone String
)
ENGINE = MergeTree
ORDER BY location_id;
