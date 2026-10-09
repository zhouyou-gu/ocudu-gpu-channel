# X7 — OAI nrUE uplink levels (2x2 per port) and the PRACH timing-advance mis-estimate

Status: 2026-10-01, measurements on the DGX Spark (`gpuch-xt-release` broker, pinned
OCUDU CPU gNB, local OAI nrUE build). Two open items from the uplink-scale work
(`scripts/native/README.md`, "OAI nrUE gates"): the 2x2 gate's per-port wire level
was assumed equal to the 1x1 value 1.17e-5 but unmeasured, and one scaled 1x1 run
got a 14 us PRACH timing advance from the gNB.

## 1. OAI 2x2 per-port wire level

### Setup

`run-ocudu-oai-2x2.sh` (defaults: broker path, DL rank 2, stock 1-layer uplink,
DL UDP iperf 80M) with a broker wire capture of 69120000 samples (3 s) per port
and direction after a 2 s skip from each port's first sample -- the same window
the 1x1 constant was measured in (`oai-1x1/20261001T110037Z`). Run
`oai-2x2/20261001T114323Z`, topology rendered with `tx_scale_db: 22.810` on both
UE ports (the shared derivation from `OAI_UE_TX_POWER = 1.17e-5`).

Gate verdict with the scaled topology: `event=oai_2x2_summary dl={'ri1_new': 10,
'ri2_new': 15066} nack_ratio=0.0 pusch={'OK': 697} ... dl_rx_mbps=79.99
rt_factor=0.9997 dl_rx_mbps_air=80.01 lost=0 health=pass`, `event=native_oai_2x2_run
status=0`; 0 PUSCH CRC KO; PRACH `idx=14 ta=0.78us detection_metric=25.0
power_dB=-16.85`; Msg3 `crc=OK sinr=43.3dB`.

### `wire-capture-power.py` (active = samples above 1e-3 of the port's peak power)

| Port | Direction | active fraction | active mean | dB | peak amplitude |
|---|---|---|---|---|---|
| gnb0_p0 | tx_in | 0.300 | 1.584e-2 | -18.0 | 0.682 |
| gnb0_p1 | tx_in | 0.250 | 1.627e-2 | -17.9 | 0.623 |
| ue0_p0 | tx_in | 0.282 | 2.152e-6 | -56.7 | 0.0076 |
| **ue0_p1** | **tx_in** | **0.0** | **0 (all zeros)** | -- | 0.0 |
| gnb0_p0 | rx_out | 0.282 | 2.352e-4 | -36.3 | 0.080 |
| gnb0_p1 | rx_out | 0.282 | 2.055e-5 | -46.9 | 0.024 |
| ue0_p0 / p1 | rx_out | 0.30 / 0.28 | 1.108e-2 / 8.60e-3 | -19.6 / -20.7 | 0.54 / 0.53 |

For comparison the 1x1 calibration port (`ue0.tx_in`, same window) had active
fraction 0.0086 and active mean 1.168e-5 (-49.3 dB).

**What the two UE ports carried.** With the gate's stock uplink (no
`OAI2X2_UL_MAX_RANK`, 1 PUSCH layer, no SRS) the OAI nrUE puts every uplink
channel -- PRACH (`nr_prach.c` writes `txData[0]` only), PUCCH and PUSCH -- on
antenna port 0; port 1 is bit-exact zero for the whole 3 s. The gNB's second
port only hears port 0 through the fixed 2x2 matrix (-10.6 dB below port 0).

**Per-channel level, like for like.** The raw active means differ by 7.4 dB, but
not because the UE emits differently. A per-burst breakdown (0.1 ms blocks,
bursts classified by duration and 90 % bandwidth, sample-weighted means):

| Burst class | 1x1 `110037Z` ue0 | 2x2 `114323Z` ue0_p0 |
|---|---|---|
| 0.3 ms PUCCH (SR), 0.1 MHz | -60.21 dB (78 % of active samples) | -60.52 dB (4 %) |
| 1.0-1.1 ms narrow PUCCH/PUSCH, 0.1 MHz | -57.20 / -57.17 dB | -57.63 / -57.41 dB |
| 1.1 ms PUSCH, 0.6-0.7 MHz | -- | -52.92 dB |
| 1.1 ms PUSCH, 7 MHz (attach / ping) | -42.55 dB (7.7 %) | not in window |
| continuous PUCCH, 10-19 ms runs (HARQ-ACK during DL iperf) | -- | -56.9 dB (86 %) |

Same channel, same level within 0.3 dB: the nrUE's digital amplitude per
resource element (`AMP`, 512) does not depend on the antenna count, so the
per-port wire level only follows the allocation width, as the 1x1 note says.
The 2x2 window simply held a different traffic mix -- from t = 2 s the DL iperf
is running and the uplink is HARQ-ACK PUCCH in 95 % of the samples (active
fraction 0.95 in the last 0.5 s), while the 1x1 window held the wideband
attach/ping PUSCH that dominates the 1.17e-5 figure (1x1 0.5 s segments
alternate between 2.7e-6 and 1.7e-5).

**Decision.** The per-port level is the 1x1 level (< 0.5 dB per channel, far
inside the 2 dB criterion), so the 2x2 renderer keeps the shared
`OAI_UE_TX_POWER` / `+22.81 dB` and the env/CLI overrides; no
`OAI_UE_TX_POWER_2X2`. What changes is the knowledge: with the stock uplink the
2x2 UE radiates from one port only, so the emitted power per UE is the 1x1
one, not twice it. The renderer docstring records the measurement, and
`tests/integrations/test_oai_renderers.py` pins the 2x2 default scale to the 1x1 one on both
ports with the overrides intact.

**UL-MIMO variant** (`OAI2X2_UL_MAX_RANK=2`, `OAI2X2_IPERF_DIR=both`, same
capture window), run `oai-2x2/20261001T115512Z`: verdict `nack_ratio=0.0
pusch={'OK': 9794}` (0 KO) `dl_rx_mbps=80.0 ul_rx_mbps=59.93 ul_tbs_per_prb_p50=183.7
ul_ri=[1.5, 2.0] health=pass`, PRACH `idx=6 ta=0.78us metric=25.0`. Here both
ports transmit, each at the same per-RE level:

| Burst class | ue0_p0 | ue0_p1 |
|---|---|---|
| 0.2 ms, 6 MHz (2-port periodic SRS, 10 ms) | -50.92 dB | -50.92 dB |
| 1.0-1.1 ms, 0.3 MHz (2-layer PUSCH, one layer per port) | -55.84 dB | -56.01 / -55.61 dB |
| 1.9 ms, 5 MHz (UL iperf PUSCH) and PUCCH 0.3 / 0.9 ms | -55.9 / -60.5 / -57.4 dB | -- (port 0 only) |
| tool active mean / active fraction | 2.63e-6 / 0.236 | 8.65e-6 / 0.025 |

The tool's per-port active means differ by 5 dB only through the mix (port 1
is half SRS by samples, port 0 mostly PUCCH); per channel the two ports are
within 0.2 dB of each other and of the 1x1 level. Consequence for the emitted
power: with rank-2 PUSCH the UE radiates two layers at full per-RE amplitude
(+3 dB per UE for those slots) and SRS on both ports, while PRACH, PUCCH and
rank-1 PUSCH stay on port 0. `tx_scale_db` is per port, so the shared constant
is the right per-port number for both variants.

**Validation run of the new gate knob** (`OCUDU_NATIVE_OAI2X2_WIRE_CAPTURE_SAMPLES=69120000
_SKIP_SECONDS=0`, so the window starts at the UE's first sample and holds RA, attach
and the pings as the 1x1 calibration did), run `oai-2x2/20261001T120129Z`, edited
scripts on the Spark tree: verdict `nack_ratio=0.0 pusch={'OK': 708} dl_rx_mbps=79.99
rt_factor=0.9996 lost=0 health=pass`, `status=0`, `run-params.txt` carries
`wire_capture_samples=69120000 / wire_capture_skip_seconds=0`, 9 capture files.
PRACH `idx=28 ta=0.78us metric=25.0`. UE port 0 in this like-for-like window: the UE
is silent until sync (t < 1 s), PRACH at 1.194 s at -49.5 dB (= the constant), then
active fraction 0.03-0.06 and a traffic-weighted active mean of **3.17e-6 (-55.0 dB)**,
5.7 dB under the 1x1 constant; port 1 again all zeros. Per channel the levels are
the 1x1 ones once more (0.3 ms PUCCH -60.52 dB, 1-3 ms narrow -57.2 dB, 1 ms
0.6-0.7 MHz PUSCH -52.5 dB); what is missing is the 1x1 window's 1.1 ms / 7 MHz
PUSCH class at -42.6 dB that carried 7.7 % of its active samples and most of its
energy. That class is the attach signalling scheduled at a low UL MCS: the 1x1
gNB measures Msg3 at -1.9 dB SINR (the 250 Hz CFO below, on 3 PRB QPSK), the 2x2
gNB at 43.3 dB (no cfo model, two RX ports), so the same bytes take a fraction of
the PRBs and, at the nrUE's fixed per-RE amplitude, a fraction of the wire power.
By the literal "traffic-weighted over the gate's pings" definition the 2x2 gate's
number is therefore 3.2e-6, but that number is a scheduler/MCS artefact that would
move again with the CFO fix or with UL-MIMO; the physically invariant quantity --
the per-RE amplitude, i.e. the power of a given allocation -- is identical, and a
separate 2x2 constant would give the same UE a 5.7 dB higher PSD in the 2x2 gate
than in the 1x1 gate for the same allocation. The constant stays shared; both
numbers are recorded here for whoever revisits the convention.

### Files

- `scripts/native/run-ocudu-oai-2x2-inner.sh`, `run-ocudu-oai-2x2.sh`: broker wire
  capture as a first-class knob (`OCUDU_NATIVE_OAI2X2_WIRE_CAPTURE_SAMPLES` /
  `_SKIP_SECONDS`, same semantics as the 1x1 gate's), recorded in `run-params.txt`;
  `OAI2X2_BROKER_EXTRA` with `WIRECAP` keeps working (asking for both fails closed).
- `scripts/native/render-oai-2x2-configs.py`: measurement recorded in
  `render_topology_2x2`; no behaviour change.
- `tests/integrations/test_oai_renderers.py`: `test_2x2_default_scale_is_the_1x1_scale_on_both_ue_ports`.
- README line "The 2x2 gate assumes the same per-port level as 1x1 (unmeasured)"
  is now wrong (measured equal, port 1 silent) -- README is outside this change.

## 2. The 14 us PRACH timing advance (`oai-1x1/20261001T110703Z`)

### gNB detector lines (`gnb-internal.log`, `rsi=1`, `zeroCorrelationZoneConfig 0`, format 0)

| Run | UE scale | preamble sent | detected | TA | metric | power_dB | rssi | RAR TA cmd |
|---|---|---|---|---|---|---|---|---|
| 105317Z | off | 12 | 12 | 0.78 us | 15.2 | -43.57 | -39.2 | 33 |
| 110037Z | off | 15 | 15 | 0.78 us | **4.0** | -44.03 | -39.2 | 33 |
| **110703Z** | +22.8 | 17 | 17 | **14.06 us** | **4.2** | -21.09 | -16.3 | **58** |
| 110703Z (2nd RA, 273.4) | +22.8 | 16 | 16 | 9.38 us | 1.6 | -34.27 | -16.4 | -- |
| 110703Z (3rd RA, 289.4) | +22.8 | 44 | 44 | 0.78 us | 13.5 | -21.05 | -16.3 | -- |
| 111159Z | +22.8 | 10 | 10 | 0.78 us | 14.5 | -20.82 | -16.4 | 33 |
| 111621Z | +22.8 | 12 | 12 | 0.78 us | 15.2 | -20.76 | -16.3 | 33 |
| 2x2 114323Z (no cfo model) | +22.8 | 14 | 14 | 0.78 us | 25.0 | -16.85 | -15.8 | 33 |
| srsUE multi-UE 094515Z | Sionna | 0, 8 | 0, 8 | 0.00 us | 35.8, 40.2 | -15.2, -14.7 | -11.7 | -- |

TA command 58 vs 33 = 25 units x 0.52 us = 13.0 us, i.e. the 14.06 - 0.78 us
offset went into the RAR; the UE then transmitted 13 us early, PUCCH/PUSCH
failed, it re-RA'd twice (the second PRACH, sent with the wrong TA, was only
seen as a -13 dB side peak at 9.38 us, metric 1.6) and recovered on the third.
The preamble index is random per attach, and the pattern is **preamble-, not
level-dependent**: the unscaled run that drew preamble 15 has the same metric
4.0 signature with the main peak winning by luck; the scaled PRACH at -21 dB
is 6 dB *below* the srsUE PRACH the detector handles at metric 36-40, and the
2x2 run at -16.9 dB gives metric 25.

### Wire capture of the scaled run (`ue0.tx_in` / `gnb0.rx_out`, cf32 23.04 MS/s)

The capture (1 s after a 2 s skip) holds the 2nd and 3rd PRACH (633.9 ms and
794.0 ms, 160 ms = 16 frames apart, matching 273.4 and 289.4). Analysis
(`x7-prach-bursts.py`, `x7-levels.py`, `x7-cfo.py` and all run outputs are kept on the
Spark in `/workspace/gpuch/xt/x7/`; the runners there wrap the gates in the shared
`spark-gate.lock`):

| Quantity | UE tx_in | gNB rx_out |
|---|---|---|
| PRACH sequence power | 1.12e-5 (-49.5 dB) | -29.7 dB (-3 dB tap + 22.8 dB) |
| other UL bursts in the window (PUCCH/small PUSCH) | -57.4 dB | -37.6 dB |
| PRACH relative to the gate's constant 1.17e-5 | -0.2 dB | -- |
| in-band fraction (839 bins at OAI position 23918/2) | 0.997 | 0.997 |
| crest factor | 3.6 dB | 3.6 dB |
| root found by brute force | u=769 (idx 16), u=811 (idx 44) | same |
| CP-based CFO of the PRACH | **+351 Hz** | **+601 Hz** |
| correlation side peak at +u^-1 / -u^-1 samples (rel. main) | -8.2 / -13.2 dB | **-0.7** / -9.7 dB |

OAI amplitudes (`openair1/PHY/impl_defs_top.h` `AMP = 1 << 9 = 512`): PRACH is
generated with `tx_amp = AMP` (`phy_procedures_nr_ue.c` nr_ue_prach_procedures,
"TODO: actuate the MAC-requested PRACH power"), PUSCH DMRS/data with `AMP`
and the beta-scaled `AMP` (`nr_ulsch_ue.c` 398/454) -- one fixed digital
amplitude per resource element for every channel, no power control. So PRACH
(839 x 1.25 kHz, 1 MHz wide) is at the per-RE level of a ~6 PRB PUSCH: equal to
the traffic-weighted constant, 7.9 dB above the PUCCH-dominated bursts, and it
scales with `tx_scale_db` exactly like PUSCH; it cannot be scaled separately
per device, and there is no reason to.

### The mechanism: CFO of half a PRACH subcarrier, and a Zadoff-Chu ambiguity

A frequency offset of delta subcarriers on a ZC root u produces correlation peaks
at `k * (u^-1 mod 839)` samples for integer k with amplitude
`sin(pi delta) / (pi |k - delta|)`; the side/main ratio is `delta / (1 - delta)`.
With delta = 601 / 1250 = 0.48 that is -0.7 dB -- exactly the measured side
peak. For u = 60 (preamble 17 with rsi 1) `60^-1 mod 839 = 14`, 14 x 800/839 us
= 13.3 us: 0.78 + 13.3 = 14.1 us, the reported TA. For u = 70 (preamble 15),
`u^-1 = 12` -> a -0.7 dB twin at 11.4 us, main won, metric 4.0 instead of 15.
The OCUDU detector (`prach_detector_generic_impl.cpp`, threshold table entry
`{1 port, 1.25 kHz, format 0, ZCZ 0} = 0.147, margin 5, flag orange`) uses one
108-sample window per root (N_cs = 0: window = CP length, delays below 0.8 of it
accepted), estimates the noise as "window energy minus the candidate peak" and
reports `peak / noise / threshold` -- two equal peaks in the window give
metric ~4 and a coin flip on which one is the TA. Preambles whose `u^-1 mod 839`
lies in 1..86 put the twin *after* the main peak, inside the window:
22 of the 64 preambles (0, 1, 3, 5, 7, 9, 11, 15, 17, 21, 23, 28, 31, 33, 35, 37,
43, 45, 51, 56, 58, 59). For the others (10, 12, 14, 16, 44: u^-1 = 829, 831,
424, 827, 809) the twin lands before the main peak, outside the window, and the
metric is 13-15. There is no detector threshold or level knob that changes
this; `zero_correlation_zone` only narrows the window (the twin then hits the
neighbouring preamble's window instead), and format 0 has one symbol, so the
detector's CFO-robust symbol combining does not apply.

Where the 601 Hz comes from:

- The 1x1 topology's `cfo` model (`cfo_hz: 125`) delivers **+250 Hz**: UE
  `Got synch: carrier off 249 Hz`; DL CP-correlation 318.7 Hz at `ue0.rx_out`
  vs 68.7 Hz estimator bias at `gnb0.tx_in` (same 65 Hz bias on the 2x2 run
  without a cfo model); every UL pair differs by exactly +250 Hz (PRACH 351 ->
  601, PUSCH bursts 1097 -> 1347 and 1078 -> 1328, unscaled 3 s mean 308 ->
  558). Twice the configured value, in both directions -- a broker finding to
  pass to the channel owners (the fixture comment sizes 125 Hz against the
  srsUE cold-Msg3 tolerance, so what the UEs actually get is 250 Hz).
- The OAI nrUE's `--cont-fo-comp 1` (gate default) pre-rotates the uplink by
  `-freq_offset * f_UL / f_DL` (`nr-ue.c` 354-366); the PRACH leaves the UE at
  +351 Hz rather than the -236 Hz that would cancel the channel, so the gNB
  sees 601 Hz instead of ~0 or 250. Whether that is the PID-tracked estimate
  drifting or a sign convention of `nr_fo_compensation` for this ZMQ path is
  not settled by the captures; the 2x2 run (no cfo model) leaves at ~0 Hz.
- The -E 3/4 sampling is not involved: OAI's PRACH at 23.04 MS/s uses the
  exact `Ncp = 2388` / `dftlen = 18432`, the burst is in band (0.997) and all
  correct detections sit at the same 0.78 us (one 1024-point IDFT sample),
  OAI 1x1, OAI 2x2 and the unscaled runs alike.

### Hypotheses

- (a) scaled PRACH too hot -> sidelobe / other index: **no**. Same index
  detected; power -21 dB is 6 dB below srsUE's (-15 dB, metric 36-40) and
  4 dB below the 2x2 run (metric 25); the emulator has no noise floor so the
  metric is level-independent; the unscaled preamble-15 run shows the same
  twin-peak signature (metric 4.0).
- (b) OAI PRACH format / frequency offset vs. gNB expectation: **no**. Format
  0, root/index/position exactly as configured, TA 0.78 us whenever the twin
  is outside the window.
- (c) timing effect of -E 3/4 sampling: **no** (above).
- (d) **CFO of ~0.5 PRACH subcarrier at the gNB (broker 2x cfo + UE UL
  pre-compensation in the wrong direction) + ZC frequency-offset ambiguity on
  22/64 preambles: yes**, quantitatively (side peak -0.7 dB predicted and
  measured, 13.3 us predicted for u=60, 14.06 - 0.78 observed).

### Recommendation (1x1 renderer and gate not touched here)

1. Do **not** lower the UE scale: the level plays no part, and
   `OCUDU_NATIVE_OAI_UE_TX_SCALE_DB=off` only changes which preamble the UE
   happened to draw. Expected hit rate stays ~1 in 3 attaches
   (22/64 x the coin flip) at either level.
2. Remove the uplink pre-compensation for this emulated path:
   `OCUDU_NATIVE_OAI_UE_CONT_FO_COMP=3` keeps the DL software rotation and sets
   the UL pre-rotation to zero (`nr-ue.c` 358), so the gNB sees the channel's
   250 Hz only: delta = 0.2, twin at -12 dB, unambiguous for every preamble.
   **Tested**: `oai-1x1/20261001T120048Z` with `OCUDU_NATIVE_OAI_UE_CONT_FO_COMP=3`
   and a 0-skip 6 s capture; gate `result=pass`. The UE drew preamble 1 (u = 140,
   `u^-1 = 6`, one of the 22 vulnerable indices): detector `idx=1 ta=0.78us
   detection_metric=15.6 power_dB=-18.14`, TA command 33. Capture: PRACH at
   1.194 s, -49.5 dB, CP-based CFO **0.0 Hz at the UE, +250.0 Hz at the gNB**,
   correlation side peak at +6 samples **-12.0 dB** (predicted
   `0.2 / 0.8 = -12.0 dB`), the next ones -15.6 / -19.1 dB; versus -0.7 dB with the
   default `--cont-fo-comp 1`. The detected PRACH power also rose from -21 to
   -18 dB because the main peak now carries sinc^2(0.2) instead of sinc^2(0.48)
   of the energy. The 2x2 run without a cfo model (`120129Z`, preamble 28, also
   a vulnerable index) shows the clean reference: 0.0 Hz at both ends, no side
   peaks above -69 dB, metric 25.0. The 1x1 gate owner can make `3` the OAI
   default for the fixed-channel gates (the DL software rotation of mode 1 is
   kept; only the UL pre-rotation goes), or apply it per run as above.
3. Fix the broker `cfo` model (or the fixture) so `cfo_hz: 125` is 125 Hz; at
   125 Hz even the stock pre-compensation would leave delta <= 0.3.
4. Longer term, an OCUDU detector that handles delta ~0.5 on format 0 would need
   a frequency-hypothesis search (two correlations at +-0.5 subcarrier); not a
   configuration matter.
