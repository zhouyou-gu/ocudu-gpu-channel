"""render-sionna-multi-ue-configs.py: UE-count slice and receiver noise floor."""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys
import unittest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
RENDERER = PROJECT_ROOT / "scripts" / "native" / "render-sionna-multi-ue-configs.py"
RING = PROJECT_ROOT / "use_cases" / "configs" / "sionna" / "scenarios" / "robot_ring" / "robot-ring.json"
SUTD = PROJECT_ROOT / "use_cases" / "configs" / "sionna" / "scenarios" / "sutd" / "sionna-multi-ue-sutd.json"


def load_renderer():
    spec = importlib.util.spec_from_file_location("render_sionna_multi_ue_for_test", RENDERER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve the module by name
    spec.loader.exec_module(module)
    return module


class RendererTests(unittest.TestCase):
    def test_self_test_passes(self):
        result = subprocess.run([sys.executable, str(RENDERER), "--self-test"],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("result=pass", result.stdout)

    def test_two_ue_scenarios_are_accepted_against_the_default_slice(self):
        renderer = load_renderer()
        for scenario in (RING, SUTD):
            shape = renderer.load_live_shape(scenario)
            self.assertEqual([ue.node_id for ue in shape.ues], ["ue0", "ue1"], scenario.name)
            with self.assertRaises(ValueError) as refused:
                renderer.load_live_shape(scenario, 4)
            self.assertIn("ue0, ue1, ue2, ue3", str(refused.exception))

    def test_noise_floor_follows_reference_snr_and_transmit_power(self):
        renderer = load_renderer()
        shape = renderer.load_live_shape(RING)
        floors = renderer.rx_noise_powers(shape, 26.6, tx_power_dl=2.0e-3, tx_power_ul=5.0e-4)
        self.assertEqual(set(floors), {"gnb0", "ue0", "ue1"})
        self.assertAlmostEqual(floors["ue0"], 2.0e-3 / 10 ** 2.66, places=12)
        self.assertAlmostEqual(floors["gnb0"], 5.0e-4 / 10 ** 2.66, places=12)
        topology = renderer.render_topology(shape, floors)
        self.assertEqual(topology.count("rx_model: rx_noise_"), 3)
        self.assertEqual(topology.count("type: awgn"), 3)
        self.assertNotIn("snr_db", topology, "the floor must be absolute, not signal-relative")
        with self.assertRaises(ValueError):
            renderer.rx_noise_powers(shape, 26.6, tx_power_dl=0.0)
        with self.assertRaises(ValueError):
            renderer.rx_noise_powers(shape, float("nan"))


if __name__ == "__main__":
    unittest.main()
