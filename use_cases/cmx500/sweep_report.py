#!/usr/bin/env python3
"""Per-step KPIs of a sweep_dl.sh run. Usage: sweep_report.py RUN_DIR [PARAM_NAME]

Reads RUN_DIR/steps.csv (step start times), RUN_DIR/metrics.csv (srsUE metrics, 1 s period, time relative to
RUN_DIR/t_srsue) and, when PHY info logging was on, the per-slot PDSCH lines of RUN_DIR/srsue.log. The first
SETTLE_S of each step are skipped. Prints a table and writes RUN_DIR/sweep.csv.
"""
import csv
import datetime as dt
import os
import re
import statistics
import sys

SETTLE_S = 1.5
run = sys.argv[1].rstrip("/")
param = sys.argv[2] if len(sys.argv) > 2 else "value"


def ts(s):
    return dt.datetime.fromisoformat(s.strip())


def mean(values, digits=1):
    values = [v for v in values if v is not None]
    return round(statistics.mean(values), digits) if values else ""


def num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


steps = []
with open(os.path.join(run, "steps.csv")) as f:
    for row in csv.DictReader(f):
        steps.append((ts(row["t_utc"]), row["value"]))

# srsUE metrics, one row per second
metrics = []
try:
    t_srsue = ts(open(os.path.join(run, "t_srsue")).read())
    with open(os.path.join(run, "metrics.csv")) as f:
        for r in csv.DictReader((l for l in f if not l.startswith("#")), delimiter=";"):
            t = num(r.get("time"))
            if t is not None:
                metrics.append((t_srsue + dt.timedelta(milliseconds=t), r))
except OSError:
    pass

# Per-slot PDSCH lines (PHY info logging only)
pdsch_re = re.compile(r"^(\S+) \[PHY\d-NR\] \[I\] \[ *\d+\] PDSCH: cc=0 pid=\d+ c-rnti=0x[0-9a-f]+ prb=\((\d+),(\d+)\)"
                      r".*? mod=(\w+) tbs=(\d+) R=([\d.]+) rv=(\d) CRC=(OK|KO).*? snr=([+-][\d.]+)")
event_re = re.compile(r"^(\S+) .*(out-of-sync|resynchronised to|does not match current SFN|Random Access)")
pdsch, events = [], []
with open(os.path.join(run, "srsue.log"), errors="replace") as f:
    for line in f:
        m = pdsch_re.match(line)
        if m:
            pdsch.append((ts(m.group(1)), m.group(4), int(m.group(5)), int(m.group(7)), m.group(8) == "OK",
                          float(m.group(9))))
            continue
        m = event_re.match(line)
        if m:
            events.append((ts(m.group(1)), m.group(2)))

rows = []
for i, (t0, value) in enumerate(steps):
    if value == "end":
        break
    t1 = steps[i + 1][0]
    a = t0 + dt.timedelta(seconds=SETTLE_S)
    m = [r for t, r in metrics if a <= t < t1]
    row = {
        param: value,
        "seconds": len(m),
        "rsrp": mean(num(r.get("rsrp")) for r in m),
        "dl_snr": mean(num(r.get("dl_snr")) for r in m),
        "dl_mcs": mean(num(r.get("dl_mcs")) for r in m),
        "dl_bler_pct": mean(num(r.get("dl_bler")) for r in m),
        "dl_mbps": mean(((num(r.get("dl_brate")) or 0) / 1e6 for r in m), 3),
        "ul_mcs": mean(num(r.get("ul_mcs")) for r in m),
        "ul_bler_pct": mean(num(r.get("ul_bler")) for r in m),
        "ul_mbps": mean(((num(r.get("ul_brate")) or 0) / 1e6 for r in m), 3),
        "sync_events": sum(1 for t, _ in events if a <= t < t1),
    }
    p = [x for x in pdsch if a <= x[0] < t1]
    if pdsch:
        first = [x for x in p if x[3] == 0]
        row.update({
            "pdsch": len(p),
            "pdsch_snr": mean(x[5] for x in p),
            "pdsch_bler_first_pct": round(100.0 * sum(not x[4] for x in first) / len(first), 1) if first else "",
            "mod": "/".join(sorted({x[1] for x in p})),
        })
    rows.append(row)

fields = list(rows[0].keys()) if rows else [param]
with open(os.path.join(run, "sweep.csv"), "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=fields)
    w.writeheader()
    w.writerows(rows)
widths = {k: max(len(k), *(len(str(r.get(k, ""))) for r in rows)) for k in fields}
print("  ".join(k.rjust(widths[k]) for k in fields))
for r in rows:
    print("  ".join(str(r.get(k, "")).rjust(widths[k]) for k in fields))
print(f"-> {os.path.join(run, 'sweep.csv')}")
