"""Temporary report for docs/graph-methodology.md (Issue #28, part 1).

Reads the normalized snapshot (cities.csv, route_measurements.csv from ``process``), builds
the 9 graph versions (3 layers x 3 alpha) and prints a Markdown table plus a worked example
of pruning. Nothing is written to disk and no database is used.

    python scripts/data/graph_report.py
    python scripts/data/graph_report.py --example "Москва" "Тула" "Орёл"
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from decimal import Decimal
from pathlib import Path

from transport_connectivity.graph import (
    AIR_ACCESS_SEC,
    LAYERS,
    TimedMeasurement,
    best_transport_types,
    layer_times,
    prune,
    unreachable_pairs,
)

DEFAULT_DIR = Path("data/normalized/tochno_intercity_v20260629")
ALPHAS = (Decimal("0.05"), Decimal("0.10"), Decimal("0.20"))


def read_cities(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8", newline="") as handle:
        return {row["city_id"]: row["name"] for row in csv.DictReader(handle)}


def read_measurements(path: Path) -> list[TimedMeasurement]:
    with path.open(encoding="utf-8", newline="") as handle:
        return [
            TimedMeasurement(
                row["from_city_id"],
                row["to_city_id"],
                row["transport_type"],
                Decimal(row["duration_sec"]) if row["duration_sec"] else None,
            )
            for row in csv.DictReader(handle)
        ]


def format_row(
    layer: str, alpha: Decimal, nodes: int, times: dict, edges: set, unreachable: int
) -> str:
    pairs = len(times)
    return (
        f"| {layer} | {alpha} | {nodes} | {pairs} | {len(edges)} | "
        f"{len(edges) / nodes:.2f} | {unreachable} |"
    )


def find_city(names: dict[str, str], name: str) -> str:
    matches = [city_id for city_id, city_name in names.items() if city_name == name]
    if len(matches) != 1:
        raise SystemExit(f"expected exactly one city named {name!r}, found {len(matches)}")
    return matches[0]


def print_example(
    names: dict[str, str], times: dict, kept: dict, origin: str, via: str, target: str
) -> None:
    a, c, b = (find_city(names, name) for name in (origin, via, target))
    direct, first, second = times.get((a, b)), times.get((a, c)), times.get((c, b))
    print(f"\nExample (best layer): {origin} -> {via} -> {target}")
    print(f"  t({origin},{target}) = {direct} s")
    print(f"  t({origin},{via}) + t({via},{target}) = {first} + {second} s")
    if None not in (direct, first, second):
        detour = first + second
        print(f"  detour = {detour} s, ratio to direct = {detour / direct:.4f}")
        for alpha, edges in kept.items():
            status = "kept" if (a, b) in edges else "removed"
            print(f"  alpha = {alpha}: edge {origin}->{target} {status}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--example", nargs=3, metavar=("FROM", "VIA", "TO"))
    args = parser.parse_args()

    names = read_cities(args.input_dir / "cities.csv")
    measurements = read_measurements(args.input_dir / "route_measurements.csv")
    print(
        f"cities: {len(names)}, measurements: {len(measurements)}, air_access_sec: {AIR_ACCESS_SEC}"
    )
    print("| layer | alpha | nodes | pairs in matrix | edges | mean out-degree | unreachable |")
    print("|---|---|---:|---:|---:|---:|---:|")

    best_kept: dict[Decimal, set] = {}
    best_times: dict = {}
    for layer in LAYERS:
        times = layer_times(measurements, layer)
        if layer == "best":
            best_times = times
        for alpha in ALPHAS:
            started = time.perf_counter()
            edges = prune(times, alpha)
            unreachable = unreachable_pairs(times, edges)
            if layer == "best":
                best_kept[alpha] = edges
            print(format_row(layer, alpha, len(names), times, edges, unreachable))
            print(f"  ({time.perf_counter() - started:.1f} s)", file=sys.stderr)

    shares = {}
    for transport in best_transport_types(measurements).values():
        shares[transport] = shares.get(transport, 0) + 1
    print(f"\nbest layer, winning type by pair: {dict(sorted(shares.items()))}")
    if args.example:
        print_example(names, best_times, best_kept, *args.example)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
