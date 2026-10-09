#!/usr/bin/env python3
"""One brain per robot: reads STATE, decides, sends CMD over UDP.

The brain runs on its own clock at --rate-hz and never blocks on the arena: a
tick with no fresh state reuses the last one (and counts it). It logs the RTT
carried by STATE echoes, gaps in the STATE sequence (loss), and its own
deadline misses. It exits when a STATE arrives with FLAG_OVER or after
--max-seconds.

Policies are plain functions ``policy(state, params, rng) -> (left, right)``
returning wheel angular-velocity targets in rad/s. The built-in one is
``pusher``. ``--policy module:callable`` plugs another one in (an LLM policy
would go there); it is imported from the venv's sys.path.
"""

from __future__ import annotations

import argparse
import collections
import importlib
import json
import math
import pathlib
import random
import socket
import sys
import threading
import time

import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import protocol  # type: ignore
else:
    from . import protocol

WHEEL_RADIUS = 0.06
WHEEL_BASE = 0.28


def wrap_angle(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


def diff_drive(v: float, w: float) -> tuple[float, float]:
    """Body speed v (m/s) and yaw rate w (rad/s) -> wheel rad/s (left, right)."""
    left = (v - w * WHEEL_BASE / 2) / WHEEL_RADIUS
    right = (v + w * WHEEL_BASE / 2) / WHEEL_RADIUS
    return left, right


DEFAULT_PARAMS = {
    "v_max": 1.6,          # m/s (wheel limit 1.8)
    "k_heading": 5.0,      # rad/s per rad of heading error
    "w_max": 7.0,          # rad/s
    "flank_m": 0.7,        # waypoint this far beside the opponent (perpendicular to its heading)
    "charge_deg": 55.0,    # once the opponent is seen this far off its own heading, charge into it
    "edge_margin_m": 0.45, # slow down when this close to the edge and heading outward
    "noise_rad": 0.02,     # per-tick heading noise (std) when seeded
    "stall_s": 1.5,        # in contact this long -> back off and switch flank
    "backoff_s": 0.6,
}


def pusher(state: protocol.State, params: dict, rng: random.Random) -> tuple[float, float]:
    """Flank, then push. Head-on pushes between equal bots stall, so the bot
    goes to a waypoint beside the opponent (perpendicular to the opponent's
    heading) and charges into its side, which its wheels cannot resist. A
    stalled push backs off and switches flank. Near the edge the bot refuses to
    drive outward at full speed, so it does not push itself out."""
    mem = params.setdefault("_mem", {"stall_since": None, "backoff_until": None, "backoff_turn": 1.0, "side": None})
    ox, oy = state.opp_x, state.opp_y
    rx, ry = ox - state.x, oy - state.y           # me -> opponent
    dist_opp = math.hypot(rx, ry)
    speed = math.hypot(state.vx, state.vy)
    t = state.sim_time_s
    hx, hy = math.cos(state.opp_yaw), math.sin(state.opp_yaw)   # opponent heading
    nx, ny = -hy, hx                                            # opponent's left side
    if mem["side"] is None:
        mem["side"] = 1.0 if (nx * -rx + ny * -ry) >= 0 else -1.0   # the side I am already on
    # Stalemate escape: in contact but not moving -> reverse while turning, then switch flank.
    if mem["backoff_until"] is not None:
        if t < mem["backoff_until"]:
            return diff_drive(-0.6 * params["v_max"], mem["backoff_turn"] * 0.5 * params["w_max"])
        mem["backoff_until"] = None
        mem["stall_since"] = None
        mem["side"] = -mem["side"]
    if dist_opp < 0.5:   # in contact: give a push this long, then disengage
        if mem["stall_since"] is None:
            mem["stall_since"] = t
        elif t - mem["stall_since"] > params["stall_s"]:
            mem["backoff_until"] = t + params["backoff_s"]
            mem["backoff_turn"] = rng.choice((-1.0, 1.0)) if params["noise_rad"] > 0 else 1.0
            mem["stall_since"] = None
    else:
        mem["stall_since"] = None
    # Where am I relative to the opponent's heading? 0 = dead ahead of it, 180 = behind it.
    if dist_opp > 1e-3:
        cos_off = (hx * -rx + hy * -ry) / dist_opp
        off_deg = math.degrees(math.acos(max(-1.0, min(1.0, cos_off))))
    else:
        off_deg = 0.0
    if off_deg >= params["charge_deg"]:
        tx, ty = ox, oy                                   # charge into the opponent's side
    else:
        tx = ox + mem["side"] * nx * params["flank_m"]    # go around to its flank
        ty = oy + mem["side"] * ny * params["flank_m"]
    dx, dy = tx - state.x, ty - state.y
    heading_err = wrap_angle(math.atan2(dy, dx) - state.yaw)
    if params["noise_rad"] > 0:
        heading_err += rng.gauss(0.0, params["noise_rad"])
    w = max(-params["w_max"], min(params["w_max"], params["k_heading"] * heading_err))
    v = params["v_max"] * max(0.0, math.cos(heading_err))
    # Edge guard: heading outward while near the edge -> creep only.
    my_rad = math.hypot(state.x, state.y)
    if my_rad > 1e-3 and state.dist_to_edge_m < params["edge_margin_m"]:
        outward = (math.cos(state.yaw) * state.x + math.sin(state.yaw) * state.y) / my_rad
        if outward > 0.3 and dist_opp > 0.5:
            v *= 0.25
    return diff_drive(v, w)


REACTIVE_PARAMS = {
    "v_max": 1.7,
    "k_heading": 6.0,
    "w_max": 8.0,
    "lead_s": 0.12,          # aim at where the opponent will be this far ahead (uses its velocity)
    "flank_m": 0.55,         # waypoint beside the opponent, perpendicular to its heading
    "side_deg": 60.0,        # I "have its side" when the opponent's heading is this far off the line to me
    "contact_m": 0.48,
    "push_out_m": 0.6,       # when pushing, aim through the opponent this far toward the edge
    "head_on_s": 0.35,       # head-on shove lasting this long -> disengage and re-flank
    "disengage_s": 0.4,
    "edge_margin_m": 0.6,    # being pushed with the edge this close -> escape along the edge
    "escape_s": 0.45,
    "dodge_dist_m": 0.9,     # opponent charging at me from within this distance ...
    "dodge_align_deg": 30.0, # ... aimed within this angle of me ...
    "dodge_closing_mps": 0.8,  # ... and closing this fast -> side-step
    "dodge_s": 0.30,
    "brake_mps2": 6.0,       # assumed braking deceleration for the edge guard (friction allows ~10)
    "brake_margin_m": 0.10,  # reaction allowance on top of the stopping distance
    "noise_rad": 0.02,
}


def reactive(state: protocol.State, params: dict, rng: random.Random) -> tuple[float, float]:
    """Time-critical sumo: get the opponent's side, push it outward, escape
    when pushed. Equal bots shoving head-on stall, so the fight is decided by
    who reaches the other's side first and who notices being pushed toward
    the edge first -- both are reaction-time contests, so stale state costs.

    Rules, in priority order:
    1. *escape* -- opponent in contact, closing on me, and the edge within
       ``edge_margin_m``: break out tangentially (biased inward) for
       ``escape_s``. Late = pushed farther before turning.
    2. *disengage* -- head-on contact for longer than ``head_on_s``: reverse
       and switch flank. A slow controller sits in the shove longer.
    3. *push* -- I have its side (its heading >= ``side_deg`` off the line to
       me): drive through the opponent's predicted position toward the edge.
    4. *flank* -- otherwise drive to a point beside the opponent's predicted
       position and come in from the side.
    Attack targets use the opponent's velocity times ``lead_s``: with old
    state the lead points where the opponent was."""
    mem = params.setdefault("_mem", {"mode": None, "mode_until": 0.0, "mode_dx": 0.0, "mode_dy": 0.0,
                                     "side": None, "head_on_since": None, "disengage_turn": 1.0,
                                     "escapes": 0, "disengages": 0, "pushes": 0, "dodges": 0, "brakes": 0})
    t = state.sim_time_s
    ox, oy = state.opp_x, state.opp_y
    rx, ry = ox - state.x, oy - state.y                     # me -> opponent
    dist_opp = math.hypot(rx, ry)
    ux, uy = (rx / dist_opp, ry / dist_opp) if dist_opp > 1e-6 else (1.0, 0.0)
    hx, hy = math.cos(state.opp_yaw), math.sin(state.opp_yaw)  # opponent heading
    nx, ny = -hy, hx                                            # opponent's left
    my_rad = math.hypot(state.x, state.y)
    radx, rady = (state.x / my_rad, state.y / my_rad) if my_rad > 1e-6 else (1.0, 0.0)
    opp_rad = math.hypot(ox, oy)
    oradx, orady = (ox / opp_rad, oy / opp_rad) if opp_rad > 1e-6 else (1.0, 0.0)
    closing = -(state.opp_vx * ux + state.opp_vy * uy) + (state.vx * ux + state.vy * uy)
    if mem["side"] is None:
        mem["side"] = 1.0 if (nx * -rx + ny * -ry) >= 0 else -1.0

    def steer_to(tx: float, ty: float, v_scale: float = 1.0) -> tuple[float, float]:
        heading_err = wrap_angle(math.atan2(ty - state.y, tx - state.x) - state.yaw)
        if params["noise_rad"] > 0:
            heading_err += rng.gauss(0.0, params["noise_rad"])
        w = max(-params["w_max"], min(params["w_max"], params["k_heading"] * heading_err))
        v = v_scale * params["v_max"] * max(0.0, math.cos(heading_err))
        return diff_drive(v, w)

    def steer_heading(dx: float, dy: float) -> tuple[float, float]:
        return steer_to(state.x + dx, state.y + dy)

    # --- a reflex already running ------------------------------------------
    if mem["mode"] is not None:
        if t < mem["mode_until"]:
            if mem["mode"] == "disengage":
                return diff_drive(-0.7 * params["v_max"], mem["disengage_turn"] * 0.6 * params["w_max"])
            return steer_heading(mem["mode_dx"], mem["mode_dy"])
        if mem["mode"] == "disengage":
            mem["side"] = -mem["side"]
        mem["mode"] = None
        mem["head_on_since"] = None

    in_contact = dist_opp < params["contact_m"]
    # angle between the opponent's heading and the line from it to me: 0 = it faces me
    cos_off = (hx * -ux + hy * -uy)
    off_deg = math.degrees(math.acos(max(-1.0, min(1.0, cos_off))))
    # angle between my heading and the line to the opponent: 0 = I face it
    cos_mine = math.cos(state.yaw) * ux + math.sin(state.yaw) * uy
    mine_deg = math.degrees(math.acos(max(-1.0, min(1.0, cos_mine))))

    # 1. escape: being pushed toward the edge
    if in_contact and closing > 0.25 and state.dist_to_edge_m < params["edge_margin_m"] and off_deg < 70:
        tx_, ty_ = -rady, radx
        if (tx_ * rx + ty_ * ry) > 0:
            tx_, ty_ = -tx_, -ty_
        dx, dy = tx_ * 0.8 - radx * 0.6, ty_ * 0.8 - rady * 0.6
        mem.update(mode="escape", mode_until=t + params["escape_s"], mode_dx=dx, mode_dy=dy)
        mem["escapes"] += 1
        return steer_heading(dx, dy)

    # 1b. edge guard with a real stopping distance: heading outward at speed with
    # less than (v^2 / 2a + margin) of ring left -> brake hard and turn inward.
    # A late brain sees the edge later and travels its delay x speed farther.
    speed = math.hypot(state.vx, state.vy)
    outward = math.cos(state.yaw) * radx + math.sin(state.yaw) * rady if my_rad > 1e-3 else 0.0
    if outward > 0.5 and speed > 0.3 and not (in_contact and closing > 0.25):
        stop_m = speed * speed / (2.0 * params["brake_mps2"]) + params["brake_margin_m"]
        if state.dist_to_edge_m < stop_m:
            mem["brakes"] += 1
            turn = 1.0 if (-radx * -math.sin(state.yaw) + -rady * math.cos(state.yaw)) >= 0 else -1.0
            return diff_drive(-params["v_max"], turn * 0.5 * params["w_max"])

    # 1c. dodge: opponent charging straight at me -> side-step so it drives past
    opp_speed = math.hypot(state.opp_vx, state.opp_vy)
    if (not in_contact and dist_opp < params["dodge_dist_m"] and opp_speed > 0.2
            and closing > params["dodge_closing_mps"] and off_deg < params["dodge_align_deg"]):
        sx, sy = nx, ny
        if (sx * radx + sy * rady) > 0:
            sx, sy = -sx, -sy
        mem.update(mode="dodge", mode_until=t + params["dodge_s"], mode_dx=sx, mode_dy=sy)
        mem["dodges"] += 1
        return steer_heading(sx, sy)

    # 2. head-on shove -> disengage
    head_on = in_contact and off_deg < params["side_deg"] and mine_deg < 60
    if head_on:
        if mem["head_on_since"] is None:
            mem["head_on_since"] = t
        elif t - mem["head_on_since"] > params["head_on_s"]:
            mem.update(mode="disengage", mode_until=t + params["disengage_s"],
                       disengage_turn=rng.choice((-1.0, 1.0)) if params["noise_rad"] > 0 else 1.0)
            mem["head_on_since"] = None
            mem["disengages"] += 1
            return diff_drive(-0.7 * params["v_max"], mem["disengage_turn"] * 0.6 * params["w_max"])
    else:
        mem["head_on_since"] = None

    px, py = ox + state.opp_vx * params["lead_s"], oy + state.opp_vy * params["lead_s"]
    # 3. push: I have its side -> drive through it toward the edge
    if off_deg >= params["side_deg"] and dist_opp < 2.5 * params["contact_m"]:
        mem["pushes"] += 1
        return steer_to(px + oradx * params["push_out_m"], py + orady * params["push_out_m"])
    # 4. flank
    tx = px + mem["side"] * nx * params["flank_m"]
    ty = py + mem["side"] * ny * params["flank_m"]
    return steer_to(tx, ty)


BALANCE_PARAMS = {
    "strategy": "ram",       # where the bot wants to go: ram | reactive | pusher | stand
    "v_cmd_max": 0.7,        # m/s the strategy may ask for (its own v_max is overridden)
    "w_cmd_max": 2.5,        # rad/s
    "ref_tau_s": 0.25,       # low-pass on the strategy's wishes: a chasing reference excites the slow pendulum mode
    # Gains from a grid search in closed loop through a one-way delay (R2c, head
    # at 0.95 m): stands with 60 ms one-way delay at rest, 45 ms at 0.6 m/s.
    "k_pitch": 8.0,          # N m per rad of lean            (all gains same sign: the base
    "k_pitch_rate": 4.5,     # N m per rad/s                   drives under the falling body)
    "k_v": 3.0,              # N m per m/s of speed error (too fast -> push base ahead -> lean back -> slow)
    "k_yaw": 0.15,           # N m differential per rad/s of yaw-rate error (balance torque has priority)
    "v_lean_limit": 1.0,     # |v - v_ref| is clipped here so a big speed error cannot demand a fatal lean
    "v_ref_accel": 1.5,      # m/s^2 slew limit on the speed reference: a step would demand a fatal lean
    "w_ref_accel": 8.0,      # rad/s^2 slew limit on the yaw-rate reference
    "noise_rad": 0.02,
}


def stand(state: protocol.State, params: dict, rng: random.Random) -> tuple[float, float]:
    """Strategy that only stands still (probe runs)."""
    return 0.0, 0.0


def cruise(state: protocol.State, params: dict, rng: random.Random) -> tuple[float, float]:
    """Strategy that drives straight at ``cruise_v`` m/s (offline threshold probes)."""
    return diff_drive(params.get("cruise_v", 0.5), 0.0)


RAM_PARAMS = {
    "v_max": 0.7, "w_max": 2.5, "k_heading": 4.0, "lead_s": 0.15,
    "edge_margin_m": 0.45,   # heading outward with less ring than this -> turn back in
    "noise_rad": 0.02,
}


def ram(state: protocol.State, params: dict, rng: random.Random) -> tuple[float, float]:
    """Strategy for the balance bot: drive into the opponent, keep away from
    the edge. The collision is the test -- a shove is a disturbance the remote
    balance loop has to catch, and the loop with the older state loses."""
    mem = params.setdefault("_mem", {"pushes": 0, "brakes": 0})
    tx = state.opp_x + state.opp_vx * params["lead_s"]
    ty = state.opp_y + state.opp_vy * params["lead_s"]
    my_rad = math.hypot(state.x, state.y)
    if my_rad > 1e-3 and state.dist_to_edge_m < params["edge_margin_m"]:
        outward = (math.cos(state.yaw) * state.x + math.sin(state.yaw) * state.y) / my_rad
        if outward > 0.2:
            mem["brakes"] += 1
            tx, ty = -state.x, -state.y      # aim at the centre
    heading_err = wrap_angle(math.atan2(ty - state.y, tx - state.x) - state.yaw)
    if params["noise_rad"] > 0:
        heading_err += rng.gauss(0.0, params["noise_rad"])
    w = max(-params["w_max"], min(params["w_max"], params["k_heading"] * heading_err))
    v = params["v_max"] * max(0.0, math.cos(heading_err))
    mem["pushes"] += 1
    return diff_drive(v, w)


def balance(state: protocol.State, params: dict, rng: random.Random) -> tuple[float, float]:
    """Remote balance of the two-wheeled inverted pendulum (--bot balance) plus a
    fighting strategy on top. Returns wheel TORQUES (N m).

    The strategy (``reactive`` by default) produces wheel speed targets as for
    the sumo bot; they are turned into a body speed / yaw-rate reference. The
    balance law is a linear full-state feedback on (pitch, pitch rate, speed
    error): ``u = k_pitch*pitch + k_pitch_rate*pitch_rate + k_v*(v - v_ref)``,
    the classic segway loop. Nothing here is local to the robot: every torque
    crosses the link, so link delay lowers the loop's phase margin and a stale
    interval (torque 0) lets the pendulum fall freely."""
    v = state.vx * math.cos(state.yaw) + state.vy * math.sin(state.yaw)
    return _balance_core(state, params, rng, state.pitch, state.pitch_rate, v)


def _balance_core(state: protocol.State, params: dict, rng: random.Random,
                  pitch: float, pitch_rate: float, v: float) -> tuple[float, float]:
    """The balance law on an explicit (pitch, pitch_rate, v): ``balance`` passes
    the measured values, ``balance_comp`` the values predicted for the moment
    the command will be applied."""
    mem = params.setdefault("_mem", {})
    if "strategy_params" not in mem:
        name = params["strategy"]
        base = {"ram": RAM_PARAMS, "reactive": REACTIVE_PARAMS, "pusher": DEFAULT_PARAMS, "stand": {},
                "cruise": {"cruise_v": params.get("cruise_v", 0.5)}}[name]
        sp = dict(base)
        sp.update({"v_max": params["v_cmd_max"], "w_max": params["w_cmd_max"], "noise_rad": params["noise_rad"]})
        mem["strategy_params"] = sp
        mem["strategy_fn"] = {"ram": ram, "reactive": reactive, "pusher": pusher, "stand": stand, "cruise": cruise}[name]
    left_ref, right_ref = mem["strategy_fn"](state, mem["strategy_params"], rng)
    v_want = (left_ref + right_ref) / 2.0 * WHEEL_RADIUS
    w_want = (right_ref - left_ref) * WHEEL_RADIUS / WHEEL_BASE
    v_want = max(-params["v_cmd_max"], min(params["v_cmd_max"], v_want))
    w_want = max(-params["w_cmd_max"], min(params["w_cmd_max"], w_want))
    # Slew the references: the strategy switches targets abruptly, and a speed
    # step through k_v would demand a lean the pendulum cannot recover from.
    t = state.sim_time_s
    dt = max(0.0, min(0.05, t - mem.get("t_prev", t)))
    mem["t_prev"] = t
    v_ref = mem.get("v_ref", 0.0)
    w_ref = mem.get("w_ref", 0.0)
    alpha = dt / (params["ref_tau_s"] + dt) if params["ref_tau_s"] > 0 else 1.0
    dv, dw = params["v_ref_accel"] * dt, params["w_ref_accel"] * dt
    v_ref += max(-dv, min(dv, alpha * (v_want - v_ref)))
    w_ref += max(-dw, min(dw, alpha * (w_want - w_ref)))
    mem["v_ref"], mem["w_ref"] = v_ref, w_ref
    v_err = max(-params["v_lean_limit"], min(params["v_lean_limit"], v - v_ref))
    u = params["k_pitch"] * pitch + params["k_pitch_rate"] * pitch_rate + params["k_v"] * v_err
    d = params["k_yaw"] * (w_ref - state.wz)
    # Balance has priority over steering: the differential may only use what
    # the common torque leaves inside the motor limit.
    limit = params.get("torque_max", 1.5)
    u = max(-limit, min(limit, u))
    room = limit - abs(u)
    d = max(-room, min(room, d))
    return u - d, u + d


# --- delay compensation (R7a) ------------------------------------------------
#
# Linearised two-wheeled inverted pendulum, derived from the arena's MJCF
# (arena.py: BAL_* constants). Body = chassis + plate + pole + head (everything
# but the wheels), pitch theta (+ = leaning forward), base speed v, total joint
# torque T = u_l + u_r minus the wheel-hinge damping. Standard Segway equations
# with theta small:
#
#     a11 v' + a12 theta'' = T / r
#     a12 v' + a22 theta'' = m_b g l theta - T
#
# a11 = m_b + 2 m_w + 2 I_w / r^2, a12 = m_b l, a22 = I_b + m_b l^2 (about the
# axle). The plate sits 10 cm ahead of the axle, which the linear model drops
# (a 6 mm forward CoM offset: a constant lean the feedback absorbs).

class PendulumModel:
    def __init__(self, base_mass: float = 1.0, plate_mass: float = 0.2, pole_mass: float = 0.3, head_mass: float = 2.0,
                 base_half_z: float = 0.02, pole_top: float = 0.89, head_z: float = 0.95, head_half: float = 0.06,
                 base_half_xy: tuple[float, float] = (0.08, 0.10), wheel_mass: float = 0.2,
                 wheel_radius: float = WHEEL_RADIUS, wheel_damping: float = 0.02, g: float = 9.81) -> None:
        m_b = base_mass + plate_mass + pole_mass + head_mass
        pole_mid = (base_half_z + pole_top) / 2.0
        pole_len = pole_top - base_half_z
        l = (base_mass * 0.0 + plate_mass * 0.0 + pole_mass * pole_mid + head_mass * head_z) / m_b
        # inertia about the axle (parallel axis from each part's own CoM)
        i_head = head_mass / 12.0 * (2 * (2 * head_half) ** 2) + head_mass * head_z ** 2
        i_base = base_mass / 12.0 * ((2 * base_half_xy[0]) ** 2 + (2 * base_half_z) ** 2)
        i_plate = plate_mass * 0.10 ** 2
        i_pole = pole_mass * pole_len ** 2 / 12.0 + pole_mass * pole_mid ** 2
        i_axle = i_head + i_base + i_plate + i_pole
        i_w = 0.5 * wheel_mass * wheel_radius ** 2
        self.r = wheel_radius
        self.c = wheel_damping
        self.m_b, self.l, self.g = m_b, l, g
        self.a11 = m_b + 2 * wheel_mass + 2 * i_w / wheel_radius ** 2
        self.a12 = m_b * l
        self.a22 = i_axle
        self.det = self.a11 * self.a22 - self.a12 ** 2
        self.mgl = m_b * g * l

    def accel(self, pitch: float, pitch_rate: float, v: float, u_total: float) -> tuple[float, float]:
        """(v_dot, pitch_ddot) for a total motor torque u_total (both wheels)."""
        torque = u_total - 2.0 * self.c * (v / self.r - pitch_rate)
        rhs_v = torque / self.r
        rhs_t = self.mgl * pitch - torque
        v_dot = (self.a22 * rhs_v - self.a12 * rhs_t) / self.det
        pitch_ddot = (self.a11 * rhs_t - self.a12 * rhs_v) / self.det
        return v_dot, pitch_ddot

    def growth_rate(self) -> float:
        """Open-loop unstable pole, rad/s."""
        return math.sqrt(self.a11 * self.mgl / self.det)

    def rollout(self, pitch: float, pitch_rate: float, v: float, horizon_s: float,
                torque_at, dt: float = 0.001) -> tuple[float, float, float]:
        """Integrate for ``horizon_s`` with ``torque_at(t_rel) -> per-wheel
        torque`` (semi-implicit Euler, arena timestep)."""
        t = 0.0
        while t < horizon_s - 1e-9:
            step = min(dt, horizon_s - t)
            v_dot, p_dd = self.accel(pitch, pitch_rate, v, 2.0 * torque_at(t))
            pitch_rate += p_dd * step
            v += v_dot * step
            pitch += pitch_rate * step
            t += step
        return pitch, pitch_rate, v


COMP_PARAMS = dict(BALANCE_PARAMS)
COMP_PARAMS.update({
    "horizon_extra_ms": 1.0,   # arena poll + half a command period is where the new command really acts
    "max_horizon_ms": 500.0,   # never predict further than this (model error grows exponentially)
    "cmd_hist": 128,
})


def balance_comp(state: protocol.State, params: dict, rng: random.Random) -> tuple[float, float]:
    """``balance`` with delay compensation (Smith-predictor style). The brain
    estimates the link from the protocol's own timestamps (``params['_link']``
    is filled by ``Brain`` before every tick: ``now_us``, ``state_age_us`` --
    time since the arena sampled the STATE in hand --, ``cmd_one_way_us``,
    ``period_us``), forward-simulates the pendulum from that STATE to the
    moment the command it is about to send will be applied, using the torques
    it already sent but that have not been applied yet (command history), and
    runs the same balance law on the predicted (pitch, pitch rate, speed).
    With a good model the loop's delay is cancelled up to the model error;
    without ``_link`` it degrades to plain ``balance``."""
    mem = params.setdefault("_mem", {})
    link = params.get("_link")
    hist = mem.setdefault("cmd_hist", collections.deque(maxlen=int(params.get("cmd_hist", 128))))
    v_meas = state.vx * math.cos(state.yaw) + state.vy * math.sin(state.yaw)
    pitch, pitch_rate, v = state.pitch, state.pitch_rate, v_meas
    horizon_s = 0.0
    if link and link.get("state_age_us") is not None and link.get("cmd_one_way_us") is not None:
        model = mem.get("model")
        if model is None:
            model = mem["model"] = PendulumModel()
        now = link["now_us"]
        t_state = now - link["state_age_us"]
        t_land = now + link["cmd_one_way_us"] + params["horizon_extra_ms"] * 1000.0
        horizon_us = max(0.0, min(params["max_horizon_ms"] * 1000.0, t_land - t_state))
        cmd_one_way = link["cmd_one_way_us"]
        # torque applied at the arena at (brain-clock) time t: the newest command
        # whose landing time t_send + cmd_one_way <= t; before the first one, 0.
        landed = [(t_send + cmd_one_way, u) for t_send, u in hist]

        def torque_at(t_rel: float) -> float:
            t_abs = t_state + t_rel * 1e6
            u = 0.0
            for t_l, u_k in landed:
                if t_l <= t_abs:
                    u = u_k
                else:
                    break
            return u

        horizon_s = horizon_us / 1e6
        pitch, pitch_rate, v = model.rollout(pitch, pitch_rate, v, horizon_s, torque_at)
        mem["last_horizon_us"] = horizon_us
        mem["last_pred"] = (pitch, pitch_rate, v)
    left, right = _balance_core(state, params, rng, pitch, pitch_rate, v)
    hist.append(((link["now_us"] if link else protocol.now_us()), (left + right) / 2.0))
    return left, right


POLICIES = {"pusher": (pusher, DEFAULT_PARAMS), "reactive": (reactive, REACTIVE_PARAMS),
            "balance": (balance, BALANCE_PARAMS), "balance_comp": (balance_comp, COMP_PARAMS)}


def load_policy(spec: str):
    if spec in POLICIES:
        return POLICIES[spec][0]
    module_name, _, attr = spec.partition(":")
    if not attr:
        raise SystemExit("--policy must be one of %s or module:callable" % "/".join(POLICIES))
    return getattr(importlib.import_module(module_name), attr)


class Brain:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.robot_id = args.robot_id
        self.rng = random.Random(args.seed)
        self.params = dict(POLICIES[args.policy][1] if args.policy in POLICIES else DEFAULT_PARAMS)
        if args.params:
            self.params.update(json.loads(args.params))
        if args.seed is not None and args.param_jitter > 0:
            for key in ("v_max", "k_heading", "flank_m", "v_cmd_max"):
                if key in self.params:
                    self.params[key] *= 1.0 + self.rng.uniform(-args.param_jitter, args.param_jitter)
        self.policy = load_policy(args.policy)
        host, _, port = args.robot.rpartition(":")
        self.robot_addr = (host, int(port))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if args.bind:
            bhost, _, bport = args.bind.rpartition(":")
            self.sock.bind((bhost, int(bport)))
        self.log_path = pathlib.Path(args.log)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = self.log_path.open("w", encoding="utf-8")
        self.cmd_seq = 0
        self.last_state: protocol.State | None = None
        self.last_state_header: protocol.Header | None = None
        self.last_state_recv_us = 0
        self.states_received = 0
        self.state_gaps = 0
        self.rtt_us: list[int] = []
        self.last_echoed_seq = 0
        self.state_one_way_us: list[int] = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.fresh = False
        self.thread = threading.Thread(target=self.receiver, name="brain-rx", daemon=True)
        self.ticks = 0
        self.ticks_without_fresh_state = 0
        self.deadline_misses = 0
        self.over_flags = 0
        # Link estimate for delay-compensating policies (balance_comp): the
        # windowed minimum RTT strips the STATE-period quantisation, the STATE
        # one-way is trusted only when it is consistent with the RTT (shared
        # clock), otherwise both directions are taken as RTT/2.
        self.rtt_window: collections.deque[int] = collections.deque(maxlen=int(args.rate_hz * args.link_window_s))
        self.state_ow_est_us: float | None = None
        self.link_horizons_us: list[float] = []
        self.link_last_log = 0.0
        self.link: dict = {"now_us": 0, "state_age_us": None, "cmd_one_way_us": None,
                           "period_us": int(1e6 / args.rate_hz), "shared_clock": None, "rtt_min_us": None}

    def emit(self, record: dict) -> None:
        record.setdefault("t_unix_us", protocol.now_us())
        self.log.write(json.dumps(record, separators=(",", ":")) + "\n")

    def receiver(self) -> None:
        """Blocking receive loop on its own thread so arrival timestamps are
        the real arrival times, not the brain's next tick."""
        self.sock.settimeout(0.05)
        while not self.stop.is_set():
            try:
                data, _ = self.sock.recvfrom(2048)
            except (socket.timeout, BlockingIOError, ConnectionRefusedError):
                continue
            except OSError:
                break
            now = protocol.now_us()
            try:
                header, body = protocol.unpack(data)
            except protocol.ProtocolError as exc:
                with self.lock:
                    self.emit({"event": "bad_datagram", "error": str(exc)})
                continue
            if header.kind != protocol.KIND_STATE or header.robot_id != self.robot_id or not isinstance(body, protocol.State):
                continue
            with self.lock:
                self.states_received += 1
                if self.last_state_header is not None:
                    if header.seq <= self.last_state_header.seq:
                        continue
                    if header.seq > self.last_state_header.seq + 1:
                        self.state_gaps += header.seq - self.last_state_header.seq - 1
                rtt = protocol.rtt_us(header, now)
                if rtt is not None and header.echo_seq != self.last_echoed_seq:
                    # First STATE that echoes a given command: RTT = link round trip
                    # + up to one STATE period. Later echoes of the same command
                    # would only add the brain's own tick period.
                    self.last_echoed_seq = header.echo_seq
                    self.rtt_us.append(rtt)
                    self.rtt_window.append(rtt)
                one_way = now - header.t_send_us
                self.state_one_way_us.append(one_way)  # valid when clocks are shared
                if self.state_ow_est_us is None:
                    self.state_ow_est_us = float(one_way)
                else:
                    self.state_ow_est_us += 0.05 * (one_way - self.state_ow_est_us)
                self.last_state, self.last_state_header, self.last_state_recv_us = body, header, now
                self.fresh = True
                if self.args.log_states:
                    self.emit({"event": "state", "seq": header.seq, "rtt_us": rtt, "one_way_us": now - header.t_send_us,
                               "echo_seq": header.echo_seq, "sim_time_s": round(body.sim_time_s, 4), "flags": body.flags})
                if body.flags & protocol.FLAG_OVER:
                    self.over_flags = body.flags

    def take_state(self) -> tuple[protocol.State | None, bool, protocol.Header | None, int]:
        """Snapshot the policy input and its timestamps under the same lock."""
        with self.lock:
            fresh, self.fresh = self.fresh, False
            return self.last_state, fresh, self.last_state_header, self.last_state_recv_us

    def send_command(self, left: float, right: float) -> None:
        self.cmd_seq += 1
        echo_seq = self.last_state_header.seq if self.last_state_header else 0
        echo_t = self.last_state_header.t_send_us if self.last_state_header else 0
        header = protocol.Header(protocol.KIND_CMD, self.robot_id, self.cmd_seq, protocol.now_us(), echo_seq, echo_t)
        try:
            self.sock.sendto(protocol.pack_command(header, protocol.Command(left, right, self.args.ttl_ms)), self.robot_addr)
        except OSError as exc:
            self.emit({"event": "send_error", "error": str(exc)})

    def run(self) -> dict:
        period = 1.0 / self.args.rate_hz
        t0 = time.perf_counter()
        next_tick = t0
        deadline = t0 + self.args.max_seconds
        self.thread.start()
        # Kick: send a zero command so the arena learns our address and starts sending STATE.
        self.send_command(0.0, 0.0)
        while time.perf_counter() < deadline:
            now = time.perf_counter()
            if now < next_tick:
                time.sleep(min(next_tick - now, period))
                continue
            if now - next_tick > period:
                self.deadline_misses += 1
                next_tick = now  # resync rather than burst
            next_tick += period
            self.ticks += 1
            state, fresh, state_header, state_recv_us = self.take_state()
            if self.over_flags:
                break
            if state is None:
                self.send_command(0.0, 0.0)
                continue
            if not fresh:
                self.ticks_without_fresh_state += 1
            self.update_link(state_header, state_recv_us)
            left, right = self.policy(state, self.params, self.rng)
            with self.lock:
                self.send_command(left, right)
        self.stop.set()
        self.thread.join(timeout=0.5)
        return self.summary()

    def update_link(self, state_header: protocol.Header | None, state_recv_us: int) -> None:
        """Estimate age for the snapshotted policy input, even if RX advanced."""
        now = protocol.now_us()
        with self.lock:
            rtt_min = min(self.rtt_window) if self.rtt_window else None
            state_ow = self.state_ow_est_us
        t_send = state_header.t_send_us if state_header else None
        link = self.link
        link["now_us"] = now
        link["rtt_min_us"] = rtt_min
        if rtt_min is None or t_send is None:
            link["state_age_us"] = None
            link["cmd_one_way_us"] = None
        else:
            shared = state_ow is not None and -500.0 <= state_ow <= rtt_min + 500.0
            link["shared_clock"] = shared
            if shared:
                link["state_age_us"] = max(0, now - t_send)
                link["cmd_one_way_us"] = max(0.0, rtt_min - max(0.0, state_ow))
            else:
                link["state_age_us"] = max(0, now - state_recv_us) + rtt_min / 2.0
                link["cmd_one_way_us"] = rtt_min / 2.0
            self.link_horizons_us.append(link["state_age_us"] + link["cmd_one_way_us"])
        self.params["_link"] = link
        if now - self.link_last_log > 1_000_000:
            self.link_last_log = now
            self.emit({"event": "link_est", "rtt_min_us": rtt_min, "state_one_way_us": None if state_ow is None else int(state_ow),
                       "state_age_us": link["state_age_us"], "cmd_one_way_us": link["cmd_one_way_us"],
                       "shared_clock": link["shared_clock"]})

    def summary(self) -> dict:
        rtt = np.array(self.rtt_us, dtype=np.int64) if self.rtt_us else np.array([0])
        ow = np.array(self.state_one_way_us, dtype=np.int64) if self.state_one_way_us else np.array([0])
        outcome = "unknown"
        if self.over_flags & protocol.FLAG_WON:
            outcome = "won"
        elif self.over_flags & protocol.FLAG_LOST:
            outcome = "lost"
        elif self.over_flags & protocol.FLAG_OVER:
            outcome = "draw"
        mem = self.params.get("_mem", {})
        if "strategy_params" in mem:   # balance: the reflex counters live in the strategy's memory
            mem = mem["strategy_params"].get("_mem", {})
        result = {
            "robot_id": self.robot_id, "policy": self.args.policy, "seed": self.args.seed,
            "params": {k: v for k, v in self.params.items() if not k.startswith("_")},
            "reflexes": {k: mem[k] for k in ("dodges", "escapes", "disengages", "pushes", "brakes") if k in mem},
            "rate_hz": self.args.rate_hz, "ttl_ms": self.args.ttl_ms, "outcome": outcome,
            "ticks": self.ticks, "deadline_misses": self.deadline_misses,
            "ticks_without_fresh_state": self.ticks_without_fresh_state, "cmds_sent": self.cmd_seq,
            "states_received": self.states_received, "state_seq_gaps": self.state_gaps,
            "rtt_us": {"p50": int(np.percentile(rtt, 50)), "p90": int(np.percentile(rtt, 90)),
                       "p99": int(np.percentile(rtt, 99)), "max": int(rtt.max()), "n": len(self.rtt_us)},
            "state_one_way_us": {"p50": int(np.percentile(ow, 50)), "p99": int(np.percentile(ow, 99)),
                                 "max": int(ow.max()), "n": len(self.state_one_way_us)},
            "link_est": {"rtt_min_us": self.link["rtt_min_us"], "cmd_one_way_us": self.link["cmd_one_way_us"],
                         "state_age_us": self.link["state_age_us"], "shared_clock": self.link["shared_clock"],
                         "horizon_ms_mean": None if not self.link_horizons_us else round(float(np.mean(self.link_horizons_us)) / 1000.0, 2)},
        }
        self.emit({"event": "summary", **result})
        return result

    def close(self) -> None:
        self.log.close()
        self.sock.close()


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--robot-id", type=int, required=True, choices=(0, 1))
    p.add_argument("--robot", required=True, help="arena UDP address for this robot, host:port")
    p.add_argument("--bind", default=None, help="local host:port to bind (default: ephemeral)")
    p.add_argument("--rate-hz", type=float, default=100.0)
    p.add_argument("--ttl-ms", type=int, default=60, help="how long the robot may keep applying a command")
    p.add_argument("--policy", default="reactive",
                   help="'reactive' (default), 'pusher', 'balance' (for --bot balance arenas; its 'strategy' "
                        "param picks reactive/pusher/stand on top of the balance loop), 'balance_comp' (balance with "
                        "delay compensation from the link estimate), or module:callable")
    p.add_argument("--link-window-s", type=float, default=0.5, help="window for the minimum-RTT link estimate")
    p.add_argument("--params", default=None, help="JSON overrides for policy params")
    p.add_argument("--param-jitter", type=float, default=0.1, help="seeded +-fraction applied to v_max/k_heading/flank_m")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--max-seconds", type=float, default=120.0)
    p.add_argument("--log-states", action="store_true", help="log every STATE (large)")
    p.add_argument("--log", default="brain.jsonl")
    p.add_argument("--result", default=None)
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    brain = Brain(args)
    try:
        result = brain.run()
    finally:
        brain.close()
    if args.result:
        pathlib.Path(args.result).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("robot_id", "outcome", "cmds_sent", "states_received", "rtt_us")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
