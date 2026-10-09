#!/usr/bin/env python3
"""UDP echo probe through the emulated link, in place of the arena (R7-prep).

The robot-fight gate's side-process hooks start one `server` in the stack
namespace (bound to the UPF-side gateway, 10.45.1.1) and one `client` per UE
inside that UE's network namespace (bound to its tun address), so the probe
packets take exactly the path the brain <-> robot control traffic takes:
core -> gNB -> broker (Sionna channel) -> srsUE -> tun and back. Server and
clients share the host clock, so each reply carries every timestamp needed
for the one-way delays of both directions:

    client send ------- uplink ------> server rx
    client rx   <------ downlink ----- server tx (echo)

Every request is `--size` bytes (100 by default: header + zero padding), sent
at `--rate-hz`. The client logs one JSON line per answered request
(`ul_us`, `dl_us`, `rtt_us`) and one per lost request (no reply within
`--reply-timeout-s`), plus a per-second summary; the server logs a per-second
summary per client. Optional background load shares the same link: the client
floods the server's sink port (uplink load) and/or asks the server to flood its
sink port (downlink load), in 1,200-byte datagrams, so the control packets
compete with real traffic for the same cell.

`analyze` turns a run's `probe-*.jsonl` files into the table R7-prep needs:
per UE, per direction p50/p90/p99, loss, and outages (runs of consecutive
losses or replies later than `--outage-ms`), split into the attach window and
steady state.

Usage as the gate expands the hooks (see the R7-prep record):
  ROOT_EXEC: python3 robot-fight-probe.py server --bind {ue_gateway}:7000 \
                 --log {log_dir}/probe-server.jsonl [--load-dl-mbps X]
  UE_EXEC:   python3 robot-fight-probe.py client --robot {ue_id} \
                 --bind {ue_ip}:0 --server {ue_gateway}:7000 \
                 --log {log_dir}/probe-{ue_id}.jsonl [--load-ul-mbps X]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import select
import signal
import socket
import struct
import sys
import threading
import time
from collections import defaultdict

MAGIC = b"RFPR"
VERSION = 1
KIND_REQUEST = 1
KIND_REPLY = 2
KIND_LOAD_REQUEST = 3   # client -> server: "flood my sink port at this rate"
KIND_LOAD = 4           # filler datagrams, both directions
# magic, version, kind, robot index, seq, client send ns, server rx ns, server tx ns
HEADER = struct.Struct("<4sBBHIqqq")
LOAD_SIZE = 1200
SINK_PORT_OFFSET = 1


def now_ns() -> int:
    return time.time_ns()


def parse_endpoint(text: str) -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        raise argparse.ArgumentTypeError(f"expected host:port, got {text!r}")
    return host, int(port)


class JsonlLog:
    def __init__(self, path: pathlib.Path | None) -> None:
        self.path = path
        self.handle = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = open(path, "a", buffering=1, encoding="utf-8")
        self.lock = threading.Lock()

    def write(self, record: dict) -> None:
        line = json.dumps(record, separators=(",", ":"))
        with self.lock:
            if self.handle is not None:
                self.handle.write(line + "\n")
            else:
                print(line, flush=True)

    def close(self) -> None:
        if self.handle is not None:
            self.handle.close()


def pad(payload: bytes, size: int) -> bytes:
    if len(payload) >= size:
        return payload
    return payload + bytes(size - len(payload))


# --- load generator -----------------------------------------------------------

class Flood(threading.Thread):
    """Send `LOAD_SIZE`-byte datagrams to one destination at a fixed bit rate."""

    def __init__(self, sock: socket.socket, destination: tuple[str, int], mbps: float, robot: int) -> None:
        super().__init__(daemon=True)
        self.sock = sock
        self.destination = destination
        self.mbps = mbps
        self.robot = robot
        self.stop = threading.Event()
        self.sent = 0

    def run(self) -> None:
        if self.mbps <= 0.0:
            return
        interval = LOAD_SIZE * 8.0 / (self.mbps * 1e6)
        payload = pad(HEADER.pack(MAGIC, VERSION, KIND_LOAD, self.robot, 0, 0, 0, 0), LOAD_SIZE)
        next_at = time.monotonic()
        while not self.stop.is_set():
            next_at += interval
            try:
                self.sock.sendto(payload, self.destination)
                self.sent += 1
            except OSError:
                pass
            delay = next_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif delay < -1.0:
                next_at = time.monotonic()


# --- server ---------------------------------------------------------------------

def run_server(args: argparse.Namespace) -> int:
    log = JsonlLog(args.log)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(args.bind)
    sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sink.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sink.bind((args.bind[0], args.bind[1] + SINK_PORT_OFFSET))
    sink.setblocking(False)
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    log.write({"event": "server_start", "t_unix_ns": now_ns(), "bind": list(args.bind),
               "sink_port": args.bind[1] + SINK_PORT_OFFSET, "load_dl_mbps": args.load_dl_mbps})
    per_client: dict[tuple[str, int], dict] = {}
    floods: dict[tuple[str, int], Flood] = {}
    sink_bytes = 0
    last_summary = time.monotonic()
    sock.setblocking(False)
    while not stopping.is_set():
        readable, _, _ = select.select([sock, sink], [], [], 0.2)
        for ready in readable:
            try:
                while True:
                    data, address = ready.recvfrom(65535)
                    if ready is sink:
                        sink_bytes += len(data)
                        continue
                    t_rx = now_ns()
                    if len(data) < HEADER.size or data[:4] != MAGIC:
                        continue
                    magic, version, kind, robot, seq, t_send, _, _ = HEADER.unpack_from(data)
                    stats = per_client.setdefault(address, {"robot": robot, "rx": 0, "min_seq": seq, "max_seq": seq,
                                                            "bytes": 0})
                    if kind == KIND_REQUEST:
                        stats["rx"] += 1
                        stats["bytes"] += len(data)
                        stats["min_seq"] = min(stats["min_seq"], seq)
                        stats["max_seq"] = max(stats["max_seq"], seq)
                        reply = HEADER.pack(MAGIC, VERSION, KIND_REPLY, robot, seq, t_send, t_rx, now_ns())
                        try:
                            sock.sendto(pad(reply, len(data)), address)
                        except OSError:
                            pass
                        if args.load_dl_mbps > 0.0 and address not in floods:
                            flood = Flood(sink, (address[0], address[1] + SINK_PORT_OFFSET), args.load_dl_mbps, robot)
                            floods[address] = flood
                            flood.start()
                            log.write({"event": "load_dl_start", "t_unix_ns": now_ns(), "robot": robot,
                                       "client": list(address), "mbps": args.load_dl_mbps})
                    elif kind == KIND_LOAD_REQUEST and address not in floods:
                        mbps = seq / 1000.0  # the client encodes kbps in the seq field
                        if mbps > 0.0:
                            flood = Flood(sink, (address[0], address[1] + SINK_PORT_OFFSET), mbps, robot)
                            floods[address] = flood
                            flood.start()
                            log.write({"event": "load_dl_start", "t_unix_ns": now_ns(), "robot": robot,
                                       "client": list(address), "mbps": mbps, "requested_by": "client"})
            except BlockingIOError:
                pass
            except OSError:
                pass
        if time.monotonic() - last_summary >= 1.0:
            last_summary = time.monotonic()
            for address, stats in per_client.items():
                log.write({"event": "server_second", "t_unix_ns": now_ns(), "robot": stats["robot"],
                           "client": list(address), "rx": stats["rx"], "bytes": stats["bytes"],
                           "min_seq": stats["min_seq"], "max_seq": stats["max_seq"],
                           "load_sent": floods[address].sent if address in floods else 0})
                stats["rx"] = 0
                stats["bytes"] = 0
                stats["min_seq"] = stats["max_seq"] + 1
            if sink_bytes:
                log.write({"event": "sink_second", "t_unix_ns": now_ns(), "bytes": sink_bytes})
                sink_bytes = 0
    for flood in floods.values():
        flood.stop.set()
    log.write({"event": "server_stop", "t_unix_ns": now_ns()})
    log.close()
    return 0


# --- client ---------------------------------------------------------------------

def run_client(args: argparse.Namespace) -> int:
    log = JsonlLog(args.log)
    robot_index = int(args.robot[-1]) if args.robot[-1].isdigit() else 0
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(args.bind)
    local = sock.getsockname()
    sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sink.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sink.bind((local[0], local[1] + SINK_PORT_OFFSET))
    sink.setblocking(False)
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    log.write({"event": "client_start", "t_unix_ns": now_ns(), "robot": args.robot, "bind": list(local),
               "server": list(args.server), "rate_hz": args.rate_hz, "size": args.size,
               "reply_timeout_s": args.reply_timeout_s, "load_ul_mbps": args.load_ul_mbps,
               "load_dl_mbps": args.load_dl_mbps})

    pending: dict[int, int] = {}          # seq -> send ns
    pending_lock = threading.Lock()
    second = {"sent": 0, "answered": 0, "lost": 0, "rtt_sum": 0.0, "rtt_max": 0.0, "sink_bytes": 0}
    totals = {"sent": 0, "answered": 0, "lost": 0}
    sock.setblocking(False)

    def receiver() -> None:
        while not stopping.is_set():
            readable, _, _ = select.select([sock, sink], [], [], 0.1)
            for ready in readable:
                try:
                    while True:
                        data, _ = ready.recvfrom(65535)
                        if ready is sink:
                            second["sink_bytes"] += len(data)
                            continue
                        t_rx = now_ns()
                        if len(data) < HEADER.size or data[:4] != MAGIC:
                            continue
                        _, _, kind, _, seq, t_send, t_srx, t_stx = HEADER.unpack_from(data)
                        if kind != KIND_REPLY:
                            continue
                        with pending_lock:
                            sent_ns = pending.pop(seq, None)
                        if sent_ns is None:
                            log.write({"event": "late_reply", "seq": seq, "t_unix_ns": t_rx,
                                       "rtt_us": (t_rx - t_send) / 1000.0})
                            continue
                        rtt = (t_rx - t_send) / 1000.0
                        second["answered"] += 1
                        totals["answered"] += 1
                        second["rtt_sum"] += rtt
                        second["rtt_max"] = max(second["rtt_max"], rtt)
                        log.write({"event": "reply", "seq": seq, "t_send_ns": t_send,
                                   "ul_us": (t_srx - t_send) / 1000.0, "dl_us": (t_rx - t_stx) / 1000.0,
                                   "server_us": (t_stx - t_srx) / 1000.0, "rtt_us": rtt})
                except BlockingIOError:
                    pass
                except OSError:
                    pass

    threading.Thread(target=receiver, daemon=True).start()
    ul_flood = None
    if args.load_ul_mbps > 0.0:
        ul_flood = Flood(sink, (args.server[0], args.server[1] + SINK_PORT_OFFSET), args.load_ul_mbps, robot_index)
        ul_flood.start()
    if args.load_dl_mbps > 0.0:
        request = pad(HEADER.pack(MAGIC, VERSION, KIND_LOAD_REQUEST, robot_index,
                                  int(args.load_dl_mbps * 1000.0), now_ns(), 0, 0), args.size)
        for _ in range(3):
            try:
                sock.sendto(request, args.server)
            except OSError:
                pass
            time.sleep(0.2)

    interval = 1.0 / args.rate_hz
    started = time.monotonic()
    next_send = started
    last_summary = started
    seq = 0
    while not stopping.is_set():
        if args.duration_s > 0.0 and time.monotonic() - started >= args.duration_s:
            break
        now = time.monotonic()
        if now >= next_send:
            seq += 1
            t_send = now_ns()
            packet = pad(HEADER.pack(MAGIC, VERSION, KIND_REQUEST, robot_index, seq, t_send, 0, 0), args.size)
            with pending_lock:
                pending[seq] = t_send
            try:
                sock.sendto(packet, args.server)
                second["sent"] += 1
                totals["sent"] += 1
            except OSError as error:
                log.write({"event": "send_error", "seq": seq, "t_unix_ns": t_send, "error": str(error)})
            next_send += interval
            if now - next_send > 1.0:
                next_send = now + interval
        # expire unanswered requests
        limit = now_ns() - int(args.reply_timeout_s * 1e9)
        with pending_lock:
            expired = [s for s, t in pending.items() if t < limit]
            for s in expired:
                t = pending.pop(s)
                second["lost"] += 1
                totals["lost"] += 1
                log.write({"event": "lost", "seq": s, "t_send_ns": t})
        if now - last_summary >= 1.0:
            last_summary = now
            answered = second["answered"]
            log.write({"event": "client_second", "t_unix_ns": now_ns(), "sent": second["sent"],
                       "answered": answered, "lost": second["lost"],
                       "rtt_mean_us": (second["rtt_sum"] / answered) if answered else None,
                       "rtt_max_us": second["rtt_max"] if answered else None,
                       "sink_bytes": second["sink_bytes"],
                       "load_ul_sent": ul_flood.sent if ul_flood else 0})
            second.update({"sent": 0, "answered": 0, "lost": 0, "rtt_sum": 0.0, "rtt_max": 0.0, "sink_bytes": 0})
        time.sleep(min(interval / 4.0, max(0.0, next_send - time.monotonic())))
    stopping.set()
    if ul_flood is not None:
        ul_flood.stop.set()
    log.write({"event": "client_stop", "t_unix_ns": now_ns(), **totals})
    log.close()
    return 0


# --- analysis -------------------------------------------------------------------

def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(math.ceil(fraction * len(ordered)) - 1)))
    return ordered[index]


def summarise(records: list[dict], outage_ms: float) -> dict:
    """Per-direction percentiles, loss and outages over a list of reply/lost records."""
    replies = [r for r in records if r["event"] == "reply"]
    lost = [r for r in records if r["event"] == "lost"]
    out = {"requests": len(replies) + len(lost), "answered": len(replies), "lost": len(lost),
           "loss_pct": (100.0 * len(lost) / (len(replies) + len(lost))) if (replies or lost) else None}
    for key in ("ul_us", "dl_us", "rtt_us"):
        values = [r[key] for r in replies]
        out[key] = {"p50": percentile(values, 0.50), "p90": percentile(values, 0.90),
                    "p99": percentile(values, 0.99), "max": max(values) if values else None,
                    "mean": (sum(values) / len(values)) if values else None}
    # outages: consecutive (by send time) requests that were lost or answered later than outage_ms
    ordered = sorted(records, key=lambda r: r["t_send_ns"])
    outages = []
    current = None
    for record in ordered:
        bad = record["event"] == "lost" or record.get("rtt_us", 0.0) > outage_ms * 1000.0
        if bad:
            if current is None:
                current = {"start_ns": record["t_send_ns"], "end_ns": record["t_send_ns"], "count": 1}
            else:
                current["end_ns"] = record["t_send_ns"]
                current["count"] += 1
        elif current is not None:
            outages.append(current)
            current = None
    if current is not None:
        outages.append(current)
    # a single slow packet is jitter, not an outage: require >= 3 consecutive (30 ms at 100 Hz)
    outages = [o for o in outages if o["count"] >= 3]
    durations = [(o["end_ns"] - o["start_ns"]) / 1e6 + 10.0 for o in outages]
    out["outages"] = {"count": len(outages), "total_ms": sum(durations), "max_ms": max(durations) if durations else 0.0,
                      "threshold_ms": outage_ms}
    return out


def analyze(args: argparse.Namespace) -> int:
    log_dir = pathlib.Path(args.log_dir)
    per_ue = {}
    for path in sorted(log_dir.glob("probe-ue*.jsonl")):
        ue = path.stem[len("probe-"):]
        records = []
        start_ns = None
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.startswith("{"):
                continue
            record = json.loads(line)
            if record["event"] == "client_start":
                start_ns = record["t_unix_ns"]
            elif record["event"] in ("reply", "lost"):
                records.append(record)
        if not records:
            per_ue[ue] = {"error": "no requests logged"}
            continue
        first = start_ns or min(r["t_send_ns"] for r in records)
        steady_from = first + int(args.steady_after_s * 1e9)
        attach = [r for r in records if r["t_send_ns"] < steady_from]
        steady = [r for r in records if r["t_send_ns"] >= steady_from]
        per_ue[ue] = {"all": summarise(records, args.outage_ms), "attach": summarise(attach, args.outage_ms),
                      "steady": summarise(steady, args.outage_ms),
                      "span_s": (max(r["t_send_ns"] for r in records) - first) / 1e9}
    result = {"log_dir": str(log_dir), "steady_after_s": args.steady_after_s, "ues": per_ue}
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    lines = [f"| UE | window | n | ul p50/p90/p99 ms | dl p50/p90/p99 ms | rtt p50/p90/p99 ms | loss % | outages (n / total ms / max ms) |",
             "|---|---|---|---|---|---|---|---|"]

    def trio(block: dict) -> str:
        if block["p50"] is None:
            return "-"
        return f"{block['p50'] / 1000:.1f} / {block['p90'] / 1000:.1f} / {block['p99'] / 1000:.1f}"

    for ue, summary in per_ue.items():
        if "error" in summary:
            lines.append(f"| {ue} | - | - | {summary['error']} | | | | |")
            continue
        for window in ("steady", "attach"):
            block = summary[window]
            o = block["outages"]
            loss = "-" if block["loss_pct"] is None else f"{block['loss_pct']:.2f}"
            lines.append(f"| {ue} | {window} | {block['requests']} | {trio(block['ul_us'])} | {trio(block['dl_us'])} | "
                         f"{trio(block['rtt_us'])} | {loss} | {o['count']} / {o['total_ms']:.0f} / {o['max_ms']:.0f} |")
    text = "\n".join(lines) + "\n"
    if args.markdown:
        pathlib.Path(args.markdown).write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)
    server = sub.add_parser("server")
    server.add_argument("--bind", type=parse_endpoint, required=True)
    server.add_argument("--log", type=pathlib.Path)
    server.add_argument("--load-dl-mbps", type=float, default=0.0,
                        help="flood every client's sink port at this rate (downlink load)")
    client = sub.add_parser("client")
    client.add_argument("--robot", required=True, help="ue0 / ue1 (the trailing digit is the robot index)")
    client.add_argument("--bind", type=parse_endpoint, required=True, help="the UE tun address (port 0 = any)")
    client.add_argument("--server", type=parse_endpoint, required=True)
    client.add_argument("--rate-hz", type=float, default=100.0)
    client.add_argument("--size", type=int, default=100)
    client.add_argument("--reply-timeout-s", type=float, default=2.0)
    client.add_argument("--duration-s", type=float, default=0.0, help="0 = until SIGTERM")
    client.add_argument("--load-ul-mbps", type=float, default=0.0, help="flood the server's sink port (uplink load)")
    client.add_argument("--load-dl-mbps", type=float, default=0.0, help="ask the server to flood this client")
    client.add_argument("--log", type=pathlib.Path)
    an = sub.add_parser("analyze")
    an.add_argument("--log-dir", required=True)
    an.add_argument("--steady-after-s", type=float, default=20.0)
    an.add_argument("--outage-ms", type=float, default=200.0)
    an.add_argument("--json")
    an.add_argument("--markdown")
    args = parser.parse_args(argv)
    if args.mode == "server":
        return run_server(args)
    if args.mode == "client":
        if args.size < HEADER.size:
            parser.error(f"--size must be >= {HEADER.size}")
        return run_client(args)
    return analyze(args)


if __name__ == "__main__":
    raise SystemExit(main())
