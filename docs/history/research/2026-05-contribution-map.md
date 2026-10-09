# ocudu-gpu-channel — research contribution map

> Historical record; its claims apply to the original date, revision and setup. Migrated from `docs/paper/2026-05-contribution-map.md` at `58d3156` without changing recorded measurements.


Date: 2026-05-29. Purpose: decide whether this project carries an academic
contribution (MobiCom / SIGCOMM / NSDI / ToN), not just an implementation,
and name exactly what is defensible today versus what needs new work. This
is an analysis doc, not a paper draft or task state.

## 1. The honest problem

The system as built is strong engineering: per-edge multipath + Jakes
Doppler + Rician LOS on a commodity GPU, in-the-loop with real srsRAN +
OCUDU at the 5G NR slot deadline, CPU<->CUDA bit-exact, validated by real UE
attaches. None of that, by itself, is a paper. Top venues reject
"we built a fast GPU version of X" as an engineering report. A contribution
needs: a framed question, an insight or methodology that generalises, a hard
constraint met, comparison against a baseline, and fidelity evidence against
ground truth.

## 2. Competitive landscape (what is already claimed)

- ACHEM (arXiv 2026): "first software-based, end-to-end channel emulator"
  validated with srsRAN / OAI / GNU Radio. Pure CPU, I/Q level, USRP-
  oriented, scenario replay. This already takes "first software in-the-loop
  channel emulator with real stacks." We cannot claim that crown.
- Colosseum / MCHEM (MobiCom 2021): FPGA channel emulator, 256x256 channels,
  FIR convolution in hardware. The emulation itself is taken at the box
  level; their paper contributions are scale + an RF-scenario tap-selection
  methodology. We are the commodity-GPU point against their FPGA cost/scale.
- Agora (CoNEXT 2020): CPU massive-MIMO baseband. Contribution pattern to
  copy: "first software realization" + "we identify the dimensions of
  parallelism and exploit them (SIMD / cache / kernel-bypass)" + meets a
  hard 5G NR requirement. The insight, not the build, is the paper.
- GPF (MobiCom 2018): GPU 5G scheduler under a ~100 us deadline; the
  deadline-meeting design is the contribution.
- Sionna (NVIDIA): GPU link-level + ray tracing, offline, research/ML. Not
  in-the-loop; a fidelity reference we can validate against, not a competitor
  for the real-time claim.

Reference URLs:
- ACHEM https://arxiv.org/abs/2604.04742
- Colosseum https://arxiv.org/abs/2110.10617
- Agora https://www.yecl.org/publications/ding2020conext.pdf
- GPF https://par.nsf.gov/servlets/purl/10090368
- Sionna https://github.com/NVlabs/sionna

## 3. The one fresh angle

Everyone above does scenario REPLAY: precompute the channel, play it back.
The runtime-mutable control plane (shadow/snap, take_effect_at_slot, warmup
contract, atomic multi-link batches) makes the channel INTERACTIVE at slot
cadence: an external controller or RL agent can change path_loss / CFO / the
whole tap profile inside the loop and the real UE reacts within bounded
slots. No surveyed system offers closed-loop, slot-interactive channel
emulation with real stacks.

Proposed framing:
"Closed-loop, slot-interactive wireless channel emulation on a commodity
GPU" — the systems techniques that let a MUTABLE per-edge channel meet the
slot deadline in the loop with real 5G stacks.

This reframes the three weakest-looking engineering pieces into the
contribution: (a) deadline-aware per-edge scheduling; (b) the PCIe-bound H2D
analysis plus the source-dedup that addresses it; (c) the slot-boundary
mutation contract (warmup, atomic batches).

## 4. Claim ledger (defensible today vs. needs work)

| Candidate claim | Status today | To make it paper-grade |
| --- | --- | --- |
| Per-edge TDL + Jakes + Rician on commodity GPU at slot cadence, in-loop with real srsRAN/OCUDU | Built, validated by attach | Frame as enabling the interactive claim, not as the claim itself (ACHEM/Colosseum own "first emulator") |
| Closed-loop slot-interactive channel mutation | Built (control plane), untested as a research capability | A use case that REPLAY cannot do (the killer demo) |
| PCIe H2D is the binding wall, not GPU compute | Measured (sec 20.5) | Generalise: model the bound; show where it bites vs edge count / SCS |
| Source-rebuffering (D4) cuts H2D | Implemented but DORMANT (one link per pair today) | Build a multi-model-per-pair scenario where it actually pays; measure the win |
| CPU<->CUDA bit-exact parity | Verified at 1e-3 | Necessary hygiene, not a contribution; keep as validation |
| Fidelity of the emulated channel | Only self-consistency (parity, analytic power) | Validate against a reference (Sionna RT or MATLAB 5G Toolbox): statistical channel metrics, not power alone. Reviewers ask this first |

## 5. Gaps that currently read as "implementation, not contribution"

1. No framed research question (interactivity, sec 3, fixes this).
2. No comparative baseline (vs ACHEM CPU; vs Colosseum FPGA cost/scale; vs
   Sionna offline). Re-use our own CPU backend as the honest baseline.
3. No fidelity validation against ground truth — self-consistency is not
   correctness.
4. D4, the most research-like optimization, is dormant; a reviewer will
   catch an unrealized win.
5. Single GPU, single box; scale is bounded and must be stated plainly.

## 6. Venue assessment

- MobiCom / SIGCOMM / NSDI: ~40% there. Needs interactivity insight + a
  killer closed-loop use case (e.g. train an RL link-adaptation or
  power-control agent against the live emulator and show something replay
  cannot do) + baseline + fidelity study. Months of new work; high reject
  risk, but the interactivity angle is genuinely unclaimed.
- ToN journal: realistic strong target; tolerates a solid systems
  contribution with thorough evaluation at moderate novelty.
- CoNEXT / WiNTECH: systems-friendly middle path; good fit for the
  GPU-channel + interactivity story with moderate added eval.

User decision (2026-05-29): aim for the top-tier insight, large effort
budget. So the bar is the MobiCom/SIGCOMM row: the interactivity reframing
plus the closed-loop use case is the spine, not optional.

## 7. Minimum new work for the top-tier target

1. Frame the question around mutable per-edge channel at slot cadence.
2. Fidelity study: emulated TDL output vs Sionna RT or MATLAB 5G Toolbox —
   statistical channel metrics (Doppler spectrum, delay-spread, envelope
   distribution), not just average power.
3. Comparative eval: GPU path vs CPU backend — latency vs edge-count vs SCS;
   where each hits the slot wall.
4. Make D4 non-dormant: a multi-model-per-pair scenario so source-dedup is
   demonstrated, with measured H2D savings.
5. Closed-loop use case: an RL or scripted controller acting through the
   control plane, achieving an outcome a replay emulator cannot — this is
   the top-tier differentiator.

## 8. Risks / things a reviewer will attack

- "ACHEM already did software in-the-loop." Answer: ours is GPU + slot-
  cadence + INTERACTIVE; theirs is CPU replay. The claim must be
  interactivity, never "first emulator."
- "Fidelity?" Answer must be a reference comparison, not parity.
- "Why GPU if PCIe is the wall?" Answer: compute headroom for the mutable
  per-edge kernel + the interactivity it enables; quantify the headroom.
- "Single box, limited scale." State plainly; position against Colosseum's
  cost, not its scale.
