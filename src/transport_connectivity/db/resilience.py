"""Run resilience analysis on published graph versions and store its metrics."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from itertools import combinations
from platform import python_version
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from transport_connectivity.accessibility import THRESHOLDS_SEC
from transport_connectivity.db.migrate import get_connection_string
from transport_connectivity.gds import ProjectedEdge, weighted_betweenness
from transport_connectivity.graph import LAYERS
from transport_connectivity.rasp_stations import distance_km
from transport_connectivity.resilience import (
    Graph,
    add_bidirectional_link,
    adjacency,
    reach_population,
    removal_effect,
    shortest_times,
    suggest_links,
)

ALPHAS = (Decimal("0.05"), Decimal("0.10"), Decimal("0.20"))
REFERENCE_ALPHA = ALPHAS[0]
VSM_TIME_SEC = 8100
COMMAND_LAYERS = {
    "key-nodes": LAYERS,
    "node-removal": ("best",),
    "link-scenarios": ("rail", "best"),
}
Metric = tuple[str, str, int | Decimal | float | None]
Times = Mapping[str, Mapping[str, Decimal]]
Parameters = dict[str, str | int]


@dataclass(frozen=True)
class Version:
    graph_id: UUID
    layer: str
    alpha: Decimal

    def parameters(self) -> Parameters:
        return {"layer": self.layer, "alpha": str(self.alpha.quantize(Decimal("0.01")))}


@dataclass
class GraphInput:
    graph: Graph
    edges: list[ProjectedEdge]
    population: dict[str, int]
    coordinates: dict[str, tuple[float, float]]
    source_ids: dict[str, str]

    def city(self, source_id: str) -> str:
        if source_id not in self.source_ids:
            raise ValueError(f"Graph has no city with Sber ID {source_id}")
        return self.source_ids[source_id]


def _versions(
    cur: psycopg.Cursor, command: str, snapshot_id: str, graph_id: UUID | None
) -> list[Version]:
    cur.execute(
        """
        SELECT g.graph_snapshot_id, g.parameters->>'layer', g.parameters->>'alpha'
        FROM graph_snapshot g JOIN graph_snapshot_input i USING (graph_snapshot_id)
        WHERE i.snapshot_id = %(snapshot)s AND g.status = 'ready' AND g.is_directed
          AND g.methodology_version = '1.0' AND g.parameters->>'layer' = ANY(%(layers)s)
          AND (%(graph)s::uuid IS NULL OR g.graph_snapshot_id = %(graph)s::uuid)
        ORDER BY g.graph_snapshot_id
        """,
        {"snapshot": snapshot_id, "layers": list(COMMAND_LAYERS[command]), "graph": graph_id},
    )
    versions = []
    for candidate, layer, raw_alpha in cur:
        try:
            alpha = Decimal(str(raw_alpha))
        except InvalidOperation:
            continue
        if alpha.is_finite() and alpha in ALPHAS:
            versions.append(Version(candidate, layer, alpha))
    found = {(v.layer, v.alpha) for v in versions}
    if graph_id is not None and len(versions) == 1:
        return versions
    expected = {(layer, alpha) for layer in COMMAND_LAYERS[command] for alpha in ALPHAS}
    if graph_id is None and found == expected and len(versions) == len(expected):
        return versions
    raise ValueError(
        f"No unambiguous set of ready graph versions for {command} and '{snapshot_id}'. "
        "Run build-graph (#28) or select a ready version with --graph-snapshot."
    )


def _read_graph(cur: psycopg.Cursor, version: Version, snapshot_id: str) -> GraphInput:
    cur.execute(
        """
        SELECT n.city_id::text, c.population, ST_Y(c.location), ST_X(c.location), x.external_id
        FROM graph_node n JOIN city c USING (city_id)
        LEFT JOIN city_external_id x ON x.city_id = c.city_id
            AND x.id_namespace = 'sber_territory' AND x.snapshot_id = %(snapshot)s
        WHERE n.graph_snapshot_id = %(graph)s
        ORDER BY n.city_id
        """,
        {"graph": version.graph_id, "snapshot": snapshot_id},
    )
    population, coordinates, source_ids = {}, {}, {}
    for city, inhabitants, latitude, longitude, source_id in cur:
        population[city] = inhabitants or 0
        if latitude is not None and longitude is not None:
            coordinates[city] = latitude, longitude
        if source_id is not None:
            source_ids[source_id] = city

    cur.execute(
        """
        SELECT from_city_id::text, to_city_id::text, transport_type, weight, weight_unit
        FROM graph_edge WHERE graph_snapshot_id = %s ORDER BY from_city_id, to_city_id
        """,
        (version.graph_id,),
    )
    edges = cur.fetchall()
    if not population or any(unit != "sec" for _, _, _, _, unit in edges):
        raise ValueError(f"Expected cities and weights in seconds for {version.graph_id}")
    graph = adjacency(population, ((a, b, weight) for a, b, _, weight, _ in edges))
    if sum(map(len, graph.values())) != len(edges):
        raise ValueError(f"Parallel edges are outside methodology 1.0 for {version.graph_id}")
    return GraphInput(graph, edges, population, coordinates, source_ids)


def _ready(
    cur: psycopg.Cursor,
    version: Version,
    algorithm: str,
    parameters: Parameters,
    scenario: dict | None = None,
    engine: str = "python",
) -> bool:
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM analysis_run
            WHERE graph_snapshot_id = %s AND algorithm = %s AND engine = %s
              AND status = 'ready' AND parameters = %s AND scenario IS NOT DISTINCT FROM %s::jsonb
        )
        """,
        (
            version.graph_id,
            algorithm,
            engine,
            Jsonb(parameters),
            Jsonb(scenario) if scenario is not None else None,
        ),
    )
    return cur.fetchone()[0]


def _publish(
    cur: psycopg.Cursor,
    version: Version,
    code_commit: str,
    algorithm: str,
    parameters: Parameters,
    values: Iterable[Metric],
    scenario: dict | None = None,
    *,
    engine: str = "python",
    engine_version: str | None = None,
) -> None:
    cur.execute(
        """
        INSERT INTO analysis_run (
            graph_snapshot_id, algorithm, engine, engine_version, parameters, scenario,
            code_commit, status, started_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'running', clock_timestamp())
        RETURNING analysis_run_id
        """,
        (
            version.graph_id,
            algorithm,
            engine,
            engine_version or python_version(),
            Jsonb(parameters),
            Jsonb(scenario) if scenario is not None else None,
            code_commit,
        ),
    )
    run_id = cur.fetchone()[0]
    with cur.copy(
        "COPY metric_result (analysis_run_id, metric, city_id, value) FROM STDIN"
    ) as copy:
        for metric, city, value in values:
            copy.write_row((run_id, metric, city, value))
    cur.execute(
        """
        UPDATE analysis_run SET status = 'ready', finished_at = clock_timestamp()
        WHERE analysis_run_id = %s
        """,
        (run_id,),
    )


def _removal_rows(graph: Graph, before: Times) -> Iterator[Metric]:
    for city in graph:
        effect = removal_effect(graph, city, before)
        yield "removal_unreachable_pairs", city, effect.lost_pairs
        yield "removal_time_increase_pct", city, effect.mean_time_change_percent


def _population_rows(
    before: Times,
    after: Times,
    population: Mapping[str, int],
    threshold: int,
) -> Iterator[Metric]:
    for origin, times in before.items():
        new_times = after.get(origin)
        delta = None
        if new_times is not None:
            delta = reach_population(new_times, population, origin, threshold) - reach_population(
                times, population, origin, threshold
            )
        yield "scenario_reach_population_delta", origin, delta


def _publish_population(
    cur: psycopg.Cursor,
    version: Version,
    code_commit: str,
    scenario: dict,
    before: Times,
    after: Times,
    population: Mapping[str, int],
    thresholds: Iterable[int],
) -> int:
    created = 0
    for threshold in thresholds:
        parameters = {**version.parameters(), "threshold_sec": threshold}
        if _ready(cur, version, "scenario", parameters, scenario):
            continue
        _publish(
            cur,
            version,
            code_commit,
            "scenario",
            parameters,
            _population_rows(before, after, population, threshold),
            scenario,
        )
        created += 1
    return created


def _node_removal(cur: psycopg.Cursor, version: Version, data: GraphInput, code_commit: str) -> int:
    before = {city: shortest_times(data.graph, city) for city in data.graph}
    parameters = version.parameters()
    created = 0
    if not _ready(cur, version, "node_removal", parameters):
        _publish(
            cur,
            version,
            code_commit,
            "node_removal",
            parameters,
            _removal_rows(data.graph, before),
        )
        created += 1
    moscow = data.city("2539")
    scenario = {"type": "remove_node", "name": "Без Москвы", "city_id": moscow}
    after = {
        city: shortest_times(data.graph, city, removed=moscow)
        for city in data.graph
        if city != moscow
    }
    return created + _publish_population(
        cur, version, code_commit, scenario, before, after, data.population, THRESHOLDS_SEC.values()
    )


def _link_scenarios(
    cur: psycopg.Cursor, version: Version, data: GraphInput, code_commit: str
) -> int:
    before = {city: shortest_times(data.graph, city) for city in data.graph}
    moscow, petersburg = data.city("2539"), data.city("2460")
    vsm = {
        "type": "add_edge",
        "name": "С ВСМ Москва — Санкт-Петербург",
        "from_city": moscow,
        "to_city": petersburg,
        "travel_time_sec": VSM_TIME_SEC,
    }
    updated = add_bidirectional_link(data.graph, moscow, petersburg, Decimal(VSM_TIME_SEC))
    after = {city: shortest_times(updated, city) for city in updated}
    created = _publish_population(
        cur, version, code_commit, vsm, before, after, data.population, THRESHOLDS_SEC.values()
    )
    if (version.layer, version.alpha) != ("best", REFERENCE_ALPHA):
        return created
    distances = {
        (a, b): distance_km(*data.coordinates[a], *data.coordinates[b])
        for a, b in combinations(sorted(data.coordinates), 2)
    }
    _, proposals = suggest_links(data.graph, before, distances, limit=5)
    for proposal in proposals:
        scenario = {
            "type": "suggested_link",
            "from_city": proposal.source,
            "to_city": proposal.target,
            "travel_time_sec": proposal.proposed_time_sec,
        }
        candidate = add_bidirectional_link(
            data.graph, proposal.source, proposal.target, Decimal(proposal.proposed_time_sec)
        )
        after = {city: shortest_times(candidate, city) for city in candidate}
        created += _publish_population(
            cur,
            version,
            code_commit,
            scenario,
            before,
            after,
            data.population,
            (THRESHOLDS_SEC["half_day"],),
        )
    return created


def run_analysis(
    command: str,
    snapshot_id: str,
    graph_id: UUID | None,
    *,
    code_commit: str,
) -> dict[str, int]:
    """Publish one command's results in one transaction and reuse successful runs."""
    created = 0
    with psycopg.connect(get_connection_string()) as conn:
        conn.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
        with conn.cursor() as cur:
            versions = _versions(cur, command, snapshot_id, graph_id)
            # Keep Neo4j native node IDs stable until GDS has read the scores.
            lock_scope = "export-graph" if command == "key-nodes" else command
            for version in versions:
                # Acquire locks in graph UUID order even when one command spans several versions.
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"transport_connectivity:{lock_scope}:{version.graph_id}",),
                )
                if command == "key-nodes":
                    params = version.parameters()
                    if _ready(cur, version, "betweenness", params, engine="neo4j-gds"):
                        continue
                    data = _read_graph(cur, version, snapshot_id)
                    version_gds, scores = weighted_betweenness(
                        version.graph_id, data.graph, data.edges
                    )
                    _publish(
                        cur,
                        version,
                        code_commit,
                        "betweenness",
                        params,
                        (("betweenness", city, score) for city, score in scores.items()),
                        engine="neo4j-gds",
                        engine_version=version_gds,
                    )
                    created += 1
                else:
                    data = _read_graph(cur, version, snapshot_id)
                    calculate = _node_removal if command == "node-removal" else _link_scenarios
                    created += calculate(cur, version, data, code_commit)
    return {"graph_versions": len(versions), "runs_created": created}
