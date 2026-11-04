#!/usr/bin/env python3
"""Verify a completed synthetic SUTD crosstalk run; emit a compact summary."""
import argparse
import json
import math
import re
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('log_dir', type=Path)
    args = parser.parse_args()
    first = last = None
    count = 0
    powers = {'ue0>ue1:crosstalk': [], 'ue1>ue0:crosstalk': []}
    with (args.log_dir / 'sionna-status.jsonl').open() as handle:
        for line in handle:
            record = json.loads(line)
            if record.get('event') != 'sionna_rt_update':
                continue
            reply = record['control_reply']
            if not reply.get('ok') or reply.get('link_count') != 10:
                raise ValueError('expected successful ten-edge commit')
            channels = record['channels']
            if len(channels) != 10 or {c['carrier_frequency_hz'] for c in channels} != {1900000000.0}:
                raise ValueError('expected ten channels at 1.9 GHz')
            for key in powers:
                channel = next(c for c in channels if c['link_id'] == key)
                power = channel.get('total_path_power_db')
                if isinstance(power, (int, float)) and math.isfinite(power):
                    powers[key].append(power)
            first = first or record
            last = record
            count += 1
    if count < 2 or any(not values for values in powers.values()):
        raise ValueError('insufficient updates or no nonzero traced crosstalk paths')
    moving = [node for node in ('ue0', 'ue1') if first['positions'][node] != last['positions'][node]]
    if len(moving) != 2:
        raise ValueError('both UE positions must advance')
    broker = (args.log_dir / 'broker.log').read_text()
    stops = [line for line in broker.splitlines() if line.startswith('event=stop ')]
    if 'event=fatal' in broker or 'backend=cuda cuda_status=cuda:compiled,runtime:available' not in broker or not stops:
        raise ValueError('broker did not complete on CUDA')
    counters = {key: int(value) for key, value in re.findall(r'(\w+)=(\d+)', stops[-1])}
    for key in ('control_updates_rejected', 'control_batches_aborted', 'zmq_errors', 'tx_queue_overflows', 'tx_sequence_gaps'):
        if counters[key] != 0:
            raise ValueError(f'{key}={counters[key]}')
    for key in ('tx_pulls', 'rx_requests', 'control_batches_committed'):
        if counters[key] <= 0:
            raise ValueError(f'no work: {key}')
    print(json.dumps({'updates': count, 'moving_ues': moving, 'carrier_hz': 1900000000,
                      'crosstalk_power_db': {key: [min(v), max(v)] for key, v in powers.items()},
                      'broker_counters': counters, 'strict_realtime_qualified': False}, indent=2))


if __name__ == '__main__':
    main()
