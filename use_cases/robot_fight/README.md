# robot_fight — sumo arena, brains and fight runner (R2)

Two differential-drive push bots in a circular ring. Each bot's brain is a
separate process that talks to the arena over UDP, so in R4 the brain sits
behind the gNB (N6) and the arena's per-robot socket behind a UE's TUN
interface: the whole control loop then crosses the channel emulator. The arena
also publishes robot positions for the Sionna bridge (R3).

Everything runs on the wall clock and nobody waits for anybody (the
"아무도 기다리지 않는다" invariant of `ROBOT_FIGHT_MILESTONES.md`): a late or
lost command is a physical event, not a stall.

```
brain0 ──UDP CMD──▶ arena:6000 (robot ue0) ──UDP STATE──▶ brain0
brain1 ──UDP CMD──▶ arena:6001 (robot ue1) ──UDP STATE──▶ brain1
arena ──ZMQ PUB tcp://127.0.0.1:5570──▶ Sionna bridge  (positions, 20 Hz)
```

Over the air (R4) each robot's UDP socket is replaced by a *modem* inside the
UE's network namespace that relays to the arena over a unix datagram socket
(see *Running across network namespaces* below).

## Files

| File | Role |
|---|---|
| `protocol.py` | Wire formats: UDP header + STATE/CMD structs, position JSON. Shared with the bridge (`parse_positions_message`). |
| `arena.py` | MuJoCo world + referee + per-robot UDP (or unix) server + position PUB. One process = one fight. |
| `brain.py` | One controller per robot, own loop at `--rate-hz`, `reactive` (default) and `pusher` policies, `--policy module:callable` hook. |
| `modem.py` | UDP ⇄ unix-datagram relay run inside a UE network namespace (stdlib only). |
| `fight.py` | Runs N fights (arena + 2 brains on loopback), optional `--handicap` delay/loss proxy (`robot=both` for a symmetric link) and `--modem` relay, `--comp` picks the delay-compensated balance brain per robot, summary JSON/MD. |
| `offline_loop.py` | Physics + a brain policy through an ideal delay line, no network (threshold probes; the policy is told the true delays). |
| `offline_curves.py` | Survival-vs-delay and vs-blackout tables for `balance` / `balance_comp` (R7a). |
| `../../tests/use_cases/test_robot_fight_protocol.py` | pack/unpack round trips, RTT from echo, position JSON validation. |
| `../../tests/use_cases/test_robot_fight_smoke.py` | one 5 s headless fight, checks RTF ≈ 1 and the streams. |
| `../../tests/use_cases/test_robot_fight_referee.py` | ring-out / fall / timeout tie-break rules on a constructed world. |
| `../../tests/use_cases/test_robot_fight_modem.py` | relay bytes + overhead; the netns case runs when `unshare -n` is allowed (gate container as root). |
| `../../tests/use_cases/test_robot_fight_balance.py` | balance bots stand through a short fight; the offline loop falls at 150 ms one-way delay and not at 15 ms. |

Interpreter: `~/ocudu-work/venvs/robot/bin/python` (mujoco, numpy, pyzmq, pytest).

## Wire formats

### UDP control plane (brain ⇄ robot)

Little-endian. 32-byte header on every datagram:

| field | type | meaning |
|---|---|---|
| magic | `2s` | `RF` |
| version | u8 | 1 |
| kind | u8 | 1 = STATE (robot → brain), 2 = CMD (brain → robot) |
| robot_id | u8 | 0 or 1 |
| seq | u32 | sender's counter per direction, +1 per datagram |
| t_send_us | u64 | sender's unix time in µs at send |
| echo_seq | u32 | seq of the newest datagram received from the peer |
| t_echo_us | u64 | that datagram's `t_send_us` |

`RTT = now − t_echo_us` on either end, no clock agreement needed (the arena
echoes the newest CMD in every STATE; the STATE stream is 100 Hz, so the RTT
sample includes up to one STATE period of quantisation). One-way latency
(`now − t_send_us`) is valid when both ends share a clock (same host, or
the Spark with brains and arena in different network namespaces).

STATE payload (version 2): `sim_time_s f64; x y yaw f32; vx vy wz f32; opp_x opp_y opp_yaw f32;
opp_vx opp_vy f32; ring_radius_m dist_to_edge_m opp_dist_to_edge_m f32;
pitch pitch_rate wheel_left wheel_right f32; flags u32`
(flags: 1 running, 2 over, 4 this robot won, 8 this robot lost). 108 bytes.
`pitch` is the body's lean toward its heading (+ forward); ~0 for a sumo bot.

CMD payload: `wheel_left wheel_right f32; ttl_ms u16; pad`. 44 bytes. Sumo bot:
wheel angular-velocity targets (rad/s); balance bot: wheel torques (N m).

### ZMQ position plane (arena → Sionna bridge)

PUB, JSON, default `tcp://127.0.0.1:5570`, `--pub-hz` 20:

```json
{"event":"positions","t_unix_ms":1790685008187,"frame":"arena",
 "nodes":{"ue0":{"position_m":[x,y,z],"velocity_mps":[vx,vy,vz]},
          "ue1":{"position_m":[x,y,z],"velocity_mps":[vx,vy,vz]}}}
```

`frame` is always `arena`: ring centre at the origin, z up, metres; `z` is the
antenna height (`--antenna-height`, 0.3 m). Node ids come from `--node-ids`
(robot order). The bridge maps arena → scene coordinates.

## Arena

- Ring radius 2.0 m (`--ring-radius`), bots spawn at ±0.5·R on x facing each other with a seeded few-cm jitter.
- Bot: 30×24×8 cm chassis, 4 kg (2 kg of it a low ballast plate), two 6 cm-radius drive wheels
  on the centre axle (velocity actuators, ±30 rad/s → 1.8 m/s), two casters floating 4 mm off
  the floor so the wheels carry the weight, a 3 cm-tall low-friction push plate in front.
- Referee: body centre outside the ring → ring-out; tilt > ~70° → fall; at `--time-limit` the
  robot whose body centre is closer to the edge loses (`timeout_edge`); it is a draw (`timeout`)
  only when the two radial distances differ by less than `--timeout-margin-m` (5 cm). The result
  carries `decided_by` and both `radial_m`.
- **Wall clock:** physics (1 ms steps) is advanced to `sim0 + wall_elapsed` each 1 ms loop; the
  result records `rtf`, `late_loops` (loop found itself > 2 ms behind) and `max_lag_ms`.
  `--lockstep` exists for debugging only and is recorded in the result as `lockstep: true`.
- Commands: newest CMD wins (older seq dropped). A command is fresh for `ttl_ms` after arrival;
  otherwise the robot follows `--stale-policy`: **`coast`** (default, motor driver off — the wheels
  free-wheel and the bot is pushable), `zero` (brake), `hold` (keep the last command).
  Stale intervals are counted from the first fresh command onward.
- Per-fight JSONL (`--log`): `spawn`, `brain_learned`, `first_command`, `cmd` (seq, one-way µs,
  brain turnaround µs), `stale_begin/end`, `pose` (20 Hz, both robots), `result`, `rtf`.
  Result JSON (`--result`): winner, reason, rtf, per-robot command/stale/latency stats.

## Balance bot (`--bot balance`, R2c)

The sumo bot's fights are decided by shove physics, not by the link (R2b).
`--bot balance` replaces it with a two-wheeled **inverted pendulum** (base at
the axle, 2 kg head 0.95 m up, CoM ≈ 0.6 m, wheel torque motors ±1.5 N m)
that has **no local balance controller**: the brain closes the balance loop
over the link at 100 Hz on the STATE fields `pitch`, `pitch_rate`, `wheel_*`
(protocol version 2) and sends wheel *torques* as CMD. Every torque crosses
the link, so link delay eats the loop's phase margin and a stale interval
(`zero`/`coast` both mean torque 0 — a starved link delivers nothing) lets
the pendulum fall freely. Fall = loss, ring-out = loss, timeout → edge
tie-break as before. The arena holds the bots upright ("starter's hand")
until both brains have sent a command (`hold_release_sim_s` in the result).

Brain `--policy balance` = linear full-state feedback
`u = k_pitch·pitch + k_pitch_rate·pitch_rate + k_v·(v − v_ref)` plus a yaw
differential, with a *strategy* on top that supplies `v_ref`/`w_ref`
(`strategy` param: `ram` default — drive into the opponent, turn back near
the edge; `reactive`, `pusher`, `stand`). References are low-passed and slew
limited so the strategy's target jumps cannot demand a fatal lean; balance
torque has priority over steering. `fight.py --bot balance` selects the
balance policy automatically and takes `--policy` as the strategy.

Tolerance of the loop (offline closed loop through a one-way delay, no
network; `tests/use_cases/test_robot_fight_balance.py` pins the ends): stands with
60 ms one-way delay at rest and 45 ms while driving at 0.6 m/s; survives a
300 ms blackout while driving and falls at 400 ms (a blackout while standing
still is harmless up to 400 ms — nothing moves a balanced pendulum). The
radio's ~30 ms RTT (15 ms one-way) sits inside that with a 3–4× margin.

## Delay-compensated balance (`--policy balance_comp`, R7a)

Same balance law and gains as `balance`, run on the state *predicted for the
moment the command will be applied* (Smith-predictor style), so the link's
delay is cancelled up to the model error. Nothing changes on the robot or in
the protocol; the brain only uses the timestamps it already has:

- **Link estimate** (`Brain.update_link`, logged as `link_est` once a second
  and in the result's `link_est`): the windowed minimum RTT over
  `--link-window-s` (0.5 s) strips the STATE-period quantisation; the STATE
  one-way (`recv − t_send`, low-passed) is trusted only when it is consistent
  with the RTT (shared clock — true on loopback and on the Spark, where arena,
  modems and brains share the host clock), otherwise both directions are
  taken as RTT/2. `state_age = now − t_send(STATE)` and
  `cmd_one_way = min RTT − state one-way`.
- **Predictor**: `PendulumModel` — the linearised two-wheeled inverted
  pendulum derived from the arena's MJCF masses and heights (body 3.5 kg, CoM
  0.58 m above the axle, `a11 = m_b + 2m_w + 2I_w/r²`, `a12 = m_b l`,
  `a22 = I_axle`, wheel-hinge damping included; open-loop pole 4.8 rad/s).
  `tests/use_cases/test_robot_fight_comp.py` pins it against MuJoCo: within 0.004 rad of
  pitch over 100 ms with and without torque and while moving.
- **Rollout**: from the STATE in hand, integrate `state_age + cmd_one_way +
  horizon_extra_ms` (1 ms) forward at the arena timestep, applying the torques
  this brain already sent (command history with landing times) — the arena is
  still executing them while this command is in flight — then feed the
  predicted (pitch, pitch rate, speed) to the balance law. The horizon is
  capped at `max_horizon_ms` (500).

Offline thresholds (`offline_curves.py`, exact delays): plain `balance`
falls at 80 ms one-way at rest and 60 ms cruising at 0.5 m/s; `balance_comp`
stands through 200 ms in both cases (the end of the sweep). Blackouts are
unchanged (a pendulum at rest needs nothing; cruising, both fall at a 500 ms
blackout): prediction cancels delay, not missing commands. A timestamped
command-sequence variant would cover that case; it needs a protocol change
and was not built.

`fight.py --bot balance --comp 0` gives robot 0 the compensated brain and
robot 1 the plain one; `--handicap robot=both,delay_ms=60` puts the same
proxy on both robots' paths, so the two brains face an identical link and
only the strategy differs — the R7 benchmark premise.

## Brain

`reactive` (default, 100 Hz, ttl 60 ms, STATE 200 Hz): time-critical sumo.
Equal bots shoving head-on stall, so the fight is decided by who gets the
other's side first and who notices being pushed toward the edge first. Rules
in priority order, each a short reflex window: **escape** (in contact,
being pushed, edge within 0.6 m → break out tangentially for 0.45 s),
**edge guard** (heading outward at speed with less than `v²/2a + 10 cm` of
ring left → brake hard and turn in; a late brain travels delay × speed
farther), **dodge** (opponent charging straight at me from < 0.9 m → side-step
0.3 s), **disengage** (head-on shove > 0.35 s → reverse, switch flank),
**push** (I have its side → drive through its predicted position toward the
edge), else **flank**. Attack targets lead the opponent by its velocity ×
0.12 s, so a stale state aims where it was. The brain result counts the
reflexes fired (`reflexes`).

`pusher`: the R2 policy (50 Hz class), flank-then-push. Head-on pushes between equal bots stall, so the bot
drives to a waypoint beside the opponent (perpendicular to the opponent's
heading, `flank_m` 0.7) and charges into its side once it sees the opponent
≥ `charge_deg` off the opponent's own heading. A push that has lasted
`stall_s` backs off for `backoff_s` and switches flank. Near the edge the bot
will not drive outward at full speed. `--seed` gives ±10 % parameter jitter
(`--param-jitter`) and small heading noise so fights differ.

`--policy module:callable` loads `callable(state, params, rng) -> (left, right)`;
`params["_mem"]` is a per-brain scratch dict. An LLM policy goes there.

Brain result: outcome, ticks, deadline misses, ticks without a fresh STATE,
STATE seq gaps (loss), RTT p50/p90/p99, STATE one-way p50/p99.

## Runner

```
~/ocudu-work/venvs/robot/bin/python use_cases/robot_fight/fight.py \
    --fights 30 --parallel 6 --time-limit 60 --seed 1000 --out results/robot-fight/noise-floor
~/ocudu-work/venvs/robot/bin/python use_cases/robot_fight/fight.py \
    --fights 12 --parallel 6 --handicap robot=1,delay_ms=100,loss=0 --out results/robot-fight/h-d100
```

`--handicap robot=1,delay_ms=D,loss=P` puts a UDP proxy on robot 1's path in
both directions (one-way delay D each way, drop probability P;
`outage_ms=,outage_period_ms=` for a periodic blackout, `outage_once_ms=,outage_once_at_ms=`
for a single one). It is a local smoke test of link sensitivity, not a radio measurement. `--modem 1` routes
robot 1 through `modem.py` over a unix socket (relay overhead check). Each
fight uses a port block `--port-base + 10·slot` (arena 0/1, PUB 2, proxy 3).

## Running across network namespaces (the R4 layout)

In the native multi-UE gate each srsUE's `tun_srsue` (10.45.1.2 for ue0,
10.45.1.3 for ue1) lives in its own nested network namespace
(`/run/netns/ue1`, `/run/netns/ue2`); the UPF side `ogstun` 10.45.1.1, the
gNB, the broker and the Sionna bridge are in the gate's parent namespace.
One arena process cannot bind both TUN addresses, so each robot gets a
**modem**: `modem.py` runs inside the UE namespace, binds the TUN address for
the brain, and relays every datagram to the arena over a filesystem unix
datagram socket (unix sockets are not tied to a network namespace; AF_UNIX
paths must stay under 107 bytes).

```
brain0 (parent ns, 10.45.1.1) ──UDP──▶ 10.45.1.2:6000 [netns ue1] modem ──unix──▶ arena (parent ns)
brain1 (parent ns, 10.45.1.1) ──UDP──▶ 10.45.1.3:6001 [netns ue2] modem ──unix──▶ arena (parent ns)
arena ──ZMQ PUB ipc://<native_root>/run/arena/positions.sock──▶ Sionna bridge (parent ns)
```

Order: arena first (it binds the unix sockets), modems after each UE's PDU
session is up (the TUN address exists only then), brains last.

```
# 1. arena, parent namespace: unix socket per robot, positions PUB on ipc (the bridge only accepts ipc)
python arena.py --robot-unix ue0=$RUN/arena/ue0.sock,ue1=$RUN/arena/ue1.sock \
                --positions-endpoint ipc://$RUN/arena/positions.sock \
                --time-limit 60 --seed 1 --log arena.jsonl --result arena.json
# 2. one modem per UE, inside its namespace, after the UE has its TUN address
nsenter --net=/run/netns/ue1 -- python modem.py --robot ue0 --bind 10.45.1.2:6000 --arena-socket $RUN/arena/ue0.sock
nsenter --net=/run/netns/ue2 -- python modem.py --robot ue1 --bind 10.45.1.3:6001 --arena-socket $RUN/arena/ue1.sock
# 3. brains, parent namespace, bound to the UPF-side address so replies route back over GTP
python brain.py --robot-id 0 --robot 10.45.1.2:6000 --bind 10.45.1.1:0 --seed 1 --result b0.json
python brain.py --robot-id 1 --robot 10.45.1.3:6001 --bind 10.45.1.1:0 --seed 2 --result b1.json
```

The modem learns the brain's address from the first UDP datagram (`--brain`
fixes it); the arena learns the modem's unix path from the first CMD
(`--state-dest` is for UDP robots). Arena, modems and brains all run on the
same host clock, so the one-way fields in the logs are valid over the air.
The relay adds ~10 µs per direction (`test_robot_fight_modem.py`); in a live
fight robot 1 via modem showed +70 µs one-way p50 against a UDP robot, the
rest being the arena's 1 ms poll. `fight.py --modem 1` reproduces that
locally without namespaces. `modem.py` is stdlib-only, so the namespace side
can use any `python3`; the arena and brains need the robot venv (mujoco,
numpy, pyzmq) — on the Spark that venv has to be created first.

A brain exits when a STATE carries the over flag or after `--max-seconds`.


## R7 validation artifacts

The [R7 report](../../docs/robot-fight-r7.md) explains the controller comparison,
radio conditions, local blackout/loss tests and their limits. It links the
machine-readable results and figures. Use the [launch guide](launch/README.md)
for side swaps, fixed-parameter repeats, probe sweeps and artifact collection.

## Native gate (`run-ocudu-robot-fight.sh`)

Two-cell, two-broker robot-fight gate (R5). Run from the repo root:
`bash use_cases/robot_fight/run-ocudu-robot-fight.sh`.

`run-ocudu-robot-fight.sh` is the scheduling battle of
`ROBOT_FIGHT_MILESTONES.md` R5: cell a (`gnb0` PCI 1 <-> `ue0`) and cell b
(`gnb1` PCI 2 <-> `ue1`) are served by **two separate broker processes** on
one GPU, each driven by its own Sionna bridge from one scene
(`use_cases/configs/sionna/scenarios/robot_ring/robot-ring-fight.json`, `gnb1` on `gnb0`'s mast) and one
live position feed. The physical channel is the same by construction; the
knobs decide how each broker is scheduled while something else shares the
GPU. It keeps the multi-UE gate's hooks (`OCUDU_NATIVE_MUE_UE_EXEC`,
`OCUDU_NATIVE_MUE_ROOT_EXEC`), position-feed and noise-floor variables.

| Variable | Default | Meaning |
| --- | --- | --- |
| `OCUDU_NATIVE_RF_BROKER_A_SCHED` / `_B_SCHED` | `plain` | `plain`: own CUDA context, unpinned, default stream, no realtime class. `protected`: MPS client, `runtime.cuda_stream_priority` (`OCUDU_NATIVE_RF_PROTECTED_STREAM_PRIORITY`, `high`), pinned to the profile's broker cores (`OCUDU_NATIVE_RF_PROTECTED_CPUS`) with the profile's `broker_env`; optionally `chrt -f` `OCUDU_NATIVE_RF_PROTECTED_RT_PRIORITY` (`0` = off, the default: with `OCG_BROKER_SPIN=1` the broker's spinning threads starve each other under FIFO, R5a; needs the rlimit, the inner logs `rt_unavailable` if it could not). |
| `OCUDU_NATIVE_RF_CONTENTION` | `none` | `busy`: `ocudu-gpu-hog` (spin kernels of `OCUDU_NATIVE_RF_HOG_KERNEL_US` µs at duty `OCUDU_NATIVE_RF_HOG_DUTY`, an MPS client when `OCUDU_NATIVE_RF_HOG_MPS=1`, then capped to `OCUDU_NATIVE_RF_HOG_SM_PERCENT` of the SMs via `CUDA_MPS_ACTIVE_THREAD_PERCENTAGE` when set). `cudagnb`: both gNBs CUDA-accelerated (`OCUDU_NATIVE_GNB_ACCELERATION`, `all`). `busy+cudagnb`. The two Sionna bridges always share the GPU; they are MPS clients by default (`OCUDU_NATIVE_RF_SIONNA_MPS=0` gives each its own context, which time-slices every broker for the length of each solve). A 2,000 µs hog kernel costs every context outside its own ~2.3 ms per slot whatever the broker does; `OCUDU_NATIVE_RF_HOG_KERNEL_US=200` is the realistic many-small-kernels tenant (CUDA gNB, Sionna), against which the protected broker holds p99 0.86 ms vs 1.87 ms plain (R5a). |
| `OCUDU_NATIVE_RF_HOG_STREAMS` / `_HOG_QUEUE_DEPTH` | `1` / `1` | Kernels the hog keeps queued: S streams × K deep (R5b). Inside the shared MPS context a default-priority kernel from another client lines up behind everything queued, a high-priority stream only behind the running blocks — so the queue, not the kernel length, is what separates a protected broker from a plain one. A broker in its own context pays the time-slice quantum (~2.2 ms/slot) whatever the depth; `200 µs × 2 × 16` is the R5b battle point (protected p99 0.84 ms, plain 2.3 ms, ping 28 vs 65 ms). |
| `OCUDU_NATIVE_RF_CONTENTION_START` | `after-attach` | The hog starts once both UEs have pinged (`immediate`: with the brokers). |
| `OCUDU_NATIVE_RF_MPS` | `auto` | One MPS server for the gate when any process is meant to be a client (`with-cuda-mps.py` re-executes the gate); `on` / `off`. |
| `OCUDU_NATIVE_RF_DURATION_SECONDS` | `300` | Broker `--duration`; attach window 150 s, the rest under contention. |
| `OCUDU_NATIVE_RF_PLAIN_CPUS` | unset | Pin the plain broker too (unpinned by default). |
| `OCUDU_NATIVE_RF_SKIP_CTEST` | `0` | Skip the tree's ctest after the build (repeat runs of one tree). |
| `OCUDU_NATIVE_RF_WEB_UI` | `0` | Start the read-only web UI on cell a's telemetry (with cell a's gNB KPI feed when metrics are on). |
| `OCUDU_NATIVE_GNB_METRICS` | **`1`** (this gate only; every other gate defaults off) | Render both gNBs with `metrics.enable_json` + `remote_control` and start one `gnb-metrics-relay.py` per gNB, exporting `gnb-metrics-{a,b}.sock` in the run dir (`run-parameters.json` → `gnb_metrics`). The Web UI's KPI panel needs `--gnb-metrics-endpoint ws+unix://<that socket>`; `robot-fight-webui-follow.sh` does that for the newest run on 8080 (cell a) / 8081 (cell b). `0` leaves the gNB configs and the process list as before. |
| `OCUDU_NATIVE_GNB_METRICS_PORT_A` / `_B` | `8001` / `8002` | Remote-control ports on the stack loopback; must differ because both gNBs share it. |
| `OCUDU_NATIVE_RF_GNB_CELL_OVERRIDES` | unset | RAN latency knobs applied to BOTH gNBs (R7-prep): `pusch.min_k2=8,pucch.sr_period_ms=40,pucch.min_k1=7,pdsch.max_nof_harq_retxs=1,pusch.max_nof_harq_retxs=1` (any subset; OCUDU defaults 4 / 20 / 4 / 4 / 4). Recorded in `robot-fight-shape.json` → `cell_overrides`. Unset leaves the gNB yaml bit-identical. |

The verdict (`results/reports/ocudu-robot-fight/<ts>/attach-summary.json`)
passes on attach, PCI camping, clean transport counters and broker exit; it
*records* per broker `rx_starvations`, node stalls and `gpu_timings`
percentiles before and under contention, the ping RTT of each UE at attach
and under contention (a 50-packet burst 20 s after the contention starts),
the Sionna feed counters and the hog's rate. Those are the measurement.
