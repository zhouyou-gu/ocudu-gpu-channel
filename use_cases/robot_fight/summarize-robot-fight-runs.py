#!/usr/bin/env python3
"""One table row per broker from robot-fight attach summaries (R5a).

    summarize-robot-fight-runs.py <attach-summary.json>... [--markdown]

Columns: run, cell, sched, contention, broker exit, rx_starvations, node
stalls under contention, kernel p50/p99/max (µs) before and under contention,
h2d p99, emulator-call wall time process_us p50/p99 before and p50/p99/max
under contention (from the broker log's cpu_stage_timings; this is where a
time-sliced context shows), UE ping p50/p90/max (ms) under contention,
Sionna updates.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re


def fmt(value, digits=1):
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def percentile(values, q):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * q))]


def process_us(log_dir: pathlib.Path, cell: str, contention_t):
    """Wall time of the emulator call per slot (cpu_stage_timings process_us).

    The kernel_us the broker reports is measured by stream events, i.e. once
    the GPU has started the work; a broker that waits for another context's
    time slice waits BEFORE that, and only the process_us wall time sees it.
    Per-second lines, all nodes of the broker pooled, before / under
    contention (2 s guard after the start), p50/p99/max in µs.
    """
    log = log_dir / f"broker-{cell}.log"
    if not log.exists():
        return None, None
    before, under = [], []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("event=cpu_stage_timings "):
            continue
        m = re.search(r" t=(\d+) .*process_us=([0-9.]+)", line)
        if not m:
            continue
        t, value = int(m.group(1)), float(m.group(2))
        if contention_t is None or t < contention_t:
            before.append(value)
        elif t >= contention_t + 2:
            under.append(value)

    def stats(values):
        if not values:
            return None
        return {"p50": percentile(values, 0.5), "p99": percentile(values, 0.99), "max": max(values), "n": len(values)}
    return stats(before), stats(under)


def rows_for(path: pathlib.Path):
    summary = json.loads(path.read_text(encoding="utf-8"))
    params = summary.get("run_parameters") or {}
    ue_by_cell = {v["cell"]: (ue, v) for ue, v in summary["per_ue"].items()}
    log_dir = pathlib.Path(summary.get("log_dir", ""))
    for cell, broker in summary["brokers"].items():
        proc_before, proc_under = process_us(log_dir, cell, broker.get("contention_t"))
        before = (broker.get("gpu_timings_before_contention") or {})
        under = (broker.get("gpu_timings_under_contention") or {})
        kb = before.get("kernel_us") or {}
        ku = under.get("kernel_us") or {}
        hu = under.get("h2d_us") or {}
        ue, ue_info = ue_by_cell.get(cell, ("?", {}))
        ping = ue_info.get("ping_under_contention") or {}
        yield {
            "run": summary["timestamp"],
            "cell": cell,
            "sched": broker["sched"],
            "contention": summary.get("contention"),
            "hog": (params.get("contention") or {}).get("hog", {}).get("mps_client"),
            "exit": broker["status"],
            "starv": broker["counters"].get("rx_starvations"),
            "stalls": broker.get("node_stalls_under_contention"),
            "k_before": f"{fmt(kb.get('p50'))}/{fmt(kb.get('p99'))}/{fmt(kb.get('max'))}",
            "k_under": f"{fmt(ku.get('p50'))}/{fmt(ku.get('p99'))}/{fmt(ku.get('max'))}",
            "h2d_p99": fmt(hu.get("p99")),
            "proc_before": f"{fmt((proc_before or {}).get('p50'), 0)}/{fmt((proc_before or {}).get('p99'), 0)}",
            "proc_under": f"{fmt((proc_under or {}).get('p50'), 0)}/{fmt((proc_under or {}).get('p99'), 0)}/{fmt((proc_under or {}).get('max'), 0)}",
            "ue": ue,
            "ping": f"{fmt(ping.get('p50_ms'))}/{fmt(ping.get('p90_ms'))}/{fmt(ping.get('max_ms'))}",
            "loss": fmt(ping.get("loss_pct")),
            "sionna": (summary.get("sionna") or {}).get(cell, {}).get("updates"),
            "status": summary["status"],
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summaries", nargs="+", type=pathlib.Path)
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args()
    columns = ("run", "cell", "sched", "contention", "hog", "exit", "starv", "stalls",
               "k_before", "k_under", "h2d_p99", "proc_before", "proc_under", "ue", "ping", "loss", "sionna", "status")
    rows = [row for path in args.summaries for row in rows_for(path)]
    if args.markdown:
        print("| " + " | ".join(columns) + " |")
        print("|" + "---|" * len(columns))
        for row in rows:
            print("| " + " | ".join(fmt(row[c]) for c in columns) + " |")
    else:
        widths = {c: max(len(c), *(len(fmt(r[c])) for r in rows)) for c in columns}
        print("  ".join(c.ljust(widths[c]) for c in columns))
        for row in rows:
            print("  ".join(fmt(row[c]).ljust(widths[c]) for c in columns))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
