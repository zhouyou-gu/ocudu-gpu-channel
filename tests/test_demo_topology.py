import importlib.util
import json
from pathlib import Path
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('check_demo', ROOT/'scripts/sionna_rt/check_demo_topology.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class DemoTopologyTests(unittest.TestCase):
    def test_sutd_dimensions_and_all_ports(self):
        scenario = json.loads((ROOT/'use_cases/configs/sionna/scenarios/sutd/multi-gnb-sutd.json').read_text())
        topology = yaml.safe_load((ROOT/'use_cases/configs/topologies/sionna/topology.sionna-multi-gnb.cuda.yaml').read_text())
        sources = [3000,3002,3004,3006,3010,3012,3014,3016,3101,3103]
        sinks = [3001,3003,3005,3007,3011,3013,3015,3017,3100,3102]
        checker.validate(topology, scenario, sources, sinks)
        with self.assertRaisesRegex(ValueError, 'launched ports'):
            checker.validate(topology, scenario, sources[:-1], sinks)
        old = yaml.safe_load((ROOT/'use_cases/configs/topologies/ocudu_docker/topology.multi-gnb.cuda.yaml').read_text())
        with self.assertRaisesRegex(ValueError, 'antenna dimensions'):
            checker.validate(old, scenario, sources, sinks)
