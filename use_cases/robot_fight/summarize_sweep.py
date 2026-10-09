#!/usr/bin/env python3
"""One markdown table over several fight.py output directories (a sweep):
per batch the brains, the handicap, ue0:ue1 (draws), ue0 share with its
Wilson CI, how each robot lost, RTT and RTF.

    python summarize_sweep.py results/robot-fight/r7a-sweep/d0 results/robot-fight/r7a-sweep/d20 ...
"""

from __future__ import annotations

import json
import pathlib
import sys


def row(path: pathlib.Path) -> str:
    s = json.loads((path / "summary.json").read_text())
    brains = "/".join(s.get("brain_policies") or [s["policy"]] * 2)
    lost = " / ".join(
        f"{k}: " + (",".join(f"{r}={c}" for r, c in v.items()) or "-") for k, v in s["lost_by"].items())
    r0, r1 = s["robots"]
    ci = s["ue0_win_share_ci95"]
    ci_txt = f"{s['ue0_win_share_of_decided']} ({ci[0]:.2f}–{ci[1]:.2f})" if ci else "-"
    ttw = s["time_to_win_s"]
    return (f"| {path.name} | {brains} | {s['handicap'] or 'none'} | {s['wins']['ue0']}:{s['wins']['ue1']} ({s['draws']}) | "
            f"{ci_txt} | {lost} | {r0['brain_rtt_ms_p50_median']:.1f} / {r1['brain_rtt_ms_p50_median']:.1f} | "
            f"{'-' if not ttw else ttw['mean']} | {s['rtf']['min']:.4f} |")


def main(argv: list[str]) -> int:
    lines = ["| batch | brains (ue0/ue1) | handicap | ue0:ue1 (draws) | ue0 share (95 % CI) | lost by | RTT p50 ms ue0 / ue1 | t-win s | RTF min |",
             "|---|---|---|---|---|---|---|---|---|"]
    for arg in argv:
        lines.append(row(pathlib.Path(arg)))
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
