"""Shared definitions of the scheduler benchmark.

One module owns the vocabulary every other part reads -- the gate renderer,
the traffic generator, the analyzer and the web UI -- so a scheduler, a 5QI
or a traffic profile cannot mean one thing in the config and another in the
report.

Source references are to the pinned OCUDU tree
(`ocudu-spark/src/ocudu`, commit a1916edcdbcd70ba6e0af47ee87be061dad5a4e4).
"""

from __future__ import annotations

import copy
from typing import Any

# --- Cells -------------------------------------------------------------------
# Cell a's radios are the ones the Sionna scenario names; cell b's are the
# fan-out copies (run_bridge.py --fanout-control-endpoint with this rename),
# so ue2 sees exactly ue0's solved channel and ue3 exactly ue1's.
CELLS = (
    {
        "name": "a",
        "gnb": {"device_id": "gnb0", "tx_port": 2000, "rx_port": 2001, "pci": 1,
                "gnb_id": 411, "bind": "127.0.0.11", "metrics_port": 8001},
        "ues": ("ue0", "ue1"),
    },
    {
        "name": "b",
        "gnb": {"device_id": "gnb1", "tx_port": 2010, "rx_port": 2011, "pci": 2,
                "gnb_id": 412, "bind": "127.0.0.12", "metrics_port": 8002},
        "ues": ("ue2", "ue3"),
    },
)
FANOUT_RENAME = {"gnb0": "gnb1", "ue0": "ue2", "ue1": "ue3"}
# Twin of each UE in the other cell: same channel, same traffic schedule.
TWINS = {"ue0": "ue2", "ue1": "ue3", "ue2": "ue0", "ue3": "ue1"}
UE_GATEWAY = "10.45.1.1"

# --- Schedulers ----------------------------------------------------------------
# The two intra-slice policies the pinned OCUDU offers
# (lib/scheduler/policy/scheduler_policy_factory.cpp:13-19). Both are chosen
# at gNB start-up; there is no runtime switch (the remote-control commands do
# not include one), which is why the benchmark runs them side by side.
SCHEDULERS: dict[str, dict[str, Any]] = {
    "rr": {
        "title": "Round-Robin (time domain)",
        "short": "RR",
        "source": "lib/scheduler/policy/scheduler_time_rr.cpp",
        "yaml": "  scheduler:\n    policy:\n      rr_sched:\n",
        "config_dump_token": "rr_sched:",
        "summary": (
            "Serves the UEs in turn. A UE's priority is the number of newTx "
            "allocations made since it was last allocated, so the UE waiting "
            "longest goes first."
        ),
        "properties": [
            "Channel-blind: ignores CQI, MCS and the achievable rate.",
            "QoS-blind: ignores 5QI priority, packet delay budget and GBR.",
            "Turns are counted in allocation slots, separately for DL and UL.",
            "Expected: equal access per UE; a UE in a poor channel gets the "
            "same turns as a good one, so aggregate throughput can drop.",
        ],
        "parameters": {},
    },
    "qos": {
        "title": "QoS-aware Proportional Fair (time domain)",
        "short": "QoS-PF",
        "source": "lib/scheduler/policy/scheduler_time_qos.cpp",
        # Written out explicitly although every value equals the default
        # (scheduler_expert_config.h:26-52): the gNB's non-default config dump
        # then shows `qos_sched`, which is how the gate proves the policy.
        "yaml": (
            "  scheduler:\n"
            "    policy:\n"
            "      qos_sched:\n"
            "        combine_function: gbr_prioritized\n"
            "        pf_fairness_coeff: 2.0\n"
            "        prio_enabled: true\n"
            "        pdb_enabled: true\n"
            "        gbr_enabled: true\n"
        ),
        "config_dump_token": "qos_sched:",
        "summary": (
            "Ranks UEs by weight = GBR x PF x priority x delay and serves the "
            "largest first. This is OCUDU's default policy."
        ),
        "properties": [
            "PF = r_est / r_avg^a, a = pf_fairness_coeff = 2.0; r_est is the "
            "TBS the UE's current MCS would carry over the full bandwidth, "
            "r_avg an exponential average of its past TBS (alpha 0.01).",
            "Priority = (max + 1 - min(5QI priority x ARP priority)) / (max + 1) "
            "over the UE's logical channels with data.",
            "Delay = sum of head-of-line delay / PDB over those channels "
            "(1 when no data is waiting).",
            "GBR = sum of GBR / average rate over GBR flows (1 when none).",
            "A UE that has never been allocated goes first.",
        ],
        "parameters": {
            "combine_function": "gbr_prioritized", "pf_fairness_coeff": 2.0,
            "prio_enabled": True, "pdb_enabled": True, "gbr_enabled": True,
        },
    },
}

# --- 5QI characteristics ---------------------------------------------------------
# lib/ran/qos/five_qi_qos_mapping.cpp (TS 23.501 Table 5.7.4-1) and the DU's
# default QoS list include/ocudu/du/du_high/du_qos_config_helpers.h (RLC mode).
FIVE_QI = {
    5: {"type": "non-GBR", "priority": 10, "pdb_ms": 100, "rlc": "AM", "example": "IMS signalling"},
    6: {"type": "non-GBR", "priority": 60, "pdb_ms": 300, "rlc": "AM", "example": "buffered video, TCP"},
    7: {"type": "non-GBR", "priority": 70, "pdb_ms": 100, "rlc": "UM", "example": "voice, live video, gaming"},
    8: {"type": "non-GBR", "priority": 80, "pdb_ms": 300, "rlc": "AM", "example": "buffered video, TCP"},
    9: {"type": "non-GBR", "priority": 90, "pdb_ms": 300, "rlc": "AM", "example": "buffered video, TCP (default)"},
}
# Open5GS subscribers get ARP priority level 8 (docker/open5gs/add_users.py).
ARP_PRIORITY = 8

# --- Traffic profiles ------------------------------------------------------------
# Per cell-local UE index. Every cell runs the same profile, and twins get the
# same seeded packet schedule. Rates are offered load at the UDP layer.
#
# Flow patterns:
#   cbr      fixed size, fixed interval from rate
#   periodic fixed size every interval_ms
#   poisson  fixed size, exponential inter-arrivals with mean from rate
#   onoff    cbr at rate during exponential ON periods, silent during OFF
TRAFFIC_PROFILES: dict[str, dict[str, Any]] = {
    "mixed": {
        "title": "Bulk (5QI 9) + delay-sensitive (5QI 7)",
        "description": (
            "ue A is a backlogged download (5QI 9, PDB 300 ms); ue B carries "
            "on/off video and a 100 Hz control stream (5QI 7, PDB 100 ms). "
            "The cell is oversubscribed, so the policies must choose."
        ),
        "ues": [
            {"role": "bulk", "five_qi": 9, "flows": [
                {"name": "dl_bulk", "direction": "dl", "pattern": "cbr", "rate_mbps": 40.0, "size_bytes": 1200},
                {"name": "ul_bulk", "direction": "ul", "pattern": "cbr", "rate_mbps": 2.0, "size_bytes": 1200},
            ]},
            {"role": "interactive", "five_qi": 7, "flows": [
                {"name": "dl_video", "direction": "dl", "pattern": "onoff", "rate_mbps": 12.0, "size_bytes": 1200,
                 "on_mean_s": 2.0, "off_mean_s": 1.0},
                {"name": "dl_control", "direction": "dl", "pattern": "periodic", "interval_ms": 10.0,
                 "size_bytes": 200},
                {"name": "ul_control", "direction": "ul", "pattern": "periodic", "interval_ms": 10.0,
                 "size_bytes": 200},
            ]},
        ],
    },
    "full_buffer": {
        "title": "Full buffer, equal QoS (5QI 9)",
        "description": (
            "Both UEs download a backlogged stream with the same 5QI, so the "
            "QoS-aware policy's priority term is equal and what remains is PF "
            "against round robin."
        ),
        "ues": [
            {"role": "bulk", "five_qi": 9, "flows": [
                {"name": "dl_bulk", "direction": "dl", "pattern": "cbr", "rate_mbps": 40.0, "size_bytes": 1200},
                {"name": "ul_bulk", "direction": "ul", "pattern": "cbr", "rate_mbps": 2.0, "size_bytes": 1200},
            ]},
            {"role": "bulk", "five_qi": 9, "flows": [
                {"name": "dl_bulk", "direction": "dl", "pattern": "cbr", "rate_mbps": 40.0, "size_bytes": 1200},
                {"name": "ul_bulk", "direction": "ul", "pattern": "cbr", "rate_mbps": 2.0, "size_bytes": 1200},
            ]},
        ],
    },
    "light": {
        "title": "Light load (both 5QI 9)",
        "description": "Poisson traffic well below capacity: a control where both policies should look alike.",
        "ues": [
            {"role": "light", "five_qi": 9, "flows": [
                {"name": "dl_poisson", "direction": "dl", "pattern": "poisson", "rate_mbps": 4.0, "size_bytes": 1000},
                {"name": "ul_poisson", "direction": "ul", "pattern": "poisson", "rate_mbps": 0.5, "size_bytes": 500},
            ]},
            {"role": "light", "five_qi": 9, "flows": [
                {"name": "dl_poisson", "direction": "dl", "pattern": "poisson", "rate_mbps": 4.0, "size_bytes": 1000},
                {"name": "ul_poisson", "direction": "ul", "pattern": "poisson", "rate_mbps": 0.5, "size_bytes": 500},
            ]},
        ],
    },
}
FLOW_PATTERNS = ("cbr", "periodic", "poisson", "onoff")
# UDP ports. DL flow f of cell-local UE u is received in the UE's namespace on
# DL_BASE_PORT + 10 * u + f; every UL flow arrives at one root-namespace port.
DL_BASE_PORT = 47000
UL_PORT = 47900


def flow_id(cell_index: int, ue_index: int, flow_index: int) -> int:
    """16-bit flow id carried in every packet: cell, cell-local UE, flow."""

    if not (0 <= cell_index < 16 and 0 <= ue_index < 16 and 0 <= flow_index < 256):
        raise ValueError("flow id field out of range")
    return (cell_index << 12) | (ue_index << 8) | flow_index


def split_flow_id(value: int) -> tuple[int, int, int]:
    return (value >> 12) & 0xF, (value >> 8) & 0xF, value & 0xFF


def resolve_profile(name: str, overrides: dict[str, float] | None = None) -> dict[str, Any]:
    """A copy of a profile, with `ueI.flow.key=value` overrides applied and checked."""

    if name not in TRAFFIC_PROFILES:
        raise ValueError(f"unknown traffic profile {name!r}; known: {', '.join(sorted(TRAFFIC_PROFILES))}")
    profile = copy.deepcopy(TRAFFIC_PROFILES[name])
    profile["name"] = name
    for key, value in (overrides or {}).items():
        parts = key.split(".")
        if len(parts) != 3 or not parts[0].startswith("ue") or not parts[0][2:].isdigit():
            raise ValueError(f"override {key!r} must be ue<i>.<flow>.<field>")
        index = int(parts[0][2:])
        if index >= len(profile["ues"]):
            raise ValueError(f"override {key!r}: profile has {len(profile['ues'])} UEs")
        if parts[1] == "five_qi" and parts[2] == "value":
            profile["ues"][index]["five_qi"] = int(value)
            continue
        flows = {flow["name"]: flow for flow in profile["ues"][index]["flows"]}
        if parts[1] not in flows:
            raise ValueError(f"override {key!r}: no flow {parts[1]!r}")
        if parts[2] not in ("rate_mbps", "size_bytes", "interval_ms", "on_mean_s", "off_mean_s"):
            raise ValueError(f"override {key!r}: field {parts[2]!r} cannot be overridden")
        flows[parts[1]][parts[2]] = float(value) if parts[2] != "size_bytes" else int(value)
    validate_profile(profile)
    return profile


def validate_profile(profile: dict[str, Any]) -> None:
    ues = profile.get("ues")
    if not isinstance(ues, list) or len(ues) != 2:
        raise ValueError("a traffic profile defines exactly two UEs per cell")
    for index, ue in enumerate(ues):
        if ue.get("five_qi") not in FIVE_QI:
            raise ValueError(f"ue{index}: 5QI {ue.get('five_qi')} is not in the supported table {sorted(FIVE_QI)}")
        names = set()
        for flow in ue.get("flows") or []:
            if flow["name"] in names:
                raise ValueError(f"ue{index}: duplicate flow {flow['name']}")
            names.add(flow["name"])
            if flow.get("direction") not in ("dl", "ul"):
                raise ValueError(f"ue{index}.{flow['name']}: direction must be dl or ul")
            if flow.get("pattern") not in FLOW_PATTERNS:
                raise ValueError(f"ue{index}.{flow['name']}: pattern must be one of {FLOW_PATTERNS}")
            size = flow.get("size_bytes")
            if not isinstance(size, int) or not 64 <= size <= 1400:
                raise ValueError(f"ue{index}.{flow['name']}: size_bytes must be 64..1400")
            if flow["pattern"] == "periodic":
                if not flow.get("interval_ms", 0) > 0:
                    raise ValueError(f"ue{index}.{flow['name']}: periodic needs interval_ms > 0")
            elif not flow.get("rate_mbps", 0) > 0:
                raise ValueError(f"ue{index}.{flow['name']}: needs rate_mbps > 0")
            if flow["pattern"] == "onoff" and not (flow.get("on_mean_s", 0) > 0 and flow.get("off_mean_s", 0) > 0):
                raise ValueError(f"ue{index}.{flow['name']}: onoff needs on_mean_s and off_mean_s > 0")
        if len(ue["flows"]) > 10:
            raise ValueError(f"ue{index}: at most 10 flows (DL port block of 10)")


def offered_rate_mbps(flow: dict[str, Any]) -> float:
    """Mean offered UDP payload rate of one flow."""

    if flow["pattern"] == "periodic":
        return flow["size_bytes"] * 8 / (flow["interval_ms"] / 1000.0) / 1e6
    if flow["pattern"] == "onoff":
        duty = flow["on_mean_s"] / (flow["on_mean_s"] + flow["off_mean_s"])
        return flow["rate_mbps"] * duty
    return float(flow["rate_mbps"])
