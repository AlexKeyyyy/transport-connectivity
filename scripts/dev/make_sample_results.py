"""Generate the map stub src/transport_connectivity/app/sample_results.json.

The values are NOT real: travel times are great-circle distance divided by a made-up
speed per transport type with seeded noise. They exist only so that the map (#36) can be
built before the accessibility (#33) and resilience (#35) runs. The rows follow the
``metric_result`` reference from #32; parameters live in the runs, not in metric names.

To keep the file small, ``travel_time_sec`` is stored only for the largest origin cities;
for the rest the stub has no run result, which the map must show as "not computed",
unlike ``null`` ("no connection").

Usage: python scripts/dev/make_sample_results.py [--cities path/to/cities.csv]
The same input always produces a byte-identical file.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
from pathlib import Path

DEFAULT_CITIES = Path("data/normalized/tochno_intercity_v20260629/cities.csv")
OUTPUT = Path("src/transport_connectivity/app/sample_results.json")

SEED = 20261121
TRAVEL_TIME_ORIGINS = 15
LAYERS = ("road", "rail", "air", "multimodal")
THRESHOLDS_SEC = (3 * 3600, 6 * 3600, 24 * 3600)
# Made-up figures, chosen only to look plausible on the map.
ROAD_KMH, ROAD_DETOUR = 70.0, 1.3
RAIL_KMH, RAIL_DETOUR, RAIL_OVERHEAD_SEC, RAIL_MISSING_SHARE = 60.0, 1.4, 3600, 0.2
AIR_KMH, AIR_ACCESS_SEC, AIR_MIN_KM, AIRPORT_MIN_POPULATION = 700.0, 3 * 3600, 300.0, 250_000
HSR_SEC = 8100  # Moscow — Saint Petersburg high-speed rail, 2 h 15 min
MOSCOW, SAINT_PETERSBURG = "Москва", "Санкт-Петербург"


def read_cities(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    cities = [
        {
            "city_id": row["city_id"],
            "name": row["name"],
            "region": row["region"],
            "latitude": float(row["latitude"]),
            "longitude": float(row["longitude"]),
            "population": int(row["population"]),
        }
        for row in rows
    ]
    return sorted(cities, key=lambda city: city["city_id"])


def distance_km(a: dict, b: dict) -> float:
    lat1, lon1, lat2, lon2 = map(
        math.radians, (a["latitude"], a["longitude"], b["latitude"], b["longitude"])
    )
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def fake_times(cities: list[dict], rng: random.Random) -> dict[str, list[list[int | None]]]:
    """Symmetric fake travel time matrices per layer; None means no connection."""
    n = len(cities)
    times: dict[str, list[list[int | None]]] = {
        layer: [[None] * n for _ in range(n)] for layer in LAYERS
    }
    for i in range(n):
        for j in range(i + 1, n):
            km = distance_km(cities[i], cities[j])
            noise = rng.uniform(0.9, 1.1)
            road = round(km * ROAD_DETOUR / ROAD_KMH * 3600 * noise)
            rail = None
            if rng.random() >= RAIL_MISSING_SHARE:
                rail = round(km * RAIL_DETOUR / RAIL_KMH * 3600 * noise) + RAIL_OVERHEAD_SEC
            air = None
            both_airports = min(cities[i]["population"], cities[j]["population"]) >= (
                AIRPORT_MIN_POPULATION
            )
            if both_airports and km >= AIR_MIN_KM:
                air = round(km / AIR_KMH * 3600 * noise) + AIR_ACCESS_SEC
            values = {"road": road, "rail": rail, "air": air}
            values["multimodal"] = min(v for v in values.values() if v is not None)
            for layer, value in values.items():
                times[layer][i][j] = times[layer][j][i] = value
    return times


def reach(
    times: list[list[int | None]], cities: list[dict], threshold: int, removed: int | None = None
) -> list[tuple[int, int]]:
    """(cities, population) reachable within the threshold from every city."""
    result = []
    for i, row in enumerate(times):
        reached = [
            j
            for j, value in enumerate(row)
            if j != i and j != removed and value is not None and value <= threshold
        ]
        result.append((len(reached), sum(cities[j]["population"] for j in reached)))
    return result


def with_hsr(times: list[list[int | None]], a: int, b: int) -> list[list[int | None]]:
    """Add a direct a—b link and allow one transfer through it."""
    new = [row[:] for row in times]
    new[a][b] = new[b][a] = min(HSR_SEC, times[a][b] or HSR_SEC)
    for i in range(len(new)):
        for j in range(len(new)):
            if i == j:
                continue
            for x, y in ((a, b), (b, a)):
                legs = (times[i][x] if i != x else 0, times[y][j] if y != j else 0)
                if None not in legs:
                    via = legs[0] + new[x][y] + legs[1]
                    if new[i][j] is None or via < new[i][j]:
                        new[i][j] = via
    return new


def build(cities: list[dict]) -> dict:
    rng = random.Random(SEED)
    index = {city["name"]: i for i, city in enumerate(cities)}
    times = fake_times(cities, rng)
    runs: list[dict] = []
    results: list[dict] = []

    def add(algorithm: str, parameters: dict, scenario: dict | None, metrics: dict) -> None:
        runs.append({"algorithm": algorithm, "parameters": parameters, "scenario": scenario})
        for metric, rows in metrics.items():
            results.append({"run": len(runs) - 1, "metric": metric, "rows": rows})

    origins = sorted(range(len(cities)), key=lambda i: -cities[i]["population"])
    origins = sorted(origins[:TRAVEL_TIME_ORIGINS])
    for layer in LAYERS:
        rows = [[i, j, times[layer][i][j]] for i in origins for j in range(len(cities)) if i != j]
        add("travel_time", {"layer": layer}, None, {"travel_time_sec": rows})
        for threshold in THRESHOLDS_SEC:
            reached = reach(times[layer], cities, threshold)
            add(
                "reachability",
                {"layer": layer, "threshold_sec": threshold},
                None,
                {
                    "reach_cities": [[i, None, c] for i, (c, _) in enumerate(reached)],
                    "reach_population": [[i, None, p] for i, (_, p) in enumerate(reached)],
                },
            )

    weights = [math.log(city["population"]) * rng.uniform(0.2, 1.0) for city in cities]
    weights[index[MOSCOW]] *= 3
    top = max(weights)
    betweenness = [[i, None, round(w / top, 4)] for i, w in enumerate(weights)]
    add("betweenness", {"layer": "multimodal"}, None, {"betweenness": betweenness})

    moscow, spb = index[MOSCOW], index[SAINT_PETERSBURG]
    multimodal = times["multimodal"]
    hsr = with_hsr(multimodal, moscow, spb)
    for threshold in THRESHOLDS_SEC:
        base = reach(multimodal, cities, threshold)
        parameters = {"layer": "multimodal", "threshold_sec": threshold}
        without = reach(multimodal, cities, threshold, removed=moscow)
        delta = [
            [i, None, None if i == moscow else without[i][1] - base[i][1]]
            for i in range(len(cities))
        ]
        scenario = {"type": "remove_node", "name": "Без Москвы", "city": cities[moscow]["city_id"]}
        add("scenario", parameters, scenario, {"scenario_reach_population_delta": delta})
        added = reach(hsr, cities, threshold)
        delta = [[i, None, added[i][1] - base[i][1]] for i in range(len(cities))]
        scenario = {
            "type": "add_edge",
            "name": "С ВСМ Москва — Санкт-Петербург",
            "from_city": cities[moscow]["city_id"],
            "to_city": cities[spb]["city_id"],
            "travel_time_sec": HSR_SEC,
        }
        add("scenario", parameters, scenario, {"scenario_reach_population_delta": delta})

    return {
        "warning": "НЕНАСТОЯЩИЕ ДАННЫЕ: значения выдуманы для разработки карты (#36).",
        "generator": "scripts/dev/make_sample_results.py",
        "seed": SEED,
        "format": "results[].rows = [city, other_city, value]; city — индекс в cities; "
        "value null — не определено (например, нет связи); нет строки — расчёт не делался.",
        "cities": cities,
        "runs": runs,
        "results": results,
    }


def dump(data: dict) -> str:
    """JSON with one city/run/row per line, so the generated file diffs readably."""

    def line(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    head = {key: value for key, value in data.items() if key not in ("cities", "runs", "results")}
    parts = [line(head)[:-1] + ","]
    for key in ("cities", "runs"):
        items = ",\n".join(line(item) for item in data[key])
        parts.append(f'"{key}":[\n{items}\n],')
    blocks = []
    for block in data["results"]:
        rows = ",\n".join(line(row) for row in block["rows"])
        blocks.append(
            f'{{"run":{block["run"]},"metric":{line(block["metric"])},"rows":[\n{rows}\n]}}'
        )
    parts.append('"results":[\n' + ",\n".join(blocks) + "\n]}")
    return "\n".join(parts) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cities", type=Path, default=DEFAULT_CITIES)
    args = parser.parse_args()
    text = dump(build(read_cities(args.cities)))
    json.loads(text)  # the hand-made layout must stay valid JSON
    OUTPUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"{OUTPUT}: {len(text.encode('utf-8')):,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
