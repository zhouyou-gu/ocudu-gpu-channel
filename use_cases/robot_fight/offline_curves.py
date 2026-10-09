#!/usr/bin/env python3
"""Offline threshold curves for the balance policies (R7a): survival vs
constant one-way delay and vs a single blackout, at rest and cruising, for
`balance` and `balance_comp`. Writes <out>/curves.json and curves.md.

    python offline_curves.py --out results/robot-fight/r7a-offline --workers 12
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from concurrent.futures import ProcessPoolExecutor

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _one(job: dict) -> dict:
    import offline_loop  # noqa: WPS433
    r = offline_loop.run(job["policy"], job["delay_ms"], job.get("cruise"), job.get("duration", 6.0),
                         job.get("blackout_ms", 0.0), job.get("blackout_at", 2.5), params_override=job.get("params"))
    r["cruise"] = job.get("cruise")
    return r


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", required=True)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--policies", default="balance,balance_comp")
    p.add_argument("--delays", default="0,20,40,60,80,100,120,140,160,180,200")
    p.add_argument("--blackouts", default="100,200,300,400,500,600")
    p.add_argument("--blackout-base-delay-ms", type=float, default=15.0)
    p.add_argument("--cruises", default="0,0.5")
    p.add_argument("--duration", type=float, default=6.0)
    args = p.parse_args(argv)
    policies = args.policies.split(",")
    delays = [float(x) for x in args.delays.split(",")]
    blackouts = [float(x) for x in args.blackouts.split(",")]
    cruises = [float(x) for x in args.cruises.split(",")]
    jobs = []
    for pol in policies:
        for cv in cruises:
            for d in delays:
                jobs.append({"policy": pol, "delay_ms": d, "cruise": cv or None, "duration": args.duration, "kind": "delay"})
            for b in blackouts:
                jobs.append({"policy": pol, "delay_ms": args.blackout_base_delay_ms, "cruise": cv or None,
                             "duration": args.duration, "blackout_ms": b, "kind": "blackout"})
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        results = list(ex.map(_one, jobs))
    for job, r in zip(jobs, results):
        r["kind"] = job["kind"]
    (out / "curves.json").write_text(json.dumps(results, indent=1) + "\n")
    lines = []
    for kind, axis, label in (("delay", "delay_ms", "one-way delay ms"), ("blackout", "blackout_ms", "single blackout ms (base delay %g ms)" % args.blackout_base_delay_ms)):
        xs = delays if kind == "delay" else blackouts
        lines.append(f"\n**{label}** — cell = t_fall s (fell) or `ok` (max |pitch| rad)\n")
        lines.append("| policy | cruise m/s | " + " | ".join(f"{x:g}" for x in xs) + " |")
        lines.append("|---|---|" + "---|" * len(xs))
        for pol in policies:
            for cv in cruises:
                cells = []
                for x in xs:
                    r = next(r for r in results if r["kind"] == kind and r["policy"] == pol and (r["cruise"] or 0) == cv and r[axis] == x)
                    cells.append(f"**{r['t_fall']}**" if r["fell"] else f"ok ({r['max_pitch']})")
                lines.append(f"| {pol} | {cv:g} | " + " | ".join(cells) + " |")
    md = "\n".join(lines) + "\n"
    (out / "curves.md").write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
