"""Run the offline Issue #35 scenarios on a normalized, verified snapshot."""

from __future__ import annotations

import argparse
import csv
import json
from collections.abc import Mapping
from dataclasses import asdict
from decimal import Decimal
from hashlib import sha256
from itertools import combinations
from pathlib import Path

from transport_connectivity import graph as methodology
from transport_connectivity.connector import atomic_write_bytes, sha256_file
from transport_connectivity.rasp_stations import distance_km
from transport_connectivity.resilience import (
    Graph,
    add_bidirectional_link,
    adjacency,
    betweenness,
    reach_population,
    removal_effect,
    shortest_times,
    suggest_links,
)

METHODOLOGY_REF = "c0117c7990bac37da1124a9666d26e5e786418f5"
METHODOLOGY_SHA256 = "7f145fdca53ffa5d09292d124cba931eb811f6f201bbe96db343a446a61c7a62"
ALPHAS = (Decimal("0.05"), Decimal("0.10"), Decimal("0.20"))
THRESHOLD_SEC = 21600
VSM_TIME_SEC = Decimal(8100)
VSM_SOURCE_IDS = ("2539", "2460")


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def read_snapshot(
    snapshot: Path,
) -> tuple[dict, dict[str, dict[str, str]], list[methodology.TimedMeasurement]]:
    report = json.loads((snapshot / "run-report.json").read_bytes())
    if report["status"] != "success" or report["snapshot_id"] != "tochno_intercity_v20260629":
        raise ValueError("expected the successful normalisation of tochno_intercity_v20260629")
    for name in ("cities.csv", "route_measurements.csv"):
        if sha256_file(snapshot / name) != report["output"][name]["sha256"]:
            raise ValueError(f"SHA-256 differs from the normalization report: {name}")
    cities = {row["city_id"]: row for row in read_rows(snapshot / "cities.csv")}
    measurements = [
        methodology.TimedMeasurement(
            row["from_city_id"],
            row["to_city_id"],
            row["transport_type"],
            Decimal(row["duration_sec"]) if row["duration_sec"] else None,
        )
        for row in read_rows(snapshot / "route_measurements.csv")
    ]
    return report, cities, measurements


def removal_row(graph: Graph, city: str, baseline: Mapping[str, Mapping[str, Decimal]]) -> dict:
    effect = removal_effect(graph, city, baseline)
    return {
        "city_id": city,
        **asdict(effect),
        "lost_pairs": effect.lost_pairs,
        "mean_time_change_percent": effect.mean_time_change_percent,
    }


def link_effect(
    graph: Graph, first: str, second: str, weight: Decimal, population: dict[str, int]
) -> list[dict]:
    updated = add_bidirectional_link(graph, first, second, weight)
    rows = []
    for origin in graph:
        before = reach_population(shortest_times(graph, origin), population, origin, THRESHOLD_SEC)
        after = reach_population(shortest_times(updated, origin), population, origin, THRESHOLD_SEC)
        rows.append(
            {
                "city_id": origin,
                "population_before": before,
                "population_after": after,
                "population_delta": after - before,
            }
        )
    return rows


def automatic_links(
    graph: Graph,
    baseline: Mapping[str, Mapping[str, Decimal]],
    cities: dict[str, dict[str, str]],
    population: dict[str, int],
) -> dict:
    coords = {
        city: (float(row["latitude"]), float(row["longitude"])) for city, row in cities.items()
    }
    distances = {
        (source, target): distance_km(*coords[source], *coords[target])
        for source, target in combinations(sorted(coords), 2)
    }
    speed, proposals = suggest_links(graph, baseline, distances, limit=5)
    return {
        "mean_direct_speed_km_h": speed,
        "links": [
            {
                **asdict(proposal),
                "population": link_effect(
                    graph,
                    proposal.source,
                    proposal.target,
                    Decimal(proposal.proposed_time_sec),
                    population,
                ),
            }
            for proposal in proposals
        ],
    }


def build_report(snapshot: Path, rank_removals: bool) -> dict:
    graph_source = Path(methodology.__file__).read_text(encoding="utf-8")
    if sha256(graph_source.encode("utf-8")).hexdigest() != METHODOLOGY_SHA256:
        raise ValueError(f"expected graph.py from {METHODOLOGY_REF}")
    report, cities, measurements = read_snapshot(snapshot)
    city_by_source_id = {
        row["source_city_id"]: city
        for city, row in cities.items()
        if row["id_namespace"] == "sber_territory"
    }
    moscow, petersburg = (city_by_source_id[source_id] for source_id in VSM_SOURCE_IDS)
    population = {city: int(row["population"] or 0) for city, row in cities.items()}
    versions = []
    for layer in methodology.LAYERS:
        times = methodology.layer_times(measurements, layer)
        for alpha in ALPHAS:
            graph = adjacency(
                cities, ((a, b, times[a, b]) for a, b in sorted(methodology.prune(times, alpha)))
            )
            centrality = betweenness(graph)
            version = {
                "layer": layer,
                "alpha": str(alpha),
                "edges": sum(map(len, graph.values())),
                "top_betweenness": [
                    {"city_id": city, "score": score}
                    for city, score in sorted(
                        centrality.items(), key=lambda item: (-item[1], item[0])
                    )[:10]
                ],
            }
            if layer == "best":
                baseline = {origin: shortest_times(graph, origin) for origin in graph}
                version["moscow_removal"] = removal_row(graph, moscow, baseline)
                if alpha == ALPHAS[0]:
                    version["automatic_links"] = automatic_links(
                        graph, baseline, cities, population
                    )
                if rank_removals and alpha == ALPHAS[0]:
                    ranking = [removal_row(graph, city, baseline) for city in graph]
                    version["removal_ranking"] = sorted(
                        ranking,
                        key=lambda row: (
                            -row["lost_pairs"],
                            row["mean_time_change_percent"] is None,
                            -(row["mean_time_change_percent"] or 0),
                            row["city_id"],
                        ),
                    )
            if layer in ("rail", "best"):
                version["vsm_population"] = link_effect(
                    graph, moscow, petersburg, VSM_TIME_SEC, population
                )
            versions.append(version)
            print(f"{layer} {alpha}: {version['edges']} edges", flush=True)
    return {
        "snapshot_id": report["snapshot_id"],
        "input_csv_sha256": report["input"]["sha256"],
        "normalized_sha256": {
            name: report["output"][name]["sha256"]
            for name in ("cities.csv", "route_measurements.csv")
        },
        "methodology_ref": METHODOLOGY_REF,
        "methodology_sha256": METHODOLOGY_SHA256,
        "threshold_sec": THRESHOLD_SEC,
        "vsm_time_sec": int(VSM_TIME_SEC),
        "vsm_endpoints": [moscow, petersburg],
        "cities_without_population": sum(not row["population"] for row in cities.values()),
        "city_names": {city: row["name"] for city, row in cities.items()},
        "versions": versions,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--rank-removals", action="store_true", help="rank all cities for best/0.05"
    )
    args = parser.parse_args()
    report = build_report(args.snapshot, args.rank_removals)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    atomic_write_bytes(args.output, (rendered + "\n").encode("utf-8"))


if __name__ == "__main__":
    main()
