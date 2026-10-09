#!/usr/bin/env python3
"""Plot successful corrected R7 UDP probe conditions from analyzer list JSON.

Missing or failed conditions are listed below the plots and never plotted as
zero. Missing individual UE metrics remain gaps. Only the analyzer's steady
window is used. Late-burst counts are request-slot runs, not PHY disconnects.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import textwrap

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator


def label(tag):
    short = tag.removeprefix('corrected-')
    if short == 'base':
        return 'Base\nAWGN 40 dB'
    if short == 'ran-retx1':
        return 'HARQ max 1\nAWGN 30 dB'
    if short == 'ran-sr40':
        return 'SR period\n40 ms'
    if re.fullmatch(r'awgn\d+', short):
        return f'AWGN\n{short[4:]} dB'
    return short.replace('-', '\n')


def sort_key(run):
    tag = run['tag'].removeprefix('corrected-')
    if tag == 'base':
        return (0, 0, tag)
    if re.fullmatch(r'awgn\d+', tag):
        return (1, -int(tag[4:]), tag)
    return (2, 0, tag)


def steady(block):
    return (block.get('probe') or {}).get('steady') or {}


def number(value):
    return float(value) if value is not None else math.nan


def render(data, output, steady_after):
    included, excluded = [], []
    for run in sorted(data, key=sort_key):
        if run.get('gate', {}).get('result') != 'passed':
            excluded.append(f"{run['tag']}: gate not passed / no valid probe condition")
        elif not any(steady(block).get('requests', 0) > 0 for block in run.get('ues', {}).values()):
            excluded.append(f"{run['tag']}: no resolved steady-window requests")
        else:
            included.append(run)
    if not included:
        raise ValueError('No successful probe conditions with steady-window requests')
    ues = sorted({ue for run in included for ue in run['ues']})
    colors = ['#227D9B', '#BD6639', '#7A66A5', '#578A51']
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 11,
                         'axes.spines.top': False, 'axes.spines.right': False,
                         'axes.titleweight': 'bold', 'axes.axisbelow': True,
                         'text.color': '#263746', 'axes.labelcolor': '#263746'})
    fig, grid = plt.subplots(2, 2, figsize=(14, 10))
    axes = grid.ravel()
    x = list(range(len(included)))
    for idx, ue in enumerate(ues):
        color = colors[idx % len(colors)]
        shift = (idx - (len(ues)-1)/2) * .13
        xpos = [i + shift for i in x]
        blocks = [r['ues'].get(ue, {}) for r in included]
        for panel, quantile in [(0, 'p50'), (1, 'p99')]:
            values = [number(steady(b).get('rtt_us', {}).get(quantile)) / 1000 for b in blocks]
            axes[panel].scatter(xpos, values, s=60, color=color, label=ue, zorder=3)
        snr = [number(b.get('srsue', {}).get('dl_snr')) for b in blocks]
        axes[2].scatter(xpos, snr, s=60, color=color, label=ue, zorder=3)
        bursts = [number(steady(b).get('outages', {}).get('count')) for b in blocks]
        axes[3].scatter(xpos, bursts, s=60, color=color, label=ue, zorder=3)
    titles = ['A  UDP RTT p50', 'B  UDP RTT p99', 'C  Measured DL SNR',
              'D  Late / lost request bursts']
    ylabels = ['RTT (ms)', 'RTT (ms)', 'Mean UE-reported DL SNR (dB)', 'Burst count']
    for ax, title, ylabel in zip(axes, titles, ylabels):
        ax.set_title(title, loc='left', pad=14)
        ax.set_ylabel(ylabel)
        ax.set_xticks(x, [label(r['tag']) for r in included])
        ax.set_xlim(-.5, len(included)-.5)
        ax.grid(axis='y', alpha=.2)
    handles, legend_labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, legend_labels, loc='upper right', bbox_to_anchor=(.98, .90),
               frameon=False, ncol=len(ues))
    for ax in (axes[0], axes[1], axes[3]):
        _, upper = ax.get_ylim()
        ax.set_ylim(bottom=-.1 if ax is axes[3] else 0, top=upper * 1.12)
    axes[3].yaxis.set_major_locator(MaxNLocator(integer=True))
    fig.suptitle('R7 corrected radio probes | RTT, measured SNR, and late bursts',
                 x=.07, ha='left', fontsize=18, weight='bold', y=.98)
    fig.text(.07, .91,
             f'100 Hz UDP probes; no arena. Spark GB10 · 23.04 MSps · two cells · CUDA Sionna + AWGN\n'
             f'160 s run budget; steady window starts {steady_after:g} s after each probe starts. Single run per condition.',
             fontsize=11)
    counts = [steady(b).get('requests', 0) for r in included for b in r['ues'].values()
              if steady(b).get('requests', 0)]
    thresholds = sorted({steady(b).get('outages', {}).get('threshold_ms', 200)
                         for r in included for b in r['ues'].values() if steady(b)})
    threshold_text = '/'.join(f'{v:g}' for v in thresholds)
    notes = [f'Steady-window resolved requests per UE: {min(counts):,}–{max(counts):,}. '
             'RTT quantiles use answered requests; unresolved requests at shutdown are excluded.',
             f'Late burst = 3+ consecutive request send slots lost or RTT > {threshold_text} ms. '
             'This is not a PHY disconnect count.',
             'AWGN labels are configured noise settings; panel C is the measured DL SNR. '
             'Conditions are separate runs; points are descriptive.']
    if excluded:
        notes.extend(textwrap.wrap('Excluded (not zero): ' + '; '.join(excluded), width=155))
    fig.text(.07, .025, '\n'.join(notes), fontsize=9.5, va='bottom')
    fig.subplots_adjust(left=.07, right=.98, top=.83, bottom=.23 + max(0, len(notes)-4)*.012,
                        wspace=.25, hspace=.54)
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=180, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    print(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=Path('results/robot-fight/r7-corrected/table.json'))
    parser.add_argument('--output', type=Path, default=Path('docs/robot-fight-r7-probes.png'))
    parser.add_argument('--steady-after', type=float, default=20, help='analyzer steady-window setting, for annotation')
    args = parser.parse_args()
    render(json.loads(args.input.read_text()), args.output, args.steady_after)


if __name__ == '__main__':
    main()
