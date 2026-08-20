#!/usr/bin/env python3
"""Render the immutable legacy Docker fixtures for a native loopback run.

The renderer is deliberately textual and strict: every expected source token
must occur the audited number of times, and no unresolved template token may
reach a process configuration.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


PLACEHOLDER_RE = re.compile(r"\$\{[A-Z0-9_]+\}|@[A-Z0-9_]+@")


def fail(message: str) -> "NoReturn":
    raise ValueError(message)


def replace_exact(text: str, old: str, new: str, count: int, label: str) -> str:
    actual = text.count(old)
    if actual != count:
        fail(f"{label}: expected {count} source token(s), found {actual}")
    rendered = text.replace(old, new)
    if rendered.count(old) != 0:
        fail(f"{label}: source token survived replacement")
    return rendered


def insert_before_exact(text: str, marker: str, insertion: str, label: str) -> str:
    actual = text.count(marker)
    if actual != 1:
        fail(f"{label}: expected one insertion marker, found {actual}")
    before, after = text.split(marker, 1)
    return before + insertion + marker + after


def read_regular(path: Path, label: str) -> str:
    if path.is_symlink() or not path.is_file():
        fail(f"{label} is missing or is not a regular non-symlink file: {path}")
    return path.read_text(encoding="utf-8", errors="strict")


def safe_output_directory(path: Path) -> Path:
    if not path.is_absolute() or path == Path("/"):
        fail("output directory must be an absolute dedicated directory")
    if path.is_symlink() or not path.is_dir():
        fail(f"output directory is missing or symlinked: {path}")
    resolved = path.resolve(strict=True)
    if resolved != path:
        fail("output directory must already be canonical")
    if any(path.iterdir()):
        fail("output directory must be empty")
    return path


def safe_log_directory(path: Path) -> Path:
    if not path.is_absolute() or path == Path("/"):
        fail("log directory must be an absolute dedicated directory")
    if path.is_symlink() or not path.is_dir():
        fail(f"log directory is missing or symlinked: {path}")
    resolved = path.resolve(strict=True)
    if resolved != path:
        fail("log directory must already be canonical")
    return path


def write_new(path: Path, text: str) -> None:
    if path.exists() or path.is_symlink():
        fail(f"refusing to replace rendered output: {path}")
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def render_gnb(source: str, log_dir: Path) -> str:
    rendered = source
    rendered = replace_exact(
        rendered,
        "    addrs: 10.53.1.2\n",
        "    addrs: 127.0.0.2\n",
        1,
        "gNB AMF address",
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
        "cu_up:\n"
        "  ngu:\n"
        "    socket:\n"
        "      - bind_addr: 127.0.0.1\n"
        "\n",
        "gNB explicit N3 bind insertion",
    )
    rendered = replace_exact(
        rendered,
        "  device_args: tx_port=tcp://*:2000,rx_port=tcp://host.docker.internal:2001,base_srate=23.04e6\n",
        "  device_args: tx_port=tcp://127.0.0.1:2000,rx_port=tcp://127.0.0.1:2001,base_srate=23.04e6\n",
        1,
        "gNB ZMQ loopback endpoints",
    )
    rendered = replace_exact(
        rendered, "  tx_gain: 75\n", "  tx_gain: 0\n", 1, "native gNB TX gain"
    )
    rendered = replace_exact(
        rendered, "  rx_gain: 75\n", "  rx_gain: 0\n", 1, "native gNB RX gain"
    )
    replacements = {
        "  filename: /tmp/gnb.log\n": f"  filename: {log_dir / 'gnb-internal.log'}\n",
        "  mac_filename: /tmp/gnb_mac.pcap\n": f"  mac_filename: {log_dir / 'gnb_mac.pcap'}\n",
        "  ngap_filename: /tmp/gnb_ngap.pcap\n": f"  ngap_filename: {log_dir / 'gnb_ngap.pcap'}\n",
    }
    for old, new in replacements.items():
        rendered = replace_exact(rendered, old, new, 1, "gNB run artifact path")
    return rendered


def render_topology(source: str) -> str:
    rendered = replace_exact(
        source,
        "    rx_endpoint: tcp://*:2001\n",
        "    rx_endpoint: tcp://127.0.0.1:2001\n",
        1,
        "gNB broker REP loopback endpoint",
    )
    rendered = replace_exact(
        rendered,
        "    rx_endpoint: tcp://*:2100\n",
        "    rx_endpoint: tcp://127.0.0.1:2100\n",
        1,
        "UE broker REP loopback endpoint",
    )
    for required in (
        "      - type: tdl\n",
        "      - type: phase\n",
        "      - type: cfo\n",
        "        cfo_hz: 125\n",
        "  - from: gnb0\n    to: ue0\n    model: cuda_mvp\n",
        "  - from: ue0\n    to: gnb0\n    model: cuda_mvp\n",
    ):
        if source.count(required) != 1:
            fail(f"legacy topology invariant is missing or ambiguous: {required!r}")
    return rendered


def render_open5gs(source: str, native_root: Path) -> str:
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
        token = "${" + name + "}"
        rendered = replace_exact(
            rendered, token, values[name], expected_count, f"Open5GS {name}"
        )

    extension_root = (
        native_root
        / "builds/open5gs-v2.7.6/subprojects/freeDiameter/extensions"
    )
    modules = {
        "dbg_msg_dumps.fdx": extension_root / "dbg_msg_dumps.fdx",
        "dict_rfc5777.fdx": extension_root / "dict_rfc5777.fdx",
        "dict_mip6i.fdx": extension_root / "dict_mip6i.fdx",
        "dict_nasreq.fdx": extension_root / "dict_nasreq.fdx",
        "dict_nas_mipv6.fdx": extension_root / "dict_nas_mipv6.fdx",
        "dict_dcca.fdx": extension_root / "dict_dcca.fdx",
        "dict_dcca_3gpp.fdx": extension_root
        / "dict_dcca_3gpp/dict_dcca_3gpp.fdx",
    }
    for name, path in modules.items():
        if path.is_symlink() or not path.is_file():
            fail(f"required freeDiameter module is missing or symlinked: {path}")
        old = f'"/open5gs/install/lib/${{INSTALL_ARCH}}/freeDiameter/{name}"'
        rendered = replace_exact(
            rendered, old, json.dumps(str(path)), 4, f"freeDiameter {name}"
        )

    unresolved = PLACEHOLDER_RE.findall(rendered)
    if unresolved:
        fail(f"unresolved Open5GS placeholders: {sorted(set(unresolved))}")
    return rendered


def render_srsue(source: str, log_dir: Path) -> str:
    invariants = {
        "device_args = tx_port=tcp://127.0.0.1:2101,rx_port=tcp://127.0.0.1:2100,base_srate=23.04e6\n": 1,
        "nof_antennas = 1\n": 1,
        "opc = 63BFA50EE6523365FF14C1F45F88737D\n": 1,
        "k = 00112233445566778899AABBCCDDEEFF\n": 1,
        "imsi = 001010123456780\n": 1,
        "netns = ue1\n": 1,
        "ip_devname = tun_srsue\n": 1,
        "@SRSUE_LOG@": 1,
    }
    for token, count in invariants.items():
        if source.count(token) != count:
            fail(f"srsUE invariant missing or ambiguous: {token!r}")
    rendered = replace_exact(
        source,
        "@SRSUE_LOG@",
        str(log_dir / "srsue-internal.log"),
        1,
        "srsUE log path",
    )
    if PLACEHOLDER_RE.search(rendered):
        fail("unresolved srsUE placeholder")
    return rendered


def validate_subscriber(source: str) -> str:
    expected = (
        "# name,imsi,key,op_type,op_or_opc,amf,qci,ipv4\n"
        "legacy-ue,001010123456780,00112233445566778899aabbccddeeff,"
        "opc,63bfa50ee6523365ff14c1f45f88737d,8000,9,10.45.1.2\n"
    )
    if source != expected:
        fail("subscriber CSV differs from the single audited legacy UE record")
    non_comments = [line for line in source.splitlines() if not line.startswith("#")]
    if non_comments != [expected.splitlines()[1]]:
        fail("subscriber CSV must contain exactly one non-comment record")
    return source


def self_test() -> None:
    sample = "one token two token"
    assert replace_exact(sample, "token", "value", 2, "self-test") == (
        "one value two value"
    )
    try:
        replace_exact(sample, "token", "value", 1, "self-test-negative")
    except ValueError:
        pass
    else:
        raise AssertionError("replace_exact did not fail closed")
    print("event=native_legacy_config_renderer_self_test result=pass")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--native-root", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--log-dir", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        if any((args.repo_root, args.native_root, args.output_dir, args.log_dir)):
            parser.error("--self-test cannot be combined with render arguments")
        self_test()
        return 0
    if not all((args.repo_root, args.native_root, args.output_dir, args.log_dir)):
        parser.error("render mode requires all path arguments")

    repo_root = args.repo_root.resolve(strict=True)
    native_root = args.native_root.resolve(strict=True)
    output_dir = safe_output_directory(args.output_dir)
    log_dir = safe_log_directory(args.log_dir)
    if repo_root != args.repo_root or native_root != args.native_root:
        fail("repo and native roots must already be canonical")

    gnb_source = read_regular(
        repo_root / "examples/ocudu/gnb_zmq_b210_fdd_srsue.yaml",
        "immutable legacy gNB fixture",
    )
    topology_source = read_regular(
        repo_root / "examples/topology.ocudu-docker.cuda.yaml",
        "immutable legacy topology",
    )
    open5gs_source = read_regular(
        native_root / "src/ocudu/docker/open5gs/open5gs-5gc.yml",
        "pinned OCUDU Open5GS template",
    )
    srsue_source = read_regular(
        repo_root / "examples/native/srsran/srsue_zmq_legacy_1x1.conf.in",
        "native srsUE template",
    )
    subscriber_source = read_regular(
        repo_root / "examples/native/open5gs/subscriber-legacy-1x1.csv",
        "native subscriber template",
    )

    rendered = {
        "gnb.yaml": render_gnb(gnb_source, log_dir),
        "topology.yaml": render_topology(topology_source),
        "open5gs.yaml": render_open5gs(open5gs_source, native_root),
        "srsue.conf": render_srsue(srsue_source, log_dir),
        "subscriber.csv": validate_subscriber(subscriber_source),
    }
    for name, text in rendered.items():
        if PLACEHOLDER_RE.search(text):
            fail(f"unresolved placeholder in {name}")
        write_new(output_dir / name, text)
    print(f'event=native_legacy_configs_rendered output_dir="{output_dir}"')
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnicodeError, ValueError) as error:
        print(f"config rendering failed: {error}", file=sys.stderr)
        raise SystemExit(2)
