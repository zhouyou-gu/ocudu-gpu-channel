#!/usr/bin/env python3
"""Regenerate the §21 perf figures from a `perf-fanin-sweep.sh` sweep.json.

Reads a sweep.json (one JSON object per line, the format
`scripts/remote/perf-fanin-sweep.sh` emits) and writes three SVGs to the
output directory:

  perf-T-fanin-breakdown.svg  — Set 1 (1-to-N) per-phase breakdown
                                (mirrors Diagram T in the HTML doc)
  perf-U-mn-collapse.svg      — Every (M, N) config's p99 model_mix
                                (mirrors Diagram U)
  perf-W-pcie-h2d.svg         — H2D µs by config (the PCIe wall)
                                (mirrors Diagram W)

The doc's inline SVGs are bespoke and narrative-tuned; this companion script
makes the same numbers reproducible from a new sweep without touching the doc.
Run after fetching a fresh sweep.json from the RTX workstation.

Usage:
  python3 scripts/figures/regen_perf_figures.py SWEEP.json [--out DIR]

Diagram O (CPU vs CUDA baseline) is not regenerated here because the
fan-in sweep is CUDA-only. A CPU-vs-CUDA companion needs a separate sweep
run with both backends.
"""
import argparse
import json
import sys
from pathlib import Path


def load_sweep(path):
    rows = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def get_metric(row, name, field="p99_us"):
    m = row.get("metrics", {}).get(name)
    return m.get(field) if m else None


def make_figure_T(rows, out_path):
    """Per-phase breakdown for Set 1 (1-to-N)."""
    import matplotlib.pyplot as plt
    set1 = sorted([r for r in rows if r["mode"] == "one-to-n"],
                  key=lambda r: r["N"])
    if not set1:
        print("  [T] no one-to-n rows; skip", file=sys.stderr)
        return
    N = [r["N"] for r in set1]
    h2d = [get_metric(r, "h2d_us") or 0 for r in set1]
    kernel = [get_metric(r, "kernel_us") or 0 for r in set1]
    d2h = [get_metric(r, "d2h_us") or 0 for r in set1]
    total = [get_metric(r, "model_mix_latency") or 0 for r in set1]

    fig, ax = plt.subplots(figsize=(7.5, 4.5), dpi=120)
    ax.bar(range(len(N)), h2d, label="H2D",
           color="#5db1ff")
    ax.bar(range(len(N)), kernel, bottom=h2d, label="kernel",
           color="#ffb454")
    bottom = [a + b for a, b in zip(h2d, kernel)]
    ax.bar(range(len(N)), d2h, bottom=bottom, label="D2H",
           color="#6fd08c")
    ax.plot(range(len(N)), total, "o-", color="#e7ecf5",
            markersize=7, label="model_mix p99",
            markeredgecolor="#1a2233", markeredgewidth=1.2)
    ax.set_xticks(range(len(N)))
    ax.set_xticklabels([f"N={n}" for n in N])
    ax.set_xlabel("Fan-in (1 gNB → N UEs)")
    ax.set_ylabel("p99 latency (µs)")
    ax.set_title("Diagram T (regenerated) — fan-in scaling, per-phase breakdown")
    ax.legend(loc="upper left", frameon=False)
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, format="svg")
    plt.close(fig)
    print(f"  [T] wrote {out_path}")


def make_figure_U(rows, out_path):
    """Every (M, N) config's p99 model_mix; show the collapse onto edge count."""
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(7.5, 4.5), dpi=120)
    # x = edges = 2 * M * N (one-to-n is M=1; m-to-n is M, N).
    one_to_n = [r for r in rows if r["mode"] == "one-to-n"]
    m_to_n = [r for r in rows if r["mode"] == "m-to-n"]

    def plot(group, marker, label, color):
        if not group:
            return
        xs = [(r.get("M", 1) * r["N"]) for r in group]  # incoming edges per RX
        ys = [get_metric(r, "model_mix_latency") or 0 for r in group]
        ax.scatter(xs, ys, marker=marker, s=70, label=label,
                   color=color, edgecolor="#1a2233", linewidth=1.0)

    plot(one_to_n, "o", "1-to-N (Set 1)", "#5db1ff")
    plot(m_to_n, "s", "M-to-N (Set 2)", "#ffb454")
    ax.set_xscale("log")
    ax.set_xlabel("Incoming edges per RX node (log scale)")
    ax.set_ylabel("p99 model_mix_latency (µs)")
    ax.set_title("Diagram U (regenerated) — every (M, N) collapses onto edge count")
    ax.legend(loc="upper left", frameon=False)
    ax.grid(True, alpha=0.3, which="both")
    fig.tight_layout()
    fig.savefig(out_path, format="svg")
    plt.close(fig)
    print(f"  [U] wrote {out_path}")


def make_figure_W(rows, out_path):
    """H2D µs by edge count — the PCIe wall."""
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(7.5, 4.5), dpi=120)
    set1 = sorted([r for r in rows if r["mode"] == "one-to-n"],
                  key=lambda r: r["N"])
    edges = [r["N"] for r in set1]
    h2d = [get_metric(r, "h2d_us") or 0 for r in set1]
    # PCIe gen 5 x4 ceiling: 126 Gbps = 15.0 GB/s. One slot = 23040 * 8 bytes
    # = 184320 B per edge for cf32 IQ -> at 15.0 GB/s, ideal H2D = 12.3 µs per
    # edge. (Diagram W in the doc plots achieved bandwidth vs this ceiling.)
    samples_per_slot = 23040
    bytes_per_sample = 8  # cf32
    pcie_bytes_per_s = 15.0e9
    ideal_h2d = [n * samples_per_slot * bytes_per_sample / pcie_bytes_per_s * 1e6
                 for n in edges]
    ax.plot(edges, h2d, "o-", color="#5db1ff", markersize=7,
            markeredgecolor="#1a2233", markeredgewidth=1.0,
            label="measured H2D p99")
    ax.plot(edges, ideal_h2d, "--", color="#ffb454", linewidth=2,
            label="PCIe 5.0 x4 ceiling (15 GB/s)")
    ax.set_xscale("log", base=2)
    ax.set_xticks(edges)
    ax.set_xticklabels([str(n) for n in edges])
    ax.set_xlabel("Incoming edges per RX (1-to-N)")
    ax.set_ylabel("H2D p99 (µs)")
    ax.set_title("Diagram W (regenerated) — H2D µs vs PCIe 5.0 x4 ceiling")
    ax.legend(loc="upper left", frameon=False)
    ax.grid(True, alpha=0.3, which="both")
    fig.tight_layout()
    fig.savefig(out_path, format="svg")
    plt.close(fig)
    print(f"  [W] wrote {out_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sweep_json", type=Path)
    ap.add_argument("--out", type=Path, default=Path("docs/reports/performance/2026-05-sweeps"))
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    rows = load_sweep(args.sweep_json)
    print(f"loaded {len(rows)} rows from {args.sweep_json}")
    make_figure_T(rows, args.out / "perf-T-fanin-breakdown.svg")
    make_figure_U(rows, args.out / "perf-U-mn-collapse.svg")
    make_figure_W(rows, args.out / "perf-W-pcie-h2d.svg")
    print(f"done -> {args.out}/")


if __name__ == "__main__":
    main()
