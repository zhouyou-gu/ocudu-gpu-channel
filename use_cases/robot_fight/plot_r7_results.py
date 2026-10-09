#!/usr/bin/env python3
"""Render R7 aggregate JSON as descriptive, publication-ready PNG figures.

Input is the output of analyze_r7_battle.py. Comparisons use only its matched-seed,
side-swapped pairs; same-policy controls remain separate. No raw-packet
percentiles or causal estimates are reconstructed from aggregate summaries.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import textwrap

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator

COLORS = {'balance': '#D77835', 'balance_comp': '#237F9A', 'draw': '#BBC3CA'}
LABELS = {'balance': 'balance', 'balance_comp': 'balance_comp'}


def setup():
    plt.rcParams.update({
        'font.family': 'DejaVu Sans', 'font.size': 11, 'axes.titlesize': 13,
        'axes.titleweight': 'bold', 'axes.spines.top': False,
        'axes.spines.right': False, 'axes.labelcolor': '#263746',
        'text.color': '#263746', 'figure.facecolor': 'white',
        'savefig.facecolor': 'white', 'axes.axisbelow': True,
    })


def save(fig, path):
    fig.savefig(path, dpi=180, bbox_inches='tight')
    plt.close(fig)
    print(path)


def realtime_note(data, tags):
    values = {(data['runs'][tag].get('attach_summary') or {}).get('run_parameters', {})
              .get('strict_realtime') for tag in tags}
    if values == {0}:
        return 'Strict realtime disabled in these runs.'
    if values == {1}:
        return 'Strict realtime enabled in these runs.'
    return 'Strict realtime setting mixed or unavailable; consult run metadata.'


def comparison_plot(data, path):
    pairs = [(name, pair) for name, pair in data['pairs'].items()
             if pair['comparison']['fights'] > 0]
    if not pairs:
        raise ValueError('No nonempty side-swapped comparisons in input')
    scope = (data.get('experiment') or {}).get('scope', '')
    local = 'local' in scope.lower() or all(
        not run.get('attach_summary') for run in data['runs'].values())
    fig, axes = plt.subplots(1, 3, figsize=(17, 6.5 + max(0, len(pairs)-2)*.65),
                             gridspec_kw={'width_ratios': [1.25, 1, 1.1]})
    names = ['\n'.join(textwrap.wrap(name.replace('_', ' ').replace('-', ' ').capitalize(), 15))
             for name, _ in pairs]
    ys = list(range(len(pairs)))
    ax = axes[0]
    for y, (name, pair) in enumerate(pairs):
        c = pair['comparison']
        left = 0
        for key, count in [('balance_comp', c['wins']['balance_comp']),
                           ('draw', c['draws']), ('balance', c['wins']['balance'])]:
            ax.barh(y, count, left=left, height=.5, color=COLORS[key],
                    label={'balance_comp': 'Comp win', 'draw': 'Draw', 'balance': 'Comp loss'}[key]
                    if y == 0 else None)
            if count:
                ax.text(left + count / 2, y, str(count), ha='center', va='center',
                        color='#263746' if key == 'draw' else 'white', weight='bold')
            left += count
        ax.text(0, y + .38,
                f"{len(pair['seeds'])} matched seeds / {c['fights']} fights", fontsize=10)
    ax.set_yticks(ys, names)
    ax.set_ylim(len(pairs) - .35, -.6)
    ax.set_xlim(0, max(p['comparison']['fights'] for _, p in pairs) * 1.12)
    ax.set_xlabel('Fight count (from balance_comp perspective)')
    ax.set_title('A  Match outcomes', loc='left', pad=18)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.legend(loc='upper left', bbox_to_anchor=(0, -.18), frameon=False, ncol=3,
              fontsize=9, handlelength=1, columnspacing=1)

    ax = axes[1]
    for offset, policy in [(-.18, 'balance'), (.18, 'balance_comp')]:
        counts = [p['comparison']['falls'][policy] for _, p in pairs]
        bars = ax.bar([y + offset for y in ys], counts, width=.32,
                      color=COLORS[policy], label=LABELS[policy])
        ax.bar_label(bars, padding=4)
    ax.set_xticks(ys, names)
    ax.set_ylabel('Recorded falls (count)')
    ax.set_title('B  Falls by policy', loc='left', pad=18)
    ax.set_ylim(0, max(1, max(p['comparison']['falls'][q] for _, p in pairs
                            for q in LABELS)) * 1.28)
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.grid(axis='y', alpha=.18)
    ax.legend(loc='upper left', bbox_to_anchor=(0, -.18), frameon=False, fontsize=10)

    ax = axes[2]
    for offset, policy in [(-.14, 'balance'), (.14, 'balance_comp')]:
        for quantile, marker in [('p50', 'o'), ('p99', '^')]:
            vals = [p['comparison']['metrics_median_of_fights'][policy]
                    .get(f'rtt_us.{quantile}', {}).get('median') for _, p in pairs]
            ax.scatter([y + offset for y, v in zip(ys, vals) if v is not None],
                       [v / 1000 for v in vals if v is not None],
                       color=COLORS[policy], marker=marker, s=85,
                       label=f'{policy}: per-fight {quantile}')
    ax.set_xticks(ys, names)
    ax.set_xlim(-.5, len(pairs) - .5)
    ax.set_ylim(bottom=0)
    ax.set_ylabel('Median across fights (ms)')
    ax.set_title('C  RTT summaries', loc='left', pad=18)
    ax.grid(axis='y', alpha=.18)
    ax.legend(loc='upper left', bbox_to_anchor=(0, -.18), frameon=False, fontsize=9)
    title = ('R7 local UDP proxy fights | matched seeds, policies swapped between robots' if local else
             'R7 live robot fights | matched seeds, policies swapped between UEs')
    fig.suptitle(title,
                 x=.065, ha='left', fontsize=18, weight='bold', y=.98)
    low, high = data['rtf_bounds']
    context = 'Local MuJoCo + UDP delay/loss proxies; no RAN or Sionna.' if local else 'Descriptive comparison; two cells.'
    fig.text(.065, .885, context + '\nRTT markers are medians of per-fight quantiles, not pooled-packet percentiles.', fontsize=11)
    tags = {tag for _, pair in pairs for tag in pair['tags']}
    details = ('Local robustness checks; UDP proxy impairments are not radio qualification.\n'
               'No radio, Spark, or Sionna workloads are represented.' if local else
               'Conditions differ in contention and broker scheduling; no isolated causal attribution.\n'
               + realtime_note(data, tags) + ' Does not certify strict radio deadlines or uninterrupted PHY synchronization.')
    fig.text(.065, .015,
             f'Accepted fight RTF: {low:g}–{high:g}. Unpaired and rejected fights excluded. '
             + details, fontsize=10)
    fig.subplots_adjust(left=.065, right=.98, top=.76, bottom=.29, wspace=.36)
    save(fig, path)


def controls_plot(data, path):
    controls = [(tag, policy, c) for tag, run in data['runs'].items()
                for policy, c in run['controls'].items() if c['fights'] > 0]
    if not controls:
        return
    fig, axes = plt.subplots(1, 2, figsize=(12, 6))
    labels = [f'{tag}\n{policy} vs {policy}' for tag, policy, _ in controls]
    for y, (tag, policy, c) in enumerate(controls):
        left = 0
        for key, count, color in [('ue0', c['side_wins']['ue0'], '#4477AA'),
                                   ('draw', c['draws'], COLORS['draw']),
                                   ('ue1', c['side_wins']['ue1'], '#AA6688')]:
            axes[0].barh(y, count, left=left, height=.5, color=color,
                         label={'ue0': 'UE0 wins', 'ue1': 'UE1 wins', 'draw': 'Draw'}[key]
                         if y == 0 else None)
            if count:
                axes[0].text(left + count/2, y, str(count), ha='center', va='center',
                             color='white', weight='bold')
            left += count
        count = c['falls'][policy]
        axes[1].barh(y, count, height=.5, color=COLORS[policy])
        axes[1].text(count + .2, y, f'{count} / {2*c["fights"]} robot participations', va='center', fontsize=10)
    for ax in axes:
        ax.set_yticks(range(len(controls)), labels if ax is axes[0] else [])
        ax.invert_yaxis()
        ax.xaxis.set_major_locator(MaxNLocator(integer=True))
        ax.grid(axis='x', alpha=.18)
    axes[0].set_xlabel('Fight count')
    axes[0].set_title('A  Side outcomes', loc='left', pad=15)
    axes[0].legend(loc='upper left', bbox_to_anchor=(0, -.16), frameon=False, ncol=3, fontsize=9)
    axes[1].set_title('B  Falls across both robots', loc='left', pad=15)
    axes[1].set_xlabel('Recorded falls (count)')
    axes[1].set_xlim(0, max(1, max(c['falls'][p] for _, p, c in controls)) * 1.95)
    fig.suptitle('R7 same-policy controls | separate from policy comparisons',
                 x=.05, ha='left', fontsize=17, weight='bold', y=.98)
    fig.text(.05, .88, 'Counts retain each control run independently; unequal sample sizes are shown through participation totals.', fontsize=10)
    fig.text(.05, .02, 'Descriptive controls. Same-policy side wins do not measure one policy beating the other.\n'
             + realtime_note(data, [tag for tag, _, _ in controls])
             + ' Does not certify strict radio deadlines or uninterrupted PHY synchronization.', fontsize=10)
    fig.subplots_adjust(left=.23, right=.98, top=.76, bottom=.26, wspace=.12)
    save(fig, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=Path('docs/robot-fight-r7-results.json'))
    parser.add_argument('--out-dir', type=Path, default=Path('docs'))
    args = parser.parse_args()
    data = json.loads(args.input.read_text())
    args.out_dir.mkdir(parents=True, exist_ok=True)
    setup()
    comparison_plot(data, args.out_dir / 'robot-fight-r7-comparison.png')
    controls_plot(data, args.out_dir / 'robot-fight-r7-controls.png')


if __name__ == '__main__':
    main()
