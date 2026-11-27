"""Collect direct trains and flights between project cities from Yandex Rasp.

Collection goes from stations, not from city pairs (docs/yandex-schedules-plan.md):

1. ``stations_list`` once, then cities are matched to Yandex settlements locally;
2. flights: departures from every airport on date D and arrivals on D and D+1,
   joined later by ``uid`` (a flight uid already contains its date);
3. trains: departures on D from the main stations of each city, then the stop list
   (``thread``) of every train found. Stations of our cities that appear in stop lists
   but were not queried yet are queried next, until no new station appears.

Every response is stored with its request in ``data/raw/yandex_rasp_<D>/``. What to
request next is derived from stored responses, so a run interrupted by the daily
quota continues where it stopped and never pays twice for the same answer.
"""

from __future__ import annotations

import csv
import hashlib
import http.client
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from time import sleep
from typing import Any

from transport_connectivity.connector import USER_AGENT, atomic_write_bytes
from transport_connectivity.rasp_schedule import (
    PAGE_LIMIT,
    departing_on,
    next_page_offsets,
    schedule_entries,
    thread_stop_codes,
)
from transport_connectivity.rasp_stations import (
    Matching,
    load_cities,
    main_train_stations,
    match_cities,
    parse_settlements,
    plane_stations,
    train_stations,
)

API_URL = "https://api.rasp.yandex-net.ru/v3.0"
SOURCE_ID = "yandex_rasp"
DAILY_REQUEST_LIMIT = 500
# The key's quota is counted per calendar day; Moscow time is the assumption. Russia
# has no daylight saving time, so a fixed offset is exact and needs no tz database.
QUOTA_TIMEZONE = timezone(timedelta(hours=3), "MSK")
DEFAULT_RAW_ROOT = Path("data/raw")
DEFAULT_QUOTA_PATH = DEFAULT_RAW_ROOT / "yandex_rasp_quota.json"
DEFAULT_CITIES_PATH = Path("data/normalized/tochno_intercity_v20260629/cities.csv")
MANIFEST_FILENAME = "manifest.json"
MATCHING_FILENAME = "city_matching.csv"

# 404 is an answer of the source ("рейс не ходит в эту дату") and is stored like data.
# Other client errors mean a wrong key or request and stop the run.
STORED_ERROR_STATUSES = frozenset({404})
RETRYABLE_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_SAFE_NAME = re.compile(r"^[0-9A-Za-z_.-]+$")


class RaspError(RuntimeError):
    """The API, the stored responses or the collection settings are not usable."""


class RequestBudgetExhausted(RaspError):
    """The daily quota or the per-run limit is spent; collection continues next run."""


def snapshot_dir(raw_root: Path, day: date) -> Path:
    return raw_root / f"yandex_rasp_{day:%Y%m%d}"


@dataclass(frozen=True, slots=True)
class RaspRequest:
    method: str
    params: tuple[tuple[str, str], ...]

    @classmethod
    def stations_list(cls) -> RaspRequest:
        return cls("stations_list", ())

    @classmethod
    def schedule(
        cls, station: str, day: date, *, event: str, transport: str, offset: int = 0
    ) -> RaspRequest:
        params = {
            "station": station,
            "date": day.isoformat(),
            "event": event,
            "transport_types": transport,
            "limit": str(PAGE_LIMIT),
            "offset": str(offset),
        }
        return cls("schedule", tuple(params.items()))

    @classmethod
    def thread(cls, uid: str) -> RaspRequest:
        # Without a date the API returns the nearest run of the thread. A train uid does
        # not carry a date, and a date that is not the start date at the origin station
        # gives 404, so the run is left to the API: times between stops do not change.
        return cls("thread", (("uid", uid),))

    def param(self, name: str) -> str:
        return dict(self.params)[name]

    @property
    def relative_path(self) -> Path:
        """Stable file name of the stored response, unique per request."""
        if self.method == "stations_list":
            return Path("stations_list.json")
        if self.method == "schedule":
            p = dict(self.params)
            name = f"{p['station']}_{p['transport_types']}_{p['event']}_{p['date']}_{p['offset']}"
        elif self.method == "thread":
            name = self.param("uid")
        else:
            raise RaspError(f"Unknown method {self.method}")
        if not _SAFE_NAME.match(name):
            raise RaspError(f"Unexpected characters in request key {name!r}")
        return Path(self.method) / f"{name}.json"


@dataclass(frozen=True, slots=True)
class StoredResponse:
    status: int
    body: dict[str, Any]

    @property
    def ok(self) -> bool:
        return self.status == 200


class ResponseStore:
    """Raw responses on disk, one JSON file per request with the request inside."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def get(self, request: RaspRequest) -> StoredResponse | None:
        path = self.root / request.relative_path
        if not path.is_file():
            return None
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            response = StoredResponse(stored["status"], stored["body"])
        except (OSError, ValueError, KeyError) as error:
            raise RaspError(f"Stored response {path.as_posix()} is unreadable: {error}") from error
        if stored.get("request") != _request_dict(request):
            raise RaspError(f"Stored response {path.as_posix()} belongs to another request")
        return response

    def put(self, request: RaspRequest, response: StoredResponse) -> None:
        stored = {
            "request": _request_dict(request),
            "status": response.status,
            "fetched_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "body": response.body,
        }
        data = json.dumps(stored, ensure_ascii=False).encode("utf-8")
        atomic_write_bytes(self.root / request.relative_path, data)

    def files(self) -> Iterator[Path]:
        for method in ("stations_list.json", "schedule", "thread"):
            path = self.root / method
            if path.is_file():
                yield path
            elif path.is_dir():
                yield from sorted(path.glob("*.json"))


def _request_dict(request: RaspRequest) -> dict[str, Any]:
    return {"method": request.method, "params": dict(request.params)}


class DailyQuota:
    """Persistent count of API calls per quota day, shared by all runs on this machine."""

    def __init__(self, path: Path, limit: int = DAILY_REQUEST_LIMIT) -> None:
        self.path = path
        self.limit = limit

    def _today(self) -> str:
        return datetime.now(QUOTA_TIMEZONE).date().isoformat()

    def used_today(self) -> int:
        if not self.path.is_file():
            return 0
        usage = json.loads(self.path.read_text(encoding="utf-8"))
        return int(usage["requests"]) if usage.get("date_msk") == self._today() else 0

    def reserve(self) -> None:
        """Count one call before it is made; a failed call still costs quota."""
        used = self.used_today()
        if used >= self.limit:
            raise RequestBudgetExhausted(f"Daily quota of {self.limit} requests is spent")
        usage = {"date_msk": self._today(), "requests": used + 1}
        atomic_write_bytes(self.path, (json.dumps(usage, indent=2) + "\n").encode("utf-8"))


Transport = Callable[[urllib.request.Request, float], tuple[int, bytes]]


def urllib_transport(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    """Return status and body for any HTTP answer; raise only for network failures."""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


class RaspClient:
    """HTTP calls to the API with retries; every attempt is counted against the quota."""

    def __init__(
        self,
        api_key: str,
        quota: DailyQuota,
        *,
        max_requests: int | None = None,
        transport: Transport = urllib_transport,
        timeout: float = 120,
        max_attempts: int = 3,
        backoff_seconds: float = 2.0,
        pause_seconds: float = 0.2,
    ) -> None:
        if not api_key:
            raise RaspError("YANDEX_RASP_API_KEY is not set")
        self._api_key = api_key
        self.quota = quota
        self.max_requests = max_requests
        self.requests_made = 0
        self._transport = transport
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._pause_seconds = pause_seconds

    def _reserve(self) -> None:
        if self.max_requests is not None and self.requests_made >= self.max_requests:
            raise RequestBudgetExhausted(f"Run limit of {self.max_requests} requests is spent")
        self.quota.reserve()
        self.requests_made += 1

    def get(self, request: RaspRequest) -> StoredResponse:
        query = urllib.parse.urlencode({**dict(request.params), "format": "json", "lang": "ru_RU"})
        http_request = urllib.request.Request(
            f"{API_URL}/{request.method}/?{query}",
            headers={
                "Authorization": self._api_key,
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
        )
        attempt = 1
        while True:
            self._reserve()
            sleep(self._pause_seconds)
            try:
                status, payload = self._transport(http_request, self._timeout)
            except (OSError, http.client.HTTPException) as error:
                status, payload, failure = None, b"", f"{type(error).__name__}: {error}"
            else:
                failure = f"HTTP {status}"
            if status == 200 or status in STORED_ERROR_STATUSES:
                return StoredResponse(status, _decode_json(payload, request))
            if status is not None and status not in RETRYABLE_HTTP_STATUSES:
                raise RaspError(f"{request.method} failed with {failure}: {_snippet(payload)}")
            if attempt >= self._max_attempts:
                raise RaspError(f"{request.method} failed after {attempt} attempts: {failure}")
            sleep(self._backoff_seconds * attempt)
            attempt += 1


def _decode_json(payload: bytes, request: RaspRequest) -> dict[str, Any]:
    try:
        body = json.loads(payload)
    except ValueError as error:
        raise RaspError(f"{request.method} returned invalid JSON: {error}") from error
    if not isinstance(body, dict):
        raise RaspError(f"{request.method} returned {type(body).__name__}, expected an object")
    return body


def _snippet(payload: bytes) -> str:
    return payload[:300].decode("utf-8", "replace")


@dataclass
class CollectionProgress:
    """What the stored responses cover; written to the manifest after every run."""

    matched_cities: int = 0
    unmatched_cities: list[dict[str, str]] = field(default_factory=list)
    airports: int = 0
    train_stations_main: int = 0
    train_stations_found_in_stops: int = 0
    train_threads: int = 0
    responses: dict[str, Counter[int]] = field(default_factory=dict)


class Collector:
    """Walks the collection plan, taking each response from the store or the API."""

    def __init__(self, store: ResponseStore, client: RaspClient | None, day: date) -> None:
        self.store = store
        self.client = client
        self.day = day
        self.progress = CollectionProgress()

    def response(self, request: RaspRequest) -> StoredResponse:
        stored = self.store.get(request)
        if stored is None:
            if self.client is None:
                missing = request.relative_path.as_posix()
                raise RequestBudgetExhausted(f"Offline: {missing} is not stored")
            stored = self.client.get(request)
            self.store.put(request, stored)
        self.progress.responses.setdefault(request.method, Counter())[stored.status] += 1
        return stored

    def schedule_pages(
        self, station: str, day: date, *, event: str, transport: str
    ) -> list[dict[str, Any]] | None:
        """Bodies of all pages of one station schedule.

        None when the API does not know the station: stations_list still lists some
        closed airports (Воронеж), and their schedule answers 404.
        """
        first = self.response(RaspRequest.schedule(station, day, event=event, transport=transport))
        if not first.ok:
            return None
        bodies = [first.body]
        for offset in next_page_offsets(first.body):
            request = RaspRequest.schedule(
                station, day, event=event, transport=transport, offset=offset
            )
            page = self.response(request)
            if not page.ok or int(page.body["pagination"]["offset"]) != offset:
                page_name = request.relative_path.as_posix()
                raise RaspError(f"Page {page_name} does not continue the schedule")
            bodies.append(page.body)
        return bodies

    def collect_flights(self, matching: Matching) -> None:
        airports = sorted({s.code for m in matching.matches for s in plane_stations(m)})
        self.progress.airports = len(airports)
        for code in airports:
            if self.schedule_pages(code, self.day, event="departure", transport="plane") is None:
                continue
            # A flight that leaves on D lands on D or, eastbound overnight, on D+1.
            for day in (self.day, self.day + timedelta(days=1)):
                self.schedule_pages(code, day, event="arrival", transport="plane")

    def collect_trains(self, matching: Matching) -> None:
        candidates = {s.code for m in matching.matches for s in train_stations(m)}
        main = {s.code for m in matching.matches for s in main_train_stations(m)}
        self.progress.train_stations_main = len(main)
        queried: set[str] = set()
        uids: set[str] = set()
        threads_done: set[str] = set()
        pending = main
        while pending:
            for code in sorted(pending):
                for body in (
                    self.schedule_pages(code, self.day, event="departure", transport="train") or []
                ):
                    uids.update(e.uid for e in departing_on(schedule_entries(body), self.day))
                queried.add(code)
            stops: set[str] = set()
            for uid in sorted(uids - threads_done):
                thread = self.response(RaspRequest.thread(uid))
                if thread.ok:
                    stops.update(thread_stop_codes(thread.body))
                threads_done.add(uid)
            pending = (stops & candidates) - queried
        self.progress.train_stations_found_in_stops = len(queried - main)
        self.progress.train_threads = len(threads_done)

    def run(self, cities_path: Path) -> Matching:
        stations_list = self.response(RaspRequest.stations_list())
        if not stations_list.ok:
            raise RaspError("stations_list did not return data")
        matching = match_cities(load_cities(cities_path), parse_settlements(stations_list.body))
        self.progress.matched_cities = len(matching.matches)
        self.progress.unmatched_cities = [
            {"city_id": u.city.city_id, "name": u.city.name, "reason": u.reason}
            for u in matching.unmatched
        ]
        write_matching(self.store.root / MATCHING_FILENAME, matching)
        self.collect_flights(matching)
        self.collect_trains(matching)
        return matching


def write_matching(path: Path, matching: Matching) -> None:
    """City -> Yandex settlement table for manual review, unmatched cities included."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "city_id",
            "name",
            "region",
            "yandex_settlement",
            "yandex_title",
            "yandex_region",
            "distance_km",
            "alternatives",
            "airports",
            "train_stations",
            "unmatched_reason",
        ]
    )
    for m in sorted(matching.matches, key=lambda m: m.city.city_id):
        writer.writerow(
            [
                m.city.city_id,
                m.city.name,
                m.city.region,
                m.settlement.code,
                m.settlement.title,
                m.settlement.region,
                f"{m.distance_km:.1f}",
                m.alternatives,
                len(plane_stations(m)),
                len(train_stations(m)),
                "",
            ]
        )
    for u in sorted(matching.unmatched, key=lambda u: u.city.city_id):
        writer.writerow([u.city.city_id, u.city.name, u.city.region, *[""] * 7, u.reason])
    atomic_write_bytes(path, buffer.getvalue().encode("utf-8"))


def responses_digest(store: ResponseStore) -> tuple[int, str]:
    """Number of stored responses and SHA-256 over their relative paths and contents."""
    digest = hashlib.sha256()
    count = 0
    for path in store.files():
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        digest.update(f"{path.relative_to(store.root).as_posix()} {file_hash}\n".encode())
        count += 1
    return count, digest.hexdigest()


def collect(
    day: date,
    *,
    raw_root: Path = DEFAULT_RAW_ROOT,
    cities_path: Path = DEFAULT_CITIES_PATH,
    client: RaspClient | None,
) -> dict[str, Any]:
    """Run or continue the collection for date D and write its manifest.

    The manifest says ``complete: false`` until every planned request has a stored
    answer; normalization refuses an incomplete snapshot. ``client=None`` only replays
    stored responses, which is how the plan of a finished collection is checked.
    A failed run also rewrites the manifest before the error is raised, so a manifest
    of an earlier complete run never describes changed inputs.
    """
    store = ResponseStore(snapshot_dir(raw_root, day))
    collector = Collector(store, client, day)
    stopped_by: str | None = None
    failure: RaspError | None = None
    try:
        collector.run(cities_path)
    except RequestBudgetExhausted as error:
        stopped_by = str(error)
    except RaspError as error:
        stopped_by = f"error: {error}"
        failure = error

    response_count, digest = responses_digest(store)
    progress = collector.progress
    manifest = {
        "source_id": SOURCE_ID,
        "api_url": API_URL,
        "service_date": day.isoformat(),
        "complete": stopped_by is None,
        "stopped_by": stopped_by,
        "updated_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "cities_file": cities_path.as_posix(),
        "cities_sha256": hashlib.sha256(cities_path.read_bytes()).hexdigest(),
        "matched_cities": progress.matched_cities,
        "unmatched_cities": progress.unmatched_cities,
        "airports": progress.airports,
        "train_stations_main": progress.train_stations_main,
        "train_stations_found_in_stops": progress.train_stations_found_in_stops,
        "train_threads": progress.train_threads,
        "responses_by_method": {
            method: {str(status): n for status, n in sorted(counts.items())}
            for method, counts in sorted(progress.responses.items())
        },
        "requests_this_run": client.requests_made if client else 0,
        "stored_responses": response_count,
        "responses_sha256": digest,
    }
    text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    atomic_write_bytes(store.root / MANIFEST_FILENAME, text.encode("utf-8"))
    if failure is not None:
        raise failure
    return manifest
