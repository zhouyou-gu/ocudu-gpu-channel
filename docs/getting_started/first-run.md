# First synthetic CUDA run

This tutorial sends IQ through two directed channel edges using synthetic peers. It proves that the selected broker processes nonzero traffic and exposes its counters. It does not prove radio attachment, uninterrupted moving traffic or strict real-time qualification.

## Before starting

Complete the [CUDA build](installation.md), run from the repository root in **Bash**, and ensure TCP ports 2000, 2001, 2100 and 2101 are free. The [MVP configuration](../../use_cases/configs/topologies/basic/topology.mvp.cuda.yaml) uses 23.04 MS/s and two scalar edges.

The snippet starts both sources and both sinks concurrently. Only the processes it starts are cleaned up. All output is saved in a new temporary directory.

```bash
set -euo pipefail
build_dir="${OCUDU_CHANNEL_BUILD:-$PWD/build}"
topology="${OCUDU_FIRST_RUN_TOPOLOGY:-use_cases/configs/topologies/basic/topology.mvp.cuda.yaml}"
run_dir="$(mktemp -d /tmp/ocudu-first-run.XXXXXX)"
pids=()
cleanup() {
  for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
  for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
  printf 'Logs: %s\n' "$run_dir"
}
trap cleanup EXIT INT TERM
for port in 2000 2101; do
  "$build_dir/ocudu-zmq-source" --endpoint "tcp://*:$port" \
    --batch-samples 23040 --duration 15s >"$run_dir/source-$port.log" 2>&1 &
  pids+=("$!")
done
"$build_dir/ocudu-gpu-channel" --config "$topology" --duration 12s \
  >"$run_dir/broker.log" 2>&1 &
broker_pid=$!
pids+=("$broker_pid")
sleep 1
sink_pids=()
for port in 2001 2100; do
  "$build_dir/ocudu-zmq-sink" --endpoint "tcp://127.0.0.1:$port" \
    --duration 8s --request-interval-us 1000 >"$run_dir/sink-$port.log" 2>&1 &
  sink_pids+=("$!")
  pids+=("$!")
done
for pid in "${sink_pids[@]}"; do wait "$pid"; done
wait "$broker_pid"
cat "$run_dir/broker.log" "$run_dir"/sink-*.log
```

## Read the result

The broker should report `event=start backend=cuda` and finish with `event=stop`. Both sinks must receive samples; `tx_pulls` and `rx_requests` must be nonzero. Inspect `rx_starvations`, `tx_queue_overflows`, `tx_sequence_gaps` and `zmq_errors` directly. Any nonzero strict-gate counter is a miss even if useful IQ was delivered.

`--request-interval-us 1000` asks each sink for a 1 ms cadence. It does not by itself establish a deadline pass. The finite startup/shutdown window and synthetic peers differ from a continuously attached radio. Use the [qualification procedure](../development/testing.md) for a measured claim.

## CPU reference route

Repeat the same snippet with `OCUDU_CHANNEL_BUILD` set to the CPU build directory and `OCUDU_FIRST_RUN_TOPOLOGY=use_cases/configs/topologies/basic/topology.local.cpu.yaml`. Confirm `backend=cpu`. Label the result as a CPU reference run.

## Troubleshooting and next steps

If a source cannot bind, identify the port owner and choose an isolated run or a separate topology. If sinks receive no IQ, inspect the broker log for configuration or CUDA errors and confirm both source processes remained alive. Keep the logs before changing anything.

Continue to the [use-case catalog](../use_cases/README.md), [Sionna guide](../guides/sionna.md), or [live-stack integration](../guides/live-stacks.md).
