"""Compare local worker counts on the same node-removal workload."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal
from functools import partial
from pathlib import Path
from statistics import median
from time import perf_counter

from resilience_report import read_snapshot

from transport_connectivity.graph import layer_times, prune
from transport_connectivity.resilience import (
    Graph,
    RemovalEffect,
    adjacency,
    removal_effect,
    shortest_times,
)

Baseline = Mapping[str, Mapping[str, Decimal]]


def remove_block(graph: Graph, baseline: Baseline, cities: list[str]) -> dict[str, RemovalEffect]:
    return {city: removal_effect(graph, city, baseline) for city in cities}


def measure(
    graph: Graph, baseline: Baseline, removed: list[str], workers: int
) -> tuple[float, dict[str, RemovalEffect]]:
    blocks = [removed[index::workers] for index in range(workers)]
    started = perf_counter()
    with ProcessPoolExecutor(max_workers=workers) as pool:
        results = pool.map(partial(remove_block, graph, baseline), blocks)
        effects = {city: effect for result in results for city, effect in result.items()}
    return perf_counter() - started, effects


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot", type=Path, default=Path("data/normalized/tochno_intercity_v20260629")
    )
    args = parser.parse_args()
    _, cities, measurements = read_snapshot(args.snapshot)
    times = layer_times(measurements, "best")
    edges = sorted(prune(times, Decimal("0.05")))
    graph = adjacency(cities, ((u, v, times[u, v]) for u, v in edges))
    baseline = {city: shortest_times(graph, city) for city in graph}
    # Rank by population and break ties by UUID to use the same cases on every run.
    removed = sorted(cities, key=lambda city: (-int(cities[city]["population"] or 0), city))[:24]
    expected = remove_block(graph, baseline, removed)
    samples: dict[int, list[float]] = {1: [], 2: []}
    for order in ((1, 2), (2, 1), (1, 2)):
        for workers in order:
            duration, result = measure(graph, baseline, removed, workers)
            if result != expected:
                raise RuntimeError(f"removal result differs with {workers} workers")
            samples[workers].append(duration)
    print(
        json.dumps(
            {
                "cities": len(cities),
                "edges": len(edges),
                "scenarios": len(removed),
                "samples_sec": samples,
                "median_sec": {workers: median(values) for workers, values in samples.items()},
                "results_equal": True,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
