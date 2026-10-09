#!/usr/bin/env python3
"""Render the native multi-UE attach configs.

The multi-UE sibling of render-legacy-1x1-configs.py. The single-UE renderer
cannot be reused as-is: its validate_subscriber() rejects any CSV carrying more
than the one audited legacy record, and its srsUE renderer asserts a fixed
IMSI, a fixed ZMQ port pair, and netns ue1. Those assertions are correct for
the 1x1 gate and are left untouched there; this file carries the multi-UE
equivalents.

Everything that is not UE-multiplicity is deliberately identical to the 1x1
renderer -- the same gNB source file with the same loopback/NGAP rewrites, and
the same Open5GS placeholder substitution -- so a difference between the two
gates points at UE multiplicity and nothing else.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import NoReturn

PLACEHOLDER_RE = re.compile(r"\$\{[A-Z0-9_]+\}|@[A-Z0-9_]+@")

# X6 (docs/reports/experiments/x6-gnb-realtime-traffic/README.md): the OCUDU gNB's lower-PHY thread
# profile. Unset (the default) renders the fixture untouched, so every run so
# far is unchanged. `single`, `dual` or `triple` append
#   expert_execution.threads.lower_phy.execution_profile
# (the key the gNB's own YAML writer emits, apps/units/flexible_o_du/split_8/
# helpers/ru_sdr_config_yaml_writer.cpp). Caveat measured on 2026-10-01: with
# device_driver zmq, OCUDU a1916edc overrides this to `blocking` after parsing
# (ru_sdr_config_cli11_schema.cpp autoderive_ru_sdr_parameters_after_parsing)
# and maps blocking to the worker manager's `sequential` profile
# (ru_sdr_config_translator.cpp fill_sdr_worker_manager_config), so the knob is
# accepted by the dry run but changes nothing until the gNB source is patched.
# The gate records the "Lower PHY in ... mode" line the gNB prints, so a run
# shows which profile it really had.
LOWER_PHY_PROFILE_ENV = "OCUDU_NATIVE_GNB_LOWER_PHY_PROFILE"
LOWER_PHY_PROFILES = ("single", "dual", "triple")
# X6: the gNB's main worker pool size, `expert_execution.threads.main_pool.nof_threads`
# (apps/services/worker_manager/worker_manager_cli11_schema.cpp). OCUDU sizes the
# pool as min(cells*(dl_ant+ul_ant+1)+2, avail_cpus-3) = min(5, avail_cpus-3)
# (worker_manager.cpp get_default_nof_workers), so the GB10 pin to five cores
# (platform-profiles.json gnb 5-9) leaves TWO workers. In ZMQ mode every DU-high
# cell/UE executor is a `sync` strand over that pool (du_high_executor_mapper.cpp
# is_sync = !rt_mode), and the gdb dump of the 2026-10-01 wedge (run
# 20261001T115125Z) shows both workers parked in sync_task_executor::defer (RLC
# buffer-state update from an F1-U DL PDU; a timer handler) while phy_worker waits
# in sync_task_executor::execute for the slot indication: the pool starves itself
# and the lock-step relay stops. Unset = fixture untouched; a positive integer is
# written as nof_threads (5 restores the unpinned default).
MAIN_POOL_THREADS_ENV = "OCUDU_NATIVE_GNB_MAIN_POOL_THREADS"

# One entry per UE. The ZMQ port pairs and the broker device ids must agree with
# use_cases/configs/topologies/ocudu_docker/topology.ocudu-docker.multi-ue.cuda.yaml; the IPv4 addresses must
# agree with use_cases/configs/ran/open5gs/subscriber-multi-ue.csv. Each UE gets its
# own network namespace because srsUE moves its TUN device into the namespace
# named in [gw], and two UEs sharing one namespace would collide on
# ip_devname.
UES = (
    {
        "device_id": "ue0",
        "tx_port": 2101,
        "rx_port": 2100,
        "imsi": "001010123456780",
        "imei": "353490069873319",
        "netns": "ue1",
        "ipv4": "10.45.1.2",
    },
    {
        "device_id": "ue1",
        "tx_port": 2103,
        "rx_port": 2102,
        "imsi": "001010123456781",
        "imei": "353490069873320",
        "netns": "ue2",
        "ipv4": "10.45.1.3",
    },
    # ue2/ue3 are used only with --ue-count 4 (topology.ocudu-docker.multi-ue-quad).
    {
        "device_id": "ue2",
        "tx_port": 2105,
        "rx_port": 2104,
        "imsi": "001010123456782",
        "imei": "353490069873321",
        "netns": "ue3",
        "ipv4": "10.45.1.4",
    },
    {
        "device_id": "ue3",
        "tx_port": 2107,
        "rx_port": 2106,
        "imsi": "001010123456783",
        "imei": "353490069873322",
        "netns": "ue4",
        "ipv4": "10.45.1.5",
    },
)
# Per UE count: the topology, the subscriber fixture and the per-UE channel
# models the gate asserts (each UE on its own channel, so a per-UE regression
# cannot hide behind a shared one).
LAYOUTS = {
    2: ("use_cases/configs/topologies/ocudu_docker/topology.ocudu-docker.multi-ue.cuda.yaml",
        "use_cases/configs/ran/open5gs/subscriber-multi-ue.csv", ("near", "far")),
    4: ("use_cases/configs/topologies/ocudu_docker/topology.ocudu-docker.multi-ue-quad.cuda.yaml",
        "use_cases/configs/ran/open5gs/subscriber-multi-ue-quad.csv", ("near", "mid1", "mid2", "far")),
}


def fail(message: str) -> "NoReturn":
    print(f"config rendering failed: {message}", file=sys.stderr)
    raise SystemExit(2)


def replace_exact(text: str, old: str, new: str, count: int, label: str) -> str:
    found = text.count(old)
    if found != count:
        fail(f"{label}: expected {count} occurrence(s) of {old!r}, found {found}")
    return text.replace(old, new)


def insert_before_exact(text: str, marker: str, insertion: str, label: str) -> str:
    if text.count(marker) != 1:
        fail(f"{label}: marker {marker!r} is missing or ambiguous")
    return text.replace(marker, insertion + marker)


def read_regular(path: Path, label: str) -> str:
    if path.is_symlink() or not path.is_file():
        fail(f"{label} is missing or is not a regular non-symlink file: {path}")
    return path.read_text(encoding="utf-8")


def safe_directory(path: Path, label: str) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_dir() or path.is_symlink():
        fail(f"{label} is not a usable directory: {path}")
    return resolved


def write_new(path: Path, text: str) -> None:
    with path.open("x", encoding="utf-8") as handle:
        handle.write(text)


def expert_execution_block() -> str:
    """The expert_execution section the X6 environment knobs ask for.

    Empty when neither OCUDU_NATIVE_GNB_LOWER_PHY_PROFILE nor
    OCUDU_NATIVE_GNB_MAIN_POOL_THREADS is set, so the default render is
    byte-identical to what it always was.
    """
    profile = os.environ.get(LOWER_PHY_PROFILE_ENV, "")
    pool = os.environ.get(MAIN_POOL_THREADS_ENV, "")
    if not profile and not pool:
        return ""
    lines = ["", "# X6 knobs (OCUDU_NATIVE_GNB_LOWER_PHY_PROFILE / OCUDU_NATIVE_GNB_MAIN_POOL_THREADS).",
             "expert_execution:", "  threads:"]
    if profile:
        if profile not in LOWER_PHY_PROFILES:
            fail(f"{LOWER_PHY_PROFILE_ENV} must be one of {LOWER_PHY_PROFILES}, got {profile!r}")
        lines += ["    lower_phy:", f"      execution_profile: {profile}"]
    if pool:
        if not pool.isdigit() or int(pool) < 1 or int(pool) > 64:
            fail(f"{MAIN_POOL_THREADS_ENV} must be an integer in 1..64, got {pool!r}")
        lines += ["    main_pool:", f"      nof_threads: {int(pool)}"]
    return "\n".join(lines) + "\n"


def render_gnb(source: str, log_dir: Path) -> str:
    """Identical to the 1x1 gNB rendering, plus the optional X6 expert_execution knobs.

    One cell serves both UEs, so nothing about the gNB changes with UE count.
    """
    rendered = replace_exact(
        source, "    addrs: 10.53.1.2\n", "    addrs: 127.0.0.2\n", 1, "gNB AMF address"
    )
    rendered = replace_exact(
        rendered,
        "    bind_addrs: 10.53.1.1\n",
        "    bind_addrs: 127.0.0.1\n",
        1,
        "gNB NGAP bind address",
    )
    rendered = insert_before_exact(
        rendered,
        "ru_sdr:\n",
        "cu_up:\n  ngu:\n    socket:\n      - bind_addr: 127.0.0.1\n\n",
        "gNB explicit N3 bind insertion",
    )
    rendered = replace_exact(
        rendered,
        "  device_args: tx_port=tcp://*:2000,rx_port=tcp://host.docker.internal:2001,base_srate=23.04e6\n",
        "  device_args: tx_port=tcp://127.0.0.1:2000,rx_port=tcp://127.0.0.1:2001,base_srate=23.04e6\n",
        1,
        "gNB ZMQ loopback endpoints",
    )
    rendered = replace_exact(rendered, "  tx_gain: 75\n", "  tx_gain: 0\n", 1, "native gNB TX gain")
    rendered = replace_exact(rendered, "  rx_gain: 75\n", "  rx_gain: 0\n", 1, "native gNB RX gain")
    for old, new in {
        "  filename: /tmp/gnb.log\n": f"  filename: {log_dir / 'gnb-internal.log'}\n",
        "  mac_filename: /tmp/gnb_mac.pcap\n": f"  mac_filename: {log_dir / 'gnb_mac.pcap'}\n",
        "  ngap_filename: /tmp/gnb_ngap.pcap\n": f"  ngap_filename: {log_dir / 'gnb_ngap.pcap'}\n",
    }.items():
        rendered = replace_exact(rendered, old, new, 1, "gNB run artifact path")
    block = expert_execution_block()
    if block:
        if "expert_execution:" in rendered:
            fail("gNB fixture already carries an expert_execution section; reconcile explicitly")
        rendered += block
    return rendered


def render_topology(source: str, ues, models) -> str:
    """Bind every broker REP socket to loopback and assert the multi-UE shape.

    The published topology binds `tcp://*` so a container peer can reach the
    broker over the bridge gateway. Native peers are all in this namespace, so
    loopback is both sufficient and tighter.
    """
    rendered = source
    for port in (2001, *(ue["rx_port"] for ue in ues)):
        rendered = replace_exact(
            rendered,
            f"    rx_endpoint: tcp://*:{port}\n",
            f"    rx_endpoint: tcp://127.0.0.1:{port}\n",
            1,
            f"broker REP loopback endpoint :{port}",
        )
    # The near/far asymmetry is the point of this gate: the two UEs must not be
    # served by the same channel, or a per-UE regression could hide.
    required_links = []
    for ue, model in zip(ues, models):
        required_links.append(f"  - from: gnb0\n    to: {ue['device_id']}\n    model: {model}\n")
        required_links.append(f"  - from: {ue['device_id']}\n    to: gnb0\n    model: {model}\n")
    for required in required_links:
        if source.count(required) != 1:
            fail(f"multi-UE topology invariant is missing or ambiguous: {required!r}")
    return rendered


def render_open5gs(source: str, native_root: Path) -> str:
    """Identical to the 1x1 Open5GS rendering."""
    expected_counts = {
        "MONGODB_IP": 1,
        "MCC": 6,
        "MNC": 6,
        "OPEN5GS_IP": 4,
        "NETWORK_NAME_FULL": 2,
        "NETWORK_NAME_SHORT": 2,
        "UE_IP_RANGE": 2,
        "UE_GATEWAY_IP": 2,
        "UPF_ADVERTISE_IP": 1,
    }
    values = {
        "MONGODB_IP": "127.0.0.1",
        "MCC": "001",
        "MNC": "01",
        "OPEN5GS_IP": "127.0.0.2",
        "NETWORK_NAME_FULL": "OCUDU",
        "NETWORK_NAME_SHORT": "OCUDU",
        "UE_IP_RANGE": "10.45.0.0/24",
        "UE_GATEWAY_IP": "10.45.0.1",
        "UPF_ADVERTISE_IP": "127.0.0.2",
    }
    rendered = source
    for name, expected_count in expected_counts.items():
        rendered = replace_exact(
            rendered, "${" + name + "}", values[name], expected_count, f"Open5GS {name}"
        )

    extension_root = native_root / "builds/open5gs-v2.7.6/subprojects/freeDiameter/extensions"
    modules = {
        "dbg_msg_dumps.fdx": extension_root / "dbg_msg_dumps.fdx",
        "dict_rfc5777.fdx": extension_root / "dict_rfc5777.fdx",
        "dict_mip6i.fdx": extension_root / "dict_mip6i.fdx",
        "dict_nasreq.fdx": extension_root / "dict_nasreq.fdx",
        "dict_nas_mipv6.fdx": extension_root / "dict_nas_mipv6.fdx",
        "dict_dcca.fdx": extension_root / "dict_dcca.fdx",
        "dict_dcca_3gpp.fdx": extension_root / "dict_dcca_3gpp/dict_dcca_3gpp.fdx",
    }
    for name, path in modules.items():
        if path.is_symlink() or not path.is_file():
            fail(f"required freeDiameter module is missing or symlinked: {path}")
        rendered = replace_exact(
            rendered,
            f'"/open5gs/install/lib/${{INSTALL_ARCH}}/freeDiameter/{name}"',
            json.dumps(str(path)),
            4,
            f"freeDiameter {name}",
        )

    unresolved = PLACEHOLDER_RE.findall(rendered)
    if unresolved:
        fail(f"unresolved Open5GS placeholders: {sorted(set(unresolved))}")
    return rendered


def render_srsue(source: str, ue: dict, log_dir: Path) -> str:
    for token in ("@UE_TX_PORT@", "@UE_RX_PORT@", "@UE_IMSI@", "@UE_IMEI@", "@UE_NETNS@", "@SRSUE_LOG@"):
        if source.count(token) != 1:
            fail(f"srsUE template token missing or ambiguous: {token}")
    if source.count("nof_antennas = 1\n") != 1:
        fail("srsUE template must pin a single antenna")
    rendered = source
    for token, value in (
        ("@UE_TX_PORT@", str(ue["tx_port"])),
        ("@UE_RX_PORT@", str(ue["rx_port"])),
        ("@UE_IMSI@", ue["imsi"]),
        ("@UE_IMEI@", ue["imei"]),
        ("@UE_NETNS@", ue["netns"]),
        ("@SRSUE_LOG@", str(log_dir / f"srsue-{ue['device_id']}-internal.log")),
    ):
        rendered = replace_exact(rendered, token, value, 1, f"srsUE {token}")
    if PLACEHOLDER_RE.search(rendered):
        fail("unresolved srsUE placeholder")
    return rendered


def validate_subscriber(source: str, UES) -> str:
    """Require exactly one record per configured UE, matching its IMSI and IP.

    The 1x1 renderer pins the CSV byte-for-byte. That is not portable to N UEs,
    so this validates the fields that the gate actually depends on instead: one
    record per UE, in order, with the IMSI the srsUE config will present and the
    IPv4 the ping target arithmetic assumes.
    """
    records = [line for line in source.splitlines() if line and not line.startswith("#")]
    if len(records) != len(UES):
        fail(f"subscriber CSV must carry exactly {len(UES)} records, found {len(records)}")
    for record, ue in zip(records, UES):
        fields = record.split(",")
        if len(fields) != 8:
            fail(f"subscriber record is not eight columns: {record!r}")
        if fields[1] != ue["imsi"]:
            fail(f"subscriber IMSI {fields[1]} does not match {ue['device_id']} ({ue['imsi']})")
        if fields[7] != ue["ipv4"]:
            fail(f"subscriber IPv4 {fields[7]} does not match {ue['device_id']} ({ue['ipv4']})")
    return source


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--native-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--ue-count", type=int, choices=sorted(LAYOUTS), default=2)
    args = parser.parse_args()
    topology_path, subscriber_path, models = LAYOUTS[args.ue_count]
    ues = UES[: args.ue_count]

    repo_root = args.repo_root.resolve(strict=True)
    native_root = args.native_root.resolve(strict=True)
    output_dir = safe_directory(args.output_dir, "output directory")
    log_dir = safe_directory(args.log_dir, "log directory")

    gnb_source = read_regular(
        repo_root / "use_cases/configs/ran/ocudu/docker/gnb_zmq_b210_fdd_srsue.yaml", "gNB fixture"
    )
    topology_source = read_regular(
        repo_root / topology_path, "multi-UE topology"
    )
    open5gs_source = read_regular(
        native_root / "src/ocudu/docker/open5gs/open5gs-5gc.yml", "pinned OCUDU Open5GS template"
    )
    srsue_source = read_regular(
        repo_root / "use_cases/configs/ran/srsue/srsue_zmq_multi_ue.conf.in", "srsUE template"
    )
    subscriber_source = read_regular(
        repo_root / subscriber_path, "subscriber fixture"
    )

    outputs = {
        "gnb.yaml": render_gnb(gnb_source, log_dir),
        "topology.yaml": render_topology(topology_source, ues, models),
        "open5gs.yaml": render_open5gs(open5gs_source, native_root),
        "subscriber.csv": validate_subscriber(subscriber_source, ues),
    }
    for ue in ues:
        outputs[f"srsue-{ue['device_id']}.conf"] = render_srsue(srsue_source, ue, log_dir)

    for name, text in outputs.items():
        write_new(output_dir / name, text)
    print(f"rendered={' '.join(sorted(outputs))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
