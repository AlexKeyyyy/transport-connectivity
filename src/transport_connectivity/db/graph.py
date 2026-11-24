"""Materialize the approved directed graph methodology in PostgreSQL."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from itertools import product
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from transport_connectivity.db.migrate import get_connection_string
from transport_connectivity.graph import (
    AIR_ACCESS_SEC,
    LAYERS,
    TimedMeasurement,
    best_transport_types,
    layer_times,
    prune,
    unreachable_pairs,
)

METHODOLOGY_VERSION = "1.0"
ALPHAS = (Decimal("0.05"), Decimal("0.10"), Decimal("0.20"))
ALL_VERSIONS = tuple(product(LAYERS, ALPHAS))


@dataclass(frozen=True)
class GraphBuild:
    graph_snapshot_id: UUID
    layer: str
    alpha: str
    nodes: int
    edges: int
    unreachable: int | None
    created: bool


def _snapshot_input(cur: psycopg.Cursor, snapshot: str) -> tuple[UUID, list[str]]:
    cur.execute(
        """
        SELECT load_run_id FROM load_run
        WHERE snapshot_id = %s AND status IN ('success', 'success_with_rejections')
        """,
        (snapshot,),
    )
    loads = cur.fetchall()
    if len(loads) != 1:
        raise ValueError(f"Expected exactly one successful load of '{snapshot}'")
    cur.execute(
        "SELECT city_id::text FROM city WHERE attributes_snapshot_id = %s ORDER BY city_id",
        (snapshot,),
    )
    cities = [city for (city,) in cur]
    if not cities:
        raise ValueError(f"Snapshot '{snapshot}' has no cities")
    return loads[0][0], cities


def _ready_graphs(
    cur: psycopg.Cursor, snapshot: str, load: UUID
) -> dict[tuple[str, str], tuple[UUID, int, int]]:
    cur.execute(
        """
        SELECT g.graph_snapshot_id, g.parameters,
            (SELECT count(*) FROM graph_node n WHERE n.graph_snapshot_id = g.graph_snapshot_id),
            (SELECT count(*) FROM graph_edge e WHERE e.graph_snapshot_id = g.graph_snapshot_id)
        FROM graph_snapshot g JOIN graph_snapshot_input i USING (graph_snapshot_id)
        WHERE g.methodology_version = %s AND g.status = 'ready' AND g.is_directed
          AND i.snapshot_id = %s AND i.load_run_id = %s
          AND NOT EXISTS (
              SELECT 1 FROM graph_snapshot_input other
              WHERE other.graph_snapshot_id = g.graph_snapshot_id AND other.snapshot_id <> %s
          )
        """,
        (METHODOLOGY_VERSION, snapshot, load, snapshot),
    )
    ready = {}
    for graph_id, params, nodes, edges in cur:
        if not isinstance(params, dict):
            continue
        layer = params.get("layer")
        try:
            alpha = Decimal(str(params.get("alpha")))
        except InvalidOperation:
            continue
        if layer not in LAYERS or not alpha.is_finite() or alpha not in ALPHAS:
            continue
        key = (layer, f"{alpha:.2f}")
        if {**params, "alpha": key[1]} != _parameters(*key):
            continue
        if key in ready:
            raise ValueError(f"More than one ready graph for {key} in '{snapshot}'")
        ready[key] = graph_id, nodes, edges
    return ready


def _parameters(layer: str, alpha: str) -> dict[str, str | int]:
    return {"layer": layer, "alpha": alpha, "air_access_sec": int(AIR_ACCESS_SEC)}


def _publish_graph(
    cur: psycopg.Cursor,
    snapshot: str,
    load: UUID,
    cities: list[str],
    params: dict[str, str | int],
    edges: list[tuple[str, str, str, Decimal, UUID]],
    code_commit: str,
) -> UUID:
    cur.execute(
        """
        INSERT INTO graph_snapshot
            (methodology_version, parameters, is_directed, status, code_commit)
        VALUES (%s, %s, true, 'building', %s) RETURNING graph_snapshot_id
        """,
        (METHODOLOGY_VERSION, Jsonb(params), code_commit),
    )
    graph_id = cur.fetchone()[0]
    cur.execute(
        """
        INSERT INTO graph_snapshot_input (graph_snapshot_id, snapshot_id, load_run_id)
        VALUES (%s, %s, %s)
        """,
        (graph_id, snapshot, load),
    )
    with cur.copy("COPY graph_node (graph_snapshot_id, city_id) FROM STDIN") as copy:
        for city in cities:
            copy.write_row((graph_id, city))
    with cur.copy(
        """
        COPY graph_edge (
            graph_snapshot_id, from_city_id, to_city_id, transport_type,
            weight, weight_unit, measurement_id
        ) FROM STDIN
        """
    ) as copy:
        for origin, target, kind, seconds, measurement in edges:
            copy.write_row((graph_id, origin, target, kind, seconds, "sec", measurement))
    cur.execute(
        "UPDATE graph_snapshot SET status = 'ready' WHERE graph_snapshot_id = %s", (graph_id,)
    )
    return graph_id


def build_graphs(
    snapshot: str, versions: Sequence[tuple[str, Decimal]], *, code_commit: str
) -> list[GraphBuild]:
    """Build requested versions atomically or reuse already published versions."""
    if not versions or any(
        not alpha.is_finite() or (layer, alpha) not in ALL_VERSIONS for layer, alpha in versions
    ):
        raise ValueError("Expected a layer road, rail or best and alpha 0.05, 0.10 or 0.20")
    with psycopg.connect(get_connection_string()) as conn, conn.cursor() as cur:
        conn.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"transport_connectivity:build-graph:{snapshot}",),
        )
        load, cities = _snapshot_input(cur, snapshot)
        ready = _ready_graphs(cur, snapshot, load)
        requested = list(dict.fromkeys((layer, f"{alpha:.2f}") for layer, alpha in versions))
        times_by_layer = {}
        measurements, measurement_ids, winners = [], {}, {}
        if any(key not in ready for key in requested):
            cur.execute(
                """
                SELECT from_city_id::text, to_city_id::text, transport_type,
                       duration_sec, measurement_id
                FROM route_measurement WHERE snapshot_id = %s
                """,
                (snapshot,),
            )
            city_ids = set(cities)
            for origin, target, kind, seconds, measurement in cur:
                if origin not in city_ids or target not in city_ids:
                    raise ValueError(f"Measurement endpoint is outside snapshot '{snapshot}'")
                measurements.append(TimedMeasurement(origin, target, kind, seconds))
                measurement_ids[origin, target, kind] = measurement
            if any(key[0] == "best" and key not in ready for key in requested):
                winners = best_transport_types(measurements)

        result = []
        for layer, alpha in requested:
            existing = ready.get((layer, alpha))
            if existing is not None:
                graph_id, nodes, edges = existing
                if nodes != len(cities):
                    raise ValueError(f"Ready graph {graph_id} has an unexpected vertex count")
                result.append(GraphBuild(graph_id, layer, alpha, nodes, edges, None, False))
                continue
            if layer not in times_by_layer:
                times_by_layer[layer] = layer_times(measurements, layer)
            times = times_by_layer[layer]
            kept = prune(times, Decimal(alpha))
            edges_to_copy = []
            for origin, target in sorted(kept):
                kind = winners[origin, target] if layer == "best" else layer
                edges_to_copy.append(
                    (
                        origin,
                        target,
                        kind,
                        times[origin, target],
                        measurement_ids[origin, target, kind],
                    )
                )
            graph_id = _publish_graph(
                cur, snapshot, load, cities, _parameters(layer, alpha), edges_to_copy, code_commit
            )
            result.append(
                GraphBuild(
                    graph_id,
                    layer,
                    alpha,
                    len(cities),
                    len(kept),
                    unreachable_pairs(times, kept),
                    True,
                )
            )
        return result
