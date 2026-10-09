#!/usr/bin/env python3
"""Combine scheduler-benchmark runs into a cell-bias-free comparison.

One run compares cell a against cell b. The two cells share the channel and
the traffic, but not everything: each has its own broker, gNB process and
CPU share, so a run's difference d = metric(a) - metric(b) is

    d = (policy effect, signed by which cell ran which policy) + cell bias.

Running the same seed twice with the policies swapped cancels the bias: with
d1 from (a=X, b=Y) and d2 from (a=Y, b=X), the policy effect X - Y is
(d1 - d2) / 2 and the cell bias is (d1 + d2) / 2. An A/A run (same policy in
both cells) measures the bias directly and checks that estimate.

Input: report directories (results/reports/ocudu-scheduler-benchmark/<ts>).
Output: JSON with, per paired metric, every run's difference and CI, the
swap-pair effects and bias estimates, and a plain-text table on stderr.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
from typing import Any, Sequence

METRICS = (
    ("aggregate_dl_mbps", "aggregate DL throughput", "Mb/s"),
    ("ue0_dl_mbps", "UE A (cell index 0) DL throughput", "Mb/s"),
    ("ue1_dl_mbps", "UE B (cell index 1) DL throughput", "Mb/s"),
    ("jain_dl", "Jain fairness, DL throughput", ""),
    ("ue0_dl_delay_p99_ms", "UE A DL delay p99", "ms"),
    ("ue1_dl_delay_p99_ms", "UE B DL delay p99", "ms"),
    ("ue0_pdb_violation", "UE A PDB violation rate", ""),
    ("ue1_pdb_violation", "UE B PDB violation rate", ""),
    ("ue0_drop", "UE A drop rate", ""),
    ("ue1_drop", "UE B drop rate", ""),
    ("dl_prb_util", "DL PRB utilization", ""),
    ("ue0_dl_wait_max_ms", "UE A worst DL wait per second", "ms"),
    ("ue1_dl_wait_max_ms", "UE B worst DL wait per second", "ms"),
    ("decision_us", "scheduler decision time", "us"),
)


def load(report_dir: pathlib.Path) -> dict[str, Any]:
    report = json.loads((report_dir / "benchmark-report.json").read_text(encoding="utf-8"))
    gate = report_dir / "gate-summary.json"
    report["_gate"] = json.loads(gate.read_text(encoding="utf-8")) if gate.exists() else None
    report["_dir"] = str(report_dir)
    return report


def channel_records(report_dir: pathlib.Path) -> dict[int, tuple[str, list, dict]]:
    """grid index -> (profile SHA-256, taps per link, positions), from the bridge status log."""

    log = pathlib.Path(str(report_dir).replace("/results/reports/", "/results/logs/")) / "sionna-status.jsonl"
    out: dict[int, tuple[str, list, dict]] = {}
    if not log.exists():
        return out
    with log.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if '"sionna_rt_update"' not in line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            timeline = record.get("timeline") or {}
            if timeline.get("phase") == "run" and record.get("profile_sha256"):
                taps = sorted((c.get("link_id"), [(x["delay_samples"], x["gain_db"], x["phase_rad"])
                                                  for x in c.get("taps", [])]) for c in record.get("channels", []))
                out[int(timeline["grid_index"])] = (record["profile_sha256"], taps, record.get("positions") or {})
    return out


def reproducibility(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Runs with the same seed: how far apart is the channel solved at each shared grid point?

    Positions must match exactly (they are a function of the seed and the
    grid index). The solved taps match bit for bit at most grid points; where
    they do not, the GPU's floating-point summation order differed, and the
    largest gain, phase and delay differences say how much that matters.
    """

    by_seed: dict[int, list[dict[str, Any]]] = {}
    for r in reports:
        by_seed.setdefault(r["seed"], []).append(r)
    out = []
    for seed, group in sorted(by_seed.items()):
        records = [(r["_dir"], channel_records(pathlib.Path(r["_dir"]))) for r in group]
        for i in range(len(records)):
            for j in range(i + 1, len(records)):
                (da, a), (db, b) = records[i], records[j]
                common = sorted(set(a) & set(b))
                same = positions_differ = structure_differs = 0
                max_gain = max_phase = max_delay = 0.0
                for k in common:
                    (ha, ta, pa), (hb, tb, pb) = a[k], b[k]
                    positions_differ += pa != pb
                    if ha == hb:
                        same += 1
                        continue
                    if [link for link, _ in ta] != [link for link, _ in tb] or \
                            any(len(x) != len(y) for (_, x), (_, y) in zip(ta, tb)):
                        structure_differs += 1
                        continue
                    for (_, x), (_, y) in zip(ta, tb):
                        for (d1, g1, p1), (d2, g2, p2) in zip(x, y):
                            max_delay = max(max_delay, abs(d1 - d2))
                            max_gain = max(max_gain, abs(g1 - g2))
                            dp = abs(p1 - p2) % (2 * math.pi)
                            max_phase = max(max_phase, min(dp, 2 * math.pi - dp))
                out.append({"seed": seed, "runs": [da, db], "common_grid_points": len(common),
                            "identical": same, "positions_differ": positions_differ,
                            "tap_structure_differs": structure_differs,
                            "max_abs_gain_db": max_gain, "max_abs_phase_rad": max_phase,
                            "max_abs_delay_samples": max_delay})
    return out


def half_width(ci: Sequence[float] | None) -> float | None:
    return None if not ci else (ci[1] - ci[0]) / 2.0


def summarize(reports: list[dict[str, Any]]) -> dict[str, Any]:
    runs = []
    for r in reports:
        lanes = r["lanes"]
        runs.append({
            "dir": r["_dir"], "seed": r["seed"],
            "a": lanes["a"]["scheduler"], "b": lanes["b"]["scheduler"],
            "valid_seconds": r["segments_valid"], "measured_seconds": r["segments_complete"],
            "gate": (r["_gate"] or {}).get("status"),
            "slots_per_s": {c: lanes[c]["metrics"]["resource_utilization"]["slots_per_s"] for c in ("a", "b")},
            "pairs": {key: r["comparison"].get(key) for key, _, _ in METRICS},
        })
    policies = sorted({run[c] for run in runs for c in ("a", "b")})
    effects: dict[str, Any] = {}
    if len(policies) == 2:
        x, y = policies
        by_seed: dict[int, dict[str, dict[str, Any]]] = {}
        for run in runs:
            if run["a"] != run["b"]:
                by_seed.setdefault(run["seed"], {})[f"{run['a']}|{run['b']}"] = run
        for key, label, unit in METRICS:
            pairs, biases = [], []
            for seed, assignments in sorted(by_seed.items()):
                first, second = assignments.get(f"{x}|{y}"), assignments.get(f"{y}|{x}")
                if not first or not second:
                    continue
                p1, p2 = first["pairs"].get(key) or {}, second["pairs"].get(key) or {}
                if p1.get("mean_diff") is None or p2.get("mean_diff") is None:
                    continue
                h1, h2 = half_width(p1.get("ci95")), half_width(p2.get("ci95"))
                effect = (p1["mean_diff"] - p2["mean_diff"]) / 2.0
                bias = (p1["mean_diff"] + p2["mean_diff"]) / 2.0
                h = math.sqrt(h1 * h1 + h2 * h2) / 2.0 if h1 is not None and h2 is not None else None
                pairs.append({"seed": seed, "effect": effect, "bias": bias, "ci95_half_width": h,
                              "runs": [first["dir"], second["dir"]]})
                biases.append(bias)
            aa = [run["pairs"][key]["mean_diff"] for run in runs
                  if run["a"] == run["b"] and (run["pairs"].get(key) or {}).get("mean_diff") is not None]
            effects[key] = {
                "label": label, "unit": unit, "difference": f"{x} - {y}",
                "swap_pairs": pairs,
                "mean_effect": sum(p["effect"] for p in pairs) / len(pairs) if pairs else None,
                "mean_bias_from_swaps": sum(biases) / len(biases) if biases else None,
                "aa_bias": sum(aa) / len(aa) if aa else None,
            }
    return {"kind": "ocudu-scheduler-benchmark-campaign", "policies": policies, "runs": runs, "effects": effects,
            "channel_reproducibility": reproducibility(reports)}


def table(summary: dict[str, Any]) -> str:
    lines = [f"runs: {len(summary['runs'])}"]
    for run in summary["runs"]:
        sps = run["slots_per_s"]
        lines.append(f"  seed {run['seed']} a={run['a']} b={run['b']} valid {run['valid_seconds']}/"
                     f"{run['measured_seconds']} s, slots/s a {sps['a']:.0f} b {sps['b']:.0f}, gate {run['gate']}")
    for check in summary.get("channel_reproducibility", []):
        lines.append(f"  seed {check['seed']} {pathlib.Path(check['runs'][0]).name} vs {pathlib.Path(check['runs'][1]).name}: "
                     f"{check['identical']}/{check['common_grid_points']} grid points bit-identical, positions differ at "
                     f"{check['positions_differ']}, tap structure at {check['tap_structure_differs']}; max |dgain| "
                     f"{check['max_abs_gain_db']:.2g} dB, |dphase| {check['max_abs_phase_rad']:.2g} rad, "
                     f"|ddelay| {check['max_abs_delay_samples']:.2g} samples")
    if summary["effects"]:
        lines.append("")
        lines.append(f"{'metric':38s} {'effect':>10s} {'+-95%':>8s} {'bias(swap)':>11s} {'bias(A/A)':>10s}")
        for key, e in summary["effects"].items():
            if e["mean_effect"] is None:
                continue
            hw = [p["ci95_half_width"] for p in e["swap_pairs"] if p["ci95_half_width"] is not None]
            h = math.sqrt(sum(v * v for v in hw)) / len(hw) if hw else None
            fmt = lambda v: "-" if v is None else f"{v:.4g}"  # noqa: E731
            lines.append(f"{e['label'][:38]:38s} {fmt(e['mean_effect']):>10s} {fmt(h):>8s} "
                         f"{fmt(e['mean_bias_from_swaps']):>11s} {fmt(e['aa_bias']):>10s}  {e['unit']}")
        lines.append(f"effect = {summary['policies'][0]} - {summary['policies'][1]}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("reports", nargs="+", type=pathlib.Path, help="report directories")
    parser.add_argument("--out", type=pathlib.Path)
    args = parser.parse_args(argv)
    summary = summarize([load(path) for path in args.reports])
    text = json.dumps(summary, indent=1, sort_keys=True)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    print(table(summary), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
