"""Headless smoke test: one short local fight (arena + two brains on loopback).

Runs only when mujoco is importable by the interpreter running pytest (the
robot venv: ``~/ocudu-work/venvs/robot/bin/python -m pytest tests/test_robot_fight_smoke.py``).
"""

import json
import pathlib
import subprocess
import sys

import pytest

pytest.importorskip("mujoco")
pytest.importorskip("zmq")

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIGHT = ROOT / "use_cases" / "robot_fight" / "fight.py"


def test_one_short_fight(tmp_path):
    out = tmp_path / "fight"
    proc = subprocess.run(
        [sys.executable, str(FIGHT), "--fights", "1", "--time-limit", "5", "--seed", "7",
         "--port-base", "6900", "--out", str(out)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    summary = json.loads((out / "summary.json").read_text())
    assert summary["fights"] == 1
    fight = json.loads((out / "fight-000" / "fight.json").read_text())
    assert fight["reason"] in ("timeout", "timeout_edge", "ring_out", "fall", "both_out", "both_fell")
    assert fight["decided_by"] == fight["reason"]
    if fight["reason"] == "timeout_edge":
        # the robot farther from the centre lost
        loser = 1 - fight["winner"]
        assert fight["radial_m"][loser] > fight["radial_m"][fight["winner"]]
    # Wall-clock stepping: the arena must not run faster or slower than real time.
    assert 0.9 <= fight["rtf"] <= 1.1, fight
    assert fight["lockstep"] is False
    for robot in fight["robots"]:
        assert robot["cmds_received"] > 200, robot  # 100 Hz brain over 5 s
        assert robot["transport"] == "udp"
    for brain in fight["brains"]:
        assert brain is not None and brain["states_received"] > 400, brain  # 200 Hz state stream
        assert brain["rtt_us"]["n"] > 0
        assert brain["policy"] == "reactive"
    poses = [json.loads(l) for l in (out / "fight-000" / "arena.jsonl").read_text().splitlines()
             if '"event":"pose"' in l]
    assert len(poses) >= 80  # 20 Hz over 5 s
