"""Integration checks for graph construction using a ten-city loaded snapshot."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from test_db_accessibility import snapshot as snapshot

from transport_connectivity.cli import main
from transport_connectivity.db import graph
from transport_connectivity.db.migrate import get_connection_string

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(os.environ.get("RUN_DB_TESTS") != "1", reason="Set RUN_DB_TESTS=1"),
]


@pytest.fixture
def graph_input(snapshot: tuple[str, list[str]]) -> Iterator[tuple[str, list[str]]]:
    try:
        yield snapshot
    finally:
        with psycopg.connect(get_connection_string()) as conn:
            conn.execute(
                """
                DELETE FROM graph_snapshot WHERE graph_snapshot_id IN (
                    SELECT graph_snapshot_id FROM graph_snapshot_input WHERE snapshot_id = %s
                )
                """,
                (snapshot[0],),
            )


def test_build_all_reuses_the_same_versions_under_concurrency(graph_input, capsys) -> None:
    snapshot_id, cities = graph_input
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [
            pool.submit(graph.build_graphs, snapshot_id, graph.ALL_VERSIONS, code_commit="test")
            for _ in range(2)
        ]
        first, second = [call.result() for call in calls]

    assert sorted(sum(row.created for row in results) for results in (first, second)) == [0, 9]
    assert [row.graph_snapshot_id for row in first] == [row.graph_snapshot_id for row in second]
    assert all(row.nodes == 10 for row in first)
    with psycopg.connect(get_connection_string()) as conn:
        conn.execute(
            """
            UPDATE graph_snapshot
            SET parameters = jsonb_set(parameters, '{alpha}', '0.050'::jsonb)
            WHERE graph_snapshot_id = %s
            """,
            (first[0].graph_snapshot_id,),
        )
    assert main(["build-graph", "--all", "--snapshot-id", snapshot_id]) == 0
    output = json.loads(capsys.readouterr().out)
    assert len(output) == 9 and not any(row["created"] for row in output)

    with psycopg.connect(get_connection_string()) as conn:
        assert conn.execute(
            """
            SELECT count(*) FROM graph_snapshot g JOIN graph_snapshot_input i
            USING (graph_snapshot_id) JOIN load_run l USING (snapshot_id, load_run_id)
            WHERE i.snapshot_id = %s AND g.is_directed AND g.status = 'ready'
            """,
            (snapshot_id,),
        ).fetchone() == (9,)
        assert (
            conn.execute(
                """
            SELECT e.weight, e.weight_unit, e.transport_type, m.duration_sec
            FROM graph_edge e JOIN graph_snapshot g USING (graph_snapshot_id)
            JOIN graph_snapshot_input i USING (graph_snapshot_id)
            JOIN route_measurement m USING (measurement_id)
            WHERE i.snapshot_id = %s AND g.parameters->>'layer' = 'best'
              AND e.from_city_id = %s AND e.to_city_id = %s
            """,
                (snapshot_id, cities[0], cities[2]),
            ).fetchall()
            == [(20800, "sec", "air", 10000)] * 3
        )
        assert conn.execute(
            """
            SELECT count(*) FROM graph_node n JOIN graph_snapshot_input i USING (graph_snapshot_id)
            WHERE i.snapshot_id = %s AND n.city_id = %s
            """,
            (snapshot_id, cities[-1]),
        ).fetchone() == (9,)


def test_failed_batch_rolls_back_earlier_graph(graph_input, monkeypatch) -> None:
    snapshot_id, _ = graph_input
    publish = graph._publish_graph
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("simulated publication failure")
        return publish(*args, **kwargs)

    monkeypatch.setattr(graph, "_publish_graph", fail_second)
    with pytest.raises(ValueError, match="simulated publication failure"):
        graph.build_graphs(snapshot_id, graph.ALL_VERSIONS, code_commit="test")
    with psycopg.connect(get_connection_string()) as conn:
        assert conn.execute(
            "SELECT count(*) FROM graph_snapshot_input WHERE snapshot_id = %s", (snapshot_id,)
        ).fetchone() == (0,)
