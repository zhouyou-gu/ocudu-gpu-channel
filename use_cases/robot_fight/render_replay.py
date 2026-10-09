#!/usr/bin/env python3
"""Replay video of a robot fight over the emulated link (R6a).

Reads the log directory of a native gate run that carried the robot-fight
hooks (multi-UE gate: one broker/bridge; robot-fight gate: broker-a/-b and
sionna-status-a/-b, one cell per robot) and renders, at wall-clock speed,

  left   a top-down view of the ring (fence, pillars and gNB direction from
         the ring scene manifest, both robots with heading, trail and — for
         balance bots — a pitch indicator; ring-out / fall flash and the
         winner banner),
  right  the link of each robot on the same time axis: brain→robot one-way
         latency with the brain's RTT p50/p99 for the fight, stale intervals
         shaded; srsUE DL SNR with out-of-sync / release markers; the
         emulator call time of each robot's broker (cpu_stage_timings
         process_us, per second) with starvations per second when the broker
         prints them (R4c heartbeat); Sionna solve time and position age.

The video clock is the arena's wall clock, so a 12 s fight is 12 s of video:
if the emulator were not real time the trace would visibly drift from the
robots. `--summary-png` draws the whole run on one page.

`--view 2d|3d|both` (default both): the 2-D top-down view is matplotlib; the
3-D view replays the arena's own MJCF (arena.build_model_xml) with the ring
fence, pillars and gNB mast from the scene manifest as visual geoms, poses set
from the log, rendered offscreen with MuJoCo (MUJOCO_GL=osmesa; EGL where it
works). Without a working OpenGL backend `--view 3d/both` fails with a clear
error and `--view 2d` still works.

Usage:
  render_replay.py <log_dir> --out fight.mp4 [--fight N] [--max-fights K]
                   [--speed 1.0] [--fps 25] [--summary-png run.png]
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import os
import pathlib
import re
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import gridspec  # noqa: E402
from matplotlib.patches import Circle, Rectangle  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
MANIFEST = REPO / "use_cases" / "configs" / "sionna" / "scenes" / "robot_ring" / "manifest.json"

KV_RE = re.compile(r"(\w+)=(-?[0-9.]+)")
UE_LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6})")
LINK_EVENTS = {
    "out_of_sync": re.compile(r"out-of-sync", re.I),
    "in_sync": re.compile(r"in-sync", re.I),
    "rrc_release": re.compile(r"RRC Release|Connection Release|Received RRCRelease", re.I),
    "rlf": re.compile(r"radio link failure|RLF", re.I),
}

COLOURS = {"A": "#1f77b4", "B": "#d62728"}


def percentile(values, q):
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * q / 100.0
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def read_jsonl(path: pathlib.Path):
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def birth_unix_ms(path: pathlib.Path) -> int | None:
    """The broker log has no absolute timestamps; its `t` is seconds since the
    broker started. Copies of a run carry `<log>.birth_unix_ms` (from
    `stat -c %W` on the machine that ran it); on the original host st_birthtime
    works."""
    side = path.with_name(path.name + ".birth_unix_ms")
    if side.exists():
        try:
            v = float(side.read_text().strip())
            return int(v if v > 1e11 else v * 1000)
        except ValueError:
            pass
    try:
        bt = getattr(os.stat(path), "st_birthtime", None)
        if bt:
            return int(bt * 1000)
    except OSError:
        pass
    return None


# --- data model ---------------------------------------------------------------

@dataclass
class Robot:
    index: int
    node: str
    side: str            # "A" | "B"
    broker: str          # key into RunData.brokers
    bridge: str          # key into RunData.bridges
    sched: str | None    # plain | protected | None


@dataclass
class Fight:
    number: int
    t0_ms: int
    t1_ms: int
    winner_node: str | None
    reason: str | None
    rtf: float | None
    bot: str
    ring_radius_m: float
    poses: list                      # [(t_ms, [pose dict per robot])]
    cmds: dict                       # robot index -> [(t_ms, one_way_ms)]
    stale: dict                      # robot index -> [(t_begin_ms, t_end_ms)]
    result_t_ms: int | None
    rtt_ms: dict                     # robot index -> {"p50":..,"p99":..}
    loser_index: int | None


@dataclass
class RunData:
    log_dir: pathlib.Path
    run_id: str
    robots: list
    fights: list
    brokers: dict = field(default_factory=dict)   # key -> {"proc": {node: [(t_ms, us)]}, "starv": {dev: [(t_ms, n)]}, "stalls": [t_ms], "kernel": [(t_ms, us)]}
    bridges: dict = field(default_factory=dict)   # key -> [(t_ms, gen_ms, age_ms, source)]
    ue_metrics: dict = field(default_factory=dict)  # node -> [(t_ms, dl_snr, dl_bler)]
    ue_events: dict = field(default_factory=dict)   # node -> [(t_ms, kind)]
    params: dict = field(default_factory=dict)
    contention_start_ms: int | None = None
    scene: dict = field(default_factory=dict)
    frame_offset: tuple = (0.0, 0.0, 0.0)


# --- loaders ------------------------------------------------------------------

def load_params(log_dir: pathlib.Path) -> dict:
    for cand in (log_dir / "report" / "run-parameters.json",
                 pathlib.Path(str(log_dir).replace("/results/logs/", "/results/reports/")) / "run-parameters.json"):
        if cand.exists():
            try:
                return json.loads(cand.read_text())
            except json.JSONDecodeError:
                return {}
    return {}


def load_broker(path: pathlib.Path) -> dict:
    start = birth_unix_ms(path)
    out = {"proc": {}, "starv": {}, "stalls": [], "kernel": [], "start_ms": start, "has_starv": False}
    if not path.exists():
        return out
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("event=cpu_stage_timings"):
                f = dict(KV_RE.findall(line))
                node = re.search(r"node=(\S+)", line)
                if node and start is not None:
                    out["proc"].setdefault(node.group(1), []).append(
                        (start + int(float(f.get("t", 0)) * 1000), float(f.get("process_us", 0))))
            elif line.startswith("event=heartbeat"):
                m = re.match(r"event=heartbeat t=(\d+) dev=(\S+)", line)
                if not m or start is None:
                    continue
                f = dict(KV_RE.findall(line))
                if "starvations_total" in f:
                    out["has_starv"] = True
                    out["starv"].setdefault(m.group(2), []).append(
                        (start + int(m.group(1)) * 1000, int(float(f.get("starvations", 0)))))
            elif line.startswith("event=gpu_timings"):
                f = dict(KV_RE.findall(line))
                if start is not None:
                    out["kernel"].append((start + int(float(f.get("t", 0)) * 1000), float(f.get("kernel_us", 0))))
            elif line.startswith("event=node_stall ") and start is not None:
                # no t= on this line; it is attributed to the last heartbeat second
                last = max((v[-1][0] for v in out["proc"].values() if v), default=None)
                if last is not None:
                    out["stalls"].append(last)
    return out


def load_bridge(path: pathlib.Path) -> list:
    rows = []
    for rec in read_jsonl(path):
        if rec.get("event") != "sionna_rt_update" or "update_started_unix_ms" not in rec:
            continue
        ps = rec.get("position_status") or {}
        rows.append((int(rec["update_started_unix_ms"]), (rec.get("timing_ms") or {}).get("channel_generation"),
                     ps.get("last_sample_age_ms"), rec.get("position_source")))
    rows.sort()
    return rows


def load_metrics(log_dir: pathlib.Path, ue: str) -> list:
    csv_path = log_dir / f"srsue-metrics-{ue}.csv"
    start_path = log_dir / f"srsue-{ue}.start_unix_ms"
    if not csv_path.exists() or not start_path.exists():
        return []
    start_ms = int(start_path.read_text().strip())
    rows = []
    with csv_path.open(encoding="utf-8", errors="replace") as fh:
        for raw in csv.DictReader(fh, delimiter=";"):
            try:  # the `#eof` row has no numeric fields
                rows.append((start_ms + int(float(raw["time"])), float(raw["dl_snr"]), float(raw["dl_bler"])))
            except (KeyError, ValueError, TypeError):
                continue
    return rows


def load_ue_events(log_dir: pathlib.Path, ue: str) -> list:
    path = log_dir / f"srsue-{ue}-internal.events.log"
    if not path.exists():
        path = log_dir / f"srsue-{ue}-internal.log"
    if not path.exists():
        return []
    events = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            kind = next((k for k, pat in LINK_EVENTS.items() if pat.search(line)), None)
            if kind is None:
                continue
            m = UE_LOG_TS.match(line)
            if not m:
                continue
            try:
                t_ms = int(datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S.%f")
                           .replace(tzinfo=timezone.utc).timestamp() * 1000)
            except ValueError:
                continue
            events.append((t_ms, kind))
    return events


def load_fight(summary: dict, log_dir: pathlib.Path, n_robots: int) -> Fight:
    fdir = log_dir / "fights" / f"f{summary['fight']:03d}"
    if not fdir.exists() and summary.get("dir"):
        fdir = log_dir / "fights" / pathlib.Path(summary["dir"]).name
    poses, cmds, stale, open_stale = [], {}, {}, {}
    result_t, bot, radius, loser = None, "sumo", 2.0, None
    for ev in read_jsonl(fdir / "arena.jsonl"):
        kind = ev.get("event")
        t_ms = int(ev["t_unix_us"] / 1000) if "t_unix_us" in ev else None
        if kind == "spawn":
            bot = ev.get("bot", "sumo")
            radius = float(ev.get("ring_radius_m", radius))
        elif kind == "pose" and t_ms is not None:
            poses.append((t_ms, ev.get("robots") or []))
        elif kind == "cmd" and t_ms is not None and ev.get("one_way_us") is not None:
            cmds.setdefault(int(ev["robot"]), []).append((t_ms, ev["one_way_us"] / 1000.0))
        elif kind == "stale_begin" and t_ms is not None:
            open_stale[int(ev["robot"])] = t_ms
        elif kind == "stale_end" and t_ms is not None:
            r = int(ev["robot"])
            t_begin = open_stale.pop(r, t_ms - int(float(ev.get("duration_s", 0)) * 1000))
            stale.setdefault(r, []).append((t_begin, t_ms))
        elif kind == "result":
            result_t = t_ms
            w = ev.get("winner")
            if isinstance(w, int) and n_robots == 2:
                loser = 1 - w
            poses.append((t_ms, ev.get("robots") or [])) if t_ms is not None else None
    for r, t_begin in open_stale.items():
        stale.setdefault(r, []).append((t_begin, summary["t_end_unix_ms"]))
    rtt = {}
    for b in summary.get("brains") or []:
        d = b.get("rtt_us") or {}
        if d.get("p50") is not None:
            rtt[int(b["robot_id"])] = {"p50": d["p50"] / 1000.0, "p99": (d.get("p99") or d["p50"]) / 1000.0}
    poses.sort(key=lambda p: p[0])
    return Fight(number=int(summary["fight"]), t0_ms=int(summary["t_start_unix_ms"]),
                 t1_ms=int(summary["t_end_unix_ms"]), winner_node=summary.get("winner_node"),
                 reason=summary.get("reason"), rtf=summary.get("rtf"), bot=bot, ring_radius_m=radius,
                 poses=poses, cmds=cmds, stale=stale, result_t_ms=result_t, rtt_ms=rtt, loser_index=loser)


def parse_offset(params: dict, override: str | None) -> tuple:
    text = override
    if text is None:
        for key in ("sionna_position_offset", "sionna_position_frame_offset", "position_offset"):
            if params.get(key):
                text = str(params[key])
                break
    if text is None:
        m = re.search(r"RF_OFFSET=([-0-9.,]+)", json.dumps(params))
        text = m.group(1) if m else None
    if not text:
        return (0.0, 0.0, 0.0)
    try:
        parts = [float(v) for v in text.split(",")]
        return tuple(parts + [0.0] * (3 - len(parts)))[:3]
    except ValueError:
        return (0.0, 0.0, 0.0)


def load_run(log_dir: pathlib.Path, frame_offset: str | None = None, manifest: pathlib.Path | None = None) -> RunData:
    log_dir = pathlib.Path(log_dir)
    params = load_params(log_dir)
    summaries = read_jsonl(log_dir / "fights" / "summary.jsonl")
    # node ids: from the first summary's robots, else ue0/ue1
    nodes = []
    for s in summaries:
        nodes = [r.get("node_id") for r in (s.get("robots") or []) if r.get("node_id")]
        if nodes:
            break
    if not nodes:
        nodes = ["ue0", "ue1"]
    two_cell = (log_dir / "broker-a.log").exists()
    sched = (params.get("broker_sched") or {}) if isinstance(params.get("broker_sched"), dict) else {}
    robots = []
    for i, node in enumerate(nodes[:2]):
        cell = "ab"[i] if two_cell else "default"
        robots.append(Robot(index=i, node=node, side="AB"[i], broker=cell, bridge=cell, sched=sched.get(cell)))
    # side A is the protected robot when exactly one is protected
    prot = [r for r in robots if r.sched == "protected"]
    if len(prot) == 1 and prot[0].index == 1:
        robots[0].side, robots[1].side = "B", "A"
    run = RunData(log_dir=log_dir, run_id=log_dir.name, robots=robots, fights=[], params=params)
    run.fights = [load_fight(s, log_dir, len(robots)) for s in summaries]
    if two_cell:
        for cell in "ab":
            run.brokers[cell] = load_broker(log_dir / f"broker-{cell}.log")
            run.bridges[cell] = load_bridge(log_dir / f"sionna-status-{cell}.jsonl")
    else:
        run.brokers["default"] = load_broker(log_dir / "broker.log")
        run.bridges["default"] = load_bridge(log_dir / "sionna-status.jsonl")
    for r in robots:
        run.ue_metrics[r.node] = load_metrics(log_dir, r.node)
        run.ue_events[r.node] = load_ue_events(log_dir, r.node)
    cs = log_dir / "contention.start_unix_ms"
    if cs.exists():
        try:
            run.contention_start_ms = int(cs.read_text().strip())
        except ValueError:
            pass
    man = manifest or MANIFEST
    if man.exists():
        try:
            run.scene = json.loads(man.read_text())
        except json.JSONDecodeError:
            run.scene = {}
    run.frame_offset = parse_offset(params, frame_offset)
    if frame_offset is None and run.frame_offset == (0.0, 0.0, 0.0):
        inferred = infer_offset(run)
        if inferred is not None:
            run.frame_offset = inferred
    return run


def infer_offset(run: RunData):
    """scene = arena + offset: compare the first fight's spawn pose with the
    first external position the bridge used after that spawn."""
    if not run.fights or not run.fights[0].poses:
        return None
    f = run.fights[0]
    t_spawn, poses = f.poses[0]
    r = run.robots[0]
    p = next((q for q in poses if q.get("robot") == r.index), None)
    rows = read_jsonl(run.log_dir / ("sionna-status.jsonl" if r.bridge == "default" else f"sionna-status-{r.bridge}.jsonl"))
    for rec in rows:
        if rec.get("event") != "sionna_rt_update" or rec.get("position_source") != "external":
            continue
        t = rec.get("update_started_unix_ms", 0)
        if t < t_spawn or t > t_spawn + 1500:
            continue
        pos = (rec.get("positions") or {}).get(r.node)
        if p is None or not pos:
            return None
        return (round(pos[0] - p["x"], 2), round(pos[1] - p["y"], 2), 0.0)
    return None


# --- helpers ------------------------------------------------------------------

def window(rows, t0, t1, key=0):
    """rows sorted by rows[i][key]; return those within [t0, t1]."""
    keys = [r[key] for r in rows]
    lo = bisect.bisect_left(keys, t0)
    hi = bisect.bisect_right(keys, t1)
    return rows[lo:hi]


def robot_label(run: RunData, r: Robot) -> str:
    cell = "" if r.broker == "default" else f" · broker {r.broker}"
    sched = f" ({r.sched})" if r.sched else ""
    return f"{r.side}: {r.node}{cell}{sched}"


def node_colour(run: RunData, node: str) -> str:
    for r in run.robots:
        if node == r.node or node.startswith(r.node + "_"):
            return COLOURS[r.side]
    return "#555"


def contention_label(params: dict) -> str:
    c = params.get("contention")
    if not isinstance(c, dict) or c.get("kind") in (None, "none"):
        return "contention: none"
    hog = c.get("hog") or {}
    bits = [str(c.get("kind"))]
    if hog.get("kernel_us"):
        bits.append(f"hog {hog['kernel_us']} µs")
    if hog.get("mps_client"):
        bits.append("MPS")
    if c.get("sionna_mps_client"):
        bits.append("Sionna in MPS")
    return "contention: " + ", ".join(bits)


# --- 3-D replay scene -----------------------------------------------------------

class Scene3D:
    """The arena's MJCF plus visual geoms for the fence, pillars and mast, posed
    from the log (no physics): free joint = (x, y, z, quat(yaw, pitch)), wheels
    spun from the forward speed."""

    def __init__(self, run: RunData, fight: Fight, width=640, height=480):
        os.environ.setdefault("MUJOCO_GL", "osmesa")
        import mujoco  # noqa: WPS433  (needs MUJOCO_GL before import)
        sys.path.insert(0, str(HERE))
        import arena as arena_mod  # noqa: WPS433
        self.mujoco, self.arena_mod = mujoco, arena_mod
        self.run, self.fight = run, fight
        R = fight.ring_radius_m
        xml = arena_mod.build_model_xml(R, bot_type=fight.bot)
        # bot colours follow the sides (A blue, B red)
        side_rgba = {"A": "0.12 0.47 0.71 1", "B": "0.84 0.15 0.16 1"}
        xml = xml.replace('rgba="0.2 0.4 0.9 1"', f'rgba="{side_rgba[run.robots[0].side]}"')
        xml = xml.replace('rgba="0.9 0.5 0.15 1"', f'rgba="{side_rgba[run.robots[1].side if len(run.robots) > 1 else "B"]}"')
        xml = xml.replace('<visual><global offwidth="960" offheight="540"/></visual>',
                          f'<visual><global offwidth="{width}" offheight="{height}" fovy="50"/><quality shadowsize="2048"/>'
                          '<headlight ambient="0.35 0.35 0.35" diffuse="0.5 0.5 0.5"/></visual>')
        xml = xml.replace('<texture name="grid" type="2d"',
                          '<texture type="skybox" builtin="gradient" rgb1="0.55 0.7 0.9" rgb2="0.9 0.95 1" width="256" height="256"/>'
                          '<texture name="grid" type="2d"')
        xml = xml.replace('rgba="0.75 0.2 0.15 0.8"', 'rgba="0.93 0.88 0.78 1"')  # ring floor: sand, not the arena's red
        xml = xml.replace('<light pos="0 0 6" dir="0 0 -1" diffuse="0.9 0.9 0.9"/>',
                          '<light pos="0 0 6" dir="0 0 -1" diffuse="0.9 0.9 0.9"/>'
                          '<light pos="-6 -6 8" dir="0.5 0.5 -0.7" diffuse="0.5 0.5 0.5" castshadow="true"/>')
        xml = xml.replace(f'size="{R * 3} {R * 3} 0.1" material="floor"', f'size="{max(R * 4, 18.0)} {max(R * 4, 18.0)} 0.1" material="floor"')
        ox, oy, _ = run.frame_offset
        extra = []
        # fence ring (visual only)
        n_seg = 48
        for k in range(n_seg):
            a0 = 2 * math.pi * k / n_seg
            cx, cy = R * math.cos(a0 + math.pi / n_seg), R * math.sin(a0 + math.pi / n_seg)
            half = R * math.tan(math.pi / n_seg) * 1.05
            extra.append(f'<geom type="box" pos="{cx:.4f} {cy:.4f} 0.15" size="0.02 {half:.4f} 0.15" '
                         f'euler="0 0 {math.degrees(a0 + math.pi / n_seg):.2f}" rgba="0.55 0.55 0.6 1" contype="0" conaffinity="0"/>')
        for pil in run.scene.get("pillars") or []:
            cx, cy = pil["centre_m"][0] - ox, pil["centre_m"][1] - oy
            sd, h = pil.get("side_m", 0.8) / 2, pil.get("height_m", 3.0)
            extra.append(f'<geom type="box" pos="{cx:.3f} {cy:.3f} {h / 2:.3f}" size="{sd} {sd} {h / 2}" '
                         f'rgba="0.54 0.5 0.44 1" contype="0" conaffinity="0"/>')
        mast = run.scene.get("mast") or {}
        if mast.get("xy_m"):
            mx, my, mh = mast["xy_m"][0] - ox, mast["xy_m"][1] - oy, mast.get("height_m", 7.5)
            extra.append(f'<geom type="cylinder" pos="{mx:.3f} {my:.3f} {mh / 2:.3f}" size="0.12 {mh / 2}" '
                         f'rgba="0.75 0.75 0.78 1" contype="0" conaffinity="0"/>')
            extra.append(f'<geom type="box" pos="{mx:.3f} {my:.3f} {mh + 0.35:.3f}" size="0.08 0.3 0.35" '
                         f'rgba="0.2 0.7 0.45 1" contype="0" conaffinity="0"/>')
            self.mast_xy = (mx, my)
        else:
            self.mast_xy = (R + 8, 0.0)
        xml = xml.replace("  <worldbody>\n", "  <worldbody>\n    " + "\n    ".join(extra) + "\n", 1)
        self.model = mujoco.MjModel.from_xml_string(xml)
        self.data = mujoco.MjData(self.model)
        self.qpos_addr = [self.model.jnt_qposadr[mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, f"bot{i}_free")] for i in range(2)]
        self.wheel_addr = [tuple(self.model.jnt_qposadr[mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, f"bot{i}_{w}")]
                                 for w in ("wl", "wr")) for i in range(2)]
        self.wheel_angle = [0.0, 0.0]
        self.last_t_ms = None
        self.renderer = mujoco.Renderer(self.model, height, width)
        self.cam = mujoco.MjvCamera()
        self.cam.type = mujoco.mjtCamera.mjCAMERA_FREE
        # elevated side view: the camera sits beside the ring (90 deg off the mast
        # direction) so the pillars are not in the way; the mast shows at the
        # right edge, the pillars on the left
        ang = math.atan2(self.mast_xy[1], self.mast_xy[0])
        self.cam.lookat[:] = (0.1 * R * math.cos(ang), 0.1 * R * math.sin(ang), 0.3)
        self.cam.distance = 2.4 * R
        self.cam.azimuth = math.degrees(ang) + 90.0   # camera position = lookat - distance * dir(azimuth, elevation)
        self.cam.elevation = -33.0
        self.opt = mujoco.MjvOption()
        self.opt.flags[mujoco.mjtVisFlag.mjVIS_CONTACTPOINT] = False

    @staticmethod
    def _quat(yaw: float, pitch: float):
        """q_yaw(z) * q_pitch(body y): + pitch leans the body toward its heading,
        matching arena.Arena.lean() (up · heading = sin(pitch))."""
        cy, sy = math.cos(yaw / 2), math.sin(yaw / 2)
        cp, sp = math.cos(pitch / 2), math.sin(pitch / 2)
        return (cy * cp, -sy * sp, cy * sp, sy * cp)

    def render(self, t_ms: int, poses: list):
        mj, d = self.mujoco, self.data
        dt = 0.0 if self.last_t_ms is None else max(0.0, (t_ms - self.last_t_ms) / 1000.0)
        self.last_t_ms = t_ms
        for r in self.run.robots:
            p = next((q for q in poses if q.get("robot") == r.index), None)
            if p is None:
                continue
            a = self.qpos_addr[r.index]
            yaw = p.get("yaw", 0.0)
            pitch = p.get("pitch", 0.0) if self.fight.bot == "balance" else 0.0
            up = p.get("up", 1.0)
            if self.fight.bot != "balance" and up < 0.95:
                pitch = math.acos(max(-1.0, min(1.0, up)))  # fallen sumo bot: tip it over
            d.qpos[a:a + 3] = (p["x"], p["y"], p.get("z", self.arena_mod.WHEEL_RADIUS))
            d.qpos[a + 3:a + 7] = self._quat(yaw, pitch)
            v = p.get("vx", 0.0) * math.cos(yaw) + p.get("vy", 0.0) * math.sin(yaw)
            self.wheel_angle[r.index] += v / self.arena_mod.WHEEL_RADIUS * dt
            for wa in self.wheel_addr[r.index]:
                d.qpos[wa] = self.wheel_angle[r.index]
        mj.mj_forward(self.model, d)
        self.renderer.update_scene(d, self.cam, self.opt)
        return self.renderer.render()

    def close(self):
        try:
            self.renderer.close()
        except Exception:  # noqa: BLE001
            pass


# --- frame renderer -----------------------------------------------------------

class FightRenderer:
    """One matplotlib figure per fight; static traces drawn once, the cursor,
    robots and banner updated per frame."""

    def __init__(self, run: RunData, fight: Fight, width=1280, height=720, dpi=100, hold_s=1.5, view: str = "2d"):
        self.run, self.fight = run, fight
        self.hold_s = hold_s
        self.view = view
        self.scene3d = Scene3D(run, fight) if view in ("3d", "both") else None
        self.fig = plt.figure(figsize=(width / dpi, height / dpi), dpi=dpi)
        self.fig.patch.set_facecolor("white")
        if view == "both":
            gs = gridspec.GridSpec(4, 2, width_ratios=[1.0, 1.15], height_ratios=[1, 1, 1, 1],
                                   left=0.04, right=0.985, top=0.885, bottom=0.07, wspace=0.18, hspace=0.5)
            self.ax_3d = self.fig.add_subplot(gs[0:2, 0])
            self.ax_ring = self.fig.add_subplot(gs[2:4, 0])
        elif view == "3d":
            gs = gridspec.GridSpec(4, 2, width_ratios=[1.0, 1.15], height_ratios=[1, 1, 1, 1],
                                   left=0.02, right=0.985, top=0.885, bottom=0.07, wspace=0.14, hspace=0.5)
            self.ax_3d = self.fig.add_subplot(gs[:, 0])
            self.ax_ring = None
        else:
            gs = gridspec.GridSpec(4, 2, width_ratios=[1.0, 1.15], height_ratios=[1, 1, 1, 1],
                                   left=0.04, right=0.985, top=0.885, bottom=0.07, wspace=0.18, hspace=0.5)
            self.ax_3d = None
            self.ax_ring = self.fig.add_subplot(gs[:, 0])
        if self.ax_3d is not None:
            self.ax_3d.set_axis_off()
            self.im_3d = self.ax_3d.imshow(np.zeros((480, 640, 3), dtype=np.uint8), interpolation="bilinear")
            self.banner_3d = self.ax_3d.text(0.5, 0.94, "", transform=self.ax_3d.transAxes, ha="center", va="center",
                                             fontsize=15, weight="bold", color="#111",
                                             bbox=dict(boxstyle="round", fc="#ffe680", ec="#c90"), visible=False, zorder=20)
            self.pitch_text_3d = self.ax_3d.text(0.01, 0.02, "", transform=self.ax_3d.transAxes, fontsize=9,
                                                 family="monospace", color="white", va="bottom",
                                                 bbox=dict(boxstyle="round,pad=0.2", fc="#000", ec="none", alpha=0.45))
            self.flash_3d = Rectangle((0, 0), 1, 1, transform=self.ax_3d.transAxes, fill=True, color="#e33", alpha=0.0, zorder=10)
            self.ax_3d.add_patch(self.flash_3d)
        self.ax_lat = self.fig.add_subplot(gs[0, 1])
        self.ax_snr = self.fig.add_subplot(gs[1, 1], sharex=self.ax_lat)
        self.ax_brk = self.fig.add_subplot(gs[2, 1], sharex=self.ax_lat)
        self.ax_sio = self.fig.add_subplot(gs[3, 1], sharex=self.ax_lat)
        self.t0 = fight.t0_ms
        self.duration_s = max((fight.t1_ms - fight.t0_ms) / 1000.0, 0.5)
        self.end_s = self.duration_s + hold_s
        self._draw_static()

    # -- static ---------------------------------------------------------------
    def _rel(self, t_ms):
        return (t_ms - self.t0) / 1000.0

    def _draw_static(self):
        run, f = self.run, self.fight
        # header
        sides = "   ".join(robot_label(run, r) for r in run.robots)
        self.fig.text(0.5, 0.965, f"run {run.run_id} — fight {f.number}/{len(run.fights)}   ·   "
                      f"{sides}   ·   {contention_label(run.params)}",
                      ha="center", va="center", fontsize=10.5, color="#222")
        self.header = self.fig.text(0.5, 0.93, "", ha="center", va="center", fontsize=11, family="monospace")
        # ring
        R = f.ring_radius_m
        self.trails, self.bodies, self.headings, self.pitch_bars, self.labels = [], [], [], [], []
        ax = self.ax_ring
        if ax is None:
            self.loser_ring = self.banner = self.pitch_text = None
        else:
            self._draw_ring(ax, R)
        self._draw_strips()

    def _draw_ring(self, ax, R):
        run, f = self.run, self.fight
        ax.set_aspect("equal")
        pad = max(0.6, R * 0.35)
        ax.set_xlim(-R - pad, R + pad)
        ax.set_ylim(-R - pad, R + pad)
        ax.set_xlabel("x (m, arena frame)")
        ax.set_ylabel("y (m)")
        ax.grid(True, alpha=0.2)
        ax.add_patch(Circle((0, 0), R, fill=False, lw=3, color="#444"))
        ax.add_patch(Circle((0, 0), R, fill=True, color="#f3efe6", zorder=0))
        ox, oy, _ = run.frame_offset
        for p in run.scene.get("pillars") or []:
            cx, cy = p["centre_m"][0] - ox, p["centre_m"][1] - oy
            s = p.get("side_m", 0.8)
            if abs(cx) <= R + pad and abs(cy) <= R + pad:
                ax.add_patch(Rectangle((cx - s / 2, cy - s / 2), s, s, color="#8a7f70", alpha=0.85, zorder=1))
        mast = (run.scene.get("mast") or {}).get("xy_m")
        if mast:
            mx, my = mast[0] - ox, mast[1] - oy
            ang = math.atan2(my, mx)
            ax.annotate("", xy=((R + pad * 0.85) * math.cos(ang), (R + pad * 0.85) * math.sin(ang)),
                        xytext=((R + pad * 0.35) * math.cos(ang), (R + pad * 0.35) * math.sin(ang)),
                        arrowprops=dict(arrowstyle="-|>", lw=2, color="#2a7"))
            ax.text((R + pad * 0.6) * math.cos(ang), (R + pad * 0.6) * math.sin(ang) + 0.18,
                    f"gNB  {math.hypot(mx, my):.0f} m", color="#2a7", ha="center", fontsize=9)
        for r in run.robots:
            c = COLOURS[r.side]
            (trail,) = ax.plot([], [], "-", color=c, alpha=0.35, lw=1.5)
            body = Circle((0, 0), 0.16, color=c, zorder=5)
            ax.add_patch(body)
            (head,) = ax.plot([], [], "-", color="white", lw=2.5, zorder=6)
            (pbar,) = ax.plot([], [], "-", color="#111", lw=4, alpha=0.6, zorder=7)
            lab = ax.text(0, 0, r.side, color="white", ha="center", va="center", fontsize=9, zorder=8, weight="bold")
            self.trails.append(trail)
            self.bodies.append(body)
            self.headings.append(head)
            self.pitch_bars.append(pbar)
            self.labels.append(lab)
        self.loser_ring = Circle((0, 0), 0.3, fill=False, lw=3, color="#e33", zorder=9, visible=False)
        ax.add_patch(self.loser_ring)
        self.banner = ax.text(0, R + pad * 0.5, "", ha="center", va="center", fontsize=15, weight="bold",
                              color="#111", bbox=dict(boxstyle="round", fc="#ffe680", ec="#c90"), visible=False, zorder=20)
        self.pitch_text = ax.text(-R - pad * 0.9, -R - pad * 0.85, "", fontsize=9, family="monospace", va="bottom")

    def _draw_strips(self):
        run, f = self.run, self.fight
        t0w, t1w = f.t0_ms - 500, f.t1_ms + int(self.hold_s * 1000) + 500
        xs_end = self.end_s
        # 1. latency
        ax = self.ax_lat
        for r in run.robots:
            c = COLOURS[r.side]
            pts = f.cmds.get(r.index) or []
            if pts:
                ax.plot([self._rel(t) for t, _ in pts], [v for _, v in pts], ".", ms=2.5, color=c, alpha=0.7,
                        label=f"{r.side} one-way brain→robot")
            rtt = f.rtt_ms.get(r.index)
            if rtt:
                ax.axhline(rtt["p50"], color=c, lw=1, ls="--", alpha=0.8,
                           label=f"{r.side} brain RTT p50 {rtt['p50']:.0f} ms")
                ax.axhline(rtt["p99"], color=c, lw=1, ls=":", alpha=0.8, label=f"{r.side} RTT p99 {rtt['p99']:.0f} ms")
            for tb, te in f.stale.get(r.index) or []:
                ax.axvspan(self._rel(tb), self._rel(te), color=c, alpha=0.18, lw=0)
        ax.set_ylabel("latency (ms)")
        ax.set_title("brain → robot one-way latency (dots), brain RTT p50/p99 (lines), stale intervals (shaded)",
                     fontsize=8.5, loc="left")
        ax.set_ylim(bottom=0)
        ax.grid(True, alpha=0.25)
        # 2. SNR
        ax = self.ax_snr
        for r in run.robots:
            c = COLOURS[r.side]
            rows = window(run.ue_metrics.get(r.node) or [], t0w, t1w)
            xs = [self._rel(t) for t, s, _ in rows if s != 0.0]
            ys = [s for _, s, _ in rows if s != 0.0]
            if xs:
                ax.step(xs, ys, where="post", color=c, lw=1.5, label=f"{r.side} DL SNR")
            for t, kind in window(run.ue_events.get(r.node) or [], t0w, t1w):
                if kind in ("out_of_sync", "rrc_release", "rlf"):
                    ax.axvline(self._rel(t), color=c, lw=1.2, ls="-.", alpha=0.9)
                    ax.text(self._rel(t), ax.get_ylim()[1] if ax.lines else 30, kind, color=c, fontsize=7,
                            rotation=90, va="top")
        ax.set_ylabel("srsUE DL SNR (dB)")
        ax.set_title("UE reported downlink SNR (1 s metrics); out-of-sync / release marked", fontsize=8.5, loc="left")
        ax.grid(True, alpha=0.25)
        # 3. broker
        ax = self.ax_brk
        drawn = set()
        for r in run.robots:
            c = COLOURS[r.side]
            b = run.brokers.get(r.broker) or {}
            if (r.broker, "proc") in drawn and r.broker == "default":
                continue
            for node, rows in (b.get("proc") or {}).items():
                w = window(rows, t0w, t1w)
                if not w:
                    continue
                # one line per broker node; with one shared broker the UE nodes take their robot's colour
                col = c if r.broker != "default" else node_colour(run, node)
                ax.plot([self._rel(t) for t, _ in w], [v for _, v in w], "-", color=col, lw=1.3, alpha=0.9,
                        label=f"{r.side if r.broker != 'default' else 'broker'} {node} call")
            drawn.add((r.broker, "proc"))
        ax.axhline(1000, color="#999", lw=1, ls="--")
        ax.text(0.0, 1000, " slot 1 ms", color="#777", fontsize=7.5, va="bottom")
        ax.set_ylabel("emulator call (µs)")
        ax.set_yscale("log")
        ax.set_title("broker per-slot emulator call time (cpu_stage process_us, 1 s samples); starvations/s as bars",
                     fontsize=8.5, loc="left")
        ax.grid(True, alpha=0.25, which="both")
        any_starv = False
        ax2 = None
        for r in run.robots:
            b = run.brokers.get(r.broker) or {}
            if not b.get("has_starv"):
                continue
            for dev, rows in (b.get("starv") or {}).items():
                if not dev.startswith(r.node):
                    continue
                w = [(t, n) for t, n in window(rows, t0w, t1w) if n]
                if not w:
                    continue
                any_starv = True
                ax2 = ax2 or ax.twinx()
                ax2.bar([self._rel(t) for t, _ in w], [n for _, n in w], width=0.8, color=COLOURS[r.side], alpha=0.35)
        if ax2 is not None:
            ax2.set_ylabel("starvations / s")
        elif not any(b.get("has_starv") for b in run.brokers.values()):
            ax.set_title(ax.get_title(loc="left") + " — none in this broker log (pre-R4c)", fontsize=8.5, loc="left")
        for r in run.robots:
            b = run.brokers.get(r.broker) or {}
            for t in b.get("stalls") or []:
                if t0w <= t <= t1w:
                    ax.axvline(self._rel(t), color=COLOURS[r.side], lw=1, ls=":", alpha=0.8)
        # 4. sionna
        ax = self.ax_sio
        for r in run.robots:
            c = COLOURS[r.side] if r.bridge != "default" else "#2a7"
            rows = window(run.bridges.get(r.bridge) or [], t0w, t1w)
            if not rows:
                continue
            ax.plot([self._rel(t) for t, g, _, _ in rows if g is not None], [g for _, g, _, _ in rows if g is not None],
                    "-", color=c, lw=1.2, label=f"{r.side if r.bridge != 'default' else 'Sionna'} solve ms")
            ages = [(t, a) for t, _, a, src in rows if a is not None and src == "external"]
            if ages:
                ax.plot([self._rel(t) for t, _ in ages], [a for _, a in ages], ":", color=c, lw=1.2,
                        label=f"{r.side if r.bridge != 'default' else 'Sionna'} position age ms")
            if r.bridge == "default":
                break
        ax.set_ylabel("ms")
        ax.set_xlabel("time since fight start (s)")
        ax.set_title("Sionna RT: channel solve per update (solid) and age of the arena position it used (dotted; y clipped at 250 ms)",
                     fontsize=8.5, loc="left")
        ax.grid(True, alpha=0.25)
        ax.set_ylim(0, 250)  # between fights the arena is down and the age climbs to seconds; clip
        # contention start
        if run.contention_start_ms and f.t0_ms - 500 <= run.contention_start_ms <= f.t1_ms + 500:
            for a in (self.ax_lat, self.ax_snr, self.ax_brk, self.ax_sio):
                a.axvline(self._rel(run.contention_start_ms), color="#a0a", lw=1.5, ls="--")
            self.ax_lat.text(self._rel(run.contention_start_ms), self.ax_lat.get_ylim()[1], " contention on",
                             color="#a0a", fontsize=8, va="top")
        for a in (self.ax_lat, self.ax_snr, self.ax_brk, self.ax_sio):
            a.set_xlim(-0.2, self.end_s)
            a.tick_params(labelsize=8)
            if a.get_legend_handles_labels()[0]:
                a.legend(fontsize=7, loc="upper left", ncol=2, framealpha=0.7)
        self.cursors = [a.axvline(0, color="#111", lw=1.2) for a in (self.ax_lat, self.ax_snr, self.ax_brk, self.ax_sio)]
        self.pose_keys = [t for t, _ in f.poses]

    # -- dynamic --------------------------------------------------------------
    def _pose_at(self, t_ms):
        if not self.fight.poses:
            return None
        i = bisect.bisect_right(self.pose_keys, t_ms) - 1
        if i < 0:
            i = 0
        return self.fight.poses[i][1]

    def render(self, t_s: float):
        """Return the RGB frame at t_s seconds after the fight start."""
        run, f = self.run, self.fight
        t_ms = self.t0 + int(t_s * 1000)
        for c in self.cursors:
            c.set_xdata([min(t_s, self.duration_s)] * 2)
        poses = self._pose_at(t_ms) or []
        trail_from = t_ms - 2000
        lo = bisect.bisect_left(self.pose_keys, trail_from)
        hi = bisect.bisect_right(self.pose_keys, t_ms)
        pitch_bits = []
        for r in run.robots:
            p = next((q for q in poses if q.get("robot") == r.index), None)
            if p is None:
                continue
            if f.bot == "balance" and "pitch" in p:
                pitch_bits.append(f"{r.side} pitch {math.degrees(p['pitch']):+5.1f}°")
            else:
                pitch_bits.append(f"{r.side} up {p.get('up', 1.0):.2f}")
        ended = f.result_t_ms is not None and t_ms >= f.result_t_ms
        wr = next((r for r in run.robots if r.node == f.winner_node), None)
        banner_text = f"{wr.side} ({wr.node}) wins — {f.reason}" if wr else f"draw — {f.reason}"
        if self.scene3d is not None:
            self.im_3d.set_data(self.scene3d.render(t_ms, poses))
            self.pitch_text_3d.set_text("   ".join(pitch_bits))
            self.banner_3d.set_text(banner_text)
            self.banner_3d.set_visible(ended)
            flash = ended and (t_ms - f.result_t_ms) < 1000 and int((t_ms - f.result_t_ms) / 250) % 2 == 0
            self.flash_3d.set_alpha(0.18 if flash else 0.0)
        if self.ax_ring is None:
            self._set_header(t_s)
            self.fig.canvas.draw()
            return np.asarray(self.fig.canvas.buffer_rgba())[:, :, :3].copy()
        for r in run.robots:
            p = next((q for q in poses if q.get("robot") == r.index), None)
            if p is None:
                continue
            x, y, yaw = p["x"], p["y"], p.get("yaw", 0.0)
            self.bodies[r.index].center = (x, y)
            self.labels[r.index].set_position((x, y))
            self.headings[r.index].set_data([x, x + 0.22 * math.cos(yaw)], [y, y + 0.22 * math.sin(yaw)])
            tx = [q["x"] for _, ps in f.poses[lo:hi] for q in ps if q.get("robot") == r.index]
            ty = [q["y"] for _, ps in f.poses[lo:hi] for q in ps if q.get("robot") == r.index]
            self.trails[r.index].set_data(tx, ty)
            if f.bot == "balance" and "pitch" in p:
                L = 0.6 * p["pitch"]
                self.pitch_bars[r.index].set_data([x, x + L * math.cos(yaw)], [y, y + L * math.sin(yaw)])
        self.pitch_text.set_text("   ".join(pitch_bits))
        if ended:
            flash = int((t_ms - f.result_t_ms) / 250) % 2 == 0
            if f.loser_index is not None:
                self.loser_ring.center = self.bodies[f.loser_index].center
                self.loser_ring.set_visible(flash or (t_ms - f.result_t_ms) > 1000)
            self.banner.set_text(banner_text)
            self.banner.set_visible(True)
        else:
            self.loser_ring.set_visible(False)
            self.banner.set_visible(False)
        self._set_header(t_s)
        self.fig.canvas.draw()
        buf = np.asarray(self.fig.canvas.buffer_rgba())[:, :, :3].copy()
        return buf

    def _set_header(self, t_s):
        f = self.fight
        rtf = f"{f.rtf:.4f}" if isinstance(f.rtf, (int, float)) else "-"
        self.header.set_text(f"wall clock +{min(t_s, self.duration_s):6.2f} s   |   video = real time   |   "
                             f"arena RTF {rtf}   |   bots: {f.bot}")

    def close(self):
        if self.scene3d is not None:
            self.scene3d.close()
        plt.close(self.fig)


def iter_frames(run: RunData, fights: list, fps: int = 25, speed: float = 1.0, hold_s: float = 1.5,
                max_seconds: float | None = None, view: str = "2d"):
    """Yield (fight_number, t_s, frame) at video rate; wall-clock = video clock / speed."""
    for f in fights:
        rend = FightRenderer(run, f, hold_s=hold_s, view=view)
        n = int(math.ceil(rend.end_s * fps / speed))
        if max_seconds is not None:
            n = min(n, int(max_seconds * fps))
        try:
            for i in range(n):
                t_s = i * speed / fps
                yield f.number, t_s, rend.render(t_s)
        finally:
            rend.close()


# --- summary page -------------------------------------------------------------

def summary_png(run: RunData, out: pathlib.Path, width=1600, height=1000, dpi=100):
    fig = plt.figure(figsize=(width / dpi, height / dpi), dpi=dpi)
    gs = gridspec.GridSpec(5, 1, height_ratios=[0.8, 1, 1, 1, 1], left=0.06, right=0.98, top=0.93, bottom=0.06, hspace=0.5)
    axes = [fig.add_subplot(gs[i, 0]) for i in range(5)]
    if not run.fights:
        fig.text(0.5, 0.5, "no fights in this run", ha="center")
        fig.savefig(out)
        plt.close(fig)
        return
    t_anchor = run.fights[0].t0_ms
    rel = lambda t: (t - t_anchor) / 1000.0  # noqa: E731
    t_lo, t_hi = -5.0, rel(run.fights[-1].t1_ms) + 5.0
    sides = "   ".join(robot_label(run, r) for r in run.robots)
    wins = {}
    for f in run.fights:
        key = f.winner_node or "draw"
        wins[key] = wins.get(key, 0) + 1
    fig.suptitle(f"run {run.run_id} — {len(run.fights)} fights, wins {wins}   ·   {sides}   ·   {contention_label(run.params)}",
                 fontsize=11)
    # fights
    ax = axes[0]
    for f in run.fights:
        wr = next((r for r in run.robots if r.node == f.winner_node), None)
        c = COLOURS[wr.side] if wr else "#999"
        ax.axvspan(rel(f.t0_ms), rel(f.t1_ms), color=c, alpha=0.35, lw=0)
        ax.text((rel(f.t0_ms) + rel(f.t1_ms)) / 2, 0.5, f"{f.number}\n{(f.reason or '')[:8]}", ha="center", va="center", fontsize=6.5)
    ax.set_yticks([])
    ax.set_title("fights (colour = winner side, label = reason)", fontsize=9, loc="left")
    # latency per fight
    ax = axes[1]
    for r in run.robots:
        c = COLOURS[r.side]
        xs, p50, p99, ow = [], [], [], []
        for f in run.fights:
            rtt = f.rtt_ms.get(r.index)
            if rtt:
                xs.append(rel(f.t0_ms))
                p50.append(rtt["p50"])
                p99.append(rtt["p99"])
            pts = [v for _, v in f.cmds.get(r.index) or []]
            if pts:
                ow.append((rel(f.t0_ms), percentile(pts, 50)))
        if xs:
            ax.plot(xs, p50, "o-", color=c, ms=4, label=f"{r.side} brain RTT p50")
            ax.plot(xs, p99, "^:", color=c, ms=4, label=f"{r.side} RTT p99")
        if ow:
            ax.plot([x for x, _ in ow], [v for _, v in ow], "s--", color=c, ms=3, alpha=0.6, label=f"{r.side} one-way p50")
        for f in run.fights:
            for tb, te in f.stale.get(r.index) or []:
                ax.axvspan(rel(tb), rel(te), color=c, alpha=0.2, lw=0)
    ax.set_ylabel("ms")
    ax.set_title("per-fight link latency (brain RTT p50/p99, one-way p50), stale intervals shaded", fontsize=9, loc="left")
    ax.legend(fontsize=7, ncol=3)
    ax.grid(True, alpha=0.25)
    # snr
    ax = axes[2]
    for r in run.robots:
        c = COLOURS[r.side]
        rows = run.ue_metrics.get(r.node) or []
        xs = [rel(t) for t, s, _ in rows if s != 0.0 and t_lo <= rel(t) <= t_hi]
        ys = [s for t, s, _ in rows if s != 0.0 and t_lo <= rel(t) <= t_hi]
        if xs:
            ax.step(xs, ys, where="post", color=c, lw=1, label=f"{r.side} DL SNR")
        for t, kind in run.ue_events.get(r.node) or []:
            if kind in ("out_of_sync", "rrc_release", "rlf") and t_lo <= rel(t) <= t_hi:
                ax.axvline(rel(t), color=c, lw=0.8, ls="-.", alpha=0.7)
    ax.set_ylabel("dB")
    ax.set_title("srsUE downlink SNR; out-of-sync / release marked", fontsize=9, loc="left")
    ax.legend(fontsize=7, ncol=2)
    ax.grid(True, alpha=0.25)
    # broker
    ax = axes[3]
    ax2 = None
    done = set()
    for r in run.robots:
        b = run.brokers.get(r.broker) or {}
        if r.broker in done:
            continue
        done.add(r.broker)
        for node, rows in (b.get("proc") or {}).items():
            xs = [rel(t) for t, _ in rows if t_lo <= rel(t) <= t_hi]
            ys = [v for t, v in rows if t_lo <= rel(t) <= t_hi]
            if not xs:
                continue
            col = COLOURS[r.side] if r.broker != "default" else node_colour(run, node)
            ax.plot(xs, ys, "-", color=col, lw=0.9, alpha=0.9, label=f"{r.side if r.broker != 'default' else 'broker'} {node}")
        if b.get("has_starv"):
            for dev, rows in (b.get("starv") or {}).items():
                w = [(rel(t), n) for t, n in rows if n and t_lo <= rel(t) <= t_hi]
                if w:
                    ax2 = ax2 or ax.twinx()
                    ax2.bar([x for x, _ in w], [n for _, n in w], width=1.0, color=COLOURS[r.side], alpha=0.3)
    ax.axhline(1000, color="#999", lw=1, ls="--")
    ax.set_yscale("log")
    ax.set_ylabel("µs")
    if ax2 is not None:
        ax2.set_ylabel("starvations / s")
    ax.set_title("broker per-slot emulator call (process_us per second; dashed = 1 ms slot); starvations/s bars", fontsize=9, loc="left")
    ax.legend(fontsize=7, ncol=4)
    ax.grid(True, alpha=0.25, which="both")
    # sionna
    ax = axes[4]
    for r in run.robots:
        c = COLOURS[r.side] if r.bridge != "default" else "#2a7"
        rows = run.bridges.get(r.bridge) or []
        xs = [rel(t) for t, g, _, _ in rows if g is not None and t_lo <= rel(t) <= t_hi]
        ys = [g for t, g, _, _ in rows if g is not None and t_lo <= rel(t) <= t_hi]
        if xs:
            ax.plot(xs, ys, "-", color=c, lw=0.8, label=f"{r.side if r.bridge != 'default' else 'Sionna'} solve ms")
        ages = [(rel(t), a) for t, _, a, src in rows if a is not None and src == "external" and t_lo <= rel(t) <= t_hi]
        if ages:
            ax.plot([x for x, _ in ages], [a for _, a in ages], ":", color=c, lw=0.8, label="position age ms")
        if r.bridge == "default":
            break
    ax.set_ylabel("ms")
    ax.set_xlabel("time since first fight (s)")
    ax.set_title("Sionna RT solve per update and arena-position age", fontsize=9, loc="left")
    ax.legend(fontsize=7, ncol=2)
    ax.grid(True, alpha=0.25)
    for a in axes:
        a.set_xlim(t_lo, t_hi)
        a.tick_params(labelsize=8)
    if run.contention_start_ms:
        for a in axes:
            a.axvline(rel(run.contention_start_ms), color="#a0a", lw=1.2, ls="--")
        axes[0].text(rel(run.contention_start_ms), 1.02, "contention on", color="#a0a", fontsize=8, transform=axes[0].get_xaxis_transform())
    fig.savefig(out)
    plt.close(fig)


# --- synthetic run (for tests) ------------------------------------------------

def make_synthetic_run(root: pathlib.Path, seconds: float = 4.0, two_cell: bool = False, bot: str = "balance") -> pathlib.Path:
    """Write a minimal but complete log dir so the renderer can be exercised
    without a gate run. Times are unix ms around a fixed anchor."""
    root = pathlib.Path(root)
    fdir = root / "fights" / "f001"
    fdir.mkdir(parents=True, exist_ok=True)
    t0 = 1_790_000_000_000
    n_pose = int(seconds * 20)
    rows = []
    rows.append({"event": "spawn", "seed": 1, "ring_radius_m": 2.0, "bot": bot, "node_ids": ["ue0", "ue1"],
                 "t_unix_us": t0 * 1000, "sim_time_s": 0.0})
    cmds = []
    stale = []
    for i in range(n_pose + 1):
        t = t0 + i * 50
        a = i / n_pose
        robots = [{"robot": 0, "x": -1.0 + 1.6 * a, "y": 0.3 * math.sin(6 * a), "z": 0.06, "yaw": 0.2, "vx": 0.4, "vy": 0.0,
                   "wz": 0.0, "up": 1.0, "pitch": 0.1 * math.sin(20 * a), "pitch_rate": 0.0, "dist_to_edge_m": 1.0},
                  {"robot": 1, "x": 1.0 - 1.3 * a, "y": -0.2, "z": 0.06, "yaw": 3.1, "vx": -0.3, "vy": 0.0,
                   "wz": 0.0, "up": 1.0, "pitch": 0.4 * a, "pitch_rate": 0.0, "dist_to_edge_m": 1.0}]
        rows.append({"event": "pose", "robots": robots, "t_unix_us": t * 1000, "sim_time_s": i * 0.05})
    for r in (0, 1):
        for k in range(int(seconds * 100)):
            t = t0 + k * 10
            ow = 8000 + (3000 if r == 1 and 1.0 < k / 100 < 1.5 else 0) + (k % 7) * 100
            cmds.append({"event": "cmd", "robot": r, "seq": k, "t_send_us": (t - 8) * 1000, "one_way_us": ow,
                         "state_echo_seq": k, "brain_turnaround_us": 500, "left": 0.0, "right": 0.0, "ttl_ms": 60,
                         "t_unix_us": t * 1000, "sim_time_s": k * 0.01})
    stale.append({"event": "stale_begin", "robot": 1, "last_seq": 100, "t_unix_us": (t0 + 1000) * 1000, "sim_time_s": 1.0})
    stale.append({"event": "stale_end", "robot": 1, "duration_s": 0.3, "t_unix_us": (t0 + 1300) * 1000, "sim_time_s": 1.3})
    t_res = t0 + int(seconds * 1000)
    rows.append({"event": "result", "winner": 0, "reason": "fall", "radial_m": [0.5, 0.6],
                 "robots": rows[-1]["robots"], "t_unix_us": t_res * 1000, "sim_time_s": seconds})
    rows.append({"event": "rtf", "rtf": 1.0, "late_loops": 0, "loops": 100, "max_lag_ms": 1.0, "wall_time_s": seconds,
                 "t_unix_us": (t_res + 5) * 1000, "sim_time_s": seconds})
    with (fdir / "arena.jsonl").open("w") as fh:
        for ev in rows + cmds + stale:
            fh.write(json.dumps(ev) + "\n")
    for r in (0, 1):
        (fdir / f"brain{r}.jsonl").write_text(json.dumps({"event": "summary", "robot_id": r, "outcome": "won" if r == 0 else "lost"}) + "\n")
    summary = {"fight": 1, "seed": 1, "t_start_unix_ms": t0, "t_end_unix_ms": t_res + 10, "arena_status": 0,
               "dir": str(fdir), "winner": 0, "winner_node": "ue0", "reason": "fall", "rtf": 1.0, "late_loops": 0,
               "max_lag_ms": 1.0, "sim_time_s": seconds, "wall_time_s": seconds, "lockstep": False,
               "robots": [{"robot": 0, "node_id": "ue0", "stale_intervals": 0, "stale_total_s": 0.0,
                           "one_way_us": {"p50": 8300, "p99": 9000, "n": 100}},
                          {"robot": 1, "node_id": "ue1", "stale_intervals": 1, "stale_total_s": 0.3,
                           "one_way_us": {"p50": 8300, "p99": 11000, "n": 100}}],
               "brains": [{"robot_id": 0, "outcome": "won", "rtt_us": {"p50": 20000, "p99": 30000}},
                          {"robot_id": 1, "outcome": "lost", "rtt_us": {"p50": 21000, "p99": 60000}}]}
    (root / "fights" / "summary.jsonl").write_text(json.dumps(summary) + "\n")
    # brokers (start 3 s before the fight; 8 s of samples)
    cells = ["a", "b"] if two_cell else [None]
    for cell in cells:
        name = f"broker-{cell}.log" if cell else "broker.log"
        start_s = (t0 - 3000) // 1000
        lines = []
        for t in range(1, 9):
            for node, dev in (("gnb0", "gnb0_p0"), ("ue0", "ue0")) if cell in (None, "a") else (("gnb1", "gnb1_p0"), ("ue1", "ue1")):
                proc = 300 if cell != "b" else 2200 + 50 * t
                lines.append(f"event=cpu_stage_timings t={t} node={node} room_us=0 align_us=0 data_us=0 read_us=0 process_us={proc} throttle_us=0 push_us=0")
                st = 3 if (cell == "b" and t in (5, 6)) else 0
                lines.append(f"event=heartbeat t={t} dev={dev} ring=0/10 rx_ring=0/46080 puller[state=recv_reply pulls=1 idle=0 room_stall=0 last=0 acquired=0] producer[state=wait_data slots=1 stall=0 last=0] rep[state=wait_req replies=1 idle=0 row_spin=0 last=0] starvations={st} starvations_total={st} gaps=0 gaps_total=0 overflows=0 overflows_total=0")
            lines.append(f"event=gpu_timings t={t} h2d_us=7.0 kernel_us=24.0 d2h_us=1.0")
        (root / name).write_text("\n".join(lines) + "\n")
        (root / (name + ".birth_unix_ms")).write_text(str(start_s) + "\n")
        sname = f"sionna-status-{cell}.jsonl" if cell else "sionna-status.jsonl"
        with (root / sname).open("w") as fh:
            for k in range(int(seconds * 10) + 20):
                t = t0 - 1000 + k * 100
                fh.write(json.dumps({"event": "sionna_rt_update", "update_started_unix_ms": t,
                                     "timing_ms": {"channel_generation": 50 + (k % 5) * 3},
                                     "position_source": "external",
                                     "position_status": {"last_sample_age_ms": 20 + (k % 3) * 5},
                                     "positions": {"ue0": [0, 0, 0.4]}}) + "\n")
    for ue in ("ue0", "ue1"):
        (root / f"srsue-{ue}.start_unix_ms").write_text(str(t0 - 20000) + "\n")
        with (root / f"srsue-metrics-{ue}.csv").open("w") as fh:
            fh.write("time;cc;earfcn;pci;rsrp;pl;cfo;pci_neigh;rsrp_neigh;cfo_neigh;dl_mcs;dl_snr;dl_turbo;dl_brate;dl_bler;ul_ta;distance_km;speed_kmph;ul_mcs;ul_buff;ul_brate;ul_bler;rf_o;rf_u;rf_l;is_attached\n")
            for t in range(0, 30):
                fh.write(f"{t * 1000};0;1;1;40;0;0;n/a;n/a;n/a;13;{33 + (t % 4) - (6 if ue == 'ue1' and t > 22 else 0)};1;1;0;0;0;0;8;0;1;0;0;0;0;1\n")
            fh.write("#eof\n")
        ts = datetime.fromtimestamp((t0 + 1500) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")
        (root / f"srsue-{ue}-internal.events.log").write_text(f"{ts} [PHY] out-of-sync detected\n" if ue == "ue1" else "")
    if two_cell:
        (root / "report").mkdir(exist_ok=True)
        (root / "report" / "run-parameters.json").write_text(json.dumps({
            "broker_sched": {"a": "protected", "b": "plain"},
            "contention": {"kind": "busy", "hog": {"kernel_us": 200, "mps_client": 1}, "sionna_mps_client": 1}}))
        (root / "contention.start_unix_ms").write_text(str(t0 - 2000) + "\n")
    return root


# --- cli ----------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("log_dir", type=pathlib.Path)
    p.add_argument("--out", type=pathlib.Path, help="MP4 path (imageio-ffmpeg, libx264)")
    p.add_argument("--fight", type=int, action="append", help="fight number(s) to render (default: all)")
    p.add_argument("--max-fights", type=int, default=None)
    p.add_argument("--speed", type=float, default=1.0, help="playback speed; 1.0 = wall clock")
    p.add_argument("--fps", type=int, default=25)
    p.add_argument("--hold-s", type=float, default=1.5, help="seconds to hold the result banner")
    p.add_argument("--frame-offset", default=None, help="scene-frame offset x,y,z (arena origin in scene metres)")
    p.add_argument("--manifest", type=pathlib.Path, default=None, help="ring scene manifest (pillars/mast)")
    p.add_argument("--summary-png", type=pathlib.Path, default=None)
    p.add_argument("--max-seconds", type=float, default=None, help="cap seconds rendered per fight (debug)")
    p.add_argument("--view", choices=("2d", "3d", "both"), default="both",
                   help="left pane: 2-D top-down, 3-D MuJoCo replay (needs MUJOCO_GL=osmesa/egl), or both stacked")
    args = p.parse_args(argv)

    run = load_run(args.log_dir, frame_offset=args.frame_offset, manifest=args.manifest)
    if not run.fights:
        print(f"no fights under {args.log_dir}/fights", file=sys.stderr)
        return 1
    if args.summary_png:
        args.summary_png.parent.mkdir(parents=True, exist_ok=True)
        summary_png(run, args.summary_png)
        print(f"summary written: {args.summary_png}")
    if args.out:
        import imageio.v2 as imageio  # noqa: WPS433
        fights = run.fights
        if args.fight:
            wanted = set(args.fight)
            fights = [f for f in fights if f.number in wanted]
        if args.max_fights:
            fights = fights[: args.max_fights]
        args.out.parent.mkdir(parents=True, exist_ok=True)
        writer = imageio.get_writer(str(args.out), fps=args.fps, codec="libx264", quality=8, pixelformat="yuv420p",
                                    macro_block_size=8)
        n = 0
        try:
            for _, _, frame in iter_frames(run, fights, fps=args.fps, speed=args.speed, hold_s=args.hold_s,
                                           max_seconds=args.max_seconds, view=args.view):
                writer.append_data(frame)
                n += 1
        finally:
            writer.close()
        print(f"video written: {args.out} ({n} frames, {n / args.fps:.1f} s, {len(fights)} fights)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
