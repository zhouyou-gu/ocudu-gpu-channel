# Telemetry and dashboard status

Source: [control serialization](../../src/control_server.cpp), [telemetry snapshot](../../include/ocudu_gpu_channel/runtime_control.h), and [dashboard server](../../apps/dashboard/server.py). These interfaces report observations; they do not replace independent traffic or boundary-IQ checks.

## Broker PUB frames

Enable the broker's `--control-endpoint` and `--telemetry-endpoint` together. `--telemetry-rate-hz` defaults to 20. The server publishes one ZMQ frame per link: the link ID, one space, then a JSON object. Subscribe to the desired topic prefix and parse JSON after the first space.

| Field | Meaning |
|---|---|
| `event`, `link_id`, `backend`, `process_id` | `telemetry`, physical-link identity, selected backend and broker process |
| `slot`, `seqno` | Last observed processing slot and applied live sequence |
| `live` | Seven runtime scalar values, using the units and ranges in the control reference |
| `profile_active`, `matrix_profile_active`, `array.nt`, `array.nr` | Applied profile state and prepared antenna dimensions |
| `warmup_event_seq`, `warmup_profile_seqno`, `warmup_begin_slot`, `warmup_end_slot`, `warmup_begin_unix_ns`, `warmup_end_unix_ns` | Observed warmup event identity, profile sequence, slot bounds and timestamps |
| `warmup_until_slot` | Backend-reported history warmup boundary; zero must not be replaced by an inferred warmup |
| `slot_processing` | Processing-call timing, nominal-slot estimates and fragmentation accounting |

`slot_processing` reports `sample_rate_hz`, `processed_samples`, `deadline_us`, `elapsed_us`, `usage_percent` and `deadline_met`. Its `cumulative` object counts processing calls (`unit: processing_call`) and includes misses, maximum, p95 and p99 at 1 µs percentile resolution. `calls` exposes call count/misses directly.

`nominal` accumulates sample-proportional call-time estimates into nominal slots: slot size, pending samples/time, completed slots, latest/deadline/usage values, misses, maximum and percentiles. Its `estimation` is `sample_proportional_call_time`; it is not a separately measured wall-clock slot. `fragments` reports fragment calls, samples, min/max sizes and fragmented nominal-slot counts/percentages. Do not interchange call counts and nominal-slot counts.

The publisher uses nonblocking sends. `telemetry_drops` counts send-side `EAGAIN` results, not every subscriber loss; PUB high-water-mark loss can be silent. See the [ZeroMQ PUB contract](https://libzmq.readthedocs.io/en/latest/zmq_socket.html). Sampling telemetry cannot recover every event; use whole-run counters and the report's recorded measurement method for qualification.

## HTTP status

`GET /healthz` checks the dashboard HTTP service. `GET /api/status` combines cached Sionna delivery, broker telemetry, resources and optional gNB metrics. The [dashboard guide](../guides/dashboard.md) describes launch and subscriptions.

| Field | Meaning |
|---|---|
| `ran_gnbs[gnb_id]` | Independent connection, errors, report counters, scheduler rows and receipt ages |
| `ran` | Legacy view of the first configured gNB |
| `ue_list_current_connection` | True only after a scheduler report on that connection |
| `ue_list_age_seconds`, `ue_list_stale` | Scheduler freshness; heartbeats do not refresh it |
| `feeds.telemetry_fresh_seconds`, `telemetry_age_seconds` | Two-second freshness bound and monotonic per-link receipt age |
| `feeds.telemetry_link_count`, `telemetry_cached_link_count` | Fresh and retained cached link counts |
| `delivery` | Matching acknowledgment, fresh backend application and usable-output state |

Scheduler freshness is five seconds. After reconnect, cached UE rows remain historical until a new scheduler report arrives. A fresh empty report clears only its producer's rows. Rows are identified by gNB, PCI and RNTI; RNTI reuse alone does not identify a UE or prove a new session.

Connection and handshake timeouts are five seconds. Five idle seconds trigger Ping; its matching Pong must arrive within ten seconds. Socket/framing failure reconnects and resubscribes while partial frames survive normal idle waits.

Channel readiness separately requires fresh applied usable telemetry for every required edge. Missing timestamps are not fresh. Stale telemetry or HTTP loss removes readiness; backend silence becomes visible within three seconds including polling. Stopping Sionna alone does not invalidate a channel that the broker continues applying and reporting.

Missing producer fields retain their own availability and timestamp semantics; an absent field is not zero. Consult the producer serialization before interpreting sentinel or clamped values. GPU/resource usage, channel readiness and UE traffic are distinct observations.
