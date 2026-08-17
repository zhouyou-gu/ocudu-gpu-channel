#!/usr/bin/env python3
"""Render configs for the native rank-1 2x1/1x2 live gate (R2).

Open5GS, srsUE, and the subscriber render from the SAME immutable legacy
fixtures through the SAME functions as the 1x1 gate (imported from
render-legacy-1x1-configs.py), so the srsUE side of this gate is
byte-identical to the regression net. What changes is the gNB (the proven
2T2R rank-1 fixture, log paths injected here) and the broker topology (the
asymmetric 2x1/1x2 fixture, already loopback-addressed).
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

sys.dont_write_bytecode = True


def load_legacy_renderer():
    path = Path(__file__).resolve().with_name("render-legacy-1x1-configs.py")
    spec = importlib.util.spec_from_file_location("render_legacy_1x1_configs", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load legacy renderer module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


legacy = load_legacy_renderer()


def render_gnb_2t2r(source: str, log_dir: Path) -> str:
    # The three R2 adaptations are load-bearing (see the fixture header);
    # requiring their exact tokens keeps a drifted fixture from silently
    # running a different experiment.
    invariants = {
        "  device_args: tx_port0=tcp://127.0.0.1:2000,tx_port1=tcp://127.0.0.1:2002,rx_port0=tcp://127.0.0.1:2001,rx_port1=tcp://127.0.0.1:2003,base_srate=23.04e6\n": 1,
        "  nof_antennas_dl: 2\n": 1,
        "  nof_antennas_ul: 2\n": 1,
        "      ss2_type: ue_dedicated\n": 1,
        "      dci_format_0_1_and_1_1: true\n": 1,
        "    csi_rs_enabled: false\n": 1,
        "    nof_cell_csi_res: 0\n": 1,
        "  mac_enable: disable\n": 1,
    }
    for token, count in invariants.items():
        if source.count(token) != count:
            legacy.fail(f"2T2R rank-1 gNB invariant missing or ambiguous: {token!r}")
    rendered = legacy.replace_exact(
        source, "@GNB_LOG@", str(log_dir / "gnb-internal.log"), 1, "gNB log path"
    )
    rendered = legacy.replace_exact(
        rendered, "@GNB_MAC_PCAP@", str(log_dir / "gnb_mac.pcap"), 1, "gNB MAC pcap path"
    )
    rendered = legacy.replace_exact(
        rendered, "@GNB_NGAP_PCAP@", str(log_dir / "gnb_ngap.pcap"), 1, "gNB NGAP pcap path"
    )
    if legacy.PLACEHOLDER_RE.search(rendered):
        legacy.fail("unresolved gNB placeholder")
    return rendered


def validate_topology_rank1(source: str) -> str:
    # The asymmetric fixture is already loopback-addressed; validate the
    # rank-1 shape rather than rewriting anything.
    invariants = (
        "    tx_ports:\n      - gnb0_p0\n      - gnb0_p1\n",
        "    tx_ports:\n      - ue0_p0\n",
        "  - from: gnb0\n    to: ue0\n    model: dl_miso_2x1\n",
        "  - from: ue0\n    to: gnb0\n    model: ul_simo_1x2\n",
    )
    for token in invariants:
        if source.count(token) != 1:
            legacy.fail(f"rank-1 topology invariant missing or ambiguous: {token!r}")
    if source.count("rx: 0") != 3 or source.count("rx: 1") != 1:
        legacy.fail("rank-1 topology coefficient shape is not 1x2 DL + 2x1 UL")
    return source


def self_test() -> None:
    sample = (
        "  device_args: tx_port0=tcp://127.0.0.1:2000,tx_port1=tcp://127.0.0.1:2002,rx_port0=tcp://127.0.0.1:2001,rx_port1=tcp://127.0.0.1:2003,base_srate=23.04e6\n"
        "  nof_antennas_dl: 2\n  nof_antennas_ul: 2\n"
        "      ss2_type: ue_dedicated\n      dci_format_0_1_and_1_1: true\n"
        "    csi_rs_enabled: false\n    nof_cell_csi_res: 0\n"
        "  mac_enable: disable\n"
        "  filename: @GNB_LOG@\n  mac_filename: @GNB_MAC_PCAP@\n"
        "  ngap_filename: @GNB_NGAP_PCAP@\n"
    )
    rendered = render_gnb_2t2r(sample, Path("/tmp/x"))
    assert "@GNB" not in rendered
    try:
        validate_topology_rank1("nothing")
    except ValueError:
        pass
    else:
        raise AssertionError("validate_topology_rank1 did not fail closed")
    print("event=native_rank1_config_renderer_self_test result=pass")


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
    output_dir = legacy.safe_output_directory(args.output_dir)
    log_dir = legacy.safe_log_directory(args.log_dir)
    if repo_root != args.repo_root or native_root != args.native_root:
        legacy.fail("repo and native roots must already be canonical")

    gnb_source = legacy.read_regular(
        repo_root / "examples/native/ocudu/gnb_zmq_b210_fdd_2t2r_rank1_srsue.yaml",
        "rank-1 2T2R gNB fixture",
    )
    topology_source = legacy.read_regular(
        repo_root / "examples/native/topology.ocudu.rank1-2x1.cuda.yaml",
        "rank-1 asymmetric topology fixture",
    )
    open5gs_source = legacy.read_regular(
        native_root / "src/ocudu/docker/open5gs/open5gs-5gc.yml",
        "pinned OCUDU Open5GS template",
    )
    srsue_source = legacy.read_regular(
        repo_root / "examples/native/srsran/srsue_zmq_legacy_1x1.conf.in",
        "native srsUE template",
    )
    subscriber_source = legacy.read_regular(
        repo_root / "examples/native/open5gs/subscriber-legacy-1x1.csv",
        "native subscriber template",
    )

    rendered = {
        "gnb.yaml": render_gnb_2t2r(gnb_source, log_dir),
        "topology.yaml": validate_topology_rank1(topology_source),
        "open5gs.yaml": legacy.render_open5gs(open5gs_source, native_root),
        "srsue.conf": legacy.render_srsue(srsue_source, log_dir),
        "subscriber.csv": legacy.validate_subscriber(subscriber_source),
    }
    for name, text in rendered.items():
        if legacy.PLACEHOLDER_RE.search(text):
            legacy.fail(f"unresolved placeholder in {name}")
        legacy.write_new(output_dir / name, text)
    print(f'event=native_rank1_configs_rendered output_dir="{output_dir}"')
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnicodeError, ValueError) as error:
        print(f"config rendering failed: {error}", file=sys.stderr)
        raise SystemExit(2)
