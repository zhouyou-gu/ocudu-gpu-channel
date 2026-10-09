# R7: delay-compensated robot control

R7 implementation and this validation campaign are complete: eight radio arena
runs, nine requested radio characterization conditions (seven successful,
two unsupported), and 96 local impairment fights. The compensated controller
wins more often in the tested paired comparisons, including a fresh-seed,
fixed-parameter repeat; it does not eliminate falls or make blackouts safe.

## What differs between the robots

Both robots have the same two-wheel inverted-pendulum body, `ram` strategy,
balance gains and torque limit. Both send state at 200 Hz and receive commands
at 100 Hz, with a 60 ms command lifetime. `balance` uses the received state;
`balance_comp` rolls that state forward with a pendulum model, estimated state
age, command delay and previously sent torques, then applies the same balance
law. Neither controller uses an LLM.

The first experiment retained the existing 10% seeded speed jitter. The longer
repeat disables it with `RF_PARAM_JITTER=0`. Physical positions, radio paths and
seeded strategy noise still differ between sides. Policy assignments are
swapped on the same seeds; only seeds completed on both sides enter the paired
comparison. Shared-clock delay estimates are used on this single-host setup;
the fallback estimate uses half the minimum recent RTT.

## Radio setup and initial result

DGX Spark GB10; two CPU gNBs, two single-port srsUEs on separate cells/PCIs 1 and 2,
two CUDA channel brokers at **23.04 MSamples/s**, and Sionna robot-ring channel
taps plus receiver AWGN. This is the R5 two-cell topology. It differs from the
single-cell R4b setup. Robot positions drive the channel in the arena runs.

Each initial gate ran for 180 seconds. Baseline used two protected brokers and
no GPU hog. Contention used two plain brokers and an MPS GPU hog requesting
200 us kernels, two streams and queue depth 16. These treatments change both GPU
contention and broker scheduling; their difference is not an isolated estimate
of either effect. Both controllers receive the same treatment within a run.

| Matched, side-swapped comparison | Fights | Comp wins | Plain wins | Draws | Comp/plain falls |
|---|---:|---:|---:|---:|---:|
| Baseline, seeds 7000–7009 | 20 | 15 | 3 | 2 | 0 / 1 |
| Contention, seeds 7000–7011 | 24 | 23 | 1 | 0 | 0 / 11 |
| Contention repeat, seeds 9000–9021, jitter 0 | 44 | 33 | 10 | 1 | 8 / 13 |

The repeat ran 300 seconds per assignment, with fresh seeds starting at 9000
and speed jitter disabled. There were 22 and 28 complete fights; the matched
comparison uses their 22 common seeds. The compensated controller won 33 of 43
decided fights (76.7%), but fell eight times versus 13 for plain control. Its
zero-fall result in the shorter initial comparison did not generalize. The
repeat changes seeds, duration and speed jitter together, so it does not isolate
which change explains the smaller advantage. Median per-fight RTT p50 was
37.5 ms for compensated control and 37.6 ms for plain control.

![Matched radio comparisons](robot-fight-r7-comparison.png)

The compensated controller wins more often in this tested setup, including
after the side swap. Both policies see similar RTT within a condition. This
supports a control benefit under delay, not a reduction in network RTT caused
by the controller. The figures report medians of per-fight RTT quantiles, not
percentiles pooled over packets. Repeated seeds are not independent replicates;
the JSON's Wilson intervals are descriptive.

Same-policy controls under contention completed 18 plain/plain fights with 13
falls and UE0/UE1 wins 6/12, versus 7 comp/comp fights with zero falls and wins 2/5.
These small, unequal samples do not establish side symmetry.

![Same-policy controls](robot-fight-r7-controls.png)

## Corrected radio characterization

The earlier preparation sweep lost eight requested AWGN/RAN overrides across
`sudo`. Those runs cannot establish the requested conditions. The corrected
runner passes overrides explicitly, rejects inherited native settings, saves
requested conditions and preserves native failure status. The audit verifies
both rendered gNB configurations, AWGN noise powers, sample rates and receiver
model wiring; a successful attachment report alone is insufficient.

The corrected UDP echo probes run at 100 Hz for 160-second gates, with the first
20 seconds excluded from steady-state statistics. They use scripted scene
positions, offset(2,0,0) and uplink reference power 300, without the fighting
arena; compare them to their own new baseline. The declared AWGN parameter is
a unit-gain reference SNR, not the measured receive SNR.

AWGN settings 40,34,30,27 and 24 dB all produced median RTT near 16 ms in both UEs,
with no losses among resolved requests. Some conditions have late-packet
bursts and larger tail delays: a stable median is not a bound on latency.
Changing SR period to 40 ms increased median RTT to about 19.5 ms, primarily in
the uplink. Limiting DL/UL HARQ retransmissions to one at the 30 dB setting
left median RTT near 16 ms, with no losses among resolved requests. The pinned gNB supports K1/K2 only in[1,4], with defaults 4; K2=8
and K1=7/K2=8 are rejected before radio startup. They are unsupported
configurations, not attachment failures or measured latency conditions.

Probe loss excludes requests still unresolved at teardown; those counts remain
in the JSON. Its historical `outages` field means runs of at least three
consecutive requests that were lost or exceeded 200 ms RTT, measured in request
send-time slots. It does not identify PHY loss of synchronization.

![Corrected radio characterization](robot-fight-r7-probes.png)

The nine requested conditions are accounted for: seven successful gates with
verified effective settings, and two unsupported configurations rejected before
radio startup. [Probe summaries](robot-fight-r7-probes.json) retain the failed
entries, unresolved requests and separate per-UE measurements.

## Local blackout and loss tests

These are local MuJoCo/UDP-proxy tests without a RAN, Sionna or Spark. Each
condition has 12 seeds 8100–8111 with both policy assignments,15 ms symmetric
one-way delay, an 8-second fight limit and 60 ms command lifetime. All 96 fights
were complete and within the accepted simulation/wall-time ratio 0.98–1.02.
Every impaired fight actually dropped traffic; exposure is recorded separately.

| Condition | Comp wins | Plain wins | Draws | Comp/plain falls |
|---|---:|---:|---:|---:|
| Baseline | 20 | 3 | 1 | 0 / 0 |
| One 100 ms blackout | 21 | 2 | 1 | 0 / 0 |
| One 300 ms blackout | 19 | 5 | 0 | 1 / 0 |
| 10% random packet loss | 21 | 3 | 0 | 0 / 0 |

![Local UDP impairment comparisons](robot-fight-r7-robustness.png)

One compensated robot fell in the 300 ms blackout condition after traffic resumed.
Compensation does not guarantee blackout survival, and the winning advantage
already exists in the baseline. Command-stale intervals were 49–51 ms for the
100 ms blackout and 248–252 ms for 300 ms; random loss did not expire the command
lifetime in these runs. Separate ideal-delay cruising tests made both
controllers fall with a 500 ms blackout. Prediction cannot deliver commands
through a missing link and assumes a simplified model of command application.

## Connectivity and timing limits

All eight arena runs each show one attach/RRC/PDU establishment per UE,
without console reconnection indicators, and report both UE processes alive
at broker stop. This is bounded session/traffic evidence. The deployed srsUE
pin's NR `in_sync()`/`out_of_sync()` callbacks are empty; console silence cannot
certify uninterrupted PHY synchronization. Provenance and coverage are in the
[audit JSON](robot-fight-r7-audit.json).

Radio strict-realtime checking was disabled and broker starvation counters are
nonzero. Accepted real-time MuJoCo fights do not qualify radio processing
deadlines. No hours-long connectivity guarantee, blackout immunity or general
controller stability proof follows from these runs. UE/gNB source trees were
not edited for this work; unsupported settings were recorded rather than
enabled by changing upstream limits.

## Artifacts and reproduction

- [Radio comparisons and run parameters](robot-fight-r7-results.json)
- [Local impairment results and exposure](robot-fight-r7-robustness.json)
- [Condition and session audit](robot-fight-r7-audit.json)
- [Launch, collection and side-swap instructions](../use_cases/robot_fight/launch/README.md)

Raw logs remain in the Spark native result directories and local ignored
`results/robot-fight/`. `collect_r7_results.py` exports small artifacts, including
failed runs and incomplete final fights. `analyze_r7_battle.py` excludes missing
or interrupted results, lockstep operation and out-of-range real-time factors;
`plot_r7_results.py` regenerates the comparison figures. The corrected probe
table comes from `analyze-r7-probe-runs.py` and its graph from
`plot_r7_probes.py`. Preserve raw failures and exclusions when reproducing the
comparison.


## Validation of tooling

The new condition/lifecycle auditor, collector, sweep failure handling and
launcher forwarding passed 23 focused tests. Python compilation, shell syntax
and diff checks passed. Figures were visually inspected. The earlier publication
checkpoint's robot and Sionna suites passed 39 and 83 tests respectively, with
five robot tests skipped; these are separate from the new focused checks.
All eight arena gates ended successfully; each interrupted final fight remains
explicitly excluded. The last repeat ended at 08:25:56 UTC on 2026-09-30 and
the GPU process list was empty afterward.
