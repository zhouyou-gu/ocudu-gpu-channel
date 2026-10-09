#!/usr/bin/env python3
"""Per-fight table for a two-broker robot-fight run (R5b).

    analyze_battle.py --log-dir <log_dir> [--report r.json] [--markdown r.md]

Joins, per fight window (fights/summary.jsonl written by launch/root_exec.sh):

* the arena result (winner, reason, rtf) and per-robot one-way / stale
  statistics, the brains' RTT (echo based) and deadline misses;
* per cell broker (broker-a.log = ue0's cell, broker-b.log = ue1's cell): the
  emulator-call wall time `process_us` p50/p99/max from the per-second
  cpu_stage_timings lines (this is where another context's time slice or a
  queue of hog kernels shows; the stream-event kernel_us does not see it),
  kernel_us p50/p99, node_stall count, and per-second rx starvations when the
  heartbeat carries them (R4c), else the run total from event=stop;
* whether the fight ran before or under the contention source
  (contention.start_unix_ms);
* the two Sionna bridges' update counts in the window and their external
  position share.

Broker time base: the broker logs only carry `t=<s since start>`; the start is
taken from the log file's birth time (same approach as analyze_run.py, which
this module imports for the helpers).
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from analyze_run import birth_time_ms, percentile, read_jsonl, summarize  # noqa: E402

CELLS = {"a": "ue0", "b": "ue1"}


def wilson(k: int, n: int, z: float = 1.96):
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return {"share": p, "lo": c - h, "hi": c + h}


def load_broker(path: pathlib.Path, start_ms: int | None):
    """Per-second rows keyed by absolute ms: process_us, kernel_us, starvation deltas, stalls."""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if start_ms is None:
        start_ms = birth_time_ms(path)
    proc, kern, starv, stalls, stop = {}, {}, {}, [], {}
    starv_prev = {}
    for line in text:
        if line.startswith("event=cpu_stage_timings "):
            m = re.search(r" t=(\d+) node=(\S+) .*process_us=([0-9.]+)", line)
            if m:
                t = int(m.group(1))
                proc.setdefault(t, []).append(float(m.group(3)))
        elif line.startswith("event=gpu_timings "):
            m = re.search(r" t=(\d+) .*kernel_us=([0-9.]+)", line)
            if m:
                kern.setdefault(int(m.group(1)), []).append(float(m.group(2)))
        elif line.startswith("event=heartbeat "):
            m = re.search(r" t=(\d+) dev=(\S+)", line)
            if not m:
                continue
            t, dev = int(m.group(1)), m.group(2)
            d = re.search(r" starvations=(\d+)", line)
            if d:  # R4c per-second delta
                starv[t] = starv.get(t, 0) + int(d.group(1))
            else:
                tot = re.search(r" starvations_total=(\d+)", line)
                if tot:
                    prev = starv_prev.get(dev, int(tot.group(1)))
                    starv[t] = starv.get(t, 0) + (int(tot.group(1)) - prev)
                    starv_prev[dev] = int(tot.group(1))
        elif line.startswith("event=node_stall "):
            m = re.search(r" t=(\d+)", line)
            stalls.append(int(m.group(1)) if m else -1)
        elif line.startswith("event=stop "):
            for k, v in re.findall(r"(\w+)=(\d+)", line):
                stop[k] = int(v)
    return {"start_ms": start_ms, "proc": proc, "kern": kern, "starv": starv, "stalls": stalls, "stop": stop,
            "has_per_second_starvations": bool(starv)}


def broker_window(b, t0_ms: int, t1_ms: int):
    if b is None or b["start_ms"] is None:
        return None
    s0 = max(0, int((t0_ms - b["start_ms"]) / 1000))
    s1 = int(math.ceil((t1_ms - b["start_ms"]) / 1000))
    proc = [v for t in range(s0, s1 + 1) for v in b["proc"].get(t, [])]
    kern = [v for t in range(s0, s1 + 1) for v in b["kern"].get(t, [])]
    starv = sum(b["starv"].get(t, 0) for t in range(s0, s1 + 1)) if b["has_per_second_starvations"] else None
    stalls = sum(1 for t in b["stalls"] if s0 <= t <= s1)
    return {"process_us": summarize(proc), "kernel_us": summarize(kern), "starvations": starv,
            "node_stalls": stalls, "seconds": s1 - s0 + 1}


def bridge_window(rows, t0_ms: int, t1_ms: int):
    inside = [r for r in rows if t0_ms <= r.get("update_started_unix_ms", -1) <= t1_ms]
    ext = sum(1 for r in inside if r.get("position_source") == "external")
    gen = [r["timing_ms"]["channel_generation"] for r in inside if "timing_ms" in r]
    return {"updates": len(inside), "external": ext, "rate_hz": (len(inside) / max(1e-9, (t1_ms - t0_ms) / 1000)),
            "generation_ms_p50": percentile(gen, 50), "generation_ms_max": max(gen) if gen else None}


def analyse(log_dir: pathlib.Path, cells: dict[str, str]):
    fights = read_jsonl(log_dir / "fights" / "summary.jsonl")
    contention_ms = None
    contention_kind = None
    cpath = log_dir / "contention.start_unix_ms"
    params = log_dir.parent.parent.parent / "reports" / log_dir.parent.name / log_dir.name / "run-parameters.json"
    if params.exists():
        try:
            contention_kind = json.loads(params.read_text()).get("contention")
            if isinstance(contention_kind, dict):
                contention_kind = contention_kind.get("kind")
        except (ValueError, OSError):
            contention_kind = None
    if cpath.exists() and contention_kind not in (None, "none"):
        try:
            contention_ms = int(cpath.read_text().strip())
        except ValueError:
            contention_ms = None
    brokers = {c: load_broker(log_dir / f"broker-{c}.log", None) for c in cells}
    bridges = {}
    for c in cells:
        rows = [r for r in read_jsonl(log_dir / f"sionna-status-{c}.jsonl") if r.get("event") == "sionna_rt_update"]
        bridges[c] = rows
    ue_of_cell = dict(cells)
    cell_of_ue = {u: c for c, u in cells.items()}
    table = []
    for f in fights:
        t0, t1 = f.get("t_start_unix_ms"), f.get("t_end_unix_ms")
        if t0 is None or t1 is None:
            continue
        row = {"fight": f.get("fight"), "seed": f.get("seed"), "t_start_unix_ms": t0, "t_end_unix_ms": t1,
               "wall_s": (t1 - t0) / 1000, "winner_node": f.get("winner_node"), "reason": f.get("reason"),
               "rtf": f.get("rtf"), "late_loops": f.get("late_loops"), "max_lag_ms": f.get("max_lag_ms"),
               "under_contention": (contention_ms is not None and t0 >= contention_ms),
               "arena_error": f.get("arena_error")}
        robots = {}
        for r in f.get("robots") or []:
            ue = r.get("node_id")
            robots[ue] = {"one_way_us_p50": (r.get("one_way_us") or {}).get("p50"),
                          "one_way_us_p99": (r.get("one_way_us") or {}).get("p99"),
                          "stale_intervals": r.get("stale_intervals"), "stale_total_s": r.get("stale_total_s"),
                          "cmds_received": r.get("cmds_received"), "cmd_seq_gaps": r.get("cmd_seq_gaps"),
                          "lost_by": r.get("lost_by") or r.get("loss_reason")}
        for b in f.get("brains") or []:
            ue = cells.get("a") if b.get("robot_id") == 0 else cells.get("b")
            robots.setdefault(ue, {}).update({
                "rtt_us_p50": (b.get("rtt_us") or {}).get("p50"), "rtt_us_p99": (b.get("rtt_us") or {}).get("p99"),
                "rtt_us_max": (b.get("rtt_us") or {}).get("max"),
                "deadline_misses": b.get("deadline_misses"), "state_seq_gaps": b.get("state_seq_gaps"),
                "ticks_without_fresh_state": b.get("ticks_without_fresh_state"), "outcome": b.get("outcome")})
        row["robots"] = robots
        row["brokers"] = {c: broker_window(brokers[c], t0, t1) for c in cells}
        row["bridges"] = {c: bridge_window(bridges[c], t0, t1) for c in cells}
        table.append(row)

    def cell_summary(subset):
        n = len(subset)
        wins = {u: sum(1 for r in subset if r["winner_node"] == u) for u in cells.values()}
        draws = sum(1 for r in subset if r["winner_node"] is None)
        reasons = {}
        for r in subset:
            reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
        loser_by = {}
        for r in subset:
            w = r["winner_node"]
            for u, info in r["robots"].items():
                if u != w and w is not None:
                    key = f"{u}:{info.get('lost_by') or r['reason']}"
                    loser_by[key] = loser_by.get(key, 0) + 1
        out = {"fights": n, "wins": wins, "draws": draws, "reasons": reasons, "loser_by": loser_by,
               "ue0_share": wilson(wins.get("ue0", 0), n - draws) if n else None,
               "rtf_min": min((r["rtf"] for r in subset if r["rtf"] is not None), default=None),
               "rtf_max": max((r["rtf"] for r in subset if r["rtf"] is not None), default=None)}
        for c, u in cells.items():
            proc99 = [r["brokers"][c]["process_us"]["p99"] for r in subset
                      if r["brokers"][c] and r["brokers"][c]["process_us"] and r["brokers"][c]["process_us"].get("p99")]
            proc50 = [r["brokers"][c]["process_us"]["p50"] for r in subset
                      if r["brokers"][c] and r["brokers"][c]["process_us"] and r["brokers"][c]["process_us"].get("p50")]
            rtt50 = [r["robots"].get(u, {}).get("rtt_us_p50") for r in subset if r["robots"].get(u, {}).get("rtt_us_p50")]
            rtt99 = [r["robots"].get(u, {}).get("rtt_us_p99") for r in subset if r["robots"].get(u, {}).get("rtt_us_p99")]
            stale = [r["robots"].get(u, {}).get("stale_intervals") or 0 for r in subset]
            starv = [r["brokers"][c]["starvations"] for r in subset if r["brokers"][c] and r["brokers"][c]["starvations"] is not None]
            out[f"cell_{c}"] = {"ue": u, "process_us_p50_median": percentile(proc50, 50),
                                "process_us_p99_median": percentile(proc99, 50), "process_us_p99_max": max(proc99, default=None),
                                "rtt_ms_p50_median": (percentile(rtt50, 50) or 0) / 1000 if rtt50 else None,
                                "rtt_ms_p99_median": (percentile(rtt99, 50) or 0) / 1000 if rtt99 else None,
                                "stale_intervals_total": sum(stale), "starvations_total": sum(starv) if starv else None}
        return out

    before = [r for r in table if not r["under_contention"]]
    under = [r for r in table if r["under_contention"]]
    report = {"log_dir": str(log_dir), "contention_start_unix_ms": contention_ms, "contention": contention_kind, "cells": cells,
              "broker_totals": {c: (brokers[c] or {}).get("stop") for c in cells},
              "per_second_starvations": {c: bool(brokers[c] and brokers[c]["has_per_second_starvations"]) for c in cells},
              "all": cell_summary(table), "before_contention": cell_summary(before), "under_contention": cell_summary(under),
              "fights": table}
    return report


def fmt(v, nd=1):
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def markdown(report) -> str:
    cells = report["cells"]
    lines = [f"# robot-fight battle — `{report['log_dir']}`", ""]
    lines.append("| 창 | 경기 | ue0:ue1 (무) | ue0 승률 (95 % CI) | 패인 | a proc p50/p99 med (µs) | b proc p50/p99 med | a RTT p50/p99 (ms) | b RTT p50/p99 | stale a/b | starv a/b | RTF |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for name, key in (("전체", "all"), ("경합 전", "before_contention"), ("경합 중", "under_contention")):
        s = report[key]
        if not s or s["fights"] == 0:
            continue
        sh = s["ue0_share"]
        share = f"{sh['share']:.2f} ({sh['lo']:.2f}–{sh['hi']:.2f})" if sh else "-"
        a, b = s["cell_a"], s["cell_b"]
        lines.append("| {} | {} | {}:{} ({}) | {} | {} | {}/{} | {}/{} | {}/{} | {}/{} | {}/{} | {}/{} | {}–{} |".format(
            name, s["fights"], s["wins"].get("ue0", 0), s["wins"].get("ue1", 0), s["draws"], share,
            ", ".join(f"{k} {v}" for k, v in sorted(s["loser_by"].items())) or "-",
            fmt(a["process_us_p50_median"], 0), fmt(a["process_us_p99_median"], 0),
            fmt(b["process_us_p50_median"], 0), fmt(b["process_us_p99_median"], 0),
            fmt(a["rtt_ms_p50_median"]), fmt(a["rtt_ms_p99_median"]), fmt(b["rtt_ms_p50_median"]), fmt(b["rtt_ms_p99_median"]),
            a["stale_intervals_total"], b["stale_intervals_total"], fmt(a["starvations_total"]), fmt(b["starvations_total"]),
            fmt(s["rtf_min"], 4), fmt(s["rtf_max"], 4)))
    lines.append("")
    lines.append("| # | 경합 | 승자 | 사유 | 패자 lost_by | RTF | a proc p50/p99/max | b proc p50/p99/max | a RTT p50/p99 | b RTT p50/p99 | one-way a/b p50 | stale a/b | stalls a/b | bridge a/b Hz |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in report["fights"]:
        ra, rb = r["robots"].get(cells["a"], {}), r["robots"].get(cells["b"], {})
        ba, bb = r["brokers"]["a"] or {}, r["brokers"]["b"] or {}
        pa, pb = ba.get("process_us") or {}, bb.get("process_us") or {}
        loser = [u for u in cells.values() if u != r["winner_node"]]
        lost_by = ", ".join(f"{u}:{r['robots'].get(u, {}).get('lost_by') or '-'}" for u in loser) if r["winner_node"] else "-"
        lines.append("| {} | {} | {} | {} | {} | {} | {}/{}/{} | {}/{}/{} | {}/{} | {}/{} | {}/{} | {}/{} | {}/{} | {}/{} |".format(
            r["fight"], "y" if r["under_contention"] else "n", r["winner_node"] or "draw", r["reason"], lost_by, fmt(r["rtf"], 4),
            fmt(pa.get("p50"), 0), fmt(pa.get("p99"), 0), fmt(pa.get("max"), 0), fmt(pb.get("p50"), 0), fmt(pb.get("p99"), 0), fmt(pb.get("max"), 0),
            fmt((ra.get("rtt_us_p50") or 0) / 1000 if ra.get("rtt_us_p50") else None), fmt((ra.get("rtt_us_p99") or 0) / 1000 if ra.get("rtt_us_p99") else None),
            fmt((rb.get("rtt_us_p50") or 0) / 1000 if rb.get("rtt_us_p50") else None), fmt((rb.get("rtt_us_p99") or 0) / 1000 if rb.get("rtt_us_p99") else None),
            fmt((ra.get("one_way_us_p50") or 0) / 1000 if ra.get("one_way_us_p50") else None), fmt((rb.get("one_way_us_p50") or 0) / 1000 if rb.get("one_way_us_p50") else None),
            fmt(ra.get("stale_intervals")), fmt(rb.get("stale_intervals")), fmt(ba.get("node_stalls")), fmt(bb.get("node_stalls")),
            fmt(r["bridges"]["a"]["rate_hz"]), fmt(r["bridges"]["b"]["rate_hz"])))
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log-dir", required=True, type=pathlib.Path)
    parser.add_argument("--cells", default="a=ue0,b=ue1", help="cell=ue pairs")
    parser.add_argument("--report", type=pathlib.Path)
    parser.add_argument("--markdown", type=pathlib.Path)
    args = parser.parse_args(argv)
    cells = dict(item.split("=", 1) for item in args.cells.split(","))
    report = analyse(args.log_dir, cells)
    if args.report:
        args.report.write_text(json.dumps(report, indent=1))
    text = markdown(report)
    if args.markdown:
        args.markdown.write_text(text)
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
