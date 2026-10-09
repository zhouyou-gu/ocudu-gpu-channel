#!/usr/bin/env python3
"""Dependency-free tests for the OSM scene builder's pedestrian routes."""

from __future__ import annotations

import json
import math
import pathlib
import sys
import unittest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "sionna_bridge"))

from build_osm_scene import (  # noqa: E402
    outward_ring,
    point_in_ring,
    segments_cross,
    walk_enters_ring,
)

SUTD_MANIFEST = (
    PROJECT_ROOT / "use_cases" / "configs" / "sionna" / "scenes" / "sionna_SUTD_test" / "manifest.json"
)
SUTD_SCENARIOS = (
    "multi-gnb-sutd.json",
    "sionna-multi-ue-sutd.json",
    "sionna_SUTD_test.json",
)


def square(x: float, y: float, side: float) -> list[tuple[float, float]]:
    return [(x, y), (x + side, y), (x + side, y + side), (x, y + side)]


class SegmentTests(unittest.TestCase):
    def test_crossing_touching_and_missing_segments(self) -> None:
        self.assertTrue(segments_cross((0.0, 0.0), (2.0, 2.0), (0.0, 2.0), (2.0, 0.0)))
        # A walk that only grazes a wall still runs into it once extruded.
        self.assertTrue(segments_cross((0.0, 0.0), (2.0, 0.0), (1.0, 0.0), (1.0, 5.0)))
        self.assertFalse(segments_cross((0.0, 0.0), (2.0, 0.0), (0.0, 1.0), (2.0, 1.0)))


class OutwardRingTests(unittest.TestCase):
    def test_ring_clears_its_own_footprint_by_the_margin(self) -> None:
        ring = square(0.0, 0.0, 20.0)
        walk = [(p[0], p[1]) for p in outward_ring(ring, 12.0, 1.5)]
        self.assertGreaterEqual(len(walk), 3)
        for point in walk:
            self.assertFalse(point_in_ring(point, ring))
            self.assertGreaterEqual(
                min(math.dist(point, corner) for corner in ring), 12.0 - 1e-6
            )

    def test_a_neighbour_in_the_way_is_walked_around_not_through(self) -> None:
        # The neighbour sits where the 12 m offset ring wants to go, which is
        # the case that used to exile one corner past it and drag both of its
        # legs through the neighbour's footprint.
        ring = square(0.0, 0.0, 20.0)
        neighbour = square(24.0, 0.0, 20.0)
        walk = [(p[0], p[1]) for p in outward_ring(ring, 12.0, 1.5, [neighbour])]
        self.assertFalse(walk_enters_ring(walk, neighbour))
        self.assertFalse(walk_enters_ring(walk, ring))
        # Rounding the pair is the point: the walk has to reach past it.
        self.assertGreater(max(x for x, _ in walk), 44.0)

    def test_a_neighbour_merely_encircled_is_left_alone(self) -> None:
        # Absorbing every footprint in sight would swallow the campus; only a
        # neighbour the walk actually runs into joins the hull.
        ring = square(0.0, 0.0, 60.0)
        far = square(200.0, 200.0, 10.0)
        walk = [(p[0], p[1]) for p in outward_ring(ring, 12.0, 1.5, [far])]
        self.assertLess(max(x for x, _ in walk), 100.0)

    def test_every_sutd_walk_ring_stays_out_of_every_footprint(self) -> None:
        manifest = json.loads(SUTD_MANIFEST.read_text())
        footprints = {
            entry["id"]: [(x, y) for x, y in entry["footprint_m"]]
            for entry in manifest["buildings"]
        }
        for entry in manifest["buildings"]:
            walk = [(p[0], p[1]) for p in entry.get("walk_ring_m", [])]
            if len(walk) < 3:
                continue
            for other_id, footprint in footprints.items():
                with self.subTest(walk=entry["id"], footprint=other_id):
                    self.assertFalse(walk_enters_ring(walk, footprint))

    def test_sutd_scenarios_route_their_pedestrian_outside_every_building(self) -> None:
        manifest = json.loads(SUTD_MANIFEST.read_text())
        footprints = [
            [(x, y) for x, y in entry["footprint_m"]] for entry in manifest["buildings"]
        ]
        scenarios = PROJECT_ROOT / "use_cases" / "configs" / "sionna" / "scenarios" / "sutd"
        for name in SUTD_SCENARIOS:
            config = json.loads((scenarios / name).read_text())
            for node_id, node in config["nodes"].items():
                route = [(p[0], p[1]) for p in node.get("route_m", [])]
                if len(route) < 2:
                    continue
                if node.get("route_mode") != "loop":
                    route = route + route[::-1]
                for index, footprint in enumerate(footprints):
                    with self.subTest(scenario=name, node=node_id, footprint=index):
                        self.assertFalse(walk_enters_ring(route, footprint))


if __name__ == "__main__":
    unittest.main()
