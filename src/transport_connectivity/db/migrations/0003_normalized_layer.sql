CREATE TABLE IF NOT EXISTS city (
    city_id UUID PRIMARY KEY,
    name TEXT NOT NULL,
    region TEXT NOT NULL,
    oktmo TEXT,
    is_capital BOOLEAN NOT NULL,
    population INTEGER NOT NULL CHECK (population >= 0),
    location GEOMETRY(Point, 4326),
    attributes_snapshot_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS city_external_id (
    id SERIAL PRIMARY KEY,
    city_id UUID NOT NULL REFERENCES city(city_id) ON DELETE CASCADE,
    id_namespace TEXT NOT NULL,
    external_id TEXT NOT NULL,
    UNIQUE (id_namespace, external_id)
);

CREATE TABLE IF NOT EXISTS route_measurement (
    measurement_id UUID PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES data_snapshot(snapshot_id),
    from_city_id UUID NOT NULL REFERENCES city(city_id),
    to_city_id UUID NOT NULL REFERENCES city(city_id),
    transport_type TEXT NOT NULL CHECK (transport_type IN ('road', 'rail', 'air')),
    distance_m NUMERIC CHECK (distance_m > 0),
    duration_sec NUMERIC CHECK (duration_sec > 0),
    missing_reason TEXT,
    source TEXT,
    comment TEXT,
    CONSTRAINT chk_different_cities CHECK (from_city_id <> to_city_id),
    CONSTRAINT chk_metrics_both_or_none CHECK (
        (distance_m IS NULL AND duration_sec IS NULL AND missing_reason IS NOT NULL)
        OR
        (distance_m IS NOT NULL AND duration_sec IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_city_location ON city USING GIST (location);
CREATE INDEX IF NOT EXISTS idx_city_external_id_city ON city_external_id(city_id);
CREATE INDEX IF NOT EXISTS idx_route_measurement_snapshot ON route_measurement(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_route_measurement_from ON route_measurement(from_city_id);
CREATE INDEX IF NOT EXISTS idx_route_measurement_to ON route_measurement(to_city_id);