# CUDA-accelerated OCUDU gNB

The CUDA gNB is an external OCUDU source tree with platform-specific revision and patch locks. Its acceleration is separate from the channel broker's CUDA backend: record which components use the GPU in each experiment.

## Select and verify

Choose the hardware profile and CUDA workspace lock from the [platform guide](platforms.md). `scripts/cuda/resolve-cuda-gnb.py` audits source revision, local changes, patch checksum and built binary. Use the matching scripts under `scripts/cuda/`; keep the upstream checkout intact and apply project patches through the locked build workflow.

The [integration assets](../../integrations/README.md) own patches and pins. Shared workspace locks remain with scripts that assemble the complete stack. Exact launch options and resolved paths belong to the selected workflow; do not copy another host's build path.

## Interpret a run

Check the resolved gNB binary, GPU context/MPS configuration, broker backend and CPU placement before collecting results. The workload can miss timing even when both CUDA components operate correctly. Use the gate's traffic, integrity and deadline verdicts, and record hardware, rates, topology and run duration.

Stop only the processes owned by that gate. For source-lock or manifest failures, rebuild through the matching locked workflow rather than bypassing the audit.

The [original integration assessment](../history/milestones/cuda-gnb-integration.md) explains the patch mechanisms and milestone measurements. Those historical results remain bounded to their recorded revisions. Current qualification is summarized in [status](../getting_started/status.md).
