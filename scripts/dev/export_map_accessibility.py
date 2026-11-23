"""Export availability metrics from the validated Tochny CSV snapshot for the map."""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from make_sample_results import THRESHOLDS_SEC, dump, read_cities

from transport_connectivity.connector import atomic_write_bytes, sha256_file

SNAPSHOT = "tochno_intercity_v20260629"
LAYERS = ("road", "rail", "air", "best")
AIR_ACCESS_SEC = 10800


def build(snapshot: Path) -> dict:
    report = json.loads((snapshot / "run-report.json").read_bytes())
    if report["snapshot_id"] != SNAPSHOT or report["status"] != "success":
        raise ValueError("Требуется успешно нормализованный снимок 20260629")
    for name in ("cities.csv", "route_measurements.csv"):
        if sha256_file(snapshot / name) != report["output"][name]["sha256"]:
            raise ValueError(f"Контрольная сумма {name} отличается от отчёта обработки")

    cities = read_cities(snapshot / "cities.csv")
    indices = {city["city_id"]: i for i, city in enumerate(cities)}
    times: dict[str, dict[tuple[int, int], Decimal]] = {layer: {} for layer in LAYERS}
    with (snapshot / "route_measurements.csv").open(encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file):
            if not row["duration_sec"]:
                continue
            layer = row["transport_type"]
            pair = (indices[row["from_city_id"]], indices[row["to_city_id"]])
            duration = Decimal(row["duration_sec"])
            if layer == "air":
                duration += AIR_ACCESS_SEC
            times[layer][pair] = duration
            times["best"][pair] = min(duration, times["best"].get(pair, duration))

    runs = []
    results = []
    for layer, pairs in times.items():
        by_origin = defaultdict(dict)
        for (origin, target), duration in pairs.items():
            by_origin[origin][target] = duration
        rows = [
            [origin, target, float(duration) if duration is not None else None]
            for origin in range(len(cities))
            for target in range(len(cities))
            if origin != target
            for duration in (by_origin[origin].get(target),)
        ]
        results.append({"run": len(runs), "metric": "travel_time_sec", "rows": rows})
        runs.append({"parameters": {"layer": layer}, "scenario": None})
        for threshold in THRESHOLDS_SEC:
            rows = [
                [
                    origin,
                    None,
                    sum(
                        cities[target]["population"]
                        for target, duration in by_origin[origin].items()
                        if duration <= threshold
                    ),
                ]
                for origin in range(len(cities))
            ]
            results.append({"run": len(runs), "metric": "reach_population", "rows": rows})
            runs.append(
                {"parameters": {"layer": layer, "threshold_sec": threshold}, "scenario": None}
            )

    return {
        "warning": "Снимок «Если быть точным» от 29.06.2026. Время в пути — оценка источника.",
        "snapshot_id": SNAPSHOT,
        "air_access_sec": AIR_ACCESS_SEC,
        "measurement_sha256": report["output"]["route_measurements.csv"]["sha256"],
        "cities": cities,
        "runs": runs,
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=Path("data/normalized") / SNAPSHOT)
    parser.add_argument(
        "--output", type=Path, default=Path("data/normalized/map_accessibility.json")
    )
    args = parser.parse_args()
    atomic_write_bytes(args.output, dump(build(args.snapshot)).encode("utf-8"))
    print(args.output)


if __name__ == "__main__":
    main()
