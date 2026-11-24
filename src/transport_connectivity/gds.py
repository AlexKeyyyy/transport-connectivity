"""Weighted GDS centrality for a single graph exported by #34."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from decimal import Decimal
from logging import getLogger
from os import getenv
from sys import exception
from uuid import UUID, uuid4

from transport_connectivity.resilience import Graph

_LOG = getLogger(__name__)

ProjectedEdge = tuple[str, str, str, Decimal, str]

_PROJECT = """
MATCH (source:City {graph_snapshot_id: $snapshot})
OPTIONAL MATCH (source)-[rel:CONNECTED]->(target:City {graph_snapshot_id: $snapshot})
WITH gds.graph.project(
    $graph, source, target, {relationshipProperties: rel {.weight}}
) AS projected,
collect(DISTINCT source.city_id) AS city_ids,
collect(CASE WHEN target IS NULL THEN [] ELSE
    [source.city_id, target.city_id, rel.transport_type, rel.weight, rel.weight_unit]
END) AS projected_edges
RETURN projected.nodeCount AS node_count, projected.relationshipCount AS edge_count,
       city_ids, [edge IN projected_edges WHERE size(edge) > 0] AS edges
"""


def weighted_betweenness(
    snapshot: UUID, graph: Graph, edges: Iterable[ProjectedEdge]
) -> tuple[str, dict[str, float]]:
    """Check the published projection, then normalize GDS scores for ordered pairs."""
    try:
        from neo4j import GraphDatabase
        from neo4j.exceptions import DriverError, Neo4jError
    except ModuleNotFoundError as error:
        if error.name != "neo4j":
            raise
        raise ValueError("Install the project's neo4j extra to run key-nodes") from error

    uri = getenv("NEO4J_URI", "bolt://localhost:7687")
    user = getenv("NEO4J_USER", "neo4j")
    password = getenv("NEO4J_PASSWORD")
    auth = (user, password) if password else None
    graph_name = f"transport-betweenness-{uuid4()}"
    expected_edges = Counter(edges)
    try:
        with (
            GraphDatabase.driver(uri, auth=auth) as driver,
            driver.session(database=getenv("NEO4J_DATABASE", "neo4j")) as session,
        ):
            version = session.run("RETURN gds.version() AS version").single(strict=True)["version"]
            try:
                projection = session.run(_PROJECT, graph=graph_name, snapshot=str(snapshot)).single(
                    strict=True
                )
                actual_edges = Counter(
                    (origin, target, kind, Decimal(str(weight)), unit)
                    for origin, target, kind, weight, unit in projection["edges"]
                )
                if (
                    projection["node_count"] != len(graph)
                    or projection["edge_count"] != expected_edges.total()
                    or set(projection["city_ids"]) != set(graph)
                    or actual_edges != expected_edges
                ):
                    raise ValueError(f"Neo4j projection does not match PostgreSQL for {snapshot}")
                if len(graph) < 3 or not expected_edges:
                    return version, dict.fromkeys(graph, 0.0)

                records = session.run(
                    """
                    CALL gds.betweenness.stream($graph, {
                        relationshipWeightProperty: 'weight', concurrency: 1
                    })
                    YIELD nodeId, score
                    RETURN gds.util.asNode(nodeId).city_id AS city_id, score
                    """,
                    graph=graph_name,
                )
                denominator = (len(graph) - 1) * (len(graph) - 2)
                scores = {row["city_id"]: row["score"] / denominator for row in records}
                if scores.keys() != graph.keys() or not all(0 <= x <= 1 for x in scores.values()):
                    raise ValueError(f"Invalid GDS scores for {snapshot}")
                return version, scores
            finally:
                failed = exception() is not None
                try:
                    session.run(
                        "CALL gds.graph.drop($graph, false) YIELD graphName RETURN graphName",
                        graph=graph_name,
                    ).consume()
                except (DriverError, Neo4jError):
                    if not failed:
                        raise
                    _LOG.warning("Could not drop temporary GDS graph %s", graph_name, exc_info=True)
    except (DriverError, Neo4jError) as error:
        raise ValueError(f"Could not compute GDS betweenness for {snapshot}: {error}") from error
