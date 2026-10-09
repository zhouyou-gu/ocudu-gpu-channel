#!/usr/bin/env python3
"""Render the native multi-gNB attach configs: two cells, two UEs, one core.

The multi-gNB sibling of render-multi-ue-configs.py, and the Docker-free
counterpart of the configs scripts/remote/ocudu-multi-gnb-smoke.sh generates.
Two gNB processes share one network namespace here, so each cell gets its own
PCI, gnb_id, ran_node_name, ZMQ port pair, and N2/N3 bind address (two gNBs on
one address would collide on the GTP-U port). Everything else -- the gNB
fixture, the Open5GS rendering, the srsUE template, the subscriber records --
is the multi-UE gate's, so a difference between the two gates points at the
second cell and nothing else.

OCUDU_NATIVE_GNB_ACCELERATION (disabled|low-phy-rx|...|all) appends the CUDA
acceleration block of scripts/cuda/render-cuda-1x1-configs.py to BOTH gNB
configs, so the same renderer serves the CPU gNB (unset) and the CUDA gNB.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


multi_ue = load("render_multi_ue_configs", HERE / "render-multi-ue-configs.py")
fail = multi_ue.fail
replace_exact = multi_ue.replace_exact

# One entry per cell. The ZMQ ports must agree with the port map below, which
# rewrites use_cases/configs/topologies/ocudu_docker/topology.multi-gnb.cuda.yaml onto the native loopback plan.
CELLS = (
    {"device_id": "gnb0", "tx_port": 2000, "rx_port": 2001, "pci": 1, "gnb_id": 411, "bind": "127.0.0.11",
     "prach_root": 1},
    {"device_id": "gnb1", "tx_port": 2010, "rx_port": 2011, "pci": 2, "gnb_id": 412, "bind": "127.0.0.12",
     "prach_root": 200},
)
# Two co-channel cells must not share a PRACH root sequence: the srsRAN ZMQ
# radios share the broker's lock-step time, so both UEs RACH on the same
# occasion, and with one root each gNB also detects the OTHER cell's preamble
# over the inter-cell path and admits a phantom UE (seen as crc=KO PUSCH for
# an RNTI the real UE never follows). Real deployments plan distinct roots.
# The UEs, IMSIs, netns and IPv4s are the multi-UE gate's: ue0 camps on gnb0,
# ue1 on gnb1 (the topology's serving/intercell path losses decide that).
# This gate has two UEs; the multi-UE table grew to four with the 4-UE option,
# so take the slice (the subscriber fixture is validated against it).
UES = tuple(multi_ue.UES)[:2]
# use_cases/configs/topologies/ocudu_docker/topology.multi-gnb.cuda.yaml port -> native port.
PORT_MAP = {3000: 2000, 3001: 2001, 3002: 2010, 3003: 2011,
            3100: 2100, 3101: 2101, 3102: 2102, 3103: 2103}


def render_gnb(source: str, cell: dict, log_dir: Path) -> str:
    """The multi-UE gNB rendering, with this cell's identity and addresses."""
    name = cell["device_id"]
    rendered = replace_exact(source, "    addrs: 10.53.1.2\n", "    addrs: 127.0.0.2\n", 1, "gNB AMF address")
    rendered = replace_exact(
        rendered, "    bind_addrs: 10.53.1.1\n", f"    bind_addrs: {cell['bind']}\n", 1, "gNB NGAP bind address"
    )
    rendered = multi_ue.insert_before_exact(
        rendered,
        "ru_sdr:\n",
        f"cu_up:\n  ngu:\n    socket:\n      - bind_addr: {cell['bind']}\n\n",
        "gNB explicit N3 bind insertion",
    )
    rendered = replace_exact(
        rendered,
        "  device_args: tx_port=tcp://*:2000,rx_port=tcp://host.docker.internal:2001,base_srate=23.04e6\n",
        f"  device_args: tx_port=tcp://127.0.0.1:{cell['tx_port']},"
        f"rx_port=tcp://127.0.0.1:{cell['rx_port']},base_srate=23.04e6\n",
        1,
        "gNB ZMQ loopback endpoints",
    )
    rendered = replace_exact(rendered, "  tx_gain: 75\n", "  tx_gain: 0\n", 1, "native gNB TX gain")
    rendered = replace_exact(rendered, "  rx_gain: 75\n", "  rx_gain: 0\n", 1, "native gNB RX gain")
    rendered = replace_exact(rendered, "cell_cfg:\n", f"cell_cfg:\n  pci: {cell['pci']}\n", 1, "gNB PCI")
    rendered = replace_exact(
        rendered, "  prach:\n    prach_config_index: 1\n",
        f"  prach:\n    prach_config_index: 1\n    prach_root_sequence_index: {cell['prach_root']}\n",
        1, "gNB PRACH root sequence")
    for old, new in {
        "  filename: /tmp/gnb.log\n": f"  filename: {log_dir / f'{name}-internal.log'}\n",
        "  mac_filename: /tmp/gnb_mac.pcap\n": f"  mac_filename: {log_dir / f'{name}_mac.pcap'}\n",
        "  ngap_filename: /tmp/gnb_ngap.pcap\n": f"  ngap_filename: {log_dir / f'{name}_ngap.pcap'}\n",
    }.items():
        rendered = replace_exact(rendered, old, new, 1, "gNB run artifact path")
    rendered += f"\ngnb_id: {cell['gnb_id']}\nran_node_name: {name}\n"
    return rendered


def add_acceleration(text: str, stage: str) -> str:
    cuda = load("render_cuda_1x1_configs", HERE.parent / "cuda/render-cuda-1x1-configs.py")
    if "expert_phy:" in text or "  expert_cfg:" in text:
        fail("rendered gNB config already carries expert settings")
    ru, phy = cuda.acceleration_block(stage)
    text = replace_exact(text, "ru_sdr:\n", "ru_sdr:\n  expert_cfg:\n" + ru + "\n", 1, "CUDA lower-PHY block")
    return text + "\nexpert_phy:\n" + phy + "\n"


def render_topology(source: str, channel_mode: str = "legacy") -> str:
    """Move the 2-cell topology onto the native ports and loopback REP binds.

    legacy: the checked-in fixed-TDL serving/intercell topology. sionna: every
    link is a `sionna_rt` placeholder the bridge replaces live, and the link
    set (8 serving/intercell, or 10 with the UE<->UE crosstalk pair) is the
    topology file's -- the scenario must name the same links.
    """
    rendered = source
    for old, new in PORT_MAP.items():
        count = rendered.count(f":{old}\n")
        if count != 1:
            fail(f"multi-gNB topology port {old}: expected 1 endpoint, found {count}")
        rendered = rendered.replace(f":{old}\n", f":{new}\n")
    count = rendered.count("tcp://*:")
    if count != 4:
        fail(f"multi-gNB topology: expected 4 wildcard REP binds, found {count}")
    rendered = rendered.replace("tcp://*:", "tcp://127.0.0.1:")
    if channel_mode == "sionna":
        required_links = (
            "  - from: gnb0\n    to: ue0\n    model: sionna_rt\n",
            "  - from: gnb1\n    to: ue1\n    model: sionna_rt\n",
            "  - from: gnb0\n    to: ue1\n    model: sionna_rt\n",
            "  - from: gnb1\n    to: ue0\n    model: sionna_rt\n",
        )
    else:
        required_links = (
            "  - from: gnb0\n    to: ue0\n    model: serving\n",
            "  - from: gnb1\n    to: ue1\n    model: serving\n",
            "  - from: gnb0\n    to: ue1\n    model: intercell\n",
            "  - from: gnb1\n    to: ue0\n    model: intercell\n",
        )
    for required in required_links:
        if source.count(required) != 1:
            fail(f"multi-gNB topology invariant is missing or ambiguous: {required!r}")
    host_memory = os.environ.get("OCUDU_NATIVE_CUDA_HOST_MEMORY")
    if host_memory:
        if host_memory not in ("copy", "zero_copy", "auto"):
            fail(f"invalid OCUDU_NATIVE_CUDA_HOST_MEMORY: {host_memory}")
        rendered = replace_exact(
            rendered, "runtime:\n", f"runtime:\n  cuda_host_memory: {host_memory}\n", 1, "broker host memory"
        )
    return rendered


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--native-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--topology", type=Path, default=Path("use_cases/configs/topologies/ocudu_docker/topology.multi-gnb.cuda.yaml"),
                        help="broker topology, relative to the repo root or absolute")
    parser.add_argument("--channel-mode", choices=("legacy", "sionna"), default="legacy")
    args = parser.parse_args()

    repo_root = args.repo_root.resolve(strict=True)
    native_root = args.native_root.resolve(strict=True)
    output_dir = multi_ue.safe_directory(args.output_dir, "output directory")
    log_dir = multi_ue.safe_directory(args.log_dir, "log directory")
    read = multi_ue.read_regular

    gnb_source = read(repo_root / "use_cases/configs/ran/ocudu/docker/gnb_zmq_b210_fdd_srsue.yaml", "gNB fixture")
    stage = os.environ.get("OCUDU_NATIVE_GNB_ACCELERATION")
    outputs = {}
    for cell in CELLS:
        text = render_gnb(gnb_source, cell, log_dir)
        outputs[f"{cell['device_id']}.yaml"] = add_acceleration(text, stage) if stage else text
    topology_path = args.topology if args.topology.is_absolute() else repo_root / args.topology
    outputs["topology.yaml"] = render_topology(
        read(topology_path, "multi-gNB topology"), args.channel_mode
    )
    outputs["open5gs.yaml"] = multi_ue.render_open5gs(
        read(native_root / "src/ocudu/docker/open5gs/open5gs-5gc.yml", "pinned OCUDU Open5GS template"),
        native_root,
    )
    # validate_subscriber compares against a UE slice since the 4-UE commit;
    # this gate's two UEs are the multi-UE table's first two.
    outputs["subscriber.csv"] = multi_ue.validate_subscriber(
        read(repo_root / "use_cases/configs/ran/open5gs/subscriber-multi-ue.csv", "subscriber fixture"), UES
    )
    srsue_source = read(repo_root / "use_cases/configs/ran/srsue/srsue_zmq_multi_ue.conf.in", "srsUE template")
    for ue in UES:
        outputs[f"srsue-{ue['device_id']}.conf"] = multi_ue.render_srsue(srsue_source, ue, log_dir)

    for name, text in outputs.items():
        multi_ue.write_new(output_dir / name, text)
    print(f"rendered={' '.join(sorted(outputs))} acceleration={stage or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
