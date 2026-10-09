# Hardware-in-the-loop tools: CMX500 / USRP X310 / GPU channel / srsUE

These tools put the GPU channel emulator between a real gNB (Rohde & Schwarz CMX500, 5G SA, n3 FDD, 15 kHz SCS, 52 PRB)
and a software UE (srsUE over ZMQ), with a USRP X310 as the radio on the CMX side.

```text
CMX500 -- RF cable -- X310 <-> usrp_zmq_bridge <-> ocudu-gpu-channel (CUDA) <-> srsUE (ZMQ, 11.52 MS/s)
```

## Where things are

| Path | What |
|---|---|
| `tools/usrp-zmq-bridge/` | UHD <-> ZMQ bridge: hardware-timestamped receive, maps uplink samples to downlink time, timed uplink bursts, frame-aligned downlink catch-up, `--timing-dir` per-block timing CSVs |
| `tools/cmx-loop/` | `run_cmx_loop.sh` (starts bridge, srsUE, broker in order), `sweep_dl.sh` + `sweep_report.py` (live downlink SNR sweep), timing analysis scripts |
| `use_cases/configs/topologies/cmx/topology.cmx-bridge.*.yaml` | Broker topologies: passthrough, and downlink path loss in front of a fixed noise floor |
| `use_cases/configs/ran/srsue/srsue_zmq_cmx_n3.conf.in` | srsUE config template for the CMX500 cell. `@UE_IMSI@` and `@UE_K@` must be filled in |
| `scripts/native/patches/srsue-cmx/` | srsUE patches (see below) |

The scripts assume the paths of the container they were developed in (`/workspace/...`); adjust the variables at the top.

## Broker changes needed for a live radio

- `runtime.pacing: false` stops the producer's real-time throttle. A live radio already is the clock, and pacing the
  UE-to-radio direction would hold back the UE's zero-padded TX prefix so the uplink never catches up.
- `devices[].tx_queue_samples` sets the TX ring (and the largest accepted ZMQ payload) per device. srsRAN's ZMQ TX
  sends transmit gaps as one zero payload of up to 3,072,000 samples; a live-radio source keeps the shallow
  `runtime.queue_samples`.

Both default to the previous behaviour.

## srsUE patches

The repository pins srsRAN_4G at `eea87b1d8` (`scripts/native/native-workspace.lock.json`). Apply in order with `git am`
/ `git apply` on that commit (tested: applies cleanly and builds with UHD 4.6 and ZMQ on aarch64, gcc 13, `ENABLE_WERROR=OFF`):

1. `0001-official-fixes-for-eea87b1d8.patch`: six official srsRAN_4G commits that the pinned version lacks and the
   CMX500 setup needs: three cmake fixes (Boost 1.83), the aarch64 timing-correction cast (`50564d9c2`), the K_AMF
   derivation and no NSSAI in the registration request.
2. `0002-cmx500-channel-and-realtime-zmq.patch`: 25 files.
   - CMX500 interoperability: SSB index and timing, NR PRACH table, `offsetToCarrier` for DMRS and PUSCH, PUCCH format 0,
     RRC and NAS fixes.
   - Channel emulator hook in the NR SA PHY (`channel.dl.*` / `channel.ul.*`).
   - Real-time ZMQ options for a live radio: `rx_sleep=false`, `tx_async=true`, `rx_max_buffered=<samples>`.
   - Frame number follows the MIB after the bridge skips whole frames.

Not included: the multi-UE random-access fixes that exist in a private copy of this srsUE (random preamble, contention
resolution identity check). Official srsRAN_4G has equivalent changes after `eea87b1d8`.

## Status and measured limits (2026-10-06)

Attach, registration and PDU session work through the GPU channel, and a downlink SNR sweep tracks the SNR set in the
channel. The uplink timing margin is the open problem: srsUE prepares slot n+4 while it decodes slot n, and the ZMQ hops
leave a median margin of about -0.1 ms against the 0.2 ms the X310 needs in connected mode (66 % of uplink blocks late,
half of the uplink samples not sent; the CMX still decodes at uplink MCS 2). The GPU is not the bottleneck: 45 us per
0.5 ms block. A container without TUN gets no IP address, so there is no ping or iperf yet.
