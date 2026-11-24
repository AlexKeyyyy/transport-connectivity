"""Read published map inputs from PostgreSQL."""

from __future__ import annotations

from collections.abc import Mapping
from os import getenv
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from transport_connectivity.db.accessibility import DEFAULT_SNAPSHOT_ID
from transport_connectivity.db.migrate import get_connection_string

# All graph metrics on the map use the same published reference graph.
_REFERENCE_GRAPH = """
    SELECT g.graph_snapshot_id
    FROM graph_snapshot g JOIN graph_snapshot_input i USING (graph_snapshot_id)
    WHERE i.snapshot_id = %(snapshot)s AND g.status = 'ready' AND g.is_directed
      AND g.methodology_version = '1.0' AND g.parameters->>'layer' = 'best'
      AND CASE WHEN pg_input_is_valid(g.parameters->>'alpha', 'numeric')
               THEN (g.parameters->>'alpha')::numeric END = 0.05
    ORDER BY g.created_at DESC, g.graph_snapshot_id
    LIMIT 1
"""


def _snapshot() -> str:
    return getenv("MAP_SNAPSHOT_ID", DEFAULT_SNAPSHOT_ID)


def city_rows() -> list[tuple]:
    with psycopg.connect(get_connection_string()) as conn:
        return conn.execute(
            """
            SELECT city_id::text, coalesce(name, city_id::text), coalesce(region, '—'),
                   ST_Y(location), ST_X(location), population
            FROM city WHERE attributes_snapshot_id = %s
            """,
            (_snapshot(),),
        ).fetchall()


def scenario_rows() -> list[dict[str, Any]]:
    with psycopg.connect(get_connection_string()) as conn:
        rows = conn.execute(
            f"""
            WITH reference_graph AS ({_REFERENCE_GRAPH})
            SELECT ar.scenario
            FROM analysis_run ar JOIN reference_graph USING (graph_snapshot_id)
            WHERE ar.status = 'ready' AND ar.parameters->>'layer' = 'best'
              AND ar.parameters->>'threshold_sec' IN ('10800', '21600', '86400')
              AND ar.scenario->>'type' IN ('remove_node', 'add_edge')
              AND ar.scenario->>'name' IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM metric_result m
                  WHERE m.analysis_run_id = ar.analysis_run_id
                    AND m.metric = 'scenario_reach_population_delta'
              )
            GROUP BY ar.scenario
            HAVING count(DISTINCT ar.parameters->>'threshold_sec') = 3
            ORDER BY ar.scenario->>'name'
            """,
            {"snapshot": _snapshot()},
        )
        return [scenario for (scenario,) in rows]


def published_layers() -> set[str]:
    with psycopg.connect(get_connection_string()) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT parameters->>'layer'
            FROM analysis_run
            WHERE snapshot_id = %s AND status = 'ready' AND scenario IS NULL
              AND algorithm = 'travel_times'
            """,
            (_snapshot(),),
        )
        return {layer for (layer,) in rows}


def metric_values(
    metric: str,
    parameters: Mapping[str, Any],
    scenario: Mapping[str, Any] | None,
    origin: str | None = None,
) -> dict[tuple[str, str | None], float | None]:
    filters = {
        "snapshot": _snapshot(),
        "metric": metric,
        "parameters": Jsonb(dict(parameters)),
        "scenario": Jsonb(dict(scenario)) if scenario is not None else None,
        "origin": origin,
        "graph_metric": metric == "betweenness" or scenario is not None,
    }
    # The UUID filter keeps the travel table indexed by origin rather than loading all pairs.
    with psycopg.connect(get_connection_string()) as conn:
        rows = conn.execute(
            f"""
            WITH reference_graph AS ({_REFERENCE_GRAPH}), chosen_run AS (
                SELECT ar.analysis_run_id FROM analysis_run ar
                WHERE ar.status = 'ready'
                  AND ar.scenario IS NOT DISTINCT FROM %(scenario)s::jsonb
                  AND ar.parameters @> %(parameters)s
                  AND (
                    (%(graph_metric)s AND ar.graph_snapshot_id =
                        (SELECT graph_snapshot_id FROM reference_graph))
                    OR (NOT %(graph_metric)s AND ar.snapshot_id = %(snapshot)s)
                  )
                  AND EXISTS (
                      SELECT 1 FROM metric_result m
                      WHERE m.analysis_run_id = ar.analysis_run_id AND m.metric = %(metric)s
                  )
                ORDER BY ar.finished_at DESC NULLS LAST, ar.analysis_run_id
                LIMIT 1
            )
            SELECT m.city_id::text, m.other_city_id::text, m.value
            FROM metric_result m JOIN chosen_run USING (analysis_run_id)
            WHERE m.metric = %(metric)s
              AND (%(origin)s::uuid IS NULL OR m.city_id = %(origin)s::uuid)
            """,
            filters,
        )
        return {
            (city, other): float(value) if value is not None else None
            for city, other, value in rows
        }
