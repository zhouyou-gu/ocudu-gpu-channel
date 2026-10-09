"""Referee rules of the arena, exercised on a constructed world (needs mujoco)."""

import math
import pathlib
import sys

import pytest

pytest.importorskip("mujoco")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "use_cases" / "robot_fight"))

import arena as arena_mod  # noqa: E402


@pytest.fixture
def world(tmp_path):
    args = arena_mod.parse_args([
        "--robot-bind", "ue0=127.0.0.1:6810,ue1=127.0.0.1:6811", "--positions-endpoint", "",
        "--time-limit", "0", "--timeout-margin-m", "0.05", "--ring-radius", "2.0",
        "--log", str(tmp_path / "arena.jsonl"), "--result", str(tmp_path / "arena.json"),
    ])
    a = arena_mod.Arena(args)
    a.spawn()
    yield a
    a.close()


def place(a: arena_mod.Arena, i: int, x: float, y: float, yaw: float = 0.0, z: float = arena_mod.WHEEL_RADIUS) -> None:
    q = a.qpos_addr[i]
    a.data.qpos[q:q + 3] = (x, y, z)
    a.data.qpos[q + 3:q + 7] = arena_mod.yaw_to_quat(yaw)
    arena_mod.mujoco.mj_forward(a.model, a.data)


def test_timeout_edge_closer_to_edge_loses(world):
    place(world, 0, 0.2, 0.0)   # near the centre
    place(world, 1, 1.5, 0.0)   # near the edge
    assert world.referee() == (0, "timeout_edge")
    place(world, 0, -1.8, 0.3)
    place(world, 1, 0.0, 0.5)
    assert world.referee() == (1, "timeout_edge")


def test_timeout_draw_within_margin(world):
    place(world, 0, 1.00, 0.0)
    place(world, 1, -1.03, 0.0)   # radial difference 3 cm < 5 cm margin
    assert world.referee() == (None, "timeout")
    place(world, 1, -1.06, 0.0)   # 6 cm > margin
    assert world.referee() == (0, "timeout_edge")


def test_ring_out_beats_timeout(world):
    place(world, 0, 2.05, 0.0)    # outside the 2 m ring
    place(world, 1, 0.0, 0.0)
    assert world.referee() == (1, "ring_out")
    place(world, 1, 0.0, -2.2)
    assert world.referee() == (None, "both_out")


def test_fall(world):
    place(world, 0, 0.0, 0.0)
    place(world, 1, 0.5, 0.0)
    q = world.qpos_addr[1]
    world.data.qpos[q + 3:q + 7] = (math.cos(math.pi / 4), math.sin(math.pi / 4), 0.0, 0.0)  # rolled 90 deg about x
    arena_mod.mujoco.mj_forward(world.model, world.data)
    assert world.referee() == (0, "fall")


def test_parse_binds_mixed():
    binds = arena_mod.parse_binds("ue0=127.0.0.1:6000,ue1=127.0.0.1:6001", ["ue0", "ue1"], "ue1=/tmp/x/ue1.sock")
    assert binds == [("127.0.0.1", 6000), "/tmp/x/ue1.sock"]
    with pytest.raises(SystemExit):
        arena_mod.parse_binds("ue0=127.0.0.1:6000", ["ue0", "ue1"])
