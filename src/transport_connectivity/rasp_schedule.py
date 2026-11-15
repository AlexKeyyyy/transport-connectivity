"""Read Yandex Rasp ``schedule`` and ``thread`` response bodies.

Pure functions shared by the collector (which requests to make next) and the
normalization of collected responses. Response fields: docs/yandex-schedules-plan.md.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

PAGE_LIMIT = 100


@dataclass(frozen=True, slots=True)
class ScheduleEntry:
    uid: str
    transport_type: str
    # Station-local times with offset; departure is None at the last stop of a thread.
    departure: datetime | None
    arrival: datetime | None


def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def schedule_entries(body: Mapping[str, Any]) -> list[ScheduleEntry]:
    return [
        ScheduleEntry(
            uid=item["thread"]["uid"],
            transport_type=item["thread"]["transport_type"],
            departure=_parse_time(item.get("departure")),
            arrival=_parse_time(item.get("arrival")),
        )
        for item in body.get("schedule", [])
    ]


def departing_on(entries: list[ScheduleEntry], day: date) -> list[ScheduleEntry]:
    """Entries that depart on ``day`` by station-local time.

    A response for a date also contains the early hours of the next day, and the same
    daily train then appears twice; this keeps one calendar day.
    """
    return [e for e in entries if e.departure is not None and e.departure.date() == day]


def arriving_on(entries: list[ScheduleEntry], day: date) -> list[ScheduleEntry]:
    return [e for e in entries if e.arrival is not None and e.arrival.date() == day]


def next_page_offsets(body: Mapping[str, Any]) -> list[int]:
    """Offsets of the pages after the first one, from the first page's pagination."""
    pagination = body.get("pagination", {})
    total = int(pagination.get("total", 0))
    limit = int(pagination.get("limit", PAGE_LIMIT)) or PAGE_LIMIT
    return list(range(limit, total, limit))


def thread_stop_codes(body: Mapping[str, Any]) -> list[str]:
    """Station codes of a thread in the order of travel."""
    return [stop["station"]["code"] for stop in body.get("stops", [])]
