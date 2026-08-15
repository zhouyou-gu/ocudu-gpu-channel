# Workspace Harness

**This file is the reusable playbook for the workspace. It holds durable workflow rules and generalized operating preferences. It shall not define control-file update policy, restate or modify the mission, or record live task state.**

## Task Context

This workspace starts as a new repository for a GPU-backed channel-emulation layer around OCUDU ZMQ SDR workflows. The repository source, tests, docs, and the four agent files are the local source of truth; external OCUDU behavior must be verified against public docs, source code, or runtime evidence before being encoded as durable project fact.

## Standard Operating Loop

1. Read the agent files in the order required by `AGENT.md`.
2. Confirm the contemplated work is in scope under `AGENT_GOAL.md`.
3. Identify the active workstream from `AGENT_PROGRESS.md`.
4. Gather the context required by the workstream before changing any state.
5. Make the change.
6. Leave every artifact touched in the turn internally consistent.
7. Apply the update dispatcher from `AGENT.md` before reporting the turn complete.
8. Stop when the workstream is in a stable state or requires user direction.

## Reusable Preferences

- Separate source-backed OCUDU/ZMQ facts from project intent or inference; cite or record the source consulted when making durable claims about OCUDU behavior.
- Do not assume a single-link topology when designing APIs, configuration, tests, or docs unless the active task explicitly narrows the scope.
- Pair real-time data-path work with validation that checks timing, throughput, buffering, and IQ stream continuity.
- Prefer configurable topology and per-link channel definitions over hard-coded endpoint pairs.
- Keep private workstation connection details in ignored `.config`; keep only placeholder structure in tracked `.config.example`.
- Treat the local repository as the canonical Git source and the remote workstation as a reproducible validation mirror; keep remote OCUDU checkouts, builds, logs, captures, datasets, and raw benchmark results outside tracked source unless deliberately promoted into docs or examples.
- Keep remote GPU workstation dependency bootstrap user-space by default; do not rely on sudo or apt unless the user explicitly changes that constraint.
- Load remote workstation settings through `scripts/remote/common.sh`; do not source `.config` directly from an interactive shell because `~/` values can expand against the local machine.
- Run `scripts/remote/*.sh` helpers as Bash scripts, not by sourcing them from zsh, because the shared helper relies on Bash-specific `BASH_SOURCE`.
- Treat strict realtime broker validation as failed when starvation, queue overflow, sequence gap, or ZMQ error counters are nonzero.
- Keep CUDA/GPU channel emulation as the primary product target; describe CPU behavior only as reference, baseline, local development, or unported-model fallback.
- Model the ZMQ broker on srsRAN's GNU Radio Companion reference broker. That broker is a concurrent, per-direction relay shaped `ZMQ REQ source -> Throttle -> channel -> ZMQ REP sink`: a dedicated puller (ZMQ REQ, drains a peer's TX) and a dedicated server (ZMQ REP, feeds a peer's RX) per device, run as independent threads, with a symmetric per-direction throttle. The broker is REQ/REP-gated and never zero-fills a reply -- when it has no processed IQ it holds the request until it does, mirroring how OCUDU's own gNB TX holds a request when its buffer is empty. When changing broker structure, reference the GRC broker shape rather than inventing a new pacing model.
- When a planning decision is reached, but the corresponding code change is deferred, update only the planned-state doc sections (e.g., the "Scope and planned work" section of `docs/index.html`) immediately, and leave the current-state doc sections (vocabulary, alignment/delays, chain-step table, diagram captions) for a coordinated sweep when the code lands. Updating current-state prose ahead of the code creates a doc-vs-code mismatch in the opposite direction of the one the planning update is trying to prevent.
- Publish measured envelopes, not unmeasured scale claims. Every performance number in the README, the technical reference, or anywhere user-facing must name the hardware, the sample rate, the topology, the model chain, the backend, and the run duration that produced it; a number without those five labels is not yet a result. Aspirational scale ("supports N UEs") belongs in planned work, not in the current-state body.
- Write human-facing prose -- commits, PRs, README, the technical reference, the agent files -- by the writing rules copied from the manuscript-writing playbook (`skills/manuscript-writing-agent-files/examples/AGENT_HARNESS_MANUSCRIPT_EXTENSION.md` in `zhouyou-gu/skill-marketplace-template`); its manuscript/LaTeX-only rules (math notation, BibTeX, two-column overflow) do not apply here. The transferable rules:
  - Preserve existing wording when it already works; prefer the smallest edit that fixes the prose, logic, or evidence problem. Scope edits to the artifact explicitly requested; do not silently bundle unrelated changes.
  - When the user makes manual edits, inspect the current diff before continuing, treat the edits as intentional unless the surrounding text contradicts them, and propagate only the remaining consistency updates needed.
  - Keep claims evidence-backed and bounded to what has been checked; distinguish established facts, design choices, inference, and open questions. When a claim depends on implementation or runtime behavior, support it with source-file, version, date, or runtime evidence -- not tool availability or parser acceptance alone.
  - Write consistent, concise, coherent, formal, natural technical English for informed readers who may be new to the subfield; define important terms and abstractions before relying on them. Build the narrative as problem context -> limitation -> method rationale -> contribution -> evidence, and when an argument feels weak, fix the reasoning before polishing wording.
  - Use terminology that matches the system's actual granularity; avoid labels that imply more precision, generality, or capability than it provides. Keep one name per concept and use the English name before its symbol: say "edge" in prose; keep `link_id` / `link_key` / `links:` as API names; record the split once in the glossary.
  - Keep body text concise: shorten the body first and move extension material into the technical reference only when still needed; keep section and subsection titles short and structurally parallel with peer titles.
  - Keep one spelling and terminology convention across the README, the technical reference, and supporting artifacts; align derived artifacts rather than letting wording diverge. Avoid conversational or self-defensive framing; state scope limits as plain claims.
  - Prefer concise figure captions naming the plotted quantity and its swept variable, terminology aligned to the body; keep figure/section labels, captions, and in-text references synchronized, and refer to a figure or section by explicit number or label rather than relative words like "above" or "below."
  - Revise in a separate pass: settle the primary artifact's logic first, then revise secondary artifacts and tighten wording.
- State a measured number with one value and one rounding everywhere it appears -- README, doc hero, and the technical-reference body must not disagree (not "≈180×" in one place and "183×" in another). [Lesson from the 2026-05 consistency pass, not the upstream playbook.]
- After any doc restructure or section renumbering, re-verify before reporting the turn complete that every internal anchor and every README→doc section-number citation still resolves. [Lesson from the 2026-05 consistency pass.]
- Judge a stochastic quantity against an ensemble, never against one realization. A test that compares a single random draw to its distribution's mean is grading which draw it got: it passes or fails on the seed, and a tolerance tuned until it passes bakes that luck in permanently. Where the system already produces independent realizations (the lanes of a link, several links, several runs), average across them and size the tolerance from the measured spread; where it does not, compare the draw against what THAT draw analytically predicts instead. State in the test which of the two it is doing. [Lesson from M2.2: a single-lane Bessel J₀ gate stayed green across three milestones while measuring nothing but its own seed, and a re-seed that could not touch the generator was enough to break it.]
- Before trusting any measurement, check that the instrument recorded work. A count of zero satisfies every threshold and passes every gate: a benchmark whose lookups all missed reported millions of iterations, a kernel count of zero, and a green exit. Assert the non-zero counts the run should have produced -- samples processed, kernels launched, cases exercised -- as part of reading the result, and treat a suspiciously fast or suspiciously clean run as a reason to check the counter first. [Lesson from M3.7: the bench had keyed destinations by device id since M1 and measured nothing on multi-port topologies for three milestones.]
- When a refactor is supposed to change nothing, prove it against the previous commit rather than against the test suite. Build the same probe in a worktree at the prior revision, print a fingerprint of the output in a bit-exact form (hex floats), and diff. A suite that passes only shows the outputs are still acceptable; the fingerprint shows they are the SAME, which is what "changes nothing" actually claims. [Lesson from M3.3.]
- A rejection test can pass because a DIFFERENT rule fired. When adding a validation rule, make the fixture satisfy every other rule so the case reaches the one under test -- and confirm it by removing that rule and watching the test fail. [Lesson from M3.6: a partial-matrix rejection was really being caught by an earlier precondition, and the probe was the only thing that showed it.]
- When a statistical test fails, get the analytic prediction for the exact inputs it used before touching the test. The prediction separates "the implementation is wrong" from "the estimator is noisy" in one measurement, and it is the only thing that makes widening a tolerance an honest act rather than a way to make the failure stop. Record the measured numbers next to the tolerance so the next reader can see what the margin actually is. [Same lesson.]

## Handoff Condition

- The active workstream is in a stable state or explicitly awaiting user direction.
- Every artifact touched in the turn is left internally consistent.
- `AGENT_PROGRESS.md` reflects the new state accurately.
- Any durable rule revealed during the turn has been promoted into `Reusable Preferences`.
