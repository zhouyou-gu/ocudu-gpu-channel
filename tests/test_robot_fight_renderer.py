"""render-robot-fight-configs.py: two cells, two brokers, one scene (R5a)."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import unittest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
RENDERER = PROJECT_ROOT / "use_cases" / "robot_fight" / "render-robot-fight-configs.py"
FIGHT = PROJECT_ROOT / "use_cases" / "configs" / "sionna" / "scenarios" / "robot_ring" / "robot-ring-fight.json"
WALK = PROJECT_ROOT / "use_cases" / "configs" / "sionna" / "scenarios" / "robot_ring" / "robot-ring-walk.json"


def load_renderer():
    spec = importlib.util.spec_from_file_location("render_robot_fight_for_test", RENDERER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RendererTests(unittest.TestCase):
    def test_self_test_passes(self):
        result = subprocess.run([sys.executable, str(RENDERER), "--self-test"],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("result=pass", result.stdout)

    def test_fight_scenario_splits_into_two_identical_cells(self):
        renderer = load_renderer()
        root = json.loads(FIGHT.read_text(encoding="utf-8"))
        renderer.check_scenario(root)
        cells = [renderer.split_scenario(root, cell) for cell in renderer.CELLS]
        self.assertEqual(sorted(cells[0]["nodes"]), ["gnb0", "ue0"])
        self.assertEqual(sorted(cells[1]["nodes"]), ["gnb1", "ue1"])
        # gnb1 stands on gnb0's mast: the two links are the same physics.
        self.assertEqual(cells[0]["nodes"]["gnb0"]["start_m"], cells[1]["nodes"]["gnb1"]["start_m"])
        self.assertEqual(cells[0]["scene"], "robot_ring")
        self.assertEqual(cells[0]["solver"], root["solver"])
        for cell, scenario in zip(renderer.CELLS, cells):
            shape = renderer.cell_shape(cell, scenario)
            self.assertEqual(len(shape.links), 2)
            floors = renderer.sionna.rx_noise_powers(shape, 40.0)
            topology = renderer.render_cell_topology(dict(cell, cuda_stream_priority="high"), shape, floors)
            self.assertIn("  cuda_stream_priority: high\n", topology)
            self.assertIn(f"tcp://127.0.0.1:{cell['gnb']['tx_port']}\n", topology)
            self.assertIn(f"tcp://127.0.0.1:{cell['ue']['rx_port']}\n", topology)
            self.assertEqual(topology.count("type: awgn"), 2)

    def test_single_cell_scenario_is_refused(self):
        renderer = load_renderer()
        root = json.loads(WALK.read_text(encoding="utf-8"))
        with self.assertRaises(SystemExit):
            renderer.check_scenario(root)


if __name__ == "__main__":
    unittest.main()
