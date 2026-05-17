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

## Handoff Condition

- The active workstream is in a stable state or explicitly awaiting user direction.
- Every artifact touched in the turn is left internally consistent.
- `AGENT_PROGRESS.md` reflects the new state accurately.
- Any durable rule revealed during the turn has been promoted into `Reusable Preferences`.
