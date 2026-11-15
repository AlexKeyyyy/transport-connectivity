"""Match project cities to Yandex Rasp settlements and choose stations to query.

Pure functions over the parsed ``stations_list`` response and ``cities.csv``; no network
and no files are written here. Rules and their reasons: docs/yandex-schedules-plan.md.
"""

from __future__ import annotations

import csv
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RUSSIA_TITLE = "Россия"
# Measured to the nearest station of a settlement: Yandex settlements have no coordinates.
# Magadan (39 km) and Magas (23 km) are correct matches whose closest station is an airport.
MAX_MATCH_DISTANCE_KM = 50.0
EARTH_RADIUS_KM = 6371.0088

# Suburban platforms and stops do not serve long-distance trains.
TRAIN_STATION_TYPES = frozenset({"train_station", "station", ""})

UNMATCHED_NO_NAME = "no_settlement_with_this_name"
UNMATCHED_TOO_FAR = "nearest_station_too_far"


@dataclass(frozen=True, slots=True)
class Station:
    code: str
    title: str
    transport_type: str
    station_type: str
    latitude: float | None
    longitude: float | None


@dataclass(frozen=True, slots=True)
class Settlement:
    code: str
    title: str
    region: str
    stations: tuple[Station, ...]


@dataclass(frozen=True, slots=True)
class City:
    city_id: str
    name: str
    region: str
    latitude: float
    longitude: float


@dataclass(frozen=True, slots=True)
class CityMatch:
    city: City
    settlement: Settlement
    distance_km: float
    # Other same-name settlements within the radius; non-zero values are worth a look.
    alternatives: int


@dataclass(frozen=True, slots=True)
class UnmatchedCity:
    city: City
    reason: str


@dataclass(frozen=True, slots=True)
class Matching:
    matches: tuple[CityMatch, ...]
    unmatched: tuple[UnmatchedCity, ...]

    def station_cities(self) -> dict[str, str]:
        """Station code -> city_id for every station of a matched settlement."""
        return {
            station.code: match.city.city_id
            for match in self.matches
            for station in match.settlement.stations
        }


def normalize_name(name: str) -> str:
    return name.strip().lower().replace("ё", "е")


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance by the haversine formula."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def _coordinate(value: Any) -> float | None:
    # Missing coordinates come as "" in the response; bool is excluded on purpose.
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


def parse_settlements(stations_list: Mapping[str, Any]) -> list[Settlement]:
    """Russian settlements that have a Yandex code, with their stations."""
    countries = [c for c in stations_list.get("countries", []) if c.get("title") == RUSSIA_TITLE]
    if len(countries) != 1:
        raise ValueError(f"Expected one country titled {RUSSIA_TITLE!r} in stations_list")

    settlements = []
    for region in countries[0].get("regions", []):
        for item in region.get("settlements", []):
            code = item.get("codes", {}).get("yandex_code")
            if not code:
                continue
            stations = tuple(
                Station(
                    code=station["codes"]["yandex_code"],
                    title=station.get("title", ""),
                    transport_type=station.get("transport_type", ""),
                    station_type=station.get("station_type", ""),
                    latitude=_coordinate(station.get("latitude")),
                    longitude=_coordinate(station.get("longitude")),
                )
                for station in item.get("stations", [])
                if station.get("codes", {}).get("yandex_code")
            )
            settlements.append(Settlement(code, item.get("title", ""), region["title"], stations))
    return settlements


def load_cities(path: Path) -> list[City]:
    """Read the normalized ``cities.csv`` written by ``process``."""
    with path.open(encoding="utf-8", newline="") as handle:
        return [
            City(
                city_id=row["city_id"],
                name=row["name"],
                region=row["region"],
                latitude=float(row["latitude"]),
                longitude=float(row["longitude"]),
            )
            for row in csv.DictReader(handle)
        ]


def _nearest_station_km(city: City, settlement: Settlement) -> float | None:
    distances = [
        distance_km(city.latitude, city.longitude, station.latitude, station.longitude)
        for station in settlement.stations
        if station.latitude is not None and station.longitude is not None
    ]
    return min(distances, default=None)


def match_cities(
    cities: Iterable[City],
    settlements: Iterable[Settlement],
    max_distance_km: float = MAX_MATCH_DISTANCE_KM,
) -> Matching:
    """Match each city to the same-name settlement whose nearest station is closest.

    The name narrows the candidates and the distance confirms them: names repeat across
    regions (Пушкино), and Yandex region titles differ from ours (Москва и Московская
    область), so neither is enough alone.
    """
    by_name: dict[str, list[Settlement]] = {}
    for settlement in settlements:
        by_name.setdefault(normalize_name(settlement.title), []).append(settlement)

    matches: list[CityMatch] = []
    unmatched: list[UnmatchedCity] = []
    for city in cities:
        candidates = []
        for settlement in by_name.get(normalize_name(city.name), []):
            distance = _nearest_station_km(city, settlement)
            if distance is not None:
                candidates.append((distance, settlement.code, settlement))
        if not candidates:
            unmatched.append(UnmatchedCity(city, UNMATCHED_NO_NAME))
            continue
        candidates.sort(key=lambda item: item[:2])
        within = [item for item in candidates if item[0] <= max_distance_km]
        if not within:
            unmatched.append(UnmatchedCity(city, UNMATCHED_TOO_FAR))
            continue
        distance, _, settlement = within[0]
        matches.append(CityMatch(city, settlement, round(distance, 1), len(within) - 1))
    return Matching(tuple(matches), tuple(unmatched))


def plane_stations(match: CityMatch) -> list[Station]:
    return [s for s in match.settlement.stations if s.transport_type == "plane"]


def train_stations(match: CityMatch) -> list[Station]:
    return [
        s
        for s in match.settlement.stations
        if s.transport_type == "train" and s.station_type in TRAIN_STATION_TYPES
    ]


def main_train_stations(match: CityMatch) -> list[Station]:
    """Stations to start from: terminals and stations named after the city.

    Station types in Yandex are uneven (Новосибирск-Главный is a plain ``station``), so
    the name is used as a second sign. Stations missed here are added later when a
    collected train is found stopping at them. A city without such stations starts
    from all of its train stations.
    """
    stations = train_stations(match)
    city_name = normalize_name(match.city.name)
    main = [
        s
        for s in stations
        if s.station_type == "train_station" or normalize_name(s.title).startswith(city_name)
    ]
    return main or stations
