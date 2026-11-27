"""Tests for the map data layer (#36) on a small stub and on the committed sample."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from transport_connectivity.app import data
from transport_connectivity.app.data import ResultNotFound

CITIES = [
    {
        "city_id": f"id-{name}",
        "name": name,
        "region": "Регион",
        "latitude": 55.0,
        "longitude": 37.0 + i,
        "population": population,
    }
    for i, (name, population) in enumerate([("А", 1000), ("Б", 200), ("В", 30), ("Г", 4)])
]
REMOVE_A = {"type": "remove_node", "name": "Без А", "city": "id-А"}


@pytest.fixture
def stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = {
        "warning": "НЕНАСТОЯЩИЕ ДАННЫЕ",
        "cities": CITIES,
        "runs": [
            {"algorithm": "travel_time", "parameters": {"layer": "road"}, "scenario": None},
            {
                "algorithm": "reachability",
                "parameters": {"layer": "road", "threshold_sec": 10800},
                "scenario": None,
            },
            {
                "algorithm": "scenario",
                "parameters": {"layer": "road", "threshold_sec": 10800},
                "scenario": REMOVE_A,
            },
        ],
        "results": [
            # Only city А has travel times; Г is unreachable (null).
            {
                "run": 0,
                "metric": "travel_time_sec",
                "rows": [[0, 1, 7200], [0, 2, 3600], [0, 3, None]],
            },
            {"run": 1, "metric": "reach_population", "rows": [[0, None, 230], [1, None, 1000]]},
            {"run": 2, "metric": "scenario_reach_population_delta", "rows": [[0, None, None]]},
        ],
    }
    path = tmp_path / "sample_results.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(data, "SAMPLE_PATH", path)


@pytest.mark.usefixtures("stub")
def test_reachable_within_filters_by_threshold_and_sorts_by_time() -> None:
    reached = data.reachable_within("id-А", "road", 7200)
    assert [(item.city.name, item.travel_time_sec) for item in reached] == [
        ("В", 3600),
        ("Б", 7200),
    ]
    assert [item.city.name for item in data.reachable_within("id-А", "road", 3599)] == []


@pytest.mark.usefixtures("stub")
def test_no_connection_is_none_and_never_reachable() -> None:
    assert data.load_travel_times("id-А", "road")["id-Г"] is None
    reached = data.reachable_within("id-А", "road", 10**9)
    assert "Г" not in {item.city.name for item in reached}


@pytest.mark.usefixtures("stub")
def test_missing_computation_differs_from_no_connection() -> None:
    with pytest.raises(ResultNotFound):
        data.load_travel_times("id-Б", "road")
    with pytest.raises(ResultNotFound):
        data.load_travel_times("id-А", "air")


@pytest.mark.usefixtures("stub")
def test_load_metric_matches_parameters_and_scenario_exactly() -> None:
    parameters = {"layer": "road", "threshold_sec": 10800}
    assert data.load_metric("reach_population", parameters) == {"id-А": 230, "id-Б": 1000}
    assert data.load_metric("scenario_reach_population_delta", parameters, REMOVE_A) == {
        "id-А": None
    }
    with pytest.raises(ResultNotFound):
        data.load_metric("reach_population", {"layer": "road", "threshold_sec": 21600})
    with pytest.raises(ResultNotFound):
        data.load_metric("reach_population", parameters, REMOVE_A)


@pytest.mark.usefixtures("stub")
def test_cities_scenarios_and_warning() -> None:
    assert [city.name for city in data.load_cities()] == ["А", "Б", "В", "Г"]
    assert data.load_scenarios() == [REMOVE_A]
    assert data.source_warning("sample") == "НЕНАСТОЯЩИЕ ДАННЫЕ"


def test_unknown_source() -> None:
    with pytest.raises(ValueError):
        data.load_cities("csv")


def test_committed_sample_is_marked_fake_and_covers_the_map() -> None:
    assert "НЕНАСТОЯЩИЕ" in data.source_warning("sample")
    cities = data.load_cities()
    assert len(cities) == len({city.city_id for city in cities}) == 180
    for layer in ("road", "rail", "air", "multimodal"):
        for threshold in (3 * 3600, 6 * 3600, 24 * 3600):
            parameters = {"layer": layer, "threshold_sec": threshold}
            assert len(data.load_metric("reach_population", parameters)) == 180
    assert len(data.load_metric("betweenness", {"layer": "multimodal"})) == 180
    assert {s["type"] for s in data.load_scenarios()} == {"remove_node", "add_edge"}


def test_offline_source_preserves_boundary_and_missing_time(tmp_path, monkeypatch):
    path = tmp_path / "map.json"
    path.write_text(
        json.dumps(
            {
                "warning": "Снимок 20260629",
                "cities": [
                    {
                        "city_id": key,
                        "name": key,
                        "region": "",
                        "latitude": 0,
                        "longitude": 0,
                        "population": 1,
                    }
                    for key in ("A", "B", "C", "D")
                ],
                "runs": [{"parameters": {"layer": "best"}, "scenario": None}],
                "results": [
                    {
                        "run": 0,
                        "metric": "travel_time_sec",
                        "rows": [[0, 1, 21600], [0, 2, 21600.5], [0, 3, None]],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(data, "OFFLINE_PATH", path)
    assert data.source_warning("offline") == "Снимок 20260629"
    assert [item.city.city_id for item in data.reachable_within("A", "best", 21600, "offline")] == [
        "B"
    ]


def test_search_uses_city_labels_without_elasticsearch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ELASTICSEARCH_URL", raising=False)
    cities = data.load_cities()
    matches, message = data.find_cities("  Орел  ", cities)
    assert "Орёл" in {city.name for city in matches}
    assert message is None
