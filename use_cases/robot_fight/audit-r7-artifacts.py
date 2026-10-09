#!/usr/bin/env python3
"""Verify requested R7 conditions and summarize bounded UE lifecycle evidence.

Conditions: --report attach-summary.json --config-dir DIR --expect-awgn 30
            --expect-cell pusch.min_k2=8 [--expect-cell pucch.min_k1=7]
Lifecycle:  --report attach-summary.json --log-dir DIR
Requires PyYAML for rendered YAML inspection. Missing evidence is unverified, never passed. Internal logs are scanned only
with --scan-internal (potentially gigabytes); console-only results cannot prove
continuous PHY synchronization. Output is JSON; exit 1 means a condition failed
or is unverified. Lifecycle observations do not certify synchronization.
"""
from __future__ import annotations
import argparse
import json
import math
import pathlib
import re


def check(label, expected, actual, present):
    equal = actual == expected and (isinstance(actual, bool) == isinstance(expected, bool))
    status = 'unverified' if not present else ('passed' if equal else 'failed')
    return dict(field=label, expected=expected, actual=actual, status=status)


def noise_conditions(config_dir, expected_awgn):
    """Cross-check rendered noise, attachment to RX ports, and declared powers."""
    import yaml
    shape_path = config_dir / 'robot-fight-shape.json' if config_dir else None
    shape = json.loads(shape_path.read_text()) if shape_path and shape_path.exists() else {}
    rx_noise = shape.get('rx_noise', {})
    out = [check('shape.rx_noise.awgn_snr_db', expected_awgn, rx_noise.get('awgn_snr_db'),
                 rx_noise.get('awgn_snr_db') is not None)]
    for cell, index in [('a', 0), ('b', 1)]:
        path = config_dir / f'topology-{cell}.yaml' if config_dir else None
        topology = yaml.safe_load(path.read_text()) if path and path.exists() else {}
        topology = topology if isinstance(topology, dict) else {}
        for role, direction in [('gnb', 'ul'), ('ue', 'dl')]:
            node = f'{role}{index}'
            model_name = f'rx_noise_{node}'
            devices = [d for d in topology.get('devices', []) if d.get('id') == f'{node}_p0']
            out.append(check(f'{cell}.{node}.device_count', 1, len(devices), path is not None and path.exists()))
            device = devices[0] if len(devices) == 1 else {}
            out.append(check(f'{cell}.{node}.rx_model', model_name, device.get('rx_model'), 'rx_model' in device))
            out.append(check(f'{cell}.{node}.sample_rate_hz', 23040000, device.get('sample_rate_hz'), 'sample_rate_hz' in device))
            chain = topology.get('models', {}).get(model_name, {}).get('chain', [])
            out.append(check(f'{cell}.{node}.noise_chain_types', ['awgn'],
                             [c.get('type') for c in chain], bool(chain)))
            actual = chain[0].get('noise_power') if len(chain) == 1 else None
            tx = rx_noise.get(f'tx_power_{direction}')
            valid_tx = isinstance(tx, (int, float)) and not isinstance(tx, bool) and math.isfinite(tx) and tx > 0
            expected = tx / (10 ** (expected_awgn / 10)) if valid_tx else None
            shape_power = shape.get('cells', {}).get(cell, {}).get('noise_power', {}).get(node)
            for suffix, observed in [('topology_noise_power', actual), ('shape_noise_power', shape_power)]:
                valid = expected is not None and isinstance(observed, (int, float)) and not isinstance(observed, bool) and math.isfinite(observed)
                row = check(f'{cell}.{node}.{suffix}', expected, observed, valid)
                if valid:
                    row['status'] = 'passed' if math.isclose(expected, observed, rel_tol=1e-6, abs_tol=0) else 'failed'
                out.append(row)
    return out


def conditions(report, config_dir, expected_awgn=None, expected_cell=None):
    import yaml
    checks = []
    if expected_awgn is not None:
        params = report.get('run_parameters')
        params = params if isinstance(params, dict) else {}
        actual = params.get('sionna_awgn_snr_db')
        numeric = isinstance(actual, (int, float)) and not isinstance(actual, bool) and math.isfinite(actual)
        checks.append(check('sionna_awgn_snr_db', expected_awgn, actual, numeric))
        checks.extend(noise_conditions(config_dir, expected_awgn))
    for gnb in ('gnb0', 'gnb1'):
        path = config_dir / f'{gnb}.yaml' if config_dir else None
        config = yaml.safe_load(path.read_text()) if path and path.exists() else None
        for key, expected in (expected_cell or {}).items():
            actual = config
            for part in ['cell_cfg', *key.split('.')]:
                actual = actual.get(part) if isinstance(actual, dict) else None
            checks.append(check(f'{gnb}.cell_cfg.{key}', expected, actual, actual is not None))
    gate = report.get('status')
    status = ('failed' if (gate is not None and gate != 'passed') or any(c['status'] == 'failed' for c in checks) else
              'unverified' if gate is None or not checks or any(c['status'] == 'unverified' for c in checks) else 'passed')
    return {'status': status, 'gate': gate, 'checks': checks}


EVENTS = {
    'rrc_connected': r'\bRRC Connected\b',
    'pdu_established': r'PDU Session Establishment successful',
    'attach_attempt': r'Attaching UE',
    'random_access_complete': r'Random Access Complete',
    'out_of_sync': r'\bout[_ -]of[_ -]sync\b|\blost (?:PHY )?sync(?:hronization)?\b',
    'radio_link_failure': r'\bradio link failure\b|\bRLF detected\b',
    'rrc_release': r'\bRRC (?:Connection )?Releas(?:e|ed)\b|\bRRC disconnected\b',
    'reestablishment': r'\bre[- ]?establishment (?:request|started|successful|failed)\b',
    'shutdown': r'^Stopping \.\.',
}


def scan_events(path):
    counts = dict.fromkeys(EVENTS, 0)
    samples = []
    if not path.exists():
        return {'present': False, 'counts': counts, 'samples': samples}
    patterns = {k: re.compile(v, re.I) for k, v in EVENTS.items()}
    with path.open(errors='replace') as f:
        for lineno, line in enumerate(f, 1):
            for key, pattern in patterns.items():
                if pattern.search(line):
                    counts[key] += 1
                    if len(samples) < 30:
                        samples.append({'line': lineno, 'event': key, 'text': line.strip()})
    return {'present': True, 'counts': counts, 'samples': samples}


def lifecycle(report, log_dir, scan_internal=False):
    out = {'configured_duration_s': report.get('run_parameters', {}).get('run_duration_seconds'),
           'duration_note': 'Configured duration is not a measured attached duration; console events lack timestamps.',
           'claim': 'Counts are log observations, not certification of continuous PHY synchronization.', 'ues': {}}
    for ue, cell in [('ue0', 'a'), ('ue1', 'b')]:
        console = scan_events(log_dir / f'srsue-{ue}.log')
        internal = scan_events(log_dir / f'srsue-{ue}-internal.log') if scan_internal else None
        counts = console['counts']
        # Keep sources separate: one event can be emitted to console AND internal logs.
        indicators = ['out_of_sync', 'radio_link_failure', 'rrc_release', 'reestablishment']
        problems = any(counts[k] for k in indicators) or any(counts[k] > 1 for k in ['rrc_connected', 'pdu_established', 'attach_attempt'])
        if internal:
            problems |= any(internal['counts'][k] for k in indicators) or any(
                internal['counts'][k] > 1 for k in ['rrc_connected', 'pdu_established'])
        status = ('events_observed' if problems else 'incomplete' if not console['present'] or
                  counts['rrc_connected'] != 1 or counts['pdu_established'] != 1 else 'no_repeat_or_failure_in_scanned_logs')
        last_t = None
        broker = log_dir / f'broker-{cell}.log'
        if broker.exists():
            with broker.open(errors='replace') as f:
                for line in f:
                    m = re.search(r'\bt=(\d+)\b', line)
                    if m:
                        last_t = max(last_t or 0, int(m[1]))
        out['ues'][ue] = {'status': status, 'console': console, 'internal': internal,
            'sync_coverage': 'internal_and_console' if internal and internal['present'] else 'console_only_incomplete',
            'rrc_repeats_console': max(0, counts['rrc_connected'] - 1),
            'pdu_repeats_console': max(0, counts['pdu_established'] - 1),
            'last_broker_elapsed_s': last_t,
            'reported_alive_at_broker_stop': report.get('srsue_alive_at_broker_stop')}
    return out


def fixed_controller_audit(report, log_dir, compensated_side, base_seed=9000):
    """Audit the fixed-parameter side-swap repeat from saved brain summaries."""
    params = report.get('run_parameters', {})
    checks = [check('broker_sched', {'a': 'plain', 'b': 'plain'}, params.get('broker_sched'), 'broker_sched' in params),
              check('contention.kind', 'busy', params.get('contention', {}).get('kind'), 'kind' in params.get('contention', {}))]
    hook = params.get('root_exec', '')
    checks.append(check('declared_param_jitter_zero', True,
                        bool(re.search(r'--param-jitter[ =]+0(?:\.0+)?(?=[\s\'"]|$)', hook)), bool(hook)))
    fights, incomplete = [], []
    for folder in sorted((log_dir / 'fights').glob('f[0-9]*')):
        paths = [folder / 'arena.json', folder / 'brain0.json', folder / 'brain1.json']
        if not all(p.exists() for p in paths):
            incomplete.append(folder.name)
            continue
        arena, *brains = [json.loads(p.read_text()) for p in paths]
        expected_seed = base_seed + int(folder.name[1:]) - 1
        rows = [check('arena.seed', expected_seed, arena.get('seed'), 'seed' in arena)]
        for side, brain in enumerate(brains):
            expected_policy = 'balance_comp' if side == compensated_side else 'balance'
            rows.extend([check(f'brain{side}.policy', expected_policy, brain.get('policy'), 'policy' in brain),
                         check(f'brain{side}.seed', expected_seed * 10 + side, brain.get('seed'), 'seed' in brain),
                         check(f'brain{side}.v_cmd_max', 0.7, brain.get('params', {}).get('v_cmd_max'),
                               'v_cmd_max' in brain.get('params', {}))])
        shared = set(brains[0].get('params', {})) & set(brains[1].get('params', {}))
        required = {'k_pitch', 'k_pitch_rate', 'k_v', 'k_yaw', 'v_cmd_max', 'w_cmd_max', 'noise_rad'}
        rows.append(check('shared_required_params_present', True, required <= shared, True))
        for key in sorted(shared):
            rows.append(check(f'shared.{key}', brains[0]['params'][key], brains[1]['params'][key], True))
        fights.append({'fight': folder.name, 'checks': len(rows),
                       'nonpassing': [r for r in rows if r['status'] != 'passed']})
    nonpassing = [r for r in checks if r['status'] != 'passed']
    status = ('failed' if nonpassing or any(f['nonpassing'] for f in fights) else
              'unverified' if not fights else 'passed_completed_fights')
    return {'status': status, 'compensated_side': compensated_side, 'base_seed': base_seed,
            'run_checks': checks, 'completed_fights': fights, 'incomplete_fights': incomplete,
            'note': 'Jitter zero is declared in the launch hook and effective v_cmd_max/shared gains are checked in each completed brain pair; unfinished fights remain listed.'}


def failure_classification(report, log_dir, runner_exit=None):
    """Separate explicit dry-run config rejection from later runtime failure."""
    evidence = []
    unsupported = re.compile(r'not in (?:the )?range|not (?:a )?valid|not supported|unsupported|unknown (?:option|config)', re.I)
    paths = sorted(log_dir.glob('gnb*-dryrun.log'))
    runner = log_dir / 'runner.out'
    if runner.exists() and re.search(r'gnb\d+ dry run failed', runner.read_text(errors='replace')):
        paths.append(runner)
    for path in paths:
        with path.open(errors='replace') as f:
            for lineno, line in enumerate(f, 1):
                if unsupported.search(line):
                    evidence.append({'path': str(path), 'line': lineno, 'text': line.strip()})
    if evidence:
        category = 'configuration_not_supported'
    elif runner_exit == 0 or report.get('status') == 'passed':
        category = 'none'
    elif runner_exit is not None or report.get('status') is not None:
        category = 'runtime_or_attach_failure_unclassified'
    else:
        category = 'unverified'
    return {'category': category, 'evidence': evidence}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--report', type=pathlib.Path, required=True)
    p.add_argument('--config-dir', type=pathlib.Path)
    p.add_argument('--expect-awgn', type=float)
    p.add_argument('--expect-cell', action='append', default=[])
    p.add_argument('--log-dir', type=pathlib.Path)
    p.add_argument('--scan-internal', action='store_true')
    p.add_argument('--fixed-comp-side', type=int, choices=(0, 1),
                   help='audit fixed-parameter busy side-swap repeat, requires --log-dir')
    p.add_argument('--base-seed', type=int, default=9000)
    p.add_argument('--json', type=pathlib.Path)
    args = p.parse_args()
    import yaml
    expected = {}
    for item in args.expect_cell:
        key, sep, value = item.partition('=')
        if not sep or not key or key in expected:
            p.error('--expect-cell requires unique KEY=VALUE entries')
        expected[key] = yaml.safe_load(value)
        if expected[key] is None:
            p.error('expected cell value cannot be null')
    report = json.loads(args.report.read_text()) if args.report.exists() else {}
    out = {}
    if args.expect_awgn is not None or expected:
        if args.expect_awgn is not None and not math.isfinite(args.expect_awgn):
            p.error('--expect-awgn must be finite')
        out['conditions'] = conditions(report, args.config_dir, args.expect_awgn, expected)
    if args.log_dir:
        out['lifecycle'] = lifecycle(report, args.log_dir, args.scan_internal)
        out['failure'] = failure_classification(report, args.log_dir)
    if args.fixed_comp_side is not None:
        if not args.log_dir:
            p.error('--fixed-comp-side requires --log-dir')
        out['controllers'] = fixed_controller_audit(report, args.log_dir, args.fixed_comp_side, args.base_seed)
    if not out:
        p.error('request conditions and/or provide --log-dir')
    text = json.dumps(out, indent=2) + '\n'
    if args.json:
        args.json.write_text(text)
    print(text, end='')
    return 0 if (out.get('conditions', {}).get('status', 'passed') == 'passed' and
                 out.get('controllers', {}).get('status', 'passed_completed_fights') == 'passed_completed_fights') else 1


if __name__ == '__main__':
    raise SystemExit(main())
