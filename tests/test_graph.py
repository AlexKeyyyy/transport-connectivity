"""Offline tests for graph.py (Issue #28) on small artificial matrices."""

from __future__ import annotations

from decimal import Decimal

import pytest

from transport_connectivity.graph import (
    AIR_ACCESS_SEC,
    TimedMeasurement,
    best_transport_types,
    layer_times,
    prune,
    unreachable_pairs,
)

D = Decimal


def road(a: str, b: str, seconds: str | None) -> TimedMeasurement:
    return TimedMeasurement(a, b, "road", None if seconds is None else D(seconds))


def triangle(direct: str, leg_ab: str, leg_bc: str) -> dict[tuple[str, str], Decimal]:
    return {("A", "C"): D(direct), ("A", "B"): D(leg_ab), ("B", "C"): D(leg_bc)}


def test_air_constant_is_three_hours() -> None:
    assert AIR_ACCESS_SEC == 10800


def test_detour_with_equal_time_removes_edge() -> None:
    times = triangle("100", "40", "60")
    assert prune(times, D("0")) == {("A", "B"), ("B", "C")}


def test_longer_detour_keeps_edge() -> None:
    times = triangle("100", "60", "50")  # detour 110 > 1.05 * 100
    assert ("A", "C") in prune(times, D("0.05"))


def test_detour_within_alpha_removes_edge() -> None:
    times = triangle("100", "60", "50")  # detour 110 <= 1.20 * 100
    assert ("A", "C") not in prune(times, D("0.20"))


def test_exact_boundary_removes_edge() -> None:
    times = triangle("100", "60", "45")  # detour 105 == (1 + 0.05) * 100
    assert ("A", "C") not in prune(times, D("0.05"))


def test_just_over_boundary_keeps_edge() -> None:
    times = triangle("100", "60", "45.01")
    assert ("A", "C") in prune(times, D("0.05"))


def test_pruning_uses_full_matrix_not_thinned_graph() -> None:
    # A->B is removed through X, and A->X is removed through Y; A->B must still be judged
    # by the full matrix, so removal does not depend on which edge is checked first.
    times = {
        ("A", "B"): D(100),
        ("A", "X"): D(50),
        ("X", "B"): D(50),
        ("A", "Y"): D(25),
        ("Y", "X"): D(25),
    }
    kept = prune(times, D(0))
    assert ("A", "B") not in kept
    assert ("A", "X") not in kept
    assert kept == {("A", "Y"), ("Y", "X"), ("X", "B")}


def test_directions_are_independent() -> None:
    times = {
        ("A", "C"): D(100),
        ("A", "B"): D(40),
        ("B", "C"): D(60),
        # reverse direction: no detour exists
        ("C", "A"): D(100),
    }
    kept = prune(times, D(0))
    assert ("A", "C") not in kept
    assert ("C", "A") in kept


def test_self_via_is_not_a_detour() -> None:
    # C must differ from A and B: a one-edge matrix has nothing to remove it.
    assert prune({("A", "B"): D(10)}, D("0.2")) == {("A", "B")}


def test_both_ends_are_not_detour_candidates() -> None:
    # Self-pairs with zero time would turn A or B into a "detour" of the edge itself:
    # t(A,A) + t(A,B) == t(A,B) and t(A,B) + t(B,B) == t(A,B). Neither end may count.
    times = {("A", "B"): D(100), ("A", "A"): D(0), ("B", "B"): D(0)}
    assert ("A", "B") in prune(times, D(0))
    assert ("A", "B") in prune(times, D("0.2"))


def test_negative_alpha_rejected() -> None:
    with pytest.raises(ValueError):
        prune({("A", "B"): D(10)}, D("-0.1"))


def test_air_gets_access_time() -> None:
    measurements = [TimedMeasurement("A", "B", "air", D(3600))]
    assert layer_times(measurements, "best") == {("A", "B"): D(14400)}


def test_best_picks_minimum_and_type() -> None:
    measurements = [
        road("A", "B", "20000"),
        TimedMeasurement("A", "B", "rail", D(15000)),
        TimedMeasurement("A", "B", "air", D(3600)),  # 14400 after the correction
        road("B", "A", "5000"),
        TimedMeasurement("B", "A", "air", D(100)),  # 10900: road is faster
    ]
    assert layer_times(measurements, "best") == {("A", "B"): D(14400), ("B", "A"): D(5000)}
    assert best_transport_types(measurements) == {("A", "B"): "air", ("B", "A"): "road"}


def test_best_tie_prefers_road_then_rail() -> None:
    measurements = [
        TimedMeasurement("A", "B", "rail", D(20000)),
        TimedMeasurement("A", "B", "air", D(9200)),  # 20000 after the correction
        road("A", "B", "20000"),
    ]
    assert best_transport_types(measurements) == {("A", "B"): "road"}


def test_road_and_rail_layers_ignore_other_types_and_air_correction() -> None:
    measurements = [
        road("A", "B", "20000"),
        TimedMeasurement("A", "B", "rail", D(15000)),
        TimedMeasurement("A", "B", "air", D(3600)),
    ]
    assert layer_times(measurements, "road") == {("A", "B"): D(20000)}
    assert layer_times(measurements, "rail") == {("A", "B"): D(15000)}


def test_missing_measurement_means_no_edge() -> None:
    measurements = [road("A", "B", None), TimedMeasurement("A", "B", "rail", D(500))]
    assert layer_times(measurements, "road") == {}
    assert layer_times(measurements, "best") == {("A", "B"): D(500)}


def test_unknown_layer_and_type_rejected() -> None:
    with pytest.raises(ValueError):
        layer_times([], "air")
    with pytest.raises(ValueError):
        layer_times([TimedMeasurement("A", "B", "boat", D(1))], "best")


def test_duplicate_measurement_rejected() -> None:
    with pytest.raises(ValueError):
        layer_times([road("A", "B", "1"), road("A", "B", "2")], "road")


def test_directed_pairs_are_independent_in_layer_times() -> None:
    times = layer_times([road("A", "B", "10"), road("B", "A", "30")], "road")
    assert times == {("A", "B"): D(10), ("B", "A"): D(30)}


def test_unreachable_pairs_counts_pairs_without_a_path() -> None:
    times = {("A", "B"): D(1), ("B", "C"): D(1), ("A", "C"): D(2), ("C", "A"): D(2)}
    assert unreachable_pairs(times, {("A", "B"), ("B", "C")}) == 1  # C->A has no path
    assert unreachable_pairs(times, set(times)) == 0


def test_unreachable_pairs_follows_multi_hop_paths() -> None:
    times = {("A", "C"): D(2)}
    assert unreachable_pairs(times, {("A", "B"), ("B", "C")}) == 0


def test_isolated_city_stays_unreachable_not_dropped() -> None:
    # D has no edges at all: pairs that involve it count as unreachable, nothing crashes.
    times = {("A", "B"): D(1), ("A", "D"): D(5), ("D", "A"): D(5)}
    assert unreachable_pairs(times, {("A", "B")}) == 2


def test_same_input_same_result_and_order() -> None:
    measurements = [
        road("B", "C", "60"),
        road("A", "C", "100"),
        road("A", "B", "40"),
        TimedMeasurement("C", "A", "air", D(50)),
    ]
    first = layer_times(measurements, "best")
    second = layer_times(list(reversed(measurements)), "best")
    assert first == second
    assert list(first) == list(second) == sorted(first)
    assert prune(first, D("0.1")) == prune(second, D("0.1"))
