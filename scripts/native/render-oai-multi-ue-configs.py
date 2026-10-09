#!/usr/bin/env python3
"""Render native configs for the TDD n78 multi-UE gate with OAI nrUEs (X3/X4).

One OCUDU gNB on a **TDD n78 20 MHz / 30 kHz** cell, two OAI nrUEs, Open5GS,
and a broker topology generated from a Sionna scenario -- the OAI counterpart
of `render-sionna-multi-ue-configs.py`, on a cell where a UE<->UE edge is
physical (both UEs transmit and receive on the one carrier).

What is reused and what is new:

  gNB        the srsUE fixture `use_cases/configs/ran/ocudu/docker/gnb_zmq_b210_fdd_srsue.yaml`
             with the multi-UE renderer's loopback/log edits, the OAI 1x1
             gate's dedicated-PDCCH override removal, and the bandwidth
             renderer's band-3 -> n78 edits at 20 MHz: band 78, dl_arfcn
             632628 (3489.42 MHz, OCUDU's own 20 MHz n78 example), common_scs
             30, the 15 kHz CORESET#0 table entry dropped, TDD PRACH index 159,
             and an explicit `tdd_ul_dl_cfg` (10-slot period, 7 DL, 2 UL,
             special slot 6 DL / 4 UL symbols -- the pattern of OCUDU's
             gnb_ru_ran550_tdd_n78_100mhz example). ZMQ ports stay 2000/2001
             and the sample rate 23.04 MS/s (51 PRB at 30 kHz; OCUDU's own
             du_rf_b200_tdd_n78_20mhz example runs this cell at that rate), so
             the broker batch stays 23040.
  nrUE       `use_cases/configs/ran/oai/nrue_zmq_multi_ue.conf.in` once per UE with
             the IMSI of the shared UE table; radio args
             `-E -r 51 --numerology 1 --band 78 -C 3489420000 --ssb 0`
             (-E: 768-point FFT = 23.04 MS/s; --ssb 0: the gNB derives SSB
             ARFCN 632256 = offsetToPointA 0, k_SSB 0 for this cell, printed
             as "SSB derived parameters" in its log). No --CO: TDD.
  uecap      OAI's uecap_ports1.xml, with a 30 kHz / 20 MHz *uplink* per-CC
             feature set added only if the file lacks one (the pinned file
             has both the DL and the UL entry, so it is copied unchanged).
  topology   `render-sionna-multi-ue-configs.py`'s render_topology, then every
             carrier label becomes the TDD shorthand `carrier: n78`, and the
             UE endpoints move from loopback to each UE's veth pair (the OAI
             nrUE has no netns option, so the whole process lives in its
             namespace and its ZMQ path crosses a veth, as in the OAI 1x1 gate).
  tx scale   the per-UE `tx_scale_db` and the gNB receiver noise floor follow
             the Sionna renderer's formula with the OAI nrUE's wire level
             OAI_UE_TX_POWER instead of srsUE's (calibrated on the OAI 1x1
             gate, see the constant's comment); --tx-power-ul
             overrides it per run.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True


def _load(name: str, filename: str):
    path = Path(__file__).resolve().with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load renderer module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve the module by name
    spec.loader.exec_module(module)
    return module


legacy = _load("render_multi_ue_configs", "render-multi-ue-configs.py")
sionna = _load("render_sionna_multi_ue_configs", "render-sionna-multi-ue-configs.py")

UE_COUNT = 2

# --- the TDD cell -----------------------------------------------------------
BAND = 78
BANDWIDTH_MHZ = 20
SCS_KHZ = 30
PRB = 51
DL_ARFCN = 632628
# NR-ARFCN -> Hz above 3 GHz: 3000 MHz + (N - 600000) * 15 kHz.
CARRIER_HZ = int(3000e6 + (DL_ARFCN - 600000) * 15e3)  # 3489420000
# What the audited OCUDU gNB (a1916ed) derives for this cell and prints at
# start-up ("SSB derived parameters ... SSB offset pointA:0, k_SSB:0, SSB
# arfcn:632256, Coreset index:0"). The nrUE's --ssb is the SSB's first
# subcarrier counted from point A in 30 kHz subcarriers: 0 * 12 + 0.
SSB_ARFCN = 632256
SSB_OFFSET_SUBCARRIERS = 0
SAMPLE_RATE_HZ = 23_040_000
# 10-slot (5 ms at 30 kHz) period, 7 DL + special (6 DL / 4 UL symbols) + 2 UL.
TDD_PATTERN = {
    "dl_ul_tx_period": 10,
    "nof_dl_slots": 7,
    "nof_dl_symbols": 6,
    "nof_ul_slots": 2,
    "nof_ul_symbols": 4,
}
PRACH_CONFIG_INDEX = 159
NRUE_RADIO_ARGS = f"-E -r {PRB} --numerology 1 --band {BAND} -C {CARRIER_HZ} --ssb {SSB_OFFSET_SUBCARRIERS}"
CARRIER_LABEL = "n78"

# --- transmit levels --------------------------------------------------------
# The OAI nrUE's wire level (mean |x|^2 over the active samples of a broker
# wire capture, in the IQ float units the ZMQ radios exchange;
# scripts/native/wire-capture-power.py), calibrated on the OAI 1x1 gate
# (oai-1x1/20261001T110037Z, 3 s window over the gate's pings: six 14-symbol
# 64-PRB QPSK PUSCH at -42 dB plus PUCCH at -56..-57 dB): 1.17e-5 (-49.3 dB).
# OAI emits every UL channel at a fixed digital AMP per RE and the ZMQ module
# scales int16 by 1/32767, so the level follows the allocation width; this is
# the traffic-weighted value render-oai-1x1-configs.py also uses. It is 94 dB
# BELOW srsUE's 3.0e4, so the per-UE tx_scale_db comes out at about +22.8 dB (a
# gain) instead of srsUE's -71.3 dB. --tx-power-ul (gate knob
# OCUDU_NATIVE_SIONNA_TX_POWER_UL) overrides it per run. The gNB level
# TX_POWER_DL is the same gNB as the srsUE gate (OCUDU default amplitude
# control, 1.11e-2 in the same capture), reused.
OAI_UE_TX_POWER = 1.17e-5
OAI_UE_TX_POWER_SOURCE = "oai-1x1/20261001T110037Z wire capture (ue0.tx_in active_mean 1.168e-05, PUSCH+PUCCH)"
TX_POWER_DL = sionna.TX_POWER_DL

# Veth pairs, one per UE: host (broker) side .1, UE side .2. 10.201.0.0/30 is
# the OAI 1x1 gate's pair; the second UE takes the next /30 of 10.201.x.0.
VETH = {
    "ue0": ("10.201.0.1", "10.201.0.2"),
    "ue1": ("10.201.1.1", "10.201.1.2"),
}


def nrue_radio_args() -> str:
    return NRUE_RADIO_ARGS


def render_gnb_tdd(source: str, log_dir: Path) -> str:
    """Multi-UE gNB render -> OAI PDCCH adaptation -> TDD n78 20 MHz cell."""

    rendered = legacy.render_gnb(source, log_dir)
    # The fixture's `ss2_type: common` + `dci_format_0_1_and_1_1: false` exist
    # FOR srsUE; the OAI nrUE never receives another DCI after
    # CellGroupConfig with them (render-oai-1x1-configs.py render_gnb_oai).
    rendered = legacy.replace_exact(
        rendered,
        "  pdcch:\n"
        "    dedicated:\n"
        "      ss2_type: common\n"
        "      dci_format_0_1_and_1_1: false\n"
        "    common:\n"
        "      ss0_index: 0\n"
        "      coreset0_index: 12\n",
        # CORESET#0 12 / SS#0 0 are 15 kHz FDD table entries; the gNB picks
        # them for the 30 kHz cell (it derives Coreset index 0).
        "",
        1,
        "srsUE-only dedicated PDCCH override and 15 kHz CORESET#0 removal",
    )
    rendered = legacy.replace_exact(rendered, "  dl_arfcn: 368500\n", f"  dl_arfcn: {DL_ARFCN}\n", 1, "gNB n78 ARFCN")
    rendered = legacy.replace_exact(rendered, "  band: 3\n", f"  band: {BAND}\n", 1, "gNB band")
    rendered = legacy.replace_exact(rendered, "  common_scs: 15\n", f"  common_scs: {SCS_KHZ}\n", 1, "gNB SCS")
    rendered = legacy.replace_exact(
        rendered, "    prach_config_index: 1\n", f"    prach_config_index: {PRACH_CONFIG_INDEX}\n", 1, "gNB TDD PRACH"
    )
    tdd = "  tdd_ul_dl_cfg:\n" + "".join(f"    {key}: {value}\n" for key, value in TDD_PATTERN.items())
    rendered = legacy.insert_before_exact(rendered, "  prach:\n", tdd, "gNB TDD pattern insertion")
    for required in (f"  channel_bandwidth_MHz: {BANDWIDTH_MHZ}\n", "  srate: 23.04\n", "base_srate=23.04e6"):
        if rendered.count(required) != 1:
            legacy.fail(f"gNB fixture invariant missing or ambiguous: {required!r}")
    return rendered


def render_nrue(template: str, ue: dict) -> str:
    if template.count("@UE_IMSI@") != 1:
        legacy.fail("nrUE template must carry exactly one @UE_IMSI@")
    for token in ('key = "00112233445566778899aabbccddeeff";\n',
                  'opc = "63bfa50ee6523365ff14c1f45f88737d";\n',
                  'pdu_sessions = ({ dnn = "internet"; nssai_sst = 1; });\n'):
        if template.count(token) != 1:
            legacy.fail(f"nrUE template invariant missing or ambiguous: {token!r}")
    rendered = template.replace("@UE_IMSI@", ue["imsi"])
    if legacy.PLACEHOLDER_RE.search(rendered):
        legacy.fail("unresolved nrUE placeholder")
    return rendered


def render_topology_tdd(shape, rx_noise: dict[str, float] | None, ue_scale_db: float | None) -> str:
    """Sionna multi-UE topology with TDD carrier labels and veth UE endpoints."""

    text = sionna.render_topology(shape, rx_noise, ue_scale_db=ue_scale_db)
    gnb_labels = "    tx_carrier: n3-dl\n    rx_carrier: n3-ul\n"
    ue_labels = "    tx_carrier: n3-ul\n    rx_carrier: n3-dl\n"
    if text.count(gnb_labels) != 1 or text.count(ue_labels) != len(shape.ues):
        legacy.fail("Sionna topology carrier labels are not the FDD band-3 pair this renderer rewrites")
    text = text.replace(gnb_labels, f"    carrier: {CARRIER_LABEL}\n")
    text = text.replace(ue_labels, f"    carrier: {CARRIER_LABEL}\n")
    endpoints = {ue["device_id"]: (ue["tx_port"], ue["rx_port"]) for ue in legacy.UES}
    for ue in shape.ues:
        host_ip, ue_ip = VETH[ue.node_id]
        tx_port, rx_port = endpoints[ue.node_id]
        text = legacy.replace_exact(
            text, f"    tx_endpoint: tcp://127.0.0.1:{tx_port}\n",
            f"    tx_endpoint: tcp://{ue_ip}:{tx_port}\n", 1, f"{ue.node_id} REQ veth endpoint")
        text = legacy.replace_exact(
            text, f"    rx_endpoint: tcp://127.0.0.1:{rx_port}\n",
            f"    rx_endpoint: tcp://{host_ip}:{rx_port}\n", 1, f"{ue.node_id} REP veth endpoint")
    if text.count(f"    sample_rate_hz: {SAMPLE_RATE_HZ}\n") != len(shape.nodes) or "  batch_samples: 23040\n" not in text:
        legacy.fail("broker sample rate / batch are not the 23.04 MS/s this cell runs at")
    return text


UL_PER_CC_RE = re.compile(r"            <FeatureSetUplinkPerCC>.*?</FeatureSetUplinkPerCC>\n", re.S)


def uecap_with_ul_30khz_20mhz(text: str) -> str:
    """Add a 30 kHz / 20 MHz UL per-CC feature set to an OAI UE capability file.

    The nrUE MAC takes its codebook PUSCH layer count from the UL per-CC entry
    whose SCS and bandwidth equal the cell's (config_ue.c
    handle_mac_uecap_info); without one the value stays at its 0 initialiser.
    The added entry is the 15 kHz 20 MHz one with the SCS changed (one layer,
    256QAM, one SRS resource). A file that lists it is returned unchanged.
    """

    blocks = UL_PER_CC_RE.findall(text)
    if any("<kHz30/>" in b and "<mhz20/>" in b for b in blocks):
        return text  # the pinned OAI's file already lists it; nothing to add
    source = [b for b in blocks if "<kHz15/>" in b and "<mhz20/>" in b and "channelBW-90mhz" not in b]
    if len(source) != 1:
        raise ValueError("expected exactly one 15 kHz 20 MHz UL feature set to copy")
    return text.replace(source[0], source[0] + source[0].replace("<kHz15/>", "<kHz30/>"), 1)


def metadata(shape, ue_scale: float, rx_noise: dict[str, float], args) -> dict:
    return {
        "scenario": str(args.scenario_config),
        "gnb": shape.gnb.node_id,
        "ues": [ue.node_id for ue in shape.ues],
        "links": [{"from": l.source, "to": l.destination, "model": l.model} for l in shape.links],
        "ue_count": UE_COUNT,
        "cell": {
            "duplex": "tdd", "band": BAND, "bandwidth_mhz": BANDWIDTH_MHZ, "scs_khz": SCS_KHZ,
            "prb": PRB, "dl_arfcn": DL_ARFCN, "carrier_hz": CARRIER_HZ, "ssb_arfcn": SSB_ARFCN,
            "sample_rate_hz": SAMPLE_RATE_HZ, "tdd_ul_dl_cfg": TDD_PATTERN,
            "prach_config_index": PRACH_CONFIG_INDEX, "carrier_label": CARRIER_LABEL,
        },
        "nrue_radio_args": NRUE_RADIO_ARGS,
        "veth": {ue: {"host": host, "ue": ue_ip} for ue, (host, ue_ip) in VETH.items()},
        "ue_tx_scale_db": ue_scale,
        "ue_tx_power_source": OAI_UE_TX_POWER_SOURCE if args.tx_power_ul == OAI_UE_TX_POWER else "--tx-power-ul",
        "rx_noise": {
            "awgn_snr_db": args.awgn_snr_db,
            "tx_power_dl": args.tx_power_dl,
            "tx_power_ul": args.tx_power_ul,
            "ul_power_offset_db": args.ul_power_offset_db,
            "noise_power": rx_noise,
        },
    }


def self_test() -> None:
    import tempfile

    fixture = Path(__file__).resolve().parents[2] / "use_cases/configs/ran/ocudu/docker/gnb_zmq_b210_fdd_srsue.yaml"
    gnb = render_gnb_tdd(fixture.read_text(encoding="utf-8"), Path("/tmp/x"))
    for token in ("  band: 78\n", "  dl_arfcn: 632628\n", "  common_scs: 30\n", "  channel_bandwidth_MHz: 20\n",
                  "    prach_config_index: 159\n", "  tdd_ul_dl_cfg:\n    dl_ul_tx_period: 10\n    nof_dl_slots: 7\n"
                  "    nof_dl_symbols: 6\n    nof_ul_slots: 2\n    nof_ul_symbols: 4\n",
                  "tx_port=tcp://127.0.0.1:2000,rx_port=tcp://127.0.0.1:2001,base_srate=23.04e6"):
        assert token in gnb, token
    assert "pdcch" not in gnb and "coreset0_index" not in gnb, gnb
    assert gnb.index("  tdd_ul_dl_cfg:") < gnb.index("  prach:")

    one = {"rows": 1, "cols": 1}
    nodes = {"gnb0": {"array": one}, "ue0": {"array": one}, "ue1": {"array": one}}
    links = [
        {"from": "gnb0", "to": "ue0", "direction": "downlink", "model": "sionna_rt"},
        {"from": "gnb0", "to": "ue1", "direction": "downlink", "model": "sionna_rt"},
        {"from": "ue0", "to": "gnb0", "direction": "uplink", "model": "sionna_rt"},
        {"from": "ue1", "to": "gnb0", "direction": "uplink", "model": "sionna_rt"},
        {"from": "ue0", "to": "ue1", "direction": "crosstalk", "model": "sionna_rt"},
        {"from": "ue1", "to": "ue0", "direction": "crosstalk", "model": "sionna_rt"},
    ]
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "s.json"
        path.write_text(json.dumps({"name": "t", "nodes": nodes, "links": links}), encoding="utf-8")
        shape = sionna.load_live_shape(path, UE_COUNT)
    scale = sionna.ue_tx_scale_db(TX_POWER_DL, OAI_UE_TX_POWER)
    floors = sionna.rx_noise_powers(shape, 40.0, TX_POWER_DL, OAI_UE_TX_POWER, scale)
    topology = render_topology_tdd(shape, floors, scale)
    assert topology.count("    carrier: n78\n") == 3, topology
    assert "tx_carrier" not in topology and "rx_carrier" not in topology
    for token in ("    tx_endpoint: tcp://127.0.0.1:2000\n", "    rx_endpoint: tcp://127.0.0.1:2001\n",
                  "    tx_endpoint: tcp://10.201.0.2:2101\n", "    rx_endpoint: tcp://10.201.0.1:2100\n",
                  "    tx_endpoint: tcp://10.201.1.2:2103\n", "    rx_endpoint: tcp://10.201.1.1:2102\n",
                  "  - from: ue0\n    to: ue1\n    model: sionna_rt\n", "  - from: ue1\n    to: ue0\n    model: sionna_rt\n"):
        assert token in topology, token
    assert topology.count("  - from: ") == 6
    assert topology.count("tx_scale_db:") == 2 and topology.count("type: awgn") == 3
    # The gNB floor is sized from the *scaled* uplink, which by construction
    # equals the DL minus the 7 dB power offset whatever the wire level.
    assert abs(floors["gnb0"] - TX_POWER_DL * 10 ** (-0.7) / 1e4) < 1e-15, floors
    assert abs(floors["ue0"] - TX_POWER_DL / 1e4) < 1e-15, floors

    conf = render_nrue('uicc0 = {\n  imsi = "@UE_IMSI@";\n  key = "00112233445566778899aabbccddeeff";\n'
                       '  opc = "63bfa50ee6523365ff14c1f45f88737d";\n'
                       '  pdu_sessions = ({ dnn = "internet"; nssai_sst = 1; });\n};\n', legacy.UES[1])
    assert 'imsi = "001010123456781";' in conf, conf

    cap = ("<x>\n"
           "            <FeatureSetUplinkPerCC>\n                <supportedSubcarrierSpacingUL><kHz15/></supportedSubcarrierSpacingUL>\n"
           "                <supportedBandwidthUL>\n                    <fr1><mhz20/></fr1>\n                </supportedBandwidthUL>\n"
           "            </FeatureSetUplinkPerCC>\n"
           "            <FeatureSetUplinkPerCC>\n                <supportedSubcarrierSpacingUL><kHz30/></supportedSubcarrierSpacingUL>\n"
           "                <supportedBandwidthUL>\n                    <fr1><mhz40/></fr1>\n                </supportedBandwidthUL>\n"
           "            </FeatureSetUplinkPerCC>\n</x>\n")
    out = uecap_with_ul_30khz_20mhz(cap)
    assert out.count("<FeatureSetUplinkPerCC>") == 3, out
    assert len([b for b in UL_PER_CC_RE.findall(out) if "<kHz30/>" in b and "<mhz20/>" in b]) == 1
    assert uecap_with_ul_30khz_20mhz(out) == out, "a file that lists the entry is left alone"
    assert nrue_radio_args() == "-E -r 51 --numerology 1 --band 78 -C 3489420000 --ssb 0"
    assert CARRIER_HZ == 3489420000
    print("event=native_oai_multi_ue_config_renderer_self_test result=pass")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--native-root", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--log-dir", type=Path)
    parser.add_argument("--scenario-config", type=Path)
    parser.add_argument("--awgn-snr-db", type=float, default=None,
                        help="SNR a unit-gain link sees against the absolute rx noise floor")
    parser.add_argument("--tx-power-dl", type=float, default=TX_POWER_DL,
                        help="gNB transmit power on the wire, mean |x|^2 of active samples")
    parser.add_argument("--tx-power-ul", type=float, default=OAI_UE_TX_POWER,
                        help="OAI nrUE transmit power on the wire (default: provisional, see OAI_UE_TX_POWER)")
    parser.add_argument("--ul-power-offset-db", type=float, default=sionna.UL_POWER_OFFSET_DB,
                        help="emitted UE power relative to the gNB, dB (default 23 dBm - 30 dBm)")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    render_args = (args.repo_root, args.native_root, args.output_dir, args.log_dir, args.scenario_config)
    if args.self_test:
        if any(render_args) or args.awgn_snr_db is not None:
            parser.error("--self-test cannot be combined with render arguments")
        self_test()
        return 0
    if not all(render_args):
        parser.error("render mode requires repo/native/output/log/scenario arguments")

    repo_root = args.repo_root.resolve(strict=True)
    native_root = args.native_root.resolve(strict=True)
    scenario = args.scenario_config.resolve(strict=True)
    output_dir = legacy.safe_directory(args.output_dir, "output directory")
    log_dir = legacy.safe_directory(args.log_dir, "log directory")
    if (repo_root != args.repo_root or native_root != args.native_root or scenario != args.scenario_config):
        legacy.fail("repo, native, and scenario paths must already be canonical")

    shape = sionna.load_live_shape(scenario, UE_COUNT)
    ues = sionna.gate_ues(UE_COUNT)
    try:
        ue_scale = sionna.ue_tx_scale_db(args.tx_power_dl, args.tx_power_ul, args.ul_power_offset_db)
        rx_noise = sionna.rx_noise_powers(shape, args.awgn_snr_db, args.tx_power_dl, args.tx_power_ul, ue_scale)
    except ValueError as error:
        legacy.fail(str(error))
    gnb_source = legacy.read_regular(repo_root / "use_cases/configs/ran/ocudu/docker/gnb_zmq_b210_fdd_srsue.yaml", "gNB fixture")
    open5gs_source = legacy.read_regular(native_root / "src/ocudu/docker/open5gs/open5gs-5gc.yml",
                                         "pinned OCUDU Open5GS template")
    nrue_source = legacy.read_regular(repo_root / "use_cases/configs/ran/oai/nrue_zmq_multi_ue.conf.in",
                                      "native OAI nrUE multi-UE template")
    _, subscriber_path, _ = legacy.LAYOUTS[UE_COUNT]
    subscriber_source = legacy.read_regular(repo_root / subscriber_path, "native subscriber template")
    uecap_source = legacy.read_regular(
        native_root / "src/oai/targets/PROJECTS/GENERIC-NR-5GC/CONF/uecap_ports1.xml", "stock OAI UE capability")
    try:
        uecap = uecap_with_ul_30khz_20mhz(uecap_source)
    except ValueError as error:
        legacy.fail(str(error))

    rendered = {
        "gnb.yaml": render_gnb_tdd(gnb_source, log_dir),
        "topology.yaml": render_topology_tdd(shape, rx_noise, ue_scale),
        "open5gs.yaml": legacy.render_open5gs(open5gs_source, native_root),
        "subscriber.csv": legacy.validate_subscriber(subscriber_source, ues),
        "nrue-radio.args": NRUE_RADIO_ARGS + "\n",
        "uecap.xml": uecap,
    }
    for ue in ues:
        rendered[f"nrue-{ue['device_id']}.conf"] = render_nrue(nrue_source, ue)
    for name, text in rendered.items():
        if name != "uecap.xml" and legacy.PLACEHOLDER_RE.search(text):
            legacy.fail(f"unresolved placeholder in {name}")
        legacy.write_new(output_dir / name, text)
    legacy.write_new(output_dir / "oai-multi-ue-shape.json",
                     json.dumps(metadata(shape, ue_scale, rx_noise, args), indent=2, sort_keys=True) + "\n")
    print("event=native_oai_multi_ue_configs_rendered "
          f"ues={len(shape.ues)} links={len(shape.links)} cell=tdd-n78-{BANDWIDTH_MHZ}mhz-{SCS_KHZ}khz "
          f"ue_tx_scale_db={ue_scale:.3f} ue_tx_power={args.tx_power_ul:.3e} "
          f"rx_noise={'on' if rx_noise else 'off'} output_dir=\"{output_dir}\"")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnicodeError, ValueError) as error:
        print(f"config rendering failed: {error}", file=sys.stderr)
        raise SystemExit(2)
