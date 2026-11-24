"""Map reader against the same database contract used by #33 and #35."""

from __future__ import annotations

import os

import psycopg
import pytest
from psycopg.types.json import Jsonb
from test_db_accessibility import snapshot as snapshot

from transport_connectivity.app import data
from transport_connectivity.db.accessibility import run_accessibility
from transport_connectivity.db.migrate import get_connection_string

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(os.environ.get("RUN_DB_TESTS") != "1", reason="Set RUN_DB_TESTS=1"),
]


def test_map_reads_published_matrix_and_reference_graph(snapshot, monkeypatch) -> None:
    snapshot_id, city_ids = snapshot
    monkeypatch.setenv("MAP_SNAPSHOT_ID", snapshot_id)
    scope = {"layer": "best", "alpha": "0.05"}

    assert run_accessibility(snapshot_id, code_version="test")["runs_created"] == 12
    assert data.available_layers("db") == ("best", "road", "rail")
    assert (
        run_accessibility(snapshot_id, code_version="test", include_air=True)["runs_created"] == 4
    )
    assert (
        run_accessibility(snapshot_id, code_version="test", include_air=True)["runs_reused"] == 16
    )
    assert data.available_layers("db") == ("best", "road", "rail", "air")
    assert len(data.load_cities("db")) == 10
    assert data.load_travel_times(city_ids[0], "air", "db")[city_ids[2]] == 20800
    assert data.load_travel_times(city_ids[0], "air", "db")[city_ids[1]] is None
    assert (
        data.load_metric(
            "reach_population", {"layer": "best", "threshold_sec": 21600}, source="db"
        )[city_ids[0]]
        == 3000
    )

    scenario = {"type": "remove_node", "name": "Без Москвы", "city_id": city_ids[0]}
    with psycopg.connect(get_connection_string()) as conn:
        graph_id = conn.execute(
            """
            INSERT INTO graph_snapshot (methodology_version, parameters, is_directed,
                                        status, code_commit)
            VALUES ('1.0', %s, true, 'ready', 'test') RETURNING graph_snapshot_id
            """,
            (Jsonb({**scope, "alpha": "0.050"}),),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO graph_snapshot_input (graph_snapshot_id, snapshot_id, load_run_id)
            SELECT %s, snapshot_id, load_run_id FROM load_run WHERE snapshot_id = %s
            """,
            (graph_id, snapshot_id),
        )
        for threshold in (10800, 21600, 86400):
            run_id = conn.execute(
                """
                INSERT INTO analysis_run (graph_snapshot_id, algorithm, engine, engine_version,
                                          parameters, scenario, code_commit, status)
                VALUES (%s, 'scenario', 'python', 'test', %s, %s, 'test', 'ready')
                RETURNING analysis_run_id
                """,
                (graph_id, Jsonb({**scope, "threshold_sec": threshold}), Jsonb(scenario)),
            ).fetchone()[0]
            conn.execute(
                """
                INSERT INTO metric_result (analysis_run_id, metric, city_id, value)
                VALUES (%s, 'scenario_reach_population_delta', %s, NULL),
                       (%s, 'scenario_reach_population_delta', %s, -3000)
                """,
                (run_id, city_ids[0], run_id, city_ids[1]),
            )
    try:
        assert data.load_scenarios("db") == [scenario]
        assert data.load_metric(
            "scenario_reach_population_delta",
            {"layer": "best", "threshold_sec": 21600},
            scenario,
            "db",
        ) == {city_ids[0]: None, city_ids[1]: -3000}
        with pytest.raises(data.ResultNotFound):
            data.load_metric("betweenness", {"layer": "best"}, source="db")
    finally:
        with psycopg.connect(get_connection_string()) as conn:
            conn.execute("DELETE FROM analysis_run WHERE graph_snapshot_id = %s", (graph_id,))
            conn.execute("DELETE FROM graph_snapshot WHERE graph_snapshot_id = %s", (graph_id,))
