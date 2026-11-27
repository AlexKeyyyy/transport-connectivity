"""Normalize cached Yandex schedules into dated, directed services and city links."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from itertools import groupby
from typing import NamedTuple


@dataclass(frozen=True, slots=True, order=True)
class Event:
    uid: str
    station: str
    at: datetime
    response: str = field(default="", compare=False)


class Stop(NamedTuple):
    station: str
    departure: datetime | None
    arrives: bool
    elapsed_sec: int
    dwell_sec: int


class Candidate(NamedTuple):
    departure: Event
    to_station: str
    duration_sec: int
    arrival_response: str = ""


class Service(NamedTuple):
    transport_type: str
    uid: str
    departure_date: str
    from_station: str
    to_station: str
    from_city_id: str
    to_city_id: str
    departure_at: str
    duration_sec: int
    departure_response: str
    arrival_response: str


class Link(NamedTuple):
    from_city_id: str
    to_city_id: str
    transport_type: str
    services: int
    min_duration_sec: int


@dataclass(frozen=True, slots=True)
class Result:
    services: list[Service]
    links: list[Link]
    departures: dict[str, Counter[str]]
    pairs: dict[str, Counter[str]]


def _air_pairs(
    departures: set[Event], arrivals: set[Event], counts: Counter[str]
) -> Iterable[Candidate]:
    arrivals_by_uid: dict[str, list[Event]] = defaultdict(list)
    for arrival in sorted(arrivals):
        arrivals_by_uid[arrival.uid].append(arrival)
    for departure in sorted(departures):
        known = arrivals_by_uid[departure.uid]
        if not known:
            counts["no_cached_arrival"] += 1
            continue
        later = [
            arrival
            for arrival in known
            if arrival.station != departure.station and arrival.at > departure.at
        ]
        if not later:
            counts["no_later_arrival_at_another_station"] += 1
            continue
        counts["with_destination"] += 1
        for arrival in later:
            duration = (arrival.at - departure.at).total_seconds()
            if not duration.is_integer():
                raise ValueError("Fractional duration in an air schedule")
            yield Candidate(departure, arrival.station, int(duration), arrival.response)


def _rail_pairs(
    departures: set[Event],
    threads: Mapping[str, tuple[Stop, ...] | None],
    counts: Counter[str],
) -> Iterable[Candidate]:
    for departure in sorted(departures):
        stops = threads[departure.uid]
        if stops is None:
            counts["thread_not_found"] += 1
            continue
        # A thread without a date returns a different occurrence with station-local wall
        # times. Match the clock time to disambiguate a repeated visit to the same station.
        origins = [
            index
            for index, stop in enumerate(stops)
            if stop.station == departure.station
            and stop.departure is not None
            and stop.departure.time() == departure.at.time()
        ]
        if len(origins) != 1:
            counts["boarding_stop_not_unique"] += 1
            continue
        origin = origins[0]
        counts["boarding_stop_matched"] += 1
        elapsed_to_departure = stops[origin].elapsed_sec
        for stop in stops[origin + 1 :]:
            if stop.arrives:
                duration = stop.elapsed_sec - stop.dwell_sec - elapsed_to_departure
                yield Candidate(departure, stop.station, duration)


def _services(
    transport: str,
    candidates: Iterable[Candidate],
    station_cities: Mapping[str, str],
    day: date,
    counts: Counter[str],
) -> list[Service]:
    # On a loop the same destination may be reached more than once. One boarding
    # can sell one direct connection to that station: retain the first usable arrival.
    # Distinct boardings under the same contracted key remain visible as conflicts.
    grouped: dict[tuple[str, str, str], dict[Event, list[tuple[int, str]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for departure, station, duration, arrival_response in candidates:
        counts["candidates"] += 1
        from_city = station_cities.get(departure.station)
        to_city = station_cities.get(station)
        if from_city is None or to_city is None:
            counts["outside_project_cities"] += 1
        elif from_city == to_city:
            counts["same_city"] += 1
        elif duration <= 0:
            counts["nonpositive_duration"] += 1
        else:
            group = grouped[departure.uid, departure.station, station]
            group[departure].append((duration, arrival_response))

    services = []
    for (uid, origin, target), boardings in sorted(grouped.items()):
        if len(boardings) != 1:
            counts["key_conflicts"] += sum(map(len, boardings.values()))
            continue
        ((departure, arrivals),) = boardings.items()
        earliest, arrival_response = min(arrivals)
        later = sum(duration != earliest for duration, _ in arrivals)
        counts["later_arrival_to_same_station"] += later
        counts["exact_duplicate"] += len(arrivals) - later - 1
        services.append(
            Service(
                transport,
                uid,
                day.isoformat(),
                origin,
                target,
                station_cities[origin],
                station_cities[target],
                departure.at.isoformat(),
                earliest,
                departure.response,
                arrival_response,
            )
        )

    counts["services"] = len(services)
    used = sum(value for key, value in counts.items() if key != "candidates")
    if counts["candidates"] != used:
        raise AssertionError(f"Unbalanced {transport} service candidates")
    return services


def _unique_events(events: Iterable[Event]) -> set[Event]:
    # Keep the lexically first response reference when an event repeats across pages.
    return set(sorted(events, key=lambda event: event.response))


def normalize(
    day: date,
    station_cities: Mapping[str, str],
    air_departures: Iterable[Event],
    air_arrivals: Iterable[Event],
    rail_departures: Iterable[Event],
    threads: Mapping[str, tuple[Stop, ...] | None],
) -> Result:
    """Compute services without using clocks from different local time zones as UTC."""
    departures = {kind: Counter() for kind in ("air", "rail")}
    pairs = {kind: Counter() for kind in ("air", "rail")}
    services: list[Service] = []
    for kind, source in (("air", air_departures), ("rail", rail_departures)):
        rows = list(source)
        unique = _unique_events(rows)
        counts = departures[kind]
        counts.update(rows=len(rows), unique=len(unique), exact_duplicate=len(rows) - len(unique))
        candidates = (
            _air_pairs(unique, _unique_events(air_arrivals), counts)
            if kind == "air"
            else _rail_pairs(unique, threads, counts)
        )
        services.extend(_services(kind, candidates, station_cities, day, pairs[kind]))
        outcome_count = sum(
            v for k, v in counts.items() if k not in {"rows", "unique", "exact_duplicate"}
        )
        if outcome_count != len(unique):
            raise AssertionError(f"Unbalanced {kind} departures")

    def link_key(service: Service) -> tuple[str, str, str]:
        return service.from_city_id, service.to_city_id, service.transport_type

    links = []
    for key, rows in groupby(sorted(services, key=link_key), key=link_key):
        durations = [row.duration_sec for row in rows]
        links.append(Link(*key, len(durations), min(durations)))
    if sum(link.services for link in links) != len(services):
        raise AssertionError("Unbalanced services and direct links")
    return Result(services, links, departures, pairs)
