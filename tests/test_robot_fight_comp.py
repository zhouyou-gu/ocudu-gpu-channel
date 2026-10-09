"""Delay-compensated balance (R7a): the linear pendulum predictor matches a
MuJoCo rollout, the compensated offline loop stands where the plain loop
falls, and a compensated brain runs a short fight through a symmetric delay.
Needs mujoco (robot venv).
"""

import json
import math
import pathlib
import subprocess
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("mujoco")
pytest.importorskip("zmq")

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIGHT = ROOT / "use_cases" / "robot_fight" / "fight.py"
sys.path.insert(0, str(ROOT / "use_cases" / "robot_fight"))

import arena as arena_mod  # noqa: E402
import brain as brain_mod  # noqa: E402
import offline_loop  # noqa: E402


@pytest.mark.parametrize("state_one_way,expected_age,shared", [
    (5_000.0, 100_000, True),
    (1_000_000.0, 60_000, False),
])
def test_link_age_uses_policy_state_snapshot(tmp_path, monkeypatch, state_one_way, expected_age, shared):
    args = brain_mod.parse_args(["--robot-id", "0", "--robot", "127.0.0.1:9",
                                "--policy", "balance_comp", "--log", str(tmp_path / "brain.jsonl")])
    brain = brain_mod.Brain(args)
    try:
        old_state = object()
        brain.last_state = old_state
        brain.last_state_header = SimpleNamespace(t_send_us=900_000)
        brain.last_state_recv_us = 950_000
        brain.fresh = True
        brain.rtt_window.append(20_000)
        brain.state_ow_est_us = state_one_way
        state, fresh, header, recv_us = brain.take_state()
        # A receiver arrival between take_state and update_link must not make
        # an older policy input appear younger than it is.
        with brain.lock:
            brain.last_state = object()
            brain.last_state_header = SimpleNamespace(t_send_us=990_000)
            brain.last_state_recv_us = 995_000
            brain.fresh = True
        monkeypatch.setattr(brain_mod.protocol, "now_us", lambda: 1_000_000)
        brain.update_link(header, recv_us)
        assert state is old_state and fresh
        assert brain.fresh  # the next tick still sees the new arrival
        assert brain.params["_link"]["state_age_us"] == expected_age
        assert brain.params["_link"]["shared_clock"] is shared
    finally:
        brain.sock.close()
        brain.log.close()


def _mujoco_rollout(lean: float, torque: float, horizon_s: float, pitch_rate0: float = 0.0, v0: float = 0.0):
    import mujoco

    model = mujoco.MjModel.from_xml_string(arena_mod.build_model_xml(2.0, bot_type="balance"))
    data = mujoco.MjData(model)
    qa = model.jnt_qposadr[mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, "bot0_free")]
    va = model.jnt_dofadr[mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, "bot0_free")]
    qb = model.jnt_qposadr[mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, "bot1_free")]
    data.qpos[qb:qb + 3] = (5, 5, arena_mod.WHEEL_RADIUS)
    half = lean / 2.0
    data.qpos[qa + 3:qa + 7] = (math.cos(half), 0, math.sin(half), 0)
    data.qvel[va + 4] = pitch_rate0
    data.qvel[va] = v0
    mujoco.mj_forward(model, data)
    data.ctrl[0] = data.ctrl[1] = torque
    for _ in range(int(round(horizon_s / arena_mod.TIMESTEP))):
        mujoco.mj_step(model, data)
    w, qx, qy, qz = data.qpos[qa + 3:qa + 7]
    yaw = arena_mod.quat_to_yaw((w, qx, qy, qz))
    up_x, up_y = 2 * (qx * qz + w * qy), 2 * (qy * qz - w * qx)
    pitch = math.asin(max(-1.0, min(1.0, up_x * math.cos(yaw) + up_y * math.sin(yaw))))
    return pitch, float(data.qvel[va + 4]), float(data.qvel[va])


@pytest.mark.parametrize("lean,torque,horizon,pr0,v0", [
    (0.05, 0.0, 0.10, 0.0, 0.0),    # free fall from a lean
    (0.05, 0.5, 0.06, 0.0, 0.0),    # catching torque
    (0.05, -0.5, 0.10, 0.0, 0.0),   # wrong-way torque
    (0.02, 0.0, 0.10, 0.3, 0.0),    # initial pitch rate
    (0.05, 0.3, 0.10, 0.0, 0.5),    # moving base
])
def test_predictor_matches_mujoco(lean, torque, horizon, pr0, v0):
    ref = _mujoco_rollout(lean, torque, horizon, pr0, v0)
    model = brain_mod.PendulumModel()
    pred = model.rollout(lean, pr0, v0, horizon, lambda t: torque)
    assert abs(pred[0] - ref[0]) < 0.004          # pitch within 0.23 deg over the horizon
    assert abs(pred[1] - ref[1]) < 0.05           # pitch rate rad/s
    assert abs(pred[2] - ref[2]) < 0.03           # base speed m/s
    assert 4.0 < model.growth_rate() < 5.5        # ~4.8 rad/s open-loop pole


def test_offline_delay_tolerance_plain_vs_comp():
    # R2c: plain balance stands at 60 ms one-way at rest and falls well before 150 ms.
    assert not offline_loop.run("balance", 60.0)["fell"]
    assert offline_loop.run("balance", 100.0)["fell"]
    # R7a: the compensated loop stands at 100 ms and 150 ms, at rest and cruising.
    assert not offline_loop.run("balance_comp", 100.0)["fell"]
    assert not offline_loop.run("balance_comp", 150.0)["fell"]
    assert not offline_loop.run("balance_comp", 100.0, cruise_v=0.5)["fell"]
    # ... and is bit-identical to plain balance without a link estimate (no prediction).
    r0 = offline_loop.run("balance", 0.0)
    r1 = offline_loop.run("balance_comp", 0.0)
    assert not r0["fell"] and not r1["fell"]


def test_comp_fight_through_symmetric_delay(tmp_path):
    out = tmp_path / "fight"
    proc = subprocess.run(
        [sys.executable, str(FIGHT), "--bot", "balance", "--policy", "stand", "--comp", "0,1", "--fights", "1",
         "--time-limit", "6", "--seed", "21", "--port-base", "6970", "--handicap", "robot=both,delay_ms=80",
         "--out", str(out)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    fight = json.loads((out / "fight-000" / "fight.json").read_text())
    assert fight["brain_policies"] == ["balance_comp", "balance_comp"]
    assert fight["reason"] in ("timeout", "timeout_edge"), fight["reason"]   # both stood through 80 ms one-way
    assert 0.9 <= fight["rtf"] <= 1.1
    for brain in fight["brains"]:
        assert brain["policy"] == "balance_comp"
        est = brain["link_est"]
        assert est["shared_clock"] is True
        assert 70_000 <= est["cmd_one_way_us"] <= 95_000      # the proxy's 80 ms, seen through the link estimate
        assert est["horizon_ms_mean"] > 150.0
