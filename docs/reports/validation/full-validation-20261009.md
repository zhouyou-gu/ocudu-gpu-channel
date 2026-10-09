# Working-tree validation — 2026-10-09

## Scope and environment

The cleaned working tree based on `8acf5b7` was tested on the RTX workstation. Validation found and corrected two launcher configuration defects. Broker processing, configuration schemas and wire protocols were unchanged. Exact source snapshots, commands and logs are retained under `~/ocudu-gpu-channel-workspace/validation/full-20261009/`. The validated cleanup, launcher fixes and dashboard fixes were subsequently committed as `b02687a`, `fb07a67` and `2b31656`, respectively.

Hardware: NVIDIA GeForce RTX 5090 and Intel Core Ultra 9 285K. Builds used CUDA 12.8.1, SM 120, Release configuration and NVIDIA driver 580.173.02. Python was 3.12.3; frontend checks used Node 24.21.0. The thirteen pre-existing containers remained running throughout validation.

## Automated and packaging checks

| Check | Result |
|---|---|
| Fresh CPU / CUDA builds and CTest | 12/12 passed for each backend |
| Nine-stage GPU sequence | All stages passed; eight synthetic broker runs had nonzero traffic and zero starvation, overflow, sequence-gap and ZMQ-error counters |
| Python suite after launcher fixes | 220 passed; two host skips passed separately in isolated containers, exercising all 222 distinct tests |
| Frontend | Disconnect and per-gNB grouping/staleness/recovery checks passed; installed dashboard reviewed at 360, 390, 700, 950 and 1440 pixels |
| Source syntax | All 98 shell scripts and Python source compilation passed |
| Integration assets | Locked patch hashes, relocated assets and native fixture mutation checks passed |
| Transfers | Source inclusion, local-artifact exclusion, ordinary deletion and preservation of excluded remote files passed |
| Docker | SM 120 image built; all nine C++ CTest targets passed in the build image; runtime CLI passed |
| Docker context | 469 required inputs retained; all 17 injected local-tooling/cache fixtures excluded |
| Installed applications | Imports and CLI help worked outside the checkout; dashboard HTML and seven browser modules matched source over HTTP; installed bridge traced and exported one update |
| Documentation | Strict Sphinx build, links, all 114 legacy fragment routes, 71 evidence files, 22 diagrams, search and 18 desktop/mobile views passed |

The main Python run used `MUJOCO_GL=egl`, enabling the previously skipped offscreen renderer. The network-namespace test passed in a disposable container with the required namespace capabilities and no external network. The scheduler fixture test used the exact OCUDU source pinned in `scripts/native/native-workspace.lock.json`, revision `a1916edcdbcd70ba6e0af47ee87be061dad5a4e4`, mounted read-only at its expected path. These checks did not require host privilege changes.

The dashboard installation used only its dependency extra. The installed bridge check reused the existing Sionna environment's dependencies and ran from `/tmp`. The Docker build image lacks the optional Python interpreter, so its nine tests cover C++ targets; the three additional CTest entries ran in both host builds.

## Defects reproduced and corrected

The static two-cell launcher generated four-antenna gNBs at the Sionna port assignments, while its broker topology described one antenna per cell at different endpoints. The failed live run completed only seven IQ pulls; neither UE attached, and one ZMQ error was recorded. A regression reproduced the antenna mismatch before the fix. Static mode now selects the scalar fixture and matching published ports; Sionna mode retains its four-port fixture. Both Sionna gNB configuration files remain byte-identical to the preceding renderer output.

The synthetic `multi-gnb` case similarly paired a four-antenna scenario with a scalar topology and incomplete peer lists. A regression exercising all five actual launcher selections detected it. Both two-cell scene cases now use the matching four-port topology, complete peer lists and the existing dimension check. The tests reject the preceding implementations and pass with the corrections.

Dashboard introductory wording now labels its architecture as an example. Display readiness does not claim live radio attachment, independent matrix verification or strict timing qualification; srsUE is identified as a software UE.

Mobile browser review reproduced horizontal overflow and clipped tap-table columns. Panels and nested grids now shrink to the available width; wide reference and tap tables scroll within their containers. Installed-package checks at five viewport widths passed with recorded Sionna data and expanded details: no page overflow or JavaScript errors, cards fit their grids, and overflowing table columns remained reachable. Fifty dashboard tests and both frontend checks passed after these corrections. Recorded data was used only for presentation checks, not fresh runtime qualification.

## Operational walkthroughs

The documented first-run commands completed on CPU and CUDA with both sources and sinks running concurrently. Each backend recorded 16,054 pulls and 16,000 receive requests, with zero strict-gate counters. SIGINT and SIGTERM checks confirmed child-process cleanup and release of all four ports.

All five synthetic Sionna launch modes processed IQ and applied updates without rejection. Multi-node dashboard checks found no JavaScript exceptions, rendered the scene and displayed advancing iteration data.

| Scene mode | Committed batches | Applied updates | Receive starvations |
|---|---:|---:|---:|
| Single cell, 8-second demo | 188 | 376 | 0 |
| Multi-UE, 12-second demo | 219 | 876 | 0 |
| Graph, 12-second demo | 230 | 1,380 | 0 |
| Two-cell street, 12-second demo | 185 | 1,480 | 0 |
| Two-cell SUTD, 12-second demo | 172 | 1,376 | 1 |

The SUTD run is a functional pass and a strict timing failure. Every mode had zero queue overflows, sequence gaps and ZMQ errors. Synthetic peer delivery was checked through per-receiver broker counters; the launcher terminates peers before their final-duration summaries are emitted. These scene runs do not establish live-radio connectivity or independently verify every changing matrix against boundary IQ.

## Live radio checks

| Configuration | Duration | Connectivity and traffic | Matrix check | Receive starvations |
|---|---:|---|---|---:|
| One gNB / one UE, scalar | 60 s | RRC/PDU; 250/250 pings | Not requested | 2 |
| Rank-1 2×1 DL / 1×2 UL | 60 s | RRC/PDU; 250/250 pings | Passed | 4 |
| Rank-1 4×1 DL / 1×4 UL | 60 s | RRC/PDU; 250/250 pings | Passed | 6 |
| One gNB / two UEs | 90 s | Both RRC/PDU; 25-ping gate passed per UE | Not requested | 11 |
| Two gNBs / two UEs, corrected static launcher | 90 s | Both cells active; both RRC/PDU; three-ping gate passed per UE | Not requested | 9 |

All successful live runs had zero queue overflows, sequence gaps and ZMQ errors. **Every live run failed strict zero-miss real-time qualification.** The multi-user gates retain Boolean ping verdicts; unlike the single-UE gate, they discard individual ping output. Live downlink matrix checks explicitly permit silent higher gNB transmit branches; they do not prove live multi-branch downlink combining.

Live validation used the freshly built broker with immutable prebuilt gNB, core and UE images from the [structure validation](structure-migration-20261009.md). The UE OCI source/revision labels identify `zhouyou-gu/srsRAN_4G` at `daa167ae3443b046ce560df646c7dc5f17e5c1dd`. Exact image IDs and provisioning-only driver differences are retained in `live-images.json` and `live-*-provisioning*.patch`. This validates the exercised radio path, not clean provisioning of external stacks. The existing raw-commit `git clone --branch` limitation remains outside this validation fix.

## Evidence and reproduction

Use the [testing guide](../../development/testing.md), [first run](../../getting_started/first-run.md), [Sionna guide](../../guides/sionna.md) and [remote gate catalog](../../../scripts/remote/README.md). On RTX, the automated suite is:

```sh
ctest --test-dir build-cpu --output-on-failure
ctest --test-dir build --output-on-failure
MUJOCO_GL=egl python -m pytest -ra tests/sionna_bridge tests/dashboard tests/integrations tests/use_cases
node tests/dashboard/test_web_ui_disconnect.mjs
node tests/dashboard/test_web_ui_ran.mjs
python -m sphinx -b html -n -W docs .build/docs/html
python scripts/docs/check_site.py .build/docs/html
```

The evidence directory retains source hashes, CPU/CUDA and container test logs, the extracted GPU sequence, before/after regression outputs, all live reports including the failed two-cell run, scene counters and screenshots, package checks, namespace/fixture provenance, transfer/context checks and container inventories. Original measurements and the declared public evidence hashes remain preserved.

OAI rank-2, native-stack provisioning, CMX500/X310, GB10 and Orin were not exercised. Long moving-radio sessions and performance/sanitizer campaigns were not repeated. Their existing limitations remain in the [capability table](../../getting_started/status.md); functional passes here do not supersede them.
