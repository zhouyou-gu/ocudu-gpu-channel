#!/usr/bin/env python3
"""Render native multi-UE configs from a Sionna scenario.

This is to `render-multi-ue-configs.py` what `render-sionna-rank1-configs.py`
is to the 1x1 renderer: the same gNB, Open5GS, subscriber and srsUE rendering,
but the broker topology is *generated* from the scenario instead of validating
a checked-in TDL file. That is the whole point — a Sionna run needs the broker
to carry the link ids and model names the bridge will send profile swaps for,
and those come from the scenario.

The multi-UE gate's own limits still apply, and they are the launcher's, not
this file's: one OCUDU gNB with a single ZMQ port pair, and exactly as many
single-port srsUEs as `render-multi-ue-configs.py` has UE records for, each
with its own IMSI, netns and address.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MODEL_ID_RE = re.compile(r"[A-Za-z0-9_-]+")
# The multi-UE gNB fixture carries one unnumbered ZMQ port pair, so a scenario
# asking for an array cannot be rendered against it. The rank-1 gate is the
# multi-antenna path.
SUPPORTED_GNB_PORTS = frozenset((1,))
DEFAULT_UE_COUNT = 2

# Receiver noise floor for the Sionna links. Sionna's taps carry the
# scene's path gain (+ the bridge's gain offset, 60 dB by default), so a UE
# behind a pillar receives ~25 dB less than one in line of sight -- but an
# `awgn` step with `snr_db` sizes its noise against the *faded* signal and
# would give both the same SNR. The floor therefore has to be absolute:
# `noise_power` on an `rx_model` applied once to each node's summed receive
# signal. It is derived from the transmit power the fixtures put on the wire
# and the SNR a unit-gain (0 dB tap) link should see:
#   noise_power = tx_power / 10^(awgn_snr_db / 10)
# TX_POWER_* are mean |x|^2 over the *active* samples of a wire capture of
# the pinned fixtures (idle slots excluded; scripts/native/wire-capture-power.py),
# in the IQ float units the ZMQ radios exchange. Measured on the DGX Spark
# multi-UE run 20260929T130021Z: the OCUDU gNB
# (gnb_zmq_b210_fdd_srsue.yaml, default amplitude control) puts 1.12e-2
# (-19.5 dB, peak amplitude 0.39) on the wire; srsUE (srsue_zmq_multi_ue.conf.in,
# whose [rf] tx_gain = 50 dB the ZMQ radio applies numerically, peak 313)
# 2.8e4-3.3e4 (+44..45 dB) while sending PUCCH/SRS/ping-sized PUSCH.
# Under traffic (X6, docs/reports/experiments/x6-gnb-realtime-traffic/README.md, runs 20261001T114501Z
# and T115125Z, two UEs at 20 Mbit/s UL, capture at run-second 20): the gNB's
# PDSCH-filled slots measure 3.4e-3..3.7e-3 (-24.7..-24.3 dB) and a UE sending
# full-rate PUSCH 9.7e3..9.9e3 (+39.9 dB), both ~5 dB below the idle-regime
# values above, while a UE sending only control bursts still measures 3.25e4.
# The UL/DL ratio -- all ue_tx_scale_db uses -- is 64.6 dB vs 64.3 dB here, so
# the constants stay at the idle-regime values they document; moving only
# TX_POWER_UL to the traffic value would skew the UE scale by +4.9 dB, and
# moving both would shift the absolute floor the SNR calibration rests on.
TX_POWER_DL = 1.12e-2
TX_POWER_UL = 3.0e4
# Emulated transmit powers. The wire levels above are software scales (srsUE
# multiplies by its 50 dB tx_gain numerically, the gNB sits at -19.8 dB), so
# the uplink arrives 64 dB hotter than the downlink although a 23 dBm UE is
# ~7 dB *weaker* than a 30 dBm small cell. The broker's per-device
# `tx_scale_db` brings every UE port onto the gNB's scale:
#   ue_tx_scale_db = (UL_dBm - DL_dBm) - 10 log10(TX_POWER_UL / TX_POWER_DL)
# so that "0 dB tap" means the same emitted power for both directions and a
# UE<->UE edge (where it is physical, i.e. TDD) is no longer an 80 dB jammer.
# srsUE cannot do this itself: a negative [rf] tx_gain means "automatic"
# (measured: -21 -> 40 dB applied) and its floor is 0 dB.
TX_POWER_DL_DBM = 30.0
TX_POWER_UL_DBM = 23.0
UL_POWER_OFFSET_DB = TX_POWER_UL_DBM - TX_POWER_DL_DBM


def ue_tx_scale_db(tx_power_dl: float = TX_POWER_DL, tx_power_ul: float = TX_POWER_UL,
                   ul_power_offset_db: float = UL_POWER_OFFSET_DB) -> float:
    """Per-UE-port `tx_scale_db` that puts the uplink on the downlink's scale."""

    for label, value in (("tx_power_dl", tx_power_dl), ("tx_power_ul", tx_power_ul)):
        if not (math.isfinite(value) and value > 0.0):
            raise ValueError(f"{label} must be a positive finite power: {value}")
    if not math.isfinite(ul_power_offset_db):
        raise ValueError(f"ul_power_offset_db must be finite: {ul_power_offset_db}")
    return ul_power_offset_db - 10.0 * math.log10(tx_power_ul / tx_power_dl)


def load_multi_ue_renderer():
    path = Path(__file__).resolve().with_name("render-multi-ue-configs.py")
    spec = importlib.util.spec_from_file_location("render_multi_ue_configs", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load multi-UE renderer module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


legacy = load_multi_ue_renderer()


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
    gnb: NodePorts
    ues: tuple[NodePorts, ...]
    links: tuple[ShapeLink, ...]

    @property
    def nodes(self) -> tuple[NodePorts, ...]:
        return (self.gnb, *self.ues)


def _object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object")
    return value


def _array_count(node: dict[str, Any], key: str, where: str) -> int:
    raw = node.get(key, node.get("array"))
    array = _object({} if raw is None else raw, f"{where}.{key}")
    rows = array.get("rows", 1)
    cols = array.get("cols", 1)
    for name, value in (("rows", rows), ("cols", cols)):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{where}.{key}.{name} must be a positive integer")
    return rows * cols


def gate_ues(ue_count: int = DEFAULT_UE_COUNT) -> tuple:
    """The UE records the gate will start for this UE count.

    `render-multi-ue-configs.py` lists every record it knows (four since the
    quad option, 9043202); the gate starts only the first OCUDU_NATIVE_MUE_UE_COUNT
    of them, so a scenario is checked against that slice and not the table.
    """

    if ue_count not in legacy.LAYOUTS:
        raise ValueError(
            f"unsupported UE count {ue_count}; the gate knows "
            f"{', '.join(str(n) for n in sorted(legacy.LAYOUTS))}"
        )
    return tuple(legacy.UES[:ue_count])


def load_live_shape(path: Path, ue_count: int = DEFAULT_UE_COUNT) -> LiveShape:
    """Read the runtime shape from a Sionna scenario, or say why it cannot run."""

    ues_wanted = gate_ues(ue_count)

    try:
        root = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load Sionna scenario {path}: {error}") from error
    root = _object(root, "scenario")
    raw_nodes = _object(root.get("nodes"), "scenario.nodes")

    gnb_ids = [node_id for node_id in raw_nodes if node_id.startswith("gnb")]
    ue_ids = [node_id for node_id in raw_nodes if not node_id.startswith("gnb")]
    if len(gnb_ids) != 1:
        raise ValueError(
            "the native multi-UE gate builds one OCUDU gNB; scenario names "
            f"{len(gnb_ids)}: {', '.join(sorted(gnb_ids)) or 'none'}"
        )
    expected = [ue["device_id"] for ue in ues_wanted]
    if sorted(ue_ids) != sorted(expected):
        raise ValueError(
            "the native multi-UE gate starts one srsUE per UE record it is "
            f"asked for (OCUDU_NATIVE_MUE_UE_COUNT={ue_count}), so the scenario "
            f"must name exactly {', '.join(expected)}; it names "
            f"{', '.join(sorted(ue_ids)) or 'none'}"
        )

    def ports(node_id: str) -> NodePorts:
        node = _object(raw_nodes[node_id], f"nodes.{node_id}")
        return NodePorts(
            node_id,
            _array_count(node, "tx_array", f"nodes.{node_id}"),
            _array_count(node, "rx_array", f"nodes.{node_id}"),
        )

    gnb = ports(gnb_ids[0])
    if gnb.tx not in SUPPORTED_GNB_PORTS or gnb.rx not in SUPPORTED_GNB_PORTS:
        raise ValueError(
            "the multi-UE gNB fixture carries a single ZMQ port pair, so "
            f"nodes.{gnb.node_id} must resolve to 1 port; it asks for "
            f"tx={gnb.tx}, rx={gnb.rx}. Use the rank-1 gate for an array."
        )
    ues = tuple(ports(ue["device_id"]) for ue in ues_wanted)
    for ue in ues:
        if (ue.tx, ue.rx) != (1, 1):
            raise ValueError(
                f"native srsUE is single-port; nodes.{ue.node_id} asks for "
                f"tx={ue.tx}, rx={ue.rx}"
            )

    raw_links = root.get("links")
    if not isinstance(raw_links, list) or not raw_links:
        raise ValueError("scenario.links must be a non-empty array")
    known = {port.node_id for port in (gnb, *ues)}
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

    # Each UE has to have both directions against the gNB: that pair is what
    # the RRC/PDU/ping verdict is measured on.
    for ue in ues:
        for source, destination in ((gnb.node_id, ue.node_id), (ue.node_id, gnb.node_id)):
            if not any(l.source == source and l.destination == destination for l in links):
                raise ValueError(f"scenario is missing the {source}->{destination} link")
    return LiveShape(gnb, ues, tuple(links))


def rx_noise_powers(shape: LiveShape, awgn_snr_db: float | None,
                    tx_power_dl: float = TX_POWER_DL,
                    tx_power_ul: float = TX_POWER_UL,
                    ue_scale_db: float = 0.0) -> dict[str, float]:
    """Absolute receiver noise per node for a reference SNR, or {} for none.

    The UEs hear the gNB (downlink transmit power); the gNB hears the UEs
    (uplink transmit power AFTER the broker's per-UE `tx_scale_db`,
    `ue_scale_db`). Both floors give a unit-gain link `awgn_snr_db`.
    """

    if awgn_snr_db is None:
        return {}
    if not (math.isfinite(awgn_snr_db) and -30.0 <= awgn_snr_db <= 120.0):
        raise ValueError(f"awgn_snr_db must be a finite value in [-30, 120]: {awgn_snr_db}")
    for label, value in (("tx_power_dl", tx_power_dl), ("tx_power_ul", tx_power_ul)):
        if not (math.isfinite(value) and value > 0.0):
            raise ValueError(f"{label} must be a positive finite power: {value}")
    if not math.isfinite(ue_scale_db):
        raise ValueError(f"ue_scale_db must be finite: {ue_scale_db}")
    ratio = 10.0 ** (awgn_snr_db / 10.0)
    floors = {shape.gnb.node_id: tx_power_ul * 10.0 ** (ue_scale_db / 10.0) / ratio}
    for ue in shape.ues:
        floors[ue.node_id] = tx_power_dl / ratio
    return floors


def render_topology(shape: LiveShape, rx_noise: dict[str, float] | None = None,
                    gnb_ports: tuple[int, int] = (2000, 2001),
                    ue_scale_db: float | None = None) -> str:
    """Generate the broker topology the scenario describes.

    Endpoints follow `render-multi-ue-configs.py`'s UE table and the gNB's
    fixed 2000/2001 pair (`gnb_ports`; a second cell uses 2010/2011), so the ports the gate pre-checks and the srsUE configs use are
    the ones the broker binds. REP sockets bind loopback: every peer is in this
    namespace.
    """

    endpoints = {shape.gnb.node_id: gnb_ports}
    for ue in legacy.UES:
        endpoints[ue["device_id"]] = (ue["tx_port"], ue["rx_port"])

    # A device id may not equal a radio node id — the broker rejects the
    # config outright ("radio node id collides with a device id"). Ports carry
    # the `_p0` suffix the rank-1 topology uses, and the node keeps the bare
    # name the bridge addresses its links by.
    rx_noise = dict(rx_noise or {})
    unknown = set(rx_noise) - {node.node_id for node in shape.nodes}
    if unknown:
        raise ValueError(f"rx noise names nodes the scenario lacks: {', '.join(sorted(unknown))}")
    devices = ""
    for node in shape.nodes:
        tx_port, rx_port = endpoints[node.node_id]
        devices += (
            f"  - id: {node.node_id}_p0\n"
            "    role: port\n"
            "    sample_rate_hz: 23040000\n"
            f"    tx_endpoint: tcp://127.0.0.1:{tx_port}\n"
            f"    rx_endpoint: tcp://127.0.0.1:{rx_port}\n"
        )
        # Carrier labels (FDD band 3 fixture): a port only hears its own
        # carrier, so the broker refuses an edge from a UE TX (uplink carrier)
        # into another UE RX (downlink carrier) at startup instead of summing a
        # physically non-existent ~80 dB jammer into the victim UE.
        if node.node_id == shape.gnb.node_id:
            devices += "    tx_carrier: n3-dl\n    rx_carrier: n3-ul\n"
        else:
            devices += "    tx_carrier: n3-ul\n    rx_carrier: n3-dl\n"
            # Uplink wire level -> emitted power (see ue_tx_scale_db).
            if ue_scale_db is not None:
                devices += f"    tx_scale_db: {ue_scale_db:.3f}\n"
        # The receiver model is applied once to the port's summed receive
        # signal, after every incoming link, so the floor is per node and a
        # profile_swap (which only replaces a link's leading tdl) leaves it.
        if node.node_id in rx_noise:
            devices += f"    rx_model: rx_noise_{node.node_id}\n"
    radio_nodes = "".join(
        f"  - id: {node.node_id}\n"
        "    tx_ports:\n"
        f"      - {node.node_id}_p0\n"
        "    rx_ports:\n"
        f"      - {node.node_id}_p0\n"
        for node in shape.nodes
    )
    links = "".join(
        f"  - from: {link.source}\n"
        f"    to: {link.destination}\n"
        f"    model: {link.model}\n"
        for link in shape.links
    )
    # One quiet TDL per model name. The Sionna bridge replaces each of them
    # atomically with a matrix_profile_swap before either radio starts, which
    # is why the startup taps are -100 dB rather than something plausible.
    models = "".join(
        f"  {model_id}:\n"
        "    chain:\n"
        "      - type: tdl\n"
        "        taps:\n"
        "          - delay_samples: 0.0\n"
        "            gain_db: -100.0\n"
        "            phase_rad: 0.0\n"
        for model_id in dict.fromkeys(link.model for link in shape.links)
    )
    models += "".join(
        f"  rx_noise_{node_id}:\n"
        "    chain:\n"
        "      - type: awgn\n"
        f"        noise_power: {rx_noise[node_id]:.6e}\n"
        for node_id in (node.node_id for node in shape.nodes if node.node_id in rx_noise)
    )
    return (
        "# Generated from the Sionna scenario by "
        "render-sionna-multi-ue-configs.py.\n"
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


def self_test() -> None:
    import tempfile

    def scenario(nodes: dict, links: list) -> str:
        return json.dumps({"name": "t", "nodes": nodes, "links": links})

    one = {"rows": 1, "cols": 1}
    good_nodes = {
        "gnb0": {"array": one},
        "ue0": {"array": one},
        "ue1": {"array": one},
    }
    good_links = [
        {"from": "gnb0", "to": "ue0", "direction": "downlink", "model": "sionna_rt"},
        {"from": "gnb0", "to": "ue1", "direction": "downlink", "model": "sionna_rt"},
        {"from": "ue0", "to": "gnb0", "direction": "uplink", "model": "sionna_rt"},
        {"from": "ue1", "to": "gnb0", "direction": "uplink", "model": "sionna_rt"},
    ]
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "s.json"

        path.write_text(scenario(good_nodes, good_links), encoding="utf-8")
        shape = load_live_shape(path)
        assert [ue.node_id for ue in shape.ues] == ["ue0", "ue1"], shape.ues
        topology = render_topology(shape)
        for token in (
            "  - id: gnb0_p0\n", "  - id: ue0_p0\n", "  - id: ue1_p0\n",
            "  - id: gnb0\n", "  - id: ue0\n", "  - id: ue1\n",
            "      - ue1_p0\n",
            "    tx_endpoint: tcp://127.0.0.1:2000\n",
            "    tx_endpoint: tcp://127.0.0.1:2101\n",
            "    tx_endpoint: tcp://127.0.0.1:2103\n",
            "  sionna_rt:\n",
        ):
            assert token in topology, token
        assert topology.count("  - from: ") == 4, topology
        # Every REP socket the gate pre-checks must be bound by the broker.
        for port in (2001, 2100, 2102):
            assert f"rx_endpoint: tcp://127.0.0.1:{port}\n" in topology, port

        # A crosstalk link is carried through: the broker supports it, and the
        # scenario is the authority on which links exist.
        path.write_text(
            scenario(good_nodes, good_links
                     + [{"from": "ue0", "to": "ue1", "direction": "crosstalk",
                         "model": "sionna_rt"}]),
            encoding="utf-8",
        )
        assert render_topology(load_live_shape(path)).count("  - from: ") == 5

        # Receiver noise floor: one rx_model per node, absolute noise_power,
        # the gNB sized from the uplink power and the UEs from the downlink.
        shape = load_live_shape(path)
        floors = rx_noise_powers(shape, 20.0, tx_power_dl=1.0, tx_power_ul=0.1)
        assert abs(floors["ue0"] - 1e-2) < 1e-9 and abs(floors["ue1"] - 1e-2) < 1e-9, floors
        assert abs(floors["gnb0"] - 1e-3) < 1e-9, floors
        noisy = render_topology(shape, floors)
        for token in (
            "    rx_model: rx_noise_gnb0\n", "    rx_model: rx_noise_ue0\n",
            "    rx_model: rx_noise_ue1\n", "  rx_noise_gnb0:\n",
            "        noise_power: 1.000000e-03\n", "        noise_power: 1.000000e-02\n",
        ):
            assert token in noisy, token
        assert noisy.count("type: awgn") == 3, noisy
        assert rx_noise_powers(shape, None) == {}
        # Uplink scale: 64.3 dB wire gap plus the 7 dB power difference.
        scale = ue_tx_scale_db()
        assert abs(scale - (-7.0 - 10 * math.log10(TX_POWER_UL / TX_POWER_DL))) < 1e-9, scale
        assert -72.0 < scale < -70.0, scale
        scaled = rx_noise_powers(shape, 20.0, tx_power_dl=1.0, tx_power_ul=0.1, ue_scale_db=-10.0)
        assert abs(scaled["gnb0"] - 1e-4) < 1e-12, scaled
        with_scale = render_topology(shape, floors, ue_scale_db=scale)
        assert with_scale.count("tx_scale_db:") == 2, with_scale
        assert "tx_scale_db" not in render_topology(shape, floors), "no scale unless asked"
        assert "rx_model" not in render_topology(shape), "no floor unless asked"
        try:
            render_topology(shape, {"ue9": 1e-3})
        except ValueError as error:
            assert "ue9" in str(error), error
        else:
            raise AssertionError("unknown rx noise node must be refused")

        # The four-UE slice: the scenario has to name ue0..ue3, and a two-UE
        # scenario is refused against it just as a four-UE one is against two.
        four_nodes = {**good_nodes, "ue2": {"array": one}, "ue3": {"array": one}}
        four_links = good_links + [
            {"from": "gnb0", "to": "ue2", "model": "sionna_rt"},
            {"from": "gnb0", "to": "ue3", "model": "sionna_rt"},
            {"from": "ue2", "to": "gnb0", "model": "sionna_rt"},
            {"from": "ue3", "to": "gnb0", "model": "sionna_rt"},
        ]
        path.write_text(scenario(four_nodes, four_links), encoding="utf-8")
        assert [ue.node_id for ue in load_live_shape(path, 4).ues] == ["ue0", "ue1", "ue2", "ue3"]
        assert "tx_endpoint: tcp://127.0.0.1:2107\n" in render_topology(load_live_shape(path, 4))
        for count, expected in ((2, "must name exactly ue0, ue1"), (3, "unsupported UE count")):
            try:
                load_live_shape(path, count)
            except ValueError as error:
                assert expected in str(error), (expected, str(error))
            else:
                raise AssertionError(f"should have been refused: {expected}")

        for nodes, links, expected in (
            ({"gnb0": {"array": one}, "ue0": {"array": one}}, good_links,
             "must name exactly ue0, ue1"),
            ({**good_nodes, "gnb1": {"array": one}}, good_links,
             "builds one OCUDU gNB"),
            ({**good_nodes, "gnb0": {"array": {"rows": 1, "cols": 4}}}, good_links,
             "single ZMQ port pair"),
            ({**good_nodes, "ue1": {"array": {"rows": 1, "cols": 2}}}, good_links,
             "single-port"),
            (good_nodes, good_links[:3], "missing the ue1->gnb0 link"),
        ):
            path.write_text(scenario(nodes, links), encoding="utf-8")
            try:
                load_live_shape(path)
            except ValueError as error:
                assert expected in str(error), (expected, str(error))
            else:
                raise AssertionError(f"scenario should have been refused: {expected}")
    print("event=native_sionna_multi_ue_config_renderer_self_test result=pass")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--native-root", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--log-dir", type=Path)
    parser.add_argument("--scenario-config", type=Path)
    parser.add_argument("--ue-count", type=int, choices=sorted(legacy.LAYOUTS),
                        default=DEFAULT_UE_COUNT)
    # Receiver noise floor (see rx_noise_powers). Omitted = no floor, the
    # topology this renderer always produced.
    parser.add_argument("--awgn-snr-db", type=float, default=None,
                        help="SNR a unit-gain link sees against the absolute rx noise floor")
    parser.add_argument("--tx-power-dl", type=float, default=TX_POWER_DL,
                        help="gNB transmit power on the wire, mean |x|^2 of active samples")
    parser.add_argument("--tx-power-ul", type=float, default=TX_POWER_UL,
                        help="srsUE transmit power on the wire, mean |x|^2 of active samples")
    parser.add_argument("--ul-power-offset-db", type=float, default=UL_POWER_OFFSET_DB,
                        help="emitted UE power relative to the gNB, dB (default 23 dBm - 30 dBm)")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    render_args = (
        args.repo_root, args.native_root, args.output_dir, args.log_dir,
        args.scenario_config,
    )
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
    if (repo_root != args.repo_root or native_root != args.native_root
            or scenario != args.scenario_config):
        legacy.fail("repo, native, and scenario paths must already be canonical")

    shape = load_live_shape(scenario, args.ue_count)
    ues = gate_ues(args.ue_count)
    try:
        ue_scale = ue_tx_scale_db(args.tx_power_dl, args.tx_power_ul, args.ul_power_offset_db)
        rx_noise = rx_noise_powers(shape, args.awgn_snr_db, args.tx_power_dl, args.tx_power_ul,
                                   ue_scale)
    except ValueError as error:
        legacy.fail(str(error))
    gnb_source = legacy.read_regular(
        repo_root / "use_cases/configs/ran/ocudu/docker/gnb_zmq_b210_fdd_srsue.yaml", "gNB fixture"
    )
    open5gs_source = legacy.read_regular(
        native_root / "src/ocudu/docker/open5gs/open5gs-5gc.yml",
        "pinned OCUDU Open5GS template",
    )
    srsue_source = legacy.read_regular(
        repo_root / "use_cases/configs/ran/srsue/srsue_zmq_multi_ue.conf.in",
        "native srsUE template",
    )
    _, subscriber_path, _ = legacy.LAYOUTS[args.ue_count]
    subscriber_source = legacy.read_regular(
        repo_root / subscriber_path, "native subscriber template",
    )
    rendered = {
        "gnb.yaml": legacy.render_gnb(gnb_source, log_dir),
        "topology.yaml": render_topology(shape, rx_noise, ue_scale_db=ue_scale),
        "open5gs.yaml": legacy.render_open5gs(open5gs_source, native_root),
        "subscriber.csv": legacy.validate_subscriber(subscriber_source, ues),
    }
    for ue in ues:
        rendered[f"srsue-{ue['device_id']}.conf"] = legacy.render_srsue(
            srsue_source, ue, log_dir
        )
    for name, text in rendered.items():
        if legacy.PLACEHOLDER_RE.search(text):
            legacy.fail(f"unresolved placeholder in {name}")
        legacy.write_new(output_dir / name, text)
    metadata = {
        "scenario": str(scenario),
        "gnb": shape.gnb.node_id,
        "ues": [ue.node_id for ue in shape.ues],
        "links": [
            {"from": link.source, "to": link.destination, "model": link.model}
            for link in shape.links
        ],
        "ue_count": args.ue_count,
        "ue_tx_scale_db": ue_scale,
        "rx_noise": {
            "awgn_snr_db": args.awgn_snr_db,
            "tx_power_dl": args.tx_power_dl,
            "tx_power_ul": args.tx_power_ul,
            "noise_power": rx_noise,
        },
    }
    legacy.write_new(
        output_dir / "sionna-multi-ue-shape.json",
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
    )
    print(
        "event=native_sionna_multi_ue_configs_rendered "
        f"ues={len(shape.ues)} links={len(shape.links)} "
        f"rx_noise={'on' if rx_noise else 'off'} "
        f'output_dir="{output_dir}"'
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
