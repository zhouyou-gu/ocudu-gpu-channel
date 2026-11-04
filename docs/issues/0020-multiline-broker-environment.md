# #20: Scheduler launcher truncates multiline broker environment settings

GitHub: [#20](https://github.com/zhouyou-gu/ocudu-gpu-channel/issues/20) · Status snapshot: **Open; fix validated in the working tree, publication pending**, 2026-11-04. GitHub carries the current status.

**Component:** Scheduler benchmark broker launch and configuration reporting

**Platform:** Both Linux platforms; reproduced with a mock child on RTX

**Category:** P2 launch-configuration defect in the pending change

## Observed and expected behavior

The profile resolver accepts whitespace-separated assignments in `OCUDU_NATIVE_BROKER_ENV`, including newlines. Before the fix, the inner launch block read only the first line. Later settings were silently omitted, while the run-parameter record still included them.

Every accepted assignment should reach the broker, or unsupported input should be rejected before launch and metadata generation.

## Reproduction and scope

The profile resolver accepts `OCG_BROKER_SPIN=2` followed by a newline and `OCG_ZMQ_IO_THREADS=2`. A non-live probe executed the actual broker launch block with a mock child that printed those variables. Single-line input produced `2|2`; newline-separated input produced `2|unset`; a leading newline produced `unset|unset`.

The regression initially covered only empty and single-line settings. The expanded test reproduced four mismatched child environments and one launch failure before the fix. No native radio or GPU computation was started.

## Findings and unknowns

`scripts/native/platform-profile.py` and run-parameter serialization use Python `.split()`, which processes all whitespace. The previous `read -r -a broker_env <<< ...` consumed one line only. The loss was therefore in launch tokenization, after the settings had already been accepted.

The working-tree fix uses the same Python `.split()` and reads its tokens into a Bash array. Quoted array expansion preserves literal values without shell evaluation or glob expansion. This is separate from the open runtime timing failures and the existing ARM CPU-reference equality failure.

## Fix verification

On RTX, all eight mock-child cases passed after the fix: empty input, whitespace-only input, single-line assignments, newline-separated assignments, leading/trailing newlines, tabs with CRLF, Unicode whitespace and literal shell characters. The tests execute the shipping launch block and verify the actual child environment, including removal of `CUDA_MPS_PIPE_DIRECTORY`.

The scheduler helper suite completed 30 tests: 28 passed and two skipped. Launcher Bash syntax passed. Raw before/after output remains in ignored `validation/runtime-cleanup-20261104/issue20-before.log` and `issue20-after.log` on the validation mirror.

## Closure checks

Consistent tokenization and child-environment regression checks pass. Publish the launcher and regression changes, link the delivered fix and verification in GitHub, then close the issue; the working-tree fix is currently uncommitted and unpublished.

## Evidence

[Launcher](../../use_cases/scheduler_benchmark/run-ocudu-scheduler-benchmark-inner.sh), [profile resolver](../../scripts/native/platform-profile.py), [launch regression](../../tests/use_cases/test_scheduler_benchmark.py). The exact probe results are retained in ignored validation storage; the launch change and this record are uncommitted.

[Issue index](README.md)
