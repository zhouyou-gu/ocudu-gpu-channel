# Profile activation regression — 2026-11-03

The pending shared-profile flag remained set after activation. A subsequent
scalar or correlation update advanced the control version and activated the
old profile again, clearing delayed samples and restarting warmup.

The fix consumes pending shared and matrix replacements when copied into live
storage. Active profiles remain available for subsequent windows. A new shared
replacement, including an identical resend, still clears delayed samples;
matrix replacements retain their existing compatibility policy.

## Reproduction and regression coverage

The added `test_matrix_profile_history` cases exercise the shipping control
handler together with `process_superposition`, using 8-sample windows and a
5-sample delay. Distinct complex impulses identify each transmitting port's
contribution across window boundaries in 1×1, 2×1, 1×2, 4×1 and 1×4 shapes.

Against the unchanged implementation at `f61ec06308ab15fa1067223dbeaa2326e0079b14`,
the new CPU regression failed on RTX after the first correlation replacement:

```text
event=control_warmup_begin slot=4 ... dl_samples=13 warmup_slots=2
FAIL: correlation/scalar updates after shared profile must preserve delayed IQ
```

With the fix, the regression requires correlation replacement, a 6 dB path-loss
update and return to independent fading to preserve the delayed impulses with
the expected complex amplitudes, advance the active version and avoid another
warmup. Explicit shared-profile resend must discard the old impulse and start
one new warmup. Scheduled shared and matrix replacements must remain pending
until eligible, activate once, and remain active without reactivation on a
later scalar update. Existing matrix compatibility and long-delay tests remain.

## Validation

All runtime checks run on NVIDIA GeForce RTX 5090, using Release builds,
CUDA 12.8.1 and architecture 120, in an isolated source/build directory under
the configured remote workspace's `validation/profile-activation-20261103/`.

| Check | Result |
|---|---|
| New regression against unchanged implementation | Failed as expected on lost delayed IQ |
| CPU CTest suite | 13/13 passed |
| CUDA CTest suite, including CPU/CUDA regression paths | 13/13 passed |
| Standard GPU test sequence, including running-broker control updates | 9/9 passed |

Logs are retained in that directory's `logs/`; local commands and captured
output are in ignored `tmp/profile-activation-20261103/`. The tests establish
the stated state-transition and synthetic signal behavior; they do not add
live-radio or real-time capacity qualification.
