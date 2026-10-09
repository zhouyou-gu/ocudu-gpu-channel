"""modem.py: UDP <-> unix-datagram relay used inside a UE network namespace.

Stdlib only (no mujoco): a fake brain sends CMD-sized datagrams over UDP to
the modem, a fake arena answers over the unix socket, and the test checks the
bytes come through unchanged in both directions and measures the relay's
added latency. When ``unshare -n`` works for the current user (root inside
the native-gate container), the modem and the fake brain are also run inside
a fresh network namespace while the fake arena stays outside, which is the
R4 layout.
"""

import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
MODEM = ROOT / "use_cases" / "robot_fight" / "modem.py"
sys.path.insert(0, str(ROOT / "use_cases" / "robot_fight"))

import protocol  # noqa: E402


def _short_tmpdir() -> pathlib.Path:
    # AF_UNIX paths are limited to 107 bytes.
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return pathlib.Path(tempfile.mkdtemp(prefix="rfm", dir=base))


def _cmd(seq: int) -> bytes:
    header = protocol.Header(protocol.KIND_CMD, 0, seq, protocol.now_us(), 0, 0)
    return protocol.pack_command(header, protocol.Command(1.0, -1.0, 60))


def _state(seq: int) -> bytes:
    header = protocol.Header(protocol.KIND_STATE, 0, seq, protocol.now_us(), 0, 0)
    return protocol.pack_state(header, protocol.State(1.0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 2.0, 2.0, 1.0, 0, 0, 0, 0, protocol.FLAG_RUNNING))


def _wait_ready(proc: subprocess.Popen, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if line and line.startswith("{"):
            record = json.loads(line)
            if record.get("event") == "modem_ready":
                return record
    raise AssertionError("modem did not report ready")


def _relay_round_trips(brain: socket.socket, arena: socket.socket, modem_udp, n: int) -> tuple[int, list[float]]:
    """Send n CMDs brain->modem->arena, answer each with a STATE arena->modem->brain.
    Returns (delivered, per-round-trip seconds)."""
    delivered = 0
    rtts = []
    brain.settimeout(1.0)
    arena.settimeout(1.0)
    for seq in range(1, n + 1):
        payload = _cmd(seq)
        t0 = time.perf_counter()
        brain.sendto(payload, modem_udp)
        data, modem_path = arena.recvfrom(4096)
        assert data == payload
        reply = _state(seq)
        arena.sendto(reply, modem_path)
        got, _ = brain.recvfrom(4096)
        rtts.append(time.perf_counter() - t0)
        assert got == reply
        delivered += 1
    return delivered, rtts


def test_relay_both_directions_and_overhead():
    tmp = _short_tmpdir()
    try:
        arena_path = tmp / "ue0.sock"
        arena = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        arena.bind(str(arena_path))
        modem = subprocess.Popen(
            [sys.executable, str(MODEM), "--robot", "ue0", "--bind", "127.0.0.1:6700", "--arena-socket", str(arena_path)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            ready = _wait_ready(modem)
            assert ready["robot"] == "ue0" and ready["arena_socket"] == str(arena_path)
            brain = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            brain.bind(("127.0.0.1", 0))
            delivered, rtts = _relay_round_trips(brain, arena, ("127.0.0.1", 6700), 500)
            assert delivered == 500
            # Reference: the same round trip over plain UDP loopback (no relay).
            ref = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            ref.bind(("127.0.0.1", 0))
            ref_addr = ref.getsockname()
            ref_rtts = []
            for seq in range(500):
                t0 = time.perf_counter()
                brain.sendto(_cmd(seq), ref_addr)
                data, who = ref.recvfrom(4096)
                ref.sendto(_state(seq), who)
                brain.recvfrom(4096)
                ref_rtts.append(time.perf_counter() - t0)
            rtts.sort()
            ref_rtts.sort()
            relay_p50 = rtts[len(rtts) // 2] * 1e6
            ref_p50 = ref_rtts[len(ref_rtts) // 2] * 1e6
            overhead_us = relay_p50 - ref_p50
            print(f"relay rtt p50 {relay_p50:.0f} us, plain udp p50 {ref_p50:.0f} us, "
                  f"relay overhead {overhead_us:.0f} us round trip")
            # Two hops each way through a python process: well under a millisecond.
            assert overhead_us < 1000
            brain.close()
            ref.close()
        finally:
            modem.terminate()
            modem.wait(timeout=5)
            status = json.loads([l for l in modem.stdout.read().splitlines() if l.startswith("{")][-1])
            assert status["to_arena"] == 500 and status["to_brain"] == 500, status
        arena.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _netns_available() -> bool:
    try:
        return subprocess.run(["unshare", "-n", "true"], capture_output=True, timeout=5).returncode == 0 \
            and shutil.which("ip") is not None
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not _netns_available(), reason="unshare -n needs CAP_SYS_ADMIN (run inside the gate container as root)")
def test_relay_crosses_network_namespace():
    """Modem + fake brain inside a fresh netns (only lo), fake arena outside:
    the unix datagram socket must cross the namespace boundary."""
    tmp = _short_tmpdir()
    try:
        arena_path = tmp / "ue1.sock"
        arena = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        arena.bind(str(arena_path))
        arena.settimeout(10.0)
        inner = f"""
import json, socket, subprocess, sys, time
sys.path.insert(0, {str(ROOT / 'use_cases' / 'robot_fight')!r})
import protocol
subprocess.run(['ip', 'link', 'set', 'lo', 'up'], check=True)
modem = subprocess.Popen([sys.executable, {str(MODEM)!r}, '--robot', 'ue1', '--bind', '127.0.0.1:6001',
                          '--arena-socket', {str(arena_path)!r}], stdout=subprocess.PIPE, text=True)
while True:
    line = modem.stdout.readline()
    if line.startswith('{{') and json.loads(line).get('event') == 'modem_ready':
        break
brain = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
brain.settimeout(5.0)
ok = 0
for seq in range(1, 201):
    h = protocol.Header(protocol.KIND_CMD, 1, seq, protocol.now_us(), 0, 0)
    brain.sendto(protocol.pack_command(h, protocol.Command(seq, -seq, 60)), ('127.0.0.1', 6001))
    data, _ = brain.recvfrom(4096)
    hdr, body = protocol.unpack(data)
    assert hdr.kind == protocol.KIND_STATE and hdr.seq == seq, (hdr, seq)
    ok += 1
modem.terminate(); modem.wait(timeout=5)
print(json.dumps({{'ok': ok}}))
"""
        proc = subprocess.Popen(["unshare", "-n", sys.executable, "-c", inner],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        answered = 0
        try:
            for _ in range(200):
                data, modem_path = arena.recvfrom(4096)
                hdr, body = protocol.unpack(data)
                assert hdr.kind == protocol.KIND_CMD and hdr.robot_id == 1
                assert body.wheel_left == pytest.approx(hdr.seq)
                reply_hdr = protocol.Header(protocol.KIND_STATE, 1, hdr.seq, protocol.now_us(), hdr.seq, hdr.t_send_us)
                arena.sendto(protocol.pack_state(reply_hdr, protocol.State(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2, 2, 2, 0, 0, 0, 0, 1)), modem_path)
                answered += 1
        finally:
            out, err = proc.communicate(timeout=30)
        assert proc.returncode == 0, err
        assert json.loads(out.strip().splitlines()[-1])["ok"] == 200
        assert answered == 200
        arena.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
