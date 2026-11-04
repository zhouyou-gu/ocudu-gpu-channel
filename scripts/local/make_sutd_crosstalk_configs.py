#!/usr/bin/env python3
"""Generate a shared-carrier SUTD synthetic demo in an output directory."""
import argparse
import copy
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


class IndentedDumper(yaml.SafeDumper):
    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    scenario = json.loads((ROOT / 'use_cases/configs/sionna/scenarios/sutd/multi-gnb-sutd.json').read_text())
    topology = yaml.safe_load((ROOT / 'use_cases/configs/topologies/sionna/topology.sionna-multi-gnb.cuda.yaml').read_text())
    if len(topology['devices']) != 10:
        raise ValueError('expected ten ports')
    for device in topology['devices']:
        device.pop('tx_carrier', None)
        device.pop('rx_carrier', None)
        device['carrier'] = 'tdd-shared'
    for source, destination in (('ue0', 'ue1'), ('ue1', 'ue0')):
        for links in (scenario['links'], topology['links']):
            if any(link['from'] == source and link['to'] == destination for link in links):
                raise ValueError(f'{source} to {destination} already exists')
        scenario['links'].append({'from': source, 'to': destination, 'direction': 'crosstalk', 'model': 'crosstalk'})
        topology['links'].append({'from': source, 'to': destination, 'model': 'crosstalk'})
    if len(scenario['links']) != 10 or len(topology['links']) != 10:
        raise ValueError('expected ten directed edges in both configurations')
    topology['models']['crosstalk'] = copy.deepcopy(topology['models']['serving'])
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'scenario.json').write_text(json.dumps(scenario, indent=2) + '\n')
    (args.output / 'topology.yaml').write_text(yaml.dump(topology, Dumper=IndentedDumper, sort_keys=False))
    print(f'generated ten-edge shared-carrier configuration in {args.output}')


if __name__ == '__main__':
    main()
