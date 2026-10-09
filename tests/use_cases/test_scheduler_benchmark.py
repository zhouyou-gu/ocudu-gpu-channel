"""Scheduler benchmark: reproducibility helpers, traffic, analyzer, renderer.

Everything here runs without Sionna, a GPU or the radio stack. The live gate
(use_cases/scheduler_benchmark/run-ocudu-scheduler-benchmark.sh) is the integration test;
these tests pin the pieces whose arithmetic the report depends on, against
synthetic logs whose correct answers are known by construction.
"""

from __future__ import annotations

import array
import json
import math
import pathlib
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "sionna_bridge"))
sys.path.insert(0, str(PROJECT_ROOT / "use_cases" / "scheduler_benchmark"))

import analyze  # noqa: E402
import campaign  # noqa: E402
import definitions as bench  # noqa: E402
import make_scenario  # noqa: E402
import traffic  # noqa: E402
from channel_adapter import LaneProfile, MatrixProfile, Tap  # noqa: E402
from run_bridge import (  # noqa: E402
    GridTimeline,
    parse_args,
    parse_fanout,
    profiles_digest,
    remap_link_id,
    scenario_environment,
)


def profile(gain_db: float) -> MatrixProfile:
    return MatrixProfile(1, 1, (LaneProfile(0, 0, (Tap(1.5, gain_db, 0.25),)),))


class BridgeHelpers(unittest.TestCase):
    def test_fanout_spec(self) -> None:
        self.assertEqual(parse_fanout("ipc:///r/b.sock=gnb0:gnb1,ue0:ue2"),
                         ("ipc:///r/b.sock", {"gnb0": "gnb1", "ue0": "ue2"}))
        self.assertEqual(parse_fanout("tcp://h:1"), ("tcp://h:1", {}))
        for bad in ("=a:b", "e=a", "e=a:b,a:c", "e=a:x,b:x"):
            with self.assertRaises(Exception, msg=bad):
                parse_fanout(bad)

    def test_remap_link_id(self) -> None:
        rename = bench.FANOUT_RENAME
        self.assertEqual(remap_link_id("gnb0>ue1:sionna_rt", rename), "gnb1>ue3:sionna_rt")
        self.assertEqual(remap_link_id("ue0>gnb0:sionna_rt", rename), "ue2>gnb1:sionna_rt")
        self.assertEqual(remap_link_id("x>y:m", rename), "x>y:m")
        with self.assertRaises(ValueError):
            remap_link_id("gnb0-ue0", rename)

    def test_digest_identifies_channel_not_order(self) -> None:
        a = {"gnb0>ue0:m": profile(-3.0), "gnb0>ue1:m": profile(-9.0)}
        b = {"gnb0>ue1:m": profile(-9.0), "gnb0>ue0:m": profile(-3.0)}
        c = {"gnb0>ue0:m": profile(-3.0), "gnb0>ue1:m": profile(-9.5)}
        self.assertEqual(profiles_digest(a), profiles_digest(b))
        self.assertNotEqual(profiles_digest(a), profiles_digest(c))

    def test_grid_timeline_bounds_lateness(self) -> None:
        grid = GridTimeline(10.0)
        with self.assertRaises(RuntimeError):
            grid.next_point(0.0)
        grid.anchor(100.0, 5_000)
        self.assertEqual(grid.next_point(100.0), 0)
        self.assertEqual(grid.next_point(100.12), 1)      # less than a step late: next point
        self.assertEqual(grid.next_point(100.55), 5)      # a full step behind: jump to the due point
        self.assertEqual(grid.skipped, 3)
        self.assertAlmostEqual(grid.scenario_time(5), 0.5)
        self.assertAlmostEqual(grid.due(7), 100.7)

    def test_args_and_default_environment_unchanged(self) -> None:
        default = parse_args(["--layout", "1x1"])
        self.assertEqual(default.timeline, "wallclock")
        self.assertEqual(default.fanout_control_endpoint, [])
        env = scenario_environment(default)
        for key in ("timeline", "fanout_control_endpoints", "hold_until_file"):
            self.assertNotIn(key, env, "an opt-in key leaked into the default environment record")
        grid = parse_args(["--layout", "1x1", "--timeline", "grid", "--hold-until-file", "/tmp/x",
                           "--fanout-control-endpoint", "ipc:///b=gnb0:gnb1"])
        env = scenario_environment(grid)
        self.assertEqual(env["timeline"], "grid")
        self.assertEqual(env["fanout_control_endpoints"], [{"endpoint": "ipc:///b", "rename": {"gnb0": "gnb1"}}])
        for argv in (["--hold-until-file", "/tmp/x"],
                     ["--timeline", "grid", "--position-endpoint", "ipc:///p"],
                     ["--fanout-control-endpoint", "tcp://127.0.0.1:5559"]):
            with self.assertRaises(SystemExit, msg=argv):
                parse_args(["--layout", "1x1", *argv])


class Scenario(unittest.TestCase):
    def setUp(self) -> None:
        self.base = json.loads(make_scenario.DEFAULT_BASE.read_text())

    def generate(self, seed: int) -> dict:
        return make_scenario.generate(seed, self.base, duration_s=120.0, speed_range=(0.5, 1.5),
                                      min_gnb_distance_m=6.0, max_gnb_distance_m=40.0,
                                      start_max_distance_m=20.0, edge_margin_m=3.0)

    def test_same_seed_same_scenario(self) -> None:
        self.assertEqual(json.dumps(self.generate(7)), json.dumps(self.generate(7)))
        self.assertNotEqual(self.generate(7)["nodes"]["ue0"]["route_m"], self.generate(8)["nodes"]["ue0"]["route_m"])
        self.assertEqual(self.generate(7)["solver"]["seed"], 7)

    def test_routes_stay_in_line_of_sight(self) -> None:
        boxes, half_extent, _ = make_scenario.scene_obstacles(self.base["scene"])
        gnb = self.base["nodes"]["gnb0"]["start_m"]
        # Check against the RAW boxes: the generator's margin must leave clearance.
        raw = make_scenario.Region(gnb, boxes, half_extent, 0.0, 1e9, 0.0)
        raw.boxes = boxes
        for seed in range(5):
            scenario = self.generate(seed)
            for ue in ("ue0", "ue1"):
                route = scenario["nodes"][ue]["route_m"]
                self.assertGreaterEqual(len(route), 2)
                for a, b in zip(route, route[1:]):
                    self.assertTrue(raw.leg_allowed(a, b), (seed, ue, a, b))
                start_distance = math.hypot(route[0][0] - gnb[0], route[0][1] - gnb[1])
                self.assertLessEqual(start_distance, 20.0)

    def test_pillar_shadow_is_refused(self) -> None:
        boxes, half_extent, _ = make_scenario.scene_obstacles(self.base["scene"])
        region = make_scenario.Region(self.base["nodes"]["gnb0"]["start_m"], boxes, half_extent, 0.0, 1e9, 0.0)
        self.assertFalse(region.allows((-2.0, 0.0, 0.4)), "directly behind the centre pillar")
        self.assertTrue(region.allows((5.0, 0.0, 0.4)), "between pillars and mast, line of sight")


class Definitions(unittest.TestCase):
    def test_flow_id_round_trip(self) -> None:
        for cell, ue, flow in ((0, 0, 0), (1, 1, 3), (15, 15, 255)):
            self.assertEqual(bench.split_flow_id(bench.flow_id(cell, ue, flow)), (cell, ue, flow))

    def test_profiles_validate_and_override(self) -> None:
        for name in bench.TRAFFIC_PROFILES:
            bench.resolve_profile(name)
        p = bench.resolve_profile("mixed", {"ue0.dl_bulk.rate_mbps": 25, "ue1.five_qi.value": 9})
        self.assertEqual(p["ues"][0]["flows"][0]["rate_mbps"], 25.0)
        self.assertEqual(p["ues"][1]["five_qi"], 9)
        self.assertEqual(bench.TRAFFIC_PROFILES["mixed"]["ues"][0]["flows"][0]["rate_mbps"], 40.0, "override leaked")
        for bad in ({"ue0.nope.rate_mbps": 1}, {"ue5.dl_bulk.rate_mbps": 1}, {"ue1.five_qi.value": 4}):
            with self.assertRaises(ValueError, msg=bad):
                bench.resolve_profile("mixed", bad)

    def test_five_qi_table_matches_ocudu_source(self) -> None:
        # lib/ran/qos/five_qi_qos_mapping.cpp of the pinned tree.
        source = pathlib.Path("/home/dev/ocudu-spark/src/ocudu/lib/ran/qos/five_qi_qos_mapping.cpp")
        if not source.exists():
            self.skipTest("pinned OCUDU tree not present")
        text = source.read_text()
        for qi, info in bench.FIVE_QI.items():
            pattern = rf"uint_to_five_qi\({qi}\),\s*qos_chars\{{flow_type::non_gbr, qos_prio_level_t\{{{info['priority']}\}}, {info['pdb_ms']},"
            self.assertRegex(text, pattern, f"5QI {qi}")


class TrafficSchedule(unittest.TestCase):
    def test_deterministic_and_shared_by_twins(self) -> None:
        flow = {"name": "dl_bulk", "pattern": "poisson", "rate_mbps": 2.0, "size_bytes": 1000}
        a = list(traffic.flow_schedule(3, 0, flow, 5.0))
        b = list(traffic.flow_schedule(3, 0, flow, 5.0))
        c = list(traffic.flow_schedule(4, 0, flow, 5.0))
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertTrue(all(x < y for x, y in zip(a, a[1:])), "schedule must be time ordered")

    def test_rates(self) -> None:
        cbr = {"name": "x", "pattern": "cbr", "rate_mbps": 4.8, "size_bytes": 1200}
        self.assertEqual(len(list(traffic.flow_schedule(1, 0, cbr, 10.0))), 5000)
        periodic = {"name": "c", "pattern": "periodic", "interval_ms": 10.0, "size_bytes": 200}
        self.assertEqual(len(list(traffic.flow_schedule(1, 1, periodic, 10.0))), 1000)
        onoff = {"name": "v", "pattern": "onoff", "rate_mbps": 9.6, "size_bytes": 1200, "on_mean_s": 2.0,
                 "off_mean_s": 1.0}
        # Ensemble over seeds: the duty cycle is a mean, not a per-draw promise.
        total = sum(len(list(traffic.flow_schedule(s, 0, onoff, 60.0))) for s in range(20))
        expected = 20 * 60.0 * 1000 * 2 / 3
        self.assertLess(abs(total - expected) / expected, 0.08, total)

    def test_loopback_send_and_receive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            port = _free_port()
            log = tmp / "rx.bin"
            receiver = threading.Thread(target=traffic.run_receiver,
                                        args=("127.0.0.1", [port], log, tmp / "stop"), daemon=True)
            receiver.start()
            time.sleep(0.2)
            flow = {"name": "f", "pattern": "periodic", "interval_ms": 5.0, "size_bytes": 300}
            targets = [{"ue_index": 0, "flow": flow,
                        "destinations": [(bench.flow_id(0, 0, 0), ("127.0.0.1", port)),
                                         (bench.flow_id(1, 0, 0), ("127.0.0.1", port))]}]
            start = int(time.time() * 1000) + 200
            traffic.run_sender(targets, 1, start, 0.5, tmp / "tx.csv", None)
            time.sleep(0.8)
            (tmp / "stop").touch()
            receiver.join(timeout=3)
            records = list(traffic.RECORD.iter_unpack(log.read_bytes()))
            expected = len(list(traffic.flow_schedule(1, 0, flow, 0.5)))
            per_flow = {}
            for fid, _, seq, tx_ns, rx_ns in records:
                per_flow.setdefault(fid, []).append(seq)
                self.assertGreaterEqual(rx_ns, tx_ns)
            self.assertEqual(sorted(per_flow), [bench.flow_id(0, 0, 0), bench.flow_id(1, 0, 0)])
            for seqs in per_flow.values():
                self.assertEqual(sorted(seqs), list(range(expected)), "both cells get every packet")
            last = (tmp / "tx.csv").read_text().strip().splitlines()[-1].split(",")
            self.assertEqual(int(last[2]), expected)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class SyntheticRun:
    """A run directory whose correct metrics are known by construction.

    Cells a (rr) and b (qos), 4 measured seconds, traffic starting at the
    grid anchor. ue0/ue2: 100 pkt/s of 1200 B (0.96 Mb/s); ue1/ue3: 100 pkt/s
    of 200 B (0.16 Mb/s), 5QI 7 with a 100 ms budget.
      delays   ue0 10 ms, ue2 20 ms, ue1 50 ms with every 10th at 150 ms, ue3 40 ms
      losses   every 5th packet of ue3 never arrives
      grants   every slot: the bulk UE 50 PRBs; every 10th slot: the other 10 PRBs.
               Cell b's ue2 misses slots 1000..1029 with data waiting (one 30 ms wait).
    """

    MEASURE = 4

    def __init__(self, root: pathlib.Path) -> None:
        self.log = root / "logs"
        self.config = root / "configs"
        self.log.mkdir()
        self.config.mkdir()
        self.anchor = 1_700_000_000_000
        flows = [
            [{"name": "dl_bulk", "direction": "dl", "pattern": "periodic", "interval_ms": 10.0, "size_bytes": 1200}],
            [{"name": "dl_control", "direction": "dl", "pattern": "periodic", "interval_ms": 10.0, "size_bytes": 200}],
        ]
        qos = [bench.FIVE_QI[9], bench.FIVE_QI[7]]
        cells = {}
        for cell, sched, ues in (("a", "rr", ("ue0", "ue1")), ("b", "qos", ("ue2", "ue3"))):
            cells[cell] = {"scheduler": sched, "scheduler_info": bench.SCHEDULERS[sched], "ues": [
                {"device_id": ue, "cell_index": i, "role": ("bulk", "interactive")[i], "five_qi": (9, 7)[i],
                 "qos": qos[i], "twin": bench.TWINS[ue], "flows": flows[i], "ipv4": "x"}
                for i, ue in enumerate(ues)]}
        self.benchmark = {"seed": 5, "measure_seconds": self.MEASURE, "cells": cells,
                          "traffic_profile": {"name": "t", "ues": [{"flows": f} for f in flows]}}
        (self.config / "benchmark.json").write_text(json.dumps(self.benchmark))
        self.write_sionna()
        self.write_traffic()
        self.write_sched()
        self.write_ue_side()

    def write_sionna(self) -> None:
        lines = []
        for k in range(self.MEASURE * 10):
            lines.append(json.dumps({"event": "sionna_rt_update", "positions": {"ue0": [1, 2, 0.4]},
                                     "timeline": {"phase": "run", "grid_index": k, "scenario_time_s": k / 10,
                                                  "anchor_unix_ms": self.anchor, "lag_ms": 1.0,
                                                  "skipped_grid_points": 0},
                                     "profile_sha256": f"{k:064x}", "timing_ms": {"channel_generation": 50.0},
                                     "fanout": [{"ok": True, "control_ack_unix_ms": 1},
                                                {"ok": True, "control_ack_unix_ms": 2}]}))
        (self.log / "sionna-status.jsonl").write_text("\n".join(lines) + "\n")

    def write_traffic(self) -> None:
        (self.log / "traffic-start.json").write_text(json.dumps(
            {"start_at_unix_ms": self.anchor, "duration_s": self.MEASURE}))
        delays = {"ue0": lambda s: 10.0, "ue2": lambda s: 20.0,
                  "ue1": lambda s: 150.0 if s % 10 == 0 else 50.0, "ue3": lambda s: 40.0}
        dropped = {"ue3": lambda s: s % 5 == 0}
        ticks = ["t_ns,flow_id,next_seq"]
        for cell_index, cell in enumerate(("a", "b")):
            for ue in self.benchmark["cells"][cell]["ues"]:
                flow = ue["flows"][0]
                fid = bench.flow_id(cell_index, ue["cell_index"], 0)
                offsets = list(traffic.flow_schedule(5, ue["cell_index"], flow, self.MEASURE))
                records = bytearray()
                for seq, offset in enumerate(offsets):
                    if dropped.get(ue["device_id"], lambda s: False)(seq):
                        continue
                    tx = int((self.anchor + offset * 1000) * 1e6)
                    rx = tx + int(delays[ue["device_id"]](seq) * 1e6)
                    records += traffic.RECORD.pack(fid, 0, seq, tx, rx)
                (self.log / f"rx-dl-{ue['device_id']}.bin").write_bytes(bytes(records))
                ticks.append(f"0,{fid},{len(offsets)}")
        (self.log / "tx-dl.csv").write_text("\n".join(ticks) + "\n")

    def write_sched(self) -> None:
        rntis = {"ue0": "0x4601", "ue1": "0x4602", "ue2": "0x4601", "ue3": "0x4602"}
        for ue, rnti in rntis.items():
            (self.log / f"srsue-{ue}.log").write_text(f"Random Access Complete.     c-rnti={rnti}, ta=0\n")
        for cell, gnb, pci, bulk, other in (("a", "gnb0", 1, "0x4601", "0x4602"), ("b", "gnb1", 2, "0x4601", "0x4602")):
            lines = ["2027-01-15T08:00:00.000000 [GNB     ] [I] SSB derived parameters for cell: 1, nof_crbs: 106"]
            for i in range(-50, self.MEASURE * 1000 + 50):
                wall = self.anchor + i + 0.5
                stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(wall / 1000)) + f".{int(wall % 1000 * 1000):06d}"
                slot = (i + 20000) % 10240
                entries = []
                if not (cell == "b" and 1000 <= i < 1030):
                    entries.append(f"DL: ue=0 c-rnti={bulk} h_id=0 ss_id=1 rb=[0..50) k1=4 cw[0]: newtx=true rv=0 "
                                   f"tbs=1000 ri=1 dl_bo=500")
                if i % 10 == 0:
                    entries.append(f"DL: ue=1 c-rnti={other} h_id=0 ss_id=1 rb=[50..60) k1=4 cw[0]: newtx=true rv=0 "
                                   f"tbs=100 ri=1 dl_bo=0")
                lines.append(f"{stamp} [SCHED   ] [I] [{slot // 10:6d}.{slot % 10}] Slot decisions pci={pci} "
                             f"t={10 if cell == 'a' else 30}us ({len(entries)} PDSCHs, 0 PUSCHs, 0 PUCCHs): "
                             + ", ".join(entries))
            (self.log / f"{gnb}-internal.log").write_text("\n".join(lines) + "\n")

    def write_ue_side(self) -> None:
        header = "time;pci;rsrp;dl_mcs;dl_snr;dl_bler;ul_bler;cpu_0"
        for ue, pci in (("ue0", 1), ("ue1", 1), ("ue2", 2), ("ue3", 2)):
            (self.log / f"srsue-{ue}.start_unix_ms").write_text(str(self.anchor - 10_000))
            rows = [header] + [f"{(10 + k) * 1000 + 500};{pci};-60;27;30;0;0;10" for k in range(-1, self.MEASURE + 1)]
            (self.log / f"srsue-metrics-{ue}.csv").write_text("\n".join(rows) + "\n")


class AnalyzerOnSyntheticRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.synthetic = SyntheticRun(pathlib.Path(cls.tmp.name))
        analyzer = analyze.RunAnalyzer(cls.synthetic.log, cls.synthetic.config)
        analyzer.poll()
        cls.analyzer = analyzer
        cls.report = analyzer.report(live=False)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def metrics(self, sched: str) -> dict:
        cell = {"rr": "a", "qos": "b"}[sched]
        self.assertEqual(self.report["lanes"][cell]["scheduler"], sched)
        return self.report["lanes"][cell]["metrics"]

    def test_all_segments_valid_and_instruments_recorded(self) -> None:
        self.assertEqual(self.report["segments_complete"], 4)
        self.assertEqual(self.report["segments_valid"], 4, [s["reasons"] for s in self.report["segments"]])
        checks = self.report["checks"]
        self.assertGreater(checks["sched_lines"]["a"], 4000)
        self.assertEqual(checks["rnti_map"]["a"], {"0x4601": "ue0", "0x4602": "ue1"})
        for name, flow in checks["flows"].items():
            self.assertEqual(flow["scheduled"], 400, name)

    def test_delay_and_violation(self) -> None:
        rr, qos = self.metrics("rr")["per_ue"], self.metrics("qos")["per_ue"]
        self.assertAlmostEqual(rr["ue0"]["delay_ms"]["dl"]["mean"], 10.0, places=3)
        self.assertAlmostEqual(qos["ue2"]["delay_ms"]["dl"]["mean"], 20.0, places=3)
        self.assertAlmostEqual(rr["ue1"]["delay_ms"]["dl"]["p99"], 150.0, places=3)
        self.assertAlmostEqual(rr["ue1"]["delay_ms"]["dl"]["p50"], 50.0, places=3)
        self.assertAlmostEqual(rr["ue1"]["pdb_violation_rate"], 0.1)
        self.assertEqual(rr["ue0"]["pdb_violation_rate"], 0.0)

    def test_drop_rate(self) -> None:
        qos = self.metrics("qos")["per_ue"]
        self.assertAlmostEqual(qos["ue3"]["drop_rate"], 0.2)
        self.assertEqual(self.metrics("rr")["per_ue"]["ue1"]["drop_rate"], 0.0)

    def test_throughput_and_fairness(self) -> None:
        rr = self.metrics("rr")
        # 100 pkt/s x 1200 B; the last 10 ms of packets arrive after the window.
        self.assertAlmostEqual(rr["per_ue"]["ue0"]["throughput_mbps"]["dl"], 0.96, delta=0.01)
        self.assertAlmostEqual(rr["per_ue"]["ue1"]["throughput_mbps"]["dl"], 0.16, delta=0.01)
        expected_jain = (0.96 + 0.16) ** 2 / (2 * (0.96 ** 2 + 0.16 ** 2))
        self.assertAlmostEqual(rr["jain_fairness"]["dl_throughput"], expected_jain, delta=0.01)
        self.assertAlmostEqual(rr["jain_fairness"]["dl_throughput_over_offered"], 1.0, delta=0.02)

    def test_prb_utilization_and_spectral_efficiency(self) -> None:
        rr = self.metrics("rr")
        self.assertAlmostEqual(rr["resource_utilization"]["dl_prb"], 51 / 106, delta=0.01)
        self.assertAlmostEqual(rr["resource_utilization"]["slots_per_s"], 1000, delta=2)
        # newTx TBS: 1000 B every slot + 100 B every 10th = 8.08 Mb/s over 20 MHz.
        self.assertAlmostEqual(rr["aggregate_throughput_mbps"]["mac_dl"], 8.08, delta=0.03)
        self.assertAlmostEqual(rr["spectral_efficiency_bps_hz"]["dl_mac"], 8.08e6 / 20e6, delta=0.002)
        share = rr["per_ue"]["ue0"]["dl_prb_share"]
        self.assertAlmostEqual(share, 50 / 51, delta=0.002)

    def test_starvation(self) -> None:
        rr, qos = self.metrics("rr")["per_ue"], self.metrics("qos")["per_ue"]
        self.assertEqual(rr["ue0"]["starvation"]["events"], 0)
        self.assertAlmostEqual(rr["ue0"]["starvation"]["dl_wait_ms"]["max"], 1.0)
        self.assertEqual(qos["ue2"]["starvation"]["events"], 1)
        self.assertAlmostEqual(qos["ue2"]["starvation"]["dl_wait_ms"]["max"], 31.0)
        # ue1 leaves nothing waiting (dl_bo=0), so its gaps are not waits.
        self.assertEqual(rr["ue1"]["starvation"]["dl_wait_ms"]["count"], 0)

    def test_decision_latency_and_pairs(self) -> None:
        self.assertAlmostEqual(self.metrics("rr")["decision_latency_us"]["mean"], 10.0)
        self.assertAlmostEqual(self.metrics("qos")["decision_latency_us"]["mean"], 30.0)
        pairs = self.report["comparison"]
        self.assertEqual(pairs["order"], ["a", "b"])
        self.assertAlmostEqual(pairs["decision_us"]["mean_diff"], -20.0)
        self.assertEqual(pairs["decision_us"]["n"], 4)

    def test_resource_grid_and_live_mode(self) -> None:
        grid = self.analyzer.resource_grid()
        self.assertEqual(len(grid["a"]["slots"]), analyze.RECENT_SLOTS)
        owners = {entry[0] for slot in grid["a"]["slots"] for entry in slot["dl"]}
        self.assertLessEqual(owners, {"ue0", "ue1"})
        live = self.analyzer.report(live=True)
        self.assertEqual(live["segments_complete"], 4, "a past run is complete even when asked live")

    def test_renderer_self_test(self) -> None:
        result = subprocess.run([sys.executable, str(PROJECT_ROOT / "use_cases/scheduler_benchmark/render-scheduler-benchmark-configs.py"),
                                 "--self-test"], capture_output=True, text=True, env={"PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)


class GnbSideValidity(unittest.TestCase):
    """Live runs have no srsUE CSV yet; the gNB report decides who was in service."""

    def write_reports(self, run: "SyntheticRun", cell: str, rows_of) -> None:
        lines = [json.dumps({"rx_unix_ms": run.anchor + k * 1000 + 500,
                             "payload": {"cells": [{"cell_metrics": {}, "ue_list": rows_of(k)}]}})
                 for k in range(SyntheticRun.MEASURE)]
        (run.log / f"gnb-metrics-{cell}.jsonl").write_text("\n".join(lines) + "\n")

    def test_gnb_report_decides_service(self) -> None:
        ok = lambda rnti: {"rnti": rnti, "dl_nof_ok": 50, "dl_nof_nok": 0}  # noqa: E731
        with tempfile.TemporaryDirectory() as tmp:
            run = SyntheticRun(pathlib.Path(tmp))
            for ue in ("ue0", "ue1", "ue2", "ue3"):
                (run.log / f"srsue-metrics-{ue}.csv").write_text("")  # as during a live run
            # Every UE in every report: every second valid.
            self.write_reports(run, "a", lambda k: [ok(0x4601), ok(0x4602)])
            # ue3 missing from one report only: the neighbouring reports cover it
            # (a 1 s report straddles two segments), so nothing is invalidated.
            self.write_reports(run, "b", lambda k: [ok(0x4601)] + ([] if k == 2 else [ok(0x4602)]))
            analyzer = analyze.RunAnalyzer(run.log, run.config)
            analyzer.poll()
            self.assertEqual([analyzer.segment_validity(k)["valid"] for k in range(4)], [True] * 4)
            # ue3 missing from every report: every second invalid, with the reason named.
            self.write_reports(run, "b", lambda k: [ok(0x4601)])
            analyzer = analyze.RunAnalyzer(run.log, run.config)
            analyzer.poll()
            for k in range(4):
                self.assertIn("ue3: not in the gNB report", analyzer.segment_validity(k)["reasons"])
            # ue1's every DL HARQ failing in all neighbouring reports: out of service.
            self.write_reports(run, "a", lambda k: [ok(0x4601), {"rnti": 0x4602, "dl_nof_ok": 0, "dl_nof_nok": 7}])
            self.write_reports(run, "b", lambda k: [ok(0x4601), ok(0x4602)])
            analyzer = analyze.RunAnalyzer(run.log, run.config)
            analyzer.poll()
            self.assertEqual(analyzer.segment_validity(1)["reasons"], ["ue1: every DL HARQ failed"])


class Campaign(unittest.TestCase):
    def fake(self, seed: int, a: str, b: str, diff: float) -> dict:
        lane = lambda s: {"scheduler": s, "metrics": {"resource_utilization": {"slots_per_s": 600.0}}}  # noqa: E731
        pairs = {key: {"n": 60, "mean_diff": diff, "ci95": [diff - 1.0, diff + 1.0]} for key, _, _ in campaign.METRICS}
        return {"seed": seed, "lanes": {"a": lane(a), "b": lane(b)}, "segments_valid": 60, "segments_complete": 60,
                "comparison": pairs, "_gate": {"status": "passed"}, "_dir": f"{seed}-{a}-{b}"}

    def test_swap_pair_separates_effect_from_cell_bias(self) -> None:
        # True effect rr - qos = 3, cell bias a - b = 0.5:
        #   rr|qos: d = +3 + 0.5;  qos|rr: d = -3 + 0.5;  rr|rr: d = 0.5
        reports = [self.fake(1, "rr", "qos", 3.5), self.fake(1, "qos", "rr", -2.5), self.fake(1, "rr", "rr", 0.5)]
        summary = campaign.summarize(reports)
        effect = summary["effects"]["aggregate_dl_mbps"]
        self.assertEqual(summary["policies"], ["qos", "rr"])
        self.assertEqual(effect["difference"], "qos - rr")
        self.assertAlmostEqual(effect["mean_effect"], -3.0)
        self.assertAlmostEqual(effect["mean_bias_from_swaps"], 0.5)
        self.assertAlmostEqual(effect["aa_bias"], 0.5)
        self.assertAlmostEqual(effect["swap_pairs"][0]["ci95_half_width"], math.sqrt(2) / 2)
        self.assertIn("effect = qos - rr", campaign.table(summary))


class Statistics(unittest.TestCase):
    def test_jain(self) -> None:
        self.assertAlmostEqual(analyze.jain([1, 1, 1]), 1.0)
        self.assertAlmostEqual(analyze.jain([1, 0]), 0.5)
        self.assertIsNone(analyze.jain([]))

    def test_paired_ci_coverage(self) -> None:
        """Ensemble coverage of the batch-means CI on AR(1) differences.

        Measured over 400 series of 120 segments when the method was chosen:
        0.945 at r = 0.5. With 200 series the binomial sd is 0.016, so 0.90 is
        ~2.8 sd below the measured coverage; a regression to the earlier
        percentile block bootstrap (0.875 at r = 0.5) fails it.
        """

        import random
        rng = random.Random(1)
        covered, trials = 0, 200
        for _ in range(trials):
            x, diffs = 0.0, []
            for _ in range(120):
                x = 0.5 * x + rng.gauss(0, 1)
                diffs.append(2.0 + x)
            low, high = analyze.batch_means_ci(diffs)
            covered += low <= 2.0 <= high
        self.assertGreaterEqual(covered / trials, 0.90, covered)
        self.assertIsNone(analyze.batch_means_ci([1.0] * 10), "too few segments for an interval")


if __name__ == "__main__":
    unittest.main()
