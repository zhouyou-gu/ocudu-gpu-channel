#!/usr/bin/env python3
"""Offline closed loop: arena physics + a brain policy through an ideal delay
line, no network, no wall clock. Used by the threshold curves (R2c/R7a) and
the tests. The link is exact here (the policy is told the true delays), so
the curves bound what the live estimate can achieve.

    python offline_loop.py --policy balance_comp --delay-ms 100 --cruise 0.5
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import pathlib
import random
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import arena as arena_mod  # type: ignore
    import brain as brain_mod  # type: ignore
    import protocol  # type: ignore
else:
    from . import arena as arena_mod, brain as brain_mod, protocol


def run(policy: str = "balance", delay_ms: float = 0.0, cruise_v: float | None = None, duration_s: float = 6.0,
        blackout_ms: float = 0.0, blackout_at_s: float = 2.5, state_hz: float = 200.0, rate_hz: float = 100.0,
        ttl_ms: float = 60.0, initial_lean_rad: float = 0.05, params_override: dict | None = None,
        seed: int = 0) -> dict:
    """Returns {fell: bool, t_fall: float|None, max_pitch: float, mean_abs_pitch, horizon_ms}."""
    import mujoco

    policy_fn, base_params = brain_mod.POLICIES[policy]
    params = dict(base_params)
    params["strategy"] = "cruise" if cruise_v else "stand"
    params["cruise_v"] = cruise_v or 0.0
    params["noise_rad"] = 0.0
    if params_override:
        params.update(params_override)
    model = mujoco.MjModel.from_xml_string(arena_mod.build_model_xml(2.0, bot_type="balance"))
    data = mujoco.MjData(model)
    qa = model.jnt_qposadr[mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, "bot0_free")]
    va = model.jnt_dofadr[mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, "bot0_free")]
    qb = model.jnt_qposadr[mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, "bot1_free")]
    data.qpos[qb:qb + 3] = (5, 5, arena_mod.WHEEL_RADIUS)
    half = initial_lean_rad / 2.0
    data.qpos[qa + 3:qa + 7] = (math.cos(half), 0, math.sin(half), 0)
    mujoco.mj_forward(model, data)
    tau = delay_ms / 1000.0
    states: collections.deque = collections.deque()
    cmds: collections.deque = collections.deque()
    last = None
    cur, cur_t = (0.0, 0.0), -1.0
    next_state = next_ctrl = 0.0
    rng = random.Random(seed)
    pitches: list[float] = []
    horizons: list[float] = []
    ttl_s = ttl_ms / 1000.0
    while data.time < duration_s:
        t = data.time
        blackout = blackout_ms > 0 and blackout_at_s <= t < blackout_at_s + blackout_ms / 1000.0
        if t >= next_state:
            w, qx, qy, qz = data.qpos[qa + 3:qa + 7]
            up_x, up_y = 2 * (qx * qz + w * qy), 2 * (qy * qz - w * qx)
            yaw = arena_mod.quat_to_yaw((w, qx, qy, qz))
            pitch = math.asin(max(-1.0, min(1.0, up_x * math.cos(yaw) + up_y * math.sin(yaw))))
            pitches.append(abs(pitch))
            st = protocol.State(t, data.qpos[qa], data.qpos[qa + 1], yaw, data.qvel[va], data.qvel[va + 1],
                                data.qvel[va + 5], 5, 5, 0, 0, 0, 2.0, 2.0, 0.0, pitch, data.qvel[va + 4],
                                data.qvel[va + 6] if model.nv > va + 6 else 0.0, 0.0, 1)
            if not blackout:
                states.append((t + tau, st))
            next_state += 1 / state_hz
        while states and states[0][0] <= t:
            last = states.popleft()[1]
        if t >= next_ctrl:
            next_ctrl += 1 / rate_hz
            if last is not None:
                now_us = int(t * 1e6)
                params["_link"] = {"now_us": now_us, "state_age_us": int((t - last.sim_time_s) * 1e6),
                                   "cmd_one_way_us": int(tau * 1e6), "period_us": int(1e6 / rate_hz),
                                   "shared_clock": True, "rtt_min_us": int(2 * tau * 1e6)}
                u = policy_fn(last, params, rng)
                mem = params.get("_mem", {})
                if "last_horizon_us" in mem:
                    horizons.append(mem["last_horizon_us"] / 1000.0)
                if not blackout:
                    cmds.append((t + tau, u))
        while cmds and cmds[0][0] <= t:
            cur_t, cur = cmds.popleft()
        fresh = (t - cur_t) <= ttl_s
        data.ctrl[0], data.ctrl[1] = (cur if fresh else (0.0, 0.0))
        mujoco.mj_step(model, data)
        if arena_mod.quat_up_z(data.qpos[qa + 3:qa + 7]) < arena_mod.BAL_FALL_UP:
            return {"policy": policy, "delay_ms": delay_ms, "cruise_v": cruise_v, "blackout_ms": blackout_ms,
                    "fell": True, "t_fall": round(float(data.time), 3), "max_pitch": round(max(pitches), 4),
                    "horizon_ms": round(sum(horizons) / len(horizons), 2) if horizons else None}
    tail = pitches[len(pitches) // 2:]
    return {"policy": policy, "delay_ms": delay_ms, "cruise_v": cruise_v, "blackout_ms": blackout_ms,
            "fell": False, "t_fall": None, "max_pitch": round(max(pitches), 4),
            "mean_abs_pitch_tail": round(sum(tail) / len(tail), 4),
            "horizon_ms": round(sum(horizons) / len(horizons), 2) if horizons else None}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--policy", default="balance")
    p.add_argument("--delay-ms", type=float, default=0.0)
    p.add_argument("--cruise", type=float, default=None)
    p.add_argument("--duration", type=float, default=6.0)
    p.add_argument("--blackout-ms", type=float, default=0.0)
    p.add_argument("--blackout-at", type=float, default=2.5)
    p.add_argument("--params", default=None)
    args = p.parse_args(argv)
    result = run(args.policy, args.delay_ms, args.cruise, args.duration, args.blackout_ms, args.blackout_at,
                 params_override=json.loads(args.params) if args.params else None)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
