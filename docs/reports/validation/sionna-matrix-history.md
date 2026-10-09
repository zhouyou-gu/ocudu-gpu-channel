# Matrix channel history preservation

> Dated evidence, preserved from `docs/sionna-matrix-history.md` at `58d3156`. Results apply only to the recorded revisions, hardware and configurations; see [current status](../../getting_started/status.md).


This is a dated validation record. See the [current integration guide](../../history/milestones/sionna-integration-20260914.md) for subsequent channel, dashboard and UE recovery changes; the results below remain tied to their recorded revisions.

Matrix updates retain recent IQ samples when dimensions, lane count, per-lane
tap counts, ordered delays, LOS fields, fading settings, and `force` are exactly
unchanged. Only tap gain and phase are excluded from the comparison. Resending
an identical matrix also preserves history. Delays use exact equality.

The physical link makes one `history_reset_required` decision before any antenna
lane processes the batch. CPU and CUDA use that decision for every lane. A
coefficient update uses the new coefficient for echoes of earlier input samples;
it neither starts a warmup nor changes an existing warmup's scheduled end.

The first matrix, a return from scalar mode, or a change to any compared field
retains the existing reset behavior. Scalar `profile_swap` and control message
formats are unchanged. Control ACK warmup values remain estimates; backend
telemetry, including zero, determines usability. The UI records reset intervals
only from backend timestamps or an actually observed legacy warmup.

## Validation on 2026-09-13

The delayed-echo regression failed on the integration merge before the fix:
`delayed echo must survive a gain/phase or identical matrix update`.

After the fix:

- All 12 CTest entries passed on macOS CPU and Linux RTX 5090 CUDA.
- All 73 Python tests passed on both machines.
- `matrix_profile_history` checked SISO, 2×1, 1×2, 4×1, and 1×4, using independent
  antenna inputs and an analytic complex-valued reference with `1e-3` tolerance.
  It covered repeated identical/coefficient updates, link-wide delay resets,
  tap-count resets, scalar compatibility, and updates during an active warmup.
  The shared decision test covered every non-coefficient field, including a
  one-ULP delay change and reordered taps.
- The existing `scripts/remote/gpu-test-sequence.sh` passed all nine stages
  (the sequence has grown beyond its earlier seven stages): build, CTest,
  clean relay, AWGN, three-node interference, two-cell interference, TDL-A,
  correlated/independent 2×2 MIMO, and a live correlation control update.
- Compute Sanitizer memcheck passed the history regression with zero errors.
  GPU: RTX 5090; driver 580.173.02; CUDA toolkit 12.8.1.

The UI tests verify authoritative zero warmup, both telemetry/ACK arrival
orders, mixed preserving/resetting links, actual reset timestamps, and no
interval invented from an ACK alone. The existing NVML cadence test now uses
a deterministic clock. The remote sync excludes root build directories without
excluding the source file `apps/sionna_bridge/build_osm_scene.py`.

CPU commands, from the integration worktree:

```sh
cmake -S . -B build-history -DCMAKE_BUILD_TYPE=Release \
  -DOCUDU_GPU_CHANNEL_ENABLE_CUDA=OFF
cmake --build build-history -j4
ctest --test-dir build-history --output-on-failure
python3 -m unittest discover -s tests -p 'test_*.py'
```

The ignored worktree `.config` points remote project/build/result paths to
`~/ocudu-gpu-channel-workspace/validation/sionna-history-20260913/`.
Local logs are in `build-history/`; remote logs are in that validation
directory's `results/`. The existing remote project and radio services were
preserved. No attached-radio or live Sionna scene qualification is claimed.

## Integration and limits

Normal merge `3f4c234` has parents main `f51c3fd` and contributor `066a702`.
Contributor commits and authorship remain in its ancestry; the history fix is
a separate subsequent commit on `integration/sionna-history-fix`.

Geometry changes can still reset history. This does not implement buffered
channel streaming. The separate control/CUDA delay-capacity mismatch remains a
merge blocker: control accepts delays up to 1023 samples while CUDA's history
capacity is 128 samples, with additional fractional-filter space required.
Passing these checks does not qualify the whole contribution for `main`.
