#!/usr/bin/env python3
"""Per PRACH: srsUE log time -> bridge arrival of its first data block, and that block's lead. Usage: prach_latency.py RUN_DIR"""
import re
import sys

run = sys.argv[1]


def secs(mm_ss):
    m, s = mm_ss.split(":")
    return int(m) * 60 + float(s)


ue = [secs(m.group(1)) for m in re.finditer(r"T\d\d:(\d\d:\d\d\.\d+) \[PHY-SA \] \[I\] \[ *\d+\] PRACH: Transmitted",
                                             open(run + "/srsue.log").read())]
blocks = [(secs(m.group(3)), int(m.group(1)), float(m.group(2)))
          for m in re.finditer(r"ul data block .*?lead=(-?\d+) us dl_backlog=([\d.]+) ms wall=(\d\d:\d\d\.\d+)",
                               open(run + "/bridge.log").read())]
for t in ue:
    nxt = [b for b in blocks if b[0] >= t]
    if not nxt:
        print(f"ue {t:9.6f}: no block")
        continue
    b = nxt[0]
    print(f"ue->bridge {1e3 * (b[0] - t):6.2f} ms  lead {b[1] / 1e3:+7.2f} ms  dl_backlog {b[2]:.2f} ms")
