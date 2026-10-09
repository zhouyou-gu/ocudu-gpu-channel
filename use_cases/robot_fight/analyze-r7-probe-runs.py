#!/usr/bin/env python3
"""One table row per (run, UE) for the R7-prep probe runs.

    analyze-r7-probe-runs.py TAG=LOG_DIR ... [--steady-after-s 20] [--markdown out.md] [--json out.json]

Joins, per run and UE: the probe's steady-state one-way/RTT percentiles,
loss and outages (robot-fight-probe.py analyze), the srsUE metrics CSV
(dl_snr, dl_mcs, dl_bler, ul_mcs, ul_bler, rf_u/rf_l means over the steady
window), the broker's emulator-call wall time (cpu_stage_timings process_us
p50/p99) and the attach summary's verdict. The steady window starts
`--steady-after-s` after the probe client started.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("robot_fight_probe", HERE / "robot-fight-probe.py")
probe = importlib.util.module_from_spec(spec)
sys.modules["robot_fight_probe"] = probe
spec.loader.exec_module(probe)

CELL_OF_UE = {"ue0": "a", "ue1": "b"}


def fmt(value, digits=1):
    if value is None:
        return "-"
    return f"{value:.{digits}f}"


def probe_summary(log_dir: pathlib.Path, ue: str, steady_after_s: float, outage_ms: float):
    path = log_dir / f"probe-{ue}.jsonl"
    if not path.exists():
        return None, None
    records, start_ns, stop = [], None, None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("{"):
            continue
        record = json.loads(line)
        if record["event"] == "client_start":
            start_ns = record["t_unix_ns"]
        elif record["event"] == "client_stop":
            stop = record
        elif record["event"] in ("reply", "lost"):
            records.append(record)
    if not records:
        return None, start_ns
    first = start_ns or min(r["t_send_ns"] for r in records)
    steady_from = first + int(steady_after_s * 1e9)
    steady = [r for r in records if r["t_send_ns"] >= steady_from]
    return {"steady": probe.summarise(steady, outage_ms), "all": probe.summarise(records, outage_ms),
            "steady_from_ns": steady_from, "end_ns": max(r["t_send_ns"] for r in records),
            "coverage": {"client_stop_present": stop is not None,
                         "sent": stop.get("sent") if stop else None,
                         "unresolved_at_stop": max(0, stop["sent"] - len(records)) if stop else None,
                         "loss_denominator": "resolved reply/lost records; unresolved requests excluded"}}, first


def srsue_metrics(log_dir: pathlib.Path, ue: str, window_ns: tuple[int, int] | None):
    path = log_dir / f"srsue-metrics-{ue}.csv"
    start_path = log_dir / f"srsue-{ue}.start_unix_ms"
    if not path.exists():
        return {}
    start_ms = int(start_path.read_text().strip()) if start_path.exists() else None
    header = None
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            continue
        parts = line.split(";")
        if header is None:
            header = parts
            continue
        if len(parts) != len(header):
            continue
        row = dict(zip(header, parts))
        try:
            t_ms = float(row["time"])
        except (KeyError, ValueError):
            continue
        if window_ns and start_ms is not None:
            t_abs_ns = (start_ms + t_ms) * 1_000_000
            if not (window_ns[0] <= t_abs_ns <= window_ns[1]):
                continue
        rows.append(row)
    if not rows:
        return {"rows": 0}

    def mean(key):
        values = []
        for row in rows:
            try:
                values.append(float(row[key]))
            except (KeyError, ValueError):
                pass
        return (sum(values) / len(values)) if values else None

    return {"rows": len(rows), "dl_snr": mean("dl_snr"), "dl_mcs": mean("dl_mcs"), "dl_bler": mean("dl_bler"),
            "ul_mcs": mean("ul_mcs"), "ul_bler": mean("ul_bler"), "dl_brate": mean("dl_brate"),
            "ul_brate": mean("ul_brate"), "rf_u": mean("rf_u"), "rf_l": mean("rf_l"),
            "attached": mean("is_attached")}


def broker_process_us(log_dir: pathlib.Path, cell: str, steady_after_s: float):
    path = log_dir / f"broker-{cell}.log"
    if not path.exists():
        return {}
    values = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "event=cpu_stage_timings" not in line:
            continue
        m = re.search(r" t=(\d+) .*process_us=([0-9.]+)", line)
        if not m:
            continue
        t = int(m.group(1))
        if t < steady_after_s:
            continue
        values.append(float(m.group(2)))
    if not values:
        return {}
    return {"p50": probe.percentile(values, 0.5), "p99": probe.percentile(values, 0.99), "max": max(values),
            "n": len(values), "window_basis": "broker elapsed seconds, not probe steady window",
            "node_scope": "all nodes pooled"}


def broker_stop(log_dir: pathlib.Path, cell: str):
    path = log_dir / f"broker-{cell}.log"
    if not path.exists():
        return {}
    for line in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()):
        if "event=stop" in line:
            out = {}
            for key in ("rx_starvations", "tx_queue_overflows", "tx_sequence_gaps"):
                m = re.search(rf"{key}=(\d+)", line)
                if m:
                    out[key] = int(m.group(1))
            return out
    return {}


def gate_result(log_dir: pathlib.Path):
    candidates = [log_dir / "report" / "attach-summary.json",
                  log_dir.parent.parent.parent / "reports" / "ocudu-robot-fight" / log_dir.name / "attach-summary.json"]
    report = next((path for path in candidates if path.exists()), None)
    if report is None:
        return {}
    data = json.loads(report.read_text(encoding="utf-8"))
    return {"result": next((data[key] for key in ("status", "result", "verdict") if key in data), None),
            "path": str(report), "run_parameters": data.get("run_parameters", {})}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runs", nargs="+", help="TAG=LOG_DIR")
    parser.add_argument("--steady-after-s", type=float, default=20.0)
    parser.add_argument("--outage-ms", type=float, default=200.0)
    parser.add_argument("--markdown")
    parser.add_argument("--json")
    args = parser.parse_args()
    results = []
    for item in args.runs:
        tag, _, path = item.partition("=")
        log_dir = pathlib.Path(path)
        run = {"tag": tag, "log_dir": str(log_dir), "gate": gate_result(log_dir), "ues": {}}
        for ue, cell in CELL_OF_UE.items():
            summary, _ = probe_summary(log_dir, ue, args.steady_after_s, args.outage_ms)
            window = (summary["steady_from_ns"], summary["end_ns"]) if summary else None
            run["ues"][ue] = {"probe": summary, "srsue": srsue_metrics(log_dir, ue, window),
                              "broker": broker_process_us(log_dir, cell, args.steady_after_s),
                              "broker_stop": broker_stop(log_dir, cell)}
        results.append(run)
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    lines = ["| run | UE | gate | n | ul p50/p90/p99 ms | dl p50/p90/p99 ms | rtt p50/p90/p99 ms | loss % | "
             "outages n/total/max ms | dl_snr | dl_mcs | dl_bler | ul_mcs | ul_bler | rf_u/l | broker p50/p99 µs | starv |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for run in results:
        for ue, block in run["ues"].items():
            p = block["probe"]
            s = block["srsue"]
            b = block["broker"]
            st = block["broker_stop"]
            if p is None:
                lines.append(f"| {run['tag']} | {ue} | {run['gate'].get('result', '-')} | no probe log | | | | | | | | | | | | | |")
                continue
            q = p["steady"]

            def trio(k):
                v = q[k]
                return "-" if v["p50"] is None else f"{v['p50'] / 1000:.1f} / {v['p90'] / 1000:.1f} / {v['p99'] / 1000:.1f}"

            o = q["outages"]
            lines.append(
                f"| {run['tag']} | {ue} | {run['gate'].get('result', '-')} | {q['requests']} | {trio('ul_us')} | {trio('dl_us')} | "
                f"{trio('rtt_us')} | {fmt(q['loss_pct'], 2)} | {o['count']} / {o['total_ms']:.0f} / {o['max_ms']:.0f} | "
                f"{fmt(s.get('dl_snr'))} | {fmt(s.get('dl_mcs'))} | {fmt(s.get('dl_bler'), 2)} | {fmt(s.get('ul_mcs'))} | "
                f"{fmt(s.get('ul_bler'), 2)} | {fmt(s.get('rf_u'), 2)}/{fmt(s.get('rf_l'), 2)} | "
                f"{fmt(b.get('p50'), 0)} / {fmt(b.get('p99'), 0)} | {st.get('rx_starvations', '-')} |")
    text = "\n".join(lines) + "\n"
    if args.markdown:
        pathlib.Path(args.markdown).write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
