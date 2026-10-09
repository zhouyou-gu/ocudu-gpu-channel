"""Exercise launcher argument handling without starting a radio or GPU runtime."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SionnaLauncherTests(unittest.TestCase):
    def run_wrapper(self, extra):
        with tempfile.TemporaryDirectory(prefix='sionna launcher ') as directory:
            folder = Path(directory)
            scenario = folder / 'scenario.json'
            scenario.write_text('{}')
            python = folder / 'python stub'
            python.write_text(f'#!{sys.executable}\n' + '''
import json, os, pathlib, sys, time
folder = pathlib.Path(os.environ['LAUNCHER_RECORDS'])
args = sys.argv[1:]
if args[0] == '-c':
    (folder / 'ready').touch()
    sys.exit(0)
name = pathlib.Path(args[0]).name
(folder / (name + '.json')).write_text(json.dumps(args[1:]))
if name == 'run_bridge.py':
    pathlib.Path(args[args.index('--status-jsonl') + 1]).write_text('{"event":"sionna_rt_update"}\\n')
    deadline = time.monotonic() + 5
    while not (folder / 'ready').exists():
        if time.monotonic() > deadline:
            sys.exit(1)
        time.sleep(0.01)
    time.sleep(0.05)
else:
    time.sleep(10)
''')
            python.chmod(0o755)
            result = subprocess.run(
                ['bash', str(ROOT / 'scripts/local/run_web_ui.sh'),
                 '--python', str(python), '--scenario', str(scenario),
                 '--status-jsonl', str(folder / 'status.jsonl'),
                 '--ready-seconds', '5', *extra],
                env={**os.environ, 'LAUNCHER_RECORDS': directory},
                capture_output=True, text=True, timeout=8)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('event=sionna_web_ui_ready', result.stdout)
            bridge = json.loads((folder / 'run_bridge.py.json').read_text())
            web = json.loads((folder / 'server.py.json').read_text())
            self.assertEqual(bridge[bridge.index('--scenario-config') + 1], str(scenario))
            return bridge, web

    def test_wrapper_default_and_empty_separator(self):
        for extra in ([], ['--']):
            with self.subTest(extra=extra):
                bridge, web = self.run_wrapper(extra)
                self.assertEqual(bridge[bridge.index('--update-hz') + 1], '500')
                self.assertNotIn('--gain-offset-db', bridge)
                self.assertNotIn('--gain-offset-db', web)

    def test_wrapper_extra_arguments_only_reach_bridge(self):
        extra = ['--ue0-start=-20,0,1.5', '--gain-offset-db', '75',
                 '--scene-xml', 'path with spaces/scene.xml']
        bridge, web = self.run_wrapper(['--', *extra])
        self.assertEqual(bridge[-len(extra):], extra)
        for flag in ('--ue0-start=-20,0,1.5', '--gain-offset-db', '--scene-xml'):
            self.assertNotIn(flag, web)

    def transport_args(self, extra):
        # Exercise the actual launcher's encoding, remote invocation and decode
        # preamble. Stop before any build, Docker, SSH or radio action.
        source = (ROOT / 'scripts/remote/ocudu-multi-gnb-smoke.sh').read_text()
        start = source.index('# Encode before ssh')
        outer, inner = source[start:].split("<<'REMOTE'\n", 1)
        inner = inner[:inner.index('[[ "${hold_seconds}"')]
        variables = ('REMOTE_WORKSPACE REMOTE_PROJECT_ROOT REMOTE_BUILDS_ROOT '
                     'REMOTE_RESULTS_ROOT REMOTE_OCUDU_ROOT duration_seconds '
                     'build_docker srsran_ref ue_stagger_seconds broker_image_arg '
                     'channel_mode sionna_python_arg sionna_update_hz '
                     'sionna_ready_seconds sionna_web_port sionna_scenario '
                     'cuda_compiler_arg execution_mode fivegc_host_port '
                     'hold_seconds ue_inactivity_seconds ue_keepalive_seconds '
                     'topology_name').split()
        preamble = 'set -euo pipefail\n' + '\n'.join(f'{name}=value{n}' for n, name in enumerate(variables))
        # ssh joins its command arguments before the remote shell parses them.
        preamble += '\nfivegc_host_port=\nremote_sh() { bash -c "$*"; }\n'
        report = 'printf \'%s\\0\' "${cuda_compiler}" "${execution_mode}" "${topology_name}" "${fivegc_host_port:-empty}"\n'
        report += 'if [[ "${#sionna_bridge_args[@]}" -gt 0 ]]; then printf \'%s\\0\' "${sionna_bridge_args[@]}"; fi\n'
        result = subprocess.run(['bash'], input=preamble + '\n' + outer + "<<'REMOTE'\n" + inner + report + 'REMOTE\n',
                                env={**os.environ, 'OCUDU_MGNB_SIONNA_EXTRA_ARGS': extra},
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        values = result.stdout.rstrip('\0').split('\0')
        self.assertEqual(values[:4], ['value16', 'value17', 'value22', 'empty'])
        return values[4:]

    def test_remote_arguments_survive_ssh_without_evaluation(self):
        args = '--ue0-start=-20,0,1.5 --gain-offset-db 75\n--literal $HOME $(id) * ;'
        self.assertEqual(self.transport_args(args), args.split())

    def test_remote_default_arguments(self):
        self.assertEqual(self.transport_args(''), [])
