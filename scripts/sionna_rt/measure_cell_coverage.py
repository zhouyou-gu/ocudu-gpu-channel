#!/usr/bin/env python3
"""Measure which cell covers a mobile node, point by point around its route.

A scenario's prose makes claims about how its cells split a walker's lap --
"gnb1 covers the far side", "the link hands over rather than sitting on one
cell". Those claims were originally measured by hand and the method was not
written down, so the next route change silently invalidated them. This script
is that method, kept next to the bridge it borrows the scene from.

It walks `--node` around its route in `--samples` equal steps of elapsed time,
ray-traces the scene at each stop exactly as the bridge does, and reports the
strongest downlink tap each cell delivers there. A cell covers a sample when
that gain is at or above `--floor-db`; the summary is the per-cell count, how
many samples both reach, and how many neither does.

    python measure_cell_coverage.py --scenario-config use_cases/configs/sionna/scenarios/sutd/multi-gnb-sutd.json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from run_bridge import SionnaScenario, parse_args as bridge_parse_args  # noqa: E402

# What the adapter writes for a link the solver found no usable path on
# (channel_adapter.rays_to_taps, `outage_gain_db`). A cell that only ever
# delivers this is not covering anything, however low the floor is set.
OUTAGE_GAIN_DB = -100.0


def lap_seconds(motion) -> float:
    """How long `motion` takes to return to where it started."""

    if len(motion.waypoints) < 2 or motion.speed_mps <= 0.0:
        raise SystemExit("node does not walk a route; nothing to sample around")
    _, _, total = motion._route()  # noqa: SLF001 - the route is the measurement
    # A pingpong walker only repeats after going out and back.
    return total / motion.speed_mps * (1.0 if motion.route_mode == "loop" else 2.0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario-config", required=True, type=pathlib.Path)
    parser.add_argument("--node", default="ue1", help="the mobile node to walk")
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument(
        "--floor-db", type=float, default=OUTAGE_GAIN_DB,
        help=(
            "strongest-tap gain at or above which a cell counts as covering; "
            "the default counts any traced path, since a link in outage is "
            "excluded regardless"
        ),
    )
    parser.add_argument("--json", type=pathlib.Path, help="write the table here")
    args = parser.parse_args(argv)

    scenario = SionnaScenario(
        bridge_parse_args(["--scenario-config", str(args.scenario_config)])
    )
    if args.node not in scenario.definition.nodes:
        raise SystemExit(f"no node {args.node} in {args.scenario_config}")
    cells = [node_id for node_id in scenario.node_ids if node_id.startswith("gnb")]
    watched = {
        cell: f"{cell}>{args.node}"
        for cell in cells
        if any(
            link.source == cell and link.destination == args.node
            and link.direction == "downlink"
            for link in scenario.links
        )
    }
    if not watched:
        raise SystemExit(f"no downlink links reach {args.node}")

    period = lap_seconds(scenario.motion[args.node])
    rows = []
    for index in range(args.samples):
        elapsed = period * index / args.samples
        positions = scenario.update_positions(elapsed)
        _, statuses, _ = scenario.trace_all_profiles()
        gains = {}
        for cell, link_id in watched.items():
            status = next(
                (
                    entry for entry in statuses
                    if entry.get("source") == cell
                    and entry.get("destination") == args.node
                    and entry.get("direction") == "downlink"
                ),
                None,
            )
            gains[cell] = None if status is None else status.get("strongest_tap_gain_db")
        rows.append({
            "index": index,
            "elapsed_seconds": round(elapsed, 3),
            "position_m": [round(value, 2) for value in positions[args.node]],
            "gain_db": {cell: gain for cell, gain in gains.items()},
        })
        print(
            f"{index:3}  t={elapsed:7.1f}s  "
            + "  ".join(
                f"{cell}={'none' if gain is None else format(gain, '8.2f')}"
                for cell, gain in gains.items()
            ),
            flush=True,
        )

    def covers(gain) -> bool:
        return gain is not None and gain > OUTAGE_GAIN_DB and gain >= args.floor_db

    counts = {
        cell: sum(covers(row["gain_db"][cell]) for row in rows) for cell in watched
    }
    both = sum(all(covers(row["gain_db"][cell]) for cell in watched) for row in rows)
    neither = sum(
        not any(covers(row["gain_db"][cell]) for cell in watched) for row in rows
    )
    stronger = {
        cell: sum(
            row["gain_db"][cell] is not None
            and all(
                row["gain_db"][other] is None
                or row["gain_db"][cell] >= row["gain_db"][other]
                for other in watched
            )
            for row in rows
        )
        for cell in watched
    }
    summary = {
        "node": args.node,
        "samples": args.samples,
        "lap_seconds": round(period, 2),
        "floor_db": args.floor_db,
        "covered": counts,
        "both": both,
        "neither": neither,
        "stronger": stronger,
    }
    print("\n" + json.dumps(summary, indent=2))
    if args.json:
        args.json.write_text(json.dumps({"summary": summary, "samples": rows}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
