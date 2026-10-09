#!/usr/bin/env python3
"""Path gain across the robot ring, on a grid, traced exactly as the bridge does.

Places `--node` (a UE of the scenario) on every point of an XY grid over the
ring, keeps the other nodes where their routes start, and records what the
bridge's own `trace_all_profiles` reports for the node's downlink and uplink:
`strongest_tap_gain_db` (what the broker will apply, i.e. after
`--gain-offset-db`) and `total_path_power_db` (physical, before the offset).

    /workspace/sionna-venv/bin/python coverage_grid.py \
        --scenario-config ../../scenarios/robot_ring/robot-ring.json --step-m 0.5 \
        --csv ring-coverage.csv --png ring-coverage.png

Only the Sionna venv is needed; matplotlib is optional (an ASCII map is
printed either way).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import pathlib
import statistics
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[4] / "scripts" / "sionna_rt"))

from run_bridge import SionnaScenario, parse_args as bridge_parse_args  # noqa: E402

OUTAGE_GAIN_DB = -100.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario-config", required=True, type=pathlib.Path)
    parser.add_argument("--node", default="ue0")
    parser.add_argument("--step-m", type=float, default=0.5)
    parser.add_argument("--radius-m", type=float, default=3.75,
                        help="grid points farther than this from the origin are skipped")
    parser.add_argument("--height-m", type=float, default=0.4)
    parser.add_argument("--csv", type=pathlib.Path)
    parser.add_argument("--png", type=pathlib.Path)
    parser.add_argument("--json", type=pathlib.Path)
    parser.add_argument("--bridge-arg", action="append", default=[],
                        help="extra run_bridge.py argument, e.g. --bridge-arg=--samples-per-src=50000")
    args = parser.parse_args(argv)

    bridge_args = ["--scenario-config", str(args.scenario_config)] + list(args.bridge_arg)
    scenario = SionnaScenario(bridge_parse_args(bridge_args))
    if args.node not in scenario.definition.nodes:
        raise SystemExit(f"no node {args.node} in {args.scenario_config}")
    cell = next(node_id for node_id in scenario.node_ids if node_id.startswith("gnb"))

    scenario.update_positions(0.0)
    tx = scenario.scene.get(f"{args.node}_tx")
    rx = scenario.scene.get(f"{args.node}_rx")
    tx.velocity = (0.0, 0.0, 0.0)
    rx.velocity = (0.0, 0.0, 0.0)

    count = int(math.floor(args.radius_m / args.step_m))
    coords = [k * args.step_m for k in range(-count, count + 1)]
    rows: list[dict] = []
    started = time.monotonic()
    solves = 0
    for y in coords:
        for x in coords:
            if math.hypot(x, y) > args.radius_m:
                continue
            position = (x, y, args.height_m)
            tx.position = position
            rx.position = position
            _, statuses, timings = scenario.trace_all_profiles()
            solves += 1

            def pick(source: str, destination: str) -> dict:
                return next(
                    (s for s in statuses if s.get("source") == source
                     and s.get("destination") == destination), {})

            down = pick(cell, args.node)
            up = pick(args.node, cell)
            rows.append({
                "x_m": x, "y_m": y,
                "dl_tap_db": down.get("strongest_tap_gain_db"),
                "dl_power_db": down.get("total_path_power_db"),
                "dl_rays": down.get("ray_count"),
                "ul_tap_db": up.get("strongest_tap_gain_db"),
                "ul_power_db": up.get("total_path_power_db"),
                "ul_rays": up.get("ray_count"),
                "trace_ms": round(sum(timings.values()), 2),
            })
    elapsed = time.monotonic() - started

    def side(row: dict) -> str:
        return "los(+x)" if row["x_m"] > 0.5 else ("nlos(-x)" if row["x_m"] < -0.5 else "centre")

    covered = [r for r in rows if r["dl_tap_db"] is not None and r["dl_tap_db"] > OUTAGE_GAIN_DB]
    outage = [r for r in rows if r not in covered]
    summary = {
        "node": args.node, "cell": cell, "points": len(rows), "solves": solves,
        "seconds": round(elapsed, 1), "ms_per_solve": round(1000 * elapsed / max(1, solves), 1),
        "outage_points": len(outage),
        "dl_tap_db": {"min": min(r["dl_tap_db"] for r in covered),
                      "median": statistics.median(r["dl_tap_db"] for r in covered),
                      "max": max(r["dl_tap_db"] for r in covered)} if covered else None,
        "by_side": {},
    }
    for name in ("los(+x)", "centre", "nlos(-x)"):
        part = [r for r in covered if side(r) == name]
        if part:
            summary["by_side"][name] = {
                "points": len(part),
                "dl_tap_db_min": round(min(r["dl_tap_db"] for r in part), 2),
                "dl_tap_db_median": round(statistics.median(r["dl_tap_db"] for r in part), 2),
                "dl_tap_db_max": round(max(r["dl_tap_db"] for r in part), 2),
                "dl_rays_median": statistics.median(r["dl_rays"] for r in part),
            }
        summary["by_side"][name + "_outage"] = sum(1 for r in outage if side(r) == name)

    # ASCII map: rows are y descending, columns x ascending, one char per point.
    print(f"strongest DL tap gain (dB) for {args.node}; gNB {cell} at "
          f"{scenario.current_positions[cell]}; '.' = outside ring, '#' = outage")
    ramp = " ▁▂▃▄▅▆▇█"
    lookup = {(r["x_m"], r["y_m"]): r for r in rows}
    finite = [r["dl_tap_db"] for r in covered]
    lo, hi = (min(finite), max(finite)) if finite else (0.0, 1.0)
    for y in reversed(coords):
        line = ""
        for x in coords:
            r = lookup.get((x, y))
            if r is None:
                line += " ."
            elif r["dl_tap_db"] is None or r["dl_tap_db"] <= OUTAGE_GAIN_DB:
                line += " #"
            else:
                level = 0 if hi == lo else int(round((r["dl_tap_db"] - lo) / (hi - lo) * (len(ramp) - 1)))
                line += " " + ramp[level]
        print(f"y={y:5.1f} |{line}")
    print(f"scale: ' '={lo:.1f} dB … '█'={hi:.1f} dB")
    print(json.dumps(summary, indent=2))

    if args.csv:
        with args.csv.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    if args.json:
        args.json.write_text(json.dumps({"summary": summary, "points": rows}, indent=2) + "\n")
    if args.png:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import numpy as np
        except ImportError:
            print("matplotlib/numpy not available; PNG skipped", file=sys.stderr)
        else:
            grid = np.full((len(coords), len(coords)), np.nan)
            for r in rows:
                i = coords.index(r["y_m"])
                j = coords.index(r["x_m"])
                grid[i, j] = r["dl_tap_db"] if r["dl_tap_db"] is not None else OUTAGE_GAIN_DB
            fig, ax = plt.subplots(figsize=(6, 5))
            extent = (coords[0] - args.step_m / 2, coords[-1] + args.step_m / 2,
                      coords[0] - args.step_m / 2, coords[-1] + args.step_m / 2)
            image = ax.imshow(grid, origin="lower", extent=extent, cmap="viridis")
            fig.colorbar(image, ax=ax, label="strongest DL tap gain (dB, after gain offset)")
            for obj in scenario.scene_geometry["objects"]:
                if obj["id"].startswith("building_pillar"):
                    xs = [p[0] for p in obj["footprint_xy_m"]] + [obj["footprint_xy_m"][0][0]]
                    ys = [p[1] for p in obj["footprint_xy_m"]] + [obj["footprint_xy_m"][0][1]]
                    ax.plot(xs, ys, color="white", linewidth=1.5)
            circle = plt.Circle((0, 0), args.radius_m, fill=False, color="white", linestyle="--")
            ax.add_patch(circle)
            ax.set_title(f"robot_ring: {cell} → {args.node} (gNB at +x)")
            ax.set_xlabel("x (m)")
            ax.set_ylabel("y (m)")
            fig.tight_layout()
            fig.savefig(args.png, dpi=120)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
