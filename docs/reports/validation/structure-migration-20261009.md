# Structure migration validation — 2026-10-09

> Dated evidence, preserved from `docs/reports/structure-migration-20261009.md` at `58d3156`. Results apply only to the recorded revisions, hardware and configurations; see [current status](../../getting_started/status.md).


The migration reorganizes the repository by responsibility, with no changes to C++ processing, executable names, configuration schemas or wire protocols. The baseline is `0b613d9`; the combined code and documentation tree checked here is `e738266`. See the [destination map](../../development/project-structure.md#migration-map--2026-10-09).

All runtime validation ran on the RTX 5090 workstation in the isolated workspace `~/ocudu-gpu-channel-workspace/validation/structure-20261009/`. Its `baseline/` is an archive of the baseline commit; `source/` contains the migrated tree. The final report/progress commit adds documentation only.

## Results

| Check | Result |
|---|---|
| CPU / CUDA CTest | 12/12 each |
| Synthetic GPU sequence | 9/9; clean/AWGN relay, graph, multi-gNB, TDL-A, correlated/IID MIMO and live control update |
| Python | 209 suite tests passed, plus 2 new integration-asset tests; 3 skipped |
| Frontend | Both JavaScript disconnect/RAN checks passed |
| Shell syntax | All 98 shell scripts passed |
| Installed packages | Imports and both console commands work outside the checkout; dashboard installation contains no Sionna dependency |
| Installed dashboard HTTP | HTML and all seven browser modules match source bytes |
| Docker | GPU image builds for SM 120; runtime contains the relocated topology and broker CLI works |
| Patch assets | All 22 relocated patches match baseline bytes; locked paths and hashes pass 17 focused subtests |
| Documentation | No new missing targets or anchors relative to the baseline; historical diagnostics remain |
| Live 2×1 rank-1 | RRC/PDU established, 250/250 pings, independent matrix capture passed; 2 receive starvations |

Python skips: a modem network-namespace test needs capabilities; MuJoCo offscreen rendering lacks a working OpenGL context; the scheduler test lacks its pinned external OCUDU checkout. These are not claimed as passes.

## Behavior comparison

The C++ implementation and headers match the baseline byte for byte. An external validation probe instruments the existing runtime-update parity test to capture each processed IQ buffer, links it against the baseline and migrated CUDA libraries, and executes the same sequence on both. All 99,680 bytes match, including CPU-reference and CUDA output across runtime updates:

```text
SHA-256: b2dc853ddb9213070006e305bc4b32627236264361f6f8b06f4d540271842fcc
```

The native OAI topology renderer and Sionna environment record also emit identical serialized output (`d6a460f8b3af4bc7fee53d74811cc9fe6e994636e76a2311092722d5c61f38a1`). These comparisons establish behavior for the exercised inputs, not every workload.

## Live-stack check

The 60-second Docker-based 2×1 gate exercised the relocated topology and gNB configuration with the migrated CUDA broker and the independent wire checker. RRC and PDU setup succeeded, all 250 pings returned, and both directions passed the matrix comparison over 4,608,000 samples per port. Maximum absolute errors were 4.712e-08 for DL and 4.535e-05 / 2.088e-05 for the two UL rows (tolerance 1e-4).

The second gNB transmit branch was explicitly declared silent, as in the existing rank-1 gate; the live DL result does not establish multi-branch downlink combining. The broker recorded two receive starvations and zero queue overflows, sequence gaps or ZMQ errors. Connectivity and matrix checks passed; strict zero-miss real-time qualification remains failed.

The successful run is `live-pinned/reports/structure-rank1/20261009T081131Z/`. Validation used prebuilt, explicitly selected core/gNB images and srsUE revision `daa167ae3443b046ce560df646c7dc5f17e5c1dd` (image `sha256:921ab4091d1ce9b964c48d97e4d6fc64ba3be26eea696cc833ecc3ec87662689`). The source/revision OCI labels were checked before using the immutable UE image ID. Test containers were removed; the thirteen original containers remained running.

## Operational migration notes

- Installed applications resolve relative inputs from the launch directory. Supply absolute scene/scenario paths outside the checkout; use-case assets remain repository inputs. Dashboard browser assets are bundled in the package.
- OAI ZMQ module manifests include patch paths and the lock-section digest. Rebuild that module after relocation; old manifests fail verification even though patches and source pins are unchanged. See [integration notes](../../../integrations/README.md).
- The existing remote UE image builder passes `SRSRAN_4G_REF` to `git clone --branch`, so a raw commit hash fails before radio launch. This was observed and retained as `live/`; it predates the migration. The follow-up uses the existing image verified by OCI source/revision labels and immutable image ID.
- The isolated Compose project otherwise attempts to build a differently named core image. The live validation driver explicitly selects existing core and gNB image IDs and disables rebuilding; an interrupted provisioning attempt is retained under `live-prebuilt/`.
- Native OAI, CMX/X310, GB10 and Orin hardware qualification were not rerun. This migration does not change their recorded limits or establish strict zero-miss real-time operation.

## Evidence

The remote workspace retains `cpu-test.log`, `cuda-test.log`, `python.log`, `integration-assets.log`, `frontend.log`, `gpu-sequence.log`, package installation/help logs, `installed-dashboard.log`, `docker-build.log`, `links.log`, baseline/current IQ captures and fingerprint probes. Live attempts retain their drivers, image IDs, reports and logs separately.
