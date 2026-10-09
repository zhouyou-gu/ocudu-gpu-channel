# Configuration

The schema is implemented by [config.h](../../include/ocudu_gpu_channel/config.h) and the block-style reader/validator in [config.cpp](../../src/config.cpp). The broker loads one file passed with `--config`; relative paths start at the launch working directory. It does not merge environment variables into YAML or reload the file while running. CLI flags control broker execution, endpoints and capture; live channel mutations use the [control API](control-api.md).

## File format and runtime fields

Use space-indented block-style YAML as in the runnable example below. Flow mappings/sequences (`{...}`, `[...]`) and tab indentation are rejected. The top-level sections are `runtime`, `devices`, optional `radio_nodes`, `links` and `models`. Unknown keys are rejected; the reader is not a general YAML implementation.

| `runtime` field | Default | Accepted value and meaning |
|---|---|---|
| `backend` | `cuda` | `cuda` or `cpu`; the selected backend must be built and available |
| `gpu_device` | `0` | Nonnegative CUDA device index |
| `batch_samples` | `auto` | Positive sample count; `auto` resolves one millisecond at each device's sample rate |
| `queue_samples` | `614400` | Positive per-TX-ring capacity; each device needs at least two batches |
| `rx_ring_batches` | `2` | Integer at least 2; RX output-ring capacity in batches |
| `cuda_host_memory` | `auto` | `auto`, `copy`, `zero_copy`; auto selects mapped memory on a suitable integrated GPU and copies on discrete GPUs |
| `cuda_stream_priority` | `default` | `default`, `high`, `low`; scheduling priority within the CUDA context |
| `pacing` | `true` | Boolean; throttle producers to sample rate. Hardware-clocked workflows explicitly choose their setting |

## Devices, radio nodes and edges

| `devices` field | Default | Contract |
|---|---|---|
| `id` | Required | Unique port identifier |
| `role` | `node` | Human-readable label; does not select a processing branch |
| `sample_rate_hz` | `23040000` | Positive integer Hz; connected endpoints must have equal rates because no resampler is implemented |
| `tx_endpoint`, `rx_endpoint` | Required | ZMQ source and destination endpoint strings, respectively |
| `rx_model` | Empty | Optional model applied once after summing all incoming edges; delay steps are rejected |
| `tx_carrier`, `rx_carrier` | Empty | Optional labels; a labeled source TX must match the destination RX |
| `carrier` | Unset | Sets both carrier labels at the point it is parsed; avoid mixing shorthand and directional fields |
| `tx_timing_offset_samples` | `0` | Nonnegative source timing offset, folded into outgoing edge delays |
| `tx_scale_db` | `0` | Finite amplitude scaling within ±200 dB; folded into a fixed `gain` step and retained across profile swaps |
| `tx_queue_samples` | `0` | Zero inherits `runtime.queue_samples`; otherwise overrides this port's TX ring and maximum payload |

A `radio_nodes` entry has `id`, ordered `tx_ports` and ordered `rx_ports`. List order defines matrix indices; IDs do not. Declared nodes must claim every device exactly once, use valid ports, and have mutually consistent rate, timing, TX scale and receiver model. Node IDs cannot collide with device IDs. Without nodes, each device is an implicit one-port radio.

An edge in `links` requires `from`, `to` and `model`; `propagation_delay_samples` defaults to zero and must be nonnegative. Endpoints name radio nodes when those are declared, otherwise devices. Each device needs incoming and outgoing connectivity. Missing endpoints/models, incompatible rates and labeled carrier mismatches reject the topology. The control identity retains the declared model name even when load-time folds create internal model clones.

## Model-level matrix fields

Models are keyed by ID and contain an ordered `chain`. Optional `fixed_mimo.coefficients` entries use `tap`, `rx`, `tx`, `real`, `imag`; unspecified coefficients are zero, and no implicit transmit normalization is applied. Indices must address prepared lanes and taps.

`spatial_correlation.kind` is `iid` or `kronecker`; `full` is explicitly unimplemented. `rx` and `tx` entries use `i`, `j`, `re`, `im` to define the Hermitian positive-semidefinite unit-diagonal correlation matrices. Omitted off-diagonal entries are zero. `los_matrix.coefficients` uses `rx`, `tx`, `re`, `im`, with unspecified lanes retaining `1+0j`. Fixed matrices and stochastic correlation cannot be declared together. See [matrix-channel explanations](../concepts/channel-models.md#mimo-matrix) for interpretation.

## Tap and fading fields

A `tdl` tap has `delay_samples`, `gain_db`, `phase_rad`, `is_los`, `los_k_db` and `los_angle_rad`, defaulting respectively to `0`, `0`, `0`, `false`, `0` and `0`. Samples may be fractional; phases/angles are radians, gains and K-factor are dB. Duplicate delays are rejected; combine their complex weights explicitly. CUDA requires a leading TDL and observes the device tap/capacity bounds.

Optional `fading` enables time-varying taps. `f_d_max_hz` defaults to `0`, `spectrum` to `jakes`, and `grid_us` to `100`. `gaussian` and `flat` parse but their processor generators are unimplemented; parser acceptance is not execution support. `gain` steps use `gain_db` and remain fixed across live profile updates. Step-specific scalar defaults and execution are defined in [processing.cpp](../../src/processing.cpp) and [backend source](../../src/cuda_backend.cu).

All topology, model-chain, port, allocation and pacing changes require restarting the broker. Only the prepared channel fields in the control reference are mutable at runtime.

(scenario-paths)=
## Scenario and scene paths

The Sionna scenario JSON defines nodes, directed links, arrays and optional scene/motion settings; see the [scenario inputs](../../use_cases/configs/sionna/scenarios/) and [loader](../../apps/sionna_bridge/run_bridge.py). `--scenario-config` overrides layout motion and arrays; a scenario's scene overrides `--scene` when present.

An XML scene path resolves from the process working directory, **not from the scenario file's directory**. Scene lookup tries an explicit XML path, then `use_cases/configs/sionna/scenes/<name>/scene.xml`, then a built-in alias/name. Source entrypoints use the checkout's scene directory; installed packages use the launch directory's corresponding hierarchy. Use absolute XML and scenario paths outside the checkout. Status/output paths likewise start at the working directory.

(section-4)=

(topology)=

## Topology graph and YAML model

**This section describes single-antenna radios.** A radio with several antenna ports groups its ports under `radio_nodes:` and its links then connect radios rather than sockets; the schema, the matrix-index rule and the load-time rejections are in [§22](../concepts/radio-topology.md#mimo-nodes). Everything below still holds — a single-port radio is the `Nt = Nr = 1` case, and a topology that declares no `radio_nodes:` keeps exactly the behaviour and the link keys described here.

The topology is a directed graph: nodes are devices (gNBs and UEs); edges are channel edges between them, each with a named `model`. The broker reads `use_cases/configs/topologies/*/topology.*.yaml` and builds one `CudaLinkSlot` per edge. The 3-node example below (`use_cases/configs/topologies/basic/topology.graph.cuda.yaml`) carries both **desired** edges (downlink + uplink) and **crosstalk** edges (UE↔UE leakage).

(arg)=
 
(argx)=
 ![Diagram G — directed-graph topology. Nodes are EXTERNAL SDR endpoints (grey per §2.1). Solid green = desired edges (primary signal flow); dashed amber = crosstalk (secondary path, FALLBACK modifier visual). Every node has its own rx_model noise floor, applied once to the summed RX. The broker sums all edges arriving at each node.](../assets/diagrams/reference-02.svg)

Diagram G — directed-graph topology. Nodes are EXTERNAL SDR endpoints (grey per §2.1). Solid green = desired edges (primary signal flow); dashed amber = crosstalk (secondary path, FALLBACK modifier visual). Every node has its own `rx_model` noise floor, applied once to the summed RX. The broker sums all edges arriving at each node.

A complete runnable configuration is maintained with the use cases:

```{literalinclude} ../../use_cases/configs/topologies/basic/topology.mvp.cuda.yaml
:language: yaml
```

(section-5)=

(channels)=

## Channel models in YAML

Every model is a chain of named steps. The CPU and CUDA backends accept the same step set and are compared numerically at the declared test tolerances (validated by `tests/core/test_processing.cpp`). YAML keys per step type:

| Step type | YAML keys | Effect |
|----|----|----|
| `tdl` | `taps:` block of `{delay_samples, gain_db, phase_rad}` (+ optional `fading:` sub-config and per-tap `is_los` / `los_k_db` / `los_angle_rad`) | Tapped delay line — the chain-leading propagation step. Each tap shifts the signal by `delay_samples` (integer or fractional, resolved by the shared 8-tap Hamming-windowed sinc in `delay.h`) and applies its complex tap weight `10`<sup>`gain_db/20`</sup>`·exp(j·phase_rad)`; with a `fading` sub-config each tap's weight becomes time-varying with a Jakes-shaped Doppler spectrum (plus optional Rician line-of-sight (LOS) specular). Applied **device-side by default** in `apply_channel_kernel` ([§11 Diagram S](device-pipeline.md#kernels)) for any topology where every incoming edge has a leading `tdl`; falls back to host-side `stage_link()` calling `apply_tdl_step{_fading}` in `delay.h` for mixed-edge or non-tdl-leading nodes. Cross-slot history lives in a per-edge `delay_line` ring (in `DeviceLinkState` global memory on the device path, in the host `LinkModelState` on the fallback). A single tap with `delay_samples = 0` is the static-gain case (subsumes the old `gain` step); a single tap with non-zero delay subsumes the old `integer_delay` / `fractional_delay` steps. The chain-leading constraint is enforced by `validate_cuda_support`; a non-leading `tdl` is rejected on the CUDA path (CPU accepts it anywhere). |
| `path_loss` | `path_loss_db` | Scale branch, sign-flipped vs. gain — attenuation. `step.a` = `10`<sup>`−path_loss_db/20`</sup>. |
| `phase` | `phase_rad` | static phase rotation — `step.a` = phase, `step.b` = 0 |
| `cfo` | `cfo_hz` | linear phase drift = carrier frequency offset — `step.b` = `2π·f`<sub>`cfo`</sub>`/f`<sub>`s`</sub> |
| `awgn` | `noise_power` *or* `snr_db` | additive white Gaussian noise (AWGN): complex Gaussian noise via counter-based Philox. Absolute mode: per-component σ = √(noise_power/2); SNR mode: noise scaled to `running_power / 10^(SNR/10)` |

**Three named knobs for the chain-leading delay.** Two physically distinct effects compose into the same chain-leading delay step ([§9](../concepts/timing.md#alignment), Layer 2). The YAML exposes a named knob for each so a topology can express each one cleanly; at config-load time `fold_link_leading_delays()` sums them and merges the total into the edge's effective chain.

| Knob | Scope | YAML key | Physical meaning |
|----|----|----|----|
| **Propagation delay** | per-edge | `propagation_delay_samples` on a `LinkConfig` | Geometry-driven physical propagation along this specific edge. One sample at 23.04 MS/s is ~13 m free-space. Different outbound edges of the same source can carry different values. |
| **TX timing offset** | per-device | `tx_timing_offset_samples` on a `DeviceConfig` | Constant TX-start-time lag of this endpoint — applies uniformly to every outgoing edge of the device. Models a radio that brought its ZMQ TX socket up later than the others. |
| **Explicit chain step** | per-edge (via model) | A leading `tdl` step in the model's chain | The raw mechanism. Use directly when neither of the above scopes fits — e.g., a model that explicitly needs a multi-tap delay profile beyond a single propagation offset. |

**Composition rules.** All three are summed per edge. If the model's chain already leads with a `tdl` step, `fold_link_leading_delays()` shifts every tap's `delay_samples` by the composed offset (preserving multi-tap multipath structure); otherwise a single-tap `tdl` with the composed offset is prepended. Fractional totals are carried through directly by the polyphase-sinc resolution in `apply_tdl_step`. Negative values for either named knob are rejected at load time. The per-edge `delay_line` ring fills on the first serve and the composed offset becomes constant for the run.

**Worked example.** A device with `tx_timing_offset_samples: 2` and a link from it carrying `propagation_delay_samples: 4` on top of a base model whose chain starts with `tdl{(1.0, 0 dB, 0)}` ends up with a synthesised leading `tdl{(7.0, 0 dB, 0)}` — the tap delay shifts from 1 to 1 + (2 + 4) = 7. At idx = 7 the receiver sees the source's sample 0. For a runnable variant of this composition see `use_cases/configs/topologies/ocudu_docker/topology.ocudu-docker.tx-offset.cuda.yaml` — the same single-cell OCUDU topology with a 4-sample `tx_timing_offset_samples` on gNB plus a 3-sample `propagation_delay_samples` on the uplink edge.

`running_power` in the AWGN SNR mode is the broker's running estimate of the signal power at the point in the chain where the AWGN step appears — built up by the host-side `build_steps()` walking the chain, so placing AWGN earlier vs later in the chain gives different absolute noise levels for the same `snr_db`. The `rx_model` for a node is just another model — typically a single `awgn` step (the noise floor), but anything is legal. The validator rejects delay steps in `rx_model` (they would have no defined input signal).

(part-ii)=
