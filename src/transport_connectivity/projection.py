"""Rebuild a single published PostgreSQL graph in Neo4j."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from itertools import batched
from os import getenv
from typing import TYPE_CHECKING, NamedTuple
from uuid import UUID

import psycopg

from transport_connectivity.db.migrate import get_connection_string

if TYPE_CHECKING:
    from neo4j import ManagedTransaction


class GraphCity(NamedTuple):
    city_id: str
    name: str | None
    region: str | None
    population: int | None


class GraphLink(NamedTuple):
    origin: str
    target: str
    transport_type: str
    weight: float
    weight_unit: str


@dataclass(frozen=True)
class Projection:
    graph_id: UUID
    cities: tuple[GraphCity, ...]
    links: tuple[GraphLink, ...]


@dataclass(frozen=True)
class ExportResult:
    graph_snapshot_id: UUID
    nodes: int
    edges: int


def projection_from_rows(
    graph_id: UUID,
    city_rows: Iterable[tuple[UUID, str | None, str | None, int | None]],
    edge_rows: Iterable[tuple[UUID, UUID, str, Decimal, str]],
) -> Projection:
    """Convert database rows to values supported by the Neo4j driver."""
    cities = tuple(GraphCity(str(city), name, region, pop) for city, name, region, pop in city_rows)
    city_ids = {city.city_id for city in cities}
    if len(city_ids) != len(cities):
        raise ValueError(f"Duplicate city in PostgreSQL graph {graph_id}")
    links = []
    for origin, target, kind, weight, unit in edge_rows:
        if str(origin) not in city_ids or str(target) not in city_ids:
            raise ValueError(f"Edge endpoint missing in PostgreSQL graph {graph_id}")
        if not weight.is_finite() or weight <= 0 or Decimal(str(float(weight))) != weight:
            raise ValueError(f"Edge weight {weight} cannot be projected exactly in {graph_id}")
        links.append(GraphLink(str(origin), str(target), kind, float(weight), unit))
    return Projection(graph_id, cities, tuple(links))


def _read_projection(cur: psycopg.Cursor, graph_id: UUID) -> Projection:
    cur.execute(
        """
        SELECT c.city_id, c.name, c.region, c.population
        FROM graph_node n JOIN city c USING (city_id)
        WHERE n.graph_snapshot_id = %s ORDER BY c.city_id
        """,
        (graph_id,),
    )
    cities = cur.fetchall()
    cur.execute(
        """
        SELECT from_city_id, to_city_id, transport_type, weight, weight_unit
        FROM graph_edge WHERE graph_snapshot_id = %s
        ORDER BY from_city_id, to_city_id, transport_type
        """,
        (graph_id,),
    )
    return projection_from_rows(graph_id, cities, cur)


_CONSTRAINT = """
CREATE CONSTRAINT city_graph_uuid IF NOT EXISTS
FOR (city:City) REQUIRE (city.graph_snapshot_id, city.city_id) IS UNIQUE
"""


def _verify_projection(tx: ManagedTransaction, projection: Projection) -> ExportResult:
    graph = str(projection.graph_id)
    cities = [
        GraphCity(row["id"], row["name"], row["region"], row["population"])
        for row in tx.run(
            """
            MATCH (c:City {graph_snapshot_id: $graph})
            RETURN c.city_id AS id, c.name AS name, c.region AS region, c.population AS population
            """,
            graph=graph,
        )
    ]
    outgoing = list(
        tx.run(
            """
        MATCH (a:City {graph_snapshot_id: $graph})-[edge:CONNECTED]->(b)
        RETURN a.city_id AS origin, b.city_id AS target, edge.transport_type AS kind,
               edge.weight AS weight, edge.weight_unit AS unit, b.graph_snapshot_id AS scope
        """,
            graph=graph,
        )
    )
    if any(row["scope"] != graph for row in outgoing):
        raise ValueError(f"Neo4j projection contains a cross-version edge from {graph}")
    edges = [
        GraphLink(row["origin"], row["target"], row["kind"], row["weight"], row["unit"])
        for row in outgoing
    ]
    if Counter(cities) != Counter(projection.cities) or Counter(edges) != Counter(projection.links):
        raise ValueError(f"Neo4j projection differs from PostgreSQL for {graph}")
    return ExportResult(projection.graph_id, len(cities), len(edges))


def replace_projection(tx: ManagedTransaction, projection: Projection) -> ExportResult:
    """Replace and verify one version in the active Neo4j write transaction."""
    graph = str(projection.graph_id)
    tx.run("MATCH (c:City {graph_snapshot_id: $graph}) DETACH DELETE c", graph=graph).consume()
    for batch in batched(projection.cities, 1000):
        tx.run(
            """
            UNWIND $rows AS row
            CREATE (:City {graph_snapshot_id: $graph, city_id: row.city_id,
                           name: row.name, region: row.region, population: row.population})
            """,
            rows=[city._asdict() for city in batch],
            graph=graph,
        ).consume()
    for batch in batched(projection.links, 1000):
        tx.run(
            """
            UNWIND $rows AS row
            MATCH (a:City {graph_snapshot_id: $graph, city_id: row.origin})
            MATCH (b:City {graph_snapshot_id: $graph, city_id: row.target})
            CREATE (a)-[:CONNECTED {transport_type: row.transport_type,
                                     weight: row.weight, weight_unit: row.weight_unit}]->(b)
            """,
            rows=[edge._asdict() for edge in batch],
            graph=graph,
        ).consume()
    return _verify_projection(tx, projection)


def export_graphs(graph_id: UUID | None) -> list[ExportResult]:
    """Export a requested ready version, or all ready versions when graph_id is None."""
    try:
        from neo4j import GraphDatabase
        from neo4j.exceptions import DriverError, Neo4jError
    except ModuleNotFoundError as error:
        if error.name != "neo4j":
            raise
        raise ValueError("Install the project's neo4j extra to run export-graph") from error

    uri = getenv("NEO4J_URI", "bolt://127.0.0.1:7687")
    password = getenv("NEO4J_PASSWORD", "changeme")
    auth = (getenv("NEO4J_USER", "neo4j"), password) if password else None
    try:
        with psycopg.connect(get_connection_string()) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT graph_snapshot_id FROM graph_snapshot WHERE status = 'ready'
                  AND (%s::uuid IS NULL OR graph_snapshot_id = %s)
                ORDER BY graph_snapshot_id
                """,
                (graph_id, graph_id),
            )
            graphs = [id_ for (id_,) in cur]
            if not graphs:
                raise ValueError("No ready PostgreSQL graph for this selection")
            with (
                GraphDatabase.driver(uri, auth=auth) as driver,
                driver.session(database=getenv("NEO4J_DATABASE", "neo4j")) as session,
            ):
                session.run(_CONSTRAINT).consume()
                result = []
                for id_ in graphs:
                    cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"transport_connectivity:export-graph:{id_}",),
                    )
                    result.append(
                        session.execute_write(replace_projection, _read_projection(cur, id_))
                    )
                return result
    except (DriverError, Neo4jError) as error:
        raise ValueError(f"Could not export a graph to Neo4j: {error}") from error
