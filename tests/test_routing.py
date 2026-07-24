# SPDX-License-Identifier: LGPL-2.1-or-later

from __future__ import annotations

import unittest

from freecad.QetRouting.routing import (
    Box3,
    CorridorRouter,
    CorridorSpec,
    NoRouteError,
    Point3,
    RoutingError,
    WireRequest,
    axis_aligned_box_from_points,
)


class CorridorRouterTests(unittest.TestCase):
    def test_axis_aligned_box_spans_selected_corner_points(self) -> None:
        box = axis_aligned_box_from_points(
            [
                Point3(30, -5, 12),
                Point3(10, 7, 2),
                Point3(30, 7, 2),
                Point3(10, -5, 12),
                Point3(10, -5, 2),
                Point3(30, 7, 12),
                Point3(30, -5, 2),
                Point3(10, 7, 12),
            ]
        )

        self.assertEqual(box.minimum, Point3(10, -5, 2))
        self.assertEqual(box.maximum, Point3(30, 7, 12))

    def test_axis_aligned_box_rejects_flat_selection(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-zero distance"):
            axis_aligned_box_from_points(
                [Point3(0, 0, 5), Point3(10, 20, 5)]
            )

    def test_axis_aligned_box_rejects_rotated_corner_set(self) -> None:
        with self.assertRaisesRegex(ValueError, "axis-aligned box"):
            axis_aligned_box_from_points(
                [
                    Point3(0, 1, 0),
                    Point3(1, 0, 0),
                    Point3(2, 1, 0),
                    Point3(1, 2, 0),
                    Point3(0, 1, 5),
                    Point3(1, 0, 5),
                    Point3(2, 1, 5),
                    Point3(1, 2, 5),
                ]
            )

    def test_routes_through_overlapping_corridors(self) -> None:
        router = CorridorRouter(
            [
                CorridorSpec(
                    "left",
                    Box3(Point3(0, 0, 0), Point3(60, 20, 20)),
                ),
                CorridorSpec(
                    "right",
                    Box3(Point3(50, 0, 0), Point3(120, 20, 20)),
                ),
            ]
        )
        result = router.route(
            WireRequest(
                "W1",
                Point3(-10, 10, 10),
                Point3(130, 10, 10),
                1.5,
            )
        )

        self.assertEqual(result.corridor_keys, ("left", "right"))
        self.assertEqual(result.points[0], Point3(-10, 10, 10))
        self.assertEqual(result.points[-1], Point3(130, 10, 10))
        self.assertAlmostEqual(result.geometric_length, 140.0)

    def test_external_terminals_use_nearest_corridor_surface_points(self) -> None:
        corridor = CorridorSpec(
            "tray",
            Box3(Point3(0, 0, 0), Point3(10, 10, 10)),
        )
        start = Point3(-5, 2, 3)
        end = Point3(16, 8, 7)
        result = CorridorRouter([corridor]).route(
            WireRequest("W1", start, end, 1.5)
        )

        self.assertEqual(result.points[0], start)
        self.assertEqual(result.points[1], Point3(0, 2, 3))
        self.assertEqual(result.points[-2], Point3(10, 8, 7))
        self.assertEqual(result.points[-1], end)
        expected = sum(
            left.distance_to(right)
            for left, right in zip(result.points, result.points[1:])
        )
        self.assertAlmostEqual(result.geometric_length, expected)

    def test_disconnected_corridors_fail(self) -> None:
        router = CorridorRouter(
            [
                CorridorSpec("left", Box3(Point3(0, 0, 0), Point3(10, 10, 10))),
                CorridorSpec("right", Box3(Point3(20, 0, 0), Point3(30, 10, 10))),
            ]
        )
        with self.assertRaises(NoRouteError):
            router.route(WireRequest("W1", Point3(0, 5, 5), Point3(30, 5, 5), 1.5))

    def test_section_filter_is_enforced(self) -> None:
        router = CorridorRouter(
            [
                CorridorSpec(
                    "only-2.5",
                    Box3(Point3(0, 0, 0), Point3(10, 10, 10)),
                    allowed_sections_mm2=(2.5,),
                )
            ]
        )
        with self.assertRaises(NoRouteError):
            router.route(WireRequest("W1", Point3(0, 0, 0), Point3(10, 0, 0), 1.5))

    def test_capacity_is_consumed_between_routes(self) -> None:
        router = CorridorRouter(
            [
                CorridorSpec(
                    "limited",
                    Box3(Point3(0, 0, 0), Point3(10, 10, 10)),
                    capacity_mm2=2.0,
                    max_fill_ratio=1.0,
                )
            ]
        )
        router.route(WireRequest("W1", Point3(0, 5, 5), Point3(10, 5, 5), 1.5))
        with self.assertRaises(NoRouteError):
            router.route(WireRequest("W2", Point3(0, 5, 5), Point3(10, 5, 5), 1.5))

    def test_coincident_unplaced_terminals_fail_with_actionable_message(self) -> None:
        router = CorridorRouter(
            [CorridorSpec("box", Box3(Point3(0, 0, 0), Point3(10, 10, 10)))]
        )

        with self.assertRaisesRegex(NoRouteError, "place its terminals"):
            router.route(WireRequest("W1", Point3(5, 5, 5), Point3(5, 5, 5), 1.5))

    def test_non_positive_occupancy_is_rejected(self) -> None:
        router = CorridorRouter(
            [CorridorSpec("box", Box3(Point3(0, 0, 0), Point3(10, 10, 10)))]
        )

        with self.assertRaisesRegex(RoutingError, "positive occupied area"):
            router.route(
                WireRequest(
                    "W1",
                    Point3(0, 5, 5),
                    Point3(10, 5, 5),
                    1.5,
                    occupancy_area_mm2=-1,
                )
            )

    def test_near_touching_floating_point_boxes_form_a_portal(self) -> None:
        left = Box3(Point3(0, 0, 0), Point3(0.3, 10, 10))
        right = Box3(Point3(0.1 + 0.2, 0, 0), Point3(1, 10, 10))

        intersection = left.intersection(right)

        self.assertIsNotNone(intersection)
        self.assertAlmostEqual(intersection.minimum.x, intersection.maximum.x)

    def test_farther_connected_corridor_can_beat_isolated_nearest_corridor(self) -> None:
        router = CorridorRouter(
            [
                CorridorSpec(
                    "isolated-near-start",
                    Box3(Point3(0, 0, 0), Point3(10, 10, 10)),
                ),
                CorridorSpec(
                    "connected",
                    Box3(Point3(20, 0, 0), Point3(60, 10, 10)),
                ),
            ]
        )

        result = router.route(
            WireRequest("W1", Point3(-1, 5, 5), Point3(55, 5, 5), 1.5)
        )

        self.assertEqual(result.corridor_keys, ("connected",))


if __name__ == "__main__":
    unittest.main()
