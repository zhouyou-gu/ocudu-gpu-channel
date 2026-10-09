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
import math
import os
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

# Wire levels, mean |x|^2 over the *active* samples (idle samples excluded,
# scripts/native/wire-capture-power.py) of broker wire captures of the pinned
# fixtures. The ZMQ radios transmit at arbitrary software scales: the OCUDU
# gNB (gnb_zmq_b210_fdd_srsue.yaml, default amplitude control) puts 1.12e-2
# (-19.5 dB, peak amplitude 0.39) on the wire -- the constant the Sionna
# multi-UE renderer carries as TX_POWER_DL, re-measured at 1.11e-2 in the
# captures below. The OAI nrUE (nrue_zmq_1x1.conf, the patched ZMQ module,
# which converts its int16 samples by 1/32767) was measured on the DGX Spark
# with OCUDU_NATIVE_OAI1X1_WIRE_CAPTURE_SAMPLES=69120000 / _SKIP_SECONDS=2
# (results/logs/oai-1x1/20261001T110037Z): a 3 s window over the gate's own
# connected-mode traffic, i.e. the three pings (six 14-symbol 64-PRB QPSK
# PUSCH bursts at -42.1 dB, peak amplitude 0.028) plus PUCCH (1-PRB format 1
# and the 20 ms CSI/SR occasions, -56 dB, peak 0.003). The mixed active mean
# is 1.17e-5 (-49.3 dB). OAI transmits every UL channel at the fixed digital
# amplitude AMP per resource element (pucch_uci_ue_nr.c, nr_ulsch_ue.c,
# phy_procedures_nr_ue.c: no numeric power control), so the per-sample level
# follows the allocation width; a PUCCH-only window (20261001T105317Z, skip
# 8 s) sits at 2.0e-6. The constant is the traffic-weighted level, as the
# srsUE one (TX_POWER_UL, PUCCH/SRS/ping-sized PUSCH) is.
TX_POWER_DL = 1.12e-2
OAI_UE_TX_POWER = 1.17e-5
# Emitted powers: a 23 dBm UE against a 30 dBm small cell. The broker's
# per-device `tx_scale_db` puts the UE's wire level onto the gNB's scale so
# that a 0 dB tap means the same emitted power in both directions:
#   ue_tx_scale_db = (UL_dBm - DL_dBm) - 10 log10(OAI_UE_TX_POWER / TX_POWER_DL)
# The fixed channels of this gate carry no absolute noise floor (tdl / phase /
# cfo only, no awgn), so the scale changes the level the gNB receives and
# nothing else; a renderer that adds an absolute `noise_power` has to size the
# gNB's floor from the *scaled* uplink power (see rx_noise_powers in the
# Sionna renderer).
TX_POWER_DL_DBM = 30.0
TX_POWER_UL_DBM = 23.0
UL_POWER_OFFSET_DB = TX_POWER_UL_DBM - TX_POWER_DL_DBM
# Carrier labels of the band-3 FDD fixture: a port only hears its own
# carrier, so the broker refuses a UE TX -> UE RX edge instead of summing a
# physically non-existent jammer.
GNB_CARRIERS = "    tx_carrier: n3-dl\n    rx_carrier: n3-ul\n"
UE_CARRIERS = "    tx_carrier: n3-ul\n    rx_carrier: n3-dl\n"


def ue_tx_scale_db(tx_power_dl: float = TX_POWER_DL, tx_power_ul: float = OAI_UE_TX_POWER,
                   ul_power_offset_db: float = UL_POWER_OFFSET_DB) -> float:
    """Per-UE-port `tx_scale_db` that puts the uplink on the downlink's scale."""
    for label, value in (("tx_power_dl", tx_power_dl), ("tx_power_ul", tx_power_ul)):
        if not (math.isfinite(value) and value > 0.0):
            raise ValueError(f"{label} must be a positive finite power: {value}")
    if not math.isfinite(ul_power_offset_db):
        raise ValueError(f"ul_power_offset_db must be finite: {ul_power_offset_db}")
    return ul_power_offset_db - 10.0 * math.log10(tx_power_ul / tx_power_dl)


def add_tx_scale_arguments(parser: argparse.ArgumentParser) -> None:
    """The uplink-scale knobs, shared with the OAI 2x2 renderer.

    The gates call their renderer with fixed arguments, so each knob also
    reads an environment default: OCUDU_NATIVE_OAI_UE_TX_POWER (wire level),
    OCUDU_NATIVE_OAI_UE_TX_SCALE_DB (explicit dB, or `off` for no scaling --
    the topology this renderer produced before the scale existed).
    """
    parser.add_argument("--tx-power-dl", type=float, default=TX_POWER_DL,
                        help="gNB transmit power on the wire, mean |x|^2 of active samples")
    parser.add_argument("--tx-power-ul", type=float,
                        default=float(os.environ.get("OCUDU_NATIVE_OAI_UE_TX_POWER", OAI_UE_TX_POWER)),
                        help="OAI nrUE transmit power on the wire, mean |x|^2 of active samples")
    parser.add_argument("--ul-power-offset-db", type=float, default=UL_POWER_OFFSET_DB,
                        help="emitted UE power relative to the gNB, dB (default 23 dBm - 30 dBm)")
    parser.add_argument("--ue-tx-scale-db", default=os.environ.get("OCUDU_NATIVE_OAI_UE_TX_SCALE_DB"),
                        help="explicit UE tx_scale_db, or `off` for none (default: derived)")


def resolve_ue_tx_scale_db(args: argparse.Namespace) -> float | None:
    """The UE `tx_scale_db` the arguments ask for; None means no key."""
    if args.ue_tx_scale_db is not None:
        if str(args.ue_tx_scale_db).strip().lower() in ("off", "none", ""):
            return None
        try:
            value = float(args.ue_tx_scale_db)
        except ValueError:
            raise ValueError(f"--ue-tx-scale-db must be a number of dB or off: {args.ue_tx_scale_db!r}")
        if not (math.isfinite(value) and abs(value) <= 200.0):
            raise ValueError(f"--ue-tx-scale-db must be finite and within +/-200 dB: {value}")
        return value
    return ue_tx_scale_db(args.tx_power_dl, args.tx_power_ul, args.ul_power_offset_db)


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


def render_topology_oai(source: str, ue_scale_db: float | None = None) -> str:
    """Legacy topology with the OAI endpoints, carrier labels and the UE scale.

    `ue_scale_db` (see ue_tx_scale_db) becomes the UE device's `tx_scale_db`;
    None leaves the key out, which the broker treats bit-identically to 0 dB.
    The channel chain itself stays byte-identical to the legacy gate's.
    """
    if ue_scale_db is not None and not (math.isfinite(ue_scale_db) and abs(ue_scale_db) <= 200.0):
        raise ValueError(f"ue_scale_db must be finite and within +/-200 dB: {ue_scale_db}")
    ue_extra = UE_CARRIERS
    if ue_scale_db is not None:
        ue_extra += f"    tx_scale_db: {ue_scale_db:.3f}\n"
    rendered = legacy.replace_exact(
        source,
        "    rx_endpoint: tcp://*:2001\n",
        "    rx_endpoint: tcp://127.0.0.1:2001\n" + GNB_CARRIERS,
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
        f"    rx_endpoint: tcp://{VETH_HOST_IP}:2100\n" + ue_extra,
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
    # Carrier labels on both devices, in device order; no scale unless asked.
    assert rendered.count("tx_carrier:") == 2 and rendered.count("rx_carrier:") == 2, rendered
    assert rendered.index("tx_carrier: n3-dl") < rendered.index("tx_carrier: n3-ul"), rendered
    assert "    rx_endpoint: tcp://127.0.0.1:2001\n" + GNB_CARRIERS in rendered, rendered
    assert f"    rx_endpoint: tcp://{VETH_HOST_IP}:2100\n" + UE_CARRIERS in rendered, rendered
    assert "tx_scale_db" not in rendered, rendered
    # The channel chain is untouched by the labels.
    for token in ("      - type: tdl\n", "      - type: phase\n", "        cfo_hz: 125\n"):
        assert rendered.count(token) == 1, token
    # Uplink scale: the measured wire gap plus the 7 dB emitted-power difference.
    scale = ue_tx_scale_db()
    assert abs(scale - (-7.0 - 10.0 * math.log10(OAI_UE_TX_POWER / TX_POWER_DL))) < 1e-9, scale
    assert abs(ue_tx_scale_db(1.0, 1.0, -7.0) + 7.0) < 1e-9
    assert abs(ue_tx_scale_db(1.0, 100.0, 0.0) + 20.0) < 1e-9
    scaled = render_topology_oai(sample, scale)
    assert scaled.count("tx_scale_db:") == 1, scaled
    assert UE_CARRIERS + f"    tx_scale_db: {scale:.3f}\n" in scaled, scaled
    assert scaled.index("tx_scale_db:") > scaled.index("tx_carrier: n3-ul"), scaled
    for bad in (float("nan"), float("inf"), 201.0):
        try:
            render_topology_oai(sample, bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"render_topology_oai accepted tx_scale_db {bad}")
    for bad_power in (0.0, -1.0, float("nan")):
        try:
            ue_tx_scale_db(TX_POWER_DL, bad_power)
        except ValueError:
            pass
        else:
            raise AssertionError(f"ue_tx_scale_db accepted {bad_power}")
    # The argument layer: derived by default, explicit override, `off`.
    parser = argparse.ArgumentParser()
    add_tx_scale_arguments(parser)
    assert abs(resolve_ue_tx_scale_db(parser.parse_args([])) - scale) < 1e-9
    assert resolve_ue_tx_scale_db(parser.parse_args(["--ue-tx-scale-db", "off"])) is None
    assert abs(resolve_ue_tx_scale_db(parser.parse_args(["--ue-tx-scale-db", "-12.5"])) + 12.5) < 1e-9
    assert abs(resolve_ue_tx_scale_db(parser.parse_args(["--tx-power-ul", "1.12e-2"])) + 7.0) < 1e-9
    assert abs(resolve_ue_tx_scale_db(parser.parse_args(["--ul-power-offset-db", "0", "--tx-power-ul", "1.12e-1"])) + 10.0) < 1e-9
    for bad_args in (["--ue-tx-scale-db", "loud"], ["--ue-tx-scale-db", "500"], ["--tx-power-ul", "0"]):
        try:
            resolve_ue_tx_scale_db(parser.parse_args(bad_args))
        except ValueError:
            pass
        else:
            raise AssertionError(f"arguments accepted: {bad_args}")
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
    add_tx_scale_arguments(parser)
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
    ue_scale_db = resolve_ue_tx_scale_db(args)

    gnb_source = legacy.read_regular(
        repo_root / "use_cases/configs/ran/ocudu/docker/gnb_zmq_b210_fdd_srsue.yaml",
        "immutable legacy gNB fixture",
    )
    topology_source = legacy.read_regular(
        repo_root / "use_cases/configs/topologies/ocudu_docker/topology.ocudu-docker.cuda.yaml",
        "immutable legacy topology",
    )
    open5gs_source = legacy.read_regular(
        native_root / "src/ocudu/docker/open5gs/open5gs-5gc.yml",
        "pinned OCUDU Open5GS template",
    )
    nrue_source = legacy.read_regular(
        repo_root / "use_cases/configs/ran/oai/nrue_zmq_1x1.conf",
        "native OAI nrUE fixture",
    )
    subscriber_source = legacy.read_regular(
        repo_root / "use_cases/configs/ran/open5gs/subscriber-legacy-1x1.csv",
        "native subscriber template",
    )

    rendered = {
        "gnb.yaml": render_gnb_oai(gnb_source, log_dir),
        "topology.yaml": render_topology_oai(topology_source, ue_scale_db),
        "open5gs.yaml": legacy.render_open5gs(open5gs_source, native_root),
        "nrue.conf": validate_nrue(nrue_source),
        "subscriber.csv": legacy.validate_subscriber(subscriber_source),
    }
    for name, text in rendered.items():
        if legacy.PLACEHOLDER_RE.search(text):
            legacy.fail(f"unresolved placeholder in {name}")
        legacy.write_new(output_dir / name, text)
    print(f'event=native_oai_configs_rendered output_dir="{output_dir}" '
          f'ue_tx_scale_db={"off" if ue_scale_db is None else f"{ue_scale_db:.3f}"} '
          f'tx_power_dl={args.tx_power_dl:g} tx_power_ul={args.tx_power_ul:g}')
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnicodeError, ValueError) as error:
        print(f"config rendering failed: {error}", file=sys.stderr)
        raise SystemExit(2)
