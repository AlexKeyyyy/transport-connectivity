CREATE TABLE IF NOT EXISTS graph_snapshot (
    graph_snapshot_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    methodology_version TEXT NOT NULL,
    parameters JSONB NOT NULL,
    is_directed BOOLEAN NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('building', 'ready', 'failed')),
    code_commit TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS graph_snapshot_input (
    graph_snapshot_id UUID NOT NULL REFERENCES graph_snapshot(graph_snapshot_id) ON DELETE CASCADE,
    snapshot_id TEXT NOT NULL REFERENCES data_snapshot(snapshot_id),
    load_run_id UUID NOT NULL REFERENCES load_run(load_run_id),
    PRIMARY KEY (graph_snapshot_id, snapshot_id)
);

CREATE TABLE IF NOT EXISTS graph_node (
    graph_snapshot_id UUID NOT NULL REFERENCES graph_snapshot(graph_snapshot_id) ON DELETE CASCADE,
    city_id UUID NOT NULL REFERENCES city(city_id),
    PRIMARY KEY (graph_snapshot_id, city_id)
);

CREATE TABLE IF NOT EXISTS graph_edge (
    graph_edge_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    graph_snapshot_id UUID NOT NULL REFERENCES graph_snapshot(graph_snapshot_id) ON DELETE CASCADE,
    from_city_id UUID NOT NULL,
    to_city_id UUID NOT NULL,
    transport_type TEXT NOT NULL CHECK (transport_type IN ('road', 'rail', 'air', 'multimodal')),
    weight NUMERIC NOT NULL CHECK (weight > 0),
    weight_unit TEXT NOT NULL CHECK (weight_unit IN ('sec', 'm')),
    measurement_id UUID REFERENCES route_measurement(measurement_id),
    UNIQUE (graph_snapshot_id, from_city_id, to_city_id, transport_type),
    CONSTRAINT chk_graph_edge_different_cities CHECK (from_city_id <> to_city_id),
    CONSTRAINT fk_graph_edge_from_node
        FOREIGN KEY (graph_snapshot_id, from_city_id)
        REFERENCES graph_node(graph_snapshot_id, city_id),
    CONSTRAINT fk_graph_edge_to_node
        FOREIGN KEY (graph_snapshot_id, to_city_id)
        REFERENCES graph_node(graph_snapshot_id, city_id)
);

CREATE TABLE IF NOT EXISTS analysis_run (
    analysis_run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    graph_snapshot_id UUID REFERENCES graph_snapshot(graph_snapshot_id),
    snapshot_id TEXT REFERENCES data_snapshot(snapshot_id),
    algorithm TEXT NOT NULL,
    engine TEXT NOT NULL CHECK (engine IN ('python', 'neo4j-gds')),
    engine_version TEXT NOT NULL,
    parameters JSONB NOT NULL,
    scenario JSONB,
    code_commit TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'ready', 'failed')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    CONSTRAINT chk_analysis_run_source CHECK (
        (graph_snapshot_id IS NULL) <> (snapshot_id IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS metric_result (
    metric_result_id BIGSERIAL PRIMARY KEY,
    analysis_run_id UUID NOT NULL REFERENCES analysis_run(analysis_run_id) ON DELETE CASCADE,
    metric TEXT NOT NULL,
    city_id UUID REFERENCES city(city_id),
    other_city_id UUID REFERENCES city(city_id),
    value NUMERIC,
    UNIQUE NULLS NOT DISTINCT (analysis_run_id, metric, city_id, other_city_id)
);

CREATE INDEX IF NOT EXISTS idx_graph_snapshot_input_snapshot ON graph_snapshot_input(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_graph_snapshot_input_load_run ON graph_snapshot_input(load_run_id);

CREATE INDEX IF NOT EXISTS idx_graph_node_city ON graph_node(city_id);

CREATE INDEX IF NOT EXISTS idx_graph_edge_from_node ON graph_edge(graph_snapshot_id, from_city_id);
CREATE INDEX IF NOT EXISTS idx_graph_edge_to_node ON graph_edge(graph_snapshot_id, to_city_id);
CREATE INDEX IF NOT EXISTS idx_graph_edge_measurement ON graph_edge(measurement_id);

CREATE INDEX IF NOT EXISTS idx_analysis_run_graph_snapshot ON analysis_run(graph_snapshot_id);
CREATE INDEX IF NOT EXISTS idx_analysis_run_snapshot ON analysis_run(snapshot_id);

CREATE INDEX IF NOT EXISTS idx_metric_result_analysis_run ON metric_result(analysis_run_id);
CREATE INDEX IF NOT EXISTS idx_metric_result_city ON metric_result(city_id);
CREATE INDEX IF NOT EXISTS idx_metric_result_other_city ON metric_result(other_city_id);
CREATE INDEX IF NOT EXISTS idx_metric_result_metric_city ON metric_result(metric, city_id);