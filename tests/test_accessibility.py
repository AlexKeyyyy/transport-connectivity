from decimal import Decimal

from transport_connectivity.accessibility import (
    RegionAccessibility,
    accessibility,
    reachable,
    region_summary,
)


def test_directed_threshold_metrics_include_unknown_population_and_zeros() -> None:
    times = {
        ("A", "A"): Decimal(0),
        ("A", "B"): Decimal(10800),
        ("A", "C"): Decimal(21600),
        ("C", "A"): Decimal(21601),
        ("C", "D"): Decimal(86400),
    }
    population = {"D": 40, "C": 30, "A": 10, "B": None}
    assert reachable(times, "A", 10800) == ["B"]
    assert reachable(times, "A", 21600) == ["B", "C"]
    assert reachable(times, "C", 21600) == []
    assert reachable(times, "C", 86400) == ["A", "D"]
    expected = {"A": (2, 30), "B": (0, 0), "C": (0, 0), "D": (0, 0)}
    assert accessibility(times, population, 21600) == expected
    assert accessibility(dict(reversed(list(times.items()))), population, 21600) == expected
    assert accessibility(times, population, 86400)["C"] == (2, 50)


def test_region_means_include_isolated_origins_and_keep_unknown_region() -> None:
    results = {"A": (1, 25), "B": (0, 0), "C": (2, 55), "D": (1, 10)}
    regions = {"A": "One", "B": "One", "C": "Two", "D": None}
    assert region_summary(results, regions) == [
        RegionAccessibility("One", 2, Decimal("12.5"), 0),
        RegionAccessibility("Two", 1, Decimal(55), 55),
        RegionAccessibility(None, 1, Decimal(10), 10),
    ]
