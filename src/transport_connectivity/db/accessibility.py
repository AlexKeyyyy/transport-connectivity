"""Compute and publish matrix accessibility for a loaded PostgreSQL snapshot."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import asdict
from decimal import Decimal
from platform import python_version
from typing import NamedTuple
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from transport_connectivity.accessibility import THRESHOLDS_SEC, accessibility, region_summary
from transport_connectivity.db.migrate import get_connection_string
from transport_connectivity.graph import AIR_ACCESS_SEC, LAYERS, Pair, TimedMeasurement, layer_times

DEFAULT_SNAPSHOT_ID = "tochno_intercity_v20260629"
ACCESSIBILITY = "threshold_accessibility"
TRAVEL_TIMES = "travel_times"
MetricRow = tuple[str, str, str | None, int | Decimal | None]


class City(NamedTuple):
    name: str | None
    region: str | None
    population: int | None


class MatrixRun(NamedTuple):
    algorithm: str
    layer: str
    threshold_sec: int | None = None

    def parameters(self) -> dict[str, str | int]:
        if self.threshold_sec is None:
            return {"layer": self.layer}
        return {
            "layer": self.layer,
            "threshold_sec": self.threshold_sec,
            "air_access_sec": int(AIR_ACCESS_SEC),
        }


def _read_cities(cur: psycopg.Cursor, snapshot_id: str) -> dict[str, City]:
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM load_run
            WHERE snapshot_id = %s AND status IN ('success', 'success_with_rejections')
        )
        """,
        (snapshot_id,),
    )
    if not cur.fetchone()[0]:
        raise ValueError(f"Snapshot '{snapshot_id}' has no successful load")
    cur.execute(
        """
        SELECT city_id::text, name, region, population
        FROM city WHERE attributes_snapshot_id = %s ORDER BY city_id
        """,
        (snapshot_id,),
    )
    cities = {city: City(name, region, population) for city, name, region, population in cur}
    if not cities:
        raise ValueError(f"Snapshot '{snapshot_id}' has no cities")
    return cities


def _read_measurements(
    cur: psycopg.Cursor, snapshot_id: str, cities: dict[str, City]
) -> list[TimedMeasurement]:
    cur.execute(
        """
        SELECT from_city_id::text, to_city_id::text, transport_type, duration_sec
        FROM route_measurement WHERE snapshot_id = %s
        """,
        (snapshot_id,),
    )
    measurements = []
    for origin, target, transport, seconds in cur:
        if origin not in cities or target not in cities:
            raise ValueError(f"Measurement endpoint is outside snapshot '{snapshot_id}'")
        measurements.append(TimedMeasurement(origin, target, transport, seconds))
    return measurements


def _ready_runs(
    cur: psycopg.Cursor, snapshot_id: str, planned: list[MatrixRun]
) -> dict[MatrixRun, UUID]:
    cur.execute(
        """
        SELECT analysis_run_id, algorithm, parameters
        FROM analysis_run
        WHERE snapshot_id = %s AND engine = 'python' AND status = 'ready'
          AND scenario IS NULL AND algorithm IN ('threshold_accessibility', 'travel_times')
        ORDER BY finished_at DESC NULLS LAST, analysis_run_id
        """,
        (snapshot_id,),
    )
    runs = {}
    for run_id, algorithm, parameters in cur:
        if not isinstance(parameters, dict):
            continue
        planned_run = MatrixRun(algorithm, parameters.get("layer"), parameters.get("threshold_sec"))
        if planned_run in planned and parameters == planned_run.parameters():
            runs.setdefault(planned_run, run_id)
    return runs


def _publish_run(
    cur: psycopg.Cursor,
    snapshot_id: str,
    run: MatrixRun,
    code_version: str,
    rows: Iterable[MetricRow],
) -> UUID:
    cur.execute(
        """
        INSERT INTO analysis_run (
            snapshot_id, algorithm, engine, engine_version, parameters, code_commit,
            status, started_at
        )
        VALUES (%s, %s, 'python', %s, %s, %s, 'running', clock_timestamp())
        RETURNING analysis_run_id
        """,
        (snapshot_id, run.algorithm, python_version(), Jsonb(run.parameters()), code_version),
    )
    run_id = cur.fetchone()[0]
    # The run and its rows become visible together on commit.
    with cur.copy(
        """
        COPY metric_result (analysis_run_id, metric, city_id, other_city_id, value)
        FROM STDIN
        """
    ) as copy:
        for row in rows:
            copy.write_row((run_id, *row))
    cur.execute(
        """
        UPDATE analysis_run SET status = 'ready', finished_at = clock_timestamp()
        WHERE analysis_run_id = %s
        """,
        (run_id,),
    )
    return run_id


def _metric_rows(
    run: MatrixRun, times: dict[Pair, Decimal], population: dict[str, int | None]
) -> Iterator[MetricRow]:
    if run.threshold_sec is None:
        for origin in population:
            for target in population:
                if origin != target:
                    yield "travel_time_sec", origin, target, times.get((origin, target))
        return
    for origin, (count, inhabitants) in accessibility(times, population, run.threshold_sec).items():
        yield "reach_cities", origin, None, count
        yield "reach_population", origin, None, inhabitants


def _half_day_summary(cur: psycopg.Cursor, run_id: UUID, cities: dict[str, City]) -> dict:
    cur.execute(
        """
        SELECT city_id::text,
               max(value) FILTER (WHERE metric = 'reach_cities'),
               max(value) FILTER (WHERE metric = 'reach_population')
        FROM metric_result
        WHERE analysis_run_id = %s AND metric IN ('reach_cities', 'reach_population')
        GROUP BY city_id
        """,
        (run_id,),
    )
    results = {city: (int(count), int(population)) for city, count, population in cur}
    city_rows = [
        {
            "city_id": city,
            "name": cities[city].name,
            "region": cities[city].region,
            "reach_cities": count,
            "reach_population": inhabitants,
        }
        for city, (count, inhabitants) in results.items()
    ]
    top = sorted(city_rows, key=lambda row: (-row["reach_population"], row["city_id"]))[:10]
    lowest = sorted(city_rows, key=lambda row: (row["reach_population"], row["city_id"]))
    regions = region_summary(results, {city: item.region for city, item in cities.items()})
    return {
        "layer": "best",
        "threshold_sec": THRESHOLDS_SEC["half_day"],
        "top_10": top,
        "bottom_10": lowest[:10],
        "lowest_decile": lowest[: max(1, len(lowest) // 10)],
        "regions": [asdict(region) for region in regions],
    }


def run_accessibility(snapshot_id: str, *, code_version: str) -> dict:
    """Publish each run once and return the six-hour summary from stored metrics."""
    thresholds = (None, *THRESHOLDS_SEC.values())
    planned = [
        MatrixRun(TRAVEL_TIMES if seconds is None else ACCESSIBILITY, layer, seconds)
        for layer in LAYERS
        for seconds in thresholds
    ]
    with psycopg.connect(get_connection_string()) as conn:
        conn.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
        with conn.cursor() as cur:
            # The current schema has no unique key on run parameters. Serialize writers
            # for this snapshot so a second invocation sees complete runs after the lock.
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"transport_connectivity:accessibility:{snapshot_id}",),
            )
            cities = _read_cities(cur, snapshot_id)
            population = {city: item.population for city, item in cities.items()}
            ready = _ready_runs(cur, snapshot_id, planned)
            pending = [run for run in planned if run not in ready]
            if pending:
                measurements = _read_measurements(cur, snapshot_id, cities)
                for layer in LAYERS:
                    layer_runs = [run for run in pending if run.layer == layer]
                    if not layer_runs:
                        continue
                    times = layer_times(measurements, layer)
                    for run in layer_runs:
                        ready[run] = _publish_run(
                            cur,
                            snapshot_id,
                            run,
                            code_version,
                            _metric_rows(run, times, population),
                        )

            best_half_day = MatrixRun(ACCESSIBILITY, "best", THRESHOLDS_SEC["half_day"])
            return {
                "snapshot_id": snapshot_id,
                "cities": len(cities),
                "cities_without_population": sum(value is None for value in population.values()),
                "runs_created": len(pending),
                "runs_reused": len(planned) - len(pending),
                "best_half_day": _half_day_summary(cur, ready[best_half_day], cities),
            }
