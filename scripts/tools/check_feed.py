#!/usr/bin/env python3
"""Verify that a live broker publishes telemetry for all requested links."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from typing import Sequence

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "web_ui"))
from server import parse_telemetry_frame  # noqa: E402


DEFAULT_LINKS = (
    "gnb0>ue0:sionna_rt",
    "gnb0>ue1:sionna_rt",
    "gnb1>ue0:sionna_rt",
    "gnb1>ue1:sionna_rt",
    "ue0>gnb0:sionna_rt",
    "ue0>gnb1:sionna_rt",
    "ue1>gnb0:sionna_rt",
    "ue1>gnb1:sionna_rt",
)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="tcp://127.0.0.1:5560")
    parser.add_argument("--duration", type=float, default=5.0)
    parser.add_argument("--min-frames-per-link", type=int, default=1)
    parser.add_argument(
        "--links",
        default=",".join(DEFAULT_LINKS),
        help="comma-separated canonical link IDs; empty accepts any link",
    )
    args = parser.parse_args(argv)
    if args.duration <= 0.0 or args.min_frames_per_link <= 0:
        parser.error("duration and min-frames-per-link must be positive")
    args.links = tuple(link for link in args.links.split(",") if link)
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        import zmq  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "pyzmq is required; install apps/sionna_bridge/requirements.txt"
        ) from exc

    context = zmq.Context.instance()
    socket = context.socket(zmq.SUB)
    socket.setsockopt(zmq.LINGER, 0)
    socket.setsockopt_string(zmq.SUBSCRIBE, "")
    socket.connect(args.endpoint)
    poller = zmq.Poller()
    poller.register(socket, zmq.POLLIN)
    counts: dict[str, int] = {}
    invalid_frames = 0
    deadline = time.monotonic() + args.duration
    try:
        while time.monotonic() < deadline:
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000.0))
            events = dict(poller.poll(min(remaining_ms, 250)))
            if events.get(socket) != zmq.POLLIN:
                continue
            try:
                link_id, _payload = parse_telemetry_frame(socket.recv_string())
            except (ValueError, UnicodeDecodeError):
                invalid_frames += 1
                continue
            counts[link_id] = counts.get(link_id, 0) + 1
            if args.links and all(
                counts.get(link_id, 0) >= args.min_frames_per_link
                for link_id in args.links
            ):
                break
    finally:
        socket.close(linger=0)

    missing = [
        link_id
        for link_id in args.links
        if counts.get(link_id, 0) < args.min_frames_per_link
    ]
    result = {
        "event": "telemetry_check",
        "ok": not missing and invalid_frames == 0 and bool(counts),
        "endpoint": args.endpoint,
        "counts": counts,
        "missing": missing,
        "invalid_frames": invalid_frames,
    }
    print(json.dumps(result, separators=(",", ":")), flush=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
