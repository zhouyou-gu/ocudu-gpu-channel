# #19: Preserve channel continuity across Sionna delay/layout changes

GitHub: [#19](https://github.com/zhouyou-gu/ocudu-gpu-channel/issues/19) · Status snapshot: **Open**, 2026-11-04. GitHub carries the current status.

**Component:** Sionna profile activation and CPU/CUDA channel history

**Platform:** Moving live Sionna channels; CPU/CUDA backends

**Category:** Known channel-update limitation

## Observed and expected behavior

Identical or gain/phase-only matrix updates preserve history, but tap-count or ordered-delay/layout changes reset lane history and restart warmup. Seamless moving-geometry channel continuity is therefore not qualified.

Any seamless geometry-update claim preserves correctly aligned signal history across supported changes and respects prepared delay/antenna bounds.

## Reproduction and scope

Apply a sequence of matrix profiles that first changes only gain/phase, then changes ordered delays or tap layout. Compare resulting IQ and warmup/history behavior against an independent time-varying channel reference.

## Findings and unknowns

The repeated identical-profile reset defect was corrected; this issue covers genuine layout/delay changes. Control delays are bounded to 0–1023 samples, and antenna dimensions remain fixed at preparation.

The buffered external-channel design is proposed, not qualified. A fix must define coefficient timing, history migration and delay-window bounds rather than suppressing the visible warmup or accepting unsupported layouts.

## Closure checks

Implement the declared continuity semantics, preserve the existing identical/gain-phase behavior, and validate changing geometry on both CPU/CUDA and the relevant live path with independent sample-time evidence.

## Evidence

[docs/history/milestones/sionna-integration-20260914.md](../history/milestones/sionna-integration-20260914.md), [docs/development/designs/sionna-live-channel.md](../development/designs/sionna-live-channel.md), [docs/getting_started/status.md](../getting_started/status.md).

[Issue index](README.md)
