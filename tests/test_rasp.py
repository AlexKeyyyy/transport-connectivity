"""Offline tests for the Yandex Rasp collector: a fake API replaces the network.

Response shapes follow the probe of 15.11.2026 (docs/yandex-schedules-plan.md),
trimmed to the fields the code reads.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import pytest

from transport_connectivity.cli import main as cli_main
from transport_connectivity.rasp_connector import (
    MANIFEST_FILENAME,
    MATCHING_FILENAME,
    DailyQuota,
    RaspClient,
    RaspError,
    RaspRequest,
    RequestBudgetExhausted,
    ResponseStore,
    StoredResponse,
    collect,
    snapshot_dir,
)
from transport_connectivity.rasp_schedule import (
    departing_on,
    next_page_offsets,
    schedule_entries,
    thread_stop_codes,
)
from transport_connectivity.rasp_stations import (
    UNMATCHED_NO_NAME,
    UNMATCHED_TOO_FAR,
    City,
    main_train_stations,
    match_cities,
    parse_settlements,
)

D = date(2026, 12, 1)

MOSCOW = City("moscow", "Москва", "Москва", 55.7558, 37.6173)
TVER = City("tver", "Тверь", "Тверская область", 56.8587, 35.9176)
ORYOL = City("oryol", "Орёл", "Орловская область", 52.9703, 36.0635)
PUSHKINO = City("pushkino", "Пушкино", "Московская область", 56.0104, 37.8471)
SEVERSK = City("seversk", "Северск", "Томская область", 56.6031, 84.8810)
MAGADAN = City("magadan", "Магадан", "Магаданская область", 59.5638, 150.8035)
CITIES = [MOSCOW, TVER, ORYOL, PUSHKINO, SEVERSK, MAGADAN]


def station(code, title, transport, kind, lat, lon):
    return {
        "title": title,
        "transport_type": transport,
        "station_type": kind,
        "latitude": lat,
        "longitude": lon,
        "codes": {"yandex_code": code},
    }


def settlement(code, title, stations):
    return {"title": title, "codes": {"yandex_code": code}, "stations": stations}


STATIONS_LIST = {
    "countries": [
        {"title": "Беларусь", "regions": []},
        {
            "title": "Россия",
            "codes": {"yandex_code": "l225"},
            "regions": [
                {
                    "title": "Москва и Московская область",
                    "settlements": [
                        settlement(
                            "c213",
                            "Москва",
                            [
                                station(
                                    "s2000006",
                                    "Москва (Ленинградский вокзал)",
                                    "train",
                                    "train_station",
                                    55.776,
                                    37.655,
                                ),
                                station(
                                    "s9601000", "Москва-Товарная", "train", "station", 55.7, 37.6
                                ),
                                station("s9602000", "Лужники", "train", "station", 55.72, 37.56),
                                station(
                                    "s9603000", "Маленковская", "train", "platform", 55.79, 37.67
                                ),
                                station(
                                    "s9600213", "Шереметьево", "plane", "airport", 55.97, 37.41
                                ),
                            ],
                        ),
                        settlement(
                            "c10748",
                            "Пушкино",
                            [
                                station(
                                    "s9601001", "Пушкино", "train", "train_station", 56.01, 37.85
                                )
                            ],
                        ),
                        # A second Пушкино two kilometres away with only a bus stop.
                        settlement(
                            "c33768",
                            "Пушкино",
                            [station("s9999999", "Дом оружия", "bus", "bus_stop", 56.02, 37.86)],
                        ),
                    ],
                },
                {
                    "title": "Тверская область",
                    "settlements": [
                        settlement(
                            "c14",
                            "Тверь",
                            [
                                station("s9603093", "Тверь", "train", "station", 56.834, 35.904),
                                station("s9603094", "Тверь агенты", "train", "station", "", ""),
                            ],
                        ),
                        # Settlements without a Yandex code are skipped.
                        {"title": "", "codes": {}, "stations": []},
                    ],
                },
                {
                    "title": "Орловская область",
                    "settlements": [
                        settlement(
                            "c10",
                            "Орел",
                            [station("s9610000", "Орёл", "train", "station", 52.96, 36.08)],
                        )
                    ],
                },
                {
                    "title": "Магаданская область",
                    "settlements": [
                        settlement(
                            "c79",
                            "Магадан",
                            [station("s9600400", "Сокол", "plane", "airport", 59.91, 150.72)],
                        )
                    ],
                },
            ],
        },
    ]
}


def schedule_item(uid, transport, departure, arrival=None):
    return {
        "thread": {"uid": uid, "transport_type": transport},
        "departure": departure,
        "arrival": arrival,
    }


def schedule_body(items, total=None, offset=0):
    return {
        "pagination": {
            "total": len(items) if total is None else total,
            "limit": 100,
            "offset": offset,
        },
        "schedule": items,
    }


def thread_body(uid, codes):
    return {"uid": uid, "stops": [{"station": {"code": code}} for code in codes]}


# --- matching and stations -------------------------------------------------------------


def test_match_uses_name_and_distance() -> None:
    matching = match_cities(CITIES, parse_settlements(STATIONS_LIST))

    matched = {m.city.city_id: m for m in matching.matches}
    assert {k: m.settlement.code for k, m in matched.items()} == {
        "moscow": "c213",
        "tver": "c14",
        "oryol": "c10",  # ё and е are the same letter for matching
        "pushkino": "c10748",
        "magadan": "c79",  # its only station is the airport, 39 km away
    }
    assert matched["pushkino"].alternatives == 1
    assert matched["magadan"].distance_km == 38.8
    assert [(u.city.city_id, u.reason) for u in matching.unmatched] == [
        ("seversk", UNMATCHED_NO_NAME)
    ]


def test_match_rejects_far_settlement() -> None:
    matching = match_cities([MAGADAN], parse_settlements(STATIONS_LIST), max_distance_km=20)

    assert matching.matches == ()
    assert [u.reason for u in matching.unmatched] == [UNMATCHED_TOO_FAR]


def test_station_without_coordinates_is_kept_but_not_used_for_distance() -> None:
    settlements = {s.code: s for s in parse_settlements(STATIONS_LIST)}

    agents = [s for s in settlements["c14"].stations if s.code == "s9603094"][0]
    assert agents.latitude is None
    assert "c33768" in settlements and "" not in settlements


def test_station_cities_cover_every_station_of_matched_settlements() -> None:
    matching = match_cities(CITIES, parse_settlements(STATIONS_LIST))

    index = matching.station_cities()
    assert index["s9600213"] == "moscow"
    assert index["s9603000"] == "moscow"  # platforms too: a train may stop anywhere
    assert "s9999999" not in index


def test_main_train_stations_by_type_or_city_name() -> None:
    matches = {
        m.city.city_id: m for m in match_cities(CITIES, parse_settlements(STATIONS_LIST)).matches
    }

    def codes(city_id):
        return [s.code for s in main_train_stations(matches[city_id])]

    # The terminal and the city-named freight station; not Лужники and not a platform.
    assert codes("moscow") == ["s2000006", "s9601000"]
    assert codes("tver") == ["s9603093", "s9603094"]
    assert codes("magadan") == []


# --- response parsing ------------------------------------------------------------------


def test_departing_on_drops_next_morning_duplicate() -> None:
    body = schedule_body(
        [
            schedule_item("072G_0_2", "train", "2026-12-01T00:10:00+03:00"),
            schedule_item(
                "010N_43_2", "train", "2026-12-01T02:34:00+05:00", "2026-12-01T02:12:00+05:00"
            ),
            schedule_item("072G_0_2", "train", "2026-12-02T00:10:00+03:00"),
        ]
    )

    entries = departing_on(schedule_entries(body), D)

    assert [e.uid for e in entries] == ["072G_0_2", "010N_43_2"]
    assert entries[1].arrival is not None and entries[1].arrival.utcoffset().seconds == 5 * 3600


def test_next_page_offsets() -> None:
    assert next_page_offsets(schedule_body([], total=303)) == [100, 200, 300]
    assert next_page_offsets(schedule_body([], total=100)) == []
    assert next_page_offsets(schedule_body([], total=0)) == []


def test_thread_stop_codes_keep_order() -> None:
    assert thread_stop_codes(thread_body("u", ["s1", "s2", "s3"])) == ["s1", "s2", "s3"]


# --- requests, store and quota ---------------------------------------------------------


def test_request_paths_are_stable_and_safe() -> None:
    request = RaspRequest.schedule("s2000006", D, event="arrival", transport="plane", offset=200)

    assert request.relative_path == Path("schedule/s2000006_plane_arrival_2026-12-01_200.json")
    assert RaspRequest.thread("SU-424_261201_c26_12").relative_path == Path(
        "thread/SU-424_261201_c26_12.json"
    )
    with pytest.raises(RaspError, match="Unexpected characters"):
        _ = RaspRequest.thread("../escape").relative_path


def test_store_round_trip_and_request_check(tmp_path: Path) -> None:
    store = ResponseStore(tmp_path)
    request = RaspRequest.thread("072G_0_2")
    store.put(request, StoredResponse(404, {"error": {"text": "не ходит"}}))

    assert store.get(request) == StoredResponse(404, {"error": {"text": "не ходит"}})
    assert store.get(RaspRequest.thread("other")) is None

    stored = json.loads((tmp_path / request.relative_path).read_text(encoding="utf-8"))
    stored["request"]["params"]["uid"] = "other"
    (tmp_path / request.relative_path).write_text(json.dumps(stored), encoding="utf-8")
    with pytest.raises(RaspError, match="another request"):
        store.get(request)


def test_daily_quota_counts_and_resets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    quota = DailyQuota(tmp_path / "quota.json", limit=2)
    monkeypatch.setattr(quota, "_today", lambda: "2026-11-16")
    quota.reserve()
    quota.reserve()
    with pytest.raises(RequestBudgetExhausted):
        quota.reserve()

    monkeypatch.setattr(quota, "_today", lambda: "2026-11-17")
    assert quota.used_today() == 0
    quota.reserve()
    assert json.loads((tmp_path / "quota.json").read_text()) == {
        "date_msk": "2026-11-17",
        "requests": 1,
    }


# --- HTTP client -----------------------------------------------------------------------


def make_client(tmp_path: Path, transport, **kwargs) -> RaspClient:
    return RaspClient(
        "test-key",
        DailyQuota(tmp_path / "quota.json"),
        transport=transport,
        backoff_seconds=0,
        pause_seconds=0,
        **kwargs,
    )


def scripted(*answers):
    calls: list[urllib.request.Request] = []

    def transport(request, timeout):
        calls.append(request)
        answer = answers[len(calls) - 1]
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return status, json.dumps(body).encode()

    return transport, calls


def test_client_retries_transient_errors_and_counts_every_attempt(tmp_path: Path) -> None:
    transport, calls = scripted(TimeoutError("slow"), (503, {}), (200, {"ok": True}))
    client = make_client(tmp_path, transport)

    assert client.get(RaspRequest.stations_list()) == StoredResponse(200, {"ok": True})
    assert len(calls) == 3 and client.requests_made == 3
    assert calls[0].get_header("Authorization") == "test-key"
    assert "apikey" not in calls[0].full_url  # the key never goes into URLs or logs
    assert client.quota.used_today() == 3


def test_client_keeps_404_and_stops_on_other_client_errors(tmp_path: Path) -> None:
    transport, _ = scripted((404, {"error": {"text": "не ходит"}}), (403, {"error": "key"}))
    client = make_client(tmp_path, transport)

    assert client.get(RaspRequest.thread("u")).status == 404
    with pytest.raises(RaspError, match="HTTP 403"):
        client.get(RaspRequest.thread("u"))


def test_client_gives_up_after_max_attempts(tmp_path: Path) -> None:
    transport, calls = scripted((500, {}), (500, {}), (500, {}))

    with pytest.raises(RaspError, match="after 3 attempts"):
        make_client(tmp_path, transport).get(RaspRequest.stations_list())
    assert len(calls) == 3


def test_client_respects_run_limit(tmp_path: Path) -> None:
    transport, calls = scripted((200, {}))
    client = make_client(tmp_path, transport, max_requests=1)
    client.get(RaspRequest.stations_list())

    with pytest.raises(RequestBudgetExhausted, match="Run limit"):
        client.get(RaspRequest.stations_list())
    assert len(calls) == 1


# --- collection ------------------------------------------------------------------------


class FakeApi:
    """Answers by method and parameters; records which requests reached the network."""

    def __init__(self) -> None:
        self.answers: dict[tuple, tuple[int, dict]] = {}
        self.calls: list[tuple] = []

    @staticmethod
    def key(method: str, params: dict[str, str]) -> tuple:
        return (method, tuple(sorted(params.items())))

    def add(self, request: RaspRequest, body: dict, status: int = 200) -> None:
        self.answers[self.key(request.method, dict(request.params))] = (status, body)

    def __call__(self, request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
        url = urllib.parse.urlsplit(request.full_url)
        method = url.path.rstrip("/").rsplit("/", 1)[-1]
        params = dict(urllib.parse.parse_qsl(url.query))
        assert (params.pop("format"), params.pop("lang")) == ("json", "ru_RU")
        key = self.key(method, params)
        self.calls.append(key)
        status, body = self.answers.get(key, (200, schedule_body([])))
        return status, json.dumps(body, ensure_ascii=False).encode("utf-8")


def schedule(code, day=D, event="departure", transport="train", offset=0):
    return RaspRequest.schedule(code, day, event=event, transport=transport, offset=offset)


@pytest.fixture
def api() -> FakeApi:
    api = FakeApi()
    api.add(RaspRequest.stations_list(), STATIONS_LIST)
    # Two pages of Sheremetyevo departures.
    api.add(
        schedule("s9600213", transport="plane"),
        schedule_body(
            [schedule_item("SU-1_261201_c26_12", "plane", "2026-12-01T09:00:00+03:00")], total=101
        ),
    )
    api.add(
        schedule("s9600213", transport="plane", offset=100),
        schedule_body(
            [schedule_item("SU-2_261201_c26_12", "plane", "2026-12-01T10:00:00+03:00")],
            total=101,
            offset=100,
        ),
    )
    # Moscow–Tver train from the Leningradsky terminal: Tver is found from its stops,
    # and a train leaving only on the next morning is not followed.
    api.add(
        schedule("s2000006"),
        schedule_body(
            [
                schedule_item("752A_0_2", "train", "2026-12-01T06:00:00+03:00"),
                schedule_item("NIGHT_0_2", "train", "2026-12-02T00:30:00+03:00"),
            ]
        ),
    )
    api.add(RaspRequest.thread("752A_0_2"), thread_body("752A_0_2", ["s2000006", "s9603093"]))
    # From Тверь: a thread that does not run on the requested day.
    api.add(
        schedule("s9603093"),
        schedule_body([schedule_item("GONE_0_2", "train", "2026-12-01T12:00:00+03:00")]),
    )
    api.add(RaspRequest.thread("GONE_0_2"), {"error": {"text": "не ходит"}}, status=404)
    # Magadan airport is listed in stations_list but unknown to schedule: no arrivals asked.
    api.add(
        schedule("s9600400", transport="plane"),
        {"error": {"text": "Не нашли станцию по yandex коду s9600400."}},
        status=404,
    )
    # Лужники is not a main station; it is queried only because a found train stops there.
    api.add(RaspRequest.thread("ORYOL_0_2"), thread_body("ORYOL_0_2", ["s9610000", "s9602000"]))
    api.add(
        schedule("s9610000"),
        schedule_body([schedule_item("ORYOL_0_2", "train", "2026-12-01T08:00:00+03:00")]),
    )
    # Second level: Лужники, found from the Орёл train, has a train of its own.
    api.add(
        schedule("s9602000"),
        schedule_body([schedule_item("LUZH_0_2", "train", "2026-12-01T09:00:00+03:00")]),
    )
    api.add(RaspRequest.thread("LUZH_0_2"), thread_body("LUZH_0_2", ["s9602000", "s9601000"]))
    return api


@pytest.fixture
def cities_csv(tmp_path: Path) -> Path:
    path = tmp_path / "cities.csv"
    rows = ["city_id,name,region,latitude,longitude"]
    rows += [f"{c.city_id},{c.name},{c.region},{c.latitude},{c.longitude}" for c in CITIES]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def run_collect(tmp_path, cities_csv, api, **client_kwargs):
    client = make_client(tmp_path, api, **client_kwargs) if api is not None else None
    return collect(D, raw_root=tmp_path / "raw", cities_path=cities_csv, client=client)


def test_collection_follows_plan_and_completes(tmp_path, cities_csv, api) -> None:
    manifest = run_collect(tmp_path, cities_csv, api)

    called = {
        (method, dict(params).get("station") or dict(params).get("uid"))
        for method, params in api.calls
    }
    assert ("schedule", "s9602000") in called  # found in stops, then queried
    assert ("schedule", "s9603000") not in called  # platforms are never queried
    assert ("thread", "NIGHT_0_2") not in called
    arrivals = [dict(p) for m, p in api.calls if m == "schedule" and ("event", "arrival") in p]
    assert sorted((p["station"], p["date"]) for p in arrivals) == [
        ("s9600213", "2026-12-01"),
        ("s9600213", "2026-12-02"),
    ]
    assert len(api.calls) == len(set(api.calls))  # nothing is requested twice
    assert manifest["complete"] is True and manifest["stopped_by"] is None
    assert manifest["matched_cities"] == 5
    assert manifest["unmatched_cities"] == [
        {"city_id": "seversk", "name": "Северск", "reason": UNMATCHED_NO_NAME}
    ]
    assert manifest["airports"] == 2
    assert manifest["train_stations_found_in_stops"] == 1
    assert manifest["train_threads"] == 4
    assert manifest["responses_by_method"]["thread"] == {"200": 3, "404": 1}
    assert manifest["responses_by_method"]["schedule"]["404"] == 1
    assert manifest["requests_this_run"] == len(api.calls) == manifest["stored_responses"]

    root = snapshot_dir(tmp_path / "raw", D)
    assert json.loads((root / MANIFEST_FILENAME).read_text(encoding="utf-8")) == manifest
    matching = (root / MATCHING_FILENAME).read_text(encoding="utf-8").splitlines()
    assert matching[-1] == "seversk,Северск,Томская область,,,,,,,," + UNMATCHED_NO_NAME


def test_interrupted_collection_continues_without_repeating(tmp_path, cities_csv, api) -> None:
    first = run_collect(tmp_path, cities_csv, api, max_requests=4)
    assert first["complete"] is False and "Run limit" in first["stopped_by"]
    assert first["stored_responses"] == 4

    second = run_collect(tmp_path, cities_csv, api)
    assert second["complete"] is True
    assert len(api.calls) == len(set(api.calls)) == second["stored_responses"]

    replay = run_collect(tmp_path, cities_csv, None)
    assert replay["complete"] is True and replay["requests_this_run"] == 0
    assert replay["responses_sha256"] == second["responses_sha256"]
    assert replay["responses_by_method"] == second["responses_by_method"]


def test_daily_quota_stops_collection_as_incomplete(tmp_path, cities_csv, api) -> None:
    client = make_client(tmp_path, api)
    client.quota.limit = 3

    manifest = collect(D, raw_root=tmp_path / "raw", cities_path=cities_csv, client=client)

    assert manifest["complete"] is False
    assert "Daily quota" in manifest["stopped_by"]
    assert len(api.calls) == manifest["stored_responses"] == 3


def test_failed_run_replaces_complete_manifest(tmp_path, cities_csv, api) -> None:
    assert run_collect(tmp_path, cities_csv, api)["complete"] is True

    # A planned response is missing again (for example, the plan grew) and the API now
    # rejects the key: the old "complete" manifest must not survive this run.
    (snapshot_dir(tmp_path / "raw", D) / RaspRequest.thread("LUZH_0_2").relative_path).unlink()

    def forbidden(request, timeout):
        return 403, b'{"error": "key"}'

    client = make_client(tmp_path, forbidden)
    with pytest.raises(RaspError, match="HTTP 403"):
        collect(D, raw_root=tmp_path / "raw", cities_path=cities_csv, client=client)

    manifest = json.loads(
        (snapshot_dir(tmp_path / "raw", D) / MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    assert manifest["complete"] is False
    assert manifest["stopped_by"].startswith("error: ")


def test_offline_replay_reports_missing_responses(tmp_path, cities_csv) -> None:
    manifest = run_collect(tmp_path, cities_csv, None)

    assert manifest["complete"] is False
    assert "stations_list.json is not stored" in manifest["stopped_by"]


def test_page_that_does_not_continue_the_schedule_is_an_error(tmp_path, cities_csv, api) -> None:
    api.add(
        schedule("s9600213", transport="plane", offset=100),
        schedule_body([], total=101, offset=0),
    )
    with pytest.raises(RaspError, match="does not continue"):
        run_collect(tmp_path, cities_csv, api)


def test_cli_offline_reports_incomplete_snapshot(tmp_path, cities_csv, capsys) -> None:
    code = cli_main(
        [
            "rasp-download",
            "--date",
            "2026-12-01",
            "--offline",
            "--raw-root",
            str(tmp_path / "raw"),
            "--cities",
            str(cities_csv),
        ]
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out)["complete"] is False


def test_cli_without_key_fails_cleanly(tmp_path, cities_csv, monkeypatch, capsys) -> None:
    monkeypatch.delenv("YANDEX_RASP_API_KEY", raising=False)
    code = cli_main(["rasp-download", "--date", "2026-12-01", "--cities", str(cities_csv)])

    assert code == 1
    assert "YANDEX_RASP_API_KEY is not set" in capsys.readouterr().err
