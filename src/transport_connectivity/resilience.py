"""Scenario metrics for a directed graph with strictly positive travel times."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from heapq import heappop, heappush
from statistics import fmean

Graph = Mapping[str, Mapping[str, Decimal]]


def adjacency(
    cities: Iterable[str], edges: Iterable[tuple[str, str, Decimal]]
) -> dict[str, dict[str, Decimal]]:
    graph: dict[str, dict[str, Decimal]] = {city: {} for city in cities}
    for source, target, weight in edges:
        if source not in graph or target not in graph:
            raise ValueError("edge endpoints must be in the graph")
        if source == target or not weight.is_finite() or weight <= 0:
            raise ValueError("edges need distinct endpoints and a positive finite time")
        graph[source][target] = min(weight, graph[source].get(target, weight))
    return graph


@dataclass
class _Paths:
    times: dict[str, Decimal]
    order: list[str]
    counts: dict[str, int]
    predecessors: dict[str, list[str]]


def _paths(
    graph: Graph, origin: str, removed: str | None = None, *, count_ties: bool = False
) -> _Paths:
    if origin not in graph or origin == removed:
        raise ValueError(f"origin unavailable: {origin}")
    times = {origin: Decimal(0)}
    queue = [(times[origin], origin)]
    predecessors: dict[str, list[str]] = {}
    counts = {origin: 1}
    order = []
    while queue:
        time, source = heappop(queue)
        if time != times[source]:
            continue
        order.append(source)
        for target, weight in graph[source].items():
            if target == removed:
                continue
            candidate = time + weight
            known = times.get(target)
            if known is None or candidate < known:
                times[target] = candidate
                heappush(queue, (candidate, target))
                if count_ties:
                    counts[target] = counts[source]
                    predecessors[target] = [source]
            elif count_ties and candidate == known:
                counts[target] += counts[source]
                predecessors[target].append(source)
    return _Paths(times, order, counts, predecessors)


def shortest_times(graph: Graph, origin: str, *, removed: str | None = None) -> dict[str, Decimal]:
    """Shortest directed times, including zero for the origin."""
    return _paths(graph, origin, removed).times


def betweenness(graph: Graph) -> dict[str, float]:
    """Weighted Brandes centrality normalized by (n-1)(n-2) for directed pairs."""
    scores = dict.fromkeys(graph, 0.0)
    if len(graph) < 3:
        return scores
    for origin in graph:
        paths = _paths(graph, origin, count_ties=True)
        dependency = dict.fromkeys(paths.order, 0.0)
        for target in reversed(paths.order[1:]):
            for predecessor in paths.predecessors[target]:
                fraction = paths.counts[predecessor] / paths.counts[target]
                dependency[predecessor] += fraction * (1 + dependency[target])
            scores[target] += dependency[target]
    normalization = (len(graph) - 1) * (len(graph) - 2)
    return {city: score / normalization for city, score in scores.items()}


@dataclass(frozen=True)
class RemovalEffect:
    reachable_before: int
    reachable_after: int
    comparable_pairs: int
    total_before_sec: Decimal
    total_after_sec: Decimal

    @property
    def lost_pairs(self) -> int:
        return self.reachable_before - self.reachable_after

    @property
    def mean_time_change_percent(self) -> Decimal | None:
        if not self.comparable_pairs:
            return None
        return (self.total_after_sec - self.total_before_sec) * 100 / self.total_before_sec


def removal_effect(
    graph: Graph, removed: str, baseline: Mapping[str, Mapping[str, Decimal]]
) -> RemovalEffect:
    """Exclude removed and self endpoints from both counts and compare common paths."""
    if removed not in graph:
        raise ValueError(f"unknown city: {removed}")
    before_count = after_count = common_count = 0
    before_sum = after_sum = Decimal(0)
    for origin in graph:
        if origin == removed:
            continue
        before = baseline[origin]
        after = shortest_times(graph, origin, removed=removed)
        before_count += len(before) - 1 - int(removed in before)
        after_count += len(after) - 1
        for target, duration in after.items():
            if target != origin and target in before:
                common_count += 1
                before_sum += before[target]
                after_sum += duration
    return RemovalEffect(before_count, after_count, common_count, before_sum, after_sum)


def add_bidirectional_link(graph: Graph, first: str, second: str, time_sec: Decimal) -> Graph:
    """Add a parallel connection and retain the faster weight in each direction."""
    proposals = adjacency(graph, ((first, second, time_sec), (second, first, time_sec)))
    updated = dict(graph)
    for source, target in ((first, second), (second, first)):
        weight = proposals[source][target]
        updated[source] = {**graph[source], target: min(weight, graph[source].get(target, weight))}
    return updated


def reach_population(
    times: Mapping[str, Decimal], population: Mapping[str, int], origin: str, threshold_sec: int
) -> int:
    return sum(
        population[target]
        for target, duration in times.items()
        if target != origin and duration <= threshold_sec
    )


@dataclass(frozen=True)
class LinkProposal:
    source: str
    target: str
    distance_km: float
    shortest_time_sec: Decimal
    time_per_km: float
    proposed_time_sec: int


def suggest_links(
    graph: Graph,
    baseline: Mapping[str, Mapping[str, Decimal]],
    distances_km: Mapping[tuple[str, str], float],
    limit: int,
) -> tuple[float, list[LinkProposal]]:
    """Rank distinct city pairs by the slower reachable direction per kilometre."""
    speed_km_sec = fmean(
        distance / float(weight)
        for source, neighbours in graph.items()
        for target, weight in neighbours.items()
        if (distance := distances_km[tuple(sorted((source, target)))]) > 0
    )
    ranked = sorted(
        (
            (float(duration) / distance, source, target, distance, duration)
            for source, times in baseline.items()
            for target, duration in times.items()
            if source != target
            if (distance := distances_km[tuple(sorted((source, target)))]) > 0
        ),
        key=lambda row: (-row[0], row[1], row[2]),
    )
    seen: set[frozenset[str]] = set()
    proposals = []
    for ratio, source, target, distance, duration in ranked:
        pair = frozenset((source, target))
        if pair in seen:
            continue
        seen.add(pair)
        proposals.append(
            LinkProposal(
                source, target, distance, duration, ratio, max(1, round(distance / speed_km_sec))
            )
        )
        if len(proposals) == limit:
            break
    return speed_km_sec * 3600, proposals
