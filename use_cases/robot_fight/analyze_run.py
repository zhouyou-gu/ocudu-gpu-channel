#!/usr/bin/env python3
"""Joined timeline of a robot fight run over the emulated link (R4b).

Reads the log directory of a native multi-UE gate run that carried the
robot-fight hooks (launch/root_exec.sh + launch/ue_exec.sh) and joins, per
fight window:

* the arena result (winner, reason, RTF, per-robot one-way latency and stale
  intervals) and the brain results (RTT from echoes, STATE gaps),
* the Sionna bridge status (`sionna-status.jsonl`: update count, external vs
  scripted position source, age of the arena's position sample),
* the broker log (`gpu_timings` p50/p99, heartbeat idle/stall deltas per
  device, node stalls) -- broker `t` is seconds since the broker started, which
  is taken from broker.log's birth time (or --broker-start-unix-ms),
* the srsUE metrics CSVs (per-second dl_snr / mcs / bler, classed LOS or
  shadow by the Sionna downlink gain like analyze-sionna-multi-ue-run.py) and
  the srsUE logs (out-of-sync / RRC release / RLF lines with timestamps).

Stdlib only. Writes a JSON report and a markdown table.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import os
import pathlib
import re
import statistics
import subprocess
import sys
from datetime import datetime, timezone


def percentile(values, q):
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * q / 100.0
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def summarize(values):
    values = [v for v in values if v is not None]
    if not values:
        return None
    return {"n": len(values), "mean": round(statistics.fmean(values), 3), "min": round(min(values), 3),
            "p50": round(percentile(values, 50), 3), "p99": round(percentile(values, 99), 3),
            "max": round(max(values), 3)}


def read_jsonl(path: pathlib.Path):
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def birth_time_ms(path: pathlib.Path) -> int | None:
    try:
        st = os.stat(path)
        bt = getattr(st, "st_birthtime", None)
        if bt:
            return int(bt * 1000)
    except OSError:
        return None
    try:
        out = subprocess.run(["stat", "-c", "%W", str(path)], capture_output=True, text=True, check=True).stdout.strip()
        if out and out != "0":
            return int(out) * 1000
    except (OSError, subprocess.CalledProcessError, ValueError):
        pass
    return None


# --- loaders ----------------------------------------------------------------

def load_fights(log_dir: pathlib.Path):
    return read_jsonl(log_dir / "fights" / "summary.jsonl")


def load_bridge(log_dir: pathlib.Path):
    updates = []
    for rec in read_jsonl(log_dir / "sionna-status.jsonl"):
        if rec.get("event") != "sionna_rt_update" or "update_started_unix_ms" not in rec:
            continue
        gains = {}
        for ch in rec.get("channels") or []:
            if ch.get("link_id"):
                gains[ch["link_id"]] = ch.get("strongest_tap_gain_db")
        ps = rec.get("position_status") or {}
        updates.append({
            "t_ms": int(rec["update_started_unix_ms"]),
            "gen_ms": (rec.get("timing_ms") or {}).get("channel_generation"),
            "positions": rec.get("positions") or {},
            "gains": gains,
            "source": rec.get("position_source"),
            "age_ms": ps.get("last_sample_age_ms"),
            "publish_age_ms": ps.get("last_sample_publish_age_ms"),
            "stale": ps.get("stale"),
            "received": ps.get("messages_received"),
        })
    updates.sort(key=lambda u: u["t_ms"])
    return updates


BROKER_RE = re.compile(r"(\w+)=(-?[0-9.]+)")


def load_broker(log_dir: pathlib.Path, start_ms: int | None):
    path = log_dir / "broker.log"
    gpu, hb, stalls, stop = [], [], [], {}
    if not path.exists():
        return gpu, hb, stalls, stop
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("event=gpu_timings"):
            f = dict(BROKER_RE.findall(line))
            gpu.append({"t": float(f.get("t", 0)), "h2d": float(f.get("h2d_us", 0)),
                        "kernel": float(f.get("kernel_us", 0)), "d2h": float(f.get("d2h_us", 0))})
        elif line.startswith("event=heartbeat"):
            m = re.match(r"event=heartbeat t=(\d+) dev=(\S+)", line)
            if not m:
                continue
            f = dict(BROKER_RE.findall(line))
            entry = {"t": int(m.group(1)), "dev": m.group(2), "idle": int(float(f.get("idle", 0))),
                     "room_stall": int(float(f.get("room_stall", 0))), "stall": int(float(f.get("stall", 0))),
                     "pulls": int(float(f.get("pulls", 0)))}
            # R4c: per-device health counters on the heartbeat (delta since the
            # previous heartbeat + run total). Older brokers do not print them;
            # leave the keys absent rather than zero so the window delta can
            # say "unknown" instead of "0".
            for key in ("starvations", "gaps", "overflows"):
                if f"{key}_total" in f:
                    entry[key] = int(float(f[key]))
                    entry[f"{key}_total"] = int(float(f[f"{key}_total"]))
            hb.append(entry)
        elif line.startswith("event=node_stall") or line.startswith("event=node_stall_cleared"):
            stalls.append(line[:200])
        elif line.startswith("event=stop"):
            stop = {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", line)}
    if start_ms is not None:
        for g in gpu:
            g["t_ms"] = start_ms + int(g["t"] * 1000)
        for h in hb:
            h["t_ms"] = start_ms + h["t"] * 1000
    return gpu, hb, stalls, stop


def load_metrics(log_dir: pathlib.Path, ue: str):
    csv_path = log_dir / f"srsue-metrics-{ue}.csv"
    start_path = log_dir / f"srsue-{ue}.start_unix_ms"
    if not csv_path.exists() or not start_path.exists():
        return []
    start_ms = int(start_path.read_text().strip())
    rows = []
    with csv_path.open(encoding="utf-8", errors="replace") as fh:
        for raw in csv.DictReader(fh, delimiter=";"):
            try:
                rows.append({"t_ms": start_ms + int(float(raw["time"])), "dl_snr": float(raw["dl_snr"]),
                             "dl_mcs": float(raw["dl_mcs"]), "dl_bler": float(raw["dl_bler"]),
                             "ul_mcs": float(raw["ul_mcs"]), "ul_bler": float(raw["ul_bler"]),
                             "dl_brate": float(raw["dl_brate"]), "ul_brate": float(raw["ul_brate"]),
                             "rf_o": float(raw["rf_o"]), "rf_u": float(raw["rf_u"]), "rf_l": float(raw["rf_l"])})
            except (KeyError, ValueError):
                continue
    return rows


UE_LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6})")
LINK_EVENT_PATTERNS = {
    "out_of_sync": re.compile(r"out-of-sync", re.I),
    "in_sync": re.compile(r"in-sync", re.I),
    "rrc_release": re.compile(r"RRC Release|Connection Release|Received RRCRelease", re.I),
    "rlf": re.compile(r"radio link failure|RLF", re.I),
    "rrc_connected": re.compile(r"RRC Connected", re.I),
}


def load_ue_events(log_dir: pathlib.Path, ue: str, day_anchor_ms: int | None):
    """srsUE's internal log stamps every line with the container clock (UTC) as
    2026-09-29T13:27:37.165566; the console log has no timestamps."""
    path = log_dir / f"srsue-{ue}-internal.log"
    events = []
    if not path.exists():
        return events
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        kind = None
        for name, pat in LINK_EVENT_PATTERNS.items():
            if pat.search(line):
                kind = name
                break
        if kind is None:
            continue
        m = UE_LOG_TS.match(line)
        t_ms = None
        if m:
            try:
                t_ms = int(datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S.%f")
                           .replace(tzinfo=timezone.utc).timestamp() * 1000)
            except ValueError:
                t_ms = None
        events.append({"t_ms": t_ms, "kind": kind, "line": line[:160]})
    return events


# --- windows ----------------------------------------------------------------

def in_window(items, key, t0, t1):
    return [it for it in items if it.get(key) is not None and t0 <= it[key] <= t1]


def hb_delta(hb, dev, t0, t1):
    rows = [h for h in hb if h["dev"] == dev and "t_ms" in h and t0 - 1000 <= h["t_ms"] <= t1 + 1000]
    if len(rows) < 2:
        return None
    a, b = rows[0], rows[-1]
    out = {"idle": b["idle"] - a["idle"], "room_stall": b["room_stall"] - a["room_stall"],
           "producer_stall": b["stall"] - a["stall"], "pulls": b["pulls"] - a["pulls"]}
    # Health counters inside the window (R4c heartbeat fields). The per-second
    # deltas of every heartbeat after the first one in the window are summed,
    # which equals total[last] - total[first] but also survives a missing line.
    for key in ("starvations", "gaps", "overflows"):
        if all(key in h for h in rows):
            out[key] = sum(h[key] for h in rows[1:])
            out[f"{key}_seconds"] = [h["t"] for h in rows[1:] if h[key]]
        else:
            out[key] = None
    return out


def analyse(log_dir: pathlib.Path, gnb: str, broker_start_ms: int | None, shadow_below_db: float):
    fights = load_fights(log_dir)
    updates = load_bridge(log_dir)
    if broker_start_ms is None:
        broker_start_ms = birth_time_ms(log_dir / "broker.log")
    gpu, hb, stalls, stop = load_broker(log_dir, broker_start_ms)
    ues = sorted({r["node_id"] for f in fights for r in (f.get("robots") or []) if r.get("node_id")}) or ["ue0", "ue1"]
    metrics = {ue: load_metrics(log_dir, ue) for ue in ues}
    anchor = fights[0]["t_start_unix_ms"] if fights else (updates[0]["t_ms"] if updates else None)
    ue_events = {ue: load_ue_events(log_dir, ue, anchor) for ue in ues}
    best_gain = {}
    for ue in ues:
        link = f"{gnb}>{ue}:sionna_rt"
        g = [u["gains"].get(link) for u in updates if u["gains"].get(link) is not None]
        best_gain[ue] = max(g) if g else None
    update_keys = [u["t_ms"] for u in updates]

    def gain_at(ue, t_ms):
        if not updates:
            return None
        i = bisect.bisect_left(update_keys, t_ms)
        cands = [j for j in (i - 1, i) if 0 <= j < len(updates)]
        j = min(cands, key=lambda j: abs(updates[j]["t_ms"] - t_ms))
        if abs(updates[j]["t_ms"] - t_ms) > 1500:
            return None
        return updates[j]["gains"].get(f"{gnb}>{ue}:sionna_rt")

    per_fight = []
    for f in fights:
        t0, t1 = f["t_start_unix_ms"], f["t_end_unix_ms"]
        row = {"fight": f["fight"], "t_rel_s": round((t0 - anchor) / 1000.0, 1) if anchor else None,
               "duration_s": round((t1 - t0) / 1000.0, 2), "winner": f.get("winner_node"), "reason": f.get("reason"),
               "rtf": f.get("rtf"), "late_loops": f.get("late_loops"), "max_lag_ms": f.get("max_lag_ms"),
               "arena_status": f.get("arena_status"), "robots": {}, "bridge": {}, "broker": {}, "ue": {}}
        for r in f.get("robots") or []:
            row["robots"][r["node_id"]] = {
                "cmds": r.get("cmds_received"), "cmd_gaps": r.get("cmd_seq_gaps"),
                "stale_intervals": r.get("stale_intervals"), "stale_total_s": r.get("stale_total_s"),
                "one_way_us": r.get("one_way_us"), "turnaround_us": r.get("brain_turnaround_us")}
        for b in f.get("brains") or []:
            rid = b.get("robot_id")
            if rid is None or rid >= len(ues):
                continue
            row["robots"].setdefault(ues[rid], {}).update({
                "outcome": b.get("outcome"), "rtt_us": b.get("rtt_us"), "state_one_way_us": b.get("state_one_way_us"),
                "state_gaps": b.get("state_seq_gaps"), "ticks_no_fresh_state": b.get("ticks_without_fresh_state"),
                "deadline_misses": b.get("deadline_misses"), "reflexes": b.get("reflexes")})
        w = in_window(updates, "t_ms", t0, t1)
        row["bridge"] = {
            "updates": len(w), "external": sum(1 for u in w if u["source"] == "external"),
            "stale": sum(1 for u in w if u["stale"]), "rate_hz": round(len(w) / max((t1 - t0) / 1000.0, 1e-3), 2),
            "gen_ms": summarize([u["gen_ms"] for u in w]), "age_ms": summarize([u["age_ms"] for u in w]),
            "publish_age_ms": summarize([u["publish_age_ms"] for u in w]),
        }
        g = in_window(gpu, "t_ms", t0, t1)
        row["broker"] = {"gpu_samples": len(g),
                         "kernel_us": summarize([x["kernel"] for x in g]), "h2d_us": summarize([x["h2d"] for x in g]),
                         "d2h_us": summarize([x["d2h"] for x in g])}
        devs = sorted({h["dev"] for h in hb})
        row["broker"]["heartbeat_delta"] = {d: hb_delta(hb, d, t0, t1) for d in devs}
        for ue in ues:
            rows_w = in_window(metrics[ue], "t_ms", t0 - 500, t1 + 500)
            classed = []
            for m in rows_w:
                gain = gain_at(ue, m["t_ms"])
                cls = None
                if gain is not None and best_gain[ue] is not None:
                    cls = "shadow" if gain <= best_gain[ue] - shadow_below_db else "los"
                classed.append({**m, "gain": gain, "cls": cls})
            ev = in_window(ue_events[ue], "t_ms", t0, t1)
            row["ue"][ue] = {
                "rows": len(classed),
                "dl_snr": summarize([m["dl_snr"] for m in classed if m["dl_snr"] != 0.0]),
                "dl_mcs": summarize([m["dl_mcs"] for m in classed]), "dl_bler": summarize([m["dl_bler"] for m in classed]),
                "ul_bler": summarize([m["ul_bler"] for m in classed]),
                "dl_brate_kbps": summarize([m["dl_brate"] / 1000.0 for m in classed]),
                "ul_brate_kbps": summarize([m["ul_brate"] / 1000.0 for m in classed]),
                "gain_db": summarize([m["gain"] for m in classed]),
                "shadow_rows": sum(1 for m in classed if m["cls"] == "shadow"),
                "los_rows": sum(1 for m in classed if m["cls"] == "los"),
                "rf_o_u_l": [sum(m["rf_o"] for m in classed), sum(m["rf_u"] for m in classed), sum(m["rf_l"] for m in classed)],
                "link_events": {k: sum(1 for e in ev if e["kind"] == k) for k in LINK_EVENT_PATTERNS},
            }
        per_fight.append(row)

    wins = {}
    for f in fights:
        wins[f.get("winner_node")] = wins.get(f.get("winner_node"), 0) + 1
    completed = [f for f in fights if f.get("arena_status") == 0 and f.get("winner_node") is not None]
    summary = {
        "log_dir": str(log_dir), "fights": len(fights), "completed": len(completed),
        "wins": wins, "reasons": {r: sum(1 for f in fights if f.get("reason") == r) for r in {f.get("reason") for f in fights}},
        "rtf": summarize([f.get("rtf") for f in fights]),
        "rtf_all_1": all(abs((f.get("rtf") or 0) - 1.0) <= 0.01 for f in fights) if fights else None,
        "broker_start_unix_ms": broker_start_ms, "broker_stop": stop, "node_stalls": len(stalls),
        "bridge_total_updates": len(updates), "bridge_external_updates": sum(1 for u in updates if u["source"] == "external"),
        "bridge_gen_ms": summarize([u["gen_ms"] for u in updates]),
        "best_dl_gain_db": best_gain,
        "ue_events_total": {ue: {k: sum(1 for e in ue_events[ue] if e["kind"] == k) for k in LINK_EVENT_PATTERNS} for ue in ues},
        "rtt_us_all": {ue: summarize([f["robots"].get(ue, {}).get("rtt_us", {}).get("p50") for f in per_fight
                                      if isinstance(f["robots"].get(ue, {}).get("rtt_us"), dict)]) for ue in ues},
    }
    # attach summary from the gate's report dir, if reachable
    report_dir = pathlib.Path(str(log_dir).replace("/results/logs/", "/results/reports/"))
    att = report_dir / "attach-summary.json"
    if att.exists():
        try:
            a = json.loads(att.read_text())
            summary["gate"] = {k: a.get(k) for k in ("status", "broker_status", "rx_starvations", "tx_queue_overflows",
                                                      "tx_sequence_gaps", "zmq_errors", "per_ue")}
        except json.JSONDecodeError:
            pass
    return {"summary": summary, "fights": per_fight}


def fmt_us(d, key="p50"):
    if not isinstance(d, dict) or d.get(key) is None:
        return "-"
    return f"{d[key] / 1000.0:.1f}"


def fmt(d, key="p50", nd=1):
    if not isinstance(d, dict) or d.get(key) is None:
        return "-"
    return f"{d[key]:.{nd}f}"


def markdown(report) -> str:
    s = report["summary"]
    lines = [f"# robot fight over the air — {s['log_dir']}", "",
             f"fights {s['fights']} (completed {s['completed']}), wins {s['wins']}, reasons {s['reasons']}, "
             f"RTF {fmt(s['rtf'], 'min', 3)}–{fmt(s['rtf'], 'max', 3)}, broker stop {s['broker_stop']}, node stalls {s['node_stalls']}, "
             f"bridge updates {s['bridge_total_updates']} (external {s['bridge_external_updates']}), solve p50 {fmt(s['bridge_gen_ms'])} ms",
             "", "| # | t (s) | dur | winner | reason | RTF | RTT p50/p99 ms ue0 | ue1 | one-way p50/p99 ms ue0 | ue1 | stale n/s ue0 | ue1 | bridge upd/ext/age p50 ms | kernel p50/p99 µs | ue0 snr mean/min, shadow rows, oos | ue1 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for f in report["fights"]:
        r0 = f["robots"].get("ue0", {})
        r1 = f["robots"].get("ue1", {})
        u0 = f["ue"].get("ue0", {})
        u1 = f["ue"].get("ue1", {})
        b = f["bridge"]
        k = f["broker"].get("kernel_us")

        def ue_cell(u):
            snr = u.get("dl_snr")
            return f"{fmt(snr, 'mean')}/{fmt(snr, 'min')}, {u.get('shadow_rows', 0)}/{u.get('rows', 0)}, {u.get('link_events', {}).get('out_of_sync', 0)}"
        lines.append(
            f"| {f['fight']} | {f['t_rel_s']} | {f['duration_s']} | {f['winner']} | {f['reason']} | {f['rtf']} "
            f"| {fmt_us(r0.get('rtt_us'))}/{fmt_us(r0.get('rtt_us'), 'p99')} | {fmt_us(r1.get('rtt_us'))}/{fmt_us(r1.get('rtt_us'), 'p99')} "
            f"| {fmt_us(r0.get('one_way_us'))}/{fmt_us(r0.get('one_way_us'), 'p99')} | {fmt_us(r1.get('one_way_us'))}/{fmt_us(r1.get('one_way_us'), 'p99')} "
            f"| {r0.get('stale_intervals', '-')}/{r0.get('stale_total_s', '-')} | {r1.get('stale_intervals', '-')}/{r1.get('stale_total_s', '-')} "
            f"| {b['updates']}/{b['external']}/{fmt(b.get('age_ms'))} | {fmt(k)}/{fmt(k, 'p99')} | {ue_cell(u0)} | {ue_cell(u1)} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--log-dir", type=pathlib.Path, required=True)
    p.add_argument("--gnb", default="gnb0")
    p.add_argument("--broker-start-unix-ms", type=int, default=None)
    p.add_argument("--shadow-below-db", type=float, default=15.0)
    p.add_argument("--report", type=pathlib.Path, default=None)
    p.add_argument("--markdown", type=pathlib.Path, default=None)
    args = p.parse_args(argv)
    report = analyse(args.log_dir, args.gnb, args.broker_start_unix_ms, args.shadow_below_db)
    text = json.dumps(report, indent=1)
    if args.report:
        args.report.write_text(text + "\n", encoding="utf-8")
    md = markdown(report)
    if args.markdown:
        args.markdown.write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
