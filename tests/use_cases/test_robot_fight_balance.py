"""Balance bot (--bot balance): the brain balances the inverted pendulum over
the link. Needs mujoco (robot venv). Two checks: both bots stand through a
short fight on loopback, and the offline closed loop through a delay falls
when the one-way delay is far past the tuned tolerance.
"""

import json
import pathlib
import subprocess
import sys

import pytest

pytest.importorskip("mujoco")
pytest.importorskip("zmq")

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIGHT = ROOT / "use_cases" / "robot_fight" / "fight.py"
sys.path.insert(0, str(ROOT / "use_cases" / "robot_fight"))

import arena as arena_mod  # noqa: E402
import brain as brain_mod  # noqa: E402
import protocol  # noqa: E402


def test_balance_bots_stand_through_a_short_fight(tmp_path):
    out = tmp_path / "fight"
    proc = subprocess.run(
        [sys.executable, str(FIGHT), "--bot", "balance", "--policy", "stand", "--fights", "1", "--time-limit", "6",
         "--seed", "11", "--port-base", "6950", "--out", str(out)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    fight = json.loads((out / "fight-000" / "fight.json").read_text())
    assert fight["bot"] == "balance"
    assert fight["hold_release_sim_s"] is not None and fight["hold_release_sim_s"] < 5.0
    assert fight["reason"] in ("timeout", "timeout_edge"), fight["reason"]   # nobody fell, nobody left the ring
    assert 0.9 <= fight["rtf"] <= 1.1
    for brain in fight["brains"]:
        assert brain["policy"] == "balance"
    poses = [json.loads(l) for l in (out / "fight-000" / "arena.jsonl").read_text().splitlines()
             if '"event":"pose"' in l]
    late = [p for p in poses if p["sim_time_s"] > fight["hold_release_sim_s"] + 1.0]
    assert late and all(abs(r["pitch"]) < 0.2 for p in late for r in p["robots"])   # < 11 deg after settling


def _closed_loop_falls(delay_ms: float) -> bool:
    """Offline loop: physics + brain.balance() through a one-way delay, no network."""
    import collections
    import math

    import mujoco

    params = dict(brain_mod.BALANCE_PARAMS)
    params["strategy"] = "stand"
    params["noise_rad"] = 0.0
    model = mujoco.MjModel.from_xml_string(arena_mod.build_model_xml(2.0, bot_type="balance"))
    data = mujoco.MjData(model)
    qa = model.jnt_qposadr[mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, "bot0_free")]
    va = model.jnt_dofadr[mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, "bot0_free")]
    qb = model.jnt_qposadr[mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, "bot1_free")]
    data.qpos[qb:qb + 3] = (5, 5, arena_mod.WHEEL_RADIUS)
    data.qpos[qa + 3:qa + 7] = (math.cos(0.025), 0, math.sin(0.025), 0)   # 0.05 rad initial lean
    mujoco.mj_forward(model, data)
    tau = delay_ms / 1000.0
    states, cmds = collections.deque(), collections.deque()
    last = None
    cur, cur_t = (0.0, 0.0), -1.0
    next_state = next_ctrl = 0.0
    import random
    rng = random.Random(0)
    while data.time < 5.0:
        t = data.time
        if t >= next_state:
            w, qx, qy, qz = data.qpos[qa + 3:qa + 7]
            up_x, up_y = 2 * (qx * qz + w * qy), 2 * (qy * qz - w * qx)
            yaw = arena_mod.quat_to_yaw((w, qx, qy, qz))
            pitch = math.asin(max(-1.0, min(1.0, up_x * math.cos(yaw) + up_y * math.sin(yaw))))
            states.append((t + tau, protocol.State(t, data.qpos[qa], data.qpos[qa + 1], yaw, data.qvel[va], data.qvel[va + 1],
                                                   data.qvel[va + 5], 5, 5, 0, 0, 0, 2.0, 2.0, 0.0, pitch, data.qvel[va + 4], 0, 0, 1)))
            next_state += 1 / 200
        while states and states[0][0] <= t:
            last = states.popleft()[1]
        if t >= next_ctrl:
            next_ctrl += 1 / 100
            if last is not None:
                cmds.append((t + tau, brain_mod.balance(last, params, rng)))
        while cmds and cmds[0][0] <= t:
            cur_t, cur = cmds.popleft()
        fresh = (t - cur_t) <= 0.06
        data.ctrl[0], data.ctrl[1] = (cur if fresh else (0.0, 0.0))
        mujoco.mj_step(model, data)
        if arena_mod.quat_up_z(data.qpos[qa + 3:qa + 7]) < arena_mod.BAL_FALL_UP:
            return True
    return False


def test_offline_loop_delay_tolerance():
    assert not _closed_loop_falls(15.0)    # the radio's ~30 ms RTT
    assert _closed_loop_falls(150.0)       # far past the tuned ~60 ms one-way tolerance
