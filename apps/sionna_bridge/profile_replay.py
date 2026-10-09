#!/usr/bin/env python3
"""Measure channel generation at recorded positions, without a broker or radio.

This is a diagnostic replay, not a live/real-time qualification.
Host wall-time stages retain GPU waits where they naturally occur. In
particular, cir_numpy includes materialization and may wait for solver work.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib.metadata
import json
import math
import pathlib
import platform
import statistics
import time

if __package__:
    from . import run_bridge
else:
    import run_bridge


def summarize(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "mean": statistics.mean(values), "p50": statistics.median(values),
        "p90": ordered[math.ceil(0.90 * len(ordered)) - 1],
        "p99": ordered[math.ceil(0.99 * len(ordered)) - 1], "max": ordered[-1],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", type=pathlib.Path, required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--path-polylines", type=int, default=6)
    parser.add_argument("--samples-per-src", type=int)
    parser.add_argument("--pace-hz", type=float, default=0.0,
                        help="pace update starts; 0 runs back to back (no catch-up bursts)")
    args = parser.parse_args()
    if args.samples < 1 or args.rounds < 1 or args.warmup < 0 or args.path_polylines < 0:
        parser.error("samples/rounds must be positive; warmup/path-polylines nonnegative")
    if not math.isfinite(args.pace_hz) or args.pace_hz < 0:
        parser.error("pace-hz must be finite and nonnegative")
    rows_path = args.out.with_name(args.out.stem + "-samples.jsonl")
    if args.out.resolve() == args.replay.resolve() or rows_path.resolve() == args.replay.resolve():
        parser.error("output must not overwrite the replay input")
    records = []
    with args.replay.open() as source:
        for line in source:
            record = json.loads(line)
            if record.get("event") == "sionna_rt_update":
                records.append(record)
    if len(records) < args.samples:
        parser.error("replay contains fewer channel updates than requested samples")
    selected = [records[i * len(records) // args.samples] for i in range(args.samples)]
    bridge_args = run_bridge.parse_args([
        "--scenario-config", args.scenario, "--dry-run", "--update-hz", "10",
        "--profile-timing",
    ])
    # Explicit benchmark overrides, applied after the scenario defaults.
    bridge_args.path_polylines = args.path_polylines
    if args.samples_per_src is not None:
        if args.samples_per_src < 1:
            parser.error("samples-per-src must be positive")
        bridge_args.samples_per_src = args.samples_per_src
    scenario = run_bridge.SionnaScenario(bridge_args)
    rows = []
    args.out.parent.mkdir(parents=True, exist_ok=True)
    next_update = time.monotonic()
    with rows_path.open("w") as output:
        for index in range(args.warmup + args.samples * args.rounds):
            if args.pace_hz:
                time.sleep(max(0.0, next_update - time.monotonic()))
            warmup = index < args.warmup
            source_index = 0 if warmup else (index - args.warmup) % args.samples
            record = selected[source_index]
            # Recorded positions are already in scene coordinates; do not add
            # the arena offset again, or substitute the scripted walk.
            for node in scenario.node_ids:
                position = record["positions"][node]
                velocity = record["velocities_mps"][node]
                scenario.current_positions[node] = tuple(position)
                scenario.current_velocities[node] = tuple(velocity)
                for suffix in ("tx", "rx"):
                    obj = scenario.scene.get(f"{node}_{suffix}")
                    obj.position = position
                    obj.velocity = velocity
            started = time.monotonic()
            profiles, statuses, _ = scenario.trace_all_profiles()
            elapsed_ms = (time.monotonic() - started) * 1000.0
            if not profiles:
                raise RuntimeError("empty measured workload: no channel profiles")
            profile_data = {key: dataclasses.asdict(value) for key, value in sorted(profiles.items())}
            fingerprint = hashlib.sha256(json.dumps(
                profile_data,
                sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest()
            row = {
                "index": index, "warmup": warmup, "source_index": source_index,
                "round": None if warmup else (index - args.warmup) // args.samples,
                "source_unix_ms": record["update_started_unix_ms"],
                "generation_ms": elapsed_ms, "stages": scenario.generation_stages_ms,
                "profile_sha256": fingerprint,
                "profiles": profile_data,
                "ray_counts": {s["link_id"]: s["ray_count"] for s in statuses},
            }
            output.write(json.dumps(row) + "\n")
            output.flush()
            if not warmup:
                rows.append(row)
            if args.pace_hz:
                next_update = max(next_update + 1.0 / args.pace_hz, time.monotonic())
    if not any(any(r["ray_counts"].values()) for r in rows):
        raise RuntimeError("all measured channels have zero rays; not a useful generation benchmark")
    stages = {}
    for group, measurements in rows[0]["stages"].items():
        stages[group] = {
            name: summarize([r["stages"][group][name] for r in rows])
            for name in measurements
        }
    summary = {
        "kind": "recorded-position replay; no gNB/UE/broker",
        "host": platform.node(), "machine": platform.machine(),
        "replay": str(args.replay), "scenario": args.scenario,
        "samples": args.samples, "rounds": args.rounds, "warmup": args.warmup,
        "pace_hz": args.pace_hz,
        "path_polylines": args.path_polylines, "samples_per_src": bridge_args.samples_per_src,
        "sample_rate_hz": bridge_args.sample_rate_hz,
        "generation_ms": summarize([r["generation_ms"] for r in rows]), "stages": stages,
        "per_round_ms": {
            str(i): summarize([r["generation_ms"] for r in rows if r["round"] == i])
            for i in range(args.rounds)
        },
        "environment": run_bridge.scenario_environment(bridge_args),
        "recorded_environment": records[0].get("environment"),
        "versions": {name: importlib.metadata.version(name) for name in ("sionna-rt", "mitsuba", "drjit")},
        "bridge_sha256": hashlib.sha256(pathlib.Path(run_bridge.__file__).read_bytes()).hexdigest(),
        "replay_sha256": hashlib.sha256(args.replay.read_bytes()).hexdigest(),
        "scenario_sha256": hashlib.sha256(pathlib.Path(args.scenario).read_bytes()).hexdigest(),
        "timing_semantics": "host wall time, no added synchronization; GPU work can finish in cir_numpy",
        "live_difference": "generation_ms excludes live publish_runtime/tracing status writes",
    }
    args.out.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
