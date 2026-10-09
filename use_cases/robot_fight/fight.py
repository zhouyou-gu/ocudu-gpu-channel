#!/usr/bin/env python3
"""Fight runner: N local fights (arena + two brains on loopback), summary.

Each fight is one arena process and two brain processes with their own
seeds. Fights can run in parallel (--parallel) on distinct port blocks; every
process still runs on the wall clock, so keep the parallelism well under the
core count.

--handicap inserts a UDP proxy on robot 1's path (both directions) that adds a
fixed one-way delay and/or random loss. It is a local smoke test of link
sensitivity, not a measurement of the radio path: over the air (R4) the
proxy is replaced by gNB -> channel emulator -> UE.

Outputs under --out: per-fight arena/brain logs and result JSONs, plus
summary.json and summary.md.
"""

from __future__ import annotations

import argparse
import heapq
import json
import os
import pathlib
import random
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent


@dataclass
class Handicap:
    robot: int = 1
    delay_ms: float = 0.0
    loss: float = 0.0
    outage_ms: float = 0.0        # periodic outage: drop everything for this long ...
    outage_period_ms: float = 0.0  # ... once per period (models bursty starvation, not random loss)
    outage_once_ms: float = 0.0    # single blackout of this length ...
    outage_once_at_ms: float = 0.0  # ... starting this long after the proxy first forwarded (0 = off)
    seed: int = 0


class UdpProxy(threading.Thread):
    """Delay/loss proxy between one brain and its robot socket in the arena.

    brain -> (listen) proxy -> arena_addr ; arena -> proxy (arena-facing socket) -> brain."""

    def __init__(self, listen: tuple[str, int], arena_addr: tuple[str, int], handicap: Handicap) -> None:
        super().__init__(name="udp-proxy", daemon=True)
        self.brain_side = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.brain_side.bind(listen)
        self.arena_side = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.arena_side.bind((listen[0], 0))
        self.arena_addr = arena_addr
        self.handicap = handicap
        self.rng = random.Random(handicap.seed)
        self.brain_addr: tuple[str, int] | None = None
        self.stop = threading.Event()
        self.queue: list[tuple[float, int, socket.socket, tuple[str, int], bytes]] = []
        self.counter = 0
        self.forwarded = 0
        self.dropped = 0
        self.first_forward: float | None = None

    def in_outage(self) -> bool:
        h = self.handicap
        now_ms = time.perf_counter() * 1000.0
        if h.outage_once_ms > 0 and h.outage_once_at_ms > 0 and self.first_forward is not None:
            since = now_ms - self.first_forward * 1000.0
            if h.outage_once_at_ms <= since < h.outage_once_at_ms + h.outage_once_ms:
                return True
        if h.outage_ms <= 0 or h.outage_period_ms <= 0:
            return False
        phase = now_ms % h.outage_period_ms
        return phase < h.outage_ms

    def schedule(self, sock: socket.socket, addr: tuple[str, int], data: bytes) -> None:
        if self.handicap.loss > 0 and self.rng.random() < self.handicap.loss:
            self.dropped += 1
            return
        if self.in_outage():
            self.dropped += 1
            return
        self.counter += 1
        heapq.heappush(self.queue, (time.perf_counter() + self.handicap.delay_ms / 1000.0, self.counter, sock, addr, data))

    def run(self) -> None:
        socks = [self.brain_side, self.arena_side]
        while not self.stop.is_set():
            timeout = 0.005
            if self.queue:
                timeout = max(0.0, min(timeout, self.queue[0][0] - time.perf_counter()))
            readable, _, _ = select.select(socks, [], [], timeout)
            for sock in readable:
                try:
                    data, addr = sock.recvfrom(2048)
                except OSError:
                    continue
                if sock is self.brain_side:
                    self.brain_addr = addr
                    self.schedule(self.arena_side, self.arena_addr, data)
                elif self.brain_addr is not None:
                    self.schedule(self.brain_side, self.brain_addr, data)
            now = time.perf_counter()
            while self.queue and self.queue[0][0] <= now:
                _, _, sock, addr, data = heapq.heappop(self.queue)
                try:
                    sock.sendto(data, addr)
                    self.forwarded += 1
                    if self.first_forward is None:
                        self.first_forward = time.perf_counter()
                except OSError:
                    pass
        self.brain_side.close()
        self.arena_side.close()


def run_fight(args: argparse.Namespace, index: int, seed: int, port_base: int, out: pathlib.Path) -> dict:
    python = sys.executable
    fight_dir = out / f"fight-{index:03d}"
    fight_dir.mkdir(parents=True, exist_ok=True)
    arena_ports = (port_base, port_base + 1)
    pub_port = port_base + 2
    proxies: list[UdpProxy] = []
    handicap = args.handicap_obj
    brain_targets = [("127.0.0.1", arena_ports[0]), ("127.0.0.1", arena_ports[1])]
    if handicap is not None and (handicap.delay_ms > 0 or handicap.loss > 0 or handicap.outage_ms > 0
                                 or handicap.outage_once_ms > 0):
        # robot=-1 ("both"): the same proxy on each robot's path, i.e. a symmetric link premise
        for k, robot in enumerate((0, 1) if handicap.robot < 0 else (handicap.robot,)):
            listen = ("127.0.0.1", port_base + 3 + k)
            proxy = UdpProxy(listen, ("127.0.0.1", arena_ports[robot]),
                             Handicap(robot, handicap.delay_ms, handicap.loss, handicap.outage_ms, handicap.outage_period_ms,
                                      handicap.outage_once_ms, handicap.outage_once_at_ms, seed + robot))
            proxy.start()
            proxies.append(proxy)
            brain_targets[robot] = listen
    arena_cmd = [
        python, str(HERE / "arena.py"), "--seed", str(seed), "--time-limit", str(args.time_limit),
        "--robot-bind", f"ue0=127.0.0.1:{arena_ports[0]},ue1=127.0.0.1:{arena_ports[1]}",
        "--positions-endpoint", f"tcp://127.0.0.1:{pub_port}" if args.publish_positions else "",
        "--stale-policy", args.stale_policy, "--ring-radius", str(args.ring_radius),
        "--state-hz", str(args.state_hz), "--timeout-margin-m", str(args.timeout_margin_m),
        "--wheel-max-rad-s", str(args.wheel_max_rad_s), "--bot", args.bot, "--torque-max", str(args.torque_max),
        "--log", str(fight_dir / "arena.jsonl"), "--result", str(fight_dir / "arena.json"),
    ]
    # Balance bots: the brain runs the balance loop and uses --policy as the strategy on top;
    # robots listed in --comp run the delay-compensated loop (balance_comp) instead.
    brain_policy = args.policy
    brain_params = json.loads(args.brain_params) if args.brain_params else {}
    if args.bot == "balance" and args.policy in ("ram", "reactive", "pusher", "stand", "cruise"):
        brain_policy = "balance"
        brain_params.setdefault("strategy", args.policy)
    brain_params_text = json.dumps(brain_params) if brain_params else None
    brain_policies = [brain_policy, brain_policy]
    for robot in (args.comp or []):
        if brain_policies[robot] != "balance":
            raise SystemExit("--comp needs --bot balance with a strategy policy")
        brain_policies[robot] = "balance_comp"
    modems = []
    sock_dir = None
    if args.modem:
        # Route these robots through modem.py over a unix datagram socket, as the
        # native gate does from inside each UE's network namespace (no namespace
        # here: it measures the relay's own overhead). AF_UNIX paths are limited
        # to 107 bytes, so the sockets live in a short runtime directory.
        sock_dir = pathlib.Path(tempfile.mkdtemp(prefix="rf", dir=os.environ.get("XDG_RUNTIME_DIR") or "/tmp"))
        unix_items = [f"ue{robot}={sock_dir / f'ue{robot}.sock'}" for robot in args.modem]
        arena_cmd += ["--robot-unix", ",".join(unix_items)]
    arena = subprocess.Popen(arena_cmd, stdout=(fight_dir / "arena.out").open("w"), stderr=subprocess.STDOUT)
    time.sleep(0.4)
    for robot in (args.modem or []):
        cmd = [python, str(HERE / "modem.py"), "--robot", f"ue{robot}", "--bind", f"127.0.0.1:{arena_ports[robot]}",
               "--arena-socket", str(sock_dir / f"ue{robot}.sock"), "--status-file", str(fight_dir / f"modem{robot}.jsonl")]
        modems.append(subprocess.Popen(cmd, stdout=(fight_dir / f"modem{robot}.out").open("w"), stderr=subprocess.STDOUT))
    if modems:
        time.sleep(0.2)
    brains = []
    for robot in range(2):
        cmd = [
            python, str(HERE / "brain.py"), "--robot-id", str(robot), "--robot", f"{brain_targets[robot][0]}:{brain_targets[robot][1]}",
            "--rate-hz", str(args.rate_hz), "--ttl-ms", str(args.ttl_ms), "--seed", str(seed * 2 + robot),
            "--policy", brain_policies[robot],
            "--param-jitter", str(args.param_jitter), "--max-seconds", str(args.time_limit + 15),
            "--log", str(fight_dir / f"brain{robot}.jsonl"), "--result", str(fight_dir / f"brain{robot}.json"),
        ]
        if brain_params_text:
            cmd += ["--params", brain_params_text]
        brains.append(subprocess.Popen(cmd, stdout=(fight_dir / f"brain{robot}.out").open("w"), stderr=subprocess.STDOUT))
    arena.wait(timeout=args.time_limit + 60)
    for b in brains:
        try:
            b.wait(timeout=20)
        except subprocess.TimeoutExpired:
            b.kill()
    for m in modems:
        m.terminate()
        try:
            m.wait(timeout=5)
        except subprocess.TimeoutExpired:
            m.kill()
    for proxy in proxies:
        proxy.stop.set()
        proxy.join(timeout=1)
    result = json.loads((fight_dir / "arena.json").read_text())
    result["fight"] = index
    result["brain_policies"] = brain_policies
    result["brains"] = []
    for robot in range(2):
        path = fight_dir / f"brain{robot}.json"
        result["brains"].append(json.loads(path.read_text()) if path.exists() else None)
    if proxies:
        result["proxy"] = {"robot": handicap.robot, "delay_ms": handicap.delay_ms, "loss": handicap.loss,
                           "outage_ms": handicap.outage_ms, "outage_period_ms": handicap.outage_period_ms,
                           "outage_once_ms": handicap.outage_once_ms, "outage_once_at_ms": handicap.outage_once_at_ms,
                           "forwarded": sum(p.forwarded for p in proxies), "dropped": sum(p.dropped for p in proxies)}
    if modems:
        result["modems"] = {}
        for robot in args.modem:
            out_path = fight_dir / f"modem{robot}.out"
            lines = [json.loads(l) for l in out_path.read_text().splitlines() if l.startswith("{")] if out_path.exists() else []
            result["modems"][f"ue{robot}"] = lines[-1] if lines else None
        shutil.rmtree(sock_dir, ignore_errors=True)
    (fight_dir / "fight.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def summarize(results: list[dict], args: argparse.Namespace) -> dict:
    n = len(results)
    wins = [sum(1 for r in results if r["winner"] == i) for i in range(2)]
    draws = sum(1 for r in results if r["winner"] is None)
    ttw = [r["sim_time_s"] for r in results if r["winner"] is not None]
    rtf = [r["rtf"] for r in results]
    late = [r["late_loops"] for r in results]
    reasons: dict[str, int] = {}
    loser_reasons = [dict(), dict()]   # how each robot lost (fall vs ring_out vs timeout_edge)
    for r in results:
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
        if r["winner"] is not None:
            loser = 1 - r["winner"]
            loser_reasons[loser][r["reason"]] = loser_reasons[loser].get(r["reason"], 0) + 1

    def robot_stat(i: int, key: str):
        vals = [r["robots"][i][key] for r in results]
        return {"mean": float(np.mean(vals)), "max": float(np.max(vals))}

    def brain_rtt(i: int, q: str):
        vals = [r["brains"][i]["rtt_us"][q] for r in results if r["brains"][i]]
        return float(np.median(vals)) / 1000.0 if vals else None

    def brain_stat(i: int, key: str):
        vals = [r["brains"][i][key] for r in results if r["brains"][i]]
        return float(np.mean(vals)) if vals else None

    # 95% Wilson interval for robot 0's win share among decided fights
    decided = wins[0] + wins[1]
    ci = None
    if decided:
        p = wins[0] / decided
        z = 1.96
        denom = 1 + z * z / decided
        centre = (p + z * z / (2 * decided)) / denom
        half = z * ((p * (1 - p) / decided + z * z / (4 * decided * decided)) ** 0.5) / denom
        ci = [round(centre - half, 3), round(centre + half, 3)]
    return {
        "fights": n, "time_limit_s": args.time_limit, "rate_hz": args.rate_hz, "ttl_ms": args.ttl_ms,
        "state_hz": args.state_hz, "policy": args.policy, "bot": args.bot, "comp": args.comp,
        "brain_policies": results[0].get("brain_policies") if results and results[0] else None,
        "timeout_margin_m": args.timeout_margin_m,
        "stale_policy": args.stale_policy, "handicap": args.handicap, "modem": args.modem, "parallel": args.parallel,
        "wins": {"ue0": wins[0], "ue1": wins[1]}, "draws": draws, "reasons": reasons,
        "lost_by": {"ue0": loser_reasons[0], "ue1": loser_reasons[1]},
        "ue0_win_share_of_decided": None if not decided else round(wins[0] / decided, 3),
        "ue0_win_share_ci95": ci,
        "time_to_win_s": None if not ttw else {"mean": round(float(np.mean(ttw)), 2), "min": round(min(ttw), 2), "max": round(max(ttw), 2)},
        "rtf": {"mean": round(float(np.mean(rtf)), 4), "min": round(float(np.min(rtf)), 4)},
        "late_loops": {"mean": round(float(np.mean(late)), 1), "max": int(np.max(late))},
        "robots": [{
            "node": f"ue{i}",
            "stale_intervals": robot_stat(i, "stale_intervals"),
            "stale_total_s": robot_stat(i, "stale_total_s"),
            "cmd_seq_gaps": robot_stat(i, "cmd_seq_gaps"),
            "cmd_one_way_ms_p50_median": round(float(np.median([r["robots"][i]["one_way_us"]["p50"] for r in results])) / 1000.0, 3),
            "brain_rtt_ms_p50_median": brain_rtt(i, "p50"),
            "brain_rtt_ms_p99_median": brain_rtt(i, "p99"),
            "brain_deadline_misses_mean": brain_stat(i, "deadline_misses"),
            "brain_state_gaps_mean": brain_stat(i, "state_seq_gaps"),
        } for i in range(2)],
    }


def summary_markdown(s: dict) -> str:
    r0, r1 = s["robots"]
    ttw = s["time_to_win_s"]
    lost_by = " / ".join(f"{k}: " + ",".join(f"{r}={c}" for r, c in v.items()) for k, v in s["lost_by"].items())
    lines = [
        f"| bot (brains) | fights | ue0 wins | ue1 wins | draws | ue0 share (95% CI) | time-to-win s | RTF mean/min | late loops mean/max | lost by |",
        f"|---|---|---|---|---|---|---|---|---|---|",
        f"| {s['bot']} ({'/'.join(s['brain_policies'] or [])}) | {s['fights']} | {s['wins']['ue0']} | {s['wins']['ue1']} | {s['draws']} | "
        f"{s['ue0_win_share_of_decided']} {s['ue0_win_share_ci95']} | "
        f"{'-' if not ttw else f'{ttw['mean']} ({ttw['min']}-{ttw['max']})'} | "
        f"{s['rtf']['mean']}/{s['rtf']['min']} | {s['late_loops']['mean']}/{s['late_loops']['max']} | {lost_by} |",
        "",
        "| robot | cmd one-way p50 ms | brain RTT p50/p99 ms | stale intervals mean | stale s mean | cmd gaps mean | brain deadline misses |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in (r0, r1):
        lines.append(f"| {r['node']} | {r['cmd_one_way_ms_p50_median']} | {r['brain_rtt_ms_p50_median']}/{r['brain_rtt_ms_p99_median']} | "
                     f"{r['stale_intervals']['mean']:.1f} | {r['stale_total_s']['mean']:.2f} | {r['cmd_seq_gaps']['mean']:.1f} | {r['brain_deadline_misses_mean']} |")
    return "\n".join(lines) + "\n"


def parse_handicap(text: str | None) -> Handicap | None:
    if not text:
        return None
    h = Handicap()
    for item in text.split(","):
        key, _, value = item.partition("=")
        key = key.strip()
        if key == "robot":
            h.robot = -1 if value.strip() == "both" else int(value)
        elif key == "delay_ms":
            h.delay_ms = float(value)
        elif key == "loss":
            h.loss = float(value)
        elif key == "outage_ms":
            h.outage_ms = float(value)
        elif key == "outage_period_ms":
            h.outage_period_ms = float(value)
        elif key == "outage_once_ms":
            h.outage_once_ms = float(value)
        elif key == "outage_once_at_ms":
            h.outage_once_at_ms = float(value)
        else:
            raise SystemExit(f"unknown handicap key {key!r} (robot=, delay_ms=, loss=, outage_ms=, outage_period_ms=, "
                             "outage_once_ms=, outage_once_at_ms=)")
    return h


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--fights", type=int, default=10)
    p.add_argument("--seed", type=int, default=1, help="first seed; fight i uses seed+i")
    p.add_argument("--time-limit", type=float, default=30.0)
    p.add_argument("--rate-hz", type=float, default=100.0, help="brain tick rate")
    p.add_argument("--state-hz", type=float, default=200.0, help="arena STATE rate")
    p.add_argument("--ttl-ms", type=int, default=60)
    p.add_argument("--policy", default=None,
                   help="brain policy for both robots (reactive, pusher, module:callable; balance bots: ram (default), "
                        "reactive, pusher, stand as the strategy on top of the balance loop)")
    p.add_argument("--stale-policy", choices=("coast", "zero", "hold"), default="coast")
    p.add_argument("--timeout-margin-m", type=float, default=0.05)
    p.add_argument("--modem", type=lambda s: [int(x) for x in s.split(",") if x.strip()], default=None,
                   help="route these robots (e.g. 1 or 0,1) through modem.py over a unix socket")
    p.add_argument("--ring-radius", type=float, default=2.0)
    p.add_argument("--bot", choices=("sumo", "balance"), default="sumo",
                   help="balance = two-wheeled inverted pendulum balanced by the brain over the link; "
                        "--policy then names the strategy on top (reactive/pusher/stand)")
    p.add_argument("--torque-max", type=float, default=1.5, help="balance bot wheel torque limit, N m")
    p.add_argument("--comp", type=lambda s: [int(x) for x in s.split(",") if x.strip()], default=None,
                   help="balance bots: these robots (e.g. 0 or 0,1) use the delay-compensated loop (balance_comp)")
    p.add_argument("--wheel-max-rad-s", type=float, default=30.0)
    p.add_argument("--param-jitter", type=float, default=0.1)
    p.add_argument("--brain-params", default=None, help="JSON overrides passed to both brains")
    p.add_argument("--handicap", default=None,
                   help="robot=1|0|both,delay_ms=50,loss=0.1,outage_ms=60,outage_period_ms=500,outage_once_ms=200,outage_once_at_ms=3000 "
                        "(one-way delay each direction; outage = periodic total blackout, the shape of a starving UE; "
                        "outage_once = a single blackout that long, that many ms after the first forwarded datagram)")
    p.add_argument("--parallel", type=int, default=1)
    p.add_argument("--port-base", type=int, default=6100)
    p.add_argument("--publish-positions", action="store_true", help="also bind the position PUB per fight")
    p.add_argument("--out", default="results/robot-fight")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.policy is None:
        args.policy = "ram" if args.bot == "balance" else "reactive"
    args.handicap_obj = parse_handicap(args.handicap)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results: list[dict] = [None] * args.fights  # type: ignore[list-item]
    lock = threading.Lock()
    pending = list(range(args.fights))

    def worker(slot: int) -> None:
        while True:
            with lock:
                if not pending:
                    return
                i = pending.pop(0)
            r = run_fight(args, i, args.seed + i, args.port_base + 10 * slot, out)
            with lock:
                results[i] = r
                print(json.dumps({"fight": i, "seed": args.seed + i, "winner": r["winner"], "reason": r["reason"],
                                  "t": r["sim_time_s"], "rtf": r["rtf"], "late": r["late_loops"]}), flush=True)

    threads = [threading.Thread(target=worker, args=(slot,)) for slot in range(max(1, args.parallel))]
    for t in threads:
        t.start()
        time.sleep(0.2)
    for t in threads:
        t.join()
    summary = summarize(results, args)
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    md = summary_markdown(summary)
    (out / "summary.md").write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
