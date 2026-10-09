"""Check the demo's antenna dimensions and complete synthetic endpoint lists."""
import argparse
import json
from pathlib import Path

import yaml


def validate(topology, scenario, sources, sinks):
    devices = {d['id']: d for d in topology['devices']}
    nodes = topology.get('radio_nodes') or [
        {'id': key, 'tx_ports': [key], 'rx_ports': [key]} for key in devices
    ]
    nodes = {node['id']: node for node in nodes}
    for key, node in scenario['nodes'].items():
        for direction in ('tx', 'rx'):
            array = node.get(direction + '_array', node.get('array', {}))
            count = array.get('rows', 1) * array.get('cols', 1)
            if key not in nodes or len(nodes[key][direction + '_ports']) != count:
                raise ValueError(f'{key} {direction}: scenario/topology antenna dimensions differ')
    for field, actual in [('tx_endpoint', sources), ('rx_endpoint', sinks)]:
        expected = sorted(int(d[field].rsplit(':', 1)[1]) for d in devices.values())
        if sorted(actual) != expected:
            raise ValueError(f'{field}: launched ports {sorted(actual)} != topology ports {expected}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--topology', required=True)
    parser.add_argument('--scenario', required=True)
    parser.add_argument('--sources', required=True)
    parser.add_argument('--sinks', required=True)
    args = parser.parse_args()
    validate(yaml.safe_load(Path(args.topology).read_text()),
             json.loads(Path(args.scenario).read_text()),
             [int(x) for x in args.sources.split(',')],
             [int(x) for x in args.sinks.split(',')])
    print('demo topology and antenna endpoints verified')


if __name__ == '__main__':
    main()
