#!/usr/bin/env python3
"""Render configs for the native OAI nrUE 1x1 attach gate (M6.2).

The gNB, Open5GS, and subscriber fixtures are the SAME immutable legacy
fixtures the srsUE gate renders -- byte-identical inputs, the same render
functions, imported from render-legacy-1x1-configs.py -- so the only variable
this gate changes against the legacy gate is the UE process.

The topology differs from the legacy render in exactly one place: the UE-side
broker endpoints move from loopback to the run's veth pair, because the OAI
nrUE runs entirely inside the nested ue1 network namespace (it has no netns
config option the way srsUE does, so the process itself is isolated and the
ZMQ path crosses the veth).
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

# Importing the legacy renderer must not drop a __pycache__ into scripts/,
# for the same reason the gates set PYTHONDONTWRITEBYTECODE on add_users: a
# bytecode cache makes the tree dirty and pollutes the channel manifest.
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

# The veth layout is part of the gate contract: the run script creates the
# pair with exactly these addresses and the inner runner points the OAI ZMQ
# channels at them. 10.201.0.0/30 collides with nothing else in the harness
# (Open5GS pools live in 10.45.0.0/16).
VETH_HOST_IP = "10.201.0.1"
VETH_UE_IP = "10.201.0.2"


def render_gnb_oai(source: str, log_dir: Path) -> str:
    """Legacy gNB render plus one OAI-specific, measured adaptation.

    The immutable fixture carries `ss2_type: common` + `dci_format_0_1_and_1_1:
    false`, an override that exists FOR srsUE (its NR PDCCH path cannot use a
    UE-specific search space). The OAI nrUE is deaf to it: measured on the
    direct (no-broker) control, the UE decoded RAR and RRCSetup on SS#1, then
    after applying CellGroupConfig never received another DCI -- the gNB
    retransmitted the first UL grant into epre=-inf silence until RLF.
    Removing the override (OCUDU's default: UE-dedicated search space with DCI
    0_1/1_1) plus the UE capability file made the same control attach with 0
    UL CRC KOs. The rest of the cell identity is untouched.
    """
    rendered = legacy.render_gnb(source, log_dir)
    return legacy.replace_exact(
        rendered,
        "  pdcch:\n"
        "    dedicated:\n"
        "      ss2_type: common\n"
        "      dci_format_0_1_and_1_1: false\n"
        "    common:\n"
        "      ss0_index: 0\n"
        "      coreset0_index: 12\n",
        "  pdcch:\n"
        "    common:\n"
        "      ss0_index: 0\n"
        "      coreset0_index: 12\n",
        1,
        "srsUE-only dedicated PDCCH override removal for the OAI UE",
    )


def render_topology_oai(source: str) -> str:
    rendered = legacy.replace_exact(
        source,
        "    rx_endpoint: tcp://*:2001\n",
        "    rx_endpoint: tcp://127.0.0.1:2001\n",
        1,
        "gNB broker REP loopback endpoint",
    )
    rendered = legacy.replace_exact(
        rendered,
        "    tx_endpoint: tcp://127.0.0.1:2101\n",
        f"    tx_endpoint: tcp://{VETH_UE_IP}:2101\n",
        1,
        "UE broker REQ veth endpoint",
    )
    rendered = legacy.replace_exact(
        rendered,
        "    rx_endpoint: tcp://*:2100\n",
        f"    rx_endpoint: tcp://{VETH_HOST_IP}:2100\n",
        1,
        "UE broker REP veth endpoint",
    )
    # The channel model must stay byte-identical to the legacy gate: the UE
    # swap is the only variable M6.2 is allowed to change.
    for required in (
        "      - type: tdl\n",
        "      - type: phase\n",
        "      - type: cfo\n",
        "        cfo_hz: 125\n",
        "  - from: gnb0\n    to: ue0\n    model: cuda_mvp\n",
        "  - from: ue0\n    to: gnb0\n    model: cuda_mvp\n",
    ):
        if source.count(required) != 1:
            legacy.fail(f"legacy topology invariant is missing or ambiguous: {required!r}")
    return rendered


def validate_nrue(source: str) -> str:
    invariants = {
        'imsi = "001010123456780";\n': 1,
        'key = "00112233445566778899aabbccddeeff";\n': 1,
        'opc = "63bfa50ee6523365ff14c1f45f88737d";\n': 1,
        'pdu_sessions = ({ dnn = "internet"; nssai_sst = 1; });\n': 1,
    }
    for token, count in invariants.items():
        if source.count(token) != count:
            legacy.fail(f"OAI nrUE invariant missing or ambiguous: {token!r}")
    if legacy.PLACEHOLDER_RE.search(source):
        legacy.fail("unresolved OAI nrUE placeholder")
    return source


def self_test() -> None:
    sample = (
        "    rx_endpoint: tcp://*:2001\n"
        "    tx_endpoint: tcp://127.0.0.1:2101\n"
        "    rx_endpoint: tcp://*:2100\n"
        "      - type: tdl\n"
        "      - type: phase\n"
        "      - type: cfo\n"
        "        cfo_hz: 125\n"
        "  - from: gnb0\n    to: ue0\n    model: cuda_mvp\n"
        "  - from: ue0\n    to: gnb0\n    model: cuda_mvp\n"
    )
    rendered = render_topology_oai(sample)
    assert f"tcp://{VETH_UE_IP}:2101" in rendered
    assert f"tcp://{VETH_HOST_IP}:2100" in rendered
    assert "tcp://*" not in rendered
    try:
        validate_nrue('imsi = "999999999999999";\n')
    except ValueError:
        pass
    else:
        raise AssertionError("validate_nrue did not fail closed")
    print("event=native_oai_config_renderer_self_test result=pass")


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
        repo_root / "examples/ocudu/gnb_zmq_b210_fdd_srsue.yaml",
        "immutable legacy gNB fixture",
    )
    topology_source = legacy.read_regular(
        repo_root / "examples/topology.ocudu-docker.cuda.yaml",
        "immutable legacy topology",
    )
    open5gs_source = legacy.read_regular(
        native_root / "src/ocudu/docker/open5gs/open5gs-5gc.yml",
        "pinned OCUDU Open5GS template",
    )
    nrue_source = legacy.read_regular(
        repo_root / "examples/native/oai/nrue_zmq_1x1.conf",
        "native OAI nrUE fixture",
    )
    subscriber_source = legacy.read_regular(
        repo_root / "examples/native/open5gs/subscriber-legacy-1x1.csv",
        "native subscriber template",
    )

    rendered = {
        "gnb.yaml": render_gnb_oai(gnb_source, log_dir),
        "topology.yaml": render_topology_oai(topology_source),
        "open5gs.yaml": legacy.render_open5gs(open5gs_source, native_root),
        "nrue.conf": validate_nrue(nrue_source),
        "subscriber.csv": legacy.validate_subscriber(subscriber_source),
    }
    for name, text in rendered.items():
        if legacy.PLACEHOLDER_RE.search(text):
            legacy.fail(f"unresolved placeholder in {name}")
        legacy.write_new(output_dir / name, text)
    print(f'event=native_oai_configs_rendered output_dir="{output_dir}"')
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnicodeError, ValueError) as error:
        print(f"config rendering failed: {error}", file=sys.stderr)
        raise SystemExit(2)
