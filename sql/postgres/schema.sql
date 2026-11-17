CREATE SCHEMA IF NOT EXISTS reference;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS etl;
CREATE SCHEMA IF NOT EXISTS analytics;

CREATE TABLE IF NOT EXISTS reference.taxi_zones
(
    location_id INTEGER PRIMARY KEY,
    borough TEXT NOT NULL,
    zone TEXT NOT NULL,
    service_zone TEXT
);

CREATE TABLE IF NOT EXISTS reference.vendors
(
    vendor_id SMALLINT PRIMARY KEY,
    vendor_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reference.rate_codes
(
    rate_code_id SMALLINT PRIMARY KEY,
    rate_description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reference.payment_types
(
    payment_type_id SMALLINT PRIMARY KEY,
    payment_description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS etl.source_files
(
    source_file_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_month DATE NOT NULL UNIQUE,
    source_url TEXT NOT NULL,
    source_file_name TEXT NOT NULL,
    rows_read BIGINT NOT NULL CHECK (rows_read >= 0),
    accepted_rows BIGINT NOT NULL CHECK (accepted_rows >= 0),
    rejected_rows BIGINT NOT NULL CHECK (rejected_rows >= 0),
    loaded_rows BIGINT NOT NULL CHECK (loaded_rows >= 0),
    load_duration_seconds NUMERIC(12, 3) NOT NULL CHECK (load_duration_seconds >= 0),
    status TEXT NOT NULL CHECK (status IN ('loaded')),
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (rows_read = accepted_rows + rejected_rows),
    CHECK (accepted_rows = loaded_rows)
);

CREATE TABLE IF NOT EXISTS etl.load_attempts
(
    load_attempt_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_month DATE NOT NULL,
    source_url TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'loaded', 'failed')),
    rows_read BIGINT NOT NULL DEFAULT 0 CHECK (rows_read >= 0),
    rows_loaded BIGINT NOT NULL DEFAULT 0 CHECK (rows_loaded >= 0),
    duration_seconds NUMERIC(12, 3) NOT NULL DEFAULT 0
        CHECK (duration_seconds >= 0),
    error_message TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    CHECK (rows_loaded <= rows_read)
);

CREATE TABLE IF NOT EXISTS core.trips
(
    trip_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_file_id BIGINT NOT NULL
        REFERENCES etl.source_files(source_file_id) ON DELETE CASCADE,
    source_row_number BIGINT NOT NULL CHECK (source_row_number > 0),
    vendor_id SMALLINT REFERENCES reference.vendors(vendor_id),
    pickup_datetime TIMESTAMP WITHOUT TIME ZONE,
    dropoff_datetime TIMESTAMP WITHOUT TIME ZONE,
    passenger_count SMALLINT,
    trip_distance NUMERIC(14, 6),
    rate_code_id SMALLINT REFERENCES reference.rate_codes(rate_code_id),
    store_and_fwd_flag TEXT,
    pickup_location_id INTEGER REFERENCES reference.taxi_zones(location_id),
    dropoff_location_id INTEGER REFERENCES reference.taxi_zones(location_id),
    payment_type_id SMALLINT REFERENCES reference.payment_types(payment_type_id),
    fare_amount NUMERIC(14, 2),
    tip_amount NUMERIC(14, 2),
    tolls_amount NUMERIC(14, 2),
    extra NUMERIC(14, 2),
    mta_tax NUMERIC(14, 2),
    improvement_surcharge NUMERIC(14, 2),
    congestion_surcharge NUMERIC(14, 2),
    airport_fee NUMERIC(14, 2),
    cbd_congestion_fee NUMERIC(14, 2),
    request_source TEXT,
    total_amount NUMERIC(14, 2),
    UNIQUE (source_file_id, source_row_number)
);

ALTER TABLE core.trips
    ADD COLUMN IF NOT EXISTS cbd_congestion_fee NUMERIC(14, 2);
ALTER TABLE core.trips
    ADD COLUMN IF NOT EXISTS request_source TEXT;

CREATE INDEX IF NOT EXISTS trips_pickup_datetime_idx
    ON core.trips (pickup_datetime);
CREATE INDEX IF NOT EXISTS trips_source_file_idx
    ON core.trips (source_file_id);
CREATE INDEX IF NOT EXISTS trips_pickup_zone_datetime_idx
    ON core.trips (pickup_location_id, pickup_datetime);

CREATE TABLE IF NOT EXISTS analytics.daily_trips
(
    pickup_date DATE PRIMARY KEY,
    trip_count BIGINT NOT NULL,
    fare_amount_total NUMERIC(18, 2) NOT NULL,
    recorded_card_tips_total NUMERIC(18, 2) NOT NULL,
    median_valid_trip_duration_seconds NUMERIC(14, 2)
);

CREATE TABLE IF NOT EXISTS analytics.daily_pickup_zones
(
    pickup_date DATE NOT NULL,
    pickup_location_id INTEGER NOT NULL
        REFERENCES reference.taxi_zones(location_id),
    trip_count BIGINT NOT NULL,
    PRIMARY KEY (pickup_date, pickup_location_id)
);
