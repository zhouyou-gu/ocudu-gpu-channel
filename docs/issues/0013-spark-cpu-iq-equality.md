# #13: Spark: cpu_parallel fails serial/parallel IQ equality in fading + LOS

GitHub: [#13](https://github.com/zhouyou-gu/ocudu-gpu-channel/issues/13) · Status snapshot: **Open**, 2026-11-04. GitHub carries the current status.

**Component:** CPU reference channel processing / cpu_parallel regression

**Platform:** DGX Spark GB10, aarch64; CPU reference backend

**Category:** Reproducible existing test failure

## Observed and expected behavior

The CUDA-enabled Spark build passes 12/13 CTest cases; cpu_parallel fails the serial/parallel IQ bit equality assertion in the fading + LOS case. The older Spark build reproduces it, including when the current test is pinned to one CPU.

Serial and parallel CPU execution produce bit-identical IQ across the existing batch sizes and control/profile updates.

## Reproduction and scope

Build with tests enabled on Spark, then run `ctest --test-dir <build> -R "^cpu_parallel$" --output-on-failure`. Compare against the older Spark build before attributing a regression to broker timing changes.

## Findings and unknowns

cpu_backend.cpp, sample_executor.cpp and test_cpu_parallel.cpp have identical hashes in the old and reviewed source trees. RTX passes all 13 core tests. The Spark CUDA live path passed its stated connectivity/runtime checks; that does not clear this CPU-reference failure.

The numerical or execution mechanism behind the equality failure has not been isolated. No tolerance was relaxed and the failing check remains enabled.

## Closure checks

Identify the first differing sample and its cause, implement the correction, and pass the unchanged equality test and relevant CPU/CUDA checks on ARM and x86. Preserve deterministic channel/control behavior.

## Evidence

[docs/reports/validation/live-runtime-limitations-20261104.md](../reports/validation/live-runtime-limitations-20261104.md), including the November cross-platform review; [docs/development/testing.md](../development/testing.md).

[Issue index](README.md)
