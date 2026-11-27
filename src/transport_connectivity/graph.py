"""Build a sparse directed graph from the all-pairs travel time matrix.

Implements the methodology of Issue #28 (docs/graph-methodology.md). Everything here is a
pure function over in-memory data: no SQL, no files, no randomness. The same input always
gives the same result.

Pair keys are ``(from_city_id, to_city_id)``; times are seconds as ``Decimal`` so that the
pruning boundary ``t(A,C) + t(C,B) == (1 + alpha) * t(A,B)`` is exact.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import NamedTuple

# Air time = source time + this constant: road to the airport and back, check-in, waiting.
AIR_ACCESS_SEC = Decimal(10800)

LAYERS = ("road", "rail", "best")
# Order matters: on equal time the first type wins, so the result never depends on input order.
_BEST_ORDER = ("road", "rail", "air")

Pair = tuple[str, str]


class TimedMeasurement(NamedTuple):
    """One source observation: a directed pair, a transport type and its time in seconds."""

    from_city_id: str
    to_city_id: str
    transport_type: str
    duration_sec: Decimal | None


def _candidates(
    measurements: Iterable[TimedMeasurement], types: tuple[str, ...]
) -> dict[Pair, list[tuple[Decimal, str]]]:
    """Group usable times by pair, with the air correction applied.

    A missing time (``None``) means there is no connection by this type, so it is skipped.
    """
    result: dict[Pair, list[tuple[Decimal, str]]] = {}
    seen: set[tuple[str, str, str]] = set()
    for m in measurements:
        if m.transport_type not in _BEST_ORDER:
            raise ValueError(f"unknown transport type: {m.transport_type!r}")
        key = (m.from_city_id, m.to_city_id, m.transport_type)
        if key in seen:
            raise ValueError(f"duplicate measurement: {key}")
        seen.add(key)
        if m.transport_type not in types or m.duration_sec is None:
            continue
        if m.duration_sec < 0:
            raise ValueError(f"negative duration: {key}")
        time = m.duration_sec + (AIR_ACCESS_SEC if m.transport_type == "air" else 0)
        result.setdefault((m.from_city_id, m.to_city_id), []).append((time, m.transport_type))
    return result


def _layer_types(layer: str) -> tuple[str, ...]:
    if layer not in LAYERS:
        raise ValueError(f"unknown layer: {layer!r}; expected one of {LAYERS}")
    return _BEST_ORDER if layer == "best" else (layer,)


def _winners(
    candidates: Mapping[Pair, list[tuple[Decimal, str]]],
) -> dict[Pair, tuple[Decimal, str]]:
    # min() over (time, order index) keeps the first type in _BEST_ORDER on ties.
    return {
        pair: min(options, key=lambda option: (option[0], _BEST_ORDER.index(option[1])))
        for pair, options in sorted(candidates.items())
    }


def layer_times(measurements: Iterable[TimedMeasurement], layer: str) -> dict[Pair, Decimal]:
    """Time in seconds for each directed pair of a layer (``road``, ``rail`` or ``best``).

    ``road`` and ``rail`` take the source time as is. ``best`` takes the minimum over the
    three types after adding ``AIR_ACCESS_SEC`` to air. Pairs without a usable time are
    absent. Keys are sorted, so the order is the same on every run.
    """
    winners = _winners(_candidates(measurements, _layer_types(layer)))
    return {pair: time for pair, (time, _) in winners.items()}


def best_transport_types(measurements: Iterable[TimedMeasurement]) -> dict[Pair, str]:
    """Transport type that gave the minimum time of the ``best`` layer for each pair."""
    winners = _winners(_candidates(measurements, _BEST_ORDER))
    return {pair: transport for pair, (_, transport) in winners.items()}


def prune(times: Mapping[Pair, Decimal], alpha: Decimal) -> set[Pair]:
    """Edges that stay after thinning.

    Edge A->B is removed if some city C (C != A, C != B; both ends are excluded) has
    ``t(A,C) + t(C,B) <= (1 + alpha) * t(A,B)``. ``t`` is always the full matrix ``times``,
    never the already thinned graph, so the result does not depend on the order of checks.
    Equality removes the edge. Iterate over the returned set with ``sorted()`` when a stable
    order is needed.
    """
    if alpha < 0:
        raise ValueError("alpha must not be negative")
    factor = 1 + alpha
    outgoing: dict[str, dict[str, Decimal]] = {}
    for (source, target), time in times.items():
        outgoing.setdefault(source, {})[target] = time

    kept: set[Pair] = set()
    for (source, target), time in sorted(times.items()):
        limit = factor * time
        detour_exists = False
        for via, first_leg in outgoing.get(source, {}).items():
            if via == source or via == target:
                continue
            second_leg = outgoing.get(via, {}).get(target)
            if second_leg is not None and first_leg + second_leg <= limit:
                detour_exists = True
                break
        if not detour_exists:
            kept.add((source, target))
    return kept


def unreachable_pairs(times: Mapping[Pair, Decimal], edges: Iterable[Pair]) -> int:
    """Pairs present in the matrix that have no directed path along ``edges``.

    Breadth-first search from every city; weights do not matter for reachability.
    """
    adjacency: dict[str, list[str]] = {}
    for source, target in edges:
        adjacency.setdefault(source, []).append(target)

    missing = 0
    targets_by_source: dict[str, set[str]] = {}
    for source, target in times:
        targets_by_source.setdefault(source, set()).add(target)
    for source in sorted(targets_by_source):
        reached = {source}
        queue = deque([source])
        while queue:
            current = queue.popleft()
            for neighbour in adjacency.get(current, ()):
                if neighbour not in reached:
                    reached.add(neighbour)
                    queue.append(neighbour)
        missing += len(targets_by_source[source] - reached)
    return missing
