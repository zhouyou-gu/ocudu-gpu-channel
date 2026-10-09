"""Fixtures for fail-closed condition provenance and bounded lifecycle claims."""
import importlib.util
import pathlib
import json
import tempfile
import unittest

PATH = pathlib.Path(__file__).resolve().parents[2] / 'use_cases/robot_fight/audit-r7-artifacts.py'
SPEC = importlib.util.spec_from_file_location('r7_audit', PATH)
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


class ArtifactAuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.report = {'status': 'passed', 'run_parameters': {'sionna_awgn_snr_db': 30, 'run_duration_seconds': 180}}
        for n in ['gnb0', 'gnb1']:
            (self.root / f'{n}.yaml').write_text('cell_cfg:\n  pusch:\n    min_k2: 8\n')

        shape = {'rx_noise': {'awgn_snr_db': 30, 'tx_power_ul': 300, 'tx_power_dl': 0.0112}, 'cells': {}}
        for cell, i in [('a', 0), ('b', 1)]:
            topo = {'devices': [], 'models': {}}
            shape['cells'][cell] = {'noise_power': {}}
            for role, power in [('gnb', 0.3), ('ue', 0.0000112)]:
                node = f'{role}{i}'
                topo['devices'].append({'id': f'{node}_p0', 'rx_model': f'rx_noise_{node}', 'sample_rate_hz': 23040000})
                topo['models'][f'rx_noise_{node}'] = {'chain': [{'type': 'awgn', 'noise_power': power}]}
                shape['cells'][cell]['noise_power'][node] = power
            (self.root / f'topology-{cell}.yaml').write_text(json.dumps(topo))
        (self.root / 'robot-fight-shape.json').write_text(json.dumps(shape))

    def tearDown(self):
        self.tmp.cleanup()

    def test_exact_applied_both_cells(self):
        out = audit.conditions(self.report, self.root, 30, {'pusch.min_k2': 8})
        self.assertEqual(out['status'], 'passed')
        self.assertEqual(len(out['checks']), 28)

    def test_sudo_dropped_knobs_fail(self):
        self.report['run_parameters']['sionna_awgn_snr_db'] = 40
        self.assertEqual(audit.conditions(self.report, self.root, 30)['status'], 'failed')

    def test_missing_evidence_never_passes(self):
        for report in [{}, {'status': 'passed'}, {'status': 'passed', 'run_parameters': {}}]:
            self.assertEqual(audit.conditions(report, self.root, 30)['status'], 'unverified')
        (self.root / 'gnb1.yaml').unlink()
        out = audit.conditions(self.report, self.root, 30, {'pusch.min_k2': 8})
        self.assertEqual(out['status'], 'unverified')
        self.assertEqual(out['checks'][-1]['status'], 'unverified')

    def test_wrong_nested_field_does_not_match(self):
        (self.root / 'gnb1.yaml').write_text('cell_cfg:\n  pdsch:\n    min_k2: 8\n')
        self.assertEqual(audit.conditions(self.report, self.root, None, {'pusch.min_k2': 8})['status'], 'unverified')

    def test_failed_gate_and_empty_checks_never_pass(self):
        self.report['status'] = 'failed'
        self.assertEqual(audit.conditions(self.report, self.root, 30)['status'], 'failed')
        self.report['status'] = 'passed'
        self.assertEqual(audit.conditions(self.report, self.root)['status'], 'unverified')

    def test_bool_is_not_numeric_parameter(self):
        self.assertEqual(audit.check('k', 1, True, True)['status'], 'failed')
        self.report['run_parameters']['sionna_awgn_snr_db'] = True
        self.assertNotEqual(audit.conditions(self.report, self.root, 1)['status'], 'passed')

    def test_report_matches_but_topology_noise_wrong(self):
        path = self.root / 'topology-b.yaml'
        data = json.loads(path.read_text())
        data['models']['rx_noise_ue1']['chain'][0]['noise_power'] *= 10
        path.write_text(json.dumps(data))
        self.assertEqual(audit.conditions(self.report, self.root, 30)['status'], 'failed')

    def test_noise_model_not_connected_fails(self):
        path = self.root / 'topology-a.yaml'
        data = json.loads(path.read_text())
        data['devices'][0]['rx_model'] = 'sionna_rt'
        path.write_text(json.dumps(data))
        self.assertEqual(audit.conditions(self.report, self.root, 30)['status'], 'failed')

    def test_missing_topology_and_power_unverified(self):
        (self.root / 'topology-b.yaml').unlink()
        self.assertEqual(audit.conditions(self.report, self.root, 30)['status'], 'unverified')

    def test_probe_analyzer_report_layouts(self):
        path = PATH.with_name('analyze-r7-probe-runs.py')
        spec = importlib.util.spec_from_file_location('r7_probe_analyzer', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        native = self.root / 'results/logs/ocudu-robot-fight/stamp'
        native.mkdir(parents=True)
        report = native.parents[2] / 'reports/ocudu-robot-fight/stamp/attach-summary.json'
        report.parent.mkdir(parents=True)
        report.write_text(json.dumps({'status': 'passed'}))
        self.assertEqual(module.gate_result(native)['result'], 'passed')
        local = native / 'report/attach-summary.json'
        local.parent.mkdir()
        local.write_text(json.dumps({'status': 'failed'}))
        self.assertEqual(module.gate_result(native)['result'], 'failed')
        self.assertEqual(module.gate_result(self.root), {})

    def test_config_rejection_is_not_radio_failure(self):
        (self.root / 'gnb0-dryrun.log').write_text('--min_k2: Value 8 not in range [1 - 4]\n')
        result = audit.failure_classification({}, self.root, 2)
        self.assertEqual(result['category'], 'configuration_not_supported')
        self.assertEqual(len(result['evidence']), 1)
        (self.root / 'gnb0-dryrun.log').write_text('configuration accepted\n')
        self.assertEqual(audit.failure_classification({'status': 'failed'}, self.root, 1)['category'], 'runtime_or_attach_failure_unclassified')

    def test_early_dryrun_failure_without_timestamp(self):
        (self.root / 'runner.out').write_text('--min_k2: Value 8 not in range [1 - 4]\nerror: gnb0 dry run failed\nR7_EXIT=2\n')
        result = audit.failure_classification({}, self.root, 2)
        self.assertEqual(result['category'], 'configuration_not_supported')
        self.assertTrue(result['evidence'][0]['path'].endswith('runner.out'))

    def test_fixed_repeat_policy_seed_and_effective_params(self):
        report = {'run_parameters': {'broker_sched': {'a': 'plain', 'b': 'plain'}, 'contention': {'kind': 'busy'}, 'root_exec': "RF_BRAIN_EXTRA='--param-jitter 0' command"}}
        folder = self.root / 'fights/f001'
        folder.mkdir(parents=True)
        (folder / 'arena.json').write_text(json.dumps({'seed': 9000}))
        params = {k: 1 for k in ['k_pitch', 'k_pitch_rate', 'k_v', 'k_yaw', 'w_cmd_max', 'noise_rad']}
        params['v_cmd_max'] = 0.7
        for side, policy in enumerate(['balance_comp', 'balance']):
            (folder / f'brain{side}.json').write_text(json.dumps({'policy': policy, 'seed': 90000 + side, 'params': params}))
        self.assertEqual(audit.fixed_controller_audit(report, self.root, 0)['status'], 'passed_completed_fights')
        self.assertEqual(audit.fixed_controller_audit(report, self.root, 1)['status'], 'failed')
        (folder / 'brain1.json').unlink()
        result = audit.fixed_controller_audit(report, self.root, 0)
        self.assertEqual(result['status'], 'unverified')
        self.assertEqual(result['incomplete_fights'], ['f001'])

    def test_initial_attach_is_not_repeat_and_console_is_incomplete(self):
        (self.root / 'srsue-ue0.log').write_text('Attaching UE...\nRRC Connected\nPDU Session Establishment successful. IP: 1\nStopping ..\n')
        (self.root / 'broker-a.log').write_text('event=heartbeat t=179\n')
        out = audit.lifecycle(self.report, self.root)
        ue = out['ues']['ue0']
        self.assertEqual(ue['rrc_repeats_console'], 0)
        self.assertEqual(ue['console']['counts']['shutdown'], 1)
        self.assertEqual(ue['last_broker_elapsed_s'], 179)
        self.assertEqual(ue['sync_coverage'], 'console_only_incomplete')
        self.assertEqual(out['ues']['ue1']['status'], 'incomplete')

    def test_repeated_attach_and_failures_detected(self):
        (self.root / 'srsue-ue0.log').write_text('RRC Connected\nPDU Session Establishment successful\nRRC Released\nRadio link failure\nout-of-sync\nRRC Connected\nPDU Session Establishment successful\n')
        ue = audit.lifecycle(self.report, self.root)['ues']['ue0']
        self.assertEqual(ue['status'], 'events_observed')
        self.assertEqual(ue['rrc_repeats_console'], 1)
        self.assertEqual(ue['pdu_repeats_console'], 1)
        self.assertEqual(ue['console']['counts']['out_of_sync'], 1)

    def test_internal_and_console_do_not_double_count_initial_attach(self):
        text = 'RRC Connected\nPDU Session Establishment successful\n'
        (self.root / 'srsue-ue0.log').write_text(text)
        (self.root / 'srsue-ue0-internal.log').write_text(text)
        ue = audit.lifecycle(self.report, self.root, True)['ues']['ue0']
        self.assertEqual(ue['status'], 'no_repeat_or_failure_in_scanned_logs')
        self.assertEqual(ue['rrc_repeats_console'], 0)
        self.assertEqual(ue['sync_coverage'], 'internal_and_console')


if __name__ == '__main__':
    unittest.main()
