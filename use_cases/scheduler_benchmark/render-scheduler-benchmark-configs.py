#!/usr/bin/env python3
"""Render the native scheduler-benchmark configs: two cells, two UEs each.

Cell a (gnb0 with ue0, ue1) and cell b (gnb1 with ue2, ue3) run side by side
on two broker processes. ONE Sionna bridge solves the seeded scenario, whose
radios are gnb0/ue0/ue1, and sends every batch to both brokers -- to cell b
under the rename gnb0->gnb1, ue0->ue2, ue1->ue3 -- so both cells carry the
same channel at the same instant. The only intended difference between the
cells is the gNB scheduler policy (rr or qos, `cell_cfg.scheduler.policy`).

Reused unchanged: the gNB identities and N2/N3 addresses of
`render-multi-gnb-configs.py`, the Open5GS/subscriber/srsUE rendering and UE
records of `render-multi-ue-configs.py`, the scenario-driven topology with the
receiver noise floor of `render-sionna-multi-ue-configs.py`, and the gNB
metrics block of `render-sionna-rank1-configs.py`.

Per-UE 5QI comes from the traffic profile and is written into the subscriber
CSV's qci column, which Open5GS's add_users.py stores as the session's
default QoS index.
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NATIVE = HERE.parents[1] / "scripts" / "native"
sys.path.insert(0, str(HERE))

import definitions as bench  # noqa: E402


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


multi_gnb = load("render_multi_gnb_configs", NATIVE / "render-multi-gnb-configs.py")
sionna = load("render_sionna_multi_ue_configs", NATIVE / "render-sionna-multi-ue-configs.py")
rank1 = load("render_sionna_rank1_configs", NATIVE / "render-sionna-rank1-configs.py")
legacy = multi_gnb.multi_ue
fail = legacy.fail

UE_RECORDS = {ue["device_id"]: ue for ue in legacy.UES}
SCENARIO_NODES = ("gnb0", "ue0", "ue1")


def check_cells() -> None:
    """The shared definitions must agree with the gNB and UE tables reused here."""

    for cell, gnb in zip(bench.CELLS, multi_gnb.CELLS):
        for key in ("device_id", "tx_port", "rx_port", "pci", "gnb_id", "bind"):
            if cell["gnb"][key] != gnb[key]:
                fail(f"definitions.CELLS[{cell['name']}].gnb.{key} disagrees with render-multi-gnb-configs.py")
        for ue in cell["ues"]:
            if ue not in UE_RECORDS:
                fail(f"definitions.CELLS names unknown UE {ue}")


def check_scenario(root: dict) -> None:
    nodes = root.get("nodes")
    if not isinstance(nodes, dict) or set(nodes) != set(SCENARIO_NODES):
        fail(f"benchmark scenario must name exactly {', '.join(SCENARIO_NODES)} (cell a's radios)")
    links = root.get("links")
    if not isinstance(links, list) or not links:
        fail("scenario.links must be a non-empty array")
    pairs = {(link.get("from"), link.get("to")) for link in links}
    for ue in ("ue0", "ue1"):
        if ("gnb0", ue) not in pairs or (ue, "gnb0") not in pairs:
            fail(f"scenario is missing the gnb0<->{ue} link pair")
    for link in links:
        if "gnb0" not in (link.get("from"), link.get("to")):
            fail(f"link {link.get('from')}->{link.get('to')} is UE-to-UE; the benchmark cells carry gNB links only")


def renamed_scenario(root: dict, rename: dict[str, str]) -> dict:
    out = copy.deepcopy(root)
    out["nodes"] = {rename.get(node_id, node_id): node for node_id, node in root["nodes"].items()}
    out["links"] = [
        dict(link, **{"from": rename.get(link["from"], link["from"]), "to": rename.get(link["to"], link["to"])})
        for link in root["links"]
    ]
    return out


def cell_shape(cell: dict, scenario: dict) -> "sionna.LiveShape":
    def ports(node_id: str) -> "sionna.NodePorts":
        node = scenario["nodes"][node_id]
        result = sionna.NodePorts(node_id, sionna._array_count(node, "tx_array", node_id),
                                  sionna._array_count(node, "rx_array", node_id))
        if (result.tx, result.rx) != (1, 1):
            fail(f"cell {cell['name']}: {node_id} asks {result.tx}x{result.rx}; the gNB fixture and srsUE are single-port")
        return result

    gnb = ports(cell["gnb"]["device_id"])
    ues = tuple(ports(ue) for ue in cell["ues"])
    links = tuple(sionna.ShapeLink(link["from"], link["to"], link.get("model", "sionna_rt"))
                  for link in scenario["links"])
    return sionna.LiveShape(gnb, ues, links)


def insert_scheduler_policy(gnb_text: str, scheduler: str) -> str:
    if scheduler not in bench.SCHEDULERS:
        fail(f"unknown scheduler {scheduler!r}; known: {', '.join(bench.SCHEDULERS)}")
    if "\n  scheduler:\n" in gnb_text:
        fail("gNB fixture already has a cell_cfg.scheduler block")
    return legacy.replace_exact(gnb_text, "cell_cfg:\n", "cell_cfg:\n" + bench.SCHEDULERS[scheduler]["yaml"], 1,
                                "gNB scheduler policy")


def add_config_dump(gnb_text: str) -> str:
    """`log.config_level: info` makes the gNB log its non-default configuration.

    That dump lists a policy subcommand only when CLI11 counted it, which is
    the evidence the gate checks for each cell's scheduler.
    """

    return legacy.replace_exact(gnb_text, "  all_level: info\n", "  all_level: info\n  config_level: info\n", 1,
                                "gNB config dump level")


def render_subscriber(source: str, five_qi: dict[str, int]) -> str:
    records = [line for line in source.splitlines() if line and not line.startswith("#")]
    header = [line for line in source.splitlines() if line.startswith("#")]
    ues = [UE_RECORDS[ue] for cell in bench.CELLS for ue in cell["ues"]]
    legacy.validate_subscriber(source, ues)
    out = []
    for record, ue in zip(records, ues):
        fields = record.split(",")
        fields[6] = str(five_qi[ue["device_id"]])
        out.append(",".join(fields))
    return "\n".join(header + out) + "\n"


def parse_overrides(text: str | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for item in (text or "").split(","):
        item = item.strip()
        if not item:
            continue
        key, sep, value = item.partition("=")
        if not sep:
            raise ValueError(f"traffic override {item!r} must be key=value")
        out[key.strip()] = float(value)
    return out


def self_test() -> None:
    global fail

    def raising(message: str) -> None:
        raise ValueError(message)

    fail = raising
    check_cells()
    one = {"rows": 1, "cols": 1}
    scenario = {
        "name": "t", "nodes": {"gnb0": {"array": one}, "ue0": {"array": one}, "ue1": {"array": one}},
        "links": [{"from": "gnb0", "to": "ue0", "model": "sionna_rt"}, {"from": "gnb0", "to": "ue1", "model": "sionna_rt"},
                  {"from": "ue0", "to": "gnb0", "model": "sionna_rt"}, {"from": "ue1", "to": "gnb0", "model": "sionna_rt"}],
    }
    check_scenario(scenario)
    b = renamed_scenario(scenario, bench.FANOUT_RENAME)
    assert set(b["nodes"]) == {"gnb1", "ue2", "ue3"}, b
    shape_a = cell_shape(bench.CELLS[0], scenario)
    shape_b = cell_shape(bench.CELLS[1], b)
    topo_a = sionna.render_topology(shape_a, sionna.rx_noise_powers(shape_a, 40.0), (2000, 2001))
    topo_b = sionna.render_topology(shape_b, sionna.rx_noise_powers(shape_b, 40.0), (2010, 2011))
    for token in ("tcp://127.0.0.1:2000\n", "tcp://127.0.0.1:2101\n", "tcp://127.0.0.1:2103\n", "from: gnb0\n"):
        assert token in topo_a, token
    for token in ("tcp://127.0.0.1:2010\n", "tcp://127.0.0.1:2105\n", "tcp://127.0.0.1:2107\n", "from: gnb1\n"):
        assert token in topo_b, token
    assert "gnb0" not in topo_b and "ue0" not in topo_b and "ue1_" not in topo_b
    fixture = "cell_cfg:\n  dl_arfcn: 1\n  pdsch:\n    mcs_table: qam64\nlog:\n  all_level: info\n"
    rr = insert_scheduler_policy(fixture, "rr")
    assert "cell_cfg:\n  scheduler:\n    policy:\n      rr_sched:\n  dl_arfcn: 1\n" in rr, rr
    qos = insert_scheduler_policy(fixture, "qos")
    assert "      qos_sched:\n        combine_function: gbr_prioritized\n" in qos, qos
    assert "config_level: info" in add_config_dump(fixture)
    for bad, expected in (
        (dict(scenario, nodes={**scenario["nodes"], "ue2": {"array": one}}), "must name exactly"),
        (dict(scenario, links=scenario["links"][:3]), "missing the gnb0<->ue1"),
        (dict(scenario, links=scenario["links"] + [{"from": "ue0", "to": "ue1"}]), "UE-to-UE"),
    ):
        try:
            check_scenario(bad)
        except ValueError as error:
            assert expected in str(error), (expected, str(error))
        else:
            raise AssertionError(f"should have been refused: {expected}")
    try:
        insert_scheduler_policy(rr, "qos")
    except ValueError as error:
        assert "already has" in str(error)
    else:
        raise AssertionError("a second policy block must be refused")
    csv = ("# name,imsi,key,op_type,op_or_opc,amf,qci,ipv4\n" + "".join(
        f"multi-ue-{i},{UE_RECORDS[f'ue{i}']['imsi']},k,opc,o,8000,9,{UE_RECORDS[f'ue{i}']['ipv4']}\n" for i in range(4)))
    rendered = render_subscriber(csv, {"ue0": 9, "ue1": 7, "ue2": 9, "ue3": 7})
    assert [line.split(",")[6] for line in rendered.splitlines()[1:]] == ["9", "7", "9", "7"], rendered
    assert parse_overrides("ue0.dl_bulk.rate_mbps=25, ue1.five_qi.value=9") == {
        "ue0.dl_bulk.rate_mbps": 25.0, "ue1.five_qi.value": 9.0}
    fail = legacy.fail
    print("event=native_scheduler_benchmark_config_renderer_self_test result=pass")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--native-root", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--log-dir", type=Path)
    parser.add_argument("--scenario-config", type=Path)
    parser.add_argument("--sched-a", choices=sorted(bench.SCHEDULERS), default="rr")
    parser.add_argument("--sched-b", choices=sorted(bench.SCHEDULERS), default="qos")
    parser.add_argument("--traffic-profile", default="mixed")
    parser.add_argument("--traffic-overrides", default=None, metavar="ueI.flow.field=value,...")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--measure-seconds", type=int)
    parser.add_argument("--awgn-snr-db", type=float, default=None)
    parser.add_argument("--tx-power-dl", type=float, default=sionna.TX_POWER_DL)
    parser.add_argument("--tx-power-ul", type=float, default=sionna.TX_POWER_UL)
    parser.add_argument("--gnb-metrics", action="store_true",
                        help="append the metrics/remote_control block on each cell's metrics port")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    render_args = (args.repo_root, args.native_root, args.output_dir, args.log_dir, args.scenario_config)
    if args.self_test:
        if any(render_args):
            parser.error("--self-test cannot be combined with render arguments")
        self_test()
        return 0
    if not all(render_args) or args.seed is None or args.measure_seconds is None:
        parser.error("render mode requires repo/native/output/log/scenario, --seed and --measure-seconds")

    check_cells()
    repo_root = args.repo_root.resolve(strict=True)
    native_root = args.native_root.resolve(strict=True)
    scenario_path = args.scenario_config.resolve(strict=True)
    output_dir = legacy.safe_directory(args.output_dir, "output directory")
    log_dir = legacy.safe_directory(args.log_dir, "log directory")
    try:
        root = json.loads(scenario_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        fail(f"cannot load scenario {scenario_path}: {error}")
    check_scenario(root)
    try:
        profile = bench.resolve_profile(args.traffic_profile, parse_overrides(args.traffic_overrides))
    except ValueError as error:
        fail(str(error))
    five_qi = {}
    for cell in bench.CELLS:
        for index, ue in enumerate(cell["ues"]):
            five_qi[ue] = profile["ues"][index]["five_qi"]

    gnb_source = legacy.read_regular(repo_root / "use_cases/configs/ran/ocudu/docker/gnb_zmq_b210_fdd_srsue.yaml", "gNB fixture")
    open5gs_source = legacy.read_regular(native_root / "src/ocudu/docker/open5gs/open5gs-5gc.yml",
                                         "pinned OCUDU Open5GS template")
    srsue_source = legacy.read_regular(repo_root / "use_cases/configs/ran/srsue/srsue_zmq_multi_ue.conf.in",
                                       "native srsUE template")
    subscriber_source = legacy.read_regular(repo_root / legacy.LAYOUTS[4][1], "native quad subscriber template")

    rendered = {
        "open5gs.yaml": legacy.render_open5gs(open5gs_source, native_root),
        "subscriber.csv": render_subscriber(subscriber_source, five_qi),
        "scenario.json": json.dumps(root, indent=2) + "\n",
    }
    schedulers = {"a": args.sched_a, "b": args.sched_b}
    metadata = {
        "kind": "ocudu-scheduler-benchmark",
        "seed": args.seed,
        "measure_seconds": args.measure_seconds,
        "segment_seconds": 1.0,
        "scenario": str(scenario_path),
        "scenario_benchmark": root.get("benchmark"),
        "fanout_rename": bench.FANOUT_RENAME,
        "twins": bench.TWINS,
        "traffic_profile": profile,
        "rx_noise": {"awgn_snr_db": args.awgn_snr_db, "tx_power_dl": args.tx_power_dl,
                     "tx_power_ul": args.tx_power_ul},
        "cells": {},
    }
    scenarios = {"a": root, "b": renamed_scenario(root, bench.FANOUT_RENAME)}
    for cell in bench.CELLS:
        name = cell["name"]
        shape = cell_shape(cell, scenarios[name])
        try:
            rx_noise = sionna.rx_noise_powers(shape, args.awgn_snr_db, args.tx_power_dl, args.tx_power_ul)
        except ValueError as error:
            fail(str(error))
        gnb_record = {key: cell["gnb"][key] for key in ("device_id", "tx_port", "rx_port", "pci", "gnb_id", "bind")}
        gnb_text = multi_gnb.render_gnb(gnb_source, gnb_record, log_dir)
        gnb_text = insert_scheduler_policy(gnb_text, schedulers[name])
        gnb_text = add_config_dump(gnb_text)
        if args.gnb_metrics:
            gnb_text += rank1.render_gnb_metrics(cell["gnb"]["metrics_port"])
        rendered[f"{cell['gnb']['device_id']}.yaml"] = gnb_text
        rendered[f"topology-{name}.yaml"] = sionna.render_topology(
            shape, rx_noise, (cell["gnb"]["tx_port"], cell["gnb"]["rx_port"]))
        ues_meta = []
        for index, ue in enumerate(cell["ues"]):
            record = UE_RECORDS[ue]
            rendered[f"srsue-{ue}.conf"] = legacy.render_srsue(srsue_source, record, log_dir)
            ues_meta.append({
                **{key: record[key] for key in ("device_id", "tx_port", "rx_port", "imsi", "netns", "ipv4")},
                "cell_index": index,
                "twin": bench.TWINS[ue],
                "scenario_node": {v: k for k, v in bench.FANOUT_RENAME.items()}.get(ue, ue),
                "role": profile["ues"][index]["role"],
                "five_qi": profile["ues"][index]["five_qi"],
                "qos": bench.FIVE_QI[profile["ues"][index]["five_qi"]],
                "flows": profile["ues"][index]["flows"],
            })
        metadata["cells"][name] = {
            "scheduler": schedulers[name],
            "scheduler_info": bench.SCHEDULERS[schedulers[name]],
            "gnb": dict(cell["gnb"], metrics_enabled=bool(args.gnb_metrics)),
            "ues": ues_meta,
            "links": [{"from": l.source, "to": l.destination, "model": l.model} for l in shape.links],
            "noise_power": rx_noise,
        }
    for name, text in rendered.items():
        if name.endswith((".yaml", ".conf", ".csv")) and legacy.PLACEHOLDER_RE.search(text):
            fail(f"unresolved placeholder in {name}")
        legacy.write_new(output_dir / name, text)
    legacy.write_new(output_dir / "benchmark.json", json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    print("event=native_scheduler_benchmark_configs_rendered cells=2 ues=4 "
          f"sched_a={args.sched_a} sched_b={args.sched_b} profile={profile['name']} seed={args.seed} "
          f"rx_noise={'on' if args.awgn_snr_db is not None else 'off'} output_dir=\"{output_dir}\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
