#!/usr/bin/env python3
"""Regenerate the §19 fading/multipath/Doppler figures.

Three matplotlib SVGs in the doc's dark-theme palette, written to
docs/figures/:

  diag-P-tdl-pdp.svg            5-panel power-delay profile (TDL-A..E)
  diag-Q-jakes-spectrum.svg     Jakes' U-shaped S(f) + Bessel J_0 autocorrelation
  diag-R-wssus-scattering.svg   WSSUS scattering function on (delay, Doppler)
                                for TDL-A at f_d_max = 100 Hz

All numbers come from the same Sionna-derived TR 38.901 §7.7.2 profiles that
ship as examples/topology.tdl-{a..e}.cuda.yaml (delay spread 100 ns, sample
rate 23.04 MS/s -> normalised_delay * 2.304 samples).

Usage:
  python3 scripts/figures/regen_fading_figures.py [--out docs/figures]
"""
import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from scipy.special import j0 as bessel_j0

# Doc palette (matches the inline SVGs in §6, §15, §21).
BG = "#0f1626"
PANEL = "#1a2233"
GRID = "#2c3850"
INK = "#e7ecf5"
DIM = "#9aa7bd"
BLUE = "#5db1ff"
ORANGE = "#ffb454"
GREEN = "#6fd08c"
PURPLE = "#c08bff"
RED = "#ff7676"


def set_dark(ax):
    ax.set_facecolor(PANEL)
    for s in ax.spines.values():
        s.set_color(GRID)
    ax.tick_params(colors=DIM)
    ax.xaxis.label.set_color(DIM)
    ax.yaxis.label.set_color(DIM)
    ax.title.set_color(INK)
    ax.grid(True, color=GRID, linewidth=0.6, alpha=0.7)


# --- TDL profile data (Sionna TR 38.901 §7.7.2 JSON, verbatim) -----------
# Format: (normalised_delays, powers_dB, los_tap_index_or_None, K_dB_if_LOS)
# Delays are in units of delay_spread; powers in dB. For LOS profiles
# (D, E), Sionna publishes two zero-delay entries (LOS + co-delay Rayleigh)
# whose dB ratio is the K-factor; in our YAMLs that pair is collapsed into
# one is_los tap. Here we plot the published unscaled lines so the
# spec-text shape is recognisable.
TDL_A = {
    "name": "TDL-A (NLOS, 23 taps)",
    "delays": [0.0000, 0.3819, 0.4025, 0.5868, 0.4610, 0.5375, 0.6708,
               0.5750, 0.7618, 1.5375, 1.8978, 2.2242, 2.1718, 2.4942,
               2.5119, 3.0582, 4.0810, 4.4579, 4.5695, 4.7966, 5.0066,
               5.3043, 9.6586],
    "powers": [-13.4, 0.0, -2.2, -4.0, -6.0, -8.2, -9.9, -10.5, -7.5,
               -15.9, -6.6, -16.7, -12.4, -15.2, -10.8, -11.3, -12.7,
               -16.2, -18.3, -18.9, -16.6, -19.9, -29.7],
    "los": None,
}
TDL_B = {
    "name": "TDL-B (NLOS, 23 taps)",
    "delays": [0.0000, 0.1072, 0.2155, 0.2095, 0.2870, 0.2986, 0.3752,
               0.5055, 0.3681, 0.3697, 0.5700, 0.5283, 1.1021, 1.2756,
               1.5474, 1.7842, 2.0169, 2.8294, 3.0219, 3.6187, 4.1067,
               4.2790, 4.7834],
    "powers": [0.0, -2.2, -4.0, -3.2, -9.8, -1.2, -3.4, -5.2, -7.6, -3.0,
               -8.9, -9.0, -4.8, -5.7, -7.5, -1.9, -7.6, -12.2, -9.8,
               -11.4, -14.9, -9.2, -11.3],
    "los": None,
}
TDL_C = {
    "name": "TDL-C (NLOS, 24 taps)",
    "delays": [0.0000, 0.2099, 0.2219, 0.2329, 0.2176, 0.6366, 0.6448,
               0.6560, 0.6584, 0.7935, 0.8213, 0.9336, 1.2285, 1.3083,
               2.1704, 2.7105, 4.2589, 4.6003, 5.4902, 5.6077, 6.3065,
               6.6374, 7.0427, 8.6523],
    "powers": [-4.4, -1.2, -3.5, -5.2, -2.5, 0.0, -2.2, -3.9, -7.4, -7.1,
               -10.7, -11.1, -5.1, -6.8, -8.7, -13.2, -13.9, -13.9, -15.8,
               -17.1, -16.0, -15.7, -21.6, -22.8],
    "los": None,
}
# TDL-D: K_1 = -0.2 - (-13.5) = 13.3 dB at the LOS tap (delay 0).
TDL_D = {
    "name": "TDL-D (LOS, K=13.3 dB, 13 taps)",
    "delays": [0.0, 0.035, 0.612, 1.363, 1.405, 1.804, 2.596, 1.775,
               4.042, 7.937, 9.424, 9.708, 12.525],
    "powers": [0.0, -18.8, -21.0, -22.8, -17.9, -20.1, -21.9, -22.9,
               -27.8, -23.6, -24.8, -30.0, -27.7],
    "los": 0,
    "K_dB": 13.3,
}
# TDL-E: K_1 = -0.03 - (-22.03) = 22 dB at the LOS tap.
TDL_E = {
    "name": "TDL-E (LOS, K=22 dB, 13 taps)",
    "delays": [0.0, 0.5133, 0.5440, 0.5630, 0.7112, 1.9092, 1.9293,
               1.9589, 2.6426, 3.7136, 5.4524, 12.0034, 20.6519],
    "powers": [0.0, -15.8, -16.86, -19.8, -22.4, -18.6, -20.8, -22.6,
               -22.3, -25.6, -20.2, -29.8, -29.2],
    "los": 0,
    "K_dB": 22.0,
}

PROFILES = [TDL_A, TDL_B, TDL_C, TDL_D, TDL_E]


def make_diag_P(out_path):
    """5-panel power-delay profile."""
    fig, axes = plt.subplots(5, 1, figsize=(7.6, 8.4), dpi=120, sharex=False)
    fig.patch.set_facecolor(BG)
    for ax, prof in zip(axes, PROFILES):
        set_dark(ax)
        delays = np.array(prof["delays"])
        powers = np.array(prof["powers"])
        los_idx = prof["los"]
        colors = [BLUE] * len(delays)
        if los_idx is not None:
            colors[los_idx] = ORANGE
        markerline, stemlines, baseline = ax.stem(
            delays, powers, basefmt=" ", linefmt=BLUE, markerfmt="o",
        )
        plt.setp(stemlines, color=BLUE, linewidth=1.3)
        plt.setp(markerline, color=BLUE, markersize=4,
                 markeredgecolor=INK, markeredgewidth=0.4)
        # Highlight LOS tap
        if los_idx is not None:
            ax.plot([delays[los_idx]], [powers[los_idx]], "o",
                    color=ORANGE, markersize=9,
                    markeredgecolor=INK, markeredgewidth=0.8,
                    zorder=5)
            ax.annotate(f" LOS (K={prof['K_dB']:.1f} dB)",
                        (delays[los_idx], powers[los_idx]),
                        color=ORANGE, fontsize=9, va="center")
        ax.set_xlim(-0.5, max(13, delays.max() * 1.05))
        ax.set_ylim(min(-32, powers.min() - 2), 3)
        ax.set_title(prof["name"], fontsize=10, loc="left")
        ax.set_ylabel("power (dB)")
    axes[-1].set_xlabel("normalised delay (× delay spread)  —  ×2.304 samples at 100 ns DS, 23.04 MS/s")
    fig.suptitle("Diagram P — TR 38.901 §7.7.2 power-delay profiles",
                 color=INK, fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(out_path, format="svg",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  [P] wrote {out_path}")


def make_diag_Q(out_path):
    """Jakes spectrum (left) + Bessel J_0 autocorrelation (right)."""
    f_d = 100.0  # Hz, the project default
    # Jakes S(f) = 1 / (pi * f_d * sqrt(1 - (f/f_d)^2)) for |f| < f_d
    f = np.linspace(-1.05 * f_d, 1.05 * f_d, 4001)
    # Avoid divide-by-zero at the band edge.
    inside = (np.abs(f) < f_d * 0.999)
    S = np.zeros_like(f)
    S[inside] = 1.0 / (np.pi * f_d * np.sqrt(1.0 - (f[inside] / f_d) ** 2))
    # Autocorrelation R(tau) = J_0(2*pi*f_d*tau)
    tau = np.linspace(0, 0.05, 2001)
    R = bessel_j0(2 * np.pi * f_d * tau)

    fig, (axL, axR) = plt.subplots(1, 2, figsize=(8.4, 3.8), dpi=120)
    fig.patch.set_facecolor(BG)

    set_dark(axL)
    axL.plot(f, S, color=BLUE, linewidth=1.8)
    axL.fill_between(f, 0, S, color=BLUE, alpha=0.18)
    axL.axvline(-f_d, color=ORANGE, linewidth=0.9, linestyle="--", alpha=0.8)
    axL.axvline(f_d, color=ORANGE, linewidth=0.9, linestyle="--", alpha=0.8)
    axL.text(f_d, S.max() * 0.5, f"  ±f_d = {f_d:.0f} Hz", color=ORANGE,
             fontsize=9, va="center")
    axL.set_xlim(-1.1 * f_d, 1.1 * f_d)
    axL.set_ylim(0, S[inside].max() * 1.1)
    axL.set_xlabel("Doppler frequency f (Hz)")
    axL.set_ylabel("Doppler PSD S(f)  [1/Hz]")
    axL.set_title("Jakes' classical spectrum  —  S(f) U-shape", fontsize=10, loc="left")

    set_dark(axR)
    axR.plot(tau * 1000, R, color=GREEN, linewidth=1.8)
    axR.axhline(0, color=GRID, linewidth=0.8)
    # Mark the first zero of J_0(x) at x ≈ 2.4048 -> tau* = 2.4048 / (2*pi*f_d)
    tau_zero = 2.4048 / (2 * np.pi * f_d)
    axR.axvline(tau_zero * 1000, color=ORANGE, linewidth=0.9, linestyle="--", alpha=0.8)
    axR.text(tau_zero * 1000, 0.6,
             f"  τ₀ ≈ {tau_zero*1000:.1f} ms  (J₀ first zero)",
             color=ORANGE, fontsize=9, va="center")
    # Mark the lags used in the Bessel test (1, 3, 5 ms)
    for lag_ms, expected in [(1, 0.904), (3, 0.305), (5, -0.304)]:
        axR.plot([lag_ms], [expected], "o", color=BLUE, markersize=6,
                 markeredgecolor=INK, markeredgewidth=0.8, zorder=5)
        axR.annotate(f"  {lag_ms} ms  →  J₀ = {expected:+.2f}",
                     (lag_ms, expected), color=BLUE, fontsize=8, va="center")
    axR.set_xlim(0, 50)
    axR.set_ylim(-0.5, 1.05)
    axR.set_xlabel("lag τ (ms)")
    axR.set_ylabel("R(τ) = J₀(2π·f_d·τ)")
    axR.set_title("Temporal autocorrelation  —  the Bessel curve", fontsize=10, loc="left")

    fig.suptitle("Diagram Q — Jakes' Doppler spectrum and its Bessel J₀ autocorrelation (f_d = 100 Hz)",
                 color=INK, fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(out_path, format="svg", facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  [Q] wrote {out_path}")


def make_diag_R(out_path):
    """WSSUS scattering function for TDL-A at f_d_max = 100 Hz on the
    (delay, Doppler) plane. Each tap contributes a Jakes U-shaped slice
    along the Doppler axis at its delay."""
    f_d = 100.0
    delays_ns = np.array(TDL_A["delays"]) * 100.0  # 100 ns delay spread
    powers_dB = np.array(TDL_A["powers"])
    powers_lin = 10 ** (powers_dB / 10.0)

    # Build a 2D image: y = Doppler bins, x = delay bins (samples or ns).
    n_delay = 400
    n_dop = 200
    delay_grid = np.linspace(0, delays_ns.max() * 1.05, n_delay)
    dop_grid = np.linspace(-1.05 * f_d, 1.05 * f_d, n_dop)
    DD, FF = np.meshgrid(delay_grid, dop_grid)
    # Per-tap Jakes' shape along the Doppler axis (normalised so the
    # integral over Doppler equals 1 per tap). At |f| >= f_d the density is
    # zero. We deposit each tap's power as a narrow gaussian in delay
    # (width = 1 % of max delay) multiplied by Jakes' Doppler shape.
    sigma_delay = 0.01 * delays_ns.max() + 5.0  # ns; visual smoothing
    scat = np.zeros_like(DD)
    for d_ns, p in zip(delays_ns, powers_lin):
        # Delay smoothing kernel (gaussian)
        delay_kern = np.exp(-0.5 * ((delay_grid - d_ns) / sigma_delay) ** 2)
        delay_kern /= delay_kern.max()
        # Doppler Jakes' shape (zero outside |f| >= f_d)
        inside = np.abs(dop_grid) < f_d * 0.999
        dop_shape = np.zeros_like(dop_grid)
        dop_shape[inside] = 1.0 / (np.pi * f_d * np.sqrt(1.0 - (dop_grid[inside] / f_d) ** 2))
        dop_shape /= dop_shape[inside].max() if inside.any() else 1.0
        scat += p * np.outer(dop_shape, delay_kern)
    # Log-scale for visualisation; clip at -40 dB below the peak.
    scat_db = 10 * np.log10(scat + 1e-12)
    peak_db = scat_db.max()
    scat_db -= peak_db
    scat_db = np.clip(scat_db, -35, 0)

    fig, ax = plt.subplots(figsize=(8.4, 4.6), dpi=120)
    fig.patch.set_facecolor(BG)
    set_dark(ax)
    im = ax.imshow(
        scat_db, origin="lower", aspect="auto",
        extent=(delay_grid[0], delay_grid[-1], dop_grid[0], dop_grid[-1]),
        cmap="magma", vmin=-35, vmax=0,
    )
    # Overlay tap positions as small markers along Doppler = 0 baseline
    ax.scatter(delays_ns, np.zeros_like(delays_ns),
               c=powers_dB, cmap="viridis", s=20,
               edgecolor=INK, linewidth=0.5, zorder=4)
    ax.axhline(f_d, color=GREEN, linestyle="--", linewidth=0.9, alpha=0.7)
    ax.axhline(-f_d, color=GREEN, linestyle="--", linewidth=0.9, alpha=0.7)
    ax.text(delay_grid[-1] * 0.97, f_d - 8, "+f_d", color=GREEN,
            ha="right", fontsize=9)
    ax.text(delay_grid[-1] * 0.97, -f_d + 4, "−f_d", color=GREEN,
            ha="right", fontsize=9)
    ax.set_xlabel("delay τ (ns)  —  TDL-A taps, 100 ns delay spread, 23.04 MS/s")
    ax.set_ylabel("Doppler frequency f (Hz)")
    ax.set_title("Diagram R — WSSUS scattering function for TDL-A at f_d_max = 100 Hz",
                 fontsize=11, loc="left")
    cbar = fig.colorbar(im, ax=ax, label="relative power (dB from peak)")
    cbar.ax.yaxis.label.set_color(DIM)
    cbar.ax.tick_params(colors=DIM)
    fig.tight_layout()
    fig.savefig(out_path, format="svg", facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  [R] wrote {out_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("docs/figures"))
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    make_diag_P(args.out / "diag-P-tdl-pdp.svg")
    make_diag_Q(args.out / "diag-Q-jakes-spectrum.svg")
    make_diag_R(args.out / "diag-R-wssus-scattering.svg")
    print(f"done -> {args.out}/")


if __name__ == "__main__":
    main()
