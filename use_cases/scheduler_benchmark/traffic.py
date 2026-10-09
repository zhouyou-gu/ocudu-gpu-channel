#!/usr/bin/env python3
"""Seeded, timestamped UDP traffic for the scheduler benchmark.

Three roles, all driven by the run's benchmark.json:

  dl-send   root namespace. Sends every DL flow of every UE in both cells.
            Each scheduled packet goes to the UE in cell a AND to its twin in
            cell b, back to back, with the order alternating per packet, so
            the two cells are offered the same packets at the same instants.
  ul-send   one UE's namespace. Sends that UE's UL flows to the gateway.
  recv      DL: a UE's namespace, one socket per DL flow port.
            UL: the root namespace, the one UL port.

Packet schedules are a pure function of (seed, cell-local UE index, flow
name), so twins share a schedule and a rerun with the same seed offers the
same packets. Every packet carries its flow id, sequence number and send time
(CLOCK_REALTIME ns); all namespaces share the host clock, so the receiver's
timestamp minus the carried one is the one-way delay.

Logs (append-only, readable while the run is live):
  sender  CSV `t_ns,flow_id,next_seq` every 100 ms per flow (the sent count)
  recv    binary records struct `<HHIqq` = flow_id, 0, seq, tx_ns, rx_ns
"""

from __future__ import annotations

import argparse
import heapq
import json
import math
import os
import pathlib
import random
import selectors
import signal
import socket
import struct
import sys
import threading
import time
from typing import Any, Iterator, Sequence

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import definitions as bench  # noqa: E402

MAGIC = b"SBM1"
HEADER = struct.Struct("<4sHHIq")
RECORD = struct.Struct("<HHIqq")
TICK_SECONDS = 0.1
SOCKET_BUFFER_BYTES = 8 * 1024 * 1024
FLUSH_SECONDS = 0.5


def flow_schedule(seed: int, ue_index: int, flow: dict[str, Any], duration_s: float) -> Iterator[float]:
    """Send offsets (seconds from the start) of one flow; deterministic."""

    rng = random.Random(f"scheduler-benchmark:{seed}:ue{ue_index}:{flow['name']}:traffic")
    size_bits = flow["size_bytes"] * 8
    pattern = flow["pattern"]
    if pattern == "periodic":
        interval = flow["interval_ms"] / 1000.0
        # A seeded phase so periodic flows of different UEs do not align.
        t = rng.uniform(0.0, interval)
        while t < duration_s:
            yield t
            t += interval
        return
    interval = size_bits / (flow["rate_mbps"] * 1e6)
    if pattern == "cbr":
        t = rng.uniform(0.0, interval)
        while t < duration_s:
            yield t
            t += interval
        return
    if pattern == "poisson":
        t = rng.expovariate(1.0 / interval)
        while t < duration_s:
            yield t
            t += rng.expovariate(1.0 / interval)
        return
    if pattern == "onoff":
        t = 0.0
        on = rng.random() < flow["on_mean_s"] / (flow["on_mean_s"] + flow["off_mean_s"])
        while t < duration_s:
            period = rng.expovariate(1.0 / (flow["on_mean_s"] if on else flow["off_mean_s"]))
            end = min(t + period, duration_s)
            if on:
                while t < end:
                    yield t
                    t += interval
            t = max(t, end)
            on = not on
        return
    raise ValueError(f"unknown pattern {pattern!r}")


def load_benchmark(path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def cell_index_of(benchmark: dict[str, Any], ue: str) -> tuple[int, int, dict[str, Any]]:
    for cell_index, cell in enumerate(bench.CELLS):
        meta = benchmark["cells"][cell["name"]]
        for ue_meta in meta["ues"]:
            if ue_meta["device_id"] == ue:
                return cell_index, ue_meta["cell_index"], ue_meta
    raise ValueError(f"{ue} is not in benchmark.json")


def big_buffers(sock: socket.socket, option_force: int, option: int) -> int:
    for opt in (option_force, option):
        try:
            sock.setsockopt(socket.SOL_SOCKET, opt, SOCKET_BUFFER_BYTES)
            break
        except OSError:
            continue
    return sock.getsockopt(socket.SOL_SOCKET, option)


def install_stop_handlers(stop: list[bool]) -> None:
    # Signal handlers can only be installed from the main thread; the tests
    # drive the receiver from a worker thread and stop it with --until-file.
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__(0, True))
        signal.signal(signal.SIGINT, lambda *_: stop.__setitem__(0, True))


def wait_until(unix_ms: int, stop: list[bool]) -> None:
    while not stop[0]:
        remaining = unix_ms / 1000.0 - time.time()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.05))


class TickLog:
    def __init__(self, path: pathlib.Path) -> None:
        self.handle = path.open("a", encoding="utf-8")
        if path.stat().st_size == 0:
            self.handle.write("t_ns,flow_id,next_seq\n")
        self.last = 0.0

    def maybe(self, now: float, seqs: dict[int, int], force: bool = False) -> None:
        if not force and now - self.last < TICK_SECONDS:
            return
        self.last = now
        t_ns = time.time_ns()
        self.handle.write("".join(f"{t_ns},{fid},{seq}\n" for fid, seq in sorted(seqs.items())))
        self.handle.flush()

    def close(self) -> None:
        self.handle.close()


def run_sender(targets: list[dict[str, Any]], seed: int, start_at_ms: int, duration_s: float,
               log_path: pathlib.Path, bind: str | None) -> int:
    """targets: one per (ue_index, flow) with `destinations` [(flow_id, (ip, port)), ...]."""

    stop = [False]
    install_stop_handlers(stop)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sndbuf = big_buffers(sock, getattr(socket, "SO_SNDBUFFORCE", 32), socket.SO_SNDBUF)
    if bind:
        sock.bind((bind, 0))
    heap: list[tuple[float, int, int]] = []
    schedules = []
    for index, target in enumerate(targets):
        schedule = flow_schedule(seed, target["ue_index"], target["flow"], duration_s)
        schedules.append(schedule)
        first = next(schedule, None)
        if first is not None:
            heapq.heappush(heap, (first, index, 0))
    seqs = {fid: 0 for target in targets for fid, _ in target["destinations"]}
    payloads = {index: bytes(target["flow"]["size_bytes"] - HEADER.size) for index, target in enumerate(targets)}
    tick = TickLog(log_path)
    print(json.dumps({"event": "traffic_sender_ready", "flows": len(seqs), "sndbuf": sndbuf,
                      "start_at_unix_ms": start_at_ms, "duration_s": duration_s}), flush=True)
    wait_until(start_at_ms, stop)
    start = start_at_ms / 1000.0
    tick.maybe(0.0, seqs, force=True)
    sent = 0
    errors = 0
    max_lag_s = 0.0
    parity = 0
    while heap and not stop[0]:
        offset, index, _ = heapq.heappop(heap)
        due = start + offset
        now = time.time()
        if due > now:
            time.sleep(due - now)
        else:
            max_lag_s = max(max_lag_s, now - due)
        target = targets[index]
        order = target["destinations"] if parity == 0 else target["destinations"][::-1]
        parity ^= 1
        for fid, address in order:
            packet = HEADER.pack(MAGIC, fid, 0, seqs[fid], time.time_ns()) + payloads[index]
            try:
                sock.sendto(packet, address)
                sent += 1
            except OSError:
                errors += 1
            seqs[fid] += 1
        following = next(schedules[index], None)
        if following is not None:
            heapq.heappush(heap, (following, index, 0))
        tick.maybe(time.time(), seqs)
    tick.maybe(time.time(), seqs, force=True)
    tick.close()
    sock.close()
    print(json.dumps({"event": "traffic_sender_done", "packets": sent, "send_errors": errors,
                      "max_lag_ms": round(max_lag_s * 1000.0, 3), "next_seq": seqs}), flush=True)
    return 0


def dl_targets(benchmark: dict[str, Any]) -> list[dict[str, Any]]:
    """One entry per (cell-local UE index, DL flow); destinations span both cells."""

    profile = benchmark["traffic_profile"]
    targets = []
    for ue_index, ue_profile in enumerate(profile["ues"]):
        for flow_index, flow in enumerate(ue_profile["flows"]):
            if flow["direction"] != "dl":
                continue
            destinations = []
            for cell_index, cell in enumerate(bench.CELLS):
                ue_meta = benchmark["cells"][cell["name"]]["ues"][ue_index]
                port = bench.DL_BASE_PORT + 10 * ue_index + flow_index
                destinations.append((bench.flow_id(cell_index, ue_index, flow_index), (ue_meta["ipv4"], port)))
            targets.append({"ue_index": ue_index, "flow": flow, "destinations": destinations})
    return targets


def ul_targets(benchmark: dict[str, Any], ue: str) -> list[dict[str, Any]]:
    cell_index, ue_index, ue_meta = cell_index_of(benchmark, ue)
    targets = []
    for flow_index, flow in enumerate(ue_meta["flows"]):
        if flow["direction"] != "ul":
            continue
        targets.append({"ue_index": ue_index, "flow": flow, "destinations": [
            (bench.flow_id(cell_index, ue_index, flow_index), (bench.UE_GATEWAY, bench.UL_PORT))]})
    return targets


def udp_rcvbuf_errors() -> int | None:
    try:
        lines = pathlib.Path("/proc/net/snmp").read_text().splitlines()
    except OSError:
        return None
    rows = [line.split() for line in lines if line.startswith("Udp:")]
    if len(rows) < 2:
        return None
    header, values = rows[0], rows[1]
    if "RcvbufErrors" not in header:
        return None
    return int(values[header.index("RcvbufErrors")])


def run_receiver(bind_ip: str, ports: Sequence[int], log_path: pathlib.Path, until_file: pathlib.Path | None) -> int:
    stop = [False]
    install_stop_handlers(stop)
    selector = selectors.DefaultSelector()
    buffers = []
    for port in ports:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        buffers.append(big_buffers(sock, getattr(socket, "SO_RCVBUFFORCE", 33), socket.SO_RCVBUF))
        sock.bind((bind_ip, port))
        sock.setblocking(False)
        selector.register(sock, selectors.EVENT_READ)
    errors_at_start = udp_rcvbuf_errors()
    print(json.dumps({"event": "traffic_receiver_ready", "bind": bind_ip, "ports": list(ports),
                      "rcvbuf": buffers, "udp_rcvbuf_errors": errors_at_start}), flush=True)
    received = 0
    malformed = 0
    pending = bytearray()
    last_flush = time.monotonic()
    with log_path.open("ab") as handle:
        while not stop[0]:
            if until_file is not None and until_file.exists():
                break
            for key, _ in selector.select(timeout=0.2):
                sock = key.fileobj
                while True:
                    try:
                        data = sock.recv(2048)
                    except BlockingIOError:
                        break
                    rx_ns = time.time_ns()
                    if len(data) < HEADER.size:
                        malformed += 1
                        continue
                    magic, fid, _, seq, tx_ns = HEADER.unpack_from(data)
                    if magic != MAGIC:
                        malformed += 1
                        continue
                    pending += RECORD.pack(fid, 0, seq, tx_ns, rx_ns)
                    received += 1
            now = time.monotonic()
            if pending and now - last_flush >= FLUSH_SECONDS:
                handle.write(pending)
                handle.flush()
                pending.clear()
                last_flush = now
        if pending:
            handle.write(pending)
    for key in list(selector.get_map().values()):
        key.fileobj.close()
    selector.close()
    errors_at_end = udp_rcvbuf_errors()
    print(json.dumps({
        "event": "traffic_receiver_done", "packets": received, "malformed": malformed,
        "udp_rcvbuf_errors_delta": (None if errors_at_start is None or errors_at_end is None
                                    else errors_at_end - errors_at_start),
    }), flush=True)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("role", choices=("dl-send", "ul-send", "dl-recv", "ul-recv"))
    parser.add_argument("--benchmark-json", type=pathlib.Path, required=True)
    parser.add_argument("--log", type=pathlib.Path, required=True)
    parser.add_argument("--ue", help="ul-send / dl-recv: the UE this process serves")
    parser.add_argument("--start-at-unix-ms", type=int, help="senders: schedule offset 0")
    parser.add_argument("--duration-s", type=float, help="senders: schedule length")
    parser.add_argument("--until-file", type=pathlib.Path, help="receivers: stop when this file exists")
    args = parser.parse_args(argv)
    benchmark = load_benchmark(args.benchmark_json)
    seed = int(benchmark["seed"])
    if args.role in ("dl-send", "ul-send"):
        if args.start_at_unix_ms is None or not args.duration_s or args.duration_s <= 0:
            parser.error("senders need --start-at-unix-ms and a positive --duration-s")
        if args.role == "dl-send":
            targets = dl_targets(benchmark)
            bind = None
        else:
            if not args.ue:
                parser.error("ul-send needs --ue")
            targets = ul_targets(benchmark, args.ue)
            bind = None
        if not targets:
            print(json.dumps({"event": "traffic_sender_idle", "role": args.role, "ue": args.ue}), flush=True)
            return 0
        return run_sender(targets, seed, args.start_at_unix_ms, args.duration_s, args.log, bind)
    if args.role == "dl-recv":
        if not args.ue:
            parser.error("dl-recv needs --ue")
        _, ue_index, ue_meta = cell_index_of(benchmark, args.ue)
        ports = [bench.DL_BASE_PORT + 10 * ue_index + flow_index
                 for flow_index, flow in enumerate(ue_meta["flows"]) if flow["direction"] == "dl"]
        if not ports:
            print(json.dumps({"event": "traffic_receiver_idle", "ue": args.ue}), flush=True)
            return 0
        return run_receiver("0.0.0.0", ports, args.log, args.until_file)
    return run_receiver(bench.UE_GATEWAY, [bench.UL_PORT], args.log, args.until_file)


if __name__ == "__main__":
    raise SystemExit(main())
