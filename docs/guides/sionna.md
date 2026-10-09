# Sionna bridge

The bridge turns scene-derived channel impulse responses into complete per-edge matrix updates. IQ continues through the CUDA broker. Requested update rate and achieved ray-tracing rate are separate measurements.

## Prerequisites

Build the CUDA broker and synthetic peers, install the `sionna` extra in a compatible environment, and select a matching topology and scenario from `use_cases/configs/`. The bridge's antenna dimensions must match the broker's prepared radio nodes. See the [control API](../reference/control-api.md) for matrix and history semantics.

## First scene and dashboard

From the repository root, use the existing single-cell synthetic workflow:

```sh
OCUDU_SIONNA_PYTHON="$PWD/.build/apps-venv/bin/python" \
OCUDU_CHANNEL_BUILD="$PWD/build" \
bash scripts/local/run_synthetic_web_ui.sh single
```

This starts sources, broker, sinks, bridge and dashboard. It prints the local HTTP URL after a Sionna update and HTTP readiness; logs are printed on exit. The default demo duration is 60 seconds. `OCUDU_SIONNA_DEMO_DURATION_SECONDS=0` keeps the scene running until interrupted. Stop with Ctrl-C; the launcher cleans up its children. For a browser on another computer, follow the [dashboard SSH tunnel instructions](dashboard.md).

For an existing broker, use:

```sh
bash scripts/local/run_web_ui.sh \
  --python "$PWD/.build/apps-venv/bin/python" \
  --scenario "$PWD/use_cases/configs/sionna/scenarios/simple_street/ocudu-docker.json" \
  --status-jsonl /tmp/ocudu-sionna/status.jsonl
```

The existing broker must expose the matching control and telemetry endpoints. Pass bridge-specific arguments after `--`. Installed `ocudu-sionna-bridge` accepts the same bridge options; supply scenario paths explicitly outside the checkout. Scene paths inside a scenario resolve according to the [configuration reference](../reference/configuration.md#scenario-paths).

## Verify and diagnose

Inspect bridge update records, broker acknowledgments and fresh applied telemetry. An ACK is not proof that samples have used the new matrix. Identical and gain/phase-only matrix updates retain history; changed ordered delays or layout reset it. Geometry changes are not seamless. Dynamic CUDA matrices require the device-channel route and a complete prepared lane set.

A readiness timeout can mean missing Sionna dependencies, scene-loading failure, mismatched topology, rejected control updates or an unavailable HTTP server; the launcher logs identify the stage. Use [dashboard guidance](dashboard.md) to distinguish backend freshness from radio connectivity.

For actual radios, follow [live-stack integration](live-stacks.md) and retain the requested movement. The [status page](../getting_started/status.md) records the remaining moving-traffic and real-time failures. The [buffered CIR design](../development/designs/sionna-live-channel.md) describes a different, proposed transport mechanism.
