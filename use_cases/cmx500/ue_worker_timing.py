#!/usr/bin/env python3
"""srsUE per-slot worker timing from a PHY-info log: worker span (first PDCCH line of slot k -> PUSCH line of slot k),
spacing between consecutive slot starts, and how many slots are in flight. Usage: ue_worker_timing.py srsue.log"""
import datetime as dt
import re
import statistics
import sys

line_re = re.compile(r"^(\S+) \[PHY(\d)-NR\] \[I\] \[ *(\d+)\] (PDCCH|PDSCH|PUSCH):")
# slot numbers wrap every 10240 slots (10.24 s): key every slot occurrence by (slot, start time of that occurrence)
cur, first, pusch = {}, {}, {}
for line in open(sys.argv[1], errors="replace"):
    m = line_re.match(line)
    if not m:
        continue
    t = dt.datetime.fromisoformat(m.group(1)).timestamp() * 1e6
    k = int(m.group(3))
    if k not in cur or t - cur[k] > 1_000_000:
        cur[k] = t
        first[(k, t)] = (t, m.group(2))
    key = (k, cur[k])
    if m.group(4) == "PUSCH":
        pusch[key] = t


def pct(v):
    v = sorted(v)
    q = lambda p: round(v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))])
    return {f"p{p}": q(p) for p in (1, 10, 50, 90, 99)} | {"max": round(v[-1]), "n": len(v)}


span = [pusch[k] - first[k][0] for k in pusch if k in first and 0 <= pusch[k] - first[k][0] < 50000]
starts = sorted(t for t, _ in first.values())
gaps = [b - a for a, b in zip(starts, starts[1:]) if b - a < 50000]
# slots whose worker started before the previous slot's PUSCH was written = overlapping (pipelined) workers
ks = sorted((k for k in pusch if k in first), key=lambda k: first[k][0])
overlap = sum(1 for a, b in zip(ks, ks[1:]) if first[b][0] < pusch[a])
print("worker span PDCCH->PUSCH (us):", pct(span))
print("slot start spacing (us):", pct(gaps))
print("threads used:", sorted({w for _, w in first.values()}))
print(f"consecutive slots processed concurrently: {overlap}/{len(ks) - 1}")
