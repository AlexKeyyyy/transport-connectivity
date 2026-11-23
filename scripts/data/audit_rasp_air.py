"""Audit the coverage and departure balance of a normalized Yandex Rasp snapshot."""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from transport_connectivity.connector import sha256_bytes
from transport_connectivity.processor import DEFAULT_OUTPUT_ROOT
from transport_connectivity.rasp_connector import (
    DEFAULT_CITIES_PATH,
    ResponseStore,
    responses_digest,
)
from transport_connectivity.rasp_stations import RUSSIA_TITLE, normalize_name


def read_verified_csv(
    directory: Path, filename: str, report: dict[str, Any]
) -> list[dict[str, str]]:
    data = (directory / filename).read_bytes()
    expected = report["output"][filename]
    if sha256_bytes(data) != expected["sha256"]:
        raise ValueError(f"{filename} SHA-256 differs from the normalization report")
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8"), newline=""), strict=True))
    if len(rows) != expected["rows"]:
        raise ValueError(f"{filename} row count differs from the normalization report")
    return rows


def title_hints(
    raw_dir: Path, cities: list[dict[str, str]], report: dict[str, Any]
) -> dict[str, int]:
    """Compare terminal names in route titles, without inferring actual arrival stations."""
    source = report["input"]
    if responses_digest(ResponseStore(raw_dir)) != (
        source["stored_responses"],
        source["responses_sha256"],
    ):
        raise ValueError("Raw response count or SHA-256 differs from the normalization report")
    labels = {normalize_name(row["name"]) for row in cities}
    stations = json.loads((raw_dir / "stations_list.json").read_bytes())["body"]
    countries: dict[str, set[str]] = defaultdict(set)
    for country in stations["countries"]:
        for region in country["regions"]:
            for settlement in region["settlements"]:
                countries[normalize_name(settlement["title"])].add(country["title"])

    day = date.fromisoformat(report["service_date"])
    last_arrival_day = day + timedelta(days=1)
    departures: dict[tuple[str, str, datetime], set[str]] = defaultdict(set)
    arrivals: dict[str, set[tuple[str, datetime]]] = defaultdict(set)
    for path in sorted(raw_dir.glob("schedule/*_plane_*.json")):
        response = json.loads(path.read_bytes())
        if response["status"] != 200:
            continue
        params = response["request"]["params"]
        event, station = params["event"], params["station"]
        for row in response["body"]["schedule"]:
            if not row.get(event):
                continue
            at = datetime.fromisoformat(row[event])
            uid = row["thread"]["uid"]
            if event == "arrival" and day <= at.date() <= last_arrival_day:
                arrivals[uid].add((station, at))
            elif event == "departure" and at.date() == day:
                title = row["thread"].get("title", "")
                _, _, end = title.rpartition(" — ")
                departures[station, uid, at].add(normalize_name(end))

    unmatched = Counter[str]()
    missing_uid = 0
    for (station, uid, at), names in departures.items():
        known = arrivals.get(uid, ())
        if any(other != station and later > at for other, later in known):
            continue
        if not known:
            missing_uid += 1
        name = next(iter(names)) if len(names) == 1 else ""
        if name in labels:
            category = "project_city_name"
        elif RUSSIA_TITLE in countries[name]:
            category = "russian_name_outside_project"
        elif countries[name]:
            category = "foreign_name_only"
        else:
            category = "unknown_or_ambiguous"
        unmatched[category] += 1

    balance = report["balance"]["departure_observations"]["air"]
    if (
        len(departures) != balance["unique"]
        or missing_uid != balance["no_cached_arrival"]
        or unmatched.total() - missing_uid != balance["no_later_arrival_at_another_station"]
    ):
        raise ValueError("Raw air observations do not reproduce the departure balance")
    return dict(sorted(unmatched.items()))


def audit(cities_dir: Path, rasp_dir: Path, raw_dir: Path | None = None) -> dict[str, object]:
    """Return small aggregate counts, keeping cities, departure records and links distinct."""
    cities_report = json.loads((cities_dir / "run-report.json").read_bytes())
    rasp_report = json.loads((rasp_dir / "run-report.json").read_bytes())
    if cities_report["status"] != "success" or rasp_report["status"] != "success":
        raise ValueError("Both normalization reports must have status success")
    cities_hash = cities_report["output"]["cities.csv"]["sha256"]
    if cities_hash != rasp_report["input"]["cities_sha256"]:
        raise ValueError("City directory differs from the Yandex Rasp input")

    cities = read_verified_csv(cities_dir, "cities.csv", cities_report)
    stations = read_verified_csv(rasp_dir, "stations.csv", rasp_report)
    links = read_verified_csv(rasp_dir, "direct_links.csv", rasp_report)
    snapshot_id = rasp_report["snapshot_id"]
    if any(row["snapshot_id"] != snapshot_id for row in stations + links):
        raise ValueError("A normalized row belongs to another snapshot")

    airport_rows = [row for row in stations if row["transport_type"] == "air"]
    air_links = [row for row in links if row["transport_type"] == "air"]
    city_ids = {row["city_id"] for row in cities}
    unmatched = {row["city_id"] for row in rasp_report["input"]["unmatched_cities"]}
    airport_cities = {row["city_id"] for row in airport_rows}
    linked_cities = {row[key] for row in air_links for key in ("from_city_id", "to_city_id")}
    if len(city_ids) != len(cities) or not unmatched <= city_ids:
        raise ValueError("Invalid city directory or unmatched city IDs")
    matched = city_ids - unmatched
    if not linked_cities <= airport_cities <= matched:
        raise ValueError("Expected air link cities to have matched airport records")
    if len(matched) != rasp_report["input"]["matched_cities"]:
        raise ValueError("Matched city count differs from the Yandex Rasp report")

    balance = rasp_report["balance"]
    departures = balance["departure_observations"]["air"]
    missing_uid = departures["no_cached_arrival"]
    unusable_arrival = departures["no_later_arrival_at_another_station"]
    with_arrival = departures["with_destination"]
    if missing_uid + unusable_arrival + with_arrival != departures["unique"]:
        raise ValueError("The aviation departure categories do not balance")
    station_services = sum(int(row["services"]) for row in air_links)
    if station_services != balance["services_by_type"].get("air", 0) or len(air_links) != balance[
        "direct_links_by_type"
    ].get("air", 0):
        raise ValueError("Aviation service or link count differs from the Yandex Rasp report")

    result: dict[str, object] = {
        "snapshot_id": snapshot_id,
        "service_date": rasp_report["service_date"],
        "city_coverage": {
            "cities": len(city_ids),
            "unmatched": len(unmatched),
            "matched_without_listed_airport": len(matched - airport_cities),
            "with_airport_without_observed_air_link": len(airport_cities - linked_cities),
            "with_airport": len(airport_cities),
            "with_observed_air_link": len(linked_cities),
            "listed_airports": len(airport_rows),
        },
        "departures": {
            "unique": departures["unique"],
            "with_compatible_arrival": with_arrival,
            "without_cached_arrival_uid": missing_uid,
            "without_later_arrival_at_another_station": unusable_arrival,
        },
        "air_station_services": station_services,
        "directed_air_links": len(air_links),
    }
    if raw_dir is not None:
        result["unmatched_departure_title_hints"] = title_hints(raw_dir, cities, rasp_report)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cities-dir", type=Path, default=DEFAULT_CITIES_PATH.parent)
    parser.add_argument(
        "--rasp-dir", type=Path, default=DEFAULT_OUTPUT_ROOT / "yandex_rasp_20261201"
    )
    parser.add_argument(
        "--raw-dir", type=Path, help="Saved API responses for the optional route-title check."
    )
    args = parser.parse_args()
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        result = audit(args.cities_dir, args.rasp_dir, args.raw_dir)
    except (OSError, ValueError, KeyError, TypeError, csv.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
