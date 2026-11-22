"""Measure directed city reachability in an offline Yandex Rasp air snapshot."""

from __future__ import annotations

import csv
import io
import json
from collections import deque
from datetime import date
from pathlib import Path
from uuid import UUID

from transport_connectivity.connector import sha256_bytes
from transport_connectivity.processor import ProcessingError, city_id
from transport_connectivity.rasp_connector import snapshot_dir

MOSCOW_ID = UUID(city_id("2539"))  # Sber Territory ID in the project city directory
RASP_SOURCE = "Яндекс Расписания"


def compare_reachability(
    cities: set[UUID], links: set[tuple[UUID, UUID]], removed: UUID
) -> tuple[int, int]:
    """Count ordered pairs of distinct remaining cities, with and without the vertex."""
    graph: dict[UUID, set[UUID]] = {city: set() for city in cities}
    for origin, destination in links:
        graph[origin].add(destination)
    endpoints = cities - {removed}
    return (
        _count_reachable_pairs(graph, endpoints, blocked=set()),
        _count_reachable_pairs(graph, endpoints, blocked={removed}),
    )


def _count_reachable_pairs(
    graph: dict[UUID, set[UUID]], endpoints: set[UUID], *, blocked: set[UUID]
) -> int:
    pairs = 0
    for origin in endpoints:
        seen = {origin}
        queue = deque([origin])
        while queue:
            neighbors = graph[queue.popleft()] - seen - blocked
            seen.update(neighbors)
            queue.extend(neighbors)
        pairs += len(seen & endpoints) - 1
    return pairs


def _verified_rows(
    path: Path, output: dict[str, str | int], columns: set[str]
) -> tuple[list[dict[str, str]], str]:
    data = path.read_bytes()
    digest = sha256_bytes(data)
    if digest != output["sha256"]:
        raise ProcessingError(f"{path.name} SHA-256 does not match its normalization report")
    reader = csv.DictReader(io.StringIO(data.decode("utf-8"), newline=""), strict=True)
    if reader.fieldnames is None or not columns.issubset(reader.fieldnames):
        raise ProcessingError(f"{path.name} is missing required columns")
    rows = list(reader)
    if len(rows) != output["rows"]:
        raise ProcessingError(f"{path.name} row count does not match its normalization report")
    return rows, digest


def analyze_air_resilience(day: date, *, cities_dir: Path, rasp_dir: Path) -> dict[str, object]:
    """Verify both normalization reports and return the unweighted removal experiment."""
    cities_path = cities_dir / "cities.csv"
    links_path = rasp_dir / "direct_links.csv"
    snapshot_id = snapshot_dir(rasp_dir.parent, day).name
    try:
        cities_report = json.loads((cities_dir / "run-report.json").read_bytes())
        rasp_report = json.loads((rasp_dir / "run-report.json").read_bytes())
        if cities_report["status"] != "success" or rasp_report["status"] != "success":
            raise ProcessingError("Both normalization reports must have status success")
        if (
            rasp_report["snapshot_id"] != snapshot_id
            or rasp_report["service_date"] != day.isoformat()
            or rasp_report["source"] != RASP_SOURCE
        ):
            raise ProcessingError("Yandex Rasp report does not describe the requested snapshot")
        city_rows, cities_sha256 = _verified_rows(
            cities_path, cities_report["output"][cities_path.name], {"city_id"}
        )
        if cities_sha256 != rasp_report["input"]["cities_sha256"]:
            raise ProcessingError("cities.csv SHA-256 does not match the Yandex Rasp input")
        link_rows, links_sha256 = _verified_rows(
            links_path,
            rasp_report["output"][links_path.name],
            {"from_city_id", "to_city_id", "transport_type", "snapshot_id"},
        )
        expected_air_links = rasp_report["balance"]["direct_links_by_type"].get("air", 0)
        cities_snapshot_id = cities_report["snapshot_id"]
        cities = {UUID(row["city_id"]) for row in city_rows}
        air_links = _air_links(link_rows, cities, snapshot_id)
    except (KeyError, TypeError, ValueError, AttributeError, csv.Error) as error:
        raise ProcessingError(f"Invalid normalization report or CSV: {error}") from error
    if len(cities) != len(city_rows):
        raise ProcessingError("cities.csv contains duplicate city UUIDs")
    if MOSCOW_ID not in cities:
        raise ProcessingError(f"Moscow city UUID {MOSCOW_ID} is missing from cities.csv")
    if len(air_links) != expected_air_links:
        raise ProcessingError("Air link count does not match the Yandex Rasp normalization report")

    before, after = compare_reachability(cities, air_links, MOSCOW_ID)
    lost = before - after
    return {
        "source": RASP_SOURCE,
        "service_date": day.isoformat(),
        "rasp_snapshot_id": snapshot_id,
        "cities_snapshot_id": cities_snapshot_id,
        "input_sha256": {"cities.csv": cities_sha256, "direct_links.csv": links_sha256},
        "removed_city_id": str(MOSCOW_ID),
        "city_count": len(cities),
        "endpoint_city_count": len(cities) - 1,
        "air_link_count": len(air_links),
        "cities_with_air_links": len({city for link in air_links for city in link}),
        "reachable_pairs_before": before,
        "reachable_pairs_after": after,
        "lost_pairs": lost,
        "lost_pct_of_reachable_before": round(100 * lost / before, 1) if before else None,
    }


def _air_links(
    rows: list[dict[str, str]], cities: set[UUID], snapshot_id: str
) -> set[tuple[UUID, UUID]]:
    links: set[tuple[UUID, UUID]] = set()
    for row in rows:
        if row["snapshot_id"] != snapshot_id:
            raise ProcessingError("direct_links.csv contains a row from another snapshot")
        if row["transport_type"] == "rail":
            continue
        if row["transport_type"] != "air":
            raise ProcessingError("direct_links.csv contains an unknown transport type")
        origin, destination = UUID(row["from_city_id"]), UUID(row["to_city_id"])
        if origin not in cities or destination not in cities:
            raise ProcessingError(f"Air link references an unknown city: {origin} -> {destination}")
        if origin == destination or (origin, destination) in links:
            raise ProcessingError(f"Self-link or duplicate air link: {origin} -> {destination}")
        links.add((origin, destination))
    return links
