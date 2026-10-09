#!/usr/bin/env python3
"""Render a native rank-1 live stack from one Sionna scenario.

The Sionna node arrays are the source of truth for the OCUDU antenna counts,
ZMQ endpoint count, Broker radio-node dimensions, and dynamic matrix link ids.
The live srsUE remains a one-port radio, so only the gNB arrays may scale.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

SUPPORTED_GNB_PORTS = frozenset((1, 2, 4))
# srsUE (srsRAN 4G) builds its ZMQ radio with nof_antennas ports; two is
# the most its downlink chain handles.
MAX_UE_PORTS = 2
MODEL_ID_RE = re.compile(r"[A-Za-z0-9_-]+")


def load_legacy_renderer():
    path = Path(__file__).resolve().with_name("render-legacy-1x1-configs.py")
    spec = importlib.util.spec_from_file_location("render_legacy_1x1_configs", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load legacy renderer module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


legacy = load_legacy_renderer()


@dataclass(frozen=True)
class NodePorts:
    node_id: str
    tx: int
    rx: int


@dataclass(frozen=True)
class ShapeLink:
    source: str
    destination: str
    model: str


@dataclass(frozen=True)
class LiveShape:
    """The runtime shape a Sionna scenario asks the native stack to build.

    Everything here is read off the scenario rather than assumed: the port
    counts come from the node arrays, and the links come from the links array
    whatever their number or model. The named `gnb_*`/`ue_*` fields stay for
    the callers and the shape metadata file that already depend on them.
    """

    gnb_tx: int
    gnb_rx: int
    ue_tx: int
    ue_rx: int
    downlink_model: str
    uplink_model: str
    nodes: tuple[NodePorts, ...] = ()
    links: tuple[ShapeLink, ...] = ()

    # A shape built from the scalar fields alone — as the self-test and any
    # older caller does — still describes one gNB and one UE, so the node and
    # link views are synthesised rather than left empty.
    @property
    def gnb(self) -> NodePorts:
        return self.nodes[0] if self.nodes else NodePorts("gnb0", self.gnb_tx, self.gnb_rx)

    @property
    def ue(self) -> NodePorts:
        return self.nodes[1] if self.nodes else NodePorts("ue0", self.ue_tx, self.ue_rx)

    @property
    def radio_nodes(self) -> tuple[NodePorts, ...]:
        return self.nodes or (self.gnb, self.ue)

    @property
    def channel_links(self) -> tuple[ShapeLink, ...]:
        if self.links:
            return self.links
        return (
            ShapeLink(self.gnb.node_id, self.ue.node_id, self.downlink_model),
            ShapeLink(self.ue.node_id, self.gnb.node_id, self.uplink_model),
        )


def _object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object")
    return value


def _array_count(node: dict[str, Any], key: str, where: str) -> int:
    raw = node.get(key, node.get("array"))
    if raw is None:
        raw = {}
    array = _object(raw, f"{where}.{key}")
    rows = array.get("rows", 1)
    cols = array.get("cols", 1)
    if (
        not isinstance(rows, int)
        or isinstance(rows, bool)
        or rows <= 0
        or not isinstance(cols, int)
        or isinstance(cols, bool)
        or cols <= 0
    ):
        raise ValueError(f"{where}.{key} rows and cols must be positive integers")
    return rows * cols


def load_live_shape(path: Path) -> LiveShape:
    """Read the runtime shape out of a Sionna scenario.

    Node port counts and the link list are taken as given. Two things are
    still checked, and only because the native stack really cannot do them:
    the OCUDU gNB is built for a fixed set of port counts, and
    `run-ocudu-legacy-1x1-inner.sh` starts exactly one srsUE process, so the
    scenario may name only one UE. Neither the number of links nor the UE
    port count is constrained any more.
    """

    try:
        root = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load Sionna scenario {path}: {error}") from error
    root = _object(root, "scenario")
    raw_nodes = _object(root.get("nodes"), "scenario.nodes")
    if not raw_nodes:
        raise ValueError("scenario.nodes must not be empty")

    gnb_ids = [node_id for node_id in raw_nodes if node_id.startswith("gnb")]
    ue_ids = [node_id for node_id in raw_nodes if not node_id.startswith("gnb")]
    if len(gnb_ids) != 1:
        raise ValueError(
            "the native runtime builds one OCUDU gNB; scenario names "
            f"{len(gnb_ids)}: {', '.join(sorted(gnb_ids)) or 'none'}"
        )
    if len(ue_ids) != 1:
        # run-ocudu-multi-ue.sh is not the way out of this: it is a
        # legacy-channel attach gate with no Sionna bridge at all. Nothing in
        # this tree drives more than one srsUE from Sionna RT today, so the
        # message says what actually exists rather than pointing somewhere
        # that would fail differently.
        raise ValueError(
            "run-ocudu-legacy-1x1-inner.sh starts a single srsUE process, so "
            f"the native Sionna gates carry one UE; scenario names "
            f"{len(ue_ids)}: {', '.join(sorted(ue_ids)) or 'none'}. Run a "
            "multi-UE scenario through the Sionna bridge and web UI directly "
            "(apps/sionna_bridge/run_bridge.py --scenario-config ...); the "
            "live radio stacks are what is limited to one UE, not the bridge."
        )

    ports: list[NodePorts] = []
    for node_id in (*gnb_ids, *ue_ids):
        node = _object(raw_nodes[node_id], f"nodes.{node_id}")
        ports.append(
            NodePorts(
                node_id,
                _array_count(node, "tx_array", f"nodes.{node_id}"),
                _array_count(node, "rx_array", f"nodes.{node_id}"),
            )
        )
    gnb, ue = ports[0], ports[1]
    if gnb.tx not in SUPPORTED_GNB_PORTS or gnb.rx not in SUPPORTED_GNB_PORTS:
        supported = ", ".join(str(value) for value in sorted(SUPPORTED_GNB_PORTS))
        raise ValueError(
            f"live OCUDU gNB arrays must resolve to {supported} ports; "
            f"scenario requested tx={gnb.tx}, rx={gnb.rx}"
        )
    if ue.tx > MAX_UE_PORTS or ue.rx > MAX_UE_PORTS:
        raise ValueError(
            f"srsUE supports at most {MAX_UE_PORTS} antenna ports; "
            f"nodes.{ue.node_id} requested tx={ue.tx}, rx={ue.rx}"
        )

    raw_links = root.get("links")
    if not isinstance(raw_links, list) or not raw_links:
        raise ValueError("scenario.links must be a non-empty array")
    known = {port.node_id for port in ports}
    links: list[ShapeLink] = []
    seen: set[tuple[str, str, str]] = set()
    for index, raw_link in enumerate(raw_links):
        link = _object(raw_link, f"links[{index}]")
        source = link.get("from")
        destination = link.get("to")
        model = link.get("model", "sionna_rt")
        if source not in known or destination not in known:
            raise ValueError(
                f"links[{index}] references a node the scenario does not "
                f"define: {source!r} -> {destination!r}"
            )
        if not isinstance(model, str) or MODEL_ID_RE.fullmatch(model) is None:
            raise ValueError(f"links[{index}].model must match {MODEL_ID_RE.pattern}")
        key = (source, destination, model)
        if key in seen:
            raise ValueError(f"duplicate link: {source}>{destination}:{model}")
        seen.add(key)
        links.append(ShapeLink(source, destination, model))

    # The gNB<->UE pair still has to be present: it is what the RRC/PDU/ping
    # gate measures. Extra links beside it are carried through untouched.
    downlink = next(
        (link.model for link in links
         if link.source == gnb.node_id and link.destination == ue.node_id), None
    )
    uplink = next(
        (link.model for link in links
         if link.source == ue.node_id and link.destination == gnb.node_id), None
    )
    if downlink is None or uplink is None:
        raise ValueError(
            f"scenario must carry {gnb.node_id}->{ue.node_id} and "
            f"{ue.node_id}->{gnb.node_id} links"
        )
    return LiveShape(
        gnb.tx, gnb.rx, ue.tx, ue.rx, downlink, uplink,
        tuple(ports), tuple(links),
    )


def render_gnb(source: str, log_dir: Path, shape: LiveShape) -> str:
    required = (
        "  nof_antennas_dl: 4\n",
        "  nof_antennas_ul: 4\n",
        "      ss2_type: ue_dedicated\n",
        "      dci_format_0_1_and_1_1: true\n",
        "    csi_rs_enabled: false\n",
        "    nof_cell_csi_res: 0\n",
        "  mac_enable: disable\n",
        "    max_ue_mcs: 9\n",
    )
    for token in required:
        if source.count(token) != 1:
            legacy.fail(f"rank-1 gNB fixture invariant missing or ambiguous: {token!r}")
    device_line = next(
        (line for line in source.splitlines(keepends=True) if line.startswith("  device_args: ")),
        None,
    )
    if device_line is None or source.count(device_line) != 1:
        legacy.fail("rank-1 gNB device_args line is missing or ambiguous")
    tx_args = [f"tx_port{i}=tcp://127.0.0.1:{2000 + 2 * i}" for i in range(shape.gnb_tx)]
    rx_args = [f"rx_port{i}=tcp://127.0.0.1:{2001 + 2 * i}" for i in range(shape.gnb_rx)]
    rendered = source.replace(
        device_line,
        "  device_args: " + ",".join((*tx_args, *rx_args, "base_srate=23.04e6")) + "\n",
        1,
    )
    # The fixture is the 4T4R template, so a 4-port scenario asks for a
    # replacement that changes nothing. legacy.replace_exact rejects that:
    # its "token survived replacement" guard cannot tell a deliberate
    # no-op from a substitution that silently failed. That made 4 ports
    # unreachable even though SUPPORTED_GNB_PORTS advertises them, so the
    # no-op is skipped here instead of weakening the shared guard. The
    # `required` check above already proved each token appears once.
    for key, count, label in (
        ("nof_antennas_dl", shape.gnb_tx, "gNB DL antenna count"),
        ("nof_antennas_ul", shape.gnb_rx, "gNB UL antenna count"),
    ):
        if count != 4:
            rendered = legacy.replace_exact(
                rendered, f"  {key}: 4\n", f"  {key}: {count}\n", 1, label,
            )
    for placeholder, filename, label in (
        ("@GNB_LOG@", "gnb-internal.log", "gNB log path"),
        ("@GNB_MAC_PCAP@", "gnb_mac.pcap", "gNB MAC pcap path"),
        ("@GNB_NGAP_PCAP@", "gnb_ngap.pcap", "gNB NGAP pcap path"),
    ):
        rendered = legacy.replace_exact(rendered, placeholder, str(log_dir / filename), 1, label)
    if legacy.PLACEHOLDER_RE.search(rendered):
        legacy.fail("unresolved gNB placeholder")
    return rendered


def _port_lines(count: int, node_id: str = "gnb0") -> str:
    return "".join(f"      - {node_id}_p{index}\n" for index in range(count))


# ZMQ endpoints are allocated by node position: the gNB keeps 2000.. and the
# UE keeps 2100.., which is what every existing config, log and gate message
# already refers to.
GNB_PORT_BASE = 2000
UE_PORT_BASE = 2100


def _ue_endpoints(index: int) -> tuple[str, str]:
    """(tx, rx) as seen by the UE: it transmits on the odd port."""

    base = UE_PORT_BASE + 2 * index
    return f"tcp://127.0.0.1:{base + 1}", f"tcp://127.0.0.1:{base}"


# Metrics are opt-in. The gNB emits none of the RAN KPIs by default, and
# turning them on costs real time on the 1 ms slot path, so the block is
# appended only when OCUDU_NATIVE_GNB_METRICS is truthy. With it unset the
# rendered gnb.yaml is byte-identical to the pre-metrics output, which is
# what keeps the 2x1/4x1 gates provably unaffected.
#
# Schema verified against the OCUDU sources rather than guessed:
#   metrics.enable_json          apps/helpers/metrics/metrics_config_yaml_writer.cpp
#   metrics.layers.enable_sched* apps/units/.../du_high/du_high_config_yaml_writer.cpp
#   metrics.periodicity.*        same file
#   remote_control.{enabled,bind_addr,port}
#                                apps/services/remote_control/remote_control_appconfig_cli11_schema.cpp
# Note metrics.enable_json is bound twice: once to the JSON metrics sink and
# once to remote_control_appconfig::enable_metrics_subscription. The struct
# field is NOT a YAML key -- writing remote_control.enable_metrics_subscription
# makes the gNB exit with "INI was not able to parse". One enable_json covers
# both, which was confirmed against the deployed binary, not just the source.
# The dashboard reads them over the same WebSocket the official GUI uses.
# The UE/PDU-session/DRB inactivity timer is opt-in for the same reason the
# metrics block is: unset, the rendered gnb.yaml stays byte-identical to the
# pre-existing output, so the 1x1 attach gate and the 2x1/4x1 matrix gates are
# provably untouched. It exists for the unbounded live demo, where the gNB's
# 120 s default (ocudu apps/units/o_cu_cp/cu_cp/cu_cp_unit_config.h) releases
# the UE a couple of minutes after the acceptance ping and leaves the RAN KPI
# panel with nothing to show. The bound is the one the gNB itself enforces
# (apps/units/o_cu_cp/cu_cp/cu_cp_unit_config_cli11_schema.cpp: range(1, 7200)).
UE_INACTIVITY_ENV = "OCUDU_NATIVE_UE_INACTIVITY_SECONDS"
UE_INACTIVITY_RANGE = (1, 7200)


def inactivity_timer_seconds(environ: dict[str, str] | None = None) -> int | None:
    """The configured inactivity timer, or None to leave the fixture alone."""

    raw = (environ if environ is not None else os.environ).get(UE_INACTIVITY_ENV, "").strip()
    if not raw:
        return None
    low, high = UE_INACTIVITY_RANGE
    if not raw.isdigit() or not low <= int(raw) <= high:
        legacy.fail(f"{UE_INACTIVITY_ENV} must be an integer in [{low}, {high}]")
    return int(raw)


def render_gnb_inactivity(source: str, seconds: int) -> str:
    """Set cu_cp.inactivity_timer inside the fixture's existing cu_cp block.

    Inserted rather than appended: a second top-level `cu_cp:` mapping is a
    duplicate key, not an override. The anchor carries the following line
    because `legacy.replace_exact` rejects a replacement the search token
    survives -- its guard cannot tell that from a substitution that failed.
    """

    low, high = UE_INACTIVITY_RANGE
    if not low <= seconds <= high:
        raise ValueError(f"inactivity timer must be in [{low}, {high}]")
    anchor = "cu_cp:\n  amf:\n"
    if source.count(anchor) != 1:
        legacy.fail("rank-1 gNB fixture cu_cp block is missing or ambiguous")
    return legacy.replace_exact(
        source, anchor, f"cu_cp:\n  inactivity_timer: {seconds}\n  amf:\n", 1,
        "gNB cu_cp inactivity timer",
    )


GNB_METRICS_ENV = "OCUDU_NATIVE_GNB_METRICS"
DEFAULT_METRICS_PORT = 8001
DEFAULT_DU_REPORT_PERIOD_MS = 1000


def metrics_enabled(environ: dict[str, str] | None = None) -> bool:
    raw = (environ if environ is not None else os.environ).get(GNB_METRICS_ENV, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def render_gnb_metrics(port: int = DEFAULT_METRICS_PORT,
                       report_period_ms: int = DEFAULT_DU_REPORT_PERIOD_MS) -> str:
    """The metrics + remote_control blocks appended to the rendered gNB config.

    Only the scheduler layers are enabled. enable_sched_ue is the one that
    carries the per-UE KPIs the dashboard shows (RNTI, CQI, RI, MCS, BLER,
    SINR/RSRP, BSR, TA, PHR); the other layers stay off so the metrics cost
    stays proportional to what is actually displayed.
    """

    if not 1 <= port <= 65535:
        raise ValueError("gNB metrics port must be in [1, 65535]")
    if report_period_ms <= 0:
        raise ValueError("du_report_period must be positive")
    return (
        "\n"
        "# Appended by render-sionna-rank1-configs.py because "
        f"{GNB_METRICS_ENV} is set.\n"
        "metrics:\n"
        "  enable_json: true\n"
        "  layers:\n"
        "    enable_sched: true\n"
        "    enable_sched_ue: true\n"
        "  periodicity:\n"
        f"    du_report_period: {report_period_ms}\n"
        "remote_control:\n"
        "  enabled: true\n"
        "  bind_addr: 127.0.0.1\n"
        f"  port: {port}\n"
    )


def render_topology(shape: LiveShape) -> str:
    gnb, ue = shape.gnb, shape.ue
    device_count = max(gnb.tx, gnb.rx)
    devices = "".join(
        f"  - id: {gnb.node_id}_p{index}\n"
        "    role: port\n"
        "    sample_rate_hz: 23040000\n"
        f"    tx_endpoint: tcp://127.0.0.1:{GNB_PORT_BASE + 2 * index}\n"
        f"    rx_endpoint: tcp://127.0.0.1:{GNB_PORT_BASE + 1 + 2 * index}\n"
        for index in range(device_count)
    )
    # The UE's device ids follow its own port count now, so a two-port handset
    # renders as ue0_p0 and ue0_p1 instead of being rejected.
    for index in range(max(ue.tx, ue.rx)):
        ue_tx, ue_rx = _ue_endpoints(index)
        devices += (
            f"  - id: {ue.node_id}_p{index}\n"
            "    role: port\n"
            "    sample_rate_hz: 23040000\n"
            f"    tx_endpoint: {ue_tx}\n"
            f"    rx_endpoint: {ue_rx}\n"
        )
    radio_nodes = "".join(
        f"  - id: {port.node_id}\n"
        "    tx_ports:\n"
        f"{_port_lines(port.tx, port.node_id)}"
        "    rx_ports:\n"
        f"{_port_lines(port.rx, port.node_id)}"
        for port in shape.radio_nodes
    )
    links = "".join(
        f"  - from: {link.source}\n"
        f"    to: {link.destination}\n"
        f"    model: {link.model}\n"
        for link in shape.channel_links
    )
    model_ids = tuple(dict.fromkeys(link.model for link in shape.channel_links))
    models = "".join(
        f"  {model_id}:\n"
        "    chain:\n"
        "      - type: tdl\n"
        "        taps:\n"
        "          - delay_samples: 0.0\n"
        "            gain_db: -100.0\n"
        "            phase_rad: 0.0\n"
        for model_id in model_ids
    )
    return (
        "# Generated from the Sionna scenario by render-sionna-rank1-configs.py.\n"
        "# Port ordering is the Broker matrix ordering; dynamic matrix_profile_swap\n"
        "# replaces the quiet startup TDL atomically before either radio starts.\n"
        "runtime:\n"
        "  backend: cuda\n"
        "  gpu_device: 0\n"
        "  batch_samples: 23040\n"
        "  queue_samples: 2457600\n"
        "devices:\n"
        f"{devices}"
        "radio_nodes:\n"
        f"{radio_nodes}"
        "links:\n"
        f"{links}"
        "models:\n"
        f"{models}"
    )


def render_srsue(source: str, log_dir: Path, shape: LiveShape) -> str:
    """The legacy single-port srsUE config, widened to the scenario's ports."""

    rendered = legacy.render_srsue(source, log_dir)
    ports = max(shape.ue.tx, shape.ue.rx)
    if ports == 1:
        return rendered
    tx_args = [f"tx_port{index}={_ue_endpoints(index)[0]}" for index in range(shape.ue.tx)]
    rx_args = [f"rx_port{index}={_ue_endpoints(index)[1]}" for index in range(shape.ue.rx)]
    rendered = legacy.replace_exact(
        rendered,
        "device_args = tx_port=tcp://127.0.0.1:2101,rx_port=tcp://127.0.0.1:2100,base_srate=23.04e6\n",
        "device_args = " + ",".join((*tx_args, *rx_args, "base_srate=23.04e6")) + "\n",
        1,
        "srsUE device args",
    )
    return legacy.replace_exact(
        rendered, "nof_antennas = 1\n", f"nof_antennas = {ports}\n", 1,
        "srsUE antenna count",
    )


def _render_gnb_document(source: str, log_dir: Path, shape: LiveShape) -> str:
    rendered = render_gnb(source, log_dir, shape)
    seconds = inactivity_timer_seconds()
    if seconds is not None:
        rendered = render_gnb_inactivity(rendered, seconds)
    return rendered + (render_gnb_metrics() if metrics_enabled() else "")


def _scenario_fixture(tmp: Path, nodes: dict, links: list) -> Path:
    path = tmp / "scenario.json"
    path.write_text(
        json.dumps({"name": "fixture", "nodes": nodes, "links": links}),
        encoding="utf-8",
    )
    return path


def _shape_self_test() -> None:
    """The scenario shape is read, not assumed."""

    import tempfile

    def array(count: int) -> dict:
        return {"rows": 1, "cols": count}

    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)

        # More than two links is fine: extras ride alongside the gNB<->UE pair
        # the gate measures.
        path = _scenario_fixture(
            tmp,
            {"gnb0": {"array": array(4)}, "ue0": {"array": array(1)}},
            [
                {"from": "gnb0", "to": "ue0", "direction": "downlink", "model": "sionna_rt"},
                {"from": "ue0", "to": "gnb0", "direction": "uplink", "model": "sionna_rt"},
                {"from": "gnb0", "to": "ue0", "direction": "downlink", "model": "probe"},
            ],
        )
        shape = load_live_shape(path)
        assert len(shape.links) == 3, shape.links
        topology = render_topology(shape)
        assert topology.count("  - from: gnb0\n") == 2
        assert "  probe:\n" in topology and "  sionna_rt:\n" in topology

        # A two-port UE renders its own devices and widens the srsUE config.
        path = _scenario_fixture(
            tmp,
            {"gnb0": {"array": array(2)}, "ue0": {"array": array(2)}},
            [
                {"from": "gnb0", "to": "ue0", "direction": "downlink", "model": "m"},
                {"from": "ue0", "to": "gnb0", "direction": "uplink", "model": "m"},
            ],
        )
        shape = load_live_shape(path)
        assert (shape.ue_tx, shape.ue_rx) == (2, 2)
        topology = render_topology(shape)
        assert "  - id: ue0_p1\n" in topology
        assert "      - ue0_p1\n" in topology
        assert "tx_endpoint: tcp://127.0.0.1:2103\n" in topology
        source = (
            Path(__file__).resolve().parents[2]
            / "use_cases/configs/ran/srsue/srsue_zmq_legacy_1x1.conf.in"
        ).read_text(encoding="utf-8")
        conf = render_srsue(source, Path("/tmp"), shape)
        assert "nof_antennas = 2\n" in conf, conf
        assert "tx_port0=tcp://127.0.0.1:2101" in conf and "tx_port1=tcp://127.0.0.1:2103" in conf
        assert "rx_port0=tcp://127.0.0.1:2100" in conf and "rx_port1=tcp://127.0.0.1:2102" in conf
        # A single-port UE must still render the untouched legacy line.
        single = load_live_shape(_scenario_fixture(
            tmp,
            {"gnb0": {"array": array(4)}, "ue0": {"array": array(1)}},
            [
                {"from": "gnb0", "to": "ue0", "direction": "downlink", "model": "m"},
                {"from": "ue0", "to": "gnb0", "direction": "uplink", "model": "m"},
            ],
        ))
        assert render_srsue(source, Path("/tmp"), single) == legacy.render_srsue(source, Path("/tmp"))

        # What the native launcher genuinely cannot do is still refused, and
        # the message has to say which launcher can.
        for nodes, links, expected in (
            (
                {"gnb0": {"array": array(4)}, "ue0": {"array": array(1)}, "ue1": {"array": array(1)}},
                [
                    {"from": "gnb0", "to": "ue0", "direction": "downlink", "model": "m"},
                    {"from": "ue0", "to": "gnb0", "direction": "uplink", "model": "m"},
                ],
                "one UE",
            ),
            (
                {"gnb0": {"array": array(3)}, "ue0": {"array": array(1)}},
                [
                    {"from": "gnb0", "to": "ue0", "direction": "downlink", "model": "m"},
                    {"from": "ue0", "to": "gnb0", "direction": "uplink", "model": "m"},
                ],
                "must resolve to 1, 2, 4 ports",
            ),
            (
                {"gnb0": {"array": array(4)}, "ue0": {"array": array(4)}},
                [
                    {"from": "gnb0", "to": "ue0", "direction": "downlink", "model": "m"},
                    {"from": "ue0", "to": "gnb0", "direction": "uplink", "model": "m"},
                ],
                "at most 2 antenna ports",
            ),
            (
                {"gnb0": {"array": array(4)}, "ue0": {"array": array(1)}},
                [{"from": "gnb0", "to": "ue0", "direction": "downlink", "model": "m"}],
                "links",
            ),
        ):
            try:
                load_live_shape(_scenario_fixture(tmp, nodes, links))
            except ValueError as error:
                assert expected in str(error), (expected, str(error))
            else:
                raise AssertionError(f"scenario should have been refused: {expected}")


def self_test() -> None:
    _shape_self_test()
    shape = LiveShape(2, 4, 1, 1, "dl_dynamic", "ul_dynamic")
    topology = render_topology(shape)
    assert topology.count("      - gnb0_p0\n") == 2
    assert "      - gnb0_p3\n" in topology
    assert "fixed_mimo" not in topology
    assert "model: dl_dynamic" in topology and "model: ul_dynamic" in topology
    # Every advertised port count must actually render. 4 is the fixture's
    # own value, so it exercises the no-op replacement path.
    gnb_source = (
        Path(__file__).resolve().parents[2]
        / "use_cases/configs/ran/ocudu/native/gnb_zmq_b210_fdd_4t4r_rank1_srsue.yaml"
    ).read_text(encoding="utf-8")
    for ports in sorted(SUPPORTED_GNB_PORTS):
        rendered = render_gnb(
            gnb_source, Path("/tmp"), LiveShape(ports, ports, 1, 1, "dl", "ul")
        )
        assert f"  nof_antennas_dl: {ports}\n" in rendered
        assert f"  nof_antennas_ul: {ports}\n" in rendered
        assert f"tx_port{ports - 1}=" in rendered
        assert f"tx_port{ports}=" not in rendered
    assert _array_count({"tx_array": {"rows": 2, "cols": 2}}, "tx_array", "node") == 4
    try:
        _array_count({"rx_array": {"rows": 0, "cols": 1}}, "rx_array", "node")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid Sionna array dimensions were accepted")
    # Metrics stay out of the rendered config unless explicitly enabled,
    # which is what keeps the existing gates byte-identical.
    assert metrics_enabled({"OCUDU_NATIVE_GNB_METRICS": "1"})
    assert metrics_enabled({"OCUDU_NATIVE_GNB_METRICS": "true"})
    assert not metrics_enabled({})
    assert not metrics_enabled({"OCUDU_NATIVE_GNB_METRICS": "0"})
    metrics_block = render_gnb_metrics()
    for token in ("enable_json: true", "enable_sched_ue: true", "port: 8001"):
        assert token in metrics_block, token
    # Not a YAML key -- the gNB refuses to start if it appears.
    assert "enable_metrics_subscription" not in metrics_block
    assert "enable_rlc" not in metrics_block, "only scheduler layers are enabled"
    # The inactivity timer is opt-in on the same terms as the metrics block:
    # with the variable unset the document must be byte-identical.
    assert inactivity_timer_seconds({}) is None
    assert inactivity_timer_seconds({"OCUDU_NATIVE_UE_INACTIVITY_SECONDS": "7200"}) == 7200
    assert inactivity_timer_seconds({"OCUDU_NATIVE_UE_INACTIVITY_SECONDS": " "}) is None
    plain = render_gnb(gnb_source, Path("/tmp"), shape)
    assert "inactivity_timer" not in plain
    timed = render_gnb_inactivity(plain, 7200)
    assert timed.count("cu_cp:\n  inactivity_timer: 7200\n") == 1
    # One cu_cp block, not two: a duplicate mapping key is not an override.
    assert timed.count("\ncu_cp:") == plain.count("\ncu_cp:") == 1
    assert timed.replace("  inactivity_timer: 7200\n", "") == plain
    for bad in ("0", "7201", "-1", "600s"):
        try:
            inactivity_timer_seconds({"OCUDU_NATIVE_UE_INACTIVITY_SECONDS": bad})
        except ValueError:
            continue
        raise AssertionError(f"accepted out-of-range inactivity timer: {bad}")
    print("event=native_sionna_rank1_config_renderer_self_test result=pass")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--native-root", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--log-dir", type=Path)
    parser.add_argument("--scenario-config", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    render_args = (args.repo_root, args.native_root, args.output_dir, args.log_dir, args.scenario_config)
    if args.self_test:
        if any(render_args):
            parser.error("--self-test cannot be combined with render arguments")
        self_test()
        return 0
    if not all(render_args):
        parser.error("render mode requires repo/native/output/log/scenario arguments")

    repo_root = args.repo_root.resolve(strict=True)
    native_root = args.native_root.resolve(strict=True)
    scenario = args.scenario_config.resolve(strict=True)
    output_dir = legacy.safe_output_directory(args.output_dir)
    log_dir = legacy.safe_log_directory(args.log_dir)
    if repo_root != args.repo_root or native_root != args.native_root or scenario != args.scenario_config:
        legacy.fail("repo, native, and scenario paths must already be canonical")
    shape = load_live_shape(scenario)
    gnb_source = legacy.read_regular(
        repo_root / "use_cases/configs/ran/ocudu/native/gnb_zmq_b210_fdd_4t4r_rank1_srsue.yaml",
        "rank-1 4T4R gNB fixture",
    )
    open5gs_source = legacy.read_regular(
        native_root / "src/ocudu/docker/open5gs/open5gs-5gc.yml",
        "pinned OCUDU Open5GS template",
    )
    srsue_source = legacy.read_regular(
        repo_root / "use_cases/configs/ran/srsue/srsue_zmq_legacy_1x1.conf.in",
        "native srsUE template",
    )
    subscriber_source = legacy.read_regular(
        repo_root / "use_cases/configs/ran/open5gs/subscriber-legacy-1x1.csv",
        "native subscriber template",
    )
    rendered = {
        "gnb.yaml": _render_gnb_document(gnb_source, log_dir, shape),
        "topology.yaml": render_topology(shape),
        "open5gs.yaml": legacy.render_open5gs(open5gs_source, native_root),
        "srsue.conf": render_srsue(srsue_source, log_dir, shape),
        "subscriber.csv": legacy.validate_subscriber(subscriber_source),
    }
    for name, text in rendered.items():
        if legacy.PLACEHOLDER_RE.search(text):
            legacy.fail(f"unresolved placeholder in {name}")
        legacy.write_new(output_dir / name, text)
    metadata = {
        "scenario": str(scenario),
        "gnb_tx_ports": shape.gnb_tx,
        "gnb_rx_ports": shape.gnb_rx,
        "ue_tx_ports": shape.ue_tx,
        "ue_rx_ports": shape.ue_rx,
        "downlink_model": shape.downlink_model,
        "uplink_model": shape.uplink_model,
    }
    legacy.write_new(output_dir / "sionna-rank1-shape.json", json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    print(
        "event=native_sionna_rank1_configs_rendered "
        f"gnb_tx={shape.gnb_tx} gnb_rx={shape.gnb_rx} ue_tx={shape.ue_tx} ue_rx={shape.ue_rx} "
        f'output_dir="{output_dir}"'
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnicodeError, ValueError) as error:
        print(f"config rendering failed: {error}", file=sys.stderr)
        raise SystemExit(2)
