"""Offline coverage for flight joins, train stop offsets, and the snapshot entry point."""

from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from transport_connectivity.cli import main
from transport_connectivity.rasp_connector import (
    RaspRequest,
    ResponseStore,
    StoredResponse,
    collect,
    snapshot_dir,
)
from transport_connectivity.rasp_processor import Event, Link, Stop, normalize

DAY = date(2026, 12, 1)
CITIES = {"s1": "one", "s2": "two", "s3": "three", "s10": "one"}


def event(uid: str, station: str, timestamp: str, response: str = "") -> Event:
    return Event(uid, station, datetime.fromisoformat(timestamp), response)


def test_air_joins_later_stops_in_absolute_time_and_keeps_provenance() -> None:
    first = event("flight", "s1", "2026-12-01T23:00:00+03:00", "dep-0")
    repeat = event("flight", "s1", "2026-12-01T23:00:00+03:00", "dep-100")
    onward = event("flight", "s2", "2026-12-02T01:30:00+04:00")
    no_arrival = event("unmatched", "s1", "2026-12-01T10:00:00+03:00")
    already_landed = event("early", "s1", "2026-12-01T09:00:00+03:00")
    result = normalize(
        DAY,
        CITIES,
        [repeat, onward, no_arrival, already_landed, first],
        [
            event("flight", "s2", "2026-12-02T01:00:00+04:00", "arr-100"),
            event("flight", "s2", "2026-12-02T01:00:00+04:00", "arr-0"),
            event("flight", "s3", "2026-12-02T03:30:00+05:00", "arr-3"),
            event("early", "s2", "2026-12-01T10:00:00+05:00"),
        ],
        [],
        {},
    )
    assert result.links == [
        Link("one", "three", "air", 1, 9000),
        Link("one", "two", "air", 1, 3600),
        Link("two", "three", "air", 1, 3600),
    ]
    assert result.departures["air"] == {
        "rows": 5,
        "unique": 4,
        "exact_duplicate": 1,
        "with_destination": 2,
        "no_cached_arrival": 1,
        "no_later_arrival_at_another_station": 1,
    }
    to_two = next(row for row in result.services if row.to_station == "s2")
    assert (to_two.departure_response, to_two.arrival_response) == ("dep-0", "arr-0")


def test_rail_matches_the_boarding_clock_and_uses_first_downstream_arrival() -> None:
    departure = event("train", "s1", "2026-12-01T00:25:00+07:00")
    stops = (
        Stop("s1", datetime.fromisoformat("2026-11-22 22:01:00"), False, 0, 0),
        Stop("s2", None, True, 3600, 60),
        Stop("s1", datetime.fromisoformat("2026-11-23 00:25:00"), True, 8640, 300),
        Stop("s10", None, True, 9010, 60),
        Stop("external", None, True, 9700, 0),
        Stop("s2", None, True, 10500, 120),
        Stop("s2", None, True, 12000, 0),
        Stop("s3", None, True, 13500, 0),
    )
    missing = event("404", "s2", "2026-12-01T01:00:00+07:00")
    result = normalize(DAY, CITIES, [], [], [departure, missing], {"train": stops, "404": None})
    assert result.links == [
        Link("one", "three", "rail", 1, 4860),
        Link("one", "two", "rail", 1, 1740),
    ]
    assert {row.departure_at for row in result.services} == {"2026-12-01T00:25:00+07:00"}
    assert result.departures["rail"]["thread_not_found"] == 1
    assert result.pairs["rail"] == {
        "candidates": 5,
        "same_city": 1,
        "outside_project_cities": 1,
        "later_arrival_to_same_station": 1,
        "exact_duplicate": 0,
        "services": 2,
    }


def test_distinct_boardings_with_the_same_service_key_are_reported() -> None:
    result = normalize(
        DAY,
        CITIES,
        [
            event("flight", "s1", "2026-12-01T09:00:00+03:00"),
            event("flight", "s1", "2026-12-01T10:00:00+03:00"),
        ],
        [event("flight", "s2", "2026-12-01T12:00:00+03:00")],
        [],
        {},
    )
    assert result.services == []
    assert result.links == []
    assert result.pairs["air"]["key_conflicts"] == 2


@pytest.fixture
def snapshot(tmp_path: Path) -> tuple[Path, Path, Path]:
    raw_root = tmp_path / "raw"
    cities_path = tmp_path / "cities.csv"
    output_root = tmp_path / "normalized"
    cities_path.write_text(
        "city_id,name,region,latitude,longitude\none,Первый,Регион,55,37\n"
        "two,Второй,Регион,56,38\n",
        encoding="utf-8",
    )
    settlements = []
    for code, title, lat, lon in (("s1", "Первый", 55, 37), ("s2", "Второй", 56, 38)):
        settlements.append(
            {
                "title": title,
                "codes": {"yandex_code": f"c{code[1:]}"},
                "stations": [
                    {
                        "title": title,
                        "transport_type": "train",
                        "station_type": "train_station",
                        "latitude": lat,
                        "longitude": lon,
                        "codes": {"yandex_code": code},
                    }
                ],
            }
        )
    store = ResponseStore(snapshot_dir(raw_root, DAY))
    store.put(
        RaspRequest.stations_list(),
        StoredResponse(
            200,
            {
                "countries": [
                    {
                        "title": "Россия",
                        "regions": [{"title": "Регион", "settlements": settlements}],
                    }
                ]
            },
        ),
    )
    store.put(
        RaspRequest.schedule("s1", DAY, event="departure", transport="train"),
        StoredResponse(
            200,
            {
                "schedule": [
                    {"thread": {"uid": "train", "transport_type": "train"}, "departure": departure}
                    for departure in ("2026-12-01T12:00:00+03:00", "2026-12-02T12:00:00+03:00")
                ]
            },
        ),
    )
    store.put(
        RaspRequest.schedule("s2", DAY, event="departure", transport="train"),
        StoredResponse(200, {"schedule": []}),
    )
    store.put(
        RaspRequest.thread("train"),
        StoredResponse(
            200,
            {
                "uid": "train",
                "stops": [
                    {
                        "station": {"code": "s1"},
                        "departure": "2026-11-22 12:00:00",
                        "arrival": None,
                        "duration": 0.0,
                        "stop_time": None,
                    },
                    {
                        "station": {"code": "s2"},
                        "departure": None,
                        "arrival": "2026-11-22 13:00:00",
                        "duration": 3600.0,
                        "stop_time": None,
                    },
                ],
            },
        ),
    )
    assert collect(DAY, raw_root=raw_root, cities_path=cities_path, client=None)["complete"]
    return raw_root, cities_path, output_root


def args(snapshot: tuple[Path, Path, Path]) -> list[str]:
    raw, cities, output = snapshot
    return [
        "rasp-process",
        "--date",
        str(DAY),
        "--raw-root",
        str(raw),
        "--cities",
        str(cities),
        "--output-root",
        str(output),
    ]


def test_cli_validates_and_replaces_a_complete_snapshot(snapshot: tuple[Path, Path, Path]) -> None:
    raw, _, output = snapshot
    assert main(args(snapshot)) == 0
    target = snapshot_dir(output, DAY)
    first = {file.name: file.read_bytes() for file in target.glob("*.csv")}
    assert set(first) == {"direct_links.csv", "stations.csv", "services.csv"}
    (row,) = csv.DictReader(io.StringIO(first["services.csv"].decode()))
    assert (row["departure_date"], row["duration_sec"]) == ("2026-12-01", "3600")
    assert (snapshot_dir(raw, DAY) / row["departure_response"]).is_file()
    assert (snapshot_dir(raw, DAY) / row["arrival_response"]).is_file()
    report = json.loads((target / "run-report.json").read_bytes())
    assert report["balance"]["schedule_rows_excluded"] == {"train_departure_outside_date_window": 1}
    assert report["balance"]["services_in_direct_links"] == 1
    assert main(args(snapshot)) == 0
    assert {file.name: file.read_bytes() for file in target.glob("*.csv")} == first

    with (snapshot_dir(raw, DAY) / "thread" / "train.json").open("ab") as file:
        file.write(b"\n")
    assert main(args(snapshot)) == 1
    assert {file.name: file.read_bytes() for file in target.glob("*.csv")} == first


def test_incomplete_snapshot_publishes_nothing(snapshot: tuple[Path, Path, Path]) -> None:
    raw, _, output = snapshot
    manifest_path = snapshot_dir(raw, DAY) / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["complete"] = False
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert main(args(snapshot)) == 1
    assert not output.exists()


def test_wrong_city_hash_publishes_nothing(snapshot: tuple[Path, Path, Path]) -> None:
    _, cities, output = snapshot
    with cities.open("ab") as file:
        file.write(b"\n")
    assert main(args(snapshot)) == 1
    assert not output.exists()
