"""Live position feed for the Sionna bridge, exercised without Sionna or pyzmq.

The transport is a fake queue, so these tests cover the frame contract,
latest-wins draining, the offset, the timeout/last-known rule and unknown
node handling — everything an external position publisher relies on.
"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "sionna_bridge"))

from run_bridge import (  # noqa: E402
    DEFAULT_POSITION_TIMEOUT_S,
    ExternalPositionSource,
    Motion,
    parse_args,
    scenario_environment,
)


class FakeTransport:
    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.closed = False

    def publish(self, message: object) -> None:
        self.frames.append(
            message if isinstance(message, bytes) else json.dumps(message).encode()
        )

    def recv_noblock(self) -> bytes | None:
        return self.frames.pop(0) if self.frames else None

    def close(self) -> None:
        self.closed = True


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


MOTION = {
    "gnb0": Motion((30.0, 10.0, 25.0), (0.0, 0.0, 0.0), mobility="fixed"),
    "ue0": Motion((-5.0, 0.0, 1.5), (1.0, 0.0, 0.0), mobility="car"),
    "ue1": Motion((5.0, 0.0, 1.5), (0.0, 0.0, 0.0), mobility="pedestrian"),
}


def make_source(**overrides):
    transport = FakeTransport()
    clock = FakeClock()
    warnings: list[str] = []
    settings = dict(
        offset=(100.0, 200.0, 0.5),
        timeout_s=0.5,
        known_nodes=tuple(MOTION),
        transport=transport,
        clock=clock,
        warn=warnings.append,
    )
    settings.update(overrides)
    return ExternalPositionSource("ipc:///tmp/positions.sock", **settings), transport, clock, warnings


def positions_message(nodes: dict, *, t_unix_ms: int = 1_790_000_000_000, **extra) -> dict:
    return {"event": "positions", "t_unix_ms": t_unix_ms, "frame": "arena", "nodes": nodes, **extra}


class ExternalPositionSourceTests(unittest.TestCase):
    def test_no_message_keeps_scripted_routes_and_reports_pending(self) -> None:
        source, _, _, _ = make_source()
        self.assertIsNone(source.poll())
        positions, velocities, sources = source.resolve(MOTION, 4.0)
        self.assertEqual(positions["ue0"], MOTION["ue0"].position_at(4.0))
        self.assertEqual(velocities["ue0"], MOTION["ue0"].velocity_at(4.0))
        self.assertEqual(set(sources.values()), {"scripted"})
        status = source.status(sources)
        self.assertTrue(status["pending"])
        self.assertFalse(status["stale"])
        self.assertIsNone(status["last_sample_age_ms"])
        self.assertEqual(status["messages_received"], 0)

    def test_live_nodes_get_offset_applied_and_velocity_passed_through(self) -> None:
        source, transport, _, _ = make_source()
        transport.publish(
            positions_message(
                {
                    "ue0": {"position_m": [1.0, -2.0, 0.0], "velocity_mps": [0.3, 0.4, 0.0]},
                    "ue1": {"position_m": [-1.0, 2.0, 0.0]},
                }
            )
        )
        self.assertIsNotNone(source.poll())
        positions, velocities, sources = source.resolve(MOTION, 4.0)
        self.assertEqual(positions["ue0"], (101.0, 198.0, 0.5))
        self.assertEqual(velocities["ue0"], (0.3, 0.4, 0.0))
        self.assertEqual(positions["ue1"], (99.0, 202.0, 0.5))
        # A node without velocity is treated as standing still, not scripted.
        self.assertEqual(velocities["ue1"], (0.0, 0.0, 0.0))
        # The gNB was not in the message: scripted position, unchanged.
        self.assertEqual(positions["gnb0"], MOTION["gnb0"].start)
        self.assertEqual(sources, {"gnb0": "scripted", "ue0": "external", "ue1": "external"})
        status = source.status(sources)
        self.assertFalse(status["pending"])
        self.assertEqual(status["last_sample_t_unix_ms"], 1_790_000_000_000)
        self.assertEqual(status["node_sources"], sources)

    def test_poll_drains_to_the_newest_frame_and_counts_dropped(self) -> None:
        source, transport, _, _ = make_source()
        for x in (1.0, 2.0, 3.0):
            transport.publish(positions_message({"ue0": {"position_m": [x, 0.0, 0.0]}}))
        source.poll()
        positions, _, _ = source.resolve(MOTION, 0.0)
        self.assertEqual(positions["ue0"][0], 103.0)
        self.assertEqual(source.messages_received, 3)
        self.assertEqual(source.messages_dropped, 2)
        self.assertEqual(source.messages_invalid, 0)

    def test_timeout_keeps_last_known_position_and_flags_stale(self) -> None:
        source, transport, clock, _ = make_source(timeout_s=0.5)
        transport.publish(positions_message({"ue0": {"position_m": [1.0, 1.0, 0.0]}}))
        source.poll()
        clock.now += 0.2
        self.assertFalse(source.stale())
        clock.now += 0.4
        self.assertIsNone(source.poll())
        positions, _, sources = source.resolve(MOTION, 9.0)
        self.assertEqual(positions["ue0"], (101.0, 201.0, 0.5))
        self.assertEqual(sources["ue0"], "external")
        status = source.status(sources)
        self.assertTrue(status["stale"])
        self.assertAlmostEqual(status["last_sample_age_ms"], 600.0, places=6)

    def test_unknown_nodes_are_ignored_and_warned_once(self) -> None:
        source, transport, _, warnings = make_source()
        for _ in range(3):
            transport.publish(
                positions_message(
                    {
                        "ue0": {"position_m": [0.0, 0.0, 0.0]},
                        "robotX": {"position_m": [9.0, 9.0, 9.0]},
                    }
                )
            )
        source.poll()
        positions, _, sources = source.resolve(MOTION, 0.0)
        self.assertEqual(sources["ue0"], "external")
        self.assertNotIn("robotX", positions)
        self.assertEqual(source.ignored_nodes, ["robotX"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("robotX", warnings[0])
        self.assertEqual(source.messages_invalid, 0)

    def test_malformed_frames_are_counted_and_do_not_replace_the_last_sample(self) -> None:
        source, transport, _, _ = make_source()
        transport.publish(positions_message({"ue0": {"position_m": [1.0, 1.0, 0.0]}}))
        source.poll()
        transport.publish(b"not json")
        transport.publish({"event": "other", "nodes": {}})
        transport.publish(positions_message({"ue0": {"position_m": [1.0, 1.0]}}))
        transport.publish(positions_message({"ue0": {"position_m": [1.0, 1.0, "nan"]}}))
        transport.publish(positions_message({"ue0": {"position_m": [{}, 1.0, 1.0]}}))
        transport.publish(positions_message({"ue0": {"position_m": [0.0, 0.0, 0.0]}}, frame="scene"))
        self.assertIsNone(source.poll())
        self.assertEqual(source.messages_invalid, 6)
        positions, _, _ = source.resolve(MOTION, 0.0)
        self.assertEqual(positions["ue0"], (101.0, 201.0, 0.5))
        self.assertIsNotNone(source.last_error)

    def test_a_later_frame_may_drop_a_node_back_to_its_previous_live_value(self) -> None:
        # Nodes absent from the newest frame keep the newest frame's view: the
        # feed is latest-wins per frame, so ue1 falls back to scripted here.
        source, transport, _, _ = make_source()
        transport.publish(
            positions_message(
                {"ue0": {"position_m": [1.0, 0.0, 0.0]}, "ue1": {"position_m": [2.0, 0.0, 0.0]}}
            )
        )
        source.poll()
        transport.publish(positions_message({"ue0": {"position_m": [3.0, 0.0, 0.0]}}))
        source.poll()
        positions, _, sources = source.resolve(MOTION, 1.0)
        self.assertEqual(positions["ue0"][0], 103.0)
        self.assertEqual(sources["ue1"], "scripted")
        self.assertEqual(positions["ue1"], MOTION["ue1"].position_at(1.0))

    def test_close_reaches_the_transport(self) -> None:
        source, transport, _, _ = make_source()
        source.close()
        self.assertTrue(transport.closed)


class PositionArgumentTests(unittest.TestCase):
    def test_defaults_keep_scripted_behaviour(self) -> None:
        args = parse_args(["--dry-run"])
        self.assertIsNone(args.position_endpoint)
        self.assertEqual(args.position_frame_offset, (0.0, 0.0, 0.0))
        self.assertEqual(args.position_timeout_s, DEFAULT_POSITION_TIMEOUT_S)
        environment = scenario_environment(args)
        self.assertIsNone(environment["position_endpoint"])
        self.assertEqual(environment["position_frame_offset_m"], [0.0, 0.0, 0.0])

    def test_endpoint_offset_and_timeout_are_recorded(self) -> None:
        args = parse_args(
            [
                "--dry-run",
                "--position-endpoint", "ipc:///run/arena/positions.sock",
                "--position-frame-offset", "12.5,-3,1.5",
                "--position-timeout-s", "0.25",
            ]
        )
        self.assertEqual(args.position_frame_offset, (12.5, -3.0, 1.5))
        environment = scenario_environment(args)
        self.assertEqual(environment["position_endpoint"], "ipc:///run/arena/positions.sock")
        self.assertEqual(environment["position_frame_offset_m"], [12.5, -3.0, 1.5])
        self.assertEqual(environment["position_timeout_s"], 0.25)

    def test_non_positive_timeout_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(["--dry-run", "--position-timeout-s", "0"])


if __name__ == "__main__":
    unittest.main()
