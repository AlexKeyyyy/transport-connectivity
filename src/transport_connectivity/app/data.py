"""Data layer of the map (#36): the only place that knows where results come from.

The map reads cities and ``metric_result`` rows (reference in docs/data-model.md, #32)
and computes nothing itself. Sources: ``offline`` — precomputed availability;
``sample`` — the fake stub ``sample_results.json``;
``db`` — PostgreSQL, available after the analytic tables (#32) are merged.

Two kinds of "nothing" are kept apart: ``ResultNotFound`` means the run or the rows were
never computed, while ``None`` as a value means the metric is undefined for that city,
for example no connection between two cities.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

SOURCES = ("offline", "sample", "db")
OFFLINE_PATH = Path("data/normalized/map_accessibility.json")
SAMPLE_PATH = Path(__file__).with_name("sample_results.json")


class ResultNotFound(LookupError):
    """The requested run or rows do not exist in the source."""


@dataclass(frozen=True)
class City:
    city_id: str
    name: str
    region: str
    latitude: float
    longitude: float
    population: int


@dataclass(frozen=True)
class Reachable:
    city: City
    travel_time_sec: float


@dataclass(frozen=True)
class _Sample:
    warning: str
    cities: tuple[City, ...]
    runs: tuple[dict[str, Any], ...]
    # (run index, metric) -> {(city_id, other_city_id | None): value}
    results: dict[tuple[int, str], dict[tuple[str, str | None], float | None]]


@lru_cache
def _load_sample(path: Path) -> _Sample:
    raw = json.loads(path.read_text(encoding="utf-8"))
    cities = tuple(City(**city) for city in raw["cities"])
    results: dict[tuple[int, str], dict[tuple[str, str | None], float | None]] = {}
    for block in raw["results"]:
        values = results.setdefault((block["run"], block["metric"]), {})
        for city, other, value in block["rows"]:
            other_id = None if other is None else cities[other].city_id
            values[(cities[city].city_id, other_id)] = value
    return _Sample(raw["warning"], cities, tuple(raw["runs"]), results)


def _sample(source: str) -> _Sample:
    if source == "sample":
        return _load_sample(SAMPLE_PATH)
    if source == "offline":
        return _load_sample(OFFLINE_PATH)
    if source == "db":
        raise NotImplementedError("Источник db появится после аналитических таблиц (#32).")
    raise ValueError(f"Неизвестный источник {source!r}; допустимо: {', '.join(SOURCES)}")


def _metric_values(
    data: _Sample, metric: str, parameters: Mapping[str, Any], scenario: Mapping[str, Any] | None
) -> dict[tuple[str, str | None], float | None]:
    for index, run in enumerate(data.runs):
        if run["parameters"] == dict(parameters) and run["scenario"] == scenario:
            values = data.results.get((index, metric))
            if values is not None:
                return values
    raise ResultNotFound(f"Нет расчёта {metric} с параметрами {dict(parameters)}")


def source_warning(source: str) -> str | None:
    """Text to show on the map when the source is not real data."""
    return _sample(source).warning if source != "db" else None


def load_cities(source: str = "sample") -> list[City]:
    return sorted(_sample(source).cities, key=lambda city: city.name)


def load_scenarios(source: str = "sample") -> list[dict[str, Any]]:
    """Distinct scenarios that have results, in run order."""
    scenarios: list[dict[str, Any]] = []
    for run in _sample(source).runs:
        if run["scenario"] is not None and run["scenario"] not in scenarios:
            scenarios.append(run["scenario"])
    return scenarios


def load_metric(
    metric: str,
    parameters: Mapping[str, Any],
    scenario: Mapping[str, Any] | None = None,
    source: str = "sample",
) -> dict[str, float | None]:
    """Per-city metric (no second city) of the run with exactly these parameters."""
    values = _metric_values(_sample(source), metric, parameters, scenario)
    return {city: value for (city, other), value in values.items() if other is None}


def load_travel_times(city_id: str, layer: str, source: str = "sample") -> dict[str, float | None]:
    """Travel time from the city to every other city; None — no connection."""
    values = _metric_values(_sample(source), "travel_time_sec", {"layer": layer}, None)
    times = {
        other: value
        for (city, other), value in values.items()
        if city == city_id and other is not None
    }
    if not times:
        raise ResultNotFound(f"Нет расчёта времени в пути из города {city_id}")
    return times


def reachable_within(
    city_id: str, layer: str, threshold_sec: int, source: str = "sample"
) -> list[Reachable]:
    """Cities reachable within the threshold, nearest first."""
    cities = {city.city_id: city for city in load_cities(source)}
    times = load_travel_times(city_id, layer, source)
    reached = [
        Reachable(cities[other], seconds)
        for other, seconds in times.items()
        if seconds is not None and seconds <= threshold_sec
    ]
    return sorted(reached, key=lambda item: (item.travel_time_sec, item.city.name))
