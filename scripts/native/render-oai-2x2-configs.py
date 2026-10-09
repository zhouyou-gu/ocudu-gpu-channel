"""Render configs for the native OAI nrUE 2x2 SU-MIMO gate (M6.3).

Starts from the OAI 1x1 render (same immutable legacy gNB fixture, the same
OAI adaptation, the same Open5GS / subscriber / UE fixture) and changes only
what 2x2 needs:

  gNB   2 DL / 2 UL antennas, two ZMQ port pairs, `pdsch.max_rank`, MAC pcap
        off (OCUDU rejects MAC pcap with more than one DL antenna), and an
        optional `pdsch.max_ue_mcs` cap. CSI-RS stays at the OCUDU default
        (enabled): the OAI nrUE measures 2-port CSI-RS and reports RI/PMI,
        which is how the gNB learns that rank 2 is possible at all.
  UL    `--ul-max-rank 2`: 2-layer codebook PUSCH. The gNB gets
        `pusch.max_rank: 2` and periodic SRS (OCUDU picks the UL rank and TPMI
        from the SRS channel matrix), and the UE gets a capability file derived
        from OAI's own uecap_ports2.xml that (a) lists band 3 -- the stock file
        only lists band 78, so OCUDU fell back to its 1-layer / 1-SRS-port
        defaults for this band-3 cell -- and (b) advertises twoLayers on every
        codebook PUSCH per-CC entry, because the OAI UE MAC takes its PUSCH
        layer capability from the last per-CC entry matching the cell SCS and
        bandwidth, and all 15 kHz entries said oneLayer.
  path  `broker`: the gNB talks to the CUDA broker on loopback and the UE on
        the veth pair, through the 2x2 topology fixture.
        `direct`: no broker; the gNB binds its TX on the veth host side and
        connects its RX to the UE, so the two stacks see an identity channel.
        This is the control that removes the emulator as a variable.
"""

from __future__ import annotations

import argparse
import importlib.util
import math
import sys
from pathlib import Path

sys.dont_write_bytecode = True


def load_module(name: str, filename: str):
    path = Path(__file__).resolve().with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load renderer module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


oai = load_module("render_oai_1x1_configs", "render-oai-1x1-configs.py")
bw_configs = load_module("render_1x1_bw_configs", "render-1x1-bw-configs.py")
legacy = oai.legacy

VETH_HOST_IP = oai.VETH_HOST_IP
VETH_UE_IP = oai.VETH_UE_IP

LEGACY_DEVICE_ARGS = (
    "  device_args: tx_port=tcp://127.0.0.1:2000,rx_port=tcp://127.0.0.1:2001,base_srate=23.04e6\n"
)
BROKER_DEVICE_ARGS = (
    "  device_args: tx_port0=tcp://127.0.0.1:2000,tx_port1=tcp://127.0.0.1:2002,"
    "rx_port0=tcp://127.0.0.1:2001,rx_port1=tcp://127.0.0.1:2003,base_srate=23.04e6\n"
)
DIRECT_DEVICE_ARGS = (
    f"  device_args: tx_port0=tcp://{VETH_HOST_IP}:2100,tx_port1=tcp://{VETH_HOST_IP}:2102,"
    f"rx_port0=tcp://{VETH_UE_IP}:2101,rx_port1=tcp://{VETH_UE_IP}:2103,base_srate=23.04e6\n"
)


def render_gnb_2x2(
    source: str,
    log_dir: Path,
    path: str,
    max_rank: int,
    max_ue_mcs: int | None,
    csi_rs: bool,
    tx_backoff_db: float | None,
    ul_max_rank: int | None = None,
    srs_period_ms: float = 10.0,
) -> str:
    rendered = oai.render_gnb_oai(source, log_dir)
    rendered = legacy.replace_exact(
        rendered,
        LEGACY_DEVICE_ARGS,
        BROKER_DEVICE_ARGS if path == "broker" else DIRECT_DEVICE_ARGS,
        1,
        "gNB 2-port ZMQ endpoints",
    )
    if tx_backoff_db is not None:
        # The OAI nrUE's 2-layer equaliser is fixed point and its output
        # scale grows with the received amplitude (see M6.3 in
        # MIMO_MILESTONES.md); at OCUDU's default 12 dB back-off it overflows
        # int16. The back-off lowers the digital TX level of every DL channel.
        rendered = legacy.replace_exact(
            rendered,
            "  tx_gain: 0\n  rx_gain: 0\n\ncell_cfg:\n",
            f"  tx_gain: 0\n  rx_gain: 0\n  amplitude_control:\n    tx_gain_backoff: {tx_backoff_db}\n\ncell_cfg:\n",
            1,
            "gNB TX back-off",
        )
    rendered = legacy.replace_exact(
        rendered,
        "cell_cfg:\n  dl_arfcn: 368500\n",
        "cell_cfg:\n  nof_antennas_dl: 2\n  nof_antennas_ul: 2\n  dl_arfcn: 368500\n",
        1,
        "gNB 2T2R antenna count",
    )
    block = f"  pdsch:\n    mcs_table: qam64\n    max_rank: {max_rank}\n"
    if max_ue_mcs is not None:
        block += f"    max_ue_mcs: {max_ue_mcs}\n"
    if not csi_rs:
        block += "  csi:\n    csi_rs_enabled: false\n  pucch:\n    nof_cell_csi_res: 0\n"
    rendered = legacy.replace_exact(
        rendered,
        "  pdsch:\n    mcs_table: qam64\n  pusch:\n",
        block + "  pusch:\n",
        1,
        "gNB PDSCH rank / CSI-RS",
    )
    if ul_max_rank is not None:
        # OCUDU selects the PUSCH rank and TPMI from the SRS channel matrix
        # (ue_channel_state_manager::get_nof_ul_layers); without SRS it stays
        # at rank 1 whatever max_rank says.
        rendered = legacy.replace_exact(
            rendered,
            "  pusch:\n    mcs_table: qam64\n\n",
            f"  pusch:\n    mcs_table: qam64\n    max_rank: {ul_max_rank}\n"
            f"  srs:\n    type_enabled: periodic\n    period_ms: {srs_period_ms:g}\n\n",
            1,
            "gNB PUSCH rank / SRS",
        )
        # Per-UE scheduler metrics in the gNB log carry ul_ri (the mean UL rank
        # the scheduler derived from SRS); the PHY PUSCH line prints the layer
        # count only at debug level.
        rendered = rendered.rstrip("\n") + (
            "\n\nmetrics:\n  enable_log: true\n  layers:\n    enable_sched: true\n"
            "  periodicity:\n    du_report_period: 1000\n"
        )
    rendered = legacy.replace_exact(
        rendered, "  mac_enable: enable\n", "  mac_enable: disable\n", 1, "gNB MAC pcap off (>1 DL antenna)"
    )
    return rendered


def render_uecap_ul2(source: str, band: int = 3) -> str:
    """Copy of OAI's uecap_ports2.xml for `band` with 2-layer codebook PUSCH everywhere.

    The FDD cells are band 3; the 100 MHz TDD cell (S12) is n78, the stock band.
    """
    if source.count("<bandNR>78</bandNR>") != 3:
        legacy.fail("uecap_ports2.xml: expected exactly three band-78 entries")
    rendered = source.replace("<bandNR>78</bandNR>", f"<bandNR>{band}</bandNR>")
    one = "<maxNumberMIMO-LayersCB-PUSCH><oneLayer/></maxNumberMIMO-LayersCB-PUSCH>"
    if rendered.count(one) == 0:
        legacy.fail("uecap_ports2.xml: no oneLayer codebook PUSCH entries to raise")
    rendered = rendered.replace(one, "<maxNumberMIMO-LayersCB-PUSCH><twoLayers/></maxNumberMIMO-LayersCB-PUSCH>")
    if "<maxNumberSRS-Ports-PerResource><n2/></maxNumberSRS-Ports-PerResource>" not in rendered:
        legacy.fail("uecap_ports2.xml: no 2-port SRS feature set")
    return rendered


# Each port's `rx_endpoint` line is the anchor the labels go after; the two
# gNB ports bind loopback 2001/2003, the two UE ports the veth host side.
GNB_PORT_RX_ENDPOINTS = ("    rx_endpoint: tcp://127.0.0.1:2001\n", "    rx_endpoint: tcp://127.0.0.1:2003\n")
UE_PORT_RX_ENDPOINTS = (f"    rx_endpoint: tcp://{VETH_HOST_IP}:2100\n", f"    rx_endpoint: tcp://{VETH_HOST_IP}:2102\n")


def validate_topology(source: str) -> str:
    for required in (
        "  - id: gnb0\n    tx_ports:\n      - gnb0_p0\n      - gnb0_p1\n",
        "  - id: ue0\n    tx_ports:\n      - ue0_p0\n      - ue0_p1\n",
        f"    tx_endpoint: tcp://{VETH_UE_IP}:2101\n",
        f"    tx_endpoint: tcp://{VETH_UE_IP}:2103\n",
        *UE_PORT_RX_ENDPOINTS,
        "    tx_endpoint: tcp://127.0.0.1:2000\n",
        "    tx_endpoint: tcp://127.0.0.1:2002\n",
        *GNB_PORT_RX_ENDPOINTS,
    ):
        if source.count(required) != 1:
            legacy.fail(f"2x2 topology invariant is missing or ambiguous: {required!r}")
    for forbidden in ("tx_carrier:", "rx_carrier:", "carrier:", "tx_scale_db:"):
        if forbidden in source:
            legacy.fail(f"2x2 topology fixture already carries {forbidden!r}; the renderer adds it")
    return source


def render_topology_2x2(source: str, ue_scale_db: float | None = None) -> str:
    """The validated 2x2 fixture with carrier labels on every port and the UE scale.

    Same labels and `tx_scale_db` as the OAI 1x1 renderer (render-oai-1x1-configs.py
    render_topology_oai), applied to each of the four ports: the broker requires
    the two ports of a radio node to agree on tx_scale_db. The fixed 2x2 matrix
    and the 0 dB tdl stay byte-identical; there is no absolute noise floor in
    this fixture, so the scale only sets the level the gNB receives.

    The per-port level behind the shared OAI_UE_TX_POWER was measured for this
    gate (X7, oai-2x2/20261001T114323Z, 3 s wire capture after a 2 s skip,
    docs/plans/x7-oai-levels-prach.md): the UE's port 0 carries every uplink
    channel at the 1x1 per-channel level (0.3 ms PUCCH -60.5 dB vs -60.2 dB,
    1 ms narrow bursts -57.4/-57.6 vs -57.2 dB; the nrUE's digital amplitude per
    resource element is the same whatever the antenna count) and port 1 is
    silent with the stock 1-layer uplink (all zeros), so the gNB's second port
    only hears port 0 through the matrix. The traffic-weighted active mean of
    that window (2.15e-6) is 7.4 dB under the 1x1 constant only because the
    window held the DL-iperf HARQ-ACK PUCCH phase instead of the 1x1
    calibration's wideband attach/ping PUSCH; a 0 s-skip window of the same
    gate (20261001T120129Z) gives 3.17e-6 for the same reason (the 2-port gNB
    grants the attach signalling at a far higher MCS, so fewer PRBs). The
    constant stays shared: a given allocation has the same wire power in both
    gates, which is what `tx_scale_db` maps onto emitted power.
    """
    rendered = validate_topology(source)
    if ue_scale_db is not None and not (math.isfinite(ue_scale_db) and abs(ue_scale_db) <= 200.0):
        raise ValueError(f"ue_scale_db must be finite and within +/-200 dB: {ue_scale_db}")
    ue_extra = oai.UE_CARRIERS
    if ue_scale_db is not None:
        ue_extra += f"    tx_scale_db: {ue_scale_db:.3f}\n"
    for index, anchor in enumerate(GNB_PORT_RX_ENDPOINTS):
        rendered = insert_after_exact(rendered, anchor, oai.GNB_CARRIERS, f"gNB port {index} carriers")
    for index, anchor in enumerate(UE_PORT_RX_ENDPOINTS):
        rendered = insert_after_exact(rendered, anchor, ue_extra, f"UE port {index} carriers / scale")
    return rendered


def insert_after_exact(text: str, anchor: str, insertion: str, label: str) -> str:
    """`insertion` right after the single occurrence of `anchor` (a whole line)."""
    if text.count(anchor) != 1:
        legacy.fail(f"{label}: expected exactly one anchor, found {text.count(anchor)}")
    return text.replace(anchor, anchor + insertion)


def self_test() -> None:
    fixture = Path(__file__).resolve().parents[2] / "use_cases/configs/topologies/ocudu_native/topology.ocudu.oai-2x2.cuda.yaml"
    source = fixture.read_text(encoding="utf-8")
    assert validate_topology(source) == source
    plain = render_topology_2x2(source)
    assert plain.count("tx_carrier: n3-dl") == 2 and plain.count("tx_carrier: n3-ul") == 2, plain
    assert plain.count("rx_carrier: n3-ul") == 2 and plain.count("rx_carrier: n3-dl") == 2, plain
    assert "tx_scale_db" not in plain
    # Each label sits inside its own port's device entry (before the next `- id:`).
    for anchor, labels in ((GNB_PORT_RX_ENDPOINTS[0], oai.GNB_CARRIERS), (GNB_PORT_RX_ENDPOINTS[1], oai.GNB_CARRIERS),
                           (UE_PORT_RX_ENDPOINTS[0], oai.UE_CARRIERS), (UE_PORT_RX_ENDPOINTS[1], oai.UE_CARRIERS)):
        assert anchor + labels in plain, anchor
    scaled = render_topology_2x2(source, -40.0)
    assert scaled.count("    tx_scale_db: -40.000\n") == 2, scaled
    assert scaled.index("tx_scale_db") > scaled.index("id: ue0_p0"), scaled
    # Lines outside the device entries are untouched: same radio nodes, links, models.
    for section in ("radio_nodes:", "links:", "models:"):
        assert scaled[scaled.index(section):] == source[source.index(section):], section
    try:
        validate_topology(scaled)
    except ValueError:
        pass
    else:
        raise AssertionError("a labelled topology must not pass as a fixture")
    try:
        render_topology_2x2(source, float("nan"))
    except ValueError:
        pass
    else:
        raise AssertionError("render_topology_2x2 accepted a NaN scale")
    print("event=native_oai_2x2_config_renderer_self_test result=pass")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--native-root", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--log-dir", type=Path)
    parser.add_argument("--path", choices=("broker", "direct"))
    parser.add_argument("--max-rank", type=int, choices=(1, 2))
    parser.add_argument("--max-ue-mcs", type=int)
    parser.add_argument("--csi-rs", choices=("on", "off"), default="on")
    parser.add_argument("--topology", type=Path)
    parser.add_argument("--tx-backoff-db", type=float)
    # S12: the same per-bandwidth rewrite as the OAI 1x1 gate
    # (render-1x1-bw-configs.py apply_bandwidth), applied to the 2x2 render.
    parser.add_argument("--bw-mhz", type=int, default=20)
    parser.add_argument("--cuda-host-memory", choices=bw_configs.HOST_MEMORY)
    parser.add_argument("--ul-max-rank", type=int, choices=(1, 2))
    parser.add_argument("--srs-period-ms", type=float, default=10.0)
    parser.add_argument("--gnb-phy-log", choices=("info", "debug"), default="info")
    # Uplink wire level -> emitted power, shared with the 1x1 renderer
    # (--tx-power-ul / --ue-tx-scale-db and their OCUDU_NATIVE_OAI_UE_* defaults).
    oai.add_tx_scale_arguments(parser)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    required = {"--repo-root": args.repo_root, "--native-root": args.native_root, "--output-dir": args.output_dir,
                "--log-dir": args.log_dir, "--topology": args.topology, "--path": args.path, "--max-rank": args.max_rank}
    if args.self_test:
        if any(value is not None for value in required.values()):
            parser.error("--self-test cannot be combined with render arguments")
        self_test()
        return 0
    missing = [name for name, value in required.items() if value is None]
    if missing:
        parser.error(f"render mode requires {', '.join(missing)}")
    if args.bw_mhz not in bw_configs.BANDWIDTHS and args.bw_mhz != 100:
        legacy.fail(f"--bw-mhz must be one of {sorted(bw_configs.BANDWIDTHS) + [100]}")

    repo_root = args.repo_root.resolve(strict=True)
    native_root = args.native_root.resolve(strict=True)
    output_dir = legacy.safe_output_directory(args.output_dir)
    log_dir = legacy.safe_log_directory(args.log_dir)

    gnb_source = legacy.read_regular(
        repo_root / "use_cases/configs/ran/ocudu/docker/gnb_zmq_b210_fdd_srsue.yaml", "immutable legacy gNB fixture"
    )
    open5gs_source = legacy.read_regular(
        native_root / "src/ocudu/docker/open5gs/open5gs-5gc.yml", "pinned OCUDU Open5GS template"
    )
    nrue_source = legacy.read_regular(
        repo_root / "use_cases/configs/ran/oai/nrue_zmq_1x1.conf", "native OAI nrUE fixture"
    )
    subscriber_source = legacy.read_regular(
        repo_root / "use_cases/configs/ran/open5gs/subscriber-legacy-1x1.csv", "native subscriber template"
    )
    topology_source = legacy.read_regular(args.topology.resolve(strict=True), "2x2 topology fixture")
    try:
        ue_scale_db = oai.resolve_ue_tx_scale_db(args)
    except ValueError as error:
        legacy.fail(str(error))

    rendered = {
        "gnb.yaml": render_gnb_2x2(
            gnb_source,
            log_dir,
            args.path,
            args.max_rank,
            args.max_ue_mcs,
            args.csi_rs == "on",
            args.tx_backoff_db,
            args.ul_max_rank,
            args.srs_period_ms,
        ),
        "topology.yaml": render_topology_2x2(topology_source, ue_scale_db),
        "open5gs.yaml": legacy.render_open5gs(open5gs_source, native_root),
        "nrue.conf": oai.validate_nrue(nrue_source),
        "subscriber.csv": legacy.validate_subscriber(subscriber_source),
    }
    if args.ul_max_rank is not None:
        rendered["uecap.xml"] = render_uecap_ul2(
            legacy.read_regular(
                native_root / "src/oai/targets/PROJECTS/GENERIC-NR-5GC/CONF/uecap_ports2.xml",
                "pinned OAI 2-port UE capability",
            ),
            band=78 if args.bw_mhz == 100 else 3,
        )
    if args.gnb_phy_log == "debug":
        # Diagnosis only: PUSCH/PDSCH PDU fields such as nof_layers and ports are
        # printed at debug level.
        rendered["gnb.yaml"] = legacy.replace_exact(
            rendered["gnb.yaml"], "\nlog:\n  filename:", "\nlog:\n  phy_level: debug\n  filename:", 1, "gNB PHY debug log"
        )
    for name, text in rendered.items():
        if legacy.PLACEHOLDER_RE.search(text):
            legacy.fail(f"unresolved placeholder in {name}")
        legacy.write_new(output_dir / name, text)
    prb, srate = bw_configs.apply_bandwidth(
        output_dir, args.bw_mhz, "oai", args.cuda_host_memory or "", native_root,
        sample_rate_entries=4, uecap_name="uecap_ports2.xml",
    )
    print(f'event=native_oai_2x2_configs_rendered output_dir="{output_dir}" path={args.path} max_rank={args.max_rank} '
          f'ul_max_rank={args.ul_max_rank} bw_mhz={args.bw_mhz} prb={prb} srate_msps={srate} cuda_host_memory={args.cuda_host_memory or "default"} '
          f'ue_tx_scale_db={"off" if ue_scale_db is None else f"{ue_scale_db:.3f}"}')
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnicodeError, ValueError) as error:
        print(f"config rendering failed: {error}", file=sys.stderr)
        raise SystemExit(2)
