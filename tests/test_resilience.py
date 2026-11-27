from decimal import Decimal

import pytest

from transport_connectivity.resilience import (
    add_bidirectional_link,
    adjacency,
    betweenness,
    reach_population,
    removal_effect,
    shortest_times,
    suggest_links,
)


def test_directed_chain_and_isolated_city() -> None:
    graph = adjacency("ABCD", [("A", "B", Decimal(2)), ("B", "C", Decimal(3))])
    assert shortest_times(graph, "A") == {"A": 0, "B": 2, "C": 5}
    assert shortest_times(graph, "C") == {"C": 0}
    assert betweenness(graph) == pytest.approx({"A": 0, "B": 1 / 6, "C": 0, "D": 0})


def test_star_removal_excludes_center_and_diagonal() -> None:
    edges = [(a, b, Decimal(1)) for leaf in "ABC" for a, b in ((leaf, "H"), ("H", leaf))]
    graph = adjacency("ABCH", edges)
    baseline = {origin: shortest_times(graph, origin) for origin in graph}
    assert betweenness(graph) == {"A": 0, "B": 0, "C": 0, "H": 1}
    effect = removal_effect(graph, "H", baseline)
    assert (effect.reachable_before, effect.reachable_after, effect.lost_pairs) == (6, 0, 6)
    assert effect.comparable_pairs == 0
    assert effect.mean_time_change_percent is None


def test_equal_shortest_paths_split_credit() -> None:
    edges = [(a, b, Decimal(1)) for a, b in ("AB", "AC", "BD", "CD")]
    assert betweenness(adjacency("ABCD", edges)) == pytest.approx(
        {"A": 0, "B": 1 / 12, "C": 1 / 12, "D": 0}
    )


def test_removal_separates_lost_pairs_from_time_change() -> None:
    graph = adjacency(
        "ABCD",
        [
            (a, b, Decimal(w))
            for a, b, w in [("A", "B", 2), ("B", "C", 3), ("A", "C", 10), ("B", "D", 1)]
        ],
    )
    effect = removal_effect(graph, "B", {origin: shortest_times(graph, origin) for origin in graph})
    assert (effect.lost_pairs, effect.comparable_pairs) == (1, 1)
    assert (effect.total_before_sec, effect.total_after_sec) == (5, 10)
    assert effect.mean_time_change_percent == 100


def test_new_link_preserves_faster_direction_and_counts_population_at_threshold() -> None:
    graph = adjacency(
        "ABC", [("A", "B", Decimal(10)), ("B", "A", Decimal(2)), ("B", "C", Decimal(3))]
    )
    updated = add_bidirectional_link(graph, "A", "B", Decimal(4))
    assert shortest_times(graph, "A")["C"] == 13
    assert shortest_times(updated, "A")["C"] == 7
    assert shortest_times(updated, "B")["A"] == 2
    assert reach_population(shortest_times(updated, "A"), {"A": 100, "B": 20, "C": 1}, "A", 7) == 21


def test_suggest_links_prioritizes_slow_pair_once_and_uses_layer_speed() -> None:
    graph = adjacency(
        "ABC",
        [
            ("A", "B", Decimal(100)),
            ("B", "A", Decimal(90)),
            ("B", "C", Decimal(5)),
        ],
    )
    baseline = {origin: shortest_times(graph, origin) for origin in graph}
    distances = {("A", "B"): 10.0, ("B", "C"): 20.0, ("A", "C"): 25.0}
    speed_km_h, proposals = suggest_links(graph, baseline, distances, limit=2)
    assert speed_km_h == pytest.approx((10 / 100 + 10 / 90 + 20 / 5) / 3 * 3600)
    assert [(link.source, link.target) for link in proposals] == [("A", "B"), ("A", "C")]
    assert proposals[0].proposed_time_sec == 7
