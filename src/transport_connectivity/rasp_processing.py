"""Verify a Yandex Rasp snapshot and publish the normalized CSVs together."""

from __future__ import annotations

import hashlib
import json
import time
import tracemalloc
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

from transport_connectivity.connector import sha256_file
from transport_connectivity.processor import (
    DEFAULT_OUTPUT_ROOT,
    ProcessingError,
    publish_directory,
    to_csv_bytes,
)
from transport_connectivity.rasp_connector import (
    DEFAULT_CITIES_PATH,
    DEFAULT_RAW_ROOT,
    MANIFEST_FILENAME,
    SOURCE_ID,
    RaspRequest,
    ResponseStore,
    responses_digest,
    snapshot_dir,
)
from transport_connectivity.rasp_processor import Event, Link, Service, Stop, normalize
from transport_connectivity.rasp_schedule import schedule_entries
from transport_connectivity.rasp_stations import load_cities, match_cities, parse_settlements

STATION_COLUMNS = (
    "station_code",
    "settlement_code",
    "city_id",
    "transport_type",
    "station_type",
    "title",
    "snapshot_id",
)
TRANSPORT_TYPES = {"plane": "air", "train": "rail"}


def _manifest(store: ResponseStore, cities_path: Path, day: date) -> dict[str, Any]:
    manifest = json.loads((store.root / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    if manifest["source_id"] != SOURCE_ID or manifest["service_date"] != day.isoformat():
        raise ProcessingError("Yandex Rasp manifest does not describe the requested snapshot")
    if manifest["complete"] is not True or manifest["stopped_by"] is not None:
        raise ProcessingError("Yandex Rasp snapshot is incomplete")
    if sha256_file(cities_path) != manifest["cities_sha256"]:
        raise ProcessingError("cities.csv SHA-256 does not match Yandex Rasp manifest")
    count, digest = responses_digest(store)
    if count != manifest["stored_responses"] or digest != manifest["responses_sha256"]:
        raise ProcessingError(
            "Cached response count or SHA-256 does not match Yandex Rasp manifest"
        )
    return manifest


def _offset(value: int | float) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"Invalid stop offset: {value!r}")
    seconds = int(value)
    if seconds != value or seconds < 0:
        raise ValueError(f"Invalid stop offset: {value!r}")
    return seconds


def _stop(row: dict[str, Any]) -> Stop:
    departure = row["departure"]
    dwell = row["stop_time"]
    return Stop(
        row["station"]["code"],
        datetime.fromisoformat(departure) if departure else None,
        bool(row["arrival"]),
        _offset(row["duration"]),
        0 if dwell is None else _offset(dwell),
    )


class CachedSnapshot(NamedTuple):
    stations: dict[str, Any]
    events: dict[tuple[str, str], list[Event]]
    threads: dict[str, tuple[Stop, ...] | None]
    responses: dict[str, dict[str, int]]
    excluded: Counter[str]


def _read_snapshot(store: ResponseStore, day: date) -> CachedSnapshot:
    stations: dict[str, Any] = {}
    events: dict[tuple[str, str], list[Event]] = {
        ("plane", "departure"): [],
        ("plane", "arrival"): [],
        ("train", "departure"): [],
    }
    threads: dict[str, tuple[Stop, ...] | None] = {}
    responses: dict[str, dict[str, int]] = {}
    excluded: Counter[str] = Counter()
    last_arrival_day = day + timedelta(days=1)
    for path in store.files():
        relative = path.relative_to(store.root)
        try:
            cached = json.loads(path.read_bytes())
            request = cached["request"]
            method, params = request["method"], request["params"]
            expected = RaspRequest(method, tuple(params.items())).relative_path
            if expected != relative:
                raise ValueError("request does not match the cache path")
            status, body = cached["status"], cached["body"]
            if status not in (200, 404):
                raise ValueError(f"unexpected HTTP status {status}")
            by_status = responses.setdefault(method, {})
            by_status[str(status)] = by_status.get(str(status), 0) + 1
            if method == "stations_list":
                if status != 200:
                    raise ValueError("stations_list has no data")
                stations = body
            elif method == "thread":
                uid = params["uid"]
                if status == 200 and body["uid"] != uid:
                    raise ValueError("response UID differs from the requested UID")
                threads[uid] = tuple(map(_stop, body["stops"])) if status == 200 else None
            elif status == 200:
                transport, event = params["transport_types"], params["event"]
                lower, upper = day, last_arrival_day if event == "arrival" else day
                target = events[transport, event]
                for entry in schedule_entries(body):
                    timestamp = entry.arrival if event == "arrival" else entry.departure
                    if timestamp is None:
                        excluded[f"{transport}_{event}_missing_time"] += 1
                    elif timestamp.utcoffset() is None:
                        raise ValueError("schedule timestamp has no UTC offset")
                    elif not lower <= timestamp.date() <= upper:
                        excluded[f"{transport}_{event}_outside_date_window"] += 1
                    else:
                        target.append(
                            Event(entry.uid, params["station"], timestamp, relative.as_posix())
                        )
        except (KeyError, ValueError, TypeError, OverflowError) as error:
            raise ProcessingError(
                f"Invalid cached response {relative.as_posix()}: {error}"
            ) from error
    return CachedSnapshot(stations, events, threads, responses, excluded)


def _run(
    day: date, store: ResponseStore, cities_path: Path, code_version: str
) -> tuple[dict[str, Any], dict[str, bytes]]:
    manifest = _manifest(store, cities_path, day)
    stations, events, threads, responses, excluded = _read_snapshot(store, day)
    if responses != manifest["responses_by_method"]:
        raise ProcessingError("Cached response statuses do not match the manifest")
    cities = load_cities(cities_path)
    matching = match_cities(cities, parse_settlements(stations))
    unmatched = [
        {"city_id": match.city.city_id, "name": match.city.name, "reason": match.reason}
        for match in matching.unmatched
    ]
    if (
        len(matching.matches) != manifest["matched_cities"]
        or unmatched != manifest["unmatched_cities"]
    ):
        raise ProcessingError("City matching does not match the manifest")
    required_threads = {event.uid for event in events["train", "departure"]}
    if threads.keys() != required_threads:
        raise ProcessingError("Cached threads do not match the observed train departures")

    snapshot_id = store.root.name
    station_rows = {
        (
            station.code,
            match.settlement.code,
            match.city.city_id,
            TRANSPORT_TYPES[station.transport_type],
            station.station_type,
            station.title,
            snapshot_id,
        )
        for match in matching.matches
        for station in match.settlement.stations
        if station.transport_type in TRANSPORT_TYPES
    }
    station_cities = {row[0]: row[2] for row in station_rows}
    if len(station_rows) != len(station_cities):
        raise ProcessingError("Yandex station code has more than one project mapping")
    departures, arrivals = events["plane", "departure"], events["plane", "arrival"]
    result = normalize(
        day, station_cities, departures, arrivals, events["train", "departure"], threads
    )
    if responses_digest(store) != (manifest["stored_responses"], manifest["responses_sha256"]):
        raise ProcessingError("Yandex Rasp cache changed during processing")
    if sha256_file(cities_path) != manifest["cities_sha256"]:
        raise ProcessingError("cities.csv changed during processing")
    service_rows = (
        (
            *row._replace(
                arrival_response=(
                    row.arrival_response or RaspRequest.thread(row.uid).relative_path.as_posix()
                )
            ),
            snapshot_id,
        )
        for row in result.services
    )
    outputs = {
        "stations.csv": to_csv_bytes(STATION_COLUMNS, sorted(station_rows)),
        "services.csv": to_csv_bytes((*Service._fields, "snapshot_id"), service_rows),
        "direct_links.csv": to_csv_bytes(
            (*Link._fields, "snapshot_id"), ((*row, snapshot_id) for row in result.links)
        ),
    }
    row_counts = (len(station_rows), len(result.services), len(result.links))
    links_by_type = Counter(link.transport_type for link in result.links)
    services_by_type = Counter(service.transport_type for service in result.services)
    report = {
        "status": "success",
        "snapshot_id": snapshot_id,
        "source": "Яндекс Расписания",
        "service_date": day.isoformat(),
        "input": {
            "stored_responses": manifest["stored_responses"],
            "responses_sha256": manifest["responses_sha256"],
            "cities_sha256": manifest["cities_sha256"],
            "responses_by_method": responses,
            "matched_cities": len(matching.matches),
            "unmatched_cities": unmatched,
        },
        "balance": {
            "departure_observations": result.departures,
            "air_arrivals": {
                "rows": len(arrivals),
                "exact_duplicate": len(arrivals) - len(set(arrivals)),
            },
            "schedule_rows_excluded": excluded,
            "pair_candidates": result.pairs,
            "services_by_type": services_by_type,
            "direct_links_by_type": links_by_type,
            "services_in_direct_links": sum(link.services for link in result.links),
        },
        "output": {
            name: {"rows": count, "sha256": hashlib.sha256(data).hexdigest()}
            for (name, data), count in zip(outputs.items(), row_counts, strict=True)
        },
        "service": {"code_version": code_version},
    }
    return report, outputs


def process_rasp_snapshot(
    day: date,
    *,
    raw_root: Path = DEFAULT_RAW_ROOT,
    cities_path: Path = DEFAULT_CITIES_PATH,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    code_version: str = "unknown",
) -> dict[str, Any]:
    """Normalize one verified snapshot without API calls and replace the output on success."""
    store = ResponseStore(snapshot_dir(raw_root, day))
    target = (output_root / store.root.name).resolve()
    source = store.root.resolve()
    if (
        source.is_relative_to(target)
        or target.is_relative_to(source)
        or cities_path.resolve().is_relative_to(target)
        or target.parent != output_root.resolve()
    ):
        raise ProcessingError("Output directory would overwrite the source snapshot or cities.csv")
    started_at = datetime.now(UTC)
    started = time.perf_counter()
    tracemalloc.start()
    try:
        report, outputs = _run(day, store, cities_path, code_version)
        peak = tracemalloc.get_traced_memory()[1]
    except (KeyError, TypeError, ValueError) as error:
        raise ProcessingError(f"Invalid Yandex Rasp snapshot: {error}") from error
    finally:
        tracemalloc.stop()
    report["service"].update(
        started_at_utc=started_at.isoformat(timespec="seconds"),
        duration_sec=round(time.perf_counter() - started, 3),
        peak_memory_bytes=peak,
        peak_memory_method="tracemalloc",
    )
    outputs["run-report.json"] = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode()
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.with_name(f".{target.name}.publish.lock")
    try:
        lock.mkdir()
    except FileExistsError as error:
        raise ProcessingError(f"Yandex Rasp publication lock exists: {lock.name}") from error
    try:
        publish_directory(target, outputs)
    finally:
        lock.rmdir()
    return report
