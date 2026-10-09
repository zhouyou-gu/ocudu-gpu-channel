#!/usr/bin/env python3
"""Scheduler-benchmark analyzer: logs of one run -> per-scheduler metrics.

Reads, incrementally (so the web UI can call it on a live run):

  rx-dl-<ue>.bin, rx-ul.bin        traffic receivers (per packet: flow, seq, tx, rx)
  tx-dl.csv, tx-ul-<ue>.csv        traffic senders' progress (sent count)
  <gnb>-internal.log               OCUDU SCHED `Slot decisions` lines (per slot grants)
  gnb-metrics-<cell>.jsonl         OCUDU JSON scheduler metrics (CQI, MCS, BLER)
  srsue-<ue>.log, srsue-metrics-<ue>.csv   C-RNTI and UE-side sync state
  sionna-status.jsonl              grid-timeline anchor, positions, channel digests

Time origin is the Sionna bridge's grid anchor: segment k covers scenario
seconds [k, k+1), the same seconds of the same channel in both cells. A
segment is valid when every UE of both cells was in service throughout it.

Metric definitions are in METRIC_DEFINITIONS below; every value names its
layer (app = UDP end to end, mac = OCUDU scheduler log).
"""

from __future__ import annotations

import argparse
import array
import bisect
import collections
import json
import math
import pathlib
import re
import statistics
import struct
import sys
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import definitions as bench  # noqa: E402
import traffic  # noqa: E402

CHANNEL_BANDWIDTH_HZ = 20e6
PRB_BANDWIDTH_HZ = 12 * 15e3
SLOTS_PER_SECOND = 1000  # 15 kHz SCS
SLOT_MODULUS = 1024 * 10
STARVATION_THRESHOLD_MS = 20.0
LIVE_LOSS_HORIZON_S = 5.0
RECENT_SLOTS = 240
BOOTSTRAP_BLOCK = 10

METRIC_DEFINITIONS = {
    "aggregate_throughput": "Sum over the cell's UEs of UDP payload received per second (app), and of newTx TBS "
                            "scheduled per second (mac).",
    "per_ue_throughput": "Per UE, UDP payload received per second, against the offered load.",
    "jain_fairness": "Jain's index (sum x)^2 / (n sum x^2) over the cell's UEs, for DL throughput, DL throughput "
                     "divided by offered load, and DL PRB share. 1 = equal, 1/n = one UE takes all.",
    "delay": "One-way UDP delay, receiver clock minus sender clock (one host clock), of delivered packets.",
    "pdb_violation": "Share of sent packets delivered later than the UE's 5QI packet delay budget.",
    "packet_drop": "Share of sent packets never received (after the drain window).",
    "resource_utilization": "PRBs granted (UE + common) / (PRBs in the carrier x slots), from the SCHED log.",
    "spectral_efficiency": "Bits per second per Hz of the 20 MHz carrier: delivered UDP payload (app) and newTx "
                           "TBS (mac); 'per used PRB' divides TBS by the granted PRB bandwidth only.",
    "starvation": f"DL: time from a newTx grant that left data waiting (dl_bo > 0) to the UE's next newTx grant. "
                  f"An event is a wait longer than {STARVATION_THRESHOLD_MS:g} ms. UL: gap between newTx grants.",
    "decision_latency": "Scheduler decision time per logged slot (`t=` of `Slot decisions`).",
    "gbr_satisfaction": "Not measurable: srsUE carries one default non-GBR QoS flow per PDU session, so no "
                        "GBR target exists to satisfy.",
}

SLOT_LINE = re.compile(
    r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+) \[SCHED\s*\] \[[IDW]\] \[\s*(\d+)\.(\d+)\] "
    r"Slot decisions pci=(\d+) t=(\d+)us \([^)]*\):?(.*)$"
)
DL_ENTRY = re.compile(r"DL: ue=(\d+) c-rnti=(0x[0-9a-f]+) .*?rb=\[(\d+)\.\.(\d+)\) .*?newtx=(\w+) rv=\d+ tbs=(\d+)"
                      r"(?: ri=\d+ dl_bo=(\d+))?")
UL_ENTRY = re.compile(r"UL: ue=(\d+) rnti=(0x[0-9a-f]+) .*?rb=\[(\d+)\.\.(\d+)\) newtx=(\w+) rv=\d+ tbs=(\d+)")
COMMON_ENTRY = re.compile(r"(?:SIB1|SI-\d+|RAR|PG):[^,]*?rb=\[(\d+)\.\.(\d+)\)")
NOF_CRBS = re.compile(r"nof_crbs: (\d+)")
RNTI_LINE = re.compile(r"Random Access Complete\.\s+c-rnti=(0x[0-9a-f]+)")


def parse_log_time_ns(text: str) -> int:
    # OCUDU writes local time; the gate hosts run in UTC (checked in the report).
    stamp = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=timezone.utc)
    return int(stamp.timestamp() * 1e9)


def percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return ordered[rank]


def summary(values: Sequence[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "mean": None, "p50": None, "p95": None, "p99": None, "max": None}
    ordered = sorted(values)

    def rank(q: float) -> float:
        return ordered[max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))]

    return {"count": len(ordered), "mean": sum(ordered) / len(ordered), "p50": rank(0.5), "p95": rank(0.95),
            "p99": rank(0.99), "max": ordered[-1]}


def jain(values: Sequence[float]) -> float | None:
    values = [v for v in values if v is not None]
    if not values:
        return None
    total = sum(values)
    squares = sum(v * v for v in values)
    if squares == 0:
        return None
    return total * total / (len(values) * squares)


def bootstrap_block_length(diffs: Sequence[float]) -> int:
    """Block length from the lag-1 autocorrelation of the differences.

    Treating the series as AR(1) with coefficient r, its integrated
    autocorrelation time is (1 + r) / (1 - r); a block of twice that keeps
    most of the long-run variance inside each block. Bounded to
    [BOOTSTRAP_BLOCK, n / 4] so there are always several blocks to resample.
    """

    n = len(diffs)
    mean = sum(diffs) / n
    var = sum((d - mean) ** 2 for d in diffs)
    if var == 0:
        return BOOTSTRAP_BLOCK
    r = sum((diffs[i] - mean) * (diffs[i + 1] - mean) for i in range(n - 1)) / var
    r = min(max(r, 0.0), 0.95)
    tau = (1.0 + r) / (1.0 - r)
    return int(min(max(BOOTSTRAP_BLOCK, math.ceil(2.0 * tau)), max(BOOTSTRAP_BLOCK, n // 4)))


# Two-sided 97.5% Student-t quantiles by degrees of freedom (beyond 30: ~normal).
T_975 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262,
         10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110,
         18: 2.101, 19: 2.093, 20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
         26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042}


def batch_means_ci(diffs: Sequence[float]) -> tuple[float, float] | None:
    """95% CI of the mean of paired per-segment differences (batch means).

    Consecutive segments share a channel and queues, so they are not
    independent. The series is cut into m non-overlapping batches of the
    length bootstrap_block_length() picks; the batch means are close to
    independent, and mean +- t(m-1) * sd(batch means) / sqrt(m) is the
    interval. Ensemble coverage on AR(1) series of 120 segments
    (tests/test_scheduler_benchmark.py): 94.5% at r <= 0.5, 88% at r = 0.8.
    """

    n = len(diffs)
    if n < 2 * BOOTSTRAP_BLOCK:
        return None
    block = bootstrap_block_length(diffs)
    m = n // block
    if m < 3:
        return None
    means = [sum(diffs[i * block:(i + 1) * block]) / block for i in range(m)]
    mean = sum(diffs[:m * block]) / (m * block)
    half = T_975.get(m - 1, 1.96) * statistics.stdev(means) / math.sqrt(m)
    return mean - half, mean + half


class TailReader:
    """Append-only file reader that returns only complete new data."""

    def __init__(self, path: pathlib.Path, binary: bool = False, record_size: int = 0) -> None:
        self.path = path
        self.offset = 0
        self.binary = binary
        self.record_size = record_size
        self.partial = b""

    def read(self) -> bytes:
        try:
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                data = handle.read()
        except FileNotFoundError:
            return b""
        self.offset += len(data)
        data = self.partial + data
        if self.record_size:
            usable = len(data) - len(data) % self.record_size
        else:
            usable = data.rfind(b"\n") + 1
        self.partial = data[usable:]
        return data[:usable]

    def lines(self) -> list[str]:
        return self.read().decode("utf-8", errors="replace").splitlines()


class FlowState:
    def __init__(self, flow_id: int, ue: str, flow: dict[str, Any], pdb_ms: float, offsets: array.array) -> None:
        self.flow_id = flow_id
        self.ue = ue
        self.flow = flow
        self.pdb_ms = pdb_ms
        self.offsets = offsets          # nominal send offsets (s) from the traffic start, by seq
        self.sent_next_seq = 0          # from the sender's progress log
        self.sender_done = False
        self.received = bytearray(len(offsets))
        self.bounds: list[int] | None = None          # segment k sends seqs bounds[k]..bounds[k+1]-1
        self.received_by_seg: dict[int, int] = {}     # nominal tx segment -> packets received
        self.delay_ms: dict[int, array.array] = {}   # tx segment -> delays
        self.rx_bytes: dict[int, int] = {}           # rx segment -> bytes
        self.all_delays = array.array("f")
        self.duplicates = 0
        self.unknown_seq = 0


class CellSched:
    """Per-cell accumulators from the SCHED log, by segment."""

    def __init__(self) -> None:
        self.nof_prb = 106
        self.slot_unwrap_last: int | None = None
        self.slot_unwrap_base = 0
        self.slot_samples: list[tuple[int, int]] = []    # (wall_ns, abs_slot), ~1 per 50 ms
        self.seg: dict[int, dict[str, Any]] = {}
        self.decision_us_all = array.array("f")
        self.last_dl_newtx: dict[str, tuple[int, int]] = {}  # ue -> (abs_slot, dl_bo)
        self.last_ul_newtx: dict[str, int] = {}
        self.lines = 0
        # The last logged slots, for the UI's resource grid: (abs_slot, wall_ns,
        # decision_us, [(owner, rb0, rb1, newtx)] DL, [...] UL).
        self.recent: collections.deque = collections.deque(maxlen=RECENT_SLOTS)

    def segment(self, k: int) -> dict[str, Any]:
        if k not in self.seg:
            self.seg[k] = {"dl_prb": 0, "ul_prb": 0, "dl_common_prb": 0, "decision_us": [], "ue": {},
                           "first_slot": None, "last_slot": None}
        return self.seg[k]

    def ue_segment(self, k: int, ue: str) -> dict[str, Any]:
        seg = self.segment(k)
        if ue not in seg["ue"]:
            seg["ue"][ue] = {"dl_prb": 0, "ul_prb": 0, "dl_newtx_bytes": 0, "dl_retx_bytes": 0,
                             "ul_newtx_bytes": 0, "ul_retx_bytes": 0, "dl_grants": 0, "ul_grants": 0,
                             "dl_wait_ms": [], "ul_gap_ms": []}
        return seg["ue"][ue]

    def abs_slot(self, sfn: int, slot: int) -> int:
        raw = sfn * 10 + slot
        if self.slot_unwrap_last is not None and raw < self.slot_unwrap_last - SLOT_MODULUS // 2:
            self.slot_unwrap_base += SLOT_MODULUS
        elif self.slot_unwrap_last is not None and raw > self.slot_unwrap_last + SLOT_MODULUS // 2:
            # A late line from before a wrap: map it to the previous lap.
            return raw + self.slot_unwrap_base - SLOT_MODULUS
        self.slot_unwrap_last = raw
        return raw + self.slot_unwrap_base

    def slots_between(self, w0_ns: int, w1_ns: int) -> float | None:
        samples = self.slot_samples
        if len(samples) < 2:
            return None
        times = [s[0] for s in samples]

        def at(w: int) -> float | None:
            i = bisect.bisect_left(times, w)
            if i == 0:
                return None if w < times[0] else float(samples[0][1])
            if i >= len(samples):
                return None
            (t0, s0), (t1, s1) = samples[i - 1], samples[i]
            if t1 == t0:
                return float(s1)
            return s0 + (s1 - s0) * (w - t0) / (t1 - t0)

        a, b = at(w0_ns), at(w1_ns)
        if a is None or b is None:
            return None
        return max(0.0, b - a)


class RunAnalyzer:
    def __init__(self, log_dir: pathlib.Path, config_dir: pathlib.Path) -> None:
        self.log_dir = log_dir
        self.config_dir = config_dir
        self.benchmark = json.loads((config_dir / "benchmark.json").read_text(encoding="utf-8"))
        self.seed = int(self.benchmark["seed"])
        self.measure_seconds = int(self.benchmark["measure_seconds"])
        self.cells = {cell["name"]: cell for cell in bench.CELLS}
        self.ue_cell: dict[str, str] = {}
        self.ue_meta: dict[str, dict[str, Any]] = {}
        for name, meta in self.benchmark["cells"].items():
            for ue in meta["ues"]:
                self.ue_cell[ue["device_id"]] = name
                self.ue_meta[ue["device_id"]] = ue
        self.gnb_cell = {cell["gnb"]["device_id"]: cell["name"] for cell in bench.CELLS}
        self.pci_cell = {cell["gnb"]["pci"]: cell["name"] for cell in bench.CELLS}
        self.anchor_ms: int | None = None
        self.traffic_start_ms: int | None = None
        self.flows: dict[int, FlowState] = {}
        self.rnti: dict[str, dict[str, str]] = {name: {} for name in self.cells}  # cell -> rnti -> ue
        self.sched = {name: CellSched() for name in self.cells}
        self.gnb_metrics: dict[str, dict[int, dict[str, list[dict[str, Any]]]]] = {name: {} for name in self.cells}
        self.cell_metrics: dict[str, dict[int, list[dict[str, Any]]]] = {name: {} for name in self.cells}
        self.ue_side: dict[str, dict[int, dict[str, float]]] = {ue: {} for ue in self.ue_cell}
        self.sionna: list[dict[str, Any]] = []
        self.sionna_digest_mismatch = 0
        self.readers: dict[str, TailReader] = {}
        self.ue_start_ms: dict[str, int] = {}
        self.sender_reports: dict[str, dict[str, Any]] = {}
        self.timezone_offset_suspect = False

    # --- helpers ---------------------------------------------------------------
    def reader(self, name: str, **kwargs: Any) -> TailReader:
        if name not in self.readers:
            self.readers[name] = TailReader(self.log_dir / name, **kwargs)
        return self.readers[name]

    def segment_of_ms(self, unix_ms: float) -> int | None:
        if self.anchor_ms is None:
            return None
        k = math.floor((unix_ms - self.anchor_ms) / 1000.0)
        return k if 0 <= k < self.measure_seconds else None

    def build_flows(self) -> None:
        if self.flows or self.traffic_start_ms is None:
            return
        duration = float(self.benchmark.get("traffic_duration_s", self.measure_seconds))
        for cell_index, cell in enumerate(bench.CELLS):
            for ue in self.benchmark["cells"][cell["name"]]["ues"]:
                for flow_index, flow in enumerate(ue["flows"]):
                    fid = bench.flow_id(cell_index, ue["cell_index"], flow_index)
                    offsets = array.array("d", traffic.flow_schedule(self.seed, ue["cell_index"], flow, duration))
                    self.flows[fid] = FlowState(fid, ue["device_id"], flow, ue["qos"]["pdb_ms"], offsets)

    # --- ingestion ---------------------------------------------------------------
    def poll(self) -> None:
        self.poll_sionna()
        self.poll_traffic_meta()
        self.build_flows()
        self.poll_rnti()
        for gnb, cell in self.gnb_cell.items():
            self.poll_sched(cell, gnb)
            self.poll_gnb_metrics(cell)
        self.poll_ue_metrics()
        self.poll_senders()
        self.poll_receivers()

    def poll_sionna(self) -> None:
        for line in self.reader("sionna-status.jsonl").lines():
            if '"sionna_rt_update"' not in line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            timeline = record.get("timeline") or {}
            if timeline.get("phase") != "run":
                continue
            if self.anchor_ms is None and timeline.get("anchor_unix_ms"):
                self.anchor_ms = int(timeline["anchor_unix_ms"])
            fanout = record.get("fanout") or []
            channels = {}
            for status in record.get("channels") or []:
                if isinstance(status, dict) and status.get("link_id"):
                    # total_path_power_db is raw Sionna path power; the tap gain
                    # is what the emulator applies (after --gain-offset-db).
                    channels[status["link_id"]] = {
                        "path_power_db": status.get("total_path_power_db"),
                        "tap_gain_db": status.get("strongest_tap_gain_db"),
                        "rays": status.get("ray_count"),
                        "taps": status.get("tap_count"),
                    }
            self.sionna.append({
                "grid_index": timeline.get("grid_index"),
                "scenario_time_s": timeline.get("scenario_time_s"),
                "lag_ms": timeline.get("lag_ms"),
                "skipped": timeline.get("skipped_grid_points"),
                "positions": record.get("positions"),
                "digest": record.get("profile_sha256"),
                "generation_ms": (record.get("timing_ms") or {}).get("channel_generation"),
                "fanout_ack_ms": [item.get("control_ack_unix_ms") for item in fanout],
                "fanout_ok": all(item.get("ok") for item in fanout) if fanout else None,
                "channels": channels,
            })

    def poll_traffic_meta(self) -> None:
        if self.traffic_start_ms is not None:
            return
        path = self.log_dir / "traffic-start.json"
        if path.exists():
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return
            self.traffic_start_ms = int(meta["start_at_unix_ms"])
            self.benchmark["traffic_duration_s"] = float(meta["duration_s"])

    def poll_rnti(self) -> None:
        for ue, cell in self.ue_cell.items():
            for line in self.reader(f"srsue-{ue}.log").lines():
                match = RNTI_LINE.search(line)
                if match:
                    self.rnti[cell][match.group(1)] = ue
            start = self.log_dir / f"srsue-{ue}.start_unix_ms"
            if ue not in self.ue_start_ms and start.exists():
                try:
                    self.ue_start_ms[ue] = int(start.read_text().strip())
                except ValueError:
                    pass

    def poll_sched(self, cell: str, gnb: str) -> None:
        state = self.sched[cell]
        for line in self.reader(f"{gnb}-internal.log").lines():
            if "Slot decisions" not in line:
                if "nof_crbs:" in line:
                    match = NOF_CRBS.search(line)
                    if match:
                        state.nof_prb = int(match.group(1))
                continue
            match = SLOT_LINE.match(line)
            if not match:
                continue
            state.lines += 1
            wall_ns = parse_log_time_ns(match.group(1))
            abs_slot = state.abs_slot(int(match.group(2)), int(match.group(3)))
            if not state.slot_samples or wall_ns - state.slot_samples[-1][0] >= 50_000_000:
                state.slot_samples.append((wall_ns, abs_slot))
            k = self.segment_of_ms(wall_ns / 1e6)
            entries = match.group(6)
            decision_us = float(match.group(5))
            rnti_map = self.rnti[cell]
            state.recent.append((
                abs_slot, wall_ns, decision_us,
                [(rnti_map.get(m.group(2), "other"), int(m.group(3)), int(m.group(4)), m.group(5) == "true")
                 for m in DL_ENTRY.finditer(entries)]
                + [("common", int(m.group(1)), int(m.group(2)), True) for m in COMMON_ENTRY.finditer(entries)],
                [(rnti_map.get(m.group(2), "other"), int(m.group(3)), int(m.group(4)), m.group(5) == "true")
                 for m in UL_ENTRY.finditer(entries)],
            ))
            if k is None:
                # Still track grant times so the first in-window gap is right.
                self._track_gaps(state, None, abs_slot, entries, cell)
                continue
            seg = state.segment(k)
            seg["decision_us"].append(decision_us)
            state.decision_us_all.append(decision_us)
            seg["first_slot"] = abs_slot if seg["first_slot"] is None else min(seg["first_slot"], abs_slot)
            seg["last_slot"] = abs_slot if seg["last_slot"] is None else max(seg["last_slot"], abs_slot)
            for common in COMMON_ENTRY.finditer(entries):
                seg["dl_common_prb"] += int(common.group(2)) - int(common.group(1))
            self._track_gaps(state, k, abs_slot, entries, cell)

    def _track_gaps(self, state: CellSched, k: int | None, abs_slot: int, entries: str, cell: str) -> None:
        rnti_map = self.rnti[cell]
        for dl in DL_ENTRY.finditer(entries):
            ue = rnti_map.get(dl.group(2))
            if ue is None:
                continue
            prbs = int(dl.group(4)) - int(dl.group(3))
            newtx = dl.group(5) == "true"
            tbs = int(dl.group(6))
            if k is not None:
                useg = state.ue_segment(k, ue)
                state.segment(k)["dl_prb"] += prbs
                useg["dl_prb"] += prbs
                useg["dl_grants"] += 1
                useg["dl_newtx_bytes" if newtx else "dl_retx_bytes"] += tbs
            if newtx:
                previous = state.last_dl_newtx.get(ue)
                if previous is not None and previous[1] > 0 and k is not None:
                    wait_ms = (abs_slot - previous[0]) * 1000.0 / SLOTS_PER_SECOND
                    if wait_ms >= 0:
                        state.ue_segment(k, ue)["dl_wait_ms"].append(wait_ms)
                bo = int(dl.group(7)) if dl.group(7) is not None else 0
                state.last_dl_newtx[ue] = (abs_slot, bo)
        for ul in UL_ENTRY.finditer(entries):
            ue = rnti_map.get(ul.group(2))
            if ue is None:
                continue
            prbs = int(ul.group(4)) - int(ul.group(3))
            newtx = ul.group(5) == "true"
            tbs = int(ul.group(6))
            if k is not None:
                useg = state.ue_segment(k, ue)
                state.segment(k)["ul_prb"] += prbs
                useg["ul_prb"] += prbs
                useg["ul_grants"] += 1
                useg["ul_newtx_bytes" if newtx else "ul_retx_bytes"] += tbs
            if newtx:
                previous_ul = state.last_ul_newtx.get(ue)
                if previous_ul is not None and k is not None:
                    gap = (abs_slot - previous_ul) * 1000.0 / SLOTS_PER_SECOND
                    if gap >= 0:
                        state.ue_segment(k, ue)["ul_gap_ms"].append(gap)
                state.last_ul_newtx[ue] = abs_slot

    def poll_gnb_metrics(self, cell: str) -> None:
        for line in self.reader(f"gnb-metrics-{cell}.jsonl").lines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = record.get("payload") or {}
            k = self.segment_of_ms(record.get("rx_unix_ms", 0))
            for cell_entry in payload.get("cells") or []:
                if k is None:
                    continue
                if "cell_metrics" in cell_entry:
                    self.cell_metrics[cell].setdefault(k, []).append(cell_entry["cell_metrics"])
                for ue_row in cell_entry.get("ue_list") or []:
                    rnti = ue_row.get("rnti")
                    rnti_hex = f"0x{int(rnti):x}" if isinstance(rnti, int) else str(rnti)
                    ue = self.rnti[cell].get(rnti_hex)
                    if ue is not None:
                        self.gnb_metrics[cell].setdefault(k, {}).setdefault(ue, []).append(ue_row)

    def poll_ue_metrics(self) -> None:
        for ue in self.ue_cell:
            start = self.ue_start_ms.get(ue)
            reader = self.reader(f"srsue-metrics-{ue}.csv")
            for line in reader.lines():
                if not line or line.startswith("time;"):
                    if line.startswith("time;"):
                        reader.header = line.split(";")  # type: ignore[attr-defined]
                    continue
                header = getattr(reader, "header", None)
                if header is None or start is None:
                    continue
                fields = line.split(";")
                if len(fields) != len(header):
                    continue
                row = dict(zip(header, fields))
                try:
                    unix_ms = start + float(row["time"])
                    k = self.segment_of_ms(unix_ms)
                    if k is None:
                        continue
                    self.ue_side[ue][k] = {
                        "pci": float(row["pci"]), "dl_snr": float(row["dl_snr"]),
                        "dl_mcs": float(row["dl_mcs"]), "rsrp": float(row["rsrp"]),
                        "dl_bler": float(row["dl_bler"]), "ul_bler": float(row["ul_bler"]),
                        "cpu": [float(row[key]) for key in header if key.startswith("cpu_")],
                    }
                except (KeyError, ValueError):
                    continue

    def poll_senders(self) -> None:
        names = ["tx-dl.csv"] + [f"tx-ul-{ue}.csv" for ue in self.ue_cell]
        for name in names:
            for line in self.reader(name).lines():
                if not line or line.startswith("t_ns"):
                    continue
                try:
                    _, fid, seq = (int(v) for v in line.split(","))
                except ValueError:
                    continue
                flow = self.flows.get(fid)
                if flow is not None:
                    flow.sent_next_seq = max(flow.sent_next_seq, seq)
        for name in ["traffic-dl-send.log"] + [f"traffic-ul-send-{ue}.log" for ue in self.ue_cell]:
            path = self.log_dir / name
            if path.exists():
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    if '"traffic_sender_done"' in line:
                        try:
                            done = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        self.sender_reports[name] = done
                        for fid, seq in (done.get("next_seq") or {}).items():
                            flow = self.flows.get(int(fid))
                            if flow is not None:
                                flow.sent_next_seq = max(flow.sent_next_seq, int(seq))
                                flow.sender_done = True

    def poll_receivers(self) -> None:
        # Segments need the grid anchor; leave the files unread until it exists.
        if not self.flows or self.traffic_start_ms is None or self.anchor_ms is None:
            return
        names = [f"rx-dl-{ue}.bin" for ue in self.ue_cell] + ["rx-ul.bin"]
        record = traffic.RECORD
        for name in names:
            data = self.reader(name, binary=True, record_size=record.size).read()
            for fid, _, seq, tx_ns, rx_ns in record.iter_unpack(data):
                flow = self.flows.get(fid)
                if flow is None or seq >= len(flow.offsets):
                    if flow is not None:
                        flow.unknown_seq += 1
                    continue
                if flow.received[seq]:
                    flow.duplicates += 1
                    continue
                flow.received[seq] = 1
                k_nominal = self.nominal_segment(flow, seq)
                if k_nominal is not None:
                    flow.received_by_seg[k_nominal] = flow.received_by_seg.get(k_nominal, 0) + 1
                delay = (rx_ns - tx_ns) / 1e6
                k_rx = self.segment_of_ms(rx_ns / 1e6)
                # Delay and lateness are filed under the packet's scheduled
                # segment, the same key its sent count uses.
                if k_nominal is not None:
                    flow.delay_ms.setdefault(k_nominal, array.array("f")).append(delay)
                    flow.all_delays.append(delay)
                if k_rx is not None:
                    flow.rx_bytes[k_rx] = flow.rx_bytes.get(k_rx, 0) + flow.flow["size_bytes"]

    # --- metrics -----------------------------------------------------------------
    def ensure_bounds(self, flow: FlowState) -> list[int] | None:
        """Seq range of each segment, from the (time-ordered) send schedule."""

        if flow.bounds is None and self.traffic_start_ms is not None and self.anchor_ms is not None:
            # Segment k starts at scenario second k = traffic offset k + (anchor - start).
            shift = (self.anchor_ms - self.traffic_start_ms) / 1000.0
            flow.bounds = [bisect.bisect_left(flow.offsets, k + shift) for k in range(self.measure_seconds + 1)]
        return flow.bounds

    def nominal_segment(self, flow: FlowState, seq: int) -> int | None:
        bounds = self.ensure_bounds(flow)
        if bounds is None or seq < bounds[0] or seq >= bounds[-1]:
            return None
        return bisect.bisect_right(bounds, seq) - 1

    def sent_by_segment(self, flow: FlowState, now_ms: float, live: bool) -> dict[int, tuple[int, int]]:
        """segment -> (sent, received) over packets whose fate is known.

        A packet counts once it was sent (seq below the sender's progress) and,
        on a live run, only in segments older than the loss horizon, so a
        packet still queued is not yet called lost.
        """

        bounds = self.ensure_bounds(flow)
        if bounds is None:
            return {}
        out = {}
        last_k = self.measure_seconds
        if live:
            last_k = min(last_k, math.floor((now_ms - LIVE_LOSS_HORIZON_S * 1000.0 - self.anchor_ms) / 1000.0))
        for k in range(max(0, last_k)):
            lo, hi = bounds[k], min(bounds[k + 1], flow.sent_next_seq)
            if hi <= lo:
                continue
            out[k] = (hi - lo, flow.received_by_seg.get(k, 0))
        return out

    def segment_validity(self, k: int) -> dict[str, Any]:
        """Every UE of both cells in service for second k.

        gNB side (live and final): the UE is in that second's scheduler report
        (or a neighbouring one: a 1 s report straddles segments) and its DL
        HARQ is not failing outright. UE side, when the srsUE metrics CSV has
        rows (srsUE buffers that file and writes it at exit, so only final
        reports have it): camped on its own PCI with a positive DL SNR.
        """

        reasons = []
        for ue, cell in self.ue_cell.items():
            if any(self.gnb_metrics[cell].values()):
                rows = [row for j in (k, k - 1, k + 1) for row in (self.gnb_metrics[cell].get(j) or {}).get(ue, [])]
                if not rows:
                    reasons.append(f"{ue}: not in the gNB report")
                elif all(row.get("dl_nof_ok", 0) == 0 and row.get("dl_nof_nok", 0) > 0 for row in rows):
                    reasons.append(f"{ue}: every DL HARQ failed")
            if not self.ue_side[ue]:
                continue
            expected_pci = self.cells[cell]["gnb"]["pci"]
            side = self.ue_side[ue].get(k)
            if side is None:
                # The CSV row may land in the neighbouring second; accept either.
                side = self.ue_side[ue].get(k - 1) or self.ue_side[ue].get(k + 1)
            if side is None:
                reasons.append(f"{ue}: no UE metrics row")
            elif int(side["pci"]) != expected_pci or side["dl_snr"] <= 0:
                reasons.append(f"{ue}: out of service (pci={int(side['pci'])}, snr={side['dl_snr']:.0f})")
        for cell, state in self.sched.items():
            seg = state.seg.get(k)
            if seg is None or seg["first_slot"] is None:
                reasons.append(f"cell {cell}: no scheduler activity")
        return {"valid": not reasons, "reasons": reasons}

    def report(self, live: bool = False) -> dict[str, Any]:
        now_ms = time.time() * 1000.0
        anchor = self.anchor_ms
        complete = 0
        if anchor is not None:
            elapsed = (now_ms - anchor) / 1000.0 if live else self.measure_seconds
            complete = max(0, min(self.measure_seconds, math.floor(elapsed) - (1 if live else 0)))
        segments = []
        flow_seg = {fid: self.sent_by_segment(flow, now_ms, live) for fid, flow in self.flows.items()}
        for k in range(complete):
            entry = {"k": k, **self.segment_validity(k), "cells": {}}
            for cell in self.cells:
                entry["cells"][cell] = self.segment_cell_metrics(k, cell, flow_seg)
            segments.append(entry)
        valid = [s for s in segments if s["valid"]]
        # Keyed by cell, not by scheduler: an A/A control runs the same policy
        # in both cells and must still report both.
        lanes = {}
        for cell, meta in self.benchmark["cells"].items():
            lanes[cell] = {
                "scheduler": meta["scheduler"],
                "info": meta["scheduler_info"],
                "metrics": self.run_cell_metrics(cell, valid, flow_seg),
            }
        return {
            "kind": "ocudu-scheduler-benchmark-report",
            "generated_unix_ms": int(now_ms),
            "live": live,
            "seed": self.seed,
            "anchor_unix_ms": anchor,
            "traffic_start_unix_ms": self.traffic_start_ms,
            "measure_seconds": self.measure_seconds,
            "segments_complete": complete,
            "segments_valid": len(valid),
            "definitions": METRIC_DEFINITIONS,
            "lanes": lanes,
            "comparison": self.compare(valid, lanes),
            "segments": segments,
            "checks": self.checks(),
            "sionna": self.sionna_summary(),
        }

    def segment_cell_metrics(self, k: int, cell: str, flow_seg: dict[int, dict[int, tuple[int, int]]]) -> dict[str, Any]:
        state = self.sched[cell]
        seg = state.seg.get(k) or {}
        w0 = (self.anchor_ms + k * 1000) * 1_000_000
        slots = state.slots_between(w0, w0 + 1_000_000_000)
        ues = {}
        for ue in (u for u, c in self.ue_cell.items() if c == cell):
            useg = (seg.get("ue") or {}).get(ue) or {}
            flows = {}
            for fid, flow in self.flows.items():
                if flow.ue != ue:
                    continue
                sent, received = flow_seg[fid].get(k, (0, 0))
                delays = list(flow.delay_ms.get(k, ()))
                late = sum(1 for d in delays if d > flow.pdb_ms)
                flows[flow.flow["name"]] = {
                    "direction": flow.flow["direction"], "sent": sent, "received": received,
                    "rx_mbps": flow.rx_bytes.get(k, 0) * 8 / 1e6,
                    "delay_ms": summary(delays), "late": late,
                }
            gm = (self.gnb_metrics[cell].get(k) or {}).get(ue) or []
            ues[ue] = {
                "flows": flows,
                "dl_rx_mbps": sum(f["rx_mbps"] for f in flows.values() if f["direction"] == "dl"),
                "ul_rx_mbps": sum(f["rx_mbps"] for f in flows.values() if f["direction"] == "ul"),
                "dl_prb": useg.get("dl_prb", 0), "ul_prb": useg.get("ul_prb", 0),
                "dl_newtx_mbps": useg.get("dl_newtx_bytes", 0) * 8 / 1e6,
                "ul_newtx_mbps": useg.get("ul_newtx_bytes", 0) * 8 / 1e6,
                "dl_wait_ms_max": max(useg.get("dl_wait_ms") or [0.0]),
                "starvation_events": sum(1 for w in useg.get("dl_wait_ms") or [] if w > STARVATION_THRESHOLD_MS),
                "cqi": _mean_valid([row.get("cqi") for row in gm], lambda v: v >= 0),
                "dl_mcs": _mean_valid([row.get("dl_mcs") for row in gm]),
                "ul_mcs": _mean_valid([row.get("ul_mcs") for row in gm]),
                "pusch_snr_db": _mean_valid([row.get("pusch_snr_db") for row in gm], lambda v: -99.9 < v < 99.9),
            }
        dl_prb = seg.get("dl_prb", 0) + seg.get("dl_common_prb", 0)
        capacity = (slots or 0) * state.nof_prb
        return {
            "slots": slots,
            "dl_prb_util": dl_prb / capacity if capacity else None,
            "ul_prb_util": seg.get("ul_prb", 0) / capacity if capacity else None,
            "decision_us_mean": (sum(seg["decision_us"]) / len(seg["decision_us"])) if seg.get("decision_us") else None,
            "dl_rx_mbps": sum(u["dl_rx_mbps"] for u in ues.values()),
            "ul_rx_mbps": sum(u["ul_rx_mbps"] for u in ues.values()),
            "jain_dl": jain([u["dl_rx_mbps"] for u in ues.values()]),
            "ues": ues,
        }

    def run_cell_metrics(self, cell: str, valid: list[dict[str, Any]],
                         flow_seg: dict[int, dict[int, tuple[int, int]]]) -> dict[str, Any]:
        state = self.sched[cell]
        ks = [s["k"] for s in valid]
        n = len(ks)
        cell_ues = [u for u, c in self.ue_cell.items() if c == cell]
        per_ue = {}
        dl_tput, dl_norm, dl_prb_share = [], [], []
        total_dl_prb = sum(state.seg.get(k, {}).get("ue", {}).get(u, {}).get("dl_prb", 0) for k in ks for u in cell_ues)
        for ue in cell_ues:
            meta = self.ue_meta[ue]
            ue_flows = {fid: f for fid, f in self.flows.items() if f.ue == ue}
            rx = {"dl": 0.0, "ul": 0.0}
            offered = {"dl": 0.0, "ul": 0.0}
            delays = {"dl": [], "ul": []}
            sent = received = late = 0
            per_flow = {}
            for fid, flow in ue_flows.items():
                direction = flow.flow["direction"]
                f_sent = f_recv = f_late = 0
                f_delays: list[float] = []
                f_rx_bytes = 0
                for k in ks:
                    s, r = flow_seg[fid].get(k, (0, 0))
                    f_sent += s
                    f_recv += r
                    d = flow.delay_ms.get(k, ())
                    f_delays.extend(d)
                    f_late += sum(1 for x in d if x > flow.pdb_ms)
                    f_rx_bytes += flow.rx_bytes.get(k, 0)
                rx[direction] += f_rx_bytes * 8 / 1e6 / n if n else 0.0
                offered[direction] += bench.offered_rate_mbps(flow.flow)
                delays[direction].extend(f_delays)
                sent += f_sent
                received += f_recv
                late += f_late
                per_flow[flow.flow["name"]] = {
                    "direction": direction, "offered_mbps": bench.offered_rate_mbps(flow.flow),
                    "throughput_mbps": f_rx_bytes * 8 / 1e6 / n if n else None,
                    "sent": f_sent, "received": f_recv, "late": f_late,
                    "drop_rate": (f_sent - f_recv) / f_sent if f_sent else None,
                    "pdb_violation_rate": f_late / f_sent if f_sent else None,
                    "delay_ms": summary(f_delays),
                }
            useg = [state.seg.get(k, {}).get("ue", {}).get(ue, {}) for k in ks]
            waits = [w for u in useg for w in u.get("dl_wait_ms", [])]
            ul_gaps = [w for u in useg for w in u.get("ul_gap_ms", [])]
            ue_dl_prb = sum(u.get("dl_prb", 0) for u in useg)
            gm_rows = [row for k in ks for row in (self.gnb_metrics[cell].get(k) or {}).get(ue, [])]
            dl_ok = sum(row.get("dl_nof_ok", 0) for row in gm_rows)
            dl_nok = sum(row.get("dl_nof_nok", 0) for row in gm_rows)
            ul_ok = sum(row.get("ul_nof_ok", 0) for row in gm_rows)
            ul_nok = sum(row.get("ul_nof_nok", 0) for row in gm_rows)
            per_ue[ue] = {
                "role": meta["role"], "five_qi": meta["five_qi"], "pdb_ms": meta["qos"]["pdb_ms"],
                "twin": meta["twin"],
                "throughput_mbps": {"dl": rx["dl"], "ul": rx["ul"]},
                "offered_mbps": offered,
                "mac_newtx_mbps": {
                    "dl": sum(u.get("dl_newtx_bytes", 0) for u in useg) * 8 / 1e6 / n if n else None,
                    "ul": sum(u.get("ul_newtx_bytes", 0) for u in useg) * 8 / 1e6 / n if n else None},
                "delay_ms": {"dl": summary(delays["dl"]), "ul": summary(delays["ul"])},
                "sent": sent, "received": received, "late": late,
                "drop_rate": (sent - received) / sent if sent else None,
                "pdb_violation_rate": late / sent if sent else None,
                "dl_prb_share": ue_dl_prb / total_dl_prb if total_dl_prb else None,
                "starvation": {
                    "dl_wait_ms": summary(waits),
                    "events": sum(1 for w in waits if w > STARVATION_THRESHOLD_MS),
                    "events_per_s": sum(1 for w in waits if w > STARVATION_THRESHOLD_MS) / n if n else None,
                    "ul_gap_ms": summary(ul_gaps),
                },
                "link": {
                    "cqi": _mean_valid([r.get("cqi") for r in gm_rows], lambda v: v >= 0),
                    "dl_mcs": _mean_valid([r.get("dl_mcs") for r in gm_rows]),
                    "ul_mcs": _mean_valid([r.get("ul_mcs") for r in gm_rows]),
                    "dl_bler": dl_nok / (dl_ok + dl_nok) if dl_ok + dl_nok else None,
                    "ul_bler": ul_nok / (ul_ok + ul_nok) if ul_ok + ul_nok else None,
                    "pusch_snr_db": _mean_valid([r.get("pusch_snr_db") for r in gm_rows], lambda v: -99.9 < v < 99.9),
                },
                "flows": per_flow,
            }
            dl_tput.append(rx["dl"])
            dl_norm.append(rx["dl"] / offered["dl"] if offered["dl"] else None)
            dl_prb_share.append(per_ue[ue]["dl_prb_share"])
        segs = [state.seg.get(k, {}) for k in ks]
        slots = [state.slots_between((self.anchor_ms + k * 1000) * 1_000_000,
                                     (self.anchor_ms + k * 1000 + 1000) * 1_000_000) or 0 for k in ks]
        capacity = sum(slots) * state.nof_prb
        dl_prb_total = sum(s.get("dl_prb", 0) + s.get("dl_common_prb", 0) for s in segs)
        ul_prb_total = sum(s.get("ul_prb", 0) for s in segs)
        dl_newtx_bits = sum(u.get("dl_newtx_bytes", 0) for s in segs for u in s.get("ue", {}).values()) * 8
        ul_newtx_bits = sum(u.get("ul_newtx_bytes", 0) for s in segs for u in s.get("ue", {}).values()) * 8
        ue_dl_prb_total = sum(s.get("dl_prb", 0) for s in segs)
        ue_ul_prb_total = sum(s.get("ul_prb", 0) for s in segs)
        decision = [d for s in segs for d in s.get("decision_us", [])]
        agg_dl = sum(dl_tput)
        agg_ul = sum(per_ue[u]["throughput_mbps"]["ul"] for u in cell_ues)
        cm_rows = [row for k in ks for row in self.cell_metrics[cell].get(k, [])]
        return {
            "segments": n,
            "aggregate_throughput_mbps": {
                "dl": agg_dl if n else None, "ul": agg_ul if n else None,
                "mac_dl": dl_newtx_bits / 1e6 / n if n else None, "mac_ul": ul_newtx_bits / 1e6 / n if n else None},
            "jain_fairness": {
                "dl_throughput": jain(dl_tput) if n else None,
                "dl_throughput_over_offered": jain([v for v in dl_norm if v is not None]) if n else None,
                "dl_prb_share": jain([v for v in dl_prb_share if v is not None]) if n else None,
            },
            "resource_utilization": {
                "dl_prb": dl_prb_total / capacity if capacity else None,
                "ul_prb": ul_prb_total / capacity if capacity else None,
                "slots_per_s": sum(slots) / n if n else None,
                "nof_prb": state.nof_prb,
            },
            "spectral_efficiency_bps_hz": {
                "dl_app": agg_dl * 1e6 / CHANNEL_BANDWIDTH_HZ if n else None,
                "ul_app": agg_ul * 1e6 / CHANNEL_BANDWIDTH_HZ if n else None,
                "dl_mac": dl_newtx_bits / n / CHANNEL_BANDWIDTH_HZ if n else None,
                "ul_mac": ul_newtx_bits / n / CHANNEL_BANDWIDTH_HZ if n else None,
                # TBS over the bandwidth actually granted, in PRB-slots.
                "dl_per_used_prb": dl_newtx_bits / (ue_dl_prb_total * PRB_BANDWIDTH_HZ / SLOTS_PER_SECOND)
                if ue_dl_prb_total else None,
                "ul_per_used_prb": ul_newtx_bits / (ue_ul_prb_total * PRB_BANDWIDTH_HZ / SLOTS_PER_SECOND)
                if ue_ul_prb_total else None,
            },
            "decision_latency_us": summary(decision),
            "cell_metrics": {
                "average_latency_us": _mean_valid([r.get("average_latency") for r in cm_rows]),
                "max_latency_us": max((r.get("max_latency", 0) for r in cm_rows), default=None),
                "late_dl_harqs": sum(r.get("late_dl_harqs", 0) for r in cm_rows),
            },
            "gbr_satisfaction": {"value": None, "reason": METRIC_DEFINITIONS["gbr_satisfaction"]},
            "per_ue": per_ue,
        }

    def compare(self, valid: list[dict[str, Any]], lanes: dict[str, Any]) -> dict[str, Any]:
        """Paired per-segment comparison of the two cells (same seconds, same channel)."""

        out = self._pairs(valid, "a", "b")
        a, b = lanes["a"]["scheduler"], lanes["b"]["scheduler"]
        out["order"] = ["a", "b"]
        out["note"] = (f"x = cell a ({a}), y = cell b ({b}); difference x - y per valid segment; "
                       "95% CI by batch means over consecutive segments")
        if a == b:
            out["note"] += ". Both cells run the same policy (A/A control): differences measure cell bias."
        return out

    def _pairs(self, valid: list[dict[str, Any]], cell_x: str, cell_y: str) -> dict[str, Any]:
        def series(extract) -> dict[str, Any]:
            xs, ys = [], []
            for seg in valid:
                x, y = extract(seg["cells"][cell_x], cell_x), extract(seg["cells"][cell_y], cell_y)
                if x is None or y is None:
                    continue
                xs.append(x)
                ys.append(y)
            diffs = [x - y for x, y in zip(xs, ys)]
            return {
                "n": len(diffs),
                "mean_x": sum(xs) / len(xs) if xs else None,
                "mean_y": sum(ys) / len(ys) if ys else None,
                "mean_diff": sum(diffs) / len(diffs) if diffs else None,
                "ci95": batch_means_ci(diffs),
                "batch_length": bootstrap_block_length(diffs) if len(diffs) >= 2 * BOOTSTRAP_BLOCK else None,
                "x_higher": sum(1 for d in diffs if d > 0), "y_higher": sum(1 for d in diffs if d < 0),
                "ties": sum(1 for d in diffs if d == 0),
            }

        def ue_index(cell: str, index: int) -> str:
            return self.benchmark["cells"][cell]["ues"][index]["device_id"]

        def flow_delay_p99(seg: dict[str, Any], cell: str, index: int) -> float | None:
            flows = seg["ues"][ue_index(cell, index)]["flows"]
            values = [f["delay_ms"]["p99"] for f in flows.values() if f["direction"] == "dl" and f["delay_ms"]["p99"] is not None]
            return max(values) if values else None

        def violation(seg: dict[str, Any], cell: str, index: int) -> float | None:
            flows = seg["ues"][ue_index(cell, index)]["flows"]
            sent = sum(f["sent"] for f in flows.values())
            late = sum(f["late"] for f in flows.values())
            return late / sent if sent else None

        def drop(seg: dict[str, Any], cell: str, index: int) -> float | None:
            flows = seg["ues"][ue_index(cell, index)]["flows"]
            sent = sum(f["sent"] for f in flows.values())
            received = sum(f["received"] for f in flows.values())
            return (sent - received) / sent if sent else None

        result = {
            "aggregate_dl_mbps": series(lambda s, c: s["dl_rx_mbps"]),
            "jain_dl": series(lambda s, c: s["jain_dl"]),
            "dl_prb_util": series(lambda s, c: s["dl_prb_util"]),
            "decision_us": series(lambda s, c: s["decision_us_mean"]),
        }
        for index in range(2):
            result[f"ue{index}_dl_mbps"] = series(lambda s, c, i=index: s["ues"][ue_index(c, i)]["dl_rx_mbps"])
            result[f"ue{index}_dl_delay_p99_ms"] = series(lambda s, c, i=index: flow_delay_p99(s, c, i))
            result[f"ue{index}_pdb_violation"] = series(lambda s, c, i=index: violation(s, c, i))
            result[f"ue{index}_drop"] = series(lambda s, c, i=index: drop(s, c, i))
            result[f"ue{index}_dl_wait_max_ms"] = series(lambda s, c, i=index: s["ues"][ue_index(c, i)]["dl_wait_ms_max"])
        return result

    def checks(self) -> dict[str, Any]:
        """Instrument checks: each counter that must be non-zero if the run measured anything."""

        flows = {}
        for fid, flow in self.flows.items():
            flows[f"{flow.ue}.{flow.flow['name']}"] = {
                "scheduled": len(flow.offsets), "sent": flow.sent_next_seq, "sender_done": flow.sender_done,
                "received": int(sum(flow.received)), "duplicates": flow.duplicates, "unknown_seq": flow.unknown_seq,
            }
        rx_logs = {}
        for name in [f"traffic-dl-recv-{ue}.log" for ue in self.ue_cell] + ["traffic-ul-recv.log"]:
            path = self.log_dir / name
            if path.exists():
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    if '"traffic_receiver_done"' in line:
                        try:
                            rx_logs[name] = json.loads(line)
                        except json.JSONDecodeError:
                            pass
        digests = [s["digest"] for s in self.sionna if s["digest"]]
        load1, others = [], {}
        path = self.log_dir / "host-load.jsonl"
        if path.exists():
            benchmark_comms = ("gnb", "srsue", "ocudu-gpu-chann", "python3", "python", "5gc", "mongod", "stdbuf")
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    sample = json.loads(line)
                    load1.append(float(sample["loadavg"].split()[0]))
                except (json.JSONDecodeError, KeyError, ValueError, IndexError):
                    continue
                for proc in sample.get("top", []):
                    comm = str(proc.get("comm", ""))
                    if not comm.startswith(benchmark_comms) and float(proc.get("cpu", 0)) >= 20:
                        others[comm] = max(others.get(comm, 0.0), float(proc["cpu"]))
        return {
            # Load average during the measurement and any non-benchmark process
            # that held >= 20% of a CPU: the host is shared.
            "host_load": {"samples": len(load1), "load1_mean": sum(load1) / len(load1) if load1 else None,
                          "load1_max": max(load1) if load1 else None, "other_busy_processes": others},
            "sched_lines": {cell: state.lines for cell, state in self.sched.items()},
            "rnti_map": self.rnti,
            "flows": flows,
            "receivers": rx_logs,
            "senders": self.sender_reports,
            "sionna_updates": len(self.sionna),
            "sionna_digests": len(digests),
            "fanout_all_ok": all(s["fanout_ok"] for s in self.sionna if s["fanout_ok"] is not None),
            "timezone": "gNB log times parsed as UTC; the analyzer host's offset is "
                        f"{time.strftime('%z')}",
        }

    def sionna_summary(self) -> dict[str, Any]:
        lags = [s["lag_ms"] for s in self.sionna if s["lag_ms"] is not None]
        gens = [s["generation_ms"] for s in self.sionna if s["generation_ms"] is not None]
        skew = [abs(s["fanout_ack_ms"][0] - s["fanout_ack_ms"][1]) for s in self.sionna
                if len(s["fanout_ack_ms"]) == 2 and None not in s["fanout_ack_ms"]]
        return {
            "updates": len(self.sionna),
            "skipped_grid_points": self.sionna[-1]["skipped"] if self.sionna else None,
            "lag_ms": summary(lags),
            "generation_ms": summary(gens),
            "fanout_ack_skew_ms": summary(skew),
            "last": self.sionna[-1] if self.sionna else None,
        }

    def resource_grid(self) -> dict[str, Any]:
        """The last logged slots of each cell: who got which PRBs."""

        return {
            cell: {
                "nof_prb": state.nof_prb,
                "slots": [{"slot": s, "wall_ms": w // 1_000_000, "decision_us": d, "dl": dl, "ul": ul}
                          for s, w, d, dl, ul in state.recent],
            }
            for cell, state in self.sched.items()
        }

    def timeline(self, step: int = 1) -> list[dict[str, Any]]:
        """Positions and channel gains per Sionna update, for the UI."""

        return [{key: s[key] for key in ("grid_index", "scenario_time_s", "positions", "channels", "digest")}
                for s in self.sionna[::step]]


def _mean_valid(values: Iterable[Any], predicate=lambda v: True) -> float | None:
    usable = [float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(v) and predicate(v)]
    return sum(usable) / len(usable) if usable else None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log-dir", type=pathlib.Path, required=True)
    parser.add_argument("--config-dir", type=pathlib.Path, required=True)
    parser.add_argument("--out", type=pathlib.Path)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    analyzer = RunAnalyzer(args.log_dir, args.config_dir)
    analyzer.poll()
    report = analyzer.report(live=args.live)
    text = json.dumps(report, indent=1, sort_keys=True, default=_json_default)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    for cell, lane in report["lanes"].items():
        m = lane["metrics"]
        agg = m["aggregate_throughput_mbps"]
        print(f"cell={cell} scheduler={lane['scheduler']} segments={m['segments']} dl_mbps={agg['dl']} "
              f"jain_dl={m['jain_fairness']['dl_throughput']} dl_prb_util={m['resource_utilization']['dl_prb']}",
              file=sys.stderr)
    return 0


def _json_default(value: Any) -> Any:
    if isinstance(value, array.array):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(type(value).__name__)


if __name__ == "__main__":
    raise SystemExit(main())
