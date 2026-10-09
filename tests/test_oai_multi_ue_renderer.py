"""render-oai-multi-ue-configs.py: the TDD n78 20 MHz cell, carrier labels, veth endpoints, tx scale (X3/X4)."""

from __future__ import annotations

import importlib.util
import json
import math
import pathlib
import subprocess
import sys
import unittest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
RENDERER = PROJECT_ROOT / "scripts" / "native" / "render-oai-multi-ue-configs.py"
GNB_FIXTURE = PROJECT_ROOT / "use_cases" / "configs" / "ran" / "ocudu" / "docker" / "gnb_zmq_b210_fdd_srsue.yaml"
NRUE_TEMPLATE = PROJECT_ROOT / "use_cases" / "configs" / "ran" / "oai" / "nrue_zmq_multi_ue.conf.in"
SUBSCRIBERS = PROJECT_ROOT / "use_cases" / "configs" / "ran" / "open5gs" / "subscriber-multi-ue.csv"
WALK_TDD = PROJECT_ROOT / "use_cases" / "configs" / "sionna" / "scenarios" / "robot_ring" / "robot-ring-walk-tdd.json"
WALK_TDD_UE2UE = PROJECT_ROOT / "use_cases" / "configs" / "sionna" / "scenarios" / "robot_ring" / "robot-ring-walk-tdd-ue2ue.json"


def load_renderer():
    spec = importlib.util.spec_from_file_location("render_oai_multi_ue_for_test", RENDERER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RendererTests(unittest.TestCase):
    def test_self_test_passes(self):
        result = subprocess.run([sys.executable, str(RENDERER), "--self-test"], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("result=pass", result.stdout)

    def test_gnb_is_a_tdd_n78_20mhz_30khz_cell_at_23_04_msps(self):
        r = load_renderer()
        gnb = r.render_gnb_tdd(GNB_FIXTURE.read_text(encoding="utf-8"), pathlib.Path("/tmp/logs"))
        cell = gnb.split("cell_cfg:\n", 1)[1].split("\nlog:", 1)[0]
        for token in ("  band: 78\n", "  dl_arfcn: 632628\n", "  common_scs: 30\n", "  channel_bandwidth_MHz: 20\n",
                      "    prach_config_index: 159\n", "    dl_ul_tx_period: 10\n", "    nof_dl_slots: 7\n",
                      "    nof_ul_slots: 2\n", "    nof_dl_symbols: 6\n", "    nof_ul_symbols: 4\n"):
            self.assertIn(token, cell)
        self.assertNotIn("pdcch", gnb, "srsUE PDCCH override and the 15 kHz CORESET#0 entry must be gone")
        self.assertIn("tx_port=tcp://127.0.0.1:2000,rx_port=tcp://127.0.0.1:2001,base_srate=23.04e6", gnb)
        self.assertIn("  srate: 23.04\n", gnb)
        # 51 PRB at 30 kHz with a 768-point FFT is 23.04 MS/s: the broker batch stays 23040.
        self.assertEqual(r.PRB * 12 <= 768, True)
        self.assertEqual(768 * r.SCS_KHZ * 1000, r.SAMPLE_RATE_HZ)
        self.assertEqual(r.CARRIER_HZ, 3_489_420_000)
        self.assertEqual(r.nrue_radio_args(), "-E -r 51 --numerology 1 --band 78 -C 3489420000 --ssb 0")

    def test_scenarios_render_with_tdd_labels_veth_endpoints_and_tx_scale(self):
        r = load_renderer()
        for scenario, link_count in ((WALK_TDD, 4), (WALK_TDD_UE2UE, 6)):
            shape = r.sionna.load_live_shape(scenario, r.UE_COUNT)
            self.assertEqual(len(shape.links), link_count, scenario.name)
            scale = r.sionna.ue_tx_scale_db(r.TX_POWER_DL, r.OAI_UE_TX_POWER)
            floors = r.sionna.rx_noise_powers(shape, 40.0, r.TX_POWER_DL, r.OAI_UE_TX_POWER, scale)
            topology = r.render_topology_tdd(shape, floors, scale)
            self.assertEqual(topology.count("    carrier: n78\n"), 3, "every port carries the one TDD carrier label")
            self.assertNotIn("tx_carrier", topology)
            self.assertNotIn("rx_carrier", topology)
            self.assertIn("    tx_endpoint: tcp://10.201.0.2:2101\n", topology)
            self.assertIn("    rx_endpoint: tcp://10.201.0.1:2100\n", topology)
            self.assertIn("    tx_endpoint: tcp://10.201.1.2:2103\n", topology)
            self.assertIn("    rx_endpoint: tcp://10.201.1.1:2102\n", topology)
            self.assertIn("    tx_endpoint: tcp://127.0.0.1:2000\n", topology)
            self.assertIn("    rx_endpoint: tcp://127.0.0.1:2001\n", topology)
            self.assertEqual(topology.count("    tx_scale_db: "), 2)
            self.assertEqual(topology.count("  - from: "), link_count)
            self.assertEqual(topology.count("    sample_rate_hz: 23040000\n"), 3)
            self.assertIn("  batch_samples: 23040\n", topology)
        ue2ue = json.loads(WALK_TDD_UE2UE.read_text(encoding="utf-8"))
        crosstalk = [(l["from"], l["to"]) for l in ue2ue["links"] if l["direction"] == "crosstalk"]
        self.assertEqual(sorted(crosstalk), [("ue0", "ue1"), ("ue1", "ue0")])
        self.assertTrue(all(l["model"] == "sionna_rt" for l in ue2ue["links"]))

    def test_tx_scale_and_gnb_floor_follow_the_ul_power_override(self):
        r = load_renderer()
        shape = r.sionna.load_live_shape(WALK_TDD, r.UE_COUNT)
        # Calibrated OAI level 1.17e-5 (-49.3 dB): the UE port gets a +22.8 dB *gain*,
        # where srsUE (3.0e4, +44.8 dB) got -71.3 dB.
        self.assertAlmostEqual(r.sionna.ue_tx_scale_db(r.TX_POWER_DL, r.OAI_UE_TX_POWER), 22.810, places=3)
        self.assertAlmostEqual(r.sionna.ue_tx_scale_db(r.TX_POWER_DL, 3.0e4), -71.279, places=3)
        # Another measured level, say 2.0e2 (+23 dB), gives -7 - 10log10(2e2/1.12e-2) = -49.5 dB...
        measured = 2.0e2
        scale = r.sionna.ue_tx_scale_db(r.TX_POWER_DL, measured)
        self.assertAlmostEqual(scale, -7.0 - 10 * math.log10(measured / r.TX_POWER_DL), places=9)
        # ...and the gNB floor is sized from the scaled uplink, i.e. identical to
        # the default case: the scale cancels the wire level by construction.
        floors = r.sionna.rx_noise_powers(shape, 40.0, r.TX_POWER_DL, measured, scale)
        default = r.sionna.rx_noise_powers(shape, 40.0, r.TX_POWER_DL, r.OAI_UE_TX_POWER,
                                           r.sionna.ue_tx_scale_db(r.TX_POWER_DL, r.OAI_UE_TX_POWER))
        self.assertAlmostEqual(floors["gnb0"], default["gnb0"], places=15)
        self.assertAlmostEqual(floors["gnb0"], r.TX_POWER_DL * 10 ** -0.7 / 1e4, places=15)
        self.assertAlmostEqual(floors["ue0"], r.TX_POWER_DL / 1e4, places=15)

    def test_nrue_confs_carry_the_subscriber_imsis(self):
        r = load_renderer()
        template = NRUE_TEMPLATE.read_text(encoding="utf-8")
        records = [l.split(",") for l in SUBSCRIBERS.read_text(encoding="utf-8").splitlines() if l and not l.startswith("#")]
        for ue, record in zip(r.legacy.UES[: r.UE_COUNT], records):
            conf = r.render_nrue(template, ue)
            self.assertIn(f'imsi = "{record[1]}";', conf)
            self.assertIn(f'key = "{record[2]}";', conf)
            self.assertIn(f'opc = "{record[4]}";', conf)
            self.assertEqual(conf.count("uicc0 = {"), 1)
        self.assertEqual(r.VETH["ue0"], ("10.201.0.1", "10.201.0.2"))
        self.assertEqual(r.VETH["ue1"], ("10.201.1.1", "10.201.1.2"))

    def test_uecap_ul_entry_is_added_once_and_only_when_missing(self):
        r = load_renderer()
        block = ("            <FeatureSetUplinkPerCC>\n"
                 "                <supportedSubcarrierSpacingUL><kHz{scs}/></supportedSubcarrierSpacingUL>\n"
                 "                <supportedBandwidthUL>\n                    <fr1><mhz{bw}/></fr1>\n"
                 "                </supportedBandwidthUL>\n            </FeatureSetUplinkPerCC>\n")
        without = "<a>\n" + block.format(scs=15, bw=20) + block.format(scs=30, bw=40) + "</a>\n"
        added = r.uecap_with_ul_30khz_20mhz(without)
        self.assertEqual(added.count("<FeatureSetUplinkPerCC>"), 3)
        self.assertEqual(r.uecap_with_ul_30khz_20mhz(added), added)
        with_entry = "<a>\n" + block.format(scs=30, bw=20) + "</a>\n"
        self.assertEqual(r.uecap_with_ul_30khz_20mhz(with_entry), with_entry)


if __name__ == "__main__":
    unittest.main()
