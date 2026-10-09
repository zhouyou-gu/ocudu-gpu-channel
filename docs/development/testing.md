# Testing and qualification

Run all project validation on the RTX workstation, including CPU reference tests. Use separate CPU/CUDA build directories and keep raw output, source revision and resolved configuration with the report.

## Supported checks

From the repository root, use an isolated test environment with `pytest`, `numpy`, `pyzmq` and `PyYAML` installed. Optional rendering dependencies may produce documented skips; inspect the test summary.

```sh
ctest --test-dir build-cpu --output-on-failure
ctest --test-dir build --output-on-failure
python -m pytest tests/sionna_bridge tests/dashboard tests/integrations tests/use_cases
node tests/dashboard/test_web_ui_disconnect.mjs
node tests/dashboard/test_web_ui_ran.mjs
```

The configured remote launcher `bash scripts/remote/gpu-test-sequence.sh` drives nine synthetic GPU stages. Remote launchers read ignored `.config`; do not run a remote wrapper from a host lacking that configuration. Native and Docker live gates are listed in [live-stack integration](../guides/live-stacks.md).

A strict real-time pass requires nonzero work and zero starvation, overflow, sequence gaps and ZMQ errors. Report failed counters alongside any successful attachment or traffic. Independently compare declared channel behavior with boundary IQ; internal counters alone do not prove the channel computation. For documentation-only changes, verify the build, links and rewritten commands without inventing a new hardware qualification.

(section-18)=

## Validation

Three layers, each progressively closer to a live radio integration. The detailed test surface is mapped in [Test architecture — what each layer guards](testing.md#test-architecture) below.

**Which layer is the testing target for what.** The **unit tests** ([Unit tests (CTest, 12/12 at the integration revision)](testing.md#test-layer-unit)) and the **synthetic GPU validation** ([Remote GPU test sequence (gpu-test-sequence.sh, 9 stages)](testing.md#test-layer-remote)) are the targets for the internal build roadmap (the *Phase 1/2/3* work — correctness, CPU↔CUDA parity, kernel behaviour); the **live-radio integration** smoke ([Live OCUDU + srsRAN smoke](testing.md#test-layer-live)) is the target for the *Milestone A/B/C* proof points (a real srsRAN gNB + srsUE completing attach + PDU (protocol data unit) session + ping through the broker). "Milestone" and "Phase" are two distinct axes — *what works end-to-end* vs *how it was built* — not competing labels.

(test-layer-unit)=

### Unit tests (CTest, 12/12 at the integration revision)

Run with `ctest --test-dir build --output-on-failure` after `cmake --build`. The following summarizes the original test groups; the integration adds matrix/control/history coverage:

- **`config`** — YAML schema validation: unknown keys, bad numerics, mixed sample rates, negative offsets, the `tdl` taps schema, fading config (`f_d_max_hz`, spectrum, LOS K-factor + angle), per-edge `propagation_delay_samples` and per-device `tx_timing_offset_samples` composition, `fold_link_leading_delays` equivalence.
- **`ring`** — `IqRing` push/overflow, cursor-based read, `discard_before`, true wrap-around across the buffer boundary, `reset()` with new capacity.
- **`processing`** — the largest surface. Covers:
  - *tdl behaviour* (CPU): identity tap, 3-tap cross-slot impulse response, sinusoid passband at fractional delay.
  - *CPU↔CUDA numerical agreement* at 1e-3 tolerance: identity, integer + fractional delay, multi-tap cross-slot ring, fading (Jakes + LOS Rician composition).
  - *Dispatch-gate test* — asserts `ProcessorTimings.used_device_channel` is `true` for a leading-tdl model and `false` for a leading-non-tdl model, so a regression that silently reverts CUDA to host staging fails ctest.
  - *Fading statistical guards*: stationary (`f_d=0`), bit-exact determinism (same seed → same floats across recompiles), strong-LOS magnitude (K = 40 dB), **Bessel J<sub>0</sub> autocorrelation match** at three lags (`tests/core/test_processing.cpp:1147`), and **Rician envelope PDF moments** at K = 10 dB (`tests/core/test_processing.cpp:1333`).
  - *Per-sample step behaviour* (CPU standalone): `path_loss`, `phase`, `cfo`, `awgn` in both `noise_power` and `snr_db` modes.
  - *CUDA AWGN statistical* — power + zero-mean + fresh draw per slot.
  - *tx_timing_offset + propagation_delay equivalence* — fold composition tested on both CPU and CUDA.
- **`broker`** — 2-device CPU loopback with all data-integrity counters at zero; 3-device fan-in/fan-out lockstep regression for the variable-size relay (documented dead-lock case).

(test-layer-remote)=

### Remote GPU test sequence (gpu-test-sequence.sh, 9 stages)

The locked-in remote validation. **Must pass before any change to the broker or CUDA backend ships.** Each step adds a layer:

1.  CUDA release build (rsync local tree → build → check exit).
2.  CTest 12/12 at the recorded integration revision on the remote box (same suite as [Unit tests (CTest, 12/12 at the integration revision)](testing.md#test-layer-unit) above, GPU path enabled so all `OCUDU_GPU_CHANNEL_HAS_CUDA` blocks actually run).
3.  Synthetic CUDA relay loop — clean 0 dB channel, sink measures `avg_power ≈ 1.0`.
4.  Synthetic CUDA relay loop — AWGN `noise_power = 0.25`, sink measures `avg_power ≈ 1.25` within 0.003 %.
5.  3-node graph (Diagram G) — `gnb0` RX `avg_power ≈ 2.005` (two UE uplinks summed), `ue0/ue1` RX `avg_power ≈ 0.501` (desired + −40 dB crosstalk).
6.  2-cell / 4-node / 8-link multi-gNB graph — all four nodes' measured RX `≈ 0.262` matches analytic superposition.
7.  **TDL-A profile** — TR 38.901 §7.7.2 23-tap NLOS + Jakes 100 Hz on every edge; bench p99 in the green band; `avg_power_cum` within ±1.5 power units of the analytic expectation (the same tolerance the CPU↔CUDA matching test uses for single-realisation fading variance).
8.  Correlated 2×2 MIMO relay, compared with an iid control.
9.  Live `correlation_swap` sent over the control socket to a running broker; received power must respond to the updated matrix.

(test-layer-live)=

### Live OCUDU + srsRAN smoke

Docker gNB + Open5GS core + srsUE through the CUDA broker, attach + IP ping verification. Three variants exist on the remote box: `ocudu-attach-smoke.sh` (1 gNB + 1 UE, Milestone A), `ocudu-multi-ue-smoke.sh` (1 gNB + 2 UEs, Milestone B), `ocudu-multi-gnb-smoke.sh` (2 gNBs + 2 UEs with ICI, Milestone C). Detailed runbook at [`docs/ocudu-interop.md`](../guides/live-stacks.md).

(test-layer-mimo)=

### Multi-port gates

Three of the standing gates exist specifically to judge multi-port behaviour, and they divide by what they can see:

- **Synthetic, through the broker** — `gpu-test-sequence.sh` steps 8 and 9 relay a declared 2×2 correlated topology through a running broker and compare the received power against the analytic expectation (measured 9.542 against an expected 9.71, with an iid control at 6.964 against 6.94), then swap the correlation matrix on a **live** broker over the control plane and confirm the received power moves off the iid value. A milestone whose gates all call the processor directly proves the processor and nothing above it; these run the capability through the layer that ships it.
- **Live transport** — `run-ocudu-mimo-2port-no-core.sh`: a real 2-antenna OCUDU gNB at a pinned revision, a byte-pinned fixture, four ZMQ endpoints, no Docker and no core. It judges four-endpoint flow, sibling reply sizes, sibling acquisition skew, the gNB's own real-time failure count and the strict broker counters.
- **Live matrix** — `verify-mimo-matrix-capture.py`, run by the same gate against the broker's wire capture. This is the one that judges what the emulator *computed*: it reads `H` from the topology and compares `y` against `Hx` sample by sample, in both directions, reporting the off-diagonal share of each row. Numbers and mutation probes in [Live evidence, and the line it stops at](../reports/validation/rank1-boundaries.md#mimo-evidence).

**A zero count satisfies every threshold.** Each of these gates asserts that its instrument recorded work — samples compared, markers checked, kernels launched — before it asserts anything about the values. That rule was written after a benchmark reported millions of iterations and a kernel count of zero and exited green, and after this very gate reported `marker_mismatches=0` beside `marker_checks=0`.

(test-architecture)=

### Test architecture — what each layer guards

![Diagram TA — Layer 1 catches algorithmic regressions in seconds and runs per commit; Layer 2 catches anything ctest can't reach (broker hot path, real ZMQ, cross-stream concurrency, cumulative-stat mismatches between CPU and CUDA) and runs pre-ship; Layer 3 catches protocol-stack wedges that only manifest with a real radio stack and runs at milestone boundaries. The specific guards in each layer are named in §18.1 – §18.3 above.](../assets/diagrams/reference-17.svg)

<a href="../assets/diagrams/reference-17.svg">Open this diagram at full size</a>

The preserved schematic shows the earlier eight-test CTest surface. The current combined tree has 12 CTest targets per backend, as recorded in the validation reports.

Diagram TA — Layer 1 catches algorithmic regressions in seconds and runs per commit; Layer 2 catches anything ctest can't reach (broker hot path, real ZMQ, cross-stream concurrency, cumulative-stat mismatches between CPU and CUDA) and runs pre-ship; Layer 3 catches protocol-stack wedges that only manifest with a real radio stack and runs at milestone boundaries. The specific guards in each layer are named in [Unit tests (CTest, 12/12 at the integration revision)](testing.md#test-layer-unit) – [Live OCUDU + srsRAN smoke](testing.md#test-layer-live) above.
