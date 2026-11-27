"""Threshold accessibility computed from a directed travel-time matrix."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from transport_connectivity.graph import Pair

THRESHOLDS_SEC = {"day_trip": 10800, "half_day": 21600, "one_day": 86400}


def reachable(times: Mapping[Pair, Decimal], origin: str, threshold_sec: int) -> list[str]:
    """Return destination IDs in the threshold, ordered by ID."""
    return sorted(
        target
        for (source, target), seconds in times.items()
        if source == origin and target != origin and seconds <= threshold_sec
    )


def accessibility(
    times: Mapping[Pair, Decimal], population: Mapping[str, int | None], threshold_sec: int
) -> dict[str, tuple[int, int]]:
    """Return (reachable cities, their known population) for every origin."""
    totals = dict.fromkeys(sorted(population), (0, 0))
    for (source, target), seconds in times.items():
        if source != target and seconds <= threshold_sec:
            count, inhabitants = totals[source]
            totals[source] = count + 1, inhabitants + (population[target] or 0)
    return totals


@dataclass(frozen=True)
class RegionAccessibility:
    region: str | None
    cities: int
    mean_reach_population: Decimal
    min_reach_population: int


def region_summary(
    results: Mapping[str, tuple[int, int]], regions: Mapping[str, str | None]
) -> list[RegionAccessibility]:
    """Group results by the origin region, weighting each origin equally."""
    grouped: dict[str | None, list[int]] = defaultdict(list)
    for city, (_, inhabitants) in results.items():
        grouped[regions[city]].append(inhabitants)
    return [
        RegionAccessibility(region, len(values), Decimal(sum(values)) / len(values), min(values))
        for region, values in sorted(grouped.items(), key=lambda item: (item[0] is None, item[0]))
    ]
