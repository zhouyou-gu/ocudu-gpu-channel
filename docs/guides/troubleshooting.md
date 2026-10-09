# Troubleshooting

Start with the failing stage and its logs. Keep the original topology, scenario, binary revision and output before changing configuration.

| Symptom | Check | Meaning |
|---|---|---|
| CUDA configuration rejected | Build flags, detected device and `event=hardware_probe` | A CPU build or unsupported device is not a CUDA execution |
| Broker starts, sinks receive nothing | Source processes, endpoint direction, matching rates and port owners | A running process does not prove nonzero IQ work |
| Control update rejected | Exact error reply, link identity, lane dimensions and delay bounds | Rejection is different from accepted but not-yet-applied state |
| Dashboard connected but stale | Per-producer timestamps, scheduler report and status path | Socket liveness does not refresh scheduler data |
| Initial attachment works, moving traffic fails | Both UE traffic streams, serving PCIs, radio logs and channel freshness | Initial PDU sessions do not establish continuous connectivity |
| Pings pass but strict gate fails | Starvation, overflow, sequence-gap and ZMQ-error counters | Keep the failed qualification alongside successful traffic |
| OAI module manifest points to old patch paths | Rebuild with current integration paths and locked patch bytes | Path relocation can invalidate a pre-migration manifest |
| Native profile differs from expected hardware | Workspace locks, platform detection and explicit overrides | Record the resolved binaries and CPU placement |

See [control API](../reference/control-api.md), [telemetry](../reference/telemetry.md), [testing](../development/testing.md) and the [validation reports](../reports/validation/README.md) for exact interpretation.
