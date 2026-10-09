#!/usr/bin/env python3
"""Render native configs for the two-cell TDD n78 gate with OAI nrUEs (X5, CLI).

Two OCUDU gNB processes on the SAME TDD n78 20 MHz / 30 kHz carrier, one OAI
nrUE under each, Open5GS, and a broker topology generated from a Sionna
scenario. The two cells are identical except PCI / gnb_id / N2-N3 bind address
/ ZMQ port pair / PRACH root sequence and -- the point of X5 -- the TDD
pattern: when cell B uses a different pattern than cell A, cell B's UE
transmits uplink in slots where cell A's UE is receiving downlink, and a
UE<->UE edge (physical on a shared TDD carrier) carries cross-link
interference (CLI) into the victim's DL slots.

Built from three existing renderers, nothing of which is edited:

  cell        render-oai-multi-ue-configs.py render_gnb_tdd (the X3 TDD n78
              20 MHz cell: dl_arfcn 632628, 51 PRB, 23.04 MS/s, PRACH 159,
              7D/1S/2U); this file then gives each copy its identity and its
              TDD pattern (render_cell).
  identity    render-multi-gnb-configs.py CELLS (PCI 1/2, gnb_id 411/412,
              bind 127.0.0.11/.12, ZMQ 2000-2001 / 2010-2011). The PRACH roots
              differ from that table: config index 159 is format B4 (L = 139),
              whose root range is 0..137, so cell B gets root 70 (not 200).
  UE side     render-oai-multi-ue-configs.py render_nrue / uecap / VETH /
              OAI_UE_TX_POWER, render-multi-ue-configs.py Open5GS + subscriber.
  topology    a four-node copy of render-sionna-multi-ue-configs.py
              render_topology + rx_noise_powers (those take exactly one gNB,
              so the two functions are re-implemented here for N gNBs, same
              formulas): every port `carrier: n78`, UE ports tx_scale_db from
              the OAI wire level, absolute noise floors.

TDD pattern knob: --tdd-patterns "A;B" with A, B of the form `<dl>D<ul>U`
(10-slot period, one special slot of 6 DL / 4 UL symbols, dl + ul = 9), or
"same" (7D2U for both). OCUDU a1916ed accepts 7D2U, 3D6U and 2D7U (dry run
checked on the Spark). The nrUE reads the pattern from SIB1; it has no knob.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.dont_write_bytecode = True


def _load(name: str, filename: str):
    path = Path(__file__).resolve().with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load renderer module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


legacy = _load("render_multi_ue_configs", "render-multi-ue-configs.py")
sionna = _load("render_sionna_multi_ue_configs", "render-sionna-multi-ue-configs.py")
oai_mue = _load("render_oai_multi_ue_configs", "render-oai-multi-ue-configs.py")

UE_COUNT = 2
CARRIER_LABEL = oai_mue.CARRIER_LABEL
CARRIER_HZ = oai_mue.CARRIER_HZ
SAMPLE_RATE_HZ = oai_mue.SAMPLE_RATE_HZ
OAI_UE_TX_POWER = oai_mue.OAI_UE_TX_POWER
TX_POWER_DL = oai_mue.TX_POWER_DL
VETH = oai_mue.VETH

# One entry per cell (see the module docstring for the PRACH root choice).
CELLS = (
    {"device_id": "gnb0", "tx_port": 2000, "rx_port": 2001, "pci": 1, "gnb_id": 411, "bind": "127.0.0.11",
     "prach_root": 1},
    {"device_id": "gnb1", "tx_port": 2010, "rx_port": 2011, "pci": 2, "gnb_id": 412, "bind": "127.0.0.12",
     "prach_root": 70},
)
PRACH_SHORT_FORMAT_ROOTS = 138  # L = 139 preambles: root sequence index 0..137
UES = tuple(legacy.UES)[:UE_COUNT]
# UE i is served by cell i: the scenario's geometry (serving vs intercell path
# gain) decides that, the verdict checks the camped PCI against it.
SERVING = {"ue0": "gnb0", "ue1": "gnb1"}

TDD_PERIOD_SLOTS = 10
TDD_SPECIAL_DL_SYMBOLS = 6
TDD_SPECIAL_UL_SYMBOLS = 4
DEFAULT_PATTERN = "7D2U"
PATTERN_RE = re.compile(r"^(\d+)D(\d+)U$")


@dataclass(frozen=True)
class TddPattern:
    dl_slots: int
    ul_slots: int

    @property
    def name(self) -> str:
        return f"{self.dl_slots}D{self.ul_slots}U"

    def config(self) -> dict:
        return {
            "dl_ul_tx_period": TDD_PERIOD_SLOTS,
            "nof_dl_slots": self.dl_slots,
            "nof_dl_symbols": TDD_SPECIAL_DL_SYMBOLS,
            "nof_ul_slots": self.ul_slots,
            "nof_ul_symbols": TDD_SPECIAL_UL_SYMBOLS,
        }

    def slot_kinds(self) -> str:
        """One letter per slot of the period: D, S, U."""
        return "D" * self.dl_slots + "S" + "U" * self.ul_slots


def parse_pattern(text: str) -> TddPattern:
    match = PATTERN_RE.match(text.strip())
    if match is None:
        raise ValueError(f"TDD pattern must look like 7D2U (got {text!r})")
    dl, ul = int(match.group(1)), int(match.group(2))
    if dl + ul != TDD_PERIOD_SLOTS - 1:
        raise ValueError(f"TDD pattern {text!r}: dl + ul must be {TDD_PERIOD_SLOTS - 1} (one special slot)")
    # Slot 0 carries the SSB and SIB1's CORESET#0; a cell needs at least one
    # full DL slot, and slot 19 (PRACH config 159, subframe 9) must be UL.
    if dl < 1 or ul < 1:
        raise ValueError(f"TDD pattern {text!r}: needs at least one DL and one UL slot")
    return TddPattern(dl, ul)


def parse_patterns(text: str) -> tuple[TddPattern, TddPattern]:
    """--tdd-patterns / OCUDU_NATIVE_TDD_PATTERNS: "same" or "A;B"."""
    text = text.strip()
    if text == "same":
        pattern = parse_pattern(DEFAULT_PATTERN)
        return pattern, pattern
    parts = text.split(";")
    if len(parts) != 2:
        raise ValueError(f"TDD patterns must be 'same' or two ';'-separated patterns (got {text!r})")
    return parse_pattern(parts[0]), parse_pattern(parts[1])


def cli_overlap_slots(a: TddPattern, b: TddPattern) -> list[int]:
    """Slots (of the period) where cell A is in DL and cell B is in UL.

    These are the slots in which B's UE transmits while A's UE receives: the
    UE->UE CLI slots. The special slot counts for both (its tail is UL in B
    while its head is DL in A) only when A has it as a full DL slot.
    """
    kinds_a, kinds_b = a.slot_kinds(), b.slot_kinds()
    return [s for s in range(TDD_PERIOD_SLOTS)
            if kinds_a[s] == "D" and kinds_b[s] in ("U", "S")]


# --- gNB ---------------------------------------------------------------------
X3_TDD_BLOCK = "  tdd_ul_dl_cfg:\n" + "".join(f"    {k}: {v}\n" for k, v in oai_mue.TDD_PATTERN.items())


def render_cell(source: str, cell: dict, pattern: TddPattern, log_dir: Path, stdout_metrics: bool = True) -> str:
    """X3's TDD cell rendering, then this cell's identity, ports and pattern."""
    if not 0 <= cell["prach_root"] < PRACH_SHORT_FORMAT_ROOTS:
        legacy.fail(f"{cell['device_id']}: PRACH root {cell['prach_root']} is outside 0..137 (format B4, L=139)")
    name = cell["device_id"]
    rendered = oai_mue.render_gnb_tdd(source, log_dir)
    rendered = legacy.replace_exact(rendered, "    bind_addrs: 127.0.0.1\n", f"    bind_addrs: {cell['bind']}\n",
                                    1, f"{name} NGAP bind address")
    rendered = legacy.replace_exact(rendered, "      - bind_addr: 127.0.0.1\n", f"      - bind_addr: {cell['bind']}\n",
                                    1, f"{name} N3 bind address")
    rendered = legacy.replace_exact(
        rendered,
        "  device_args: tx_port=tcp://127.0.0.1:2000,rx_port=tcp://127.0.0.1:2001,base_srate=23.04e6\n",
        f"  device_args: tx_port=tcp://127.0.0.1:{cell['tx_port']},rx_port=tcp://127.0.0.1:{cell['rx_port']},"
        "base_srate=23.04e6\n",
        1, f"{name} ZMQ endpoints")
    rendered = legacy.replace_exact(rendered, "cell_cfg:\n", f"cell_cfg:\n  pci: {cell['pci']}\n", 1, f"{name} PCI")
    rendered = legacy.replace_exact(
        rendered, f"    prach_config_index: {oai_mue.PRACH_CONFIG_INDEX}\n",
        f"    prach_config_index: {oai_mue.PRACH_CONFIG_INDEX}\n    prach_root_sequence_index: {cell['prach_root']}\n",
        1, f"{name} PRACH root")
    tdd = "  tdd_ul_dl_cfg:\n" + "".join(f"    {k}: {v}\n" for k, v in pattern.config().items())
    rendered = legacy.replace_exact(rendered, X3_TDD_BLOCK, tdd, 1, f"{name} TDD pattern")
    for old, new in {
        f"  filename: {log_dir / 'gnb-internal.log'}\n": f"  filename: {log_dir / f'{name}-internal.log'}\n",
        f"  mac_filename: {log_dir / 'gnb_mac.pcap'}\n": f"  mac_filename: {log_dir / f'{name}_mac.pcap'}\n",
        f"  ngap_filename: {log_dir / 'gnb_ngap.pcap'}\n": f"  ngap_filename: {log_dir / f'{name}_ngap.pcap'}\n",
    }.items():
        rendered = legacy.replace_exact(rendered, old, new, 1, f"{name} run artifact path")
    rendered += f"\ngnb_id: {cell['gnb_id']}\nran_node_name: {name}\n"
    if stdout_metrics:
        # Per-UE DL/UL HARQ ok/nok, CQI and PUSCH SNR once a second on the
        # console log: the gate's DL NACK evidence (accepted by a1916ed --dryrun).
        rendered += "\nmetrics:\n  autostart_stdout_metrics: true\n"
    return rendered


# --- scenario shape ----------------------------------------------------------
@dataclass(frozen=True)
class ShapeLink:
    source: str
    destination: str
    model: str


@dataclass(frozen=True)
class TwoCellShape:
    gnbs: tuple[str, ...]
    ues: tuple[str, ...]
    links: tuple[ShapeLink, ...]

    @property
    def nodes(self) -> tuple[str, ...]:
        return (*self.gnbs, *self.ues)


def load_two_cell_shape(path: Path) -> TwoCellShape:
    """gnb0, gnb1, ue0, ue1 and their links from a Sionna scenario.

    Required: both directions between every gNB and every UE (8 links: the
    serving pairs the verdict is measured on and the intercell pairs that make
    this a two-cell test). Optional: ue0<->ue1 (the X5 edge) and gnb0<->gnb1
    (gNB-to-gNB CLI, not part of the X5 runs). Single-antenna nodes only.
    """
    try:
        root = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load Sionna scenario {path}: {error}") from error
    if not isinstance(root, dict) or not isinstance(root.get("nodes"), dict):
        raise ValueError("scenario.nodes must be an object")
    nodes = root["nodes"]
    gnbs = tuple(c["device_id"] for c in CELLS)
    ues = tuple(u["device_id"] for u in UES)
    if sorted(nodes) != sorted(gnbs + ues):
        raise ValueError(f"the two-cell gate needs exactly the nodes {', '.join(gnbs + ues)}; "
                         f"the scenario names {', '.join(sorted(nodes)) or 'none'}")
    for node_id, node in nodes.items():
        if not isinstance(node, dict):
            raise ValueError(f"nodes.{node_id} must be an object")
        for key in ("array", "tx_array", "rx_array"):
            array = node.get(key)
            if isinstance(array, dict) and array.get("rows", 1) * array.get("cols", 1) != 1:
                raise ValueError(f"nodes.{node_id}.{key}: the two-cell gate is single-port per node")
    raw_links = root.get("links")
    if not isinstance(raw_links, list) or not raw_links:
        raise ValueError("scenario.links must be a non-empty array")
    links: list[ShapeLink] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(raw_links):
        if not isinstance(raw, dict):
            raise ValueError(f"links[{index}] must be an object")
        source, destination, model = raw.get("from"), raw.get("to"), raw.get("model", "sionna_rt")
        if source not in nodes or destination not in nodes or source == destination:
            raise ValueError(f"links[{index}] references an unknown node or is a self-loop: {source!r} -> {destination!r}")
        if not isinstance(model, str) or sionna.MODEL_ID_RE.fullmatch(model) is None:
            raise ValueError(f"links[{index}].model must match {sionna.MODEL_ID_RE.pattern}")
        if (source, destination) in seen:
            raise ValueError(f"duplicate link: {source}>{destination}")
        seen.add((source, destination))
        links.append(ShapeLink(source, destination, model))
    for gnb in gnbs:
        for ue in ues:
            for pair in ((gnb, ue), (ue, gnb)):
                if pair not in seen:
                    raise ValueError(f"scenario is missing the {pair[0]}->{pair[1]} link")
    return TwoCellShape(gnbs, ues, tuple(links))


def rx_noise_powers(shape: TwoCellShape, awgn_snr_db: float | None, tx_power_dl: float,
                    tx_power_ul: float, ue_scale_db: float) -> dict[str, float]:
    """render-sionna-multi-ue-configs.py rx_noise_powers for N gNBs (same formula)."""
    if awgn_snr_db is None:
        return {}
    if not (math.isfinite(awgn_snr_db) and -30.0 <= awgn_snr_db <= 120.0):
        raise ValueError(f"awgn_snr_db must be a finite value in [-30, 120]: {awgn_snr_db}")
    ratio = 10.0 ** (awgn_snr_db / 10.0)
    floors = {gnb: tx_power_ul * 10.0 ** (ue_scale_db / 10.0) / ratio for gnb in shape.gnbs}
    floors.update({ue: tx_power_dl / ratio for ue in shape.ues})
    return floors


def render_topology(shape: TwoCellShape, rx_noise: dict[str, float], ue_scale_db: float) -> str:
    """Four-node TDD topology: X3's labels/veth endpoints, the two-cell port plan."""
    endpoints = {c["device_id"]: ("127.0.0.1", c["tx_port"], "127.0.0.1", c["rx_port"]) for c in CELLS}
    for ue in UES:
        host_ip, ue_ip = VETH[ue["device_id"]]
        endpoints[ue["device_id"]] = (ue_ip, ue["tx_port"], host_ip, ue["rx_port"])
    devices = ""
    for node in shape.nodes:
        tx_ip, tx_port, rx_ip, rx_port = endpoints[node]
        devices += (f"  - id: {node}_p0\n    role: port\n    sample_rate_hz: {SAMPLE_RATE_HZ}\n"
                    f"    tx_endpoint: tcp://{tx_ip}:{tx_port}\n    rx_endpoint: tcp://{rx_ip}:{rx_port}\n"
                    f"    carrier: {CARRIER_LABEL}\n")
        if node in shape.ues:
            devices += f"    tx_scale_db: {ue_scale_db:.3f}\n"
        if node in rx_noise:
            devices += f"    rx_model: rx_noise_{node}\n"
    radio_nodes = "".join(f"  - id: {node}\n    tx_ports:\n      - {node}_p0\n    rx_ports:\n      - {node}_p0\n"
                          for node in shape.nodes)
    links = "".join(f"  - from: {l.source}\n    to: {l.destination}\n    model: {l.model}\n" for l in shape.links)
    models = "".join(f"  {model}:\n    chain:\n      - type: tdl\n        taps:\n"
                     "          - delay_samples: 0.0\n            gain_db: -100.0\n            phase_rad: 0.0\n"
                     for model in dict.fromkeys(l.model for l in shape.links))
    models += "".join(f"  rx_noise_{node}:\n    chain:\n      - type: awgn\n        noise_power: {rx_noise[node]:.6e}\n"
                      for node in shape.nodes if node in rx_noise)
    return ("# Generated from the Sionna scenario by render-oai-two-cell-tdd-configs.py.\n"
            "runtime:\n  backend: cuda\n  gpu_device: 0\n  batch_samples: 23040\n  queue_samples: 2457600\n"
            f"devices:\n{devices}radio_nodes:\n{radio_nodes}links:\n{links}models:\n{models}")


def metadata(shape: TwoCellShape, patterns, ue_scale: float, rx_noise: dict[str, float], args) -> dict:
    cells = []
    for cell, pattern in zip(CELLS, patterns):
        cells.append({**cell, "tdd_pattern": pattern.name, "tdd_ul_dl_cfg": pattern.config(),
                      "slot_kinds": pattern.slot_kinds(), "serves": next(u for u, g in SERVING.items() if g == cell["device_id"])})
    return {
        "scenario": str(args.scenario_config),
        "gnbs": list(shape.gnbs), "ues": list(shape.ues), "serving": SERVING,
        "links": [{"from": l.source, "to": l.destination, "model": l.model} for l in shape.links],
        "ue_ue_links": [[l.source, l.destination] for l in shape.links if l.source in shape.ues and l.destination in shape.ues],
        "gnb_gnb_links": [[l.source, l.destination] for l in shape.links if l.source in shape.gnbs and l.destination in shape.gnbs],
        "cells": cells,
        "tdd_mode": "same" if patterns[0] == patterns[1] else "different",
        "cli_slots_ue1_into_ue0": cli_overlap_slots(patterns[0], patterns[1]),
        "cli_slots_ue0_into_ue1": cli_overlap_slots(patterns[1], patterns[0]),
        "cell": {
            "duplex": "tdd", "band": oai_mue.BAND, "bandwidth_mhz": oai_mue.BANDWIDTH_MHZ, "scs_khz": oai_mue.SCS_KHZ,
            "prb": oai_mue.PRB, "dl_arfcn": oai_mue.DL_ARFCN, "carrier_hz": CARRIER_HZ, "ssb_arfcn": oai_mue.SSB_ARFCN,
            "sample_rate_hz": SAMPLE_RATE_HZ, "prach_config_index": oai_mue.PRACH_CONFIG_INDEX,
            "carrier_label": CARRIER_LABEL, "slot_samples": SAMPLE_RATE_HZ // 2000,
        },
        "nrue_radio_args": oai_mue.NRUE_RADIO_ARGS,
        "veth": {ue: {"host": host, "ue": ue_ip} for ue, (host, ue_ip) in VETH.items()},
        "ue_tx_scale_db": ue_scale,
        "ue_tx_power_source": oai_mue.OAI_UE_TX_POWER_SOURCE if args.tx_power_ul == OAI_UE_TX_POWER else "--tx-power-ul",
        "rx_noise": {"awgn_snr_db": args.awgn_snr_db, "tx_power_dl": args.tx_power_dl, "tx_power_ul": args.tx_power_ul,
                     "ul_power_offset_db": args.ul_power_offset_db, "noise_power": rx_noise},
        "stdout_metrics": bool(args.stdout_metrics),
    }


def self_test() -> None:
    import tempfile

    assert parse_patterns("same") == (TddPattern(7, 2), TddPattern(7, 2))
    a, b = parse_patterns("7D2U;3D6U")
    assert (a.name, b.name) == ("7D2U", "3D6U") and b.slot_kinds() == "DDDSUUUUUU"
    assert cli_overlap_slots(a, b) == [3, 4, 5, 6], cli_overlap_slots(a, b)
    assert cli_overlap_slots(b, a) == [], cli_overlap_slots(b, a)
    assert cli_overlap_slots(a, a) == []
    for bad in ("7D3U", "8D1S1U", "x", "7D2U;3D6U;2D7U", "0D9U"):
        try:
            parse_patterns(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(bad)

    fixture = Path(__file__).resolve().parents[2] / "use_cases/configs/ran/ocudu/docker/gnb_zmq_b210_fdd_srsue.yaml"
    source = fixture.read_text(encoding="utf-8")
    log_dir = Path("/tmp/x")
    cell_a = render_cell(source, CELLS[0], a, log_dir)
    cell_b = render_cell(source, CELLS[1], b, log_dir)
    for token in ("  pci: 1\n", "    bind_addrs: 127.0.0.11\n", "      - bind_addr: 127.0.0.11\n",
                  "tx_port=tcp://127.0.0.1:2000,rx_port=tcp://127.0.0.1:2001,", "    prach_root_sequence_index: 1\n",
                  "    nof_dl_slots: 7\n    nof_dl_symbols: 6\n    nof_ul_slots: 2\n", "gnb_id: 411\nran_node_name: gnb0\n",
                  "  filename: /tmp/x/gnb0-internal.log\n", "  autostart_stdout_metrics: true\n", "  band: 78\n",
                  "  dl_arfcn: 632628\n", "    prach_config_index: 159\n"):
        assert token in cell_a, token
    for token in ("  pci: 2\n", "    bind_addrs: 127.0.0.12\n", "tx_port=tcp://127.0.0.1:2010,rx_port=tcp://127.0.0.1:2011,",
                  "    prach_root_sequence_index: 70\n", "    nof_dl_slots: 3\n    nof_dl_symbols: 6\n    nof_ul_slots: 6\n",
                  "gnb_id: 412\nran_node_name: gnb1\n", "  mac_filename: /tmp/x/gnb1_mac.pcap\n"):
        assert token in cell_b, token
    assert "pdcch" not in cell_a and cell_a.count("tdd_ul_dl_cfg") == 1
    assert "metrics:" not in render_cell(source, CELLS[0], a, log_dir, stdout_metrics=False)

    one = {"rows": 1, "cols": 1}
    nodes = {n: {"array": one} for n in ("gnb0", "gnb1", "ue0", "ue1")}
    eight = [(g, u) for g in ("gnb0", "gnb1") for u in ("ue0", "ue1")]
    links = [{"from": g, "to": u, "model": "sionna_rt"} for g, u in eight] + \
            [{"from": u, "to": g, "model": "sionna_rt"} for g, u in eight]
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "s.json"
        path.write_text(json.dumps({"nodes": nodes, "links": links}), encoding="utf-8")
        shape8 = load_two_cell_shape(path)
        links += [{"from": "ue0", "to": "ue1", "model": "sionna_rt"}, {"from": "ue1", "to": "ue0", "model": "sionna_rt"}]
        path.write_text(json.dumps({"nodes": nodes, "links": links}), encoding="utf-8")
        shape10 = load_two_cell_shape(path)
        path.write_text(json.dumps({"nodes": nodes, "links": links[1:]}), encoding="utf-8")
        try:
            load_two_cell_shape(path)
        except ValueError as error:
            assert "missing the gnb0->ue0 link" in str(error), error
        else:
            raise AssertionError("a scenario without the serving link must be refused")
    assert len(shape8.links) == 8 and len(shape10.links) == 10
    scale = sionna.ue_tx_scale_db(TX_POWER_DL, OAI_UE_TX_POWER)
    assert abs(scale - 22.810) < 1e-3, scale
    floors = rx_noise_powers(shape10, 40.0, TX_POWER_DL, OAI_UE_TX_POWER, scale)
    assert abs(floors["gnb0"] - TX_POWER_DL * 10 ** (-0.7) / 1e4) < 1e-15 and floors["gnb0"] == floors["gnb1"]
    assert abs(floors["ue0"] - TX_POWER_DL / 1e4) < 1e-15
    topology = render_topology(shape10, floors, scale)
    assert topology.count("    carrier: n78\n") == 4 and "tx_carrier" not in topology
    for token in ("    tx_endpoint: tcp://127.0.0.1:2000\n", "    rx_endpoint: tcp://127.0.0.1:2001\n",
                  "    tx_endpoint: tcp://127.0.0.1:2010\n", "    rx_endpoint: tcp://127.0.0.1:2011\n",
                  "    tx_endpoint: tcp://10.201.0.2:2101\n", "    rx_endpoint: tcp://10.201.0.1:2100\n",
                  "    tx_endpoint: tcp://10.201.1.2:2103\n", "    rx_endpoint: tcp://10.201.1.1:2102\n",
                  "  - from: ue1\n    to: ue0\n    model: sionna_rt\n", "  - id: gnb1\n    tx_ports:\n      - gnb1_p0\n"):
        assert token in topology, token
    assert topology.count("  - from: ") == 10 and topology.count("tx_scale_db:") == 2 and topology.count("type: awgn") == 4
    assert topology.count(f"    sample_rate_hz: {SAMPLE_RATE_HZ}\n") == 4 and "  batch_samples: 23040\n" in topology
    print("event=native_oai_two_cell_tdd_config_renderer_self_test result=pass")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--native-root", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--log-dir", type=Path)
    parser.add_argument("--scenario-config", type=Path)
    parser.add_argument("--tdd-patterns", default="same", help="'same' or 'A;B', each like 7D2U (see module docstring)")
    parser.add_argument("--awgn-snr-db", type=float, default=None)
    parser.add_argument("--tx-power-dl", type=float, default=TX_POWER_DL)
    parser.add_argument("--tx-power-ul", type=float, default=OAI_UE_TX_POWER)
    parser.add_argument("--ul-power-offset-db", type=float, default=sionna.UL_POWER_OFFSET_DB)
    parser.add_argument("--stdout-metrics", type=int, choices=(0, 1), default=1,
                        help="gNB per-UE stdout metrics table on the console log (DL/UL HARQ ok/nok, CQI)")
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
    if repo_root != args.repo_root or native_root != args.native_root or scenario != args.scenario_config:
        legacy.fail("repo, native, and scenario paths must already be canonical")
    try:
        patterns = parse_patterns(args.tdd_patterns)
        shape = load_two_cell_shape(scenario)
        ue_scale = sionna.ue_tx_scale_db(args.tx_power_dl, args.tx_power_ul, args.ul_power_offset_db)
        rx_noise = rx_noise_powers(shape, args.awgn_snr_db, args.tx_power_dl, args.tx_power_ul, ue_scale)
    except ValueError as error:
        legacy.fail(str(error))

    gnb_source = legacy.read_regular(repo_root / "use_cases/configs/ran/ocudu/docker/gnb_zmq_b210_fdd_srsue.yaml", "gNB fixture")
    open5gs_source = legacy.read_regular(native_root / "src/ocudu/docker/open5gs/open5gs-5gc.yml", "pinned OCUDU Open5GS template")
    nrue_source = legacy.read_regular(repo_root / "use_cases/configs/ran/oai/nrue_zmq_multi_ue.conf.in", "native OAI nrUE multi-UE template")
    _, subscriber_path, _ = legacy.LAYOUTS[UE_COUNT]
    subscriber_source = legacy.read_regular(repo_root / subscriber_path, "native subscriber template")
    uecap_source = legacy.read_regular(native_root / "src/oai/targets/PROJECTS/GENERIC-NR-5GC/CONF/uecap_ports1.xml",
                                       "stock OAI UE capability")
    try:
        uecap = oai_mue.uecap_with_ul_30khz_20mhz(uecap_source)
    except ValueError as error:
        legacy.fail(str(error))

    rendered = {
        "topology.yaml": render_topology(shape, rx_noise, ue_scale),
        "open5gs.yaml": legacy.render_open5gs(open5gs_source, native_root),
        "subscriber.csv": legacy.validate_subscriber(subscriber_source, UES),
        "nrue-radio.args": oai_mue.NRUE_RADIO_ARGS + "\n",
        "uecap.xml": uecap,
    }
    for cell, pattern in zip(CELLS, patterns):
        rendered[f"{cell['device_id']}.yaml"] = render_cell(gnb_source, cell, pattern, log_dir, bool(args.stdout_metrics))
    for ue in UES:
        rendered[f"nrue-{ue['device_id']}.conf"] = oai_mue.render_nrue(nrue_source, ue)
    for name, text in rendered.items():
        if name != "uecap.xml" and legacy.PLACEHOLDER_RE.search(text):
            legacy.fail(f"unresolved placeholder in {name}")
        legacy.write_new(output_dir / name, text)
    meta = metadata(shape, patterns, ue_scale, rx_noise, args)
    legacy.write_new(output_dir / "two-cell-tdd-shape.json", json.dumps(meta, indent=2, sort_keys=True) + "\n")
    print("event=native_oai_two_cell_tdd_configs_rendered "
          f"cells=2 ues=2 links={len(shape.links)} ue_ue_links={len(meta['ue_ue_links'])} "
          f"patterns={patterns[0].name};{patterns[1].name} tdd_mode={meta['tdd_mode']} "
          f"cli_slots_ue1_into_ue0={','.join(map(str, meta['cli_slots_ue1_into_ue0'])) or 'none'} "
          f"ue_tx_scale_db={ue_scale:.3f} ue_tx_power={args.tx_power_ul:.3e} "
          f"rx_noise={'on' if rx_noise else 'off'} output_dir=\"{output_dir}\"")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnicodeError, ValueError) as error:
        print(f"config rendering failed: {error}", file=sys.stderr)
        raise SystemExit(2)
