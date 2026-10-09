#!/usr/bin/env python3
"""Summarize R7 live trials; compare only valid real-time, complete fights.

Example: analyze_r7_battle.py comp0=/logs/a comp1=/logs/b \
    --pair baseline=comp0,comp1 --json result.json --markdown result.md
Metrics are medians of per-fight summaries, not pooled packet percentiles.
"""
from __future__ import annotations
import argparse
import collections
import json
import math
import pathlib
import re
import statistics

POLICIES = ("balance", "balance_comp")


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def wilson(wins, total):
    if not total:
        return None
    p, z = wins / total, 1.959963984540054
    den = 1 + z*z/total
    centre = (p + z*z/(2*total))/den
    half = z*math.sqrt(p*(1-p)/total + z*z/(4*total*total))/den
    return {"share": p, "ci95": [centre-half, centre+half], "decided": total}


def load_rows(path):
    path = pathlib.Path(path)
    summary = path / "fights" / "summary.jsonl"
    if not summary.exists():
        summary = path / "summary.jsonl"
    rows = []
    base = path / "fights" if (path / "fights").is_dir() else path
    directories = sorted(d for d in base.iterdir() if d.is_dir() and re.fullmatch(r"(?:fight-|f)\d+", d.name))
    if summary.exists():
        for i, line in enumerate(summary.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                rows.append(row if isinstance(row, dict) else {"error": "non-object record", "line": i})
            except ValueError:
                rows.append({"error": "invalid JSON", "line": i})
        # A copied summary retains remote absolute paths; match by basename.
        reported = {pathlib.Path(r["dir"]).name for r in rows if isinstance(r.get("dir"), str)}
        for r in rows:
            if not r.get("dir") and isinstance(r.get("fight"), int):
                reported.update(d.name for d in directories if int(re.search(r"\d+$", d.name).group()) == r["fight"])
        for directory in directories:
            if directory.name not in reported:
                rows.append({"dir": str(directory), "error": "unreported fight directory (possibly interrupted)"})
        return rows
    for directory in directories:
        try:
            if (directory / "fight.json").exists():
                row = json.loads((directory / "fight.json").read_text())
            else:
                row = json.loads((directory / "arena.json").read_text())
                row["brains"] = [json.loads((directory / f"brain{i}.json").read_text()) for i in (0, 1)]
            rows.append(row)
        except (OSError, ValueError) as exc:
            rows.append({"dir": str(directory), "error": str(exc)})
    return rows


def rejection(row, rtf_min=0.98, rtf_max=1.02):
    if row.get("error") or row.get("arena_error") or row.get("arena_status", 0) != 0:
        return "error"
    if row.get("lockstep") is not False:
        return "lockstep_or_unknown"
    if not number(row.get("rtf")) or not rtf_min <= row["rtf"] <= rtf_max:
        return "rtf"
    if row.get("reason") not in {"fall", "ring_out", "timeout", "timeout_edge", "both_fell", "both_out"}:
        return "incomplete_reason"
    if "winner" not in row or row["winner"] not in (None, 0, 1) or not isinstance(row.get("seed"), int):
        return "incomplete_outcome"
    brains = row.get("brains")
    if not isinstance(brains, list) or len(brains) != 2 or any(not isinstance(b, dict) for b in brains):
        return "incomplete_brains"
    if {b.get("robot_id") for b in brains} != {0, 1}:
        return "incomplete_brains"
    for b in brains:
        if b.get("error") or b.get("policy") not in POLICIES or b.get("outcome") not in {"won", "lost", "draw"}:
            return "incomplete_brains"
    return None


def aggregate(rows):
    wins = collections.Counter()
    side_wins = collections.Counter()
    falls = collections.Counter()
    metrics = {p: collections.defaultdict(list) for p in POLICIES}
    draws = 0
    for row in rows:
        brains = {b["robot_id"]: b for b in row["brains"]}
        winner = row["winner"]
        if winner is None:
            draws += 1
        else:
            wins[brains[winner]["policy"]] += 1
            side_wins[f"ue{winner}"] += 1
        for robot, b in brains.items():
            p = b["policy"]
            if (row["reason"] == "fall" and winner is not None and robot != winner) or row["reason"] == "both_fell":
                falls[p] += 1
            for group, keys in (("rtt_us", ("p50", "p99")), ("state_one_way_us", ("p50", "p99")),
                                ("link_est", ("rtt_min_us", "cmd_one_way_us", "state_age_us", "horizon_ms_mean"))):
                for key in keys:
                    value = (b.get(group) or {}).get(key)
                    if number(value):
                        metrics[p][f"{group}.{key}"].append(value)
            robot_row = next((r for r in (row.get("robots") or []) if r.get("robot") == robot or r.get("node_id") == f"ue{robot}"), {})
            for key in ("p50", "p99"):
                value = (robot_row.get("one_way_us") or {}).get(key)
                if number(value):
                    metrics[p][f"command_one_way_us.{key}"].append(value)
    return {"fights": len(rows), "wins": {p: wins[p] for p in POLICIES}, "draws": draws,
            "side_wins": {side: side_wins[side] for side in ("ue0", "ue1")},
            "falls": {p: falls[p] for p in POLICIES},
            "comp_share_of_decided": wilson(wins["balance_comp"], len(rows)-draws),
            "metrics_median_of_fights": {p: {k: {"median": statistics.median(v), "n": len(v)} for k,v in values.items()} for p,values in metrics.items()}}


def analyse(inputs, pairs=(), rtf_min=0.98, rtf_max=1.02):
    if not number(rtf_min) or not number(rtf_max) or not 0 < rtf_min <= rtf_max:
        raise ValueError("RTF bounds must be finite, positive and ordered min <= max")
    report = {"rtf_bounds": [rtf_min, rtf_max], "metric_note": "Medians of per-fight summaries; Wilson CI is descriptive and treats fights as independent.", "runs": {}, "pairs": {}}
    accepted = {}
    for tag, path in inputs.items():
        valid, rejected = [], []
        seen = set()
        for row in load_rows(path):
            why = rejection(row, rtf_min, rtf_max)
            if why is None and row["seed"] in seen:
                why = "duplicate_seed"
            if why:
                rejected.append({"fight": row.get("fight"), "seed": row.get("seed"), "reason": why,
                                 "dir": row.get("dir"), "detail": row.get("error") or row.get("arena_error")})
            else:
                seen.add(row["seed"])
                valid.append(row)
        mixed = [r for r in valid if len({b["policy"] for b in r["brains"]}) == 2]
        controls = {p: aggregate([r for r in valid if all(b["policy"] == p for b in r["brains"])]) for p in POLICIES}
        # Controller win share has no comparison meaning in same-policy controls.
        for control in controls.values():
            control.pop("comp_share_of_decided")
        attach = None
        for candidate in (pathlib.Path(path)/"report/attach-summary.json", pathlib.Path(path)/"attach-summary.json"):
            if candidate.exists():
                try:
                    attach = json.loads(candidate.read_text())
                except (OSError, ValueError) as exc:
                    attach = {"error": str(exc)}
                break
        report["runs"][tag] = {"attach_summary": attach, "path": str(path), "valid": len(valid), "rejected": rejected, "comparison": aggregate(mixed), "controls": controls}
        accepted[tag] = {r["seed"]: r for r in mixed}
    for name, left, right in pairs:
        a, b = accepted[left], accepted[right]
        common = sorted(set(a) & set(b))
        matched = [s for s in common if next(x["robot_id"] for x in a[s]["brains"] if x["policy"] == "balance_comp") != next(x["robot_id"] for x in b[s]["brains"] if x["policy"] == "balance_comp")]
        report["pairs"][name] = {"tags": [left, right], "seeds": matched, "excluded_same_assignment": sorted(set(common)-set(matched)),
                                 "unpaired_seeds": {left: sorted(set(a)-set(b)), right: sorted(set(b)-set(a))},
                                 "comparison": aggregate([r for s in matched for r in (a[s], b[s])])}
    return report


def markdown(report):
    lines = ["# R7 controller comparison", "", report["metric_note"], "", "| Run / pair | Fights | Comp wins | Plain wins | Draws | Comp share (95% CI) |", "|---|---:|---:|---:|---:|---|"]
    for label, item in [(k, v) for k,v in report["runs"].items()] + [("pair:"+k,v) for k,v in report["pairs"].items()]:
        s = item["comparison"]
        ci = s["comp_share_of_decided"]
        interval = "—" if ci is None else f"{ci['share']:.3f} ({ci['ci95'][0]:.3f}–{ci['ci95'][1]:.3f})"
        lines.append(f"| {label} | {s['fights']} | {s['wins']['balance_comp']} | {s['wins']['balance']} | {s['draws']} | {interval} |")
    for tag, run in report["runs"].items():
        lines.extend(["", f"{tag}: attach status={(run['attach_summary'] or {}).get('status', 'unknown')}; {run['valid']} valid, {len(run['rejected'])} rejected. Same-policy controls: " + ", ".join(f"{p}={s['fights']} fights (UE0/UE1 wins {s['side_wins']['ue0']}/{s['side_wins']['ue1']})" for p,s in run['controls'].items()) + "."])
    lines.extend(["", "Full fall counts, link metrics, exclusions and paired seeds are in the JSON report.", ""])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", metavar="TAG=LOG_DIR")
    parser.add_argument("--pair", action="append", default=[], metavar="NAME=TAG0,TAG1")
    parser.add_argument("--rtf-min", type=float, default=0.98)
    parser.add_argument("--rtf-max", type=float, default=1.02)
    parser.add_argument("--json", type=pathlib.Path)
    parser.add_argument("--markdown", type=pathlib.Path)
    args = parser.parse_args()
    try:
        inputs = dict(x.split("=", 1) for x in args.runs)
        pairs = [(name, *tags.split(",")) for name,tags in (x.split("=", 1) for x in args.pair)]
        if any(len(p) != 3 or p[1] not in inputs or p[2] not in inputs for p in pairs):
            raise ValueError("pairs must name two existing run tags")
        report = analyse(inputs, pairs, args.rtf_min, args.rtf_max)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2)+"\n")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(markdown(report))
    print(markdown(report))


if __name__ == "__main__":
    main()
