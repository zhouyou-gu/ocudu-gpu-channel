"""render-oai-two-cell-tdd-configs.py: two co-channel TDD n78 cells, per-cell TDD patterns, CLI slots (X5)."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import unittest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
RENDERER = PROJECT_ROOT / "scripts" / "native" / "render-oai-two-cell-tdd-configs.py"
GNB_FIXTURE = PROJECT_ROOT / "use_cases" / "configs" / "ran" / "ocudu" / "docker" / "gnb_zmq_b210_fdd_srsue.yaml"
SCENARIOS = {8: PROJECT_ROOT / "use_cases" / "configs" / "sionna" / "scenarios" / "robot_ring" / "two-cell-tdd-8.json",
             10: PROJECT_ROOT / "use_cases" / "configs" / "sionna" / "scenarios" / "robot_ring" / "two-cell-tdd-10.json",
             12: PROJECT_ROOT / "use_cases" / "configs" / "sionna" / "scenarios" / "robot_ring" / "two-cell-tdd-12.json"}


def load_renderer():
    spec = importlib.util.spec_from_file_location("render_oai_two_cell_tdd_for_test", RENDERER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RendererTests(unittest.TestCase):
    def test_self_test_passes(self):
        result = subprocess.run([sys.executable, str(RENDERER), "--self-test"], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("result=pass", result.stdout)

    def test_pattern_knob_same_and_different(self):
        r = load_renderer()
        same = r.parse_patterns("same")
        self.assertEqual(same[0], same[1])
        self.assertEqual(same[0].config(), r.oai_mue.TDD_PATTERN, "'same' is X3's 7D/1S/2U cell on both sides")
        a, b = r.parse_patterns("7D2U;3D6U")
        self.assertEqual(a.slot_kinds(), "DDDDDDDSUU")
        self.assertEqual(b.slot_kinds(), "DDDSUUUUUU")
        self.assertEqual(b.config(), {"dl_ul_tx_period": 10, "nof_dl_slots": 3, "nof_dl_symbols": 6,
                                      "nof_ul_slots": 6, "nof_ul_symbols": 4})
        # Cell B's UE transmits in slots 3(S tail)..9; cell A's UE receives in 0..6: CLI in 3, 4, 5, 6.
        self.assertEqual(r.cli_overlap_slots(a, b), [3, 4, 5, 6])
        self.assertEqual(r.cli_overlap_slots(b, a), [], "cell A's UL (8, 9) never meets cell B's DL (0..2)")
        self.assertEqual(r.cli_overlap_slots(a, a), [])
        self.assertEqual(r.cli_overlap_slots(a, r.parse_pattern("2D7U")), [2, 3, 4, 5, 6])
        for bad in ("7D3U", "7D2U;3D6U;2D7U", "0D9U", "7d2u", ""):
            with self.assertRaises(ValueError, msg=bad):
                r.parse_patterns(bad)

    def test_two_cells_differ_only_in_identity_ports_and_pattern(self):
        r = load_renderer()
        source = GNB_FIXTURE.read_text(encoding="utf-8")
        a, b = r.parse_patterns("7D2U;3D6U")
        log_dir = pathlib.Path("/tmp/logs")
        cell_a = r.render_cell(source, r.CELLS[0], a, log_dir)
        cell_b = r.render_cell(source, r.CELLS[1], b, log_dir)
        differing = [(x, y) for x, y in zip(cell_a.splitlines(), cell_b.splitlines()) if x != y]
        self.assertEqual(len(cell_a.splitlines()), len(cell_b.splitlines()))
        self.assertEqual([x.strip().split(":")[0] for x, _ in differing],
                         ["bind_addrs", "- bind_addr", "device_args", "pci", "nof_dl_slots", "nof_ul_slots",
                          "prach_root_sequence_index", "filename", "mac_filename", "ngap_filename", "gnb_id", "ran_node_name"])
        for text in (cell_a, cell_b):
            for token in ("  band: 78\n", "  dl_arfcn: 632628\n", "  common_scs: 30\n", "  channel_bandwidth_MHz: 20\n",
                          "    prach_config_index: 159\n", "    dl_ul_tx_period: 10\n", "    nof_dl_symbols: 6\n",
                          "    nof_ul_symbols: 4\n", "  autostart_stdout_metrics: true\n", "  srate: 23.04\n"):
                self.assertIn(token, text)
            self.assertNotIn("pdcch", text)
        self.assertIn("    bind_addrs: 127.0.0.11\n", cell_a)
        self.assertIn("    bind_addrs: 127.0.0.12\n", cell_b)
        self.assertIn("tx_port=tcp://127.0.0.1:2010,rx_port=tcp://127.0.0.1:2011,", cell_b)
        # Config index 159 is format B4 (L = 139): roots live in 0..137.
        for cell in r.CELLS:
            self.assertTrue(0 <= cell["prach_root"] < 138, cell)
        self.assertNotEqual(r.CELLS[0]["prach_root"], r.CELLS[1]["prach_root"])
        self.assertNotIn("metrics:", r.render_cell(source, r.CELLS[0], a, log_dir, stdout_metrics=False))
        with self.assertRaises(SystemExit):
            r.render_cell(source, {**r.CELLS[1], "prach_root": 200}, b, log_dir)

    def test_scenarios_render_topologies_with_tdd_labels_and_scale(self):
        r = load_renderer()
        scale = r.sionna.ue_tx_scale_db(r.TX_POWER_DL, r.OAI_UE_TX_POWER)
        self.assertAlmostEqual(scale, 22.810, places=3, msg="OAI nrUE constant 1.17e-5 against the gNB's 1.12e-2")
        for count, path in SCENARIOS.items():
            shape = r.load_two_cell_shape(path)
            self.assertEqual(len(shape.links), count, path.name)
            floors = r.rx_noise_powers(shape, 40.0, r.TX_POWER_DL, r.OAI_UE_TX_POWER, scale)
            self.assertEqual(floors["gnb0"], floors["gnb1"])
            self.assertAlmostEqual(floors["ue0"], r.TX_POWER_DL / 1e4, places=15)
            topology = r.render_topology(shape, floors, scale)
            self.assertEqual(topology.count("    carrier: n78\n"), 4)
            self.assertNotIn("tx_carrier", topology)
            self.assertEqual(topology.count("  - from: "), count)
            self.assertEqual(topology.count("    tx_scale_db: 22.810\n"), 2)
            self.assertEqual(topology.count("type: awgn"), 4)
            for token in ("    tx_endpoint: tcp://127.0.0.1:2000\n", "    tx_endpoint: tcp://127.0.0.1:2010\n",
                          "    rx_endpoint: tcp://127.0.0.1:2011\n", "    tx_endpoint: tcp://10.201.0.2:2101\n",
                          "    rx_endpoint: tcp://10.201.1.1:2102\n"):
                self.assertIn(token, topology, path.name)
            scenario = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(scenario["scene"], "robot_ring")
            self.assertEqual(scenario["nodes"]["gnb0"]["start_m"], [-12.0, 6.0, 8.0])
            self.assertEqual(scenario["nodes"]["ue1"]["start_m"], [2.0, 0.8, 0.4])
            self.assertTrue(all(l["model"] == "sionna_rt" for l in scenario["links"]))
        ten = json.loads(SCENARIOS[10].read_text(encoding="utf-8"))
        crosstalk = sorted((l["from"], l["to"]) for l in ten["links"] if l["direction"] == "crosstalk")
        self.assertEqual(crosstalk, [("ue0", "ue1"), ("ue1", "ue0")])
        eight = json.loads(SCENARIOS[8].read_text(encoding="utf-8"))
        self.assertEqual([l for l in eight["links"] if l["direction"] == "crosstalk"], [])
        twelve = json.loads(SCENARIOS[12].read_text(encoding="utf-8"))
        self.assertIn(("gnb0", "gnb1"), [(l["from"], l["to"]) for l in twelve["links"]])

    def test_shape_loader_refuses_missing_serving_and_wrong_nodes(self):
        import tempfile
        r = load_renderer()
        scenario = json.loads(SCENARIOS[8].read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "s.json"
            broken = dict(scenario, links=[l for l in scenario["links"] if (l["from"], l["to"]) != ("ue1", "gnb1")])
            path.write_text(json.dumps(broken), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing the ue1->gnb1 link"):
                r.load_two_cell_shape(path)
            three = dict(scenario, nodes={k: v for k, v in scenario["nodes"].items() if k != "gnb1"})
            path.write_text(json.dumps(three), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "exactly the nodes"):
                r.load_two_cell_shape(path)
            array = json.loads(json.dumps(scenario))
            array["nodes"]["gnb0"]["array"]["cols"] = 2
            path.write_text(json.dumps(array), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "single-port"):
                r.load_two_cell_shape(path)


if __name__ == "__main__":
    unittest.main()
