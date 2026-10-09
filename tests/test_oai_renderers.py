"""render-oai-1x1-configs.py / render-oai-2x2-configs.py: carrier labels and the OAI UE uplink scale."""

from __future__ import annotations

import argparse
import importlib.util
import math
import os
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
RENDERER_1X1 = PROJECT_ROOT / "scripts" / "native" / "render-oai-1x1-configs.py"
RENDERER_2X2 = PROJECT_ROOT / "scripts" / "native" / "render-oai-2x2-configs.py"
LEGACY_TOPOLOGY = PROJECT_ROOT / "use_cases" / "configs" / "topologies" / "ocudu_docker" / "topology.ocudu-docker.cuda.yaml"
TOPOLOGY_2X2 = PROJECT_ROOT / "use_cases" / "configs" / "topologies" / "ocudu_native" / "topology.ocudu.oai-2x2.cuda.yaml"


def load(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RendererTests(unittest.TestCase):
    def test_self_tests_pass(self):
        for renderer in (RENDERER_1X1, RENDERER_2X2):
            result = subprocess.run([sys.executable, str(renderer), "--self-test"],
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, renderer.name + "\n" + result.stdout + result.stderr)
            self.assertIn("result=pass", result.stdout, renderer.name)

    def test_ue_scale_puts_the_uplink_seven_db_below_the_gnb(self):
        oai = load(RENDERER_1X1, "render_oai_1x1_for_test")
        scale = oai.ue_tx_scale_db()
        self.assertAlmostEqual(scale, -7.0 - 10.0 * math.log10(oai.OAI_UE_TX_POWER / oai.TX_POWER_DL), places=9)
        # Scaled uplink power relative to the gNB's wire level is exactly the emitted-power offset.
        scaled_ul = oai.OAI_UE_TX_POWER * 10.0 ** (scale / 10.0)
        self.assertAlmostEqual(10.0 * math.log10(scaled_ul / oai.TX_POWER_DL), -7.0, places=9)
        self.assertEqual(oai.TX_POWER_DL, 1.12e-2, "gNB wire level shared with the Sionna renderer")
        with self.assertRaises(ValueError):
            oai.ue_tx_scale_db(tx_power_ul=0.0)

    def test_legacy_topology_gets_labels_and_scale_without_touching_the_channel(self):
        oai = load(RENDERER_1X1, "render_oai_1x1_for_test")
        source = LEGACY_TOPOLOGY.read_text(encoding="utf-8")
        plain = oai.render_topology_oai(source)
        scaled = oai.render_topology_oai(source, oai.ue_tx_scale_db())
        for rendered in (plain, scaled):
            self.assertEqual(rendered.count("tx_carrier: n3-dl"), 1)
            self.assertEqual(rendered.count("rx_carrier: n3-ul"), 1)
            self.assertEqual(rendered.count("tx_carrier: n3-ul"), 1)
            self.assertEqual(rendered.count("rx_carrier: n3-dl"), 1)
            self.assertLess(rendered.index("id: gnb0"), rendered.index("tx_carrier: n3-dl"))
            self.assertLess(rendered.index("id: ue0"), rendered.index("tx_carrier: n3-ul"))
            # The channel model is the legacy one, byte for byte.
            self.assertEqual(rendered[rendered.index("links:"):], source[source.index("links:"):])
        self.assertNotIn("tx_scale_db", plain)
        self.assertEqual(scaled.count("tx_scale_db:"), 1)
        self.assertGreater(scaled.index("tx_scale_db:"), scaled.index("id: ue0"))
        self.assertNotIn("awgn", scaled, "fixed TDL only: no absolute noise floor to re-derive")

    def test_2x2_fixture_gets_every_port_labelled_and_both_ue_ports_scaled(self):
        two = load(RENDERER_2X2, "render_oai_2x2_for_test")
        source = TOPOLOGY_2X2.read_text(encoding="utf-8")
        rendered = two.render_topology_2x2(source, -40.0)
        self.assertEqual(rendered.count("tx_carrier: n3-dl"), 2)
        self.assertEqual(rendered.count("tx_carrier: n3-ul"), 2)
        self.assertEqual(rendered.count("    tx_scale_db: -40.000\n"), 2)
        devices = rendered[rendered.index("devices:"):rendered.index("radio_nodes:")]
        entries = devices.split("  - id: ")[1:]
        self.assertEqual([entry.split("\n", 1)[0] for entry in entries], ["gnb0_p0", "gnb0_p1", "ue0_p0", "ue0_p1"])
        for entry in entries:
            self.assertEqual(entry.count("tx_carrier:"), 1, entry)
            self.assertEqual(entry.count("rx_carrier:"), 1, entry)
            self.assertEqual(entry.count("tx_scale_db:"), 1 if entry.startswith("ue0") else 0, entry)
        self.assertEqual(rendered[rendered.index("radio_nodes:"):], source[source.index("radio_nodes:"):])
        self.assertNotIn("tx_scale_db", two.render_topology_2x2(source))

    def test_2x2_default_scale_is_the_1x1_scale_on_both_ue_ports(self):
        # X7 measured the 2x2 UE's port 0 at the 1x1 per-channel wire level and
        # port 1 silent, so the renderers share OAI_UE_TX_POWER and the derived
        # +22.8 dB; the 2x2 parser must resolve the same default and keep the
        # env/CLI overrides of the shared argument layer.
        oai = load(RENDERER_1X1, "render_oai_1x1_for_test")
        two = load(RENDERER_2X2, "render_oai_2x2_for_test")
        parser = argparse.ArgumentParser()
        two.oai.add_tx_scale_arguments(parser)
        default = two.oai.resolve_ue_tx_scale_db(parser.parse_args([]))
        self.assertAlmostEqual(default, oai.ue_tx_scale_db(), places=9)
        self.assertAlmostEqual(default, 22.81, places=2)
        source = TOPOLOGY_2X2.read_text(encoding="utf-8")
        rendered = two.render_topology_2x2(source, default)
        self.assertEqual(rendered.count(f"    tx_scale_db: {default:.3f}\n"), 2)
        self.assertIsNone(two.oai.resolve_ue_tx_scale_db(parser.parse_args(["--ue-tx-scale-db", "off"])))
        self.assertAlmostEqual(
            two.oai.resolve_ue_tx_scale_db(parser.parse_args(["--tx-power-ul", str(oai.OAI_UE_TX_POWER * 10.0)])),
            default - 10.0, places=9)
        with mock.patch.dict(os.environ, {"OCUDU_NATIVE_OAI_UE_TX_SCALE_DB": "-3.5"}):
            env_parser = argparse.ArgumentParser()
            two.oai.add_tx_scale_arguments(env_parser)
            self.assertAlmostEqual(two.oai.resolve_ue_tx_scale_db(env_parser.parse_args([])), -3.5, places=9)


if __name__ == "__main__":
    unittest.main()
