# Distributed-Computer Feasibility

Running gNBs, UEs, and the channel emulator on separated computers is possible only when the network behaves like a low-jitter lab fabric. The current Wi-Fi path to the GPU workstation is not acceptable for real-time IQ transport; a measured 20-packet ping had min/avg/max/stddev `7.178/47.690/151.816/45.733 ms`.

## Required Network

Use wired, same-switch Ethernet:

- no Wi-Fi;
- no VPN or Tailscale on the IQ data path;
- no congested shared switch;
- p99 RTT under load <= 200 us, preferred <= 100 us;
- one-way jitter target <= 50 us;
- zero packet loss during bidirectional `iperf3`.

Use PTP or chrony for measurement correlation, but do not use clock sync to hide sample-path jitter.

## Bandwidth Sizing

OCUDU ZMQ carries `cf32` IQ samples, which are 8 bytes per complex sample. Full-duplex raw bandwidth per device is:

```text
sample_rate_hz * 128 bits/s
```

Reference rates:

| Sample rate | Full-duplex raw bandwidth per device |
| --- | ---: |
| 23.04 MS/s | ~2.95 Gbps |
| 30.72 MS/s | ~3.93 Gbps |
| 61.44 MS/s | ~7.86 Gbps |

Practical guidance:

- 10GbE: only safe for 1 gNB + 1 UE at 23.04 MS/s with limited headroom.
- 25GbE: target 1 gNB + 3 UEs at 23.04 MS/s or 1 gNB + 1 UE at 61.44 MS/s.
- 100GbE: recommended for multi-gNB/multi-UE at 61.44 MS/s or higher.

## Distributed Test Order

1. Measure idle RTT histogram.
2. Run bidirectional `iperf3` at expected load.
3. Run synthetic ZMQ source/sink through the emulator.
4. Run OCUDU only after the synthetic path passes bandwidth, jitter, and loss gates.
