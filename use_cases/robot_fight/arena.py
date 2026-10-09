#!/usr/bin/env python3
"""MuJoCo sumo arena: two differential-drive push bots in a circular ring.

The arena is the physical world and the referee. Each robot is driven by a
separate brain process over UDP (see protocol.py); the arena never waits for a
brain. Simulation time tracks the wall clock, so a late or missing command has
a physical consequence instead of stalling the world.

Per robot the arena binds one UDP socket, or (--robot-unix) one unix datagram
socket served by a modem inside the UE's network namespace (modem.py). The
brain's address is learned from the first CMD that arrives on it (or fixed
with --state-dest). STATE datagrams go back at --state-hz. Robot positions are
published for the Sionna bridge on a ZMQ PUB socket at --pub-hz.

Exit: the fight ends when a robot's body centre leaves the ring, a robot falls
over, or the time limit passes. At the time limit the robot whose body centre
is closer to the edge loses (decided_by timeout_edge); it is a draw only when
the two radial distances differ by less than --timeout-margin-m. The result
JSON is written to --result and the per-fight JSONL log to --log.
"""

from __future__ import annotations

import argparse
import errno
import json
import math
import os
import pathlib
import random
import socket
import sys
import time
from dataclasses import dataclass, field

import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import protocol  # type: ignore
else:
    from . import protocol

try:
    import mujoco
except ImportError as exc:  # pragma: no cover - reported at startup
    raise SystemExit(f"mujoco is required for the arena: {exc}")


# --------------------------------------------------------------------------
# Robot and ring geometry (metres, kilograms). Chosen so a bot is a ~30 cm
# sumo-class wheeled pusher: fast enough that a 50-100 Hz control loop matters,
# heavy enough that pushes take a few hundred milliseconds to resolve.
WHEEL_RADIUS = 0.06
WHEEL_HALF_WIDTH = 0.02
WHEEL_Y = 0.14          # wheel centre offset from chassis centre
CHASSIS_HALF = (0.15, 0.12, 0.04)
CHASSIS_MASS = 2.0
BALLAST_MASS = 2.0      # low plate: keeps the CoM near the axle so pushes do not tip the bot
CASTER_RADIUS = 0.03
CASTER_CLEARANCE = 0.004  # casters float when level: the wheels carry the weight
PLATE_HALF = (0.02, 0.15, 0.03)
PLATE_Z = -0.01          # plate centred just below the chassis centre so pushes act below the opponent's CoM
WHEEL_MAX_RAD_S = 30.0  # actuator ctrl range; 30 rad/s * 0.06 m = 1.8 m/s
TIMESTEP = 0.001

# Balance bot (--bot balance): a two-wheeled inverted pendulum with no local
# balance controller -- the brain closes the balance loop over the link, so the
# link's delay and its stale intervals decide whether the bot stands. Wheel
# motors are torque actuators. The mass sits high (head at 0.95 m, CoM ~0.6 m,
# growth rate sqrt(g / l_com) ~4 rad/s). Tuned in closed loop through a delay
# (R2c): stands with 60 ms one-way delay at rest and 45 ms while driving at
# 0.6 m/s, survives a 300 ms blackout while driving, falls at 400 ms.
BAL_BASE_HALF = (0.08, 0.10, 0.02)
BAL_BASE_MASS = 1.0
BAL_POLE_RADIUS = 0.02
BAL_HEAD_Z = 0.95          # head centre above the axle
BAL_HEAD_HALF = 0.06
BAL_HEAD_MASS = 2.0
BAL_PLATE_HALF = (0.02, 0.12, 0.03)
BAL_TORQUE_MAX = 1.5       # N m per wheel
BAL_WHEEL_DAMPING = 0.02   # caps the free-running wheel speed at torque_max / damping
BAL_FALL_UP = 0.64         # body z axis below cos(50 deg) -> fallen (unrecoverable for the pendulum)
SUMO_FALL_UP = 0.3


def build_model_xml(ring_radius: float, wheel_max: float = WHEEL_MAX_RAD_S, bot_type: str = "sumo",
                    torque_max: float = BAL_TORQUE_MAX) -> str:
    def balance_bot(name: str, rgba: str) -> str:
        return f"""
    <body name="{name}" pos="0 0 {WHEEL_RADIUS}">
      <freejoint name="{name}_free"/>
      <geom name="{name}_chassis" type="box" size="{BAL_BASE_HALF[0]} {BAL_BASE_HALF[1]} {BAL_BASE_HALF[2]}"
            mass="{BAL_BASE_MASS}" rgba="{rgba}" friction="0.3 0.005 0.0001"/>
      <geom name="{name}_plate" type="box" pos="{BAL_BASE_HALF[0] + BAL_PLATE_HALF[0]} 0 0"
            size="{BAL_PLATE_HALF[0]} {BAL_PLATE_HALF[1]} {BAL_PLATE_HALF[2]}" mass="0.2" rgba="{rgba}"
            friction="0.05 0.005 0.0001"/>
      <geom name="{name}_pole" type="capsule" fromto="0 0 {BAL_BASE_HALF[2]} 0 0 {BAL_HEAD_Z - BAL_HEAD_HALF}"
            size="{BAL_POLE_RADIUS}" mass="0.3" rgba="{rgba}"/>
      <geom name="{name}_head" type="box" pos="0 0 {BAL_HEAD_Z}" size="{BAL_HEAD_HALF} {BAL_HEAD_HALF} {BAL_HEAD_HALF}"
            mass="{BAL_HEAD_MASS}" rgba="{rgba}"/>
      <body name="{name}_wheel_l" pos="0 {WHEEL_Y} 0">
        <joint name="{name}_wl" type="hinge" axis="0 1 0" damping="{BAL_WHEEL_DAMPING}"/>
        <geom type="cylinder" size="{WHEEL_RADIUS} {WHEEL_HALF_WIDTH}" euler="90 0 0" mass="0.2"
              rgba="0.1 0.1 0.1 1" friction="1.2 0.005 0.0001" condim="4"/>
      </body>
      <body name="{name}_wheel_r" pos="0 {-WHEEL_Y} 0">
        <joint name="{name}_wr" type="hinge" axis="0 1 0" damping="{BAL_WHEEL_DAMPING}"/>
        <geom type="cylinder" size="{WHEEL_RADIUS} {WHEEL_HALF_WIDTH}" euler="90 0 0" mass="0.2"
              rgba="0.1 0.1 0.1 1" friction="1.2 0.005 0.0001" condim="4"/>
      </body>
    </body>"""

    def bot(name: str, rgba: str) -> str:
        if bot_type == "balance":
            return balance_bot(name, rgba)
        return f"""
    <body name="{name}" pos="0 0 {WHEEL_RADIUS}">
      <freejoint name="{name}_free"/>
      <geom name="{name}_chassis" type="box" size="{CHASSIS_HALF[0]} {CHASSIS_HALF[1]} {CHASSIS_HALF[2]}"
            mass="{CHASSIS_MASS}" rgba="{rgba}" friction="0.3 0.005 0.0001"/>
      <geom name="{name}_plate" type="box" pos="{CHASSIS_HALF[0] + PLATE_HALF[0]} 0 {PLATE_Z}"
            size="{PLATE_HALF[0]} {PLATE_HALF[1]} {PLATE_HALF[2]}" mass="0.3" rgba="{rgba}"
            friction="0.05 0.005 0.0001"/>
      <geom name="{name}_ballast" type="box" pos="0 0 {-(CHASSIS_HALF[2] - 0.01)}" size="0.10 0.08 0.01"
            mass="{BALLAST_MASS}" rgba="{rgba}" contype="0" conaffinity="0"/>
      <geom name="{name}_caster_f" type="sphere" pos="{CHASSIS_HALF[0] - 0.02} 0 {-(WHEEL_RADIUS - CASTER_RADIUS) + CASTER_CLEARANCE}"
            size="{CASTER_RADIUS}" mass="0.05" rgba="0.2 0.2 0.2 1" friction="0.02 0.001 0.0001"/>
      <geom name="{name}_caster_b" type="sphere" pos="{-(CHASSIS_HALF[0] - 0.02)} 0 {-(WHEEL_RADIUS - CASTER_RADIUS) + CASTER_CLEARANCE}"
            size="{CASTER_RADIUS}" mass="0.05" rgba="0.2 0.2 0.2 1" friction="0.02 0.001 0.0001"/>
      <body name="{name}_wheel_l" pos="0 {WHEEL_Y} 0">
        <joint name="{name}_wl" type="hinge" axis="0 1 0" damping="0.01"/>
        <geom type="cylinder" size="{WHEEL_RADIUS} {WHEEL_HALF_WIDTH}" euler="90 0 0" mass="0.2"
              rgba="0.1 0.1 0.1 1" friction="1.2 0.005 0.0001" condim="4"/>
      </body>
      <body name="{name}_wheel_r" pos="0 {-WHEEL_Y} 0">
        <joint name="{name}_wr" type="hinge" axis="0 1 0" damping="0.01"/>
        <geom type="cylinder" size="{WHEEL_RADIUS} {WHEEL_HALF_WIDTH}" euler="90 0 0" mass="0.2"
              rgba="0.1 0.1 0.1 1" friction="1.2 0.005 0.0001" condim="4"/>
      </body>
    </body>"""

    if bot_type == "balance":
        actuators = "\n".join(
            f'    <motor name="bot{i}_{w}" joint="bot{i}_w{w}" ctrlrange="-{torque_max} {torque_max}" gear="1"/>'
            for i in range(2) for w in ("l", "r"))
    else:
        actuators = "\n".join(
            f'    <velocity name="bot{i}_{w}" joint="bot{i}_w{w}" kv="2.0" ctrlrange="-{wheel_max} {wheel_max}" forcerange="-3 3"/>'
            for i in range(2) for w in ("l", "r"))
    return f"""
<mujoco model="sumo_arena">
  <option timestep="{TIMESTEP}" gravity="0 0 -9.81" integrator="implicitfast"/>
  <default>
    <geom solref="0.02 1" solimp="0.9 0.95 0.001"/>
  </default>
  <visual><global offwidth="960" offheight="540"/></visual>
  <asset>
    <texture name="grid" type="2d" builtin="checker" rgb1="0.85 0.85 0.85" rgb2="0.7 0.7 0.7" width="256" height="256"/>
    <material name="floor" texture="grid" texrepeat="8 8" reflectance="0.1"/>
  </asset>
  <worldbody>
    <light pos="0 0 6" dir="0 0 -1" diffuse="0.9 0.9 0.9"/>
    <geom name="floor" type="plane" size="{ring_radius * 3} {ring_radius * 3} 0.1" material="floor"
          friction="1.0 0.005 0.0001"/>
    <geom name="ring" type="cylinder" pos="0 0 0.0005" size="{ring_radius} 0.0005" rgba="0.75 0.2 0.15 0.8"
          contype="0" conaffinity="0"/>
    {bot("bot0", "0.2 0.4 0.9 1")}
    {bot("bot1", "0.9 0.5 0.15 1")}
  </worldbody>
  <actuator>
{actuators}
  </actuator>
</mujoco>"""


def yaw_to_quat(yaw: float) -> tuple[float, float, float, float]:
    return (math.cos(yaw / 2), 0.0, 0.0, math.sin(yaw / 2))


def quat_to_yaw(q) -> float:
    w, x, y, z = q
    return math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))


def quat_up_z(q) -> float:
    """z component of the body's z axis in world frame (1 = upright)."""
    w, x, y, z = q
    return 1 - 2 * (x * x + y * y)


# --------------------------------------------------------------------------


Addr = tuple[str, int] | str   # UDP host/port, or a unix datagram socket path (modem)


def addr_json(addr: Addr | None):
    if addr is None:
        return None
    return list(addr) if isinstance(addr, tuple) else str(addr)


@dataclass
class RobotIO:
    index: int
    node_id: str
    sock: socket.socket
    dest: Addr | None
    dest_fixed: bool
    unix_path: str | None = None
    last_cmd: protocol.Command | None = None
    last_cmd_recv_us: int = 0
    last_cmd_seq: int = 0
    last_cmd_t_send_us: int = 0
    last_cmd_apply_sim_s: float = 0.0
    state_seq: int = 0
    cmds_received: int = 0
    cmds_stale_rejected: int = 0
    seq_gaps: int = 0
    stale: bool = True
    ever_fresh: bool = False
    stale_since_sim_s: float = 0.0
    stale_intervals: int = 0
    stale_total_s: float = 0.0
    one_way_us: list[int] = field(default_factory=list)
    brain_turnaround_us: list[int] = field(default_factory=list)


def parse_binds(text: str, node_ids: list[str], unix_text: str | None = None) -> list[Addr]:
    """Per robot either a UDP host:port (--robot-bind) or a unix socket path
    (--robot-unix); a node named in --robot-unix ignores its --robot-bind entry."""
    binds: dict[str, Addr] = {}
    for item in text.split(","):
        if not item.strip():
            continue
        node, _, addr = item.partition("=")
        host, _, port = addr.rpartition(":")
        if not node or not host or not port:
            raise SystemExit(f"bad --robot-bind item: {item!r} (want node=host:port)")
        binds[node.strip()] = (host.strip(), int(port))
    for item in (unix_text or "").split(","):
        if not item.strip():
            continue
        node, _, path = item.partition("=")
        if not node or not path:
            raise SystemExit(f"bad --robot-unix item: {item!r} (want node=/path/to.sock)")
        binds[node.strip()] = path.strip()
    missing = [n for n in node_ids if n not in binds]
    if missing:
        raise SystemExit(f"--robot-bind/--robot-unix lacks {missing}")
    return [binds[n] for n in node_ids]


def parse_dests(text: str | None, node_ids: list[str]) -> list[tuple[str, int] | None]:
    if not text:
        return [None] * len(node_ids)
    dests: dict[str, tuple[str, int]] = {}
    for item in text.split(","):
        node, _, addr = item.partition("=")
        host, _, port = addr.rpartition(":")
        dests[node.strip()] = (host.strip(), int(port))
    return [dests.get(n) for n in node_ids]


class Arena:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.node_ids = [s.strip() for s in args.node_ids.split(",")]
        if len(self.node_ids) != 2:
            raise SystemExit("--node-ids must name exactly two robots")
        self.ring_radius = float(args.ring_radius)
        self.wheel_max = float(args.wheel_max_rad_s)
        self.bot_type = args.bot
        self.torque_max = float(args.torque_max)
        self.ctrl_max = self.torque_max if self.bot_type == "balance" else self.wheel_max
        self.fall_up = BAL_FALL_UP if self.bot_type == "balance" else SUMO_FALL_UP
        # Balance bots are held upright by the "starter" until both brains have
        # sent a fresh command (or --hold-max-s), so neither falls before its
        # brain is connected; the release time goes into the result.
        self.holding = self.bot_type == "balance"
        self.hold_release_sim_s: float | None = None
        self.model = mujoco.MjModel.from_xml_string(build_model_xml(self.ring_radius, self.wheel_max, self.bot_type, self.torque_max))
        self.data = mujoco.MjData(self.model)
        self.rng = random.Random(args.seed)
        self.log_path = pathlib.Path(args.log)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = self.log_path.open("w", encoding="utf-8")
        binds = parse_binds(args.robot_bind, self.node_ids, args.robot_unix)
        dests = parse_dests(args.state_dest, self.node_ids)
        self.robots: list[RobotIO] = []
        for index, node_id in enumerate(self.node_ids):
            bind = binds[index]
            unix_path = None
            if isinstance(bind, str):
                # Served by a modem (modem.py) over a filesystem unix datagram
                # socket, which crosses network namespaces.
                unix_path = bind
                pathlib.Path(unix_path).parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.unlink(unix_path)
                except FileNotFoundError:
                    pass
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                sock.bind(unix_path)
            else:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(bind)
            sock.setblocking(False)
            self.robots.append(RobotIO(index, node_id, sock, dests[index], dests[index] is not None, unix_path))
        self.body_ids = [mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, f"bot{i}") for i in range(2)]
        self.qpos_addr = [self.model.jnt_qposadr[mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, f"bot{i}_free")] for i in range(2)]
        self.qvel_addr = [self.model.jnt_dofadr[mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, f"bot{i}_free")] for i in range(2)]
        self.wheel_dof = [tuple(self.model.jnt_dofadr[mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, f"bot{i}_{w}")] for w in ("wl", "wr")) for i in range(2)]
        self.pub = None
        if args.positions_endpoint:
            import zmq
            ctx = zmq.Context.instance()
            self.pub = ctx.socket(zmq.PUB)
            self.pub.setsockopt(zmq.SNDHWM, 10)
            self.pub.bind(args.positions_endpoint)
        self.result: dict | None = None
        self.late_loops = 0
        self.loops = 0
        self.max_lag_s = 0.0

    # -- setup ------------------------------------------------------------
    def spawn(self) -> None:
        mujoco.mj_resetData(self.model, self.data)
        spread = self.ring_radius * self.args.spawn_fraction
        for i in range(2):
            sign = -1.0 if i == 0 else 1.0
            x = sign * spread + self.rng.uniform(-0.05, 0.05)
            y = self.rng.uniform(-0.05, 0.05)
            yaw = (0.0 if i == 0 else math.pi) + self.rng.uniform(-0.05, 0.05)
            a = self.qpos_addr[i]
            self.data.qpos[a:a + 3] = (x, y, WHEEL_RADIUS + 0.002)
            self.data.qpos[a + 3:a + 7] = yaw_to_quat(yaw)
        mujoco.mj_forward(self.model, self.data)
        self.emit({"event": "spawn", "seed": self.args.seed, "ring_radius_m": self.ring_radius, "bot": self.bot_type,
                   "robots": [self.pose_dict(i) for i in range(2)], "node_ids": self.node_ids,
                   "stale_policy": self.args.stale_policy, "time_limit_s": self.args.time_limit,
                   "timeout_margin_m": self.args.timeout_margin_m,
                   "state_hz": self.args.state_hz, "pub_hz": self.args.pub_hz, "timestep_s": TIMESTEP,
                   "transports": [r.unix_path or "udp" for r in self.robots]})

    # -- helpers ----------------------------------------------------------
    def emit(self, record: dict) -> None:
        record.setdefault("t_unix_us", protocol.now_us())
        record.setdefault("sim_time_s", round(float(self.data.time), 4))
        self.log.write(json.dumps(record, separators=(",", ":")) + "\n")

    def pose(self, i: int):
        a = self.qpos_addr[i]
        v = self.qvel_addr[i]
        x, y, z = (float(q) for q in self.data.qpos[a:a + 3])
        quat = self.data.qpos[a + 3:a + 7]
        vx, vy, vz = (float(q) for q in self.data.qvel[v:v + 3])
        wz = float(self.data.qvel[v + 5])
        return x, y, z, quat_to_yaw(quat), vx, vy, vz, wz, quat_up_z(quat)

    def lean(self, i: int) -> tuple[float, float, float, float]:
        """(pitch, pitch_rate, wheel_left, wheel_right): lean of the body about the
        axle, + = leaning forward (toward its heading); the free joint's angular
        velocity is in the body frame, so its y component is the pitch rate."""
        a = self.qpos_addr[i]
        v = self.qvel_addr[i]
        w, qx, qy, qz = self.data.qpos[a + 3:a + 7]
        # body z axis in world: third column of R(q); its component along the heading
        up_x = 2 * (qx * qz + w * qy)
        up_y = 2 * (qy * qz - w * qx)
        yaw = quat_to_yaw((w, qx, qy, qz))
        forward = up_x * math.cos(yaw) + up_y * math.sin(yaw)
        pitch = math.asin(max(-1.0, min(1.0, forward)))
        pitch_rate = float(self.data.qvel[v + 4])
        wl = float(self.data.qvel[self.wheel_dof[i][0]])
        wr = float(self.data.qvel[self.wheel_dof[i][1]])
        return pitch, pitch_rate, wl, wr

    def pose_dict(self, i: int) -> dict:
        x, y, z, yaw, vx, vy, vz, wz, up = self.pose(i)
        pitch, pitch_rate, _, _ = self.lean(i)
        return {"robot": i, "x": round(x, 4), "y": round(y, 4), "z": round(z, 4), "yaw": round(yaw, 4),
                "vx": round(vx, 4), "vy": round(vy, 4), "wz": round(wz, 4), "up": round(up, 4),
                "pitch": round(pitch, 4), "pitch_rate": round(pitch_rate, 3),
                "dist_to_edge_m": round(self.ring_radius - math.hypot(x, y), 4)}

    def hold_upright(self) -> None:
        """Starter's hand: keep both balance bots vertical (yaw kept, roll/pitch and
        their rates zeroed) until both brains are connected."""
        for i in range(2):
            a = self.qpos_addr[i]
            v = self.qvel_addr[i]
            yaw = quat_to_yaw(self.data.qpos[a + 3:a + 7])
            self.data.qpos[a + 2] = WHEEL_RADIUS
            self.data.qpos[a + 3:a + 7] = yaw_to_quat(yaw)
            self.data.qvel[v:v + 6] = 0.0
            self.data.qvel[self.wheel_dof[i][0]] = 0.0
            self.data.qvel[self.wheel_dof[i][1]] = 0.0

    # -- network ----------------------------------------------------------
    def drain_commands(self) -> None:
        now = protocol.now_us()
        for robot in self.robots:
            while True:
                try:
                    data, addr = robot.sock.recvfrom(2048)
                except BlockingIOError:
                    break
                except OSError as exc:
                    if exc.errno in (errno.ECONNREFUSED, errno.EAGAIN):
                        break
                    raise
                try:
                    header, body = protocol.unpack(data)
                except protocol.ProtocolError as exc:
                    self.emit({"event": "bad_datagram", "robot": robot.index, "error": str(exc)})
                    continue
                if header.kind != protocol.KIND_CMD or not isinstance(body, protocol.Command):
                    continue
                if header.robot_id != robot.index:
                    self.emit({"event": "bad_datagram", "robot": robot.index, "error": f"robot_id {header.robot_id}"})
                    continue
                if not robot.dest_fixed and robot.dest != addr:
                    robot.dest = addr
                    self.emit({"event": "brain_learned", "robot": robot.index, "addr": addr_json(addr)})
                robot.cmds_received += 1
                if robot.last_cmd_seq and header.seq > robot.last_cmd_seq + 1:
                    robot.seq_gaps += header.seq - robot.last_cmd_seq - 1
                if header.seq <= robot.last_cmd_seq and robot.last_cmd_seq - header.seq < 1000:
                    robot.cmds_stale_rejected += 1
                    continue  # reordered / duplicate: keep the newer command
                one_way = now - header.t_send_us
                robot.one_way_us.append(one_way)
                turnaround = None
                if header.t_echo_us:
                    turnaround = header.t_send_us - header.t_echo_us  # state sent -> cmd sent, brain side
                    robot.brain_turnaround_us.append(turnaround)
                robot.last_cmd = body
                robot.last_cmd_recv_us = now
                robot.last_cmd_seq = header.seq
                robot.last_cmd_t_send_us = header.t_send_us
                self.emit({"event": "cmd", "robot": robot.index, "seq": header.seq, "t_send_us": header.t_send_us,
                           "one_way_us": one_way, "state_echo_seq": header.echo_seq,
                           "brain_turnaround_us": turnaround, "left": round(body.wheel_left, 3),
                           "right": round(body.wheel_right, 3), "ttl_ms": body.ttl_ms})

    def send_states(self, flags_per_robot: list[int]) -> None:
        now = protocol.now_us()
        poses = [self.pose(i) for i in range(2)]
        for robot in self.robots:
            if robot.dest is None:
                continue
            me, opp = poses[robot.index], poses[1 - robot.index]
            pitch, pitch_rate, wl, wr = self.lean(robot.index)
            robot.state_seq += 1
            header = protocol.Header(protocol.KIND_STATE, robot.index, robot.state_seq, now,
                                     robot.last_cmd_seq, robot.last_cmd_t_send_us)
            state = protocol.State(
                float(self.data.time), me[0], me[1], me[3], me[4], me[5], me[7],
                opp[0], opp[1], opp[3], opp[4], opp[5], self.ring_radius,
                self.ring_radius - math.hypot(me[0], me[1]), self.ring_radius - math.hypot(opp[0], opp[1]),
                pitch, pitch_rate, wl, wr, flags_per_robot[robot.index])
            try:
                robot.sock.sendto(protocol.pack_state(header, state), robot.dest)
            except OSError:
                pass

    def publish_positions(self) -> None:
        if self.pub is None:
            return
        nodes = {}
        for i, node_id in enumerate(self.node_ids):
            x, y, z, _, vx, vy, vz, _, _ = self.pose(i)
            nodes[node_id] = ((x, y, z + self.args.antenna_height), (vx, vy, vz))
        self.pub.send(protocol.positions_message(protocol.now_us() // 1000, nodes))

    # -- control ----------------------------------------------------------
    def apply_controls(self) -> None:
        now = protocol.now_us()
        for robot in self.robots:
            cmd = robot.last_cmd
            fresh = cmd is not None and (now - robot.last_cmd_recv_us) <= cmd.ttl_ms * 1000
            if fresh:
                if robot.stale:
                    if robot.ever_fresh:
                        duration = float(self.data.time) - robot.stale_since_sim_s
                        robot.stale_intervals += 1
                        robot.stale_total_s += duration
                        self.emit({"event": "stale_end", "robot": robot.index, "duration_s": round(duration, 4)})
                    else:
                        self.emit({"event": "first_command", "robot": robot.index})
                    robot.stale = False
                    robot.ever_fresh = True
                left, right = cmd.wheel_left, cmd.wheel_right
            else:
                if not robot.stale:
                    robot.stale = True
                    robot.stale_since_sim_s = float(self.data.time)
                    self.emit({"event": "stale_begin", "robot": robot.index, "last_seq": robot.last_cmd_seq})
                if self.args.stale_policy == "hold" and cmd is not None:
                    left, right = cmd.wheel_left, cmd.wheel_right
                elif self.args.stale_policy == "coast" and self.bot_type != "balance":
                    # Motor driver off: servo each wheel to its current speed so it
                    # free-wheels (rolling resistance only) instead of braking.
                    left = float(self.data.qvel[self.wheel_dof[robot.index][0]])
                    right = float(self.data.qvel[self.wheel_dof[robot.index][1]])
                else:
                    # zero; for a torque bot coast == zero: a starved link delivers no torque
                    left, right = 0.0, 0.0
            base = robot.index * 2
            self.data.ctrl[base] = max(-self.ctrl_max, min(self.ctrl_max, left))
            self.data.ctrl[base + 1] = max(-self.ctrl_max, min(self.ctrl_max, right))

    def referee(self) -> tuple[int | None, str] | None:
        """None while the fight runs; otherwise (winner index or None, reason).

        Reasons: ring_out, fall, both_out, both_fell, timeout_edge (time limit,
        the robot closer to the edge loses), timeout (time limit and the two
        radial distances differ by less than --timeout-margin-m: a real draw)."""
        out, fallen, radial = [], [], []
        for i in range(2):
            x, y, z, _, _, _, _, _, up = self.pose(i)
            r = math.hypot(x, y)
            radial.append(r)
            if r > self.ring_radius:
                out.append(i)
            if (up < self.fall_up or z < -0.5) and not self.holding:
                fallen.append(i)
        if fallen:
            if len(fallen) == 2:
                return None, "both_fell"
            return 1 - fallen[0], "fall"
        if out:
            if len(out) == 2:
                return None, "both_out"
            return 1 - out[0], "ring_out"
        if self.data.time >= self.args.time_limit:
            if abs(radial[0] - radial[1]) < self.args.timeout_margin_m:
                return None, "timeout"
            loser = 0 if radial[0] > radial[1] else 1
            return 1 - loser, "timeout_edge"
        return None

    # -- main loop --------------------------------------------------------
    def run(self) -> dict:
        self.spawn()
        state_period = 1.0 / self.args.state_hz
        pub_period = 1.0 / self.args.pub_hz
        pose_period = 1.0 / self.args.pose_log_hz
        next_state = next_pub = next_pose = 0.0
        wall0 = time.perf_counter()
        sim0 = float(self.data.time)
        verdict = None
        loop_period = self.args.loop_period_s
        while verdict is None:
            self.loops += 1
            self.drain_commands()
            if self.holding:
                self.hold_upright()
                if all(r.ever_fresh for r in self.robots) or float(self.data.time) >= self.args.hold_max_s:
                    self.holding = False
                    self.hold_release_sim_s = float(self.data.time)
                    self.emit({"event": "hold_release", "both_connected": all(r.ever_fresh for r in self.robots)})
            if self.args.lockstep:
                # DEBUG ONLY: advance one loop period per received command pair.
                # Not real time; forbidden for validation fights.
                self.apply_controls()
                for _ in range(int(round(loop_period / TIMESTEP))):
                    mujoco.mj_step(self.model, self.data)
            else:
                target = sim0 + (time.perf_counter() - wall0)
                lag = target - float(self.data.time)
                if lag > 2 * loop_period:
                    self.late_loops += 1
                    self.max_lag_s = max(self.max_lag_s, lag)
                steps = 0
                while float(self.data.time) < target and steps < self.args.max_catchup_steps:
                    self.apply_controls()
                    if self.holding:
                        self.hold_upright()
                    mujoco.mj_step(self.model, self.data)
                    steps += 1
            sim_t = float(self.data.time)
            if sim_t >= next_state:
                self.send_states([protocol.FLAG_RUNNING, protocol.FLAG_RUNNING])
                next_state = sim_t + state_period
            if sim_t >= next_pub:
                self.publish_positions()
                next_pub = sim_t + pub_period
            if sim_t >= next_pose:
                self.emit({"event": "pose", "robots": [self.pose_dict(i) for i in range(2)]})
                next_pose = sim_t + pose_period
            verdict = self.referee()
            if verdict is None and not self.args.lockstep:
                sleep_for = (float(self.data.time) + loop_period) - (sim0 + (time.perf_counter() - wall0))
                if sleep_for > 0:
                    time.sleep(min(sleep_for, loop_period))
        wall = time.perf_counter() - wall0
        winner, reason = verdict
        radial = [math.hypot(*self.pose(i)[:2]) for i in range(2)]
        self.emit({"event": "result", "winner": winner, "reason": reason, "radial_m": [round(r, 4) for r in radial],
                   "robots": [self.pose_dict(i) for i in range(2)]})
        flags = []
        for i in range(2):
            f = protocol.FLAG_OVER
            if winner is not None:
                f |= protocol.FLAG_WON if winner == i else protocol.FLAG_LOST
            flags.append(f)
        # Tell the brains the fight is over (a few repeats; UDP).
        for _ in range(int(self.args.over_repeats)):
            self.send_states(flags)
            time.sleep(0.05)
        rtf = (float(self.data.time) - sim0) / wall if wall > 0 else 0.0
        result = {
            "winner": winner, "winner_node": None if winner is None else self.node_ids[winner],
            "reason": reason, "decided_by": reason, "radial_m": [round(r, 4) for r in radial],
            "timeout_margin_m": self.args.timeout_margin_m,
            "seed": self.args.seed, "sim_time_s": round(float(self.data.time) - sim0, 4),
            "wall_time_s": round(wall, 4), "rtf": round(rtf, 4), "late_loops": self.late_loops,
            "loops": self.loops, "max_lag_ms": round(self.max_lag_s * 1000, 3), "lockstep": bool(self.args.lockstep),
            "stale_policy": self.args.stale_policy, "ring_radius_m": self.ring_radius, "wheel_max_rad_s": self.wheel_max,
            "bot": self.bot_type, "torque_max_nm": self.torque_max if self.bot_type == "balance" else None,
            "hold_release_sim_s": None if self.hold_release_sim_s is None else round(self.hold_release_sim_s, 4),
            "robots": [self.robot_summary(r) for r in self.robots],
        }
        self.emit({"event": "rtf", **{k: result[k] for k in ("rtf", "late_loops", "loops", "max_lag_ms", "wall_time_s")}})
        self.result = result
        return result

    def robot_summary(self, robot: RobotIO) -> dict:
        if robot.stale and robot.ever_fresh:
            robot.stale_total_s += float(self.data.time) - robot.stale_since_sim_s
            robot.stale_intervals += 1
        ow = np.array(robot.one_way_us, dtype=np.int64) if robot.one_way_us else np.array([0])
        ta = np.array(robot.brain_turnaround_us, dtype=np.int64) if robot.brain_turnaround_us else np.array([0])
        return {
            "robot": robot.index, "node_id": robot.node_id, "cmds_received": robot.cmds_received,
            "cmd_seq_gaps": robot.seq_gaps, "cmds_reordered_dropped": robot.cmds_stale_rejected,
            "stale_intervals": robot.stale_intervals, "stale_total_s": round(robot.stale_total_s, 4),
            "one_way_us": {"p50": int(np.percentile(ow, 50)), "p99": int(np.percentile(ow, 99)), "max": int(ow.max()), "n": len(robot.one_way_us)},
            "brain_turnaround_us": {"p50": int(np.percentile(ta, 50)), "p99": int(np.percentile(ta, 99)), "n": len(robot.brain_turnaround_us)},
            "brain_addr": addr_json(robot.dest), "transport": robot.unix_path or "udp",
        }

    def close(self) -> None:
        self.log.close()
        for robot in self.robots:
            robot.sock.close()
            if robot.unix_path:
                try:
                    os.unlink(robot.unix_path)
                except FileNotFoundError:
                    pass
        if self.pub is not None:
            self.pub.close(linger=0)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--node-ids", default="ue0,ue1", help="robot/node ids in order (also the position PUB keys)")
    p.add_argument("--robot-bind", default="ue0=127.0.0.1:6000,ue1=127.0.0.1:6001",
                   help="UDP bind per robot: node=host:port,...")
    p.add_argument("--robot-unix", default=None,
                   help="serve these robots over a unix datagram socket instead (node=/path.sock,...); "
                        "a modem (modem.py) inside the UE's network namespace relays to it")
    p.add_argument("--state-dest", default=None, help="fixed brain address per robot: node=host:port,... (default: learn from first CMD)")
    p.add_argument("--timeout-margin-m", type=float, default=0.05,
                   help="at the time limit the robot closer to the edge loses unless the radial distances "
                        "differ by less than this (then it is a draw)")
    p.add_argument("--positions-endpoint", default=protocol.DEFAULT_POSITIONS_ENDPOINT,
                   help="ZMQ PUB endpoint for the Sionna bridge ('' to disable)")
    p.add_argument("--antenna-height", type=float, default=0.3, help="published z above the floor, metres")
    p.add_argument("--ring-radius", type=float, default=2.0)
    p.add_argument("--bot", choices=("sumo", "balance"), default="sumo",
                   help="sumo: low wheeled pusher, CMD = wheel speeds; balance: two-wheeled inverted pendulum "
                        "balanced by the brain over the link, CMD = wheel torques")
    p.add_argument("--torque-max", type=float, default=BAL_TORQUE_MAX, help="balance bot wheel torque limit, N m")
    p.add_argument("--hold-max-s", type=float, default=5.0,
                   help="balance bots are held upright until both brains sent a command, at most this long")
    p.add_argument("--wheel-max-rad-s", type=float, default=WHEEL_MAX_RAD_S, help="actuator limit; 30 rad/s = 1.8 m/s")
    p.add_argument("--spawn-fraction", type=float, default=0.5, help="spawn at +-fraction*radius on the x axis")
    p.add_argument("--time-limit", type=float, default=60.0, help="seconds of sim time before a draw")
    p.add_argument("--state-hz", type=float, default=200.0)
    p.add_argument("--pub-hz", type=float, default=20.0)
    p.add_argument("--pose-log-hz", type=float, default=20.0)
    p.add_argument("--stale-policy", choices=("coast", "zero", "hold"), default="coast",
                   help="what a robot does when no command is fresh within its ttl: coast = motor driver off "
                        "(free-wheel, pushable), zero = brake to a stop, hold = keep the last command")
    p.add_argument("--loop-period-s", type=float, default=0.001)
    p.add_argument("--max-catchup-steps", type=int, default=50, help="physics steps per loop when behind the wall clock")
    p.add_argument("--lockstep", action="store_true",
                   help="DEBUG ONLY: advance the world without waiting for the wall clock. Never for validation fights.")
    p.add_argument("--over-repeats", type=int, default=20)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--log", default="arena.jsonl")
    p.add_argument("--result", default="arena-result.json")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    arena = Arena(args)
    try:
        result = arena.run()
    finally:
        arena.close()
    pathlib.Path(args.result).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("winner", "reason", "sim_time_s", "rtf", "late_loops")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
