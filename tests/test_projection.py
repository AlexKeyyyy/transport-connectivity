"""Conversion, isolated graph replacement and rollback in Neo4j."""

from __future__ import annotations

import os
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest

from transport_connectivity.projection import (
    _CONSTRAINT,
    GraphCity,
    GraphLink,
    Projection,
    _verify_projection,
    projection_from_rows,
    replace_projection,
)


def _sample() -> Projection:
    graph_id, a, b, c = (uuid4() for _ in range(4))
    return projection_from_rows(
        graph_id,
        [(a, "А", "Регион", 10), (b, "Б", None, None), (c, "В", "Регион", 30)],
        [(a, b, "rail", Decimal("1.25"), "sec"), (b, c, "road", Decimal(12), "sec")],
    )


def test_postgresql_rows_keep_direction_and_missing_attributes() -> None:
    graph = _sample()
    a, b, c = graph.cities
    assert a == GraphCity(a.city_id, "А", "Регион", 10)
    assert b == GraphCity(b.city_id, "Б", None, None)
    assert graph.links == (
        GraphLink(a.city_id, b.city_id, "rail", 1.25, "sec"),
        GraphLink(b.city_id, c.city_id, "road", 12.0, "sec"),
    )


def test_rejects_an_edge_without_both_vertices() -> None:
    a, b = uuid4(), uuid4()
    with pytest.raises(ValueError, match="Edge endpoint missing"):
        projection_from_rows(uuid4(), [(a, None, None, None)], [(a, b, "road", Decimal(1), "sec")])


@pytest.mark.neo4j
@pytest.mark.skipif(not os.environ.get("NEO4J_URI"), reason="Set NEO4J_URI")
def test_rebuild_repeat_and_verification_rollback() -> None:
    from neo4j import GraphDatabase

    graph = _sample()
    other = replace(graph, graph_id=uuid4(), links=())
    password = os.getenv("NEO4J_PASSWORD")
    auth = (os.getenv("NEO4J_USER", "neo4j"), password) if password else None
    with (
        GraphDatabase.driver(os.environ["NEO4J_URI"], auth=auth) as driver,
        driver.session(database=os.getenv("NEO4J_DATABASE", "neo4j")) as session,
    ):
        try:
            assert session.run("RETURN gds.version() AS version").single()["version"]
            session.run(_CONSTRAINT).consume()
            isolated = session.execute_write(replace_projection, other)
            assert (isolated.nodes, isolated.edges) == (3, 0)
            first = session.execute_write(replace_projection, graph)
            assert (first.nodes, first.edges) == (3, 2)
            assert session.execute_write(replace_projection, graph) == first
            name = f"test-projection-{graph.graph_id}"
            try:
                session.run(
                    """
                    MATCH (a:City {graph_snapshot_id:$graph})
                    OPTIONAL MATCH (a)-[r:CONNECTED]->(b:City {graph_snapshot_id:$graph})
                    WITH gds.graph.project(
                        $name,a,b,{relationshipProperties:r {.weight}}
                    ) AS projected
                    RETURN projected.nodeCount AS nodes
                    """,
                    graph=str(graph.graph_id),
                    name=name,
                ).consume()
                scores = session.run(
                    """
                    CALL gds.betweenness.stream($name,{relationshipWeightProperty:'weight'})
                    YIELD nodeId, score RETURN score
                    """,
                    name=name,
                )
                assert sorted(row["score"] for row in scores) == [0.0, 0.0, 1.0]
            finally:
                session.run(
                    "CALL gds.graph.drop($name,false) YIELD graphName RETURN graphName",
                    name=name,
                ).consume()

            broken = replace(
                graph,
                links=(graph.links[0]._replace(target=str(uuid4())),),
            )
            with pytest.raises(ValueError, match="projection differs"):
                session.execute_write(replace_projection, broken)
            assert session.execute_read(_verify_projection, graph) == first
            assert session.execute_read(_verify_projection, other) == isolated
        finally:
            session.run(
                "MATCH (n:City) WHERE n.graph_snapshot_id IN $ids DETACH DELETE n",
                ids=[str(graph.graph_id), str(other.graph_id)],
            ).consume()
