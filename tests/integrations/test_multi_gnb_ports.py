"""Generated gNB antennas and Docker ports must match the broker topology."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "scripts/remote/ocudu-multi-gnb-smoke.sh"


class MultiGnbPortsTest(unittest.TestCase):
    def check_mode(self, mode, topology):
        source = LAUNCHER.read_text()
        generated = source.split("# --- generated config", 1)[1]
        generated = generated[generated.index("\ngnb0_config="):]
        generated = generated[:generated.index("\nawk '\n")]
        compose = source.split('cat >"${compose_override}" <<YAML\n', 1)[1].split("\nYAML", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, project_root=str(ROOT), config_dir=directory,
                       channel_mode=mode, ue_inactivity_seconds="600",
                       OPEN5GS_IP="10.53.1.2", GNB_IP="10.53.1.3",
                       ran_prefix="10.53.1", metrics_prefix="172.19.1",
                       ran_subnet="10.53.1.0/24", metrics_subnet="172.19.1.0/24",
                       fivegc_ports="      []")
            result = subprocess.run(
                ["bash"], input="set -euo pipefail\n" + generated
                + '\ncat >"${compose_override}" <<YAML\n' + compose + "\nYAML\n",
                env=env, text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            directory = Path(directory)
            services = yaml.safe_load((directory / "docker-compose.ocudu-gpu-channel.yml")
                                      .read_text().replace("!override", ""))["services"]
            devices = yaml.safe_load((ROOT / topology).read_text())["devices"]
            for cell in range(2):
                node = f"gnb{cell}"
                ports = [d for d in devices if d["id"] == node or d["id"].startswith(node + "_p")]
                config = yaml.safe_load((directory / f"{node}_zmq.yml").read_text())
                # The scalar fixture uses OCUDU's one-antenna defaults.
                self.assertEqual(config["cell_cfg"].get("nof_antennas_dl", 1), len(ports), node)
                self.assertEqual(config["cell_cfg"].get("nof_antennas_ul", 1), len(ports), node)
                arguments = config["ru_sdr"]["device_args"]
                for direction in ("tx", "rx"):
                    actual = re.findall(direction + r"_port\d+=tcp://[^,:]+:(\d+)", arguments)
                    expected = [d[direction + "_endpoint"].rsplit(":", 1)[1] for d in ports]
                    self.assertEqual(actual, expected, (node, direction))
                service = services["gnb" if cell == 0 else "gnb1"]
                self.assertEqual(service["ports"], [f'{p}:{p}' for p in
                                 (d["tx_endpoint"].rsplit(":", 1)[1] for d in ports)])
                self.assertEqual(config["cu_cp"]["inactivity_timer"], 600)
                self.assertEqual(config["cell_cfg"]["pci"], cell + 1)
                if mode == "sionna":
                    self.assertEqual(config["cell_cfg"]["pusch"]["max_ue_mcs"], 9)

    def test_static_radios_match_scalar_topology(self):
        self.check_mode("static", "use_cases/configs/topologies/ocudu_docker/topology.multi-gnb.cuda.yaml")

    def test_sionna_radios_match_four_port_topology(self):
        self.check_mode("sionna", "use_cases/configs/topologies/sionna/topology.sionna-multi-gnb.cuda.yaml")


if __name__ == "__main__":
    unittest.main()
