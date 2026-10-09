# Workspace Harness

Reusable workflow and preferences. File ownership and update rules are defined in `AGENT.md`; mission and current state belong in their respective files.

## Operating Loop

1. Read `AGENT.md`, `AGENT_GOAL.md`, `AGENT_HARNESS.md`, then `AGENT_PROGRESS.md`.
2. Confirm scope, identify the active task, and inspect relevant source and existing edits.
3. Make the scoped change, check consistency, and apply `AGENT.md`'s update dispatcher.
4. Finish at a stable handoff with accurate state, evidence, blockers and resume point.

## Repository and Runtime

- Organize by responsibility: runnable components in `apps/`, scenarios in `use_cases/`, external patches and revision pins in `integrations/`, and orchestration in `scripts/`. Keep shared workspace locks with their workflows.

- Deliver UE/gNB fixes as separate local patches with reproducible application and validation; preserve upstream source checkouts.

- Local source is canonical; the RTX 5090 workstation is a reproducible validation mirror. Keep remote builds, dependencies, logs, captures and datasets outside tracked source unless deliberately promoted.
- Run all validation on the RTX 5090, including CPU reference tests. Do not run backend tests on the user's PC.
- Keep workstation credentials in ignored `.config`, with placeholders only in `.config.example`. Load settings through `scripts/remote/common.sh`; run helpers with Bash, never source them from zsh or source `.config` directly.
- Bootstrap remote dependencies in user space; do not use sudo/apt without an explicit change to that constraint.
- Build UEs from `https://github.com/zhouyou-gu/srsRAN_4G.git`, pinning each baseline/fixed image to an exact commit. Do not silently substitute upstream or release images.
- Retain contributor history through normal merges, verify ancestry, and commit integration fixes separately; do not squash or rebase contributions.
- When asked to consolidate into main, merge in the current folder, preserve pending edits, and report conflicts. Resolve conflicts when authorized; local merge authorization does not imply publication.
- Preserve the requested demonstration scenario and movement. Label stationary runs as diagnostic controls.
- For live Web inspection, leave the actual OCUDU runtime available for a stated window and provide one dashboard for all configured gNBs.

## Architecture and Timing

- Verify external OCUDU/ZMQ behavior against source, documentation or runtime evidence, and record the source for durable claims. Distinguish facts from project intent and inference.
- Keep CUDA as the primary target; label CPU use as reference, baseline, development or unported-model fallback.
- Prefer configurable topologies and per-edge models. Assume a single link only when the task requires it.
- Follow the srsRAN GRC broker: independent per-direction REQ pullers and REP servers with symmetric throttling. Hold requests until processed IQ exists; never zero-fill replies.
- Bound pacing debt to one publication unit so blocked time cannot become a later burst.
- Before blaming peer back pressure, inspect executor sharing: a retry can deadlock when its thread also services the drain.
- When moving ownership, preserve values that differed per old owner. Share the decision without accidentally sharing distinct lane coefficients.
- Check where foreign-stack parameters bind: CLI, initialization, signaling or relative files. Do not assume later signaling repairs initialization defaults.
- In rootless namespaces, capability probes may promise privileges the kernel refuses. Drop misleading capabilities, such as `CAP_SYS_NICE` via `setpriv --bounding-set -sys_nice`, so foreign binaries use their fallback; do not patch or grant privilege blindly.

## Validation and Evidence

- Pair data-path changes with timing, throughput, buffering and IQ-continuity checks. Strict real-time gates fail on any starvation, queue overflow, sequence gap or ZMQ error.
- Prove live connectivity with real gNB, UE and core processes, antenna mappings, registration, PDU sessions and traffic. Synthetic peers prove broker behavior only. Check both UEs, actual serving PCIs and both scheduler feeds in multi-gNB cases.
- Exercise capabilities through the shipping top-level path and add regression coverage or mark them unproven. Inspect actual stack construction and event dispatch; component or alternate-stack tests alone are insufficient.
- Assert nonzero work and expected parsed-record counts before evaluating thresholds. Recheck log parsers after producer renames.
- Recompute output independently from declared configuration and boundary IQ captures. Traffic counters alone do not prove channel computation; identify any evidence that relies on internal accounting.
- Demonstrate the failure mechanism before fixing it. Use causal or mutation probes; a passing run alone does not prove why the fix works.
- For rejection tests, satisfy other rules and confirm removing the targeted rule makes the test fail.
- For behavior-preserving refactors, compare bit-exact output fingerprints against the prior revision as well as running tests.
- Judge stochastic quantities against an ensemble or the exact realization's analytic prediction; state which the test uses. Check analytic expectations before changing tolerances and record measured spread beside them.
- Compare defect statistics in passing and failing runs before assuming distinct failure states. Check whether protocol silence, such as DTX, is legitimate before treating absence as a fault.
- Extract probe helpers from shipping source; label any stand-in explicitly.
- Keep optional behavior behind opt-in switches and prove default artifacts unchanged by diff. Start diagnostic traffic after the gate verdict and log it separately.
- Match injected traffic cadence to the instrument's reporting window; support subsecond intervals when needed.
- Label every performance result with hardware, sample rate, topology, model chain, backend and run duration. Publish measured envelopes; keep unmeasured scale claims in planned work.

## Metrics and UI

- On shared metric buses, update only the reporting producer's fields and maintain per-group timestamps. An absent field does not mean zero.
- Read producer serialization before formatting values. Map sentinels to “not reported,” identify clamp bounds, and explain ambiguous defaults.
- Keep transport liveness, metric freshness and UE connectivity separate; cached or advancing telemetry alone does not prove working traffic.

## Documentation and Handoff

- Maintain current English guidance in the Markdown site; link canonical glossary, status and interface pages. Preserve dated evidence and legacy routes, and validate the built artifact before publication.

- Preserve wording that works and make the smallest scoped edit. Inspect user edits first and treat them as intentional unless context contradicts them.
- Write concise, natural technical English for informed newcomers. Define concepts before using them; structure arguments as problem, limitation, method, contribution and evidence.
- Support implementation claims with source/version/date or runtime evidence. Separate facts, design choices, inference and open questions; parser acceptance is not runtime proof.
- Match terminology to actual granularity and use one name per concept. Say “edge” in prose; retain `link_id`, `link_key` and `links:` as API names, documenting the distinction once.
- Keep spelling, terminology, measured values and rounding consistent across README, reference and supporting artifacts. Use short parallel headings and explicit figure/section references with concise captions.
- Settle the primary artifact's logic before updating derived material. Keep bodies concise and move necessary detail into the technical reference.
- Update planned sections when decisions are deferred. When code lands, update every current-state section it makes false, including definitions, diagrams and invariants.
- After restructuring, check all internal anchors and cross-document section references.
- Use the README contributor table for public names, affiliations, profiles and roles. Use given-name/family-name order, Name (Affiliation) bylines and an expanded affiliation column. Propagate confirmed corrections to current docs; preserve Git authorship and historical evidence.
- Keep rules short and general; record outcomes, evidence and unresolved limits concisely. Avoid repeating historical examples or superseded intermediate states; follow `AGENT.md` for ledger retention and updates.
- Leave touched artifacts consistent, progress current and reusable lessons in this harness before handoff.

## Progress Entries

- Limit each new or revised entry to one bullet, at most two sentences and 40 words. Use one entry per meaningful change; do not split verbose narration across bullets to evade the limit.
- State the outcome, decisive validation and any remaining blocker. Include a commit or evidence link only when useful for resuming work.
- Link detailed logs, commands, hashes, measurements and investigation history instead of copying them into progress.
- Record only changed state; omit repeated context, routine steps and unchanged results. Update the resume point only when it changes, preserving ledger history under `AGENT.md`.
- Before finishing, check every added or revised entry against these limits and shorten any that exceed them.
