CREATE TABLE IF NOT EXISTS city (
    city_id UUID PRIMARY KEY,
    name TEXT,
    region TEXT,
    population INTEGER CHECK (population >= 0),
    location GEOMETRY(Point, 4326),
    attributes_snapshot_id TEXT NOT NULL REFERENCES data_snapshot(snapshot_id)
);

CREATE TABLE IF NOT EXISTS city_external_id (
    city_id UUID NOT NULL REFERENCES city(city_id) ON DELETE CASCADE,
    id_namespace TEXT NOT NULL,
    external_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL REFERENCES data_snapshot(snapshot_id),
    PRIMARY KEY (id_namespace, external_id),
    UNIQUE (city_id, id_namespace)
);

CREATE TABLE IF NOT EXISTS route_measurement (
    measurement_id UUID PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES data_snapshot(snapshot_id),
    source_row_number INTEGER NOT NULL,
    from_city_id UUID NOT NULL REFERENCES city(city_id),
    to_city_id UUID NOT NULL REFERENCES city(city_id),
    transport_type TEXT NOT NULL CHECK (transport_type IN ('road', 'rail', 'air')),
    distance_m NUMERIC CHECK (distance_m > 0),
    duration_sec NUMERIC CHECK (duration_sec > 0),
    missing_reason_raw TEXT,
    source_raw TEXT,
    comment_raw TEXT,
    UNIQUE (snapshot_id, source_row_number),
    UNIQUE (snapshot_id, from_city_id, to_city_id, transport_type),
    CONSTRAINT chk_different_cities CHECK (from_city_id <> to_city_id),
    CONSTRAINT chk_metrics_both_or_none CHECK (
        (distance_m IS NULL AND duration_sec IS NULL AND missing_reason_raw IS NOT NULL)
        OR
        (distance_m IS NOT NULL AND duration_sec IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_city_location ON city USING GIST (location);
CREATE INDEX IF NOT EXISTS idx_city_attributes_snapshot ON city(attributes_snapshot_id);
CREATE INDEX IF NOT EXISTS idx_city_external_id_snapshot ON city_external_id(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_route_measurement_from ON route_measurement(from_city_id);
CREATE INDEX IF NOT EXISTS idx_route_measurement_to ON route_measurement(to_city_id);