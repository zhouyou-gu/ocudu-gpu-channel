> Editorial note (2026-10-09): local paths and workflow narration were normalized for this [public edition](../../development/documentation.md#public-editions); measurements and conclusions are unchanged.

# Local main merge and validation

> Dated evidence, preserved from `docs/main-merge-validation.md` at `58d3156`. Results apply only to the recorded revisions, hardware and configurations; see [current status](../../getting_started/status.md).


Date: 14 September 2026. Integration was consolidated into `main` with conflicts
resolved and contributor history retained. This record covers the local merge
and validation before publication. Current implementation and qualification limits are described in the
[Sionna integration guide](../../history/milestones/sionna-integration-20260914.md).

## Retained history

| Commit | Purpose and parents |
|---|---|
| `22a51ba` | Merge integration tip `0e0a6c3` into main `f51c3fd` |
| `e59e095` | Merge older local tip `5f55bb7` into `22a51ba`, retaining local commits `b726ab3` and `5f55bb7` |

Contributor tip `066a702ab2ecc301d5329c5fec89baefa30336f3`, integration tip
`0e0a6c3`, old checkout tip `5f55bb7` and starting main `f51c3fd` all remain
ancestors of the result. Original contributor commits and authorship were
neither squashed nor rebased. The README credits Zhouyou Gu, Minwoo Eun and
Hyunsoo Lee consistently with the retained contribution history.

## Conflict and pending-edit reconciliation

The only source conflict was `scripts/remote/ocudu-multi-gnb-smoke.sh`.
The resolution retains the integrated subnet selection, four-port antenna
handling, validated hold/keepalive settings and cleanup behavior. It restores
`OCUDU_MGNB_SIONNA_EXTRA_ARGS` through the current `run_web_ui.sh` wrapper:

- Extra bridge arguments survive SSH as encoded data. Whitespace separates
  legacy environment arguments; shell substitutions and wildcard expansion
  are not evaluated.
- The wrapper accepts `-- BRIDGE_ARGS...` and forwards these arguments only
  to `run_bridge.py`. Direct callers can quote individual arguments with spaces.
- The empty default core host-port value uses a placeholder across SSH so it
  cannot shift the later remote positional arguments.

Relative to integration `0e0a6c3`, executable changes are limited to these two
launcher scripts and `tests/test_sionna_launcher.py`. The channel engine,
OCUDU, UE source, moving scenario and dashboard implementation are unchanged.

Before switching branches, tracked and untracked local edits were saved in
stash `5021c39660e39aab3b5dd9784a8c2f4e399300aa` and byte-for-byte backups under
`.git/merge-backups/main-20260914T075710Z`. The original files remain available
there. Pending workflow notes were reconciled, and the original
[merge plan](../../history/designs/merge-miso-siso-sionna-rt-5090.md) was retained as a historical
record with current-guide pointers. The older UI translations and demo
hold/keepalive changes are covered by the integrated implementations; the
obsolete UI was not copied over the unified dashboard. Keepalive remains an
explicit `OCUDU_MGNB_UE_KEEPALIVE_SECONDS` option.

The separate srsRAN fork still contains tested UE source
`daa167ae3`; its commits were not copied into this channel repository. Those
fixes are not implied to be on remote `master`.

## RTX 5090 checks

Host: NVIDIA GeForce RTX 5090, driver `580.173.02`. All executable checks below
ran in `~/ocudu-gpu-channel-workspace/validation/main-merge-20260914`, with the
existing Sionna Python environment and cached `node:22` image. No radio
launcher, GPU performance suite or live-runtime restart was invoked.

| Check | Result |
|---|---|
| New wrapper forwarding regression against pre-resolution `22a51ba` | Fails as expected: extra arguments rejected with exit 2 |
| Four launcher regressions on `e59e095` | Pass: defaults, bridge-only forwarding, SSH metacharacters/whitespace and empty core-port position |
| Full discovered Python suite on `e59e095` | 90/90 pass |
| RAN grouping/recovery and HTTP-disconnect frontend regressions | Both pass |
| Shell syntax for the two changed launchers | Pass |
| Required ancestry and Git whitespace checks | Pass |
| Documentation links, credits and HTML parsing | Nine documents; 147 local links/anchors; three normalized contributors; zero HTML errors |
| Isolated Chrome documentation preview on RTX 5090 | Six views pass, including desktop/mobile credits, reference, guide, UE results and merge report |

Commands, from the isolated remote project directory:

```bash
w="$HOME/ocudu-gpu-channel-workspace"
v="$w/validation/main-merge-20260914"
cd "$v/project"
"$w/venvs/sionna/bin/python" -m unittest discover -s tests -p 'test_*.py'
for test in tests/test_web_ui_ran.mjs tests/test_web_ui_disconnect.mjs; do
  docker run --rm --network none -v "$v/project:/work:ro" -w /work \
    node:22 node "$test"
done
bash -n scripts/remote/ocudu-multi-gnb-smoke.sh scripts/sionna_rt/run_web_ui.sh
```

For the before-fix check, a separate `baseline` directory contains the
`22a51ba` wrapper and the new test. Run from its `tests` directory:

```bash
"$w/venvs/sionna/bin/python" -m unittest \
  test_sionna_launcher.SionnaLauncherTests.test_wrapper_extra_arguments_only_reach_bridge
```

Raw logs are `launcher-before.log`, `launcher-tests.log`, `python.log` and
`frontend.log` under the isolation root. These tests use process stand-ins
for launcher argument checks; they are not evidence of new OCUDU attachment
or traffic qualification. Source hashes match the tested remote mirror; the
manifest and comparison are `source-sha256.json` and `source-verification.json`.
The documentation preview used a temporary loopback server and a separate
browser tab; both were closed afterward. The earlier real-radio results retain their exact
software revisions and limitations in the integration guide.

## Remaining qualification failures

Continuous two-UE moving connectivity and strict zero-miss real-time operation
remain failed. The bounded fixes were merged into local main;
the merge does not change those measurements or establish seamless geometry
updates. No thresholds or runtime scheduling settings were relaxed.
