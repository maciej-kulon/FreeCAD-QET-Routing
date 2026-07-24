# SPDX-License-Identifier: LGPL-2.1-or-later
"""Dependency-free corridor graph and A* routing core."""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from itertools import count


class RoutingError(RuntimeError):
    pass


class NoRouteError(RoutingError):
    pass


@dataclass(frozen=True)
class Point3:
    x: float
    y: float
    z: float

    def distance_to(self, other: Point3) -> float:
        return math.dist(self.as_tuple(), other.as_tuple())

    def as_tuple(self) -> tuple[float, float, float]:
        return self.x, self.y, self.z

    @property
    def is_finite(self) -> bool:
        return all(math.isfinite(value) for value in self.as_tuple())


@dataclass(frozen=True)
class Box3:
    minimum: Point3
    maximum: Point3

    def __post_init__(self) -> None:
        if not self.minimum.is_finite or not self.maximum.is_finite:
            raise ValueError("Box coordinates must be finite")
        if (
            self.minimum.x > self.maximum.x
            or self.minimum.y > self.maximum.y
            or self.minimum.z > self.maximum.z
        ):
            raise ValueError("Box minimum must not exceed maximum")

    @property
    def center(self) -> Point3:
        return Point3(
            (self.minimum.x + self.maximum.x) / 2,
            (self.minimum.y + self.maximum.y) / 2,
            (self.minimum.z + self.maximum.z) / 2,
        )

    def contains(self, point: Point3, tolerance: float = 1e-9) -> bool:
        return (
            self.minimum.x - tolerance <= point.x <= self.maximum.x + tolerance
            and self.minimum.y - tolerance <= point.y <= self.maximum.y + tolerance
            and self.minimum.z - tolerance <= point.z <= self.maximum.z + tolerance
        )

    def clamp(self, point: Point3) -> Point3:
        return Point3(
            min(max(point.x, self.minimum.x), self.maximum.x),
            min(max(point.y, self.minimum.y), self.maximum.y),
            min(max(point.z, self.minimum.z), self.maximum.z),
        )

    def intersection(self, other: Box3, tolerance: float = 1e-9) -> Box3 | None:
        raw_minimum = Point3(
            max(self.minimum.x, other.minimum.x),
            max(self.minimum.y, other.minimum.y),
            max(self.minimum.z, other.minimum.z),
        )
        raw_maximum = Point3(
            min(self.maximum.x, other.maximum.x),
            min(self.maximum.y, other.maximum.y),
            min(self.maximum.z, other.maximum.z),
        )
        if (
            raw_minimum.x > raw_maximum.x + tolerance
            or raw_minimum.y > raw_maximum.y + tolerance
            or raw_minimum.z > raw_maximum.z + tolerance
        ):
            return None
        x_min, x_max = _collapse_tolerance_gap(
            raw_minimum.x,
            raw_maximum.x,
        )
        y_min, y_max = _collapse_tolerance_gap(
            raw_minimum.y,
            raw_maximum.y,
        )
        z_min, z_max = _collapse_tolerance_gap(
            raw_minimum.z,
            raw_maximum.z,
        )
        minimum = Point3(x_min, y_min, z_min)
        maximum = Point3(x_max, y_max, z_max)
        return Box3(minimum, maximum)


def axis_aligned_box_from_points(
    points: list[Point3] | tuple[Point3, ...],
    *,
    tolerance: float = 1e-7,
) -> Box3:
    """Return the non-degenerate world-axis-aligned box spanned by points."""

    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("Point tolerance must be a finite non-negative value")
    if len(points) < 2:
        raise ValueError("Select at least two points")
    if any(not point.is_finite for point in points):
        raise ValueError("Selected point coordinates must be finite")

    minimum = Point3(
        min(point.x for point in points),
        min(point.y for point in points),
        min(point.z for point in points),
    )
    maximum = Point3(
        max(point.x for point in points),
        max(point.y for point in points),
        max(point.z for point in points),
    )
    extents = (
        maximum.x - minimum.x,
        maximum.y - minimum.y,
        maximum.z - minimum.z,
    )
    if any(extent <= tolerance for extent in extents):
        raise ValueError(
            "Selected points must span a non-zero distance in X, Y, and Z"
        )
    for point in points:
        coordinates = (
            (point.x, minimum.x, maximum.x),
            (point.y, minimum.y, maximum.y),
            (point.z, minimum.z, maximum.z),
        )
        if any(
            min(abs(value - low), abs(value - high)) > tolerance
            for value, low, high in coordinates
        ):
            raise ValueError(
                "Selected vertices must be corners of an axis-aligned box; "
                "rotated corridors are not supported yet"
            )
    return Box3(minimum, maximum)


@dataclass(frozen=True)
class CorridorSpec:
    key: str
    box: Box3
    allowed_sections_mm2: tuple[float, ...] = ()
    capacity_mm2: float = 0.0
    max_fill_ratio: float = 1.0
    initial_used_area_mm2: float = 0.0

    def allows(self, section_mm2: float, tolerance: float = 1e-6) -> bool:
        if not self.allowed_sections_mm2:
            return True
        return any(abs(value - section_mm2) <= tolerance for value in self.allowed_sections_mm2)


@dataclass(frozen=True)
class WireRequest:
    key: str
    start: Point3
    end: Point3
    section_mm2: float
    occupancy_area_mm2: float | None = None

    @property
    def occupancy(self) -> float:
        if self.occupancy_area_mm2 is None:
            return self.section_mm2
        return self.occupancy_area_mm2


@dataclass(frozen=True)
class RouteResult:
    wire_key: str
    points: tuple[Point3, ...]
    corridor_keys: tuple[str, ...]
    geometric_length: float


@dataclass(frozen=True)
class _Edge:
    target: str
    cost: float
    points: tuple[Point3, ...]
    corridor_key: str = ""


class CorridorRouter:
    """Routes wires through connected, axis-aligned convex corridor volumes."""

    def __init__(
        self,
        corridors: list[CorridorSpec] | tuple[CorridorSpec, ...],
        *,
        congestion_weight: float = 2.0,
    ) -> None:
        keys = [corridor.key for corridor in corridors]
        if len(keys) != len(set(keys)):
            raise ValueError("Corridor keys must be unique")
        self.corridors = tuple(corridors)
        self._corridor_by_key = {corridor.key: corridor for corridor in corridors}
        self.congestion_weight = max(congestion_weight, 0.0)
        self._usage = {
            corridor.key: max(corridor.initial_used_area_mm2, 0.0)
            for corridor in corridors
        }

    def route(self, request: WireRequest) -> RouteResult:
        if not request.start.is_finite or not request.end.is_finite:
            raise RoutingError(f"Wire {request.key} has non-finite endpoint coordinates")
        if not math.isfinite(request.section_mm2) or request.section_mm2 <= 0:
            raise RoutingError(f"Wire {request.key} must have a positive section")
        if not math.isfinite(request.occupancy) or request.occupancy <= 0:
            raise RoutingError(f"Wire {request.key} must have a positive occupied area")
        if request.start.distance_to(request.end) <= 1e-9:
            raise NoRouteError(
                f"Wire {request.key} endpoints coincide; place its terminals first"
            )

        eligible = [
            corridor
            for corridor in self.corridors
            if corridor.allows(request.section_mm2)
            and self._has_capacity(corridor, request.occupancy)
        ]
        if not eligible:
            raise NoRouteError(f"No corridor accepts wire {request.key}")

        nodes: dict[str, Point3] = {
            "start": request.start,
            "goal": request.end,
        }
        adjacency: dict[str, list[_Edge]] = {"start": [], "goal": []}
        for corridor in eligible:
            node = f"corridor:{corridor.key}"
            nodes[node] = corridor.box.center
            adjacency[node] = []

        for index, left in enumerate(eligible):
            for right_index, right in enumerate(eligible[index + 1:], start=index + 1):
                intersection = left.box.intersection(right.box)
                if intersection is None:
                    continue
                portal_node = f"portal:{index}:{right_index}"
                portal = intersection.center
                nodes[portal_node] = portal
                adjacency[portal_node] = []
                self._add_edge(
                    adjacency,
                    nodes,
                    f"corridor:{left.key}",
                    portal_node,
                    left.key,
                )
                self._add_edge(
                    adjacency,
                    nodes,
                    portal_node,
                    f"corridor:{right.key}",
                    right.key,
                )

        start_corridors = _endpoint_corridors(request.start, eligible)
        end_corridors = _endpoint_corridors(request.end, eligible)
        for corridor in start_corridors:
            corridor_node = f"corridor:{corridor.key}"
            start_entry = corridor.box.clamp(request.start)
            self._add_polyline_edge(
                adjacency,
                "start",
                corridor_node,
                (request.start, start_entry, corridor.box.center),
                corridor.key,
            )
        for corridor in end_corridors:
            corridor_node = f"corridor:{corridor.key}"
            end_entry = corridor.box.clamp(request.end)
            self._add_polyline_edge(
                adjacency,
                corridor_node,
                "goal",
                (corridor.box.center, end_entry, request.end),
                corridor.key,
            )

        path_nodes, edge_points = self._a_star(nodes, adjacency)
        used_corridors = tuple(
            dict.fromkeys(
                node.split(":", 1)[1]
                for node in path_nodes
                if node.startswith("corridor:")
            )
        )
        if not used_corridors:
            raise NoRouteError(f"No connected corridor path for wire {request.key}")
        for key in used_corridors:
            self._usage[key] += request.occupancy

        points = _simplify_polyline(edge_points)
        length = sum(
            left.distance_to(right) for left, right in zip(points, points[1:])
        )
        return RouteResult(
            wire_key=request.key,
            points=points,
            corridor_keys=used_corridors,
            geometric_length=length,
        )

    def usage_mm2(self, corridor_key: str) -> float:
        return self._usage[corridor_key]

    def _has_capacity(self, corridor: CorridorSpec, occupancy: float) -> bool:
        if corridor.capacity_mm2 <= 0:
            return True
        allowed = corridor.capacity_mm2 * min(max(corridor.max_fill_ratio, 0.0), 1.0)
        return self._usage[corridor.key] + occupancy <= allowed + 1e-9

    def _edge_multiplier(self, corridor_key: str) -> float:
        corridor = self._corridor_by_key[corridor_key]
        if corridor.capacity_mm2 <= 0:
            return 1.0
        fill = self._usage[corridor_key] / corridor.capacity_mm2
        return 1.0 + self.congestion_weight * max(fill, 0.0)

    def _add_edge(
        self,
        adjacency: dict[str, list[_Edge]],
        nodes: dict[str, Point3],
        left: str,
        right: str,
        corridor_key: str,
    ) -> None:
        self._add_polyline_edge(
            adjacency,
            left,
            right,
            (nodes[left], nodes[right]),
            corridor_key,
        )

    def _add_polyline_edge(
        self,
        adjacency: dict[str, list[_Edge]],
        left: str,
        right: str,
        points: tuple[Point3, ...],
        corridor_key: str,
    ) -> None:
        normalized = _deduplicate_points(points)
        raw_cost = sum(
            start.distance_to(end) for start, end in zip(normalized, normalized[1:])
        )
        cost = raw_cost * self._edge_multiplier(corridor_key)
        adjacency[left].append(_Edge(right, cost, normalized, corridor_key))
        adjacency[right].append(
            _Edge(left, cost, tuple(reversed(normalized)), corridor_key)
        )

    @staticmethod
    def _a_star(
        nodes: dict[str, Point3],
        adjacency: dict[str, list[_Edge]],
    ) -> tuple[tuple[str, ...], tuple[Point3, ...]]:
        sequence = count()
        queue: list[tuple[float, int, str]] = [(0.0, next(sequence), "start")]
        distance = {"start": 0.0}
        previous: dict[str, tuple[str, _Edge]] = {}
        while queue:
            _priority, _sequence, node = heapq.heappop(queue)
            if node == "goal":
                break
            current_distance = distance[node]
            for edge in adjacency[node]:
                tentative = current_distance + edge.cost
                if tentative >= distance.get(edge.target, math.inf):
                    continue
                distance[edge.target] = tentative
                previous[edge.target] = (node, edge)
                heuristic = nodes[edge.target].distance_to(nodes["goal"])
                heapq.heappush(
                    queue,
                    (tentative + heuristic, next(sequence), edge.target),
                )
        if "goal" not in previous:
            raise NoRouteError("No connected corridor path between endpoints")

        reversed_edges: list[_Edge] = []
        reversed_nodes = ["goal"]
        node = "goal"
        while node != "start":
            parent, edge = previous[node]
            reversed_edges.append(edge)
            reversed_nodes.append(parent)
            node = parent
        path_nodes = tuple(reversed(reversed_nodes))
        edges = list(reversed(reversed_edges))
        points: list[Point3] = [nodes["start"]]
        for edge in edges:
            points.extend(edge.points[1:])
        return path_nodes, tuple(points)


def _deduplicate_points(points: tuple[Point3, ...]) -> tuple[Point3, ...]:
    result: list[Point3] = []
    for point in points:
        if not result or point.distance_to(result[-1]) > 1e-9:
            result.append(point)
    return tuple(result)


def _endpoint_corridors(
    point: Point3,
    corridors: list[CorridorSpec],
    *,
    tolerance: float = 1e-9,
) -> list[CorridorSpec]:
    containing = [corridor for corridor in corridors if corridor.box.contains(point)]
    if containing:
        return containing

    # Choose the nearest entry in every connected component. This keeps
    # lead-ins local within a usable network while avoiding a false failure
    # when the globally nearest corridor belongs to an isolated component.
    remaining = set(range(len(corridors)))
    components: list[list[int]] = []
    while remaining:
        seed = min(remaining)
        remaining.remove(seed)
        component = [seed]
        pending = [seed]
        while pending:
            current = pending.pop()
            neighbors = sorted(
                candidate
                for candidate in tuple(remaining)
                if corridors[current].box.intersection(corridors[candidate].box)
                is not None
            )
            for candidate in neighbors:
                remaining.remove(candidate)
                component.append(candidate)
                pending.append(candidate)
        components.append(component)

    selected: list[CorridorSpec] = []
    for component in components:
        distances = [
            (
                point.distance_to(corridors[index].box.clamp(point)),
                corridors[index],
            )
            for index in component
        ]
        nearest = min(distance for distance, _corridor in distances)
        selected.extend(
            corridor
            for distance, corridor in distances
            if abs(distance - nearest) <= tolerance
        )
    return selected


def _simplify_polyline(points: tuple[Point3, ...]) -> tuple[Point3, ...]:
    points = _deduplicate_points(points)
    if len(points) < 3:
        return points
    result = [points[0]]
    for middle, end in zip(points[1:-1], points[2:]):
        start = result[-1]
        first = (middle.x - start.x, middle.y - start.y, middle.z - start.z)
        second = (end.x - middle.x, end.y - middle.y, end.z - middle.z)
        cross = (
            first[1] * second[2] - first[2] * second[1],
            first[2] * second[0] - first[0] * second[2],
            first[0] * second[1] - first[1] * second[0],
        )
        cross_length = math.sqrt(sum(value * value for value in cross))
        dot = sum(left * right for left, right in zip(first, second))
        if cross_length <= 1e-9 and dot >= 0:
            continue
        result.append(middle)
    result.append(points[-1])
    return tuple(result)


def _collapse_tolerance_gap(minimum: float, maximum: float) -> tuple[float, float]:
    if minimum <= maximum:
        return minimum, maximum
    midpoint = (minimum + maximum) / 2
    return midpoint, midpoint
