#!/usr/bin/env python3
"""Modem: UDP <-> unix-datagram relay that stands in for a robot's radio.

In the native gate each srsUE's TUN interface lives in its own nested network
namespace, so one arena process cannot bind both UEs' addresses. The modem
runs *inside* a UE namespace, binds the UE's TUN address for the brain, and
hands every datagram to the arena over a filesystem unix datagram socket
(unix sockets are not tied to a network namespace):

    brain --UDP--> [UE netns] modem --unix dgram--> arena (--robot-unix)
    brain <--UDP-- [UE netns] modem <--unix dgram-- arena

Started by the gate as::

    nsenter --net=/run/netns/ue1 -- python modem.py --robot ue0 \\
        --bind 10.45.1.2:6000 --arena-socket <run_dir>/arena/ue0.sock

The modem keeps no protocol knowledge beyond the datagram boundary: bytes in,
bytes out. It learns the brain's address from the first UDP datagram (or takes
--brain) and answers STATE datagrams from the arena to that address. It never
buffers: a datagram is forwarded as soon as it is read, so the relay adds only
two syscalls per direction (measured ~30-60 us, see README).

Stdlib only, so it runs with any python3 inside the namespace.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import pathlib
import select
import signal
import socket
import sys
import time


def parse_hostport(text: str) -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    if not host or not port:
        raise SystemExit(f"expected host:port, got {text!r}")
    return host, int(port)


class Modem:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.arena_path = str(pathlib.Path(args.arena_socket))
        self.modem_path = args.modem_socket or f"{self.arena_path}.{args.robot}.modem"
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.udp.bind(parse_hostport(args.bind))
        self.unix = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            os.unlink(self.modem_path)
        except FileNotFoundError:
            pass
        pathlib.Path(self.modem_path).parent.mkdir(parents=True, exist_ok=True)
        self.unix.bind(self.modem_path)
        self.brain: tuple[str, int] | None = parse_hostport(args.brain) if args.brain else None
        self.brain_fixed = self.brain is not None
        self.to_arena = 0
        self.to_brain = 0
        self.dropped_no_brain = 0
        self.arena_errors = 0
        self.last_arena_error = ""
        self.stop = False
        self.started_us = time.time_ns() // 1000

    def status(self) -> dict:
        return {
            "event": "modem_status", "robot": self.args.robot, "bind": self.args.bind,
            "arena_socket": self.arena_path, "modem_socket": self.modem_path,
            "brain": None if self.brain is None else list(self.brain), "brain_fixed": self.brain_fixed,
            "to_arena": self.to_arena, "to_brain": self.to_brain, "dropped_no_brain": self.dropped_no_brain,
            "arena_errors": self.arena_errors, "last_arena_error": self.last_arena_error,
            "uptime_s": round((time.time_ns() // 1000 - self.started_us) / 1e6, 3),
        }

    def run(self) -> None:
        socks = [self.udp, self.unix]
        next_status = time.monotonic() + self.args.status_interval_s
        status_file = pathlib.Path(self.args.status_file) if self.args.status_file else None
        while not self.stop:
            readable, _, _ = select.select(socks, [], [], 0.2)
            for sock in readable:
                if sock is self.udp:
                    try:
                        data, addr = self.udp.recvfrom(4096)
                    except OSError as exc:
                        if exc.errno in (errno.ECONNREFUSED, errno.EAGAIN):
                            continue
                        raise
                    if not self.brain_fixed and self.brain != addr:
                        self.brain = addr
                    try:
                        self.unix.sendto(data, self.arena_path)
                        self.to_arena += 1
                    except OSError as exc:
                        # Arena not up yet, or gone: the datagram is lost, as it
                        # would be on a radio link with nobody listening.
                        self.arena_errors += 1
                        self.last_arena_error = str(exc)
                else:
                    try:
                        data = self.unix.recv(4096)
                    except OSError:
                        continue
                    if self.brain is None:
                        self.dropped_no_brain += 1
                        continue
                    try:
                        self.udp.sendto(data, self.brain)
                        self.to_brain += 1
                    except OSError:
                        pass
            now = time.monotonic()
            if now >= next_status:
                next_status = now + self.args.status_interval_s
                line = json.dumps(self.status(), separators=(",", ":"))
                if status_file is not None:
                    with status_file.open("a", encoding="utf-8") as fh:
                        fh.write(line + "\n")
                elif self.args.verbose:
                    print(line, flush=True)

    def close(self) -> None:
        self.udp.close()
        self.unix.close()
        try:
            os.unlink(self.modem_path)
        except FileNotFoundError:
            pass


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--robot", required=True, help="robot/node id, e.g. ue0 (names the modem socket)")
    p.add_argument("--bind", required=True, help="UDP host:port the brain sends to (the UE TUN address)")
    p.add_argument("--arena-socket", required=True, help="arena's unix datagram socket path for this robot (--robot-unix)")
    p.add_argument("--modem-socket", default=None, help="this modem's own unix socket path (default <arena-socket>.<robot>.modem)")
    p.add_argument("--brain", default=None, help="fixed brain host:port (default: learn from the first datagram)")
    p.add_argument("--status-file", default=None, help="append a JSON status line every --status-interval-s")
    p.add_argument("--status-interval-s", type=float, default=5.0)
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    modem = Modem(args)

    def on_signal(signum, frame):  # noqa: ARG001
        modem.stop = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    print(json.dumps({**modem.status(), "event": "modem_ready"}), flush=True)
    try:
        modem.run()
    finally:
        print(json.dumps(modem.status()), flush=True)
        modem.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
