"""R7 overrides must cross sudo explicitly; failed gates must remain failures."""
import os
import pathlib
import subprocess
import tempfile
import unittest


LAUNCH = pathlib.Path(__file__).resolve().parents[2] / "use_cases/robot_fight/launch/run_r7_spark.sh"


class R7LaunchTests(unittest.TestCase):
    def run_launcher(self, *, inherited=False, status=0):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        for name, text in {
            "nvidia-smi": "#!/bin/sh\nexit 0\n",
            "sudo": f"#!/bin/sh\nprintf '%s\\n' \"$@\"\nexit {status}\n",
        }.items():
            path = root / name
            path.write_text(text)
            path.chmod(0o755)
        env = {key: value for key, value in os.environ.items() if not key.startswith("OCUDU_NATIVE_")}
        env.update(PATH=str(root) + os.pathsep + env["PATH"], R7_OUTPUT_ROOT=str(root / "out"),
                   RF_POLICY_0="balance", RF_POLICY_1="balance_comp", RF_PARAM_JITTER="0")
        if inherited:
            env["OCUDU_NATIVE_SIONNA_AWGN_SNR_DB"] = "27"
        proc = subprocess.run(["bash", str(LAUNCH), "case", "OCUDU_NATIVE_SIONNA_AWGN_SNR_DB=27"],
                              env=env, text=True, capture_output=True)
        log = root / "out/case.out"
        return proc, log.read_text() if log.exists() else ""

    def test_explicit_override_and_policies_reach_sudo(self):
        proc, log = self.run_launcher()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("OCUDU_NATIVE_SIONNA_AWGN_SNR_DB=27\n", log)
        self.assertIn("RF_POLICY_0=balance RF_POLICY_1=balance_comp", log)
        self.assertIn("RF_BRAIN_EXTRA='--param-jitter 0'", log)

    def test_inherited_override_is_not_silently_ignored(self):
        proc, log = self.run_launcher(inherited=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("as a command argument", proc.stderr)
        self.assertEqual(log, "")

    def test_gate_failure_preserves_exit_status(self):
        proc, log = self.run_launcher(status=7)
        self.assertEqual(proc.returncode, 7)
        self.assertIn("R7_EXIT=7", log)


if __name__ == "__main__":
    unittest.main()
