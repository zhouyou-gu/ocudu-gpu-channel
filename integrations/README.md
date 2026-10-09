# External integrations

| Directory | Contents | Workflow |
|---|---|---|
| `ocudu/` | CPU/CUDA gNB patches and source locks for RTX, Spark and Jetson | [Native](../scripts/native/README.md), [CUDA](../scripts/README.md) |
| `srsran/` | srsUE random-access and CMX patches; exact srsUE source pin | [Native](../scripts/native/README.md), [CMX500](../use_cases/cmx500/README.md) |
| `oai/` | nrUE and ZMQ patches, source revision and patch hashes | [Native](../scripts/native/README.md) |
| `usrp/` | Standalone UHD–ZMQ bridge and CMake build | [CMX500](../use_cases/cmx500/README.md) |

Patch files are applied to the revisions recorded in their locks; upstream checkouts remain external. Shared workspace/toolchain locks stay in `scripts/native/`. CUDA source locks live in `ocudu/` and are selected with the existing `OCUDU_CUDA_WORKSPACE_LOCK` setting.

OAI ZMQ module manifests record repository-relative patch paths and a digest of the lock section. After this relocation, rebuild the patched module with `scripts/native/build-oai-zmq-patched.py` before running native OAI gates. Old manifests correctly fail verification; patch contents and source revisions have not changed.
