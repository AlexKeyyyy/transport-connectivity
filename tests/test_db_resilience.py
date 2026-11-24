"""Resilience publication and an optional weighted GDS cross-check."""

from __future__ import annotations

import os
from collections.abc import Iterator
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from test_db_accessibility import snapshot as snapshot

from transport_connectivity.db.migrate import get_connection_string
from transport_connectivity.db.resilience import run_analysis
from transport_connectivity.gds import weighted_betweenness
from transport_connectivity.resilience import adjacency, betweenness

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(os.environ.get("RUN_DB_TESTS") != "1", reason="Set RUN_DB_TESTS=1"),
]


@pytest.fixture
def graph_version(snapshot: tuple[str, list[str]]) -> Iterator[tuple[str, UUID, list[str]]]:
    snapshot_id, cities = snapshot
    with psycopg.connect(get_connection_string()) as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO city_external_id (city_id, id_namespace, external_id, snapshot_id)
            VALUES (%s, 'sber_territory', %s, %s)
            """,
            [(cities[1], "2539", snapshot_id), (cities[2], "2460", snapshot_id)],
        )
        cur.executemany(
            "UPDATE city SET location = ST_SetSRID(ST_MakePoint(%s, 1), 4326) WHERE city_id = %s",
            [(i, city) for i, city in enumerate(cities)],
        )
        cur.execute(
            """
            INSERT INTO graph_snapshot (methodology_version, parameters, is_directed,
                                        status, code_commit)
            VALUES ('1.0', %s, true, 'ready', 'test') RETURNING graph_snapshot_id
            """,
            (Jsonb({"layer": "best", "alpha": "0.05"}),),
        )
        graph_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO graph_snapshot_input
            SELECT %s, snapshot_id, load_run_id FROM load_run WHERE snapshot_id = %s
            """,
            (graph_id, snapshot_id),
        )
        cur.executemany(
            "INSERT INTO graph_node VALUES (%s, %s)", [(graph_id, city) for city in cities]
        )
        cur.executemany(
            """
            INSERT INTO graph_edge (
                graph_snapshot_id, from_city_id, to_city_id, transport_type, weight, weight_unit
            )
            VALUES (%s, %s, %s, 'road', %s, 'sec')
            """,
            [(graph_id, cities[a], cities[b], w) for a, b, w in ((0, 1, 10000), (1, 2, 10000))],
        )
    try:
        yield snapshot_id, graph_id, cities
    finally:
        with psycopg.connect(get_connection_string()) as conn:
            conn.execute("DELETE FROM analysis_run WHERE graph_snapshot_id = %s", (graph_id,))
            conn.execute("DELETE FROM graph_snapshot WHERE graph_snapshot_id = %s", (graph_id,))
            conn.execute("DELETE FROM city_external_id WHERE snapshot_id = %s", (snapshot_id,))


def test_removal_and_link_scenarios_are_published_once(
    graph_version: tuple[str, UUID, list[str]],
) -> None:
    snapshot_id, graph_id, cities = graph_version
    assert (
        run_analysis("node-removal", snapshot_id, graph_id, code_commit="test")["runs_created"] == 4
    )
    assert (
        run_analysis("link-scenarios", snapshot_id, graph_id, code_commit="test")["runs_created"]
        == 6
    )
    for command in ("node-removal", "link-scenarios"):
        assert run_analysis(command, snapshot_id, graph_id, code_commit="test")["runs_created"] == 0

    with psycopg.connect(get_connection_string()) as conn:
        assert conn.execute(
            "SELECT count(*) FROM analysis_run WHERE graph_snapshot_id = %s AND status = 'ready'",
            (graph_id,),
        ).fetchone() == (10,)
        assert conn.execute(
            """
            SELECT m.value FROM metric_result m JOIN analysis_run ar USING (analysis_run_id)
            WHERE ar.graph_snapshot_id = %s AND ar.algorithm = 'node_removal'
              AND m.city_id = %s AND m.metric = 'removal_unreachable_pairs'
            """,
            (graph_id, cities[1]),
        ).fetchone() == (1,)
        assert dict(
            conn.execute(
                """
            SELECT m.city_id::text, m.value
            FROM metric_result m JOIN analysis_run ar USING (analysis_run_id)
            WHERE ar.graph_snapshot_id = %s AND ar.scenario->>'type' = 'remove_node'
              AND ar.parameters->>'threshold_sec' = '21600'
            """,
                (graph_id,),
            ).fetchall()
        ) == {city: (-3000 if i == 0 else None if i == 1 else 0) for i, city in enumerate(cities)}


@pytest.mark.parametrize("alpha", ["0.051", "NaN", "foo", None])
def test_rejects_unsupported_graph_parameter(
    graph_version: tuple[str, UUID, list[str]], alpha: str | None
) -> None:
    snapshot_id, graph_id, _ = graph_version
    with psycopg.connect(get_connection_string()) as conn:
        conn.execute(
            "UPDATE graph_snapshot SET parameters = %s WHERE graph_snapshot_id = %s",
            (Jsonb({"layer": "best", "alpha": alpha}), graph_id),
        )
    with pytest.raises(ValueError, match="ready graph"):
        run_analysis("node-removal", snapshot_id, graph_id, code_commit="test")


@pytest.mark.skipif(os.environ.get("RUN_GDS_TESTS") != "1", reason="Set RUN_GDS_TESTS=1")
def test_gds_agrees_with_python_for_ten_cities() -> None:
    from neo4j import GraphDatabase

    graph_id = uuid4()
    cities = [str(uuid4()) for _ in range(10)]
    edges = [
        (cities[a], cities[b], "road", Decimal(weight), "sec")
        for a, b, weight in ((0, 1, 2), (1, 2, 3), (0, 3, 3), (3, 2, 2), (2, 4, 1))
    ]
    graph = adjacency(cities, ((a, b, w) for a, b, _, w, _ in edges))
    password = os.getenv("NEO4J_PASSWORD")
    auth = (os.getenv("NEO4J_USER", "neo4j"), password) if password else None
    with (
        GraphDatabase.driver(os.environ["NEO4J_URI"], auth=auth) as driver,
        driver.session(database=os.getenv("NEO4J_DATABASE", "neo4j")) as session,
    ):
        try:
            session.run(
                "UNWIND $cities AS id CREATE (:City {graph_snapshot_id: $graph, city_id: id})",
                graph=str(graph_id),
                cities=cities,
            ).consume()
            session.run(
                """
                UNWIND $edges AS edge
                MATCH (a:City {graph_snapshot_id: $graph, city_id: edge[0]})
                MATCH (b:City {graph_snapshot_id: $graph, city_id: edge[1]})
                CREATE (a)-[:CONNECTED {
                    transport_type:'road', weight:edge[2], weight_unit:'sec'
                }]->(b)
                """,
                graph=str(graph_id),
                edges=[(a, b, int(weight)) for a, b, _, weight, _ in edges],
            ).consume()
            version, actual = weighted_betweenness(graph_id, graph, edges)
            assert version
            assert actual == pytest.approx(betweenness(graph), abs=1e-6)
        finally:
            session.run(
                "MATCH (n:City {graph_snapshot_id:$graph}) DETACH DELETE n", graph=str(graph_id)
            ).consume()
