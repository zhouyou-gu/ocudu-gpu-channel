# Command-line interfaces

Generated from shipping entrypoint declarations by `scripts/docs/update_cli_reference.py`. Run commands from the repository root unless input paths are absolute. This reference imports no runtime packages.

## Broker defaults and precedence

`--config` is required. `--duration` defaults to zero (run until stopped); durations accept the units parsed by `app_support.h`. Control and telemetry endpoints default to disabled; telemetry requires a control server. `--telemetry-rate-hz` defaults to 20 and `--control-warmup-cap-slots` to 3. Strict real-time and strict hardware checks are opt-in. Wire capture defaults to disabled: provide both directory and sample count, with skip defaulting to zero.

The broker does not overlay environment variables onto YAML. Orchestration scripts may choose files and pass CLI options; their variables belong to the owning guide. Repeated parsed CLI values use the last assignment unless the option is explicitly repeatable. Unknown or incomplete broker arguments exit with status 2; runtime failures exit with status 1.

Usage examples below show syntax, not a universal default for every bracketed value. See [configuration](configuration.md), [control](control-api.md), and [telemetry](telemetry.md) for the contracts.

## ocudu-control-req

[Source](../../apps/ocudu_control_req.cpp)

```text
usage: ocudu-control-req --endpoint tcp://127.0.0.1:5559 --message '<json>'
  Sends one control REQ and prints the reply. Exit 0 on a reply that
  reports ok, 1 otherwise, so a script can gate on it.
```

## ocudu-gpu-channel

[Source](../../apps/ocudu_gpu_channel.cpp)

```text
usage: ocudu-gpu-channel --config topology.yaml [--duration 60s] [--strict-realtime] [--control-endpoint tcp://*:5559] [--telemetry-endpoint tcp://*:5560 --telemetry-rate-hz 20] [--hardware-strict] [--control-warmup-cap-slots N] [--wire-capture-dir DIR --wire-capture-samples N [--wire-capture-skip N]]
```

## ocudu-gpu-channel-bench

[Source](../../apps/ocudu_gpu_channel_bench.cpp)

```text
usage: ocudu-gpu-channel-bench --config topology.yaml [--duration 10s] [--scs-khz 30] [--per-node]
  Drives ChannelProcessor::process_superposition() once per RX node per slot,
  the same call the live broker makes. One fused H2D + kernel + D2H on CUDA
  regardless of edge count.
  Measurement-only environment knobs (S11, live vs bench), all off by default:
    OCG_BENCH_PACE=1     wait for each node's next batch boundary instead of
                         calling back to back (the live cadence, e.g. 1 ms)
    OCG_BENCH_REFRESH=1  rewrite every input batch on the CPU before each call
                         (live inputs are fresh ring data, not a reused buffer)
    OCG_BENCH_THREADS=1  one thread per RX node, concurrently, as the broker does
```

## ocudu-mimo-transport-peer

[Source](../../apps/ocudu_mimo_transport_peer.cpp)

```text
usage: ocudu-mimo-transport-peer [options]
  --tx0-endpoint tcp://*:2101       peer port-0 TX REP bind
  --tx1-endpoint tcp://*:2103       peer port-1 TX REP bind
  --rx0-endpoint tcp://127.0.0.1:2100  peer port-0 RX REQ connect
  --rx1-endpoint tcp://127.0.0.1:2102  peer port-1 RX REQ connect
  --tx0-chunk-samples 23040         independent TX reply size
  --tx1-chunk-samples 23040         independent TX reply size
  --tx0-reply-delay-us 0            delay after a port-0 pull
  --tx1-reply-delay-us 0            delay after a port-1 pull
  --rx0-request-offset-us 0         port-0 request offset per group
  --rx1-request-offset-us 0         port-1 request offset per group
  --duration 30s                    zero is not accepted
  --summary-json PATH               write a machine-readable result
  --self-test                       run an in-process four-socket oracle

The peer owns two TX REP and two RX REQ sockets. It validates only
raw cf32 multi-port transport and grouped continuity; it does not
decode a UE, layers, rank, or modulation.
```

## ocudu-zmq-sink

[Source](../../apps/ocudu_zmq_sink.cpp)

```text
usage: ocudu-zmq-sink --endpoint tcp://127.0.0.1:2001 [--duration 60s] [--request-interval-us 1000]
```

## ocudu-zmq-source

[Source](../../apps/ocudu_zmq_source.cpp)

```text
usage: ocudu-zmq-source --endpoint tcp://*:2000 [--batch-samples 23040] [--duration 60s]
```

## ocudu-gpu-hog

[Source](../../apps/ocudu_gpu_hog.cu). Controlled GPU contention tool; it does not run a radio.

```text
ocudu-gpu-hog [--duration-s S] [--kernel-us US] [--duty 0..1] [--blocks N] [--threads N] [--streams S] [--queue-depth K]
```

## ocudu-sionna-bridge

[Source](../../apps/sionna_bridge/run_bridge.py). Python declaration defaults are shown below; scenario overrides are described in [configuration](configuration.md#scenario-paths).

| Option | Default | Meaning |
|---|---|---|
| `--control-endpoint` | `'tcp://127.0.0.1:5559'` | See option name and source declaration. |
| `--fanout-control-endpoint` | `[]` | send every solved batch to this broker as well, renaming node ids in the link ids (repeatable). All endpoints, including --control-endpoint, receive the batch concurrently |
| `--timeline` | `'wallclock'` | wallclock: nodes move with elapsed wall time (default). grid: the scenario time of update k is k/update_hz, so a seed reproduces the same channel sequence Choices: ('wallclock', 'grid'). |
| `--hold-until-file` | `None` | grid timeline only: solve and send scenario time 0 once, hold it until this file exists, then anchor grid point 0 to that instant |
| `--profile-digest` | `False` | record the SHA-256 of every sent profile batch in the status record |
| `--scenario-config` | `None` | JSON node/link/antenna configuration; overrides --layout motion and arrays |
| `--layout` | `'2x2'` | emulator graph driven by Sionna RT (default: 2x2) Choices: ('2x2', '1x1'). |
| `--scene` | `'sionna_simple_test'` | scene XML path, a directory name under use_cases/configs/sionna/scenes, or a built-in Sionna scene |
| `--duration` | `30.0` | wall-clock seconds; 0 runs until interrupted |
| `--iterations` | `0` | optional update-count cap; 0 means no cap |
| `--update-hz` | `500.0` | See option name and source declaration. |
| `--profile-timing` | `False` | record host wall-time stages of channel generation; no extra GPU synchronization |
| `--sample-rate-hz` | `23040000.0` | See option name and source declaration. |
| `--downlink-frequency-hz` | `1842500000.0` | Sionna carrier for gNB-to-UE links (default: band-3 DL) |
| `--uplink-frequency-hz` | `1747500000.0` | Sionna carrier for UE-to-gNB links (default: band-3 UL) |
| `--carrier-frequency-hz` | `None` | compatibility/TDD override: use one carrier for both directions |
| `--gain-offset-db` | `60.0` | fixed Sionna-field to normalized-IQ calibration |
| `--max-depth` | `3` | See option name and source declaration. |
| `--samples-per-src` | `200000` | See option name and source declaration. |
| `--seed` | `42` | See option name and source declaration. |
| `--path-polylines` | `6` | per link, publish the interaction points of the N strongest rays so the web UI can draw real multipath. UI-only: never part of a control message. 0 disables it, which also skips the component build Sionna does on first access to paths.vertices. |
| `--simple-road`, `--no-simple-road` | `True` | split the built-in floor into a visible road and two ground strips |
| `--road-width-m` | `14.0` | See option name and source declaration. |
| `--gnb-height-m` | `60.0` | shared gNB antenna height; default clears the 51 m rooftops |
| `--ue0-start` | `DEFAULT_MOTION['ue0'].start` | See option name and source declaration. |
| `--ue1-start` | `DEFAULT_MOTION['ue1'].start` | See option name and source declaration. |
| `--ue0-velocity` | `DEFAULT_MOTION['ue0'].velocity` | See option name and source declaration. |
| `--ue1-velocity` | `DEFAULT_MOTION['ue1'].velocity` | See option name and source declaration. |
| `--ue0-route-x` | `(-82.0, 82.0)` | car ping-pong route bounds in meters: min,max |
| `--ue1-route-x` | `(-82.0, 82.0)` | pedestrian ping-pong route bounds in meters: min,max |
| `--status-jsonl` | `None` | append full per-update channel status as JSON Lines |
| `--position-endpoint` | `None` | f'ZMQ PUB endpoint to subscribe to for live node positions ({POSITION_MESSAGE_EVENT!r} messages in the arena frame); unset keeps the scripted routes |
| `--position-frame-offset` | `(0.0, 0.0, 0.0)` | arena-frame origin expressed in scene metres, added to every live position |
| `--position-timeout-s` | `1.0` | after this long without a live message the last known positions are kept and the status record flags them stale; the update loop never waits for a message |
| `--dry-run` | `False` | trace and print control JSON without connecting to ZMQ |

Propagation flags use paired `--NAME` / `--no-NAME` forms: `los` and `specular-reflection` default to true; `diffuse-reflection`, `refraction` and `diffraction` default to false. `--hold-until-file` requires grid mode; grid mode rejects live position input. Control fanout endpoints must be distinct and position timeout must be positive.

## ocudu-dashboard

[Source](../../apps/dashboard/server.py). Python declaration defaults are shown below; scenario overrides are described in [configuration](configuration.md#scenario-paths).

| Option | Default | Meaning |
|---|---|---|
| `--bind` | `'127.0.0.1'` | See option name and source declaration. |
| `--port` | `8080` | See option name and source declaration. |
| `--telemetry-endpoint` | `'tcp://127.0.0.1:5560'` | See option name and source declaration. |
| `--status-jsonl` | `PROJECT_ROOT / 'results' / 'sionna-2gnb-2ue.jsonl'` | See option name and source declaration. |
| `--index` | `pathlib.Path(__file__).with_name('index.html')` | See option name and source declaration. |
| `--gnb-metrics-endpoint` | `''` | gNB remote-control WebSocket to subscribe to for RAN KPIs (e.g. ws://127.0.0.1:8001). Empty disables the feed. Requires the gNB to run with metrics.enable_json and remote_control. |
| `--gnb-metrics-source` | `[]` | Named gNB metrics source; repeat for multiple gNBs. Cannot combine with --gnb-metrics-endpoint. |
| `--resource-interval-ms` | `RESOURCE_SAMPLE_INTERVAL_SECONDS * 1000.0` | CPU/GPU resource sampling period in milliseconds. The default puts about 100 points inside the 1,000 ms resource charts; raise it if the NVML queries start costing the measured run more than the extra resolution is worth. |

## Supporting tools and launchers

Scene construction, coverage measurement, topology checking and profile replay live with the [Sionna application](../../apps/sionna_bridge/). Use each script’s `--help` for its task-specific arguments. Hardware bridge options are owned by the [USRP integration](../../use_cases/cmx500/README.md).

Workspace preparation and live gate variables are documented by the [native scripts](../../scripts/native/README.md), [remote scripts](../../scripts/remote/README.md), and each [use case](../use_cases/README.md). The [Sionna](../guides/sionna.md) and [dashboard](../guides/dashboard.md) guides show supported launch combinations.
