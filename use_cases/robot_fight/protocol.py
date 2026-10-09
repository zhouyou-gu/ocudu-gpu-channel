"""Wire formats shared by the robot-fight arena, brains and the Sionna bridge.

Two planes:

1. **Control plane (UDP, brain <-> robot).** Fixed little-endian structs. Every
   datagram starts with the same 32-byte header so both ends can compute RTT,
   loss, staleness and deadline misses without any side channel::

       magic        2s   b"RF"
       version      u8   PROTOCOL_VERSION
       kind         u8   KIND_STATE (robot -> brain) or KIND_CMD (brain -> robot)
       robot_id     u8   0 or 1
       pad          3x
       seq          u32  sender's own counter, +1 per datagram, per direction
       t_send_us    u64  sender's wall clock (unix microseconds) at send
       echo_seq     u32  seq of the newest datagram received from the peer
       pad          4x
       t_echo_us    u64  t_send_us copied from that newest peer datagram

   RTT on either end is ``now_us - t_echo_us`` of an incoming datagram when the
   peer has answered promptly, and the two ends need no clock agreement for it.
   One-way latency needs synchronised clocks (loopback: same host, exact).

   STATE payload (robot -> brain), after the header::

       sim_time_s        f64   arena simulation time
       x, y, yaw         f32   own pose, arena frame (ring centre = origin, z up, metres, radians)
       vx, vy, wz        f32   own planar velocity and yaw rate
       opp_x, opp_y, opp_yaw  f32   opponent pose as seen by the referee (global observability)
       opp_vx, opp_vy    f32
       ring_radius_m     f32
       dist_to_edge_m    f32   ring_radius - |own xy|
       opp_dist_to_edge_m f32
       pitch, pitch_rate f32   own body lean about the axle (rad, rad/s; + = leaning forward).
                               ~0 for a sumo bot; the balance bot's brain closes its balance
                               loop on these two numbers (version 2)
       wheel_left, wheel_right f32   own wheel angular velocities, rad/s (version 2)
       flags             u32   FLAG_* bits (fight running / over / this robot won or lost)

   CMD payload (brain -> robot)::

       wheel_left, wheel_right  f32   sumo bot: wheel angular-velocity targets, rad/s;
                                      balance bot: wheel torques, N m (the arena's --bot decides)
       ttl_ms                   u16   how long the robot may keep applying this command
       pad                      2x

2. **Position plane (ZMQ PUB, arena -> Sionna bridge).** JSON text frames, at
   10-20 Hz, default endpoint ``tcp://127.0.0.1:5570``::

       {"event": "positions", "t_unix_ms": 1790685008187, "frame": "arena",
        "nodes": {"ue0": {"position_m": [x, y, z], "velocity_mps": [vx, vy, vz]},
                  "ue1": {"position_m": [x, y, z], "velocity_mps": [vx, vy, vz]}}}

   ``frame`` is always ``"arena"``: ring centre at the origin, z up, metres. The
   bridge maps arena coordinates into the scene. ``z`` is the antenna height
   above the floor. Node ids are whatever the arena was started with
   (``--node-ids ue0,ue1``), in robot order.
"""

from __future__ import annotations

import json
import struct
import time
from dataclasses import dataclass

MAGIC = b"RF"
PROTOCOL_VERSION = 2   # 2: STATE carries pitch, pitch_rate and the wheel speeds (balance bot)
KIND_STATE = 1
KIND_CMD = 2

FLAG_RUNNING = 1 << 0
FLAG_OVER = 1 << 1
FLAG_WON = 1 << 2
FLAG_LOST = 1 << 3

_HEADER = struct.Struct("<2sBBB3xIQI4xQ")
_STATE = struct.Struct("<d3f3f3f2f3f4fI")
_CMD = struct.Struct("<2fH2x")

HEADER_SIZE = _HEADER.size
STATE_SIZE = HEADER_SIZE + _STATE.size
CMD_SIZE = HEADER_SIZE + _CMD.size

DEFAULT_POSITIONS_ENDPOINT = "tcp://127.0.0.1:5570"


def now_us() -> int:
    return time.time_ns() // 1000


@dataclass(frozen=True)
class Header:
    kind: int
    robot_id: int
    seq: int
    t_send_us: int
    echo_seq: int
    t_echo_us: int


@dataclass(frozen=True)
class State:
    sim_time_s: float
    x: float
    y: float
    yaw: float
    vx: float
    vy: float
    wz: float
    opp_x: float
    opp_y: float
    opp_yaw: float
    opp_vx: float
    opp_vy: float
    ring_radius_m: float
    dist_to_edge_m: float
    opp_dist_to_edge_m: float
    pitch: float
    pitch_rate: float
    wheel_left: float
    wheel_right: float
    flags: int


@dataclass(frozen=True)
class Command:
    wheel_left: float
    wheel_right: float
    ttl_ms: int


class ProtocolError(ValueError):
    pass


def _pack_header(header: Header) -> bytes:
    return _HEADER.pack(
        MAGIC, PROTOCOL_VERSION, header.kind, header.robot_id,
        header.seq & 0xFFFFFFFF, header.t_send_us, header.echo_seq & 0xFFFFFFFF, header.t_echo_us,
    )


def _unpack_header(data: bytes) -> Header:
    if len(data) < HEADER_SIZE:
        raise ProtocolError(f"datagram too short: {len(data)} < {HEADER_SIZE}")
    magic, version, kind, robot_id, seq, t_send, echo_seq, t_echo = _HEADER.unpack_from(data)
    if magic != MAGIC:
        raise ProtocolError("bad magic")
    if version != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported version {version}")
    return Header(kind, robot_id, seq, t_send, echo_seq, t_echo)


def pack_state(header: Header, state: State) -> bytes:
    if header.kind != KIND_STATE:
        raise ProtocolError("header kind is not STATE")
    return _pack_header(header) + _STATE.pack(
        state.sim_time_s, state.x, state.y, state.yaw, state.vx, state.vy, state.wz,
        state.opp_x, state.opp_y, state.opp_yaw, state.opp_vx, state.opp_vy,
        state.ring_radius_m, state.dist_to_edge_m, state.opp_dist_to_edge_m,
        state.pitch, state.pitch_rate, state.wheel_left, state.wheel_right, state.flags,
    )


def pack_command(header: Header, command: Command) -> bytes:
    if header.kind != KIND_CMD:
        raise ProtocolError("header kind is not CMD")
    return _pack_header(header) + _CMD.pack(command.wheel_left, command.wheel_right, command.ttl_ms)


def unpack(data: bytes) -> tuple[Header, State | Command]:
    """Decode either datagram kind. Raises ProtocolError on malformed input."""
    header = _unpack_header(data)
    body = data[HEADER_SIZE:]
    if header.kind == KIND_STATE:
        if len(body) != _STATE.size:
            raise ProtocolError(f"STATE payload size {len(body)} != {_STATE.size}")
        return header, State(*_STATE.unpack(body))
    if header.kind == KIND_CMD:
        if len(body) != _CMD.size:
            raise ProtocolError(f"CMD payload size {len(body)} != {_CMD.size}")
        left, right, ttl = _CMD.unpack(body)
        return header, Command(left, right, ttl)
    raise ProtocolError(f"unknown kind {header.kind}")


def rtt_us(header: Header, received_at_us: int) -> int | None:
    """Round-trip estimate carried by an incoming datagram, or None when the
    peer has not echoed anything yet."""
    if header.t_echo_us == 0:
        return None
    return received_at_us - header.t_echo_us


def positions_message(t_unix_ms: int, nodes: dict[str, tuple[tuple[float, float, float], tuple[float, float, float]]]) -> bytes:
    """Encode the position-plane JSON frame. ``nodes`` maps node id to
    (position_m, velocity_mps)."""
    payload = {
        "event": "positions",
        "t_unix_ms": int(t_unix_ms),
        "frame": "arena",
        "nodes": {
            node_id: {
                "position_m": [float(v) for v in position],
                "velocity_mps": [float(v) for v in velocity],
            }
            for node_id, (position, velocity) in nodes.items()
        },
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def parse_positions_message(data: bytes) -> dict:
    """Decode and validate a position-plane frame (the bridge's SUB side uses
    this). Returns the parsed dict."""
    payload = json.loads(data.decode("utf-8"))
    if payload.get("event") != "positions":
        raise ProtocolError("not a positions event")
    if payload.get("frame") != "arena":
        raise ProtocolError(f"unexpected frame {payload.get('frame')!r}")
    nodes = payload.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        raise ProtocolError("positions without nodes")
    for node_id, node in nodes.items():
        for key in ("position_m", "velocity_mps"):
            vec = node.get(key)
            if not (isinstance(vec, list) and len(vec) == 3 and all(isinstance(v, (int, float)) for v in vec)):
                raise ProtocolError(f"node {node_id}: bad {key}")
    return payload
