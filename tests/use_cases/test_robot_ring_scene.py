#!/usr/bin/env python3
"""Dependency-free checks for the ring scene and scenario.

Nothing here needs Sionna: the scenario contract, the generated scene XML
and the native multi-UE renderer's shape check are all plain Python.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "sionna_bridge"))

from run_bridge import (  # noqa: E402
    load_scenario_config,
    resolve_scene,
    scene_geometry,
)

SCENARIO = PROJECT_ROOT / "use_cases" / "configs" / "sionna" / "scenarios" / "robot_ring" / "robot-ring.json"
SCENE_DIR = PROJECT_ROOT / "use_cases" / "configs" / "sionna" / "scenes" / "robot_ring"
RENDERER = PROJECT_ROOT / "scripts" / "native" / "render-sionna-multi-ue-configs.py"


def load_module(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class RobotRingScenarioTest(unittest.TestCase):
    def test_scenario_links_and_nodes(self):
        definition = load_scenario_config(SCENARIO)
        self.assertEqual(definition.scene, "robot_ring")
        self.assertEqual(tuple(definition.nodes), ("gnb0", "ue0", "ue1"))
        links = {(l.source, l.destination, l.direction) for l in definition.links}
        self.assertEqual(links, {
            ("gnb0", "ue0", "downlink"), ("gnb0", "ue1", "downlink"),
            ("ue0", "gnb0", "uplink"), ("ue1", "gnb0", "uplink"),
        })
        # Transmission through the pillars is what keeps a shadowed UE at
        # roughly -25 dB instead of outage, so it must stay on.
        self.assertTrue(definition.solver["refraction"])
        # No UE<->UE edge: the ring cell is FDD band 3, where one robot's uplink
        # carrier is never heard by the other's downlink receiver.
        self.assertFalse(any(l.direction == "crosstalk" for l in definition.links))
        crosstalk = load_scenario_config(SCENARIO.with_name("robot-ring-crosstalk.json"))
        self.assertEqual(
            {(l.source, l.destination, l.direction) for l in crosstalk.links},
            links | {("ue0", "ue1", "crosstalk"), ("ue1", "ue0", "crosstalk")},
        )
        for node_id in ("ue0", "ue1"):
            motion = definition.nodes[node_id].motion
            self.assertEqual(motion.route_mode, "pingpong")
            self.assertGreater(motion.speed_mps, 0.0)
            # Robots stay inside the 4 m ring at antenna height.
            for x, y, z in motion.waypoints:
                self.assertLess((x * x + y * y) ** 0.5, 4.0)
                self.assertAlmostEqual(z, 0.4)
        gnb = definition.nodes["gnb0"].motion
        self.assertEqual(gnb.start, (15.0, 0.0, 8.0))
        self.assertEqual(definition.solver["max_depth"], 3)

    def test_scene_resolves_to_generated_xml(self):
        class NoBuiltins:
            scene = object()

        resolved = resolve_scene("robot_ring", NoBuiltins)
        self.assertEqual(resolved, (SCENE_DIR / "scene.xml").resolve())

    def test_scene_geometry(self):
        geometry = scene_geometry(SCENE_DIR / "scene.xml")
        objects = {item["id"]: item for item in geometry["objects"]}
        self.assertEqual(
            set(objects),
            {"ground", "fence", "building_pillar_0", "building_pillar_1",
             "building_pillar_2", "building_mast"},
        )
        for index in range(3):
            pillar = objects[f"building_pillar_{index}"]
            self.assertEqual(pillar["kind"], "building")
            self.assertEqual(pillar["material"], "concrete")
            self.assertAlmostEqual(pillar["z_max_m"], 3.0, places=5)
            xs = [p[0] for p in pillar["footprint_xy_m"]]
            # Pillars straddle x = 0: the gNB (at +x) sees them edge-on.
            self.assertLess(min(xs), 0.0)
            self.assertGreater(max(xs), 0.0)
        mast = objects["building_mast"]
        self.assertAlmostEqual(mast["z_max_m"], 7.5, places=5)  # antenna at 8.0 is clear of it
        self.assertEqual(mast["material"], "metal")
        fence = objects["fence"]
        self.assertAlmostEqual(fence["z_max_m"], 0.3, places=5)
        self.assertLess(fence["z_max_m"], 0.4)  # below the robots' antennas
        manifest = json.loads((SCENE_DIR / "manifest.json").read_text())
        self.assertEqual(manifest["mast"]["gnb_antenna_m"], [15.0, 0.0, 8.0])

    def test_generator_is_deterministic(self):
        generator = SCENE_DIR / "build_robot_ring.py"
        with tempfile.TemporaryDirectory() as workspace:
            copy = pathlib.Path(workspace) / "build_robot_ring.py"
            copy.write_text(generator.read_text())
            module = load_module(copy, "build_robot_ring_copy")
            module.main()
            for mesh in sorted((SCENE_DIR / "meshes").glob("*.ply")):
                self.assertEqual(
                    (pathlib.Path(workspace) / "meshes" / mesh.name).read_bytes(),
                    mesh.read_bytes(), mesh.name,
                )
            self.assertEqual(
                (pathlib.Path(workspace) / "scene.xml").read_text(),
                (SCENE_DIR / "scene.xml").read_text(),
            )

    def test_multi_ue_renderer_accepts_shape(self):
        renderer = load_module(RENDERER, "render_sionna_multi_ue_configs_for_test")
        # The renderer checks the scenario against the gate's UE-count slice
        # (two by default), not against every record the legacy table knows.
        shape = renderer.load_live_shape(SCENARIO)
        self.assertEqual(len(shape.nodes), 3)
        self.assertEqual(
            {(link.source, link.destination) for link in shape.links},
            {("gnb0", "ue0"), ("gnb0", "ue1"), ("ue0", "gnb0"), ("ue1", "gnb0")},
        )
        crosstalk = renderer.load_live_shape(SCENARIO.with_name("robot-ring-crosstalk.json"))
        self.assertEqual(len(crosstalk.links), 6)


if __name__ == "__main__":
    unittest.main()
