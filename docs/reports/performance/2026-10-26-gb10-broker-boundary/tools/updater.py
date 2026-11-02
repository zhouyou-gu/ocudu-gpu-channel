#!/usr/bin/env python3
"""Synthetic Sionna-rate channel updates: every 1/rate s, one matrix_profile_swap per link, each in its own
batch_begin / swap / batch_commit like replay_bridge.commit_one. Prints ok/rejected counts at the end."""
import argparse, json, math, random, sys, time
import zmq
ap = argparse.ArgumentParser()
ap.add_argument("--endpoint", required=True); ap.add_argument("--ues", type=int, required=True)
ap.add_argument("--model", default="tdl"); ap.add_argument("--rate-hz", type=float, default=10.0)
ap.add_argument("--duration", type=float, default=20.0); ap.add_argument("--taps", type=int, default=8)
ap.add_argument("--srate-hz", type=int, default=23040000); ap.add_argument("--gnb", default="gnb0"); ap.add_argument("--ue-prefix", default="ue")
a = ap.parse_args()
links = [f"{a.gnb}>{a.ue_prefix}{u}:{a.model}" for u in range(a.ues)] + [f"{a.ue_prefix}{u}>{a.gnb}:{a.model}" for u in range(a.ues)]
ctx = zmq.Context.instance(); s = ctx.socket(zmq.REQ); s.setsockopt(zmq.LINGER, 0); s.setsockopt(zmq.RCVTIMEO, 2000); s.setsockopt(zmq.SNDTIMEO, 2000); s.connect(a.endpoint)
rnd = random.Random(1); ok = rej = err = 0; k = 0; span = 2.5e-6 * a.srate_hz
def req(m):
    s.send_string(json.dumps(m, separators=(",", ":"))); return json.loads(s.recv_string())
t0 = time.monotonic(); period = 1.0 / a.rate_hz; lat = []
while time.monotonic() - t0 < a.duration:
    tick = time.monotonic()
    for li, link in enumerate(links):
        bid = f"u{k:06d}"; k += 1
        taps = [{"delay_samples": round(rnd.uniform(0, span), 3), "gain_db": round(-3 - rnd.uniform(0, 17), 2), "phase_rad": round(rnd.uniform(-math.pi, math.pi), 4)} for _ in range(a.taps)]
        swap = {"type": "matrix_profile_swap", "batch_id": bid, "link_id": link, "nr": 1, "nt": 1, "lanes": [{"rx_port": 0, "tx_port": 0, "taps": taps}]}
        try:
            t1 = time.monotonic()
            r0 = req({"type": "batch_begin", "id": bid}); r1 = req(swap); r2 = req({"type": "batch_commit", "id": bid})
            lat.append(time.monotonic() - t1)
            if r0.get("ok") and r1.get("ok") and r2.get("ok"): ok += 1
            else:
                rej += 1
                if rej <= 3: print("rejected", r0, str(r1)[:200], r2, file=sys.stderr)
        except Exception as e:
            err += 1
            if err <= 3: print("error", e, file=sys.stderr)
            try: req({"type": "batch_abort", "id": bid})
            except Exception: pass
    nxt = tick + period; now = time.monotonic()
    if nxt > now: time.sleep(nxt - now)
lat.sort()
print(f"updates ok={ok} rejected={rej} errors={err} links={len(links)} rate_hz={a.rate_hz} "
      f"commit_ms_p50={1000*lat[len(lat)//2]:.2f} commit_ms_p99={1000*lat[int(len(lat)*0.99)]:.2f}" if lat else f"updates ok={ok} rejected={rej} errors={err}")
