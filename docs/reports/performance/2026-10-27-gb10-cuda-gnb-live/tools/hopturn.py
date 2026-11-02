#!/usr/bin/env python3
"""Broker hop trace -> per-port radio turnaround: time from the broker's reply of block j (RepReplySent, event 22)
to the arrival of that radio's next TX block (PullReplyRecv, event 1) on the same port. gNB vs UE ports."""
import sys, csv, collections, statistics as st
f = sys.argv[1]; last_s = float(sys.argv[2]) if len(sys.argv) > 2 else 0
ev = collections.defaultdict(lambda: {"rep": [], "pull": []})
for row in csv.reader(open(f)):
    if not row or row[0] == "role": continue
    role, pid, e, t = row[0], row[1], int(row[2]), int(row[3])
    if role == "rep" and e == 22: ev[pid]["rep"].append(t)
    elif role == "puller" and e == 1: ev[pid]["pull"].append(t)
tmax = max((t for v in ev.values() for t in v["rep"] + v["pull"]), default=0)
def pct(v, q): v = sorted(v); return v[min(len(v) - 1, int(len(v) * q))] / 1e6
groups = collections.defaultdict(list)
for pid, v in ev.items():
    pulls = sorted(v["pull"]); reps = sorted(v["rep"]); turn = []; i = 0
    for r in reps:
        if last_s and r < tmax - last_s * 1e9: continue
        while i < len(pulls) and pulls[i] <= r: i += 1
        if i < len(pulls): turn.append(pulls[i] - r)
    if turn:
        g = "gnb" if pid.startswith("gnb") else "ue"
        groups[g] += turn
        print(f"{pid:10s} n={len(turn):6d} turnaround_ms p50={pct(turn,0.5):.2f} p90={pct(turn,0.9):.2f} p99={pct(turn,0.99):.2f}")
for g, v in groups.items(): print(f"ALL {g:4s} n={len(v):7d} p50={pct(v,0.5):.2f} p90={pct(v,0.9):.2f} p99={pct(v,0.99):.2f} ms")
