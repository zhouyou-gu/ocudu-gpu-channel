"""Wire-format round trips for the robot-fight control and position planes."""

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "use_cases" / "robot_fight"))

import protocol  # noqa: E402


def test_state_round_trip():
    header = protocol.Header(protocol.KIND_STATE, 1, 42, 1_790_000_000_000_000, 7, 1_789_999_999_990_000)
    state = protocol.State(12.5, 0.5, -0.25, 1.5, 0.1, -0.2, 0.3, -1.0, 0.75, -2.0, 0.0, 0.05, 2.5, 1.9, 0.4,
                           0.02, -0.5, 10.0, 11.0, protocol.FLAG_RUNNING)
    data = protocol.pack_state(header, state)
    assert len(data) == protocol.STATE_SIZE
    got_header, got = protocol.unpack(data)
    assert got_header == header
    assert isinstance(got, protocol.State)
    assert got.sim_time_s == pytest.approx(12.5)
    assert got.opp_yaw == pytest.approx(-2.0)
    assert got.pitch == pytest.approx(0.02)
    assert got.wheel_right == pytest.approx(11.0)
    assert got.flags == protocol.FLAG_RUNNING


def test_command_round_trip():
    header = protocol.Header(protocol.KIND_CMD, 0, 1, 100, 0, 0)
    data = protocol.pack_command(header, protocol.Command(12.5, -3.0, 100))
    assert len(data) == protocol.CMD_SIZE
    got_header, got = protocol.unpack(data)
    assert got_header == header
    assert isinstance(got, protocol.Command)
    assert (got.wheel_left, got.wheel_right, got.ttl_ms) == (pytest.approx(12.5), pytest.approx(-3.0), 100)


def test_kind_mismatch_and_malformed():
    with pytest.raises(protocol.ProtocolError):
        protocol.pack_state(protocol.Header(protocol.KIND_CMD, 0, 1, 1, 0, 0), protocol.State(*([0.0] * 19), 0))
    with pytest.raises(protocol.ProtocolError):
        protocol.unpack(b"XX" + bytes(protocol.STATE_SIZE - 2))
    with pytest.raises(protocol.ProtocolError):
        protocol.unpack(bytes(4))
    good = protocol.pack_command(protocol.Header(protocol.KIND_CMD, 0, 1, 1, 0, 0), protocol.Command(0, 0, 50))
    with pytest.raises(protocol.ProtocolError):
        protocol.unpack(good + b"\0")


def test_rtt_from_echo():
    # Brain sent CMD seq 5 at t=1000; the robot echoes it in a STATE received at t=1750.
    header = protocol.Header(protocol.KIND_STATE, 0, 9, 1700, 5, 1000)
    assert protocol.rtt_us(header, 1750) == 750
    assert protocol.rtt_us(protocol.Header(protocol.KIND_STATE, 0, 1, 1700, 0, 0), 1750) is None


def test_positions_message_round_trip():
    msg = protocol.positions_message(1790685008187, {
        "ue0": ((0.5, -1.0, 0.3), (0.1, 0.0, 0.0)),
        "ue1": ((-0.5, 1.0, 0.3), (0.0, -0.2, 0.0)),
    })
    payload = json.loads(msg)
    assert payload["event"] == "positions" and payload["frame"] == "arena"
    assert payload["nodes"]["ue1"]["position_m"] == [-0.5, 1.0, 0.3]
    parsed = protocol.parse_positions_message(msg)
    assert parsed == payload
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_positions_message(json.dumps({"event": "positions", "frame": "scene", "nodes": {}}).encode())
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_positions_message(json.dumps({"event": "positions", "frame": "arena",
                                                     "nodes": {"ue0": {"position_m": [0, 0], "velocity_mps": [0, 0, 0]}}}).encode())
