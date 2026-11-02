#!/usr/bin/env python3
"""Broker event=cpu_stage_timings (per node per second, mean per slot in that second): all stages, averaged over
t>=T0 and over nodes; also the slowest node. room = wait for RX-ring room (consumer not draining), align = window
alignment, data = wait for input IQ, read = ring read, process = channel call, throttle = pacing sleep, push = RX ring push."""
import sys, re, statistics as st
T0 = 5; S = ["room", "align", "data", "read", "process", "throttle", "push"]
print(f"{'run':30s} {'nodes':>5s} " + " ".join(f"{s:>8s}" for s in S) + f" {'sum':>8s} {'slow-node sum':>13s}")
for f in sys.argv[1:]:
    per = {}
    for line in open(f, errors="replace"):
        if "event=cpu_stage_timings" not in line: continue
        seg = [x for x in line.split("event=") if x.startswith("cpu_stage_timings")][0]
        kv = dict(re.findall(r"(\w+)=([^\s]+)", seg))
        if "t" not in kv or int(kv["t"]) < T0 or any(k + "_us" not in kv for k in S): continue
        d = per.setdefault(kv["node"], {s: [] for s in S})
        for s in S: d[s].append(float(kv[s + "_us"]))
    if not per: print(f"{f.split('/')[-2]:30s} no stage timings"); continue
    means = {s: st.mean([st.mean(v[s]) for v in per.values()]) for s in S}
    slow = max(per.values(), key=lambda v: sum(st.mean(v[s]) for s in S))
    print(f"{f.split('/')[-2]:30s} {len(per):5d} " + " ".join(f"{means[s]:8.1f}" for s in S) + f" {sum(means.values()):8.1f} {sum(st.mean(slow[s]) for s in S):13.1f}")
