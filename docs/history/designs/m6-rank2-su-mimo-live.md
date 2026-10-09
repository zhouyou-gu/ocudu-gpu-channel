> Editorial note (2026-10-09): local paths and workflow narration were normalized for this [public edition](../../development/documentation.md#public-editions); measurements and conclusions are unchanged.

# M6 — rank-2 SU-MIMO 라이브 acceptance 상세 설계

> Historical record; its claims apply to the original date, revision and setup. Closed or historical design: consult current reference and validation before treating a proposal as implemented. Migrated from `docs/plans/m6-rank2-su-mimo-live.md` at `58d3156` without changing recorded measurements.


상위 문서: [`MIMO_MILESTONES.md`](https://github.com/zhouyou-gu/ocudu-gpu-channel/blob/5ffd73ea4afa2295b9a588b0b03a411329e81d9b/MIMO_MILESTONES.md) §2 M6
선행: [`m5-live-integration.md`](m5-live-integration.md) · 배경: [`why-radionode-and-srsue-rank1-ko.md`](../translations/ko/why-radionode-and-srsue-rank1.md)

---

## 0. 이 마일스톤이 존재하는 이유

M0~M5는 성공 기준을 **transport 수준**으로 정의했다. 그 결과 전 게이트가 green이면서도 "선언한 `H`가 실제로 rank-2 전송을 나르는가"를 요구하는 게이트가 하나도 없었다.

더 근본적인 실패는 그 위에 있다. **rank-2 acceptance를 수행할 UE가 존재하는지를 M0 착수 전에 정찰하지 않았다.** 한 시간짜리 조사였고, 다섯 마일스톤이 끝난 뒤에야 물었다. M6은 그 격차를 닫고, 이후 마일스톤은 최종 acceptance 경로를 먼저 정찰한다.

---

## 1. 확인한 사실 — 전부 소스 (2026-08-16)

### 1.1 막힌 계층은 UE 하나다

| 계층 | rank 2 | 근거 |
|---|---|---|
| gNB (OCUDU `a1916edcd`) | **가능** | `pdsch`/`pusch` `max_rank` CLI 옵션, `ue_channel_state_manager::get_nof_dl_layers()`; 상류 srsRAN Project가 2×2 DL 공식 지원(4×4 릴리스 존재) |
| 에뮬레이터 (이 저장소) | **가능** | M5.5가 라이브에서 측정: `max │y − Hx│` = 4.1e−08(DL) / 1.5e−07(UL), 허용 1e−04. 행별 교차항 기여 0.12~0.77 |
| UE (srsUE `eea87b1`) | **불가** | §1.2 |

에뮬레이터는 레이어 개념 자체가 코드에 없다 — 포트 도메인 IQ에 `H`만 적용하므로 **rank에 대해 불가지론적이고, 따라서 이미 rank-2-capable이다.** OCUDU의 DL ZMQ IQ는 precoding 이후 port-domain이라 `H` 적용 경계가 물리적으로 맞다.

### 1.2 srsUE가 정확히 어디서 막히는가 — 두 층이 동시에

| 위치 | 사실 | 결과 |
|---|---|---|
| `srsue/src/stack/rrc_nr/rrc_nr.cc:105` | `phy_cfg.carrier.max_mimo_layers = 1;` **하드코딩** | UE capability가 1 레이어 → **gNB가 애초에 rank 2를 스케줄하지 않는다.** 복호 이전 문제 |
| `lib/src/phy/phch/pdsch_nr.c:537` | 주석 그대로 `// Antenna port demapping ... Not implemented` | 포트 디매핑 부재 |
| `pdsch_nr.c:541` | `srsran_predecoding_single(q->x[0], channel->ce[0][0], ...)` | 단일 포트 등화. `ce[MAX_PORTS][MAX_PORTS]` 배열은 선언돼 있으나 NR 경로가 채우지 않는다 |
| `lib/src/phy/ue/ue_dl_nr.c:234,300,581,624,636` | 추정/복호가 전부 `sf_symbols[0]` | 두 번째 포트 샘플이 추정기에 진입조차 못 한다 |

`srsran_layerdemap_nr`은 **존재한다**(`pdsch_nr.c:543`). 즉 layer demapping은 있는데 그 위(capability)와 아래(포트 디매핑·등화)가 막혀 있어 도달하지 못한다. **어느 한쪽만 고쳐서 우회할 수 있는 종류가 아니다.**

> `max_mimo_layers = 1`은 [`why-radionode-and-srsue-rank1-ko.md`](../translations/ko/why-radionode-and-srsue-rank1.md) §2.2가 "재확인하지 않았다"고 표시한 항목이다. 2026-08-16에 소스에서 확인했다.

### 1.3 OAI nrUE는 되는가 — 핀 커밋에서 직접 읽었다

**핀: `2026.w33` = `2b69bde6aeafe892cda1531a0f0cbba2e37792cd`.** 이 커밋은 조사 시점의 `develop` tip과 동일하므로, **아래 근거는 전부 핀된 커밋 자체에서 나온 것이다.**

| 확인 항목 | 결과 | 소스 |
|---|---|---|
| ZMQ 라디오 다채널 | **지원.** `tx_channels`/`rx_channels`가 주소 **목록**(`STRINGLISTPARAM`), `AssertFatal(num_configured_tx_channels == openair0_cfg->tx_num_channels)`로 채널 수 = 안테나 수 강제, **채널당 독립 폴 스레드** | `radio/zmq/zmq_radio.cpp`, `radio/zmq/README.md` |
| 와이어 프로토콜 | **바이트 수준 동일.** REP: `zmq_recv(&dummy,1)` → `zmq_msg_send(msg)`. REQ: 초기 요청 후 매 수신마다 `zmq_send(&dummy,1)` | `zmq_radio.cpp` |
| 페이로드 | **헤더 없는 cf32 인터리브.** `zmq_msg_init_size(total_samples * sizeof(cf_t))`, 0번지부터 `cf_t` | `zmq_imported.cpp` `zmq_tx_channel::transmit` |
| 수신 버퍼 | `rx_buffer_size = sample_size * 300000` — 우리 배치 23,040 대비 **13배 여유** | `zmq_radio.cpp:56` |
| nrUE DL rank 2 | **한다.** `dl_ch_estimates[(l * fp->nb_antennas_rx) + aarx]`(=[layer][rx] 2차원 채널추정), `nr_dlsch_mmse`(주석: *"For 2x2 MIMO matrix, we compute"*), `nr_dlsch_layer_demapping(Nl, ..., llr_layers[..][Nl][..])` | `openair1/PHY/NR_UE_TRANSPORT/nr_dlsch_demodulation.c` |
| OCUDU 연동 | OCUDU 공식 튜토리얼 존재. OAI 2026.w17이 호환 ZMQ 라디오 도입, **2026.w25 이상** 권장(그 이전은 CSI-RS 켠 상태의 상향에 CSI-PUSCH 다중화 문제) | `docs.ocudu.org/tutorials/oaiue/` |

우리 브로커가 상대에게 요구하는 규약은 **1바이트 더미 요청 → 헤더 없는 cf32 페이로드**가 전부이고(`recv_samples_into`는 `nbytes % sizeof(IqSample)`만 검사), 위 표가 그것을 충족한다.

### 1.4 부수 소득 — M5.4의 교착 구조가 UE 쪽에는 없다

OCUDU/srsRAN은 세션의 **모든** TX/RX 채널을 단일 `radio` 워커로 서비스하고(`create_prio_worker` → `single_worker`), 가득 찬 버퍼에서 **제자리 재시도**를 돌린다. 그 조합이 M5.4의 교착이었다.

OAI는 **채널마다 폴 스레드가 따로**이고, TX는 `queue_.push(msg)`로 큐에 넣어 폴 스레드가 pop한다 — **제자리 재시도가 없다.** 즉 M5.4형 자기 교착이 UE 쪽에서는 구조적으로 발생할 수 없다. massive MIMO(M7)로 갈 때 확장성 벽은 gNB 쪽에만 남는다.

### 1.5 워크스페이스 델타는 작다

| 항목 | 상태 |
|---|---|
| `git_sources` | OAI 항목 없음 → 추가 (현재 7개) |
| `build_profiles` | OAI 없음 → 추가 (현재 bison/gnutls/ocudu/srsran4g/open5gs) |
| deb 오버레이 (89행, `apt-get download` → user-space sysroot, `closure: user-space-overlay-only`) | libconfig-dev, libconfig9, libsctp-dev, libsctp1, libssl-dev, libyaml-cpp-dev, zlib1g-dev, ninja-build **이미 있음**. 부족: **libreadline-dev, libtool** 2개 (libtool은 cmake 빌드에 불필요할 수 있음) |

sudo 없이 user-space로 추가된다 — 하네스의 "의존성 부트스트랩은 user-space 우선" 규칙과 충돌 없다.

---

## 2. 설계

### 2.1 교체가 아니라 **추가**

srsUE를 걷어내지 않는다. `native-workspace.lock.json`의 `git_sources`는 배열이므로 OAI를 **추가**하고, **1×1 srsUE attach 게이트는 회귀 안전망으로 그대로 유지**한다. 지금까지 쌓은 게이트를 하나도 잃지 않는다.

### 2.2 왜 1×1을 먼저 거치는가 — 변수 분리

UE 교체와 rank 2를 한 번에 올리면 실패했을 때 원인이 (a) UE 교체 자체 (b) 다채널 ZMQ (c) rank-2 PHY 중 어디인지 분리할 수 없다. M5.4가 정확히 그 대가를 치렀다(격리 실험 하나가 무효였고 결론이 세 번 바뀌었다).

**M6.2에서 OAI를 1T1R로 먼저 붙여 attach/PDU/ping을 통과시킨다.** 그 지점을 통과하면 이후 실패는 다채널 또는 rank-2로 범위가 좁혀진다.

### 2.3 에뮬레이터가 왜 안 바뀌는가

RadioNode는 "ZMQ 엔드포인트 쌍 N개가 한 라디오"라고만 말하고, 그 N개를 **몇 개의 프로세스가 종단하는지 모르며 알 필요도 없다.** 그래서 UE 교체는 재설계가 아니라 실행 구성 변경이다.

감사 결과: 에뮬레이터 코어(`src/`, `include/`, `apps/`, `tests/`)에 srsUE 결합 **0건** — 매치되는 것은 srsRAN GRC 브로커 설계를 언급한 **주석 4줄**뿐이다. 2-port MIMO 게이트와 `gpu-test-sequence`는 합성 peer/도구를 쓰므로 무관하다. **결합은 1×1 라이브 attach 하네스 한 곳에만 있다.**

### 2.4 무엇을 주장하고 무엇을 주장하지 않는가

- **주장한다**: 실제 OCUDU gNB가 rank 2로 송신하고, 이 에뮬레이터가 선언된 2×2 `H`를 적용하며, 실제 UE PHY가 두 레이어를 공동 추정·등화·복호해 attach와 사용자 평면이 성립한다.
- **주장하지 않는다**: massive MIMO(M7), MU-MIMO, 빔포밍, 4 레이어 이상. 그리고 **처리량 수치는 하드웨어·대역폭·MCS·실행 시간을 명시하지 않으면 결과가 아니다**(하네스의 measured-envelope 규칙).

---

## 3. 작업 순서 (커밋 단위)

| 단계 | 내용 | 산출물 |
|---|---|---|
| **M6.1** | OAI 소스 핀 + 빌드 프로파일 + deb 2개 추가, `nr-uesoftmodem` 빌드(UHD off, ZMQ 라디오 on) | `native-workspace.lock.json` 갱신, `builds/oai-zmq-release`, `check-workspace.sh` 통과 |
| **M6.2** | **1×1 OAI 회귀 게이트** — OCUDU gNB 1T1R ↔ 브로커 ↔ OAI nrUE 1R | `scripts/native/run-oai-1x1.sh` + 검증 스크립트. srsUE 게이트는 그대로 유지 |
| **M6.3** | 2포트 승격 — gNB 2T2R ↔ 2×2 `H` ↔ OAI nrUE 2R, `maxMIMO_layers = 2` | 신규 fixture(gNB conf, OAI conf, 토폴로지), 게이트 스크립트 — **완료 2026-09-28** (§8) |
| **M6.4** | **rank-2 acceptance** + M5.5 행렬 검증 동시 통과 | acceptance 리포트, 뮤테이션 프로브 결과 — **완료 2026-09-28, 조건부** (§8.4) |

각 단계는 exit 게이트를 통과하기 전에 다음으로 넘어가지 않는다.

---

## 4. Exit 게이트

| 게이트 | 판정 |
|---|---|
| OAI 빌드가 핀에서 재현 | `git_sources`의 커밋과 빌드 산출물 해시가 리포트에 기록됨 |
| **1×1 OAI attach** | `rrc_connected` / `pdu_session_established` / `ping_ok` 전부 1, 브로커 strict counter 0 |
| **srsUE 1×1 무회귀** | 기존 게이트 재실행 `status=passed` (UE 추가가 기존 경로를 건드리지 않음) |
| 2포트 해석 | `event=radio_node_resolved id=ue0 ... implicit=false`, OAI가 채널 2개로 기동 |
| gNB가 rank 2를 스케줄 | gNB 로그/메트릭에 **`ri = 2`** 관측 |
| **UE가 2 레이어를 복호** | 2포트 구성에서 attach + PDU + ping 성립, UE 로그에 2-layer 복호 흔적 |
| **행렬이 동시에 맞다** | 같은 실행에서 `verify-mimo-matrix-capture.py` 통과 (`max │y − Hx│` ≤ 1e−04, 행별 교차항 기여 > 0) |
| 처리량 | rank 1 대비 유의한 증가. **하드웨어·대역폭·MCS·실행 시간을 함께 기록** |
| gNB 실시간 위반 없음 | gNB 로그에 `Real-time failure in RF` **0건** |

**행렬 검증과 rank-2 acceptance를 같은 실행에서 동시에 요구하는 것이 핵심이다.** 둘을 분리하면 "rank 2가 돌았지만 우리 행렬이 아닐 수도" 또는 "행렬은 맞지만 rank 1이었다"가 통과한다.

---

## 5. 뮤테이션 프로브 (FAIL 확인 후에만 게이트로 인정)

| 프로브 | 기대 |
|---|---|
| `maxMIMO_layers = 1`로 되돌림 | `ri = 2` 관측 실패 → acceptance FAIL |
| 행렬을 대각으로(교차항 0) | 행렬 게이트 FAIL (M5.5에서 이미 확인: 네 행 전부 0.052~0.284) |
| 행렬을 특이(singular)에 가깝게 | rank 2 유지 실패 또는 처리량 붕괴 — **분리 가능성이 실제로 측정되고 있다는 증거** |
| 형제 포트 epoch를 1샘플 어긋냄 | 행렬 게이트 FAIL (M5.5에서 확인: 네 행 전부 0.066~0.428) |

---

## 6. 리스크

| 리스크 | 대응 |
|---|---|
| OAI nrUE가 OCUDU gNB와 1×1조차 붙지 않는다 | M6.2가 그 지점이다. 붙지 않으면 rank 2로 올라가지 않는다. 핀을 w25까지 내리며 이분 탐색 |
| OAI ZMQ 라디오가 2채널에서 우리 브로커와 어긋난다 | 프로토콜은 소스로 확인했으나 **2채널 동시 동작은 미검증**. M6.3의 첫 실패 지점 |
| gNB가 rank 2를 스케줄하지 않는다 | CSI-RS/RI 보고 경로 문제일 수 있다. `max_rank` 설정과 CSI 설정을 먼저 확인 |
| OAI 빌드가 무겁다 | UHD/SIMD 옵션을 끄고 `nr-uesoftmodem` + ZMQ 라디오만 빌드 |
| 처리량 이득이 미미하다 | 실패가 아니라 **측정**이다. 채널 조건(`H`의 조건수)과 함께 기록한다 |

---

## 7. 이 마일스톤이 닫지 않는 것

- **massive MIMO** — M7. 3GPP는 한 UE에 DL 최대 8 레이어이고, massive MIMO는 MU-MIMO + 빔포밍이라 별개 문제다. 그리고 end-to-end로 되는 오픈소스 스택이 현재 없다.
- **MU-MIMO** — OCUDU 스케줄러에 co-scheduling 경로가 없다.
- **4 레이어 이상** — gNB `pusch_constants::MAX_NOF_LAYERS = 4`, srsRAN_4G `SRSRAN_MAX_LAYERS 4`.
- **srsUE의 MIMO 확장** — 하지 않는다. srsUE는 1×1 회귀 안전망으로만 남는다. 선회하려면 미션 개정이 필요하다.
- **M0의 라이브 부채 2건**(multi-UE / multi-gNB) — 환경 차단이며 M6과 무관하다.

---

## 8. M6.3 / M6.4 결과 (2026-09-28, 워크스테이션 RTX 5090)

브랜치 `oai-2x2`(워크트리 `~/ocudu-work/ocudu-oai-mimo`, `gb10-zero-copy`에서 분기). OAI `2b69bde6`(2026.w33), OCUDU `a1916edc`, 컨테이너 `ocudu-minwoo`. 셀: band 3 FDD, 20 MHz, 106 PRB, 15 kHz, 23.04 MS/s, `mcs_table: qam64`. 핀 트리(OAI·OCUDU)는 한 줄도 바꾸지 않았다.

### 8.1 8월의 차단 요인은 규약 차이가 아니라 OAI 2-layer 등화기의 int16 오버플로다

8월 정찰은 "UE가 ri=2 PDSCH의 93–96%를 NACK, QPSK도 동일, 채널 추정은 샘플 수준까지 정확"에서 멈췄다. 남은 용의는 MMSE / LLR / 디매핑 입력 3단계였다. 이번에 워크스테이션 direct 대조(브로커 없음)에서 똑같이 재현했다(`20260928T055037Z`: ri=2 568건, NACK 96%). 그 뒤 계측 빌드로 원인을 확정했다.

- **계측:** 임시 패치(`docs/evidence/m6.3/oai-zf-compare.debug.patch`, 별도 클론 `~/ocudu-work/oai-debug`에서 빌드, 핀 트리는 그대로)로 OAI가 쓰는 추출 샘플 `y`와 채널 추정 `H`를 받아 float zero-forcing을 따로 계산하고, OAI의 고정소수점 출력과 나란히 찍었다.
- **결과:** float ZF는 깨끗한 64QAM 점(±0.15/0.46/0.77/1.08)을 낸다. 즉 추정과 샘플은 맞다. OAI 출력은 같은 점에 약 48.8k를 곱한 값이라 int16을 넘어 감긴다(1.08 → −12.9k, −0.77 → +27.8k). `docs/evidence/m6.3/zf-vs-oai-equaliser.txt`.
- **메커니즘(소스):** `nr_dlsch_mmse`는 `inv(HᴴH)`가 아니라 **adjugate**를 곱하고 행렬식으로 나누지 않는다(행렬식은 LLR 문턱 `dl_ch_mag`로 넘긴다). 그래서 출력은 `det(G)·x / 2^(log2_maxh−1)`이다(G = HᴴH ≫ log2_maxh). 이 크기는 수신 진폭의 4제곱에 비례하고 `log2_maxh`(≈ log2|H|²/2, 정수)가 한 칸 오를 때마다 1/8로 떨어지는 톱니 모양이다. 진폭이 옥타브 안 어디에 걸리느냐에 따라 멀쩡하기도 하고 감기기도 한다.
- **왜 QPSK도 실패했나:** QPSK 점 0.707 × 48.8k = 34.5k도 넘친다. MCS를 낮춰도 소용없던 이유다.
- **왜 OAI 자체 시뮬레이터(dlsim)는 통과했나:** OAI gNB의 송신 진폭이 작아 톱니의 안전 구간에 있다. OAI의 OCUDU 2×2 CI 설정(`ci-scripts/yaml_files/5g_zmq_radio_2x2_ocudu/ocudu.yml`)에 `amplitude_control.tx_gain_backoff: 22`가 들어 있는 것과 맞아떨어진다. 다만 그 CI는 CSI-RS가 꺼져 있어 rank 2를 스케줄하지 않는다.

**조치:** 코드 수정 없이 gNB 설정 `ru_sdr.amplitude_control.tx_gain_backoff`(OCUDU 기본 12 dB)만 올린다. direct 28 dB에서 ri=2 3103건, NACK 0%(`20260928T055557Z`). 출력 크기는 약 15.9k로 감기지 않는다.

**진폭 창은 토폴로지마다 다르다(실측).** 브로커, 유니터리 H, DL UDP 60M, NACK 비율:

| back-off | 12 (기본) | 16 | 20 | 24 | 28 |
|---|---|---|---|---|---|
| 기준 H(조건수 1.6) | 2.7%(MCS 강하) | – | – | – | 0% |
| 유니터리 H | – | 1.3% | **57–60%** | **0%** | 0% |
| direct(항등 H) | **96%** | – | – | – | 0% |

20 dB 실패의 원인은 계측 빌드의 누적 통계(`docs/evidence/m6.3/equaliser-sign-errors.txt`)로 확인했다. PDSCH마다 `log2_maxh`가 10과 11 사이를 오가고, 10인 PDSCH에서 출력이 포화(|max| 31.4k)해 성분의 4–14%가 부호를 잃는다. 24 dB에서는 `log2_maxh`가 10으로 고정되고 |max| 5.6k, 부호 오류 0이다. **back-off는 토폴로지 이득과 함께 골라야 하는 운영 조건이고, 근본 수정은 OAI 쪽이다**(adjugate 출력을 행렬식이나 추가 시프트로 정규화). 상류 제보 후보로 기록한다.

### 8.2 M6.3 — 2포트 승격: 통과

`scripts/native/run-ocudu-oai-2x2.sh`(+ `-inner.sh`, `render-oai-2x2-configs.py`, `summarize-oai-2x2-run.py`). gNB 설정은 OAI 1×1 게이트의 렌더에서 안테나 2/2, 포트 2쌍, `pdsch.max_rank`, MAC pcap off, TX back-off만 바꾼다. OAI는 `--ue-nb-ant-rx 2 --ue-nb-ant-tx 2`, `uecap_ports2.xml`, ZMQ 채널 2개씩이다. 토폴로지 `use_cases/configs/topologies/ocudu_native/topology.ocudu.oai-2x2*.cuda.yaml`에서 gNB와 UE는 모두 2포트 radio node다.

- 2포트 해석: `radio_node_resolved id=ue0 tx[0]=ue0_p0 tx[1]=ue0_p1 ... implicit=false`
- gNB가 UE의 RI=2 CSI 보고(PUCCH F2 `csi1=1…`)를 따라 **ri=2를 스케줄**한다. 로그에서 CSI 보고가 바뀌는 슬롯에 맞춰 ri가 바뀌는 것을 확인했다.
- attach + PDU + ping 10/10, `Real-time failure` 0건(이번 세션 전 실행)
- UE 2-layer 복호: ri=2 PDSCH의 HARQ-ACK가 전부 ACK였고, 슬롯당 TBS는 18.9 kB로 rank 1 최대치(9.4 kB)의 2배다. 계측 빌드에서는 `nl == 2` 경로의 등화 출력을 직접 확인했다.

### 8.3 하네스 수리 (OAI 1×1 게이트, 워크스테이션 재가동)

8월 이후 이 호스트에서 처음 돌렸다. 네 가지를 고쳤고, 같은 날 OAI 1×1 `result=pass`(`20260928T054330Z`)와 srsUE 1×1 무회귀 `result=pass`(`ocudu-interop/20260928T063339Z`)를 확인했다.

1. 고정 기준 커밋 `bc88865`이 이 이력에 없다. fixture 바이트가 같은 `0c13a1a`(M6.2)로 바꿨다.
2. gNB 버전 문자열이 `OCUDU gNB (commit a1916ed)` 형식이다. 두 형식을 모두 받도록 했다.
3. `start_group` pgid 경쟁을 고쳤다. Spark에서 쓰던 패치를 inner 스크립트 4개에 적용했다.
4. OAI UE는 작업 디렉터리에 `nrL1_UE_stats-0.log`를 만들지 못하면 assert로 죽는다. userns에서는 cwd가 매핑되지 않은 uid 소유일 수 있어 로그 디렉터리에서 기동한다.

추가로 이 호스트는 자체 MongoDB/Open5GS를 띄우고 있다. 스택은 별도 netns라 충돌하지 않으므로, 이 경우 `OCUDU_NATIVE_ALLOW_HOST_PORTS=1`로 포트 사전검사를 끈다. OAI 빌드에는 sysroot의 `libtoolize` 경로 재배치와 컨테이너의 `xxd`가 필요했다(워크스페이스 쪽 조치이며 저장소 변경은 없다).

### 8.4 M6.4 — rank-2 acceptance: 통과 (조건 명시)

유니터리 2×2 H(`topology.ocudu.oai-2x2-unitary.cuda.yaml`, 이득 0.8, 조건수 1, 교차항 에너지 0.18), back-off 24 dB, 창 75 s, DL UDP 15 s @ 60 Mbit/s(벽시계) 제시. rank 1과 rank 2를 번갈아 2쌍 측정했다.

| 실행 | max_rank | ri=2 / ri=1 PDSCH | NACK | 슬롯당 TBS | 수신(벽시계) | 수신(공중 시간) | 행렬 검증 |
|---|---|---|---|---|---|---|---|
| `062227Z` | 2 | 4506 / 11 | 0% | 18.9 kB | 41.05 Mb/s | **148.4 Mb/s** | 통과 |
| `062350Z` | 1 | 0 / 4819 | 0% | 9.4 kB | 20.41 | **74.4** | 통과 |
| `062514Z` | 2 | 4501 / 10 | 0% | 18.9 kB | 40.93 | **148.6** | 통과 |
| `062637Z` | 1 | 0 / 4847 | 0% | 9.4 kB | 20.46 | **74.4** | 통과 |

- **처리량: rank 2 / rank 1 = 2.00×.** 두 쌍의 차이는 0.2 Mb/s 이내다. ZMQ 락스텝은 벽시계의 약 0.275배로 흐른다. 그래서 공중 시간 처리량은 iperf 수신 구간의 시뮬레이션 슬롯 수로 환산했다(`realtime_factor`). 두 모드 모두 셀 용량에서 포화한다(유실 24%/50%).
- **행렬이 같은 실행에서 맞다:** 브로커 wire capture(SFN 300–310, 100 ms)에서 `verify-mimo-matrix-capture.py` 통과. DL 두 행 `max|y−Hx|` ≤ 3.1e−08(허용 1e−04), 교차항 몫 0.416/0.429. 그 100 ms 안의 PDSCH는 rank-2 실행에서 100/100이 ri=2였다. UL은 행 오차가 7.9e−10 이하로 맞지만, **OAI UE는 1-layer PUSCH를 포트 0으로만 보내서 UE TX 포트 1이 0이다.** `--allow-silent-source ue0->gnb0:1`로 기록했고, UL 2×2는 주장하지 않는다. **(정정, §8.9: UL 1레이어는 UE 한계가 아니라 설정 때문이었다. UL 2레이어도 된다.)**
- **direct 대조(항등 H):** rank 2 144.9 Mb/s(공중 시간). 에뮬레이터 2×2 H를 거쳐도 rank-2 처리량이 줄지 않는다.

**뮤테이션 프로브:**

| 프로브 | 결과 | 판정 |
|---|---|---|
| `max_rank: 1` | ri=2 0건, 74.4 Mb/s | 기대대로 |
| 거의 특이한 H(조건수 107, `-near-singular`) | UE가 여전히 RI=2를 보고(PUCCH F2 CSI 479/489) → ri=2 PDSCH NACK 94%, ping 실패(`062800Z`) | 분리 가능성이 실제로 측정되고 있다(처리량 붕괴). 단 **OAI UE의 RI 판정은 이 진폭에서 특이 채널을 걸러내지 못한다** |
| back-off 12(OCUDU 기본), direct | NACK 96% | 8월 증상 재현 = 원인이 진폭임을 확인 |
| back-off 20, 유니터리 H | NACK 57–60%, 부호 오류 4–14% | 톱니 창 확인(§8.1) |

**조건과 한계:**
- back-off 24 dB는 **이 토폴로지 이득(0.8)에 맞춘 값**이다. 다른 H나 경로 손실을 쓰면 NACK와 RI 안정성을 다시 확인해야 한다. 기준 H(조건수 1.6)와 유니터리 H @28 dB에서는 OAI의 RI 판정(CSI-RS 조건수 < 5 dB, `csi_rx.c`)이 RI 1/2를 오가서, rank 2는 102–106 Mb/s에 그쳤다.
- 실시간이 아니다(락스텝 0.275배). 브로커 `rx_starvations`는 75 s마다 약 12.3k로 두 모드가 같다(M6.2의 soft 신호와 같은 성격). **→ §8.5에서 원인(OAI ZMQ 드라이버의 응답 지연)을 확인했고, 패치한 드라이버로 실시간(0.999배)이 된다.**
- UL MIMO, 64QAM 초과(qam256), 페이딩 채널에서의 rank 2는 이번 범위 밖이다. (UL MIMO는 §8.9에서 검증했다.)

**재현:**

```bash
docker exec ocudu-minwoo bash -c 'cd ~minwoo/ocudu-work/ocudu-oai-mimo && env HOME=/root \
  OCUDU_NATIVE_ROOT=${HOME}/ocudu-native-workspace CUDACXX=/usr/local/cuda/bin/nvcc \
  OAI2X2_PATH=broker OAI2X2_MAX_RANK=2 OAI2X2_TX_BACKOFF_DB=24 \
  OAI2X2_TOPOLOGY=$PWD/use_cases/configs/topologies/ocudu_native/topology.ocudu.oai-2x2-unitary.cuda.yaml \
  "OAI2X2_BROKER_EXTRA=--wire-capture-dir WIRECAP --wire-capture-samples 2304000 --wire-capture-skip 69120000" \
  bash scripts/native/run-ocudu-oai-2x2.sh'
# 행렬 검증(호스트 /usr/bin/python3에 numpy·yaml 있음)
/usr/bin/python3 scripts/native/verify-mimo-matrix-capture.py --capture-dir <log>/wire-capture \
  --topology <report>/topology.yaml --allow-silent-source 'ue0->gnb0:1'
```

### 8.5 실시간 복원 — OAI ZMQ 드라이버 패치 재측정 (2026-09-28, 워크스테이션 RTX 5090)

M6.4가 벽시계의 0.275배로 흐른 원인은 Spark S9에서 찾은 것과 같다. OAI `radio/zmq/zmq_radio.cpp`의 `tx_poll_thread`는 REP 응답을 보내야 하는데 TX 샘플이 아직 큐에 없으면 `zmq_poll(..., 10)`으로 들어가 응답을 최대 10 ms 늦춘다. 패치 `integrations/oai/patches/oai-zmq-tx-reply-poll.patch`(S9, 이 브랜치 `90acaf6`)는 응답을 기다리는 동안 20 µs마다 큐를 다시 본다.

- **빌드:** 핀 트리는 그대로 두고 `zmq_radio.cpp`만 복사해 `builds/oai-zmq-s9`(패치)와 `builds/oai-zmq-s9-stock`(원본, 같은 절차)로 따로 빌드했다. 컴파일 플래그는 원래 빌드의 `flags.make`, 링크는 `link.txt`와 같다. nrUE 바이너리는 두 경우 모두 `builds/oai-zmq-release`의 것이고 ZMQ 모듈만 `OCUDU_NATIVE_OAI_SHLIBPATH`로 바꾼다(2×2 러너에도 같은 노브를 추가, `72a65ec`).
- **조건:** M6.4와 같다(유니터리 H, back-off 24 dB, rank 2, wire capture). DL UDP 제시율만 200M으로 올렸다(실시간에서 60M은 셀 용량보다 작다). 원본과 패치를 번갈아 2쌍 측정했다. GPU에 다른 프로세스는 없었다.

| 실행 | ZMQ 모듈 | 실시간 배율 | rx_starvations | ri=2 PDSCH | NACK | 수신(벽시계) | 수신(공중 시간) | 행렬 검증 |
|---|---|---|---|---|---|---|---|---|
| `081445Z` | 원본 | 0.277 | 15,077 | 4,524 | 0% | 41.2 Mb/s | 148.6 | 통과 |
| `081623Z` | **패치** | **0.999** | **1** | 15,346 | 0% | **148.2** | 148.3 | 통과 |
| `081802Z` | 원본 | 0.278 | 15,112 | 4,526 | 0% | 41.2 | 148.4 | 통과 |
| `081940Z` | **패치** | **1.000** | **5** | 15,364 | 0% | **148.3** | 148.3 | 통과 |

- **판정:** 원인은 Spark와 같다. 패치로 실시간이 되고 `rx_starvations`는 약 15k에서 1–5로 줄어든다. 공중 시간 처리량(148.3–148.6 Mb/s)은 그대로라서, M6.4의 환산이 맞았다는 것도 확인된다. 이제 벽시계 처리량 자체가 148 Mb/s다. ri=2 PDSCH 수가 약 3.4배인 것은 같은 벽시계 창에 시뮬레이션 슬롯이 그만큼 더 들어가기 때문이다.
- **rank 1(패치, `082806Z`):** 74.1 Mb/s(벽시계), 실시간 배율 1.000, ri=2 0건. rank 2 / rank 1 = **2.00×**가 실시간에서도 유지된다.

**PHY 동작은 바뀌지 않았다 (예상대로).** 드라이버 패치는 타이밍만 바꾸고 PHY에는 손대지 않는다.

| 확인 | 원본 | 패치 | 판정 |
|---|---|---|---|
| back-off 20 dB, 유니터리 H(`082132Z`/`082311Z`) | NACK 52.8% | NACK 51.8% | int16 오버플로(§8.1)는 그대로다. 24 dB가 여전히 필요하다 |
| 기준 H(조건수 1.6), 28 dB(`082449Z`/`082627Z`) | ri=2 36%, 100.7 Mb/s(공중) | ri=2 38%, 102.6 Mb/s | RI 1/2 요동과 102–106 Mb/s 한계도 그대로다 |

- back-off 20 dB에서 처리량은 원본 38.3 Mb/s(공중)와 패치 10.2 Mb/s로 달랐다. NACK 비율은 같다(52%). 원인은 §8.6에서 찾았다: 패치 실행에서만 RLF 해제가 iperf 창 안에 들어왔다. 실패 조건의 처리량이라 판정에는 쓰지 않는다.

**회귀:** OAI 1×1 게이트 원본 모듈 `083218Z` pass(`rx_starvations` 3,363), 패치 모듈 `083259Z` pass(5), srsUE 1×1 `ocudu-interop/083339Z` pass(8).

**하네스 수정(`31ce47b`):** OAI 1×1 게이트는 바깥 스크립트가 `builds/ocudu-gpu-channel-rank1-cuda-release`를 빌드·검사하는데, 안쪽 스크립트는 브로커를 `builds/ocudu-gpu-channel-cuda-release`에서 띄우고 있었다. 이 빌드 디렉터리는 다른 체크아웃(`ocudu-cuda-rebuild`)의 것이다. 이제 둘 다 `OCUDU_NATIVE_CHANNEL_BUILD`(기본 rank1 빌드)를 쓴다. §8.3의 `054330Z` pass는 다른 트리의 브로커로 돈 것이다. srsUE 게이트는 이 호스트에서 `OCUDU_NATIVE_CHANNEL_BUILD=builds/ocudu-gpu-channel-oai2x2-cuda-release`로 돌렸다(기본 빌드 디렉터리가 다른 트리를 가리켜 cmake 구성이 실패한다).

**재현(패치 모듈):**

```bash
docker exec ocudu-minwoo bash -c 'cd ~minwoo/ocudu-work/ocudu-oai-mimo && env HOME=/root \
  OCUDU_NATIVE_ROOT=${HOME}/ocudu-native-workspace CUDACXX=/usr/local/cuda/bin/nvcc \
  OCUDU_NATIVE_OAI_SHLIBPATH=${HOME}/ocudu-native-workspace/builds/oai-zmq-s9 \
  OAI2X2_PATH=broker OAI2X2_MAX_RANK=2 OAI2X2_TX_BACKOFF_DB=24 OAI2X2_IPERF_RATE=200M \
  OAI2X2_TOPOLOGY=$PWD/use_cases/configs/topologies/ocudu_native/topology.ocudu.oai-2x2-unitary.cuda.yaml \
  "OAI2X2_BROKER_EXTRA=--wire-capture-dir WIRECAP --wire-capture-samples 2304000 --wire-capture-skip 69120000" \
  bash scripts/native/run-ocudu-oai-2x2.sh'
```

실행 스크립트와 결과 수집기는 `~/ocudu-work/perf-platform/oai2x2-s9/`(git 밖)에 있다.

### 8.6 패치 모듈 기본화와 20 dB 처리량 차이의 원인 (2026-09-28, 워크스테이션 RTX 5090)

**패치 ZMQ 모듈을 기본으로 (`e254089`).** §8.5의 패치 모듈은 환경 변수로 경로를 줘야만 쓰였다. 이제 OAI 1×1과 2×2 게이트가 기본으로 패치 모듈을 쓴다.

- `scripts/native/build-oai-zmq-module.sh patched|stock`: 핀 소스(`2b69bde6`)의 `zmq_radio.cpp`를 복사해 패치(`oai-zmq-tx-reply-poll.patch`, sha256 `b2f1e51b…`)를 적용하고, CMake가 만든 `flags.make`/`link.txt` 그대로 컴파일·링크해 `builds/oai-zmq-s9`에 넣는다. `MODULE-MANIFEST.txt`에 핀, 패치 sha256, 소스·모듈 sha256을 적는다. 다시 빌드한 모듈은 §8.5에서 쓴 모듈과 **바이트 단위로 같다**(패치 `089b1149…`, 원본 절차 `878432fe…`). 핀 소스 트리와 핀 빌드는 건드리지 않는다.
- `scripts/native/oai-zmq-module.sh`: 게이트가 쓸 모듈을 고른다. `OCUDU_NATIVE_OAI_ZMQ_MODULE=patched`(기본) 또는 `stock`(핀 빌드의 원래 모듈), 명시한 `OCUDU_NATIVE_OAI_SHLIBPATH`가 있으면 그것이 우선한다. 패치 모듈은 manifest의 핀·패치·모듈 해시가 맞을 때만 쓴다(해시를 일부러 틀리게 하면 거부됨을 확인). 고른 모듈은 2×2의 `run-params.txt`(`oai_zmq_module`, `oai_zmq_module_sha256`)와 1×1의 source evidence에 남는다.
- **검증(기본값, 환경 변수 없음):**

| 게이트 | 실행 | 모듈 | 결과 |
|---|---|---|---|
| OAI 1×1 | `oai-1x1/104713Z` | patched | pass, 25 s에 tx_pulls 38,942, rx_starvations 3 (실시간) |
| OAI 1×1 (opt-out) | `oai-1x1/104754Z` | stock | pass, tx_pulls 10,891, rx_starvations 3,345 (원래의 느린 경로) |
| OAI 2×2 유니터리 H, 24 dB | `oai-2x2/104455Z` | patched | 실시간 배율 0.9997, ri=2 15,366, NACK 0%, **148.2 Mb/s**, 행렬 검증 통과(`matrix_capture_status=passed`) |
| srsUE 1×1 | `ocudu-interop/104635Z` | — | pass, rx_starvations 14 |

  처음 1×1 두 실행은 source evidence 단계에서 멈췄다(`exit=1`). 바꾼 파이썬 블록이 `os`를 import하지 않았다. 고친 뒤 위 두 실행이 통과했다. 행렬 검증은 numpy가 있는 호스트 `/usr/bin/python3`로 돌린다(컨테이너 파이썬에는 numpy가 없다).

**20 dB에서 원본 38.3 대 패치 10.2 Mb/s — 원인은 RLF 해제가 측정 창 안에 들어온 것이다.** 20 dB는 int16 오버플로(§8.1)로 rank-2 PDSCH의 약 52%가 NACK인 실패 조건이다. 같은 NACK 비율에서 처리량이 4배 다른 이유를 찾으려고 원본·패치를 번갈아 2쌍, 그리고 측정 창을 공중 시간 기준으로 맞바꾼 대조 2회를 돌렸다(모두 DL UDP `-R`, 200M, GPU에 다른 프로세스 없음).

| 실행 | 모듈 | iperf 창(벽시계) | 실시간 배율 | NACK | TB당 크기 | 0 Mb/s 구간 | 0이 아닌 구간 평균(공중) | RLF → 해제 (iperf 시작 기준, 벽시계) |
|---|---|---|---|---|---|---|---|---|
| `104834Z` | 원본 | 15 s | 0.277 | 52.9% | 10.8 kB | 0 | 37.3 Mb/s | 없음 |
| `105012Z` | 패치 | 15 s | 1.000 | 51.8% | 10.9 kB | 6 | 38.3 | 5.1 s → 9.1 s |
| `105150Z` | 원본 | 15 s | 0.276 | 46.8% | 10.8 kB | 0 | 42.5 | 없음 |
| `105328Z` | 패치 | 15 s | 1.000 | 52.8% | 10.8 kB | 3 | 36.5 | 7.7 s → 11.7 s |
| `105506Z` | 원본 | **54 s**(공중 약 15 s) | 0.276 | 53.0% | 10.7 kB | 0 | 37.4 | 없음 |
| `105705Z` | 패치 | **4 s**(원본 15 s 창과 같은 공중 시간) | 1.000 | 54.1% | 10.8 kB | 0 | 41.7 | 없음 |
| `082132Z`(§8.5) | 원본 | 15 s | 0.277 | 52.8% | 10.8 kB | 0 | 38.3 | 5.9 s → **20.4 s** (창 밖) |
| `082311Z`(§8.5) | 패치 | 15 s | 1.000 | 51.8% | 10.8 kB | 6 | 39.5 | 5.6 s → 9.6 s |

- **링크 자체는 같다.** NACK 비율(47–54%), rank 2, TB당 크기(10.7–10.9 kB)가 모드와 무관하게 같다. 그래서 HARQ 재전송 타이밍, 링크 적응(OLLA), rank 폴백은 원인이 아니다. 해제 전 구간의 처리량(패치 36.5–39.5 Mb/s)도 원본의 공중 시간 처리량(37.3–42.5 Mb/s)과 같다.
- **차이는 해제다.** 20 dB에서는 KO(NACK·DTX)가 길게 몰려 온다. NACK의 절반 이상이 20회 이상 연속된 묶음 안에 있고, 가장 긴 묶음은 두 모드 모두 79–99회다. 독립적인 52% NACK이라면 100회 연속은 사실상 불가능하므로(0.52¹⁰⁰), 오버플로 구간이 몰려서 생긴다(`log2_maxh` 전환, §8.1). gNB는 KO가 100회 연속되면 `RLF detected. Cause: 100 consecutive HARQ-ACK KOs`를 찍고 4,000 ms 타이머 뒤 UE를 해제한다. **이 타이머는 슬롯으로 센다**: 원본 `082132Z`에서 RLF부터 만료까지 벽시계 14.46 s = 4.0 s ÷ 0.2766, 패치에서는 정확히 4.000 s다.
  - 패치(실시간): 15 s 창 세 번 모두 5–8 s에 RLF가 나고 4 s 뒤 해제되어, 창의 마지막 3–6 s가 0 Mb/s다. 이것이 평균을 10–12 Mb/s로 끌어내린다.
  - 원본(0.277배): RLF가 난 한 번(`082132Z`)도 해제가 벽시계 20.4 s, 창이 끝난 뒤였다. 나머지 원본 세 번은 RLF 자체가 없었다.
  - 대조: 패치를 4 s 창으로 돌리면 RLF 전에 끝나 41.7 Mb/s, 원본과 같다.
- **판정:** 38.3 대 10.2 Mb/s는 링크 성능의 차이가 아니다. 느린 원본 실행에서는 RLF 해제가 측정 창 밖으로 밀려나 보이지 않았을 뿐이다. 실시간에서는 20 dB의 오버플로 묶음이 몇 초 안에 RLF와 UE 해제로 이어진다. 즉 20 dB는 처리량이 낮은 조건이 아니라 **연결이 끊기는 조건**이다.
- **확인하지 못한 것:** RLF가 실시간에서 더 잘 나는지는 정하지 못했다. 패치 4/4, 원본 1/4이지만, 원본 54 s 창(공중 약 15 s)에서도 RLF가 없었고, 가장 긴 묶음(98–99회)이 두 모드 모두 임계값 100 바로 아래에 있어 표본이 작다.

드라이버와 원시 데이터: `~/ocudu-work/perf-platform/oai2x2-f/`(git 밖).

### 8.7 24 dB back-off 대신 OAI 로컬 수정 — 2-layer 등화기 int16 오버플로 (2026-09-28, 워크스테이션 RTX 5090)

**무엇을 하려 했나.** §8.1의 결함 때문에 rank 2는 gNB 송신을 24 dB 낮춰야만 동작했다. 안전한 값은 채널 행렬에 따라 달랐고(20 dB는 NACK 52%, §8.6에서는 RLF로 UE가 끊긴다), OCUDU 기본값(12 dB)으로는 쓸 수 없었다. 방침은 OAI를 고치되 **핀 트리는 건드리지 않고 빌드할 때 로컬 패치를 적용**하는 것이다. 업스트림 제보는 마지막에 한다.

**어디서 깨지는지 — 코드 위치.** `nr_dlsch_mmse`(`openair1/PHY/NR_UE_TRANSPORT/nr_dlsch_demodulation.c`)는 A = HᴴH를 만든 뒤 `det(A)`와 `adj(A)·(Hᴴy)`(= `det(A)·x`)를 `mult_complex_vectors`로 계산한다. 이 함수(`openair1/PHY/TOOLS/tools_defs.h:337`)는 32비트 곱을 `shift`만큼 민 뒤 **하위 16비트만 남긴다**. 포화(saturation)가 없어서 범위를 넘으면 값이 감긴다(wrap). shift는 슬롯마다 정해지는 `log2_maxh − 1`이다. 수신 전력이 `log2_maxh` 한 단계 안에서 높은 쪽에 있으면 `det`와 출력이 int16을 넘는다. 그러면 등화된 심볼과 LLR 기준값(`det × QAM 진폭`)의 부호가 뒤집힌다. §8.1에서 본 "float ZF × 48.8k"는 `det` 자체가 int16을 넘은 값이었다.

**수정 (`integrations/oai/patches/oai-nr-dlsch-mmse-scale.patch`, sha256 `1856ac0f…`).** shift를 데이터로 정한다. 모든 중간 곱은 대략 `2·max|A|²·|x|` 이하이므로, `max|A|² >> s ≤ 2¹³`이 되는 s를 쓰고, stock shift가 충분하면 stock shift를 그대로 쓴다. 출력과 LLR 기준값이 같은 비율로 줄어서 LLR의 기하는 바뀌지 않는다. 추가 shift가 필요 없는 조건에서는 stock과 비트 단위로 같다. 이 함수는 다중 레이어일 때만 불린다(`nl > 2 || (nl == 2 && !do_ml)`, 같은 파일 1021행). 그래서 rank 1과 1×1 경로는 코드상 영향이 없다.

**빌드.** `scripts/native/build-oai-ue-local.sh`는 핀 트리를 `git clone --shared`로 `src/oai-local`에 복제하고, 기록된 UE 패치를 적용해 `builds/oai-zmq-local`에 빌드한다(`build-oai-ue.sh`와 같은 플래그·타깃, 일반 사용자 uid 1001). `BUILD-MANIFEST.txt`에 핀, 패치 sha256, `nr-uesoftmodem` sha256을 적는다. 핀 트리와 `builds/oai-zmq-release`는 그대로다. 모든 로컬 OAI 패치와 해시는 `scripts/native/oai-local-patches.sh` 한곳에 있다(ZMQ 패치 포함).

**등화기 수준 검증 (계측 빌드).** 계측 클론 `~/ocudu-work/oai-debug`(float ZF와 비교해 부호 오류율을 찍는 `M63STAT`)에 같은 패치를 얹어 다시 빌드했다(`~/ocudu-work/oai-debug-build`, uid 1001).

| 조건 | 부호 오류율 | 최대 \|출력\| | rank-2 NACK |
|---|---|---|---|
| stock, 20 dB (`063013Z`, §8.1) | 3.0–15.2% | 31.4k (int16 끝) | 52% |
| **패치, 20 dB** (`110618Z`) | **0.0000** | 8.8k | **0%** (148.3 Mb/s) |
| **패치, 12 dB** (`110757Z`) | **0.0000** | 6.6–7.3k | 1.2% |

**라이브 검증.** stock UE(`builds/oai-zmq-release`)와 패치 UE를 번갈아 돌렸다. 모든 실행이 실시간(배율 0.999–1.000)이었고, 패치 ZMQ 모듈을 썼으며, GPU에 다른 프로세스는 없었다. 1차는 빌드 디렉터리를 직접 지정했고, 2차는 게이트 기본값(`OCUDU_NATIVE_OAI_UE` 미지정 → local, `=stock`으로 opt-out)으로 돌렸다.

| 조건 | stock UE | 패치 UE | 판정 |
|---|---|---|---|
| 유니터리 H, **20 dB** | NACK 50.8% / 54.2%, RLF 2회 / 2회, **11.7 / 11.1 Mb/s** | NACK **0% / 0%**, RLF 0, **148.3 / 148.2 Mb/s** | 결함 해소 |
| 유니터리 H, **12 dB**(OCUDU 기본) | NACK 1.3% / 1.4%, TB 12.5 kB, **96.2 / 96.1 Mb/s** | NACK 1.1% / 1.2%, TB 15.3–15.5 kB, **119.1 / 117.5 Mb/s** | 패치가 +22% |
| 유니터리 H, 24 dB | NACK 0%, 148.25 Mb/s | NACK 0%, 148.21 Mb/s | 같음 (추가 shift 불필요) |
| 기준 H(조건수 1.6), **12 dB** | rank 2 66% / 66%, UE의 RI=1 보고 247 / 266회, **71.0 / 70.6 Mb/s** | rank 2 100% / 100%, RI=1 보고 4 / 4회, **90.4 / 90.9 Mb/s** | 패치가 +28% |
| 기준 H, 28 dB | rank 2 38% / 39%, 102.4 / 102.6 Mb/s | rank 2 52% / 56%, 112.8 / 115.3 Mb/s | RI 요동은 양쪽에 남는다 |
| rank 1 고정, 24 dB | 74.11 Mb/s, TB 크기 같음 | 74.08 Mb/s | 영향 없음 (코드 경로 밖) |

- 유니터리 H의 모든 실행(12/20/24 dB, 양쪽)에서 같은 실행의 행렬 검증 `matrix_capture_status=passed`.
- **12 dB 유니터리에서 stock이 느린 이유:** 두 실행의 CSI 보고 분포는 같다(`1001111` 3,733 대 3,726회). 차이는 초반에 있다. stock은 첫 1/5 구간에 NACK 72회(패치 36회)가 몰리고, 이후 구간은 비슷하다(29–33 대 24–28). 그 뒤 gNB가 고른 TB 크기가 실행 끝까지 한 단계 낮게 유지된다(13.3k 대 16.4k). 초반 오버플로 묶음이 gNB의 링크 적응(OLLA) 오프셋을 끌어내리고 천천히만 회복되는 것으로 해석한다. 2회 모두 재현했다.
- **12 dB 기준 H에서 RI 차이(재현 2/2, 원인 미확인):** RI는 UE가 CSI-RS로 따로 계산한다(`csi_rx.c:431` `nr_csi_rs_ri_estimation`, 자체 `log2_maxh`와 행렬식). 이 코드는 패치가 건드리지 않는다. 그런데도 stock UE만 RI=1을 약 7% 보고한다. 간접 결합으로 보이지만 UE의 RI 로그가 이 로그 수준에서 찍히지 않아 메커니즘은 분리하지 못했다.
- **28 dB 기준 H의 RI 요동**은 오버플로와 무관하다(NACK 0%). OAI의 RI 판정(조건수 5 dB 임계값) 문제로 남는다.

**게이트 기본값.** `run-ocudu-oai-2x2.sh`는 이제 패치 UE를 기본으로 쓴다(`OCUDU_NATIVE_OAI_UE=local`). `=stock`으로 되돌릴 수 있고, `OAI2X2_NRUE_DIR`을 주면 그것이 우선한다. local 빌드는 manifest의 핀·패치·바이너리 해시가 맞을 때만 쓴다. `run-params.txt`에 `oai_ue`와 `oai_ue_sha256`을 남긴다. OAI 1×1 게이트는 핀 UE를 그대로 쓴다(1-layer라 이 패치의 경로 밖이다).

**반복 점검 — `scripts/native/check-oai-local-patches.sh [--probe]`.** 로컬 패치마다 (1) 기록된 sha256, (2) 핀 트리에 아직 적용되는지(`patch --dry-run`), (3) 빌드 산출물의 manifest가 핀·패치와 맞는지를 확인한다. `--probe`는 짧은 2×2 실행 4회(약 6분, GPU 단독)로 각 패치가 없을 때 결함이, 있을 때 수정이 보이는지 확인한다. 기준: ZMQ는 stock 실시간 배율 < 0.5, 패치 > 0.9. MMSE는 20 dB에서 stock NACK > 20%, 패치 < 5%.

| 점검 | 결과 |
|---|---|
| 정적 7항목 | 모두 PASS |
| 음성 대조: 패치 문맥 한 줄을 바꾼 사본 | sha256과 적용 두 항목 FAIL, exit 1 |
| `--probe` | ZMQ stock 0.2768 → 패치 0.9991, MMSE stock NACK 0.5437 → 패치 0.0, `result=pass` |

**남은 것.** 12 dB에서도 NACK가 약 1.2% 남는다(패치 후 부호 오류 0, 오버플로는 아님). 원인은 보지 않았다. 12 dB 기준 H의 RI 차이는 메커니즘을 찾지 못했다. 기준 H의 RI 요동은 OAI RI 판정의 별도 문제다. 업스트림 제보(OAI 두 건: MMSE 오버플로, ZMQ 응답 대기)는 마지막에 한다.

드라이버·원시 데이터: `~/ocudu-work/perf-platform/oai2x2-f/`(git 밖). 계측 클론 `~/ocudu-work/oai-debug`에는 계측 코드와 이 패치가 함께 적용되어 있다.


### 8.8 브랜치 통합 — OAI 로컬 패치 도구 하나로 (2026-09-28, `integration-0928`)

`oai-2x2`와 `gb10-zero-copy`(S7–S11)를 합치면서, 두 브랜치가 따로 만든 "패치한 OAI ZMQ 모듈" 도구를 하나로 정리했다. §8.5–§8.7의 이름(`build-oai-zmq-module.sh`, `oai-zmq-module.sh`, `builds/oai-zmq-s9`)은 당시 기록으로 남기고, 지금 쓰는 이름은 아래다.

| 역할 | 남긴 것 | 없앤 것 |
|---|---|---|
| 패치 목록·sha256 (단일 출처) | `integrations/oai/oai-local-patches.lock.json` — `zmq_module`(tx-reply-poll; rx-poll은 `optional`), `ue`(nr-dlsch-mmse-scale) | `oai-zmq-module.lock.json`(이름 변경·확장), `oai-local-patches.sh` 안의 하드코딩 배열 |
| ZMQ 모듈 빌드 | `build-oai-zmq-patched.py` → `builds/oai-zmq-patched` (manifest.json, lock 항목 digest·패치 파일·모듈 해시 대조, `-ffile-prefix-map`으로 재빌드 바이트 동일, release의 다른 모듈 symlink) | `build-oai-zmq-module.sh` → `builds/oai-zmq-s9` |
| 패치 UE 빌드 | `build-oai-ue-local.sh` → `builds/oai-zmq-local` (변경 없음, 목록만 lock에서 읽음) | — |
| 선택기 | `oai-local-patches.sh`의 `resolve_oai_zmq_module`(`OCUDU_NATIVE_OAI_ZMQ_MODULE=patched\|stock`)과 `resolve_oai_ue_build`(`OCUDU_NATIVE_OAI_UE=local\|stock`) | `oai-zmq-module.sh`, 1×1 게이트 안의 인라인 선택 코드 |
| 게이트 기본값 | `oai-gate-defaults.sh` — MPS 재실행, 플랫폼 CPU 배치, 기본값 로그 한 줄. OAI 1×1과 2×2가 같이 쓴다 | 1×1 게이트 안의 인라인 MPS·배치 코드 |
| 점검 | `check-oai-local-patches.sh` — optional 패치까지 해시·적용 확인, ZMQ 모듈은 `build-oai-zmq-patched.py --verify` | `builds/oai-zmq-s9/MODULE-MANIFEST.txt` 대조 |

- **Python 빌더를 남긴 이유:** lock과 패치 파일 자체를 모두 대조하고, 두 번 빌드해도 바이트가 같으며(S11에서 확인), release 빌드의 다른 모듈을 옆에 symlink해 shlibpath 하나로 로더가 모두 찾는다. 셸 빌더는 패치 파일 해시만 봤다.
- **워크스테이션 영향:** 기존 `builds/oai-zmq-s9`는 더 이상 쓰지 않는다. 게이트 전에 한 번 `build-oai-zmq-patched.py`로 `builds/oai-zmq-patched`를 만들어야 한다. Spark의 기존 `builds/oai-zmq-patched`는 manifest 필드가 `lock_sha256` → `lock_section_sha256`으로 바뀌어 `--verify`가 stale로 거부하므로 한 번 다시 빌드한다(같은 입력이라 모듈 바이트는 같아야 한다).
- **2×2 게이트:** 1×1과 같은 기본값(CUDA gNB면 MPS, 플랫폼 CPU 배치, `OCUDU_NATIVE_{GNB,BROKER,NRUE}_WRAPPER`)과 `OCUDU_NATIVE_GNB_BINARY`, `OCUDU_NATIVE_CUDA_ARCH`(GB10은 121), `OCUDU_NATIVE_CHANNEL_BUILD`, `OCUDU_NATIVE_BROKER_STARTUP_ALLOWANCE_SECONDS`를 받는다. 무엇을 적용했는지 `run-params.txt`와 `gate-defaults.log`에 남는다. 이 조합은 아직 라이브로 돌리지 않았다(Spark 2×2, CUDA gNB 2×2).
- **rank-1 2×1/4×1 게이트:** 이 히스토리에 없는 anchor `bc88865` 대신 `f93386b`(해당 fixture·드라이버를 마지막으로 바꾼 커밋)로 대조한다.

### 8.9 UL 2레이어 MIMO — OAI nrUE ⇄ 2×2 행렬 ⇄ OCUDU gNB (2026-09-28, 워크스테이션 RTX 5090, 브랜치 `ul-mimo`)

**정정.** §8.4는 "OAI UE는 1-layer PUSCH를 포트 0으로만 보낸다"고 적고 UL 2×2를 범위 밖으로 뒀다. UE의 한계가 아니었다. 소스를 보면 OAI nrUE는 codebook PUSCH 2레이어를 보내고(`nr_ulsch_ue.c` 1485–1495의 레이어 매핑, `mac_tables.c`의 precoding 표), OCUDU gNB는 UL rank를 `min(nof_antennas_ul, pusch.max_rank, UE가 보고한 pusch_max_rank)`로 정한다(`du_pusch_resource_manager.cpp` 169–179). 1레이어로 묶은 것은 설정 세 가지였다.

| # | 어디서 막혔나 | 증상 | 원인 |
|---|---|---|---|
| 1 | OCUDU의 UE capability 해석 | UE가 2포트인데도 gNB가 1레이어·SRS 1포트로만 설정 | OAI `uecap_ports2.xml`에는 **band 78만** 있다. 셀은 band 3이라 OCUDU `ue_capability_manager`가 이 밴드의 기능을 못 찾고 기본값(`default_pusch_max_rank = 1`)을 썼다 |
| 2 | OAI UE MAC의 자기 capability | 파일의 30 kHz 항목은 `twoLayers`인데 UE는 1레이어로 본다 | UE MAC(`config_ue.c` 2948–2961)은 셀 SCS·대역폭과 맞는 per-CC 항목 중 **마지막 것**의 `maxNumberMIMO-LayersCB-PUSCH`를 쓴다. 15 kHz 항목은 전부 `oneLayer`였다 |
| 3 | OCUDU 스케줄러 | `pusch.max_rank`만 올려서는 rank가 오르지 않는다 | OCUDU는 UL rank와 TPMI를 **SRS 채널 행렬**로 고른다(`ue_channel_state_manager::get_nof_ul_layers`). SRS가 꺼져 있었다 |

**수정 (설정만, OAI·OCUDU 소스 무수정).** `OAI2X2_UL_MAX_RANK=2`를 주면 렌더러가 gNB에 `pusch.max_rank: 2`와 주기 SRS(`srs.type_enabled: periodic`, 10 ms)를 넣고, UE에는 핀된 `uecap_ports2.xml`에서 만든 band-3 파생본을 준다(band 78 → 3, codebook PUSCH 항목 전부 `twoLayers`; SRS 2포트는 원본 그대로). 기본값(변수 없음)은 이전과 같다. 같은 커밋에서 `OAI2X2_IPERF_DIR=dl|ul|both`, `OAI2X2_IPERF_UL_RATE`, 진단용 `OAI2X2_GNB_PHY_LOG=debug`를 추가했고, UL 실행에서는 gNB 로그에 스케줄러 metrics(`ul_ri`)를 켠다. 요약기는 UL 처리량, 새 전송의 PRB당 TBS, `ul_ri`를 낸다.

**판정 근거 세 가지.** gNB INFO 로그의 PUSCH 줄에는 레이어 수가 없어서(debug에서만 `nof_layers`) 다음으로 판정했다.
- **PRB당 TBS:** 64QAM, 새 전송의 PRB당 TBS 중앙값이 1레이어 설정에서 91.9 B, `UL_MAX_RANK=2`에서 **183.7 B**(정확히 2배). 1레이어 64QAM의 상한은 약 92 B/PRB다.
- **스케줄러 `ul_ri`:** 1초마다 1.6 → 2.0.
- **wire capture:** UL 트래픽 중 100 ms에서 UE TX 포트 두 개가 100/100 슬롯 모두 에너지가 있고 RMS가 같으며(0.00956 / 0.00956) 서로 상관이 없다(|상관| 0.0001–0.0006). 두 레이어가 non-coherent TPMI 0(단위 행렬)로 각 포트에 하나씩 실린 모양 그대로다. `verify-mimo-matrix-capture.py`가 **`--allow-silent-source` 없이** UL·DL 두 방향 모두 통과한다(UL 행 오차 ≤ 5.4e−09, 교차항 몫 0.23/0.30).

**A/B** (유니터리 H, DL back-off 12 dB(OCUDU 기본, 패치 UE), UL UDP 제시율 200M, 20 s, 번갈아 2라운드, GPU에 다른 프로세스 없음):

| 실행 | 설정 | UL 수신 | 새 전송 TBS/PRB | `ul_ri` | PUSCH KO | 실시간 배율 |
|---|---|---|---|---|---|---|
| `122226Z` | **UL rank 2** | **101.6 Mb/s** | 183.7 B | 1.6→2.0 | 0 | 0.997 |
| `122415Z` | rank 1 + 2포트 SRS | 45.4 | 91.9 | 1.0 | 2,756 | 0.952 |
| `122554Z` | 기존(변수 없음) | 50.6 | 91.9 | — | 0 | 0.956 |
| `122732Z` | **UL rank 2** | **105.6** | 183.7 | 1.6→2.0 | 0 | 0.996 |
| `122911Z` | rank 1 + 2포트 SRS | 44.2 | 91.9 | 1.0 | 2,756 | 0.953 |
| `123050Z` | 기존 | 51.2 | 91.9 | — | 0 | 0.959 |

- **판정:** UL rank 2가 기존 1레이어 대비 **2.0배**(101.6–105.6 vs 50.6–51.2 Mb/s), 실시간, PUSCH CRC 전부 OK.
- **DL+UL 동시(`both`, DL 200M 15 s 후 UL 200M 15 s, `123823Z`/`124003Z`):** DL 119.0/117.9 Mb/s(ri=2, NACK 1.0%), UL 112.8/107.4 Mb/s, 실시간 1.000. 행렬 검증 양방향 통과(위).
- 처음 실행 `121030Z`(UL 40M 제시): 39.96 Mb/s 수신, 행렬 검증 통과.

**알려진 결함 (대조 설정에서만): rank 1 상한 + 2포트 SRS에서 HARQ-ACK를 실은 PUSCH가 모두 실패한다.** `UL_MAX_RANK=1`(pusch.max_rank 1, SRS 2포트)에서 KO 2,756건은 두 실행에서 건수까지 같고, **전부 HARQ-ACK가 실린 PUSCH**다(ACK 실린 PUSCH 2,756/2,756 KO, CSI만 실린 것 1,162/1,162 OK, UCI 없는 것 전부 OK). PHY debug 실행(`123535Z`)에서 KO PUSCH는 EPRE −90 dB(OK는 약 −40 dB), 두 수신 포트 RSRP −95/−103 dB로 **UE가 그 PUSCH를 아예 보내지 않은 것**으로 보인다. UE 로그에는 이 레벨에서 오류가 없다. UL MIMO 모드(`max_rank 2`)에서는 rank 1로 내려간 슬롯(`ul_ri` 1.6)을 포함해 KO가 0이고, 기본 설정도 KO가 0이라 게이트 기본값과 UL MIMO에는 영향이 없다. 원인(OAI의 maxRank=1·2포트 codebook 경로 또는 DCI 0_1 해석)은 찾지 않았다.

**회귀 (같은 브랜치, 기본값):** 아래 실행 중 `124143Z`부터는 다른 사용자의 Sionna·채널 에뮬레이터 작업이 같은 GPU에서 돌고 있었다. 통과·실패 판정은 유효하지만 시간 수치(실시간 배율 0.89–0.95 등)는 비교에 쓰지 않는다.

| 게이트 | 결과 |
|---|---|
| OAI 2×2 DL, 12 dB(`124143Z`) | 통과, 118.1 Mb/s(공중 시간), NACK 1.0% |
| OAI 2×2 DL, 24 dB(`124322Z`) | 통과, 148.0 Mb/s(공중 시간), NACK 0 |
| OAI 1×1(`124502Z`) | 통과, `rx_starvations` 1 |
| srsUE 1×1(`124542Z`) | 통과, `rx_starvations` 3 |
| rank-1 2×1 srsUE(`125035Z`) | 통과, 행렬 판정 통과 |
| rank-1 4×1 srsUE(`125115Z`) | 통과, 행렬 판정 통과 |

**rank-1 게이트가 돌기까지 고친 것 (`a41c73a`):** (1) 안쪽 스크립트가 브로커를 다른 체크아웃의 `builds/ocudu-gpu-channel-cuda-release`에서 띄웠다 — OAI 1×1 게이트와 같은 버그라 `OCUDU_NATIVE_CHANNEL_BUILD`로 통일했다. (2) 호스트 포트 선검사에 `OCUDU_NATIVE_ALLOW_HOST_PORTS=1`을 추가했다(이 호스트는 자체 MongoDB를 돌린다). (3) gNB 버전 검사가 옛 배너만 받아 현재 빌드(`OCUDU gNB (commit a1916ed)`)를 거부했다. (4) 행렬 판정 스크립트가 컨테이너의 python3에서 numpy·yaml이 없어 실패했다 — `ocudu-minwoo` 컨테이너에 `python3-numpy`, `python3-yaml`을 apt로 넣고 `~/create-ocudu-minwoo.sh`에도 추가했다.

**워크스테이션 CPU 배치 프로파일: 정하지 않았다.** 이 호스트는 Core Ultra 9 285K(P코어 0–7, E코어 8–23)다. A/B를 시작했지만 다른 사용자의 GPU 작업이 겹쳐 시간 비교가 무의미했고, gNB를 P코어 4개(0–3)에 묶은 실행은 gNB가 20 s 안에 뜨지 않았다(`gNB did not start`, 2/2). `platform-profiles.json`은 그대로 두었다(이 호스트에서는 배치 없음). 참고로 이 호스트에서는 `platform-profile.py`의 "가장 빠른 CPU"가 E코어 16·17로 나온다(sysfs가 6.5 GHz로 보고). 프로파일을 만들 때 탐지 지문으로만 쓰고 배치에는 `/sys/devices/cpu_core/cpus`를 봐야 한다.

**재현:**

```bash
docker exec ocudu-minwoo bash -c 'cd ~minwoo/ocudu-work/ocudu-ulmimo && env HOME=/root \
  OCUDU_NATIVE_ROOT=${HOME}/ocudu-native-workspace CUDACXX=/usr/local/cuda/bin/nvcc \
  OCUDU_NATIVE_CHANNEL_BUILD=${HOME}/ocudu-native-workspace/builds/ocudu-gpu-channel-ulmimo-cuda-release \
  OAI2X2_PATH=broker OAI2X2_MAX_RANK=2 OAI2X2_UL_MAX_RANK=2 OAI2X2_IPERF_DIR=both \
  OAI2X2_IPERF_RATE=200M OAI2X2_IPERF_UL_RATE=200M OAI2X2_IPERF_SECONDS=15 \
  OAI2X2_TOPOLOGY=$PWD/use_cases/configs/topologies/ocudu_native/topology.ocudu.oai-2x2-unitary.cuda.yaml \
  "OAI2X2_BROKER_EXTRA=--wire-capture-dir WIRECAP --wire-capture-samples 2304000 --wire-capture-skip 829440000" \
  bash scripts/native/run-ocudu-oai-2x2.sh'
/usr/bin/python3 scripts/native/verify-mimo-matrix-capture.py --capture-dir <log>/wire-capture --topology <report>/topology.yaml
```

실행 스크립트와 로그는 `~/ocudu-work/perf-platform/ulmimo-h/`(git 밖)에 있다.


### 8.10 `ul-mimo` 통합과 DL 건강도 판정 (2026-09-28, 워크스테이션 RTX 5090, `integration-0928`)

**통합.** `ul-mimo`(§8.9)를 S12·J8이 들어간 `integration-0928`에 합쳤다(`98f16a9`). 충돌은 `render-oai-2x2-configs.py`와 `run-ocudu-oai-2x2.sh` 두 곳이었고, 양쪽 노브(S12의 `--bw-mhz`/`--cuda-host-memory`, `ul-mimo`의 `--ul-max-rank`/`--srs-period-ms`/`--gnb-phy-log`)와 run-params 줄을 모두 남겼다. 두 기능이 함께 동작하도록 두 가지를 고쳤다.
- UL rank-2 capability 파생본은 20–50 MHz FDD 셀에서 band 3, 100 MHz TDD 셀에서는 stock의 n78을 유지한다.
- `apply_bandwidth`가 렌더러가 이미 쓴 `uecap.xml`을 stock 파일로 덮어쓰던 것을, 그 파일에 대역폭 항목을 추가하도록 바꿨다. 렌더 확인: 20/50/100 MHz × DL 전용/UL rank 2 여섯 조합 모두 렌더되고, 50 MHz UL rank 2 파일은 band 3 + `twoLayers` + 50 MHz 항목을 함께 갖는다.

**DL 건강도 판정 추가 (`6a283cd`).** S12에서 OAI 1x1 게이트가 attach와 ping만 보고 PDSCH NACK 약 78%인 실행을 통과시켰다. 이제 1x1 verifier는 gNB PUCCH 로그의 HARQ-ACK 비트로 NACK 비율을 계산해 10%(`OCUDU_NATIVE_OAI_MAX_NACK_RATIO`)를 넘거나 비트가 20개 미만이면 실패한다. self-test에 NACK 50% 음성 대조군을 넣었다. 2x2 요약기도 NACK ≤ 10%, PUSCH KO ≤ 10%를 보고 실패하면 3을 반환하고, 게이트는 `OAI2X2_ALLOW_UNHEALTHY=1`이 아니면 exit 3을 낸다. 오늘 2x2 실행 40개에 다시 돌리면 20 dB stock UE 실행(NACK 0.51–0.54), UL `max_rank 1` + SRS 2포트 대조(PUSCH KO 0.108), gNB가 뜨지 못한 실행만 실패로 잡힌다.

**라이브 (통합 기본값: 패치 ZMQ 모듈, 패치 UE, UE RX 이득 −12 dB; GPU에 다른 프로세스 없음):**

| 실행 | 결과 | NACK | 처리량 | 실시간 | y=Hx |
|---|---|---|---|---|---|
| 2x2 DL rank 2, 12 dB (`145629Z`) | 통과 | 0.0 | DL 148.3 Mb/s | 0.9997 | 통과 |
| 2x2 UL rank 2 + DL (`145820Z`) | 통과 | 0.0, PUSCH KO 0 | DL 148.3, UL 111.8 Mb/s (PRB당 183.7 B) | 0.9997 | UL·DL 모두 통과, silent 허용 없음 |
| OAI 1x1 (`150012Z`) | **실패 (새 판정)** | 0.595 (50/84) | — | — | — |
| srsUE 1x1 | 통과 | — | — | — | — |

**OAI 1x1의 NACK — 새 판정이 잡은 것, 원인 미확정.** attach·PDU·ping은 통과하지만 DL NACK이 높다. UE RX 이득을 바꿔 보면 0 dB 77%(101/131), −12 dB 60%, −24 dB 39%(18/46)로 줄지만 없어지지 않는다. 실패는 UE 전용 search space(ss_id 2)의 64QAM PDSCH에 몰려 있고, ss_id 1의 QPSK 첫 전송은 모두 복호된다. 1x1 fixture는 CSI-RS가 꺼져 있어 UE가 CQI 0을 보고하고 gNB가 링크 적응을 하지 못한다. UE가 본 SSB SINR은 34.5 dB라 64QAM이 실패할 신호 조건이 아니다. 2x2 게이트(패치 UE, 수신 2안테나, CSI-RS 켜짐)는 같은 기본값에서 NACK 0이다. 차이 후보는 CSI-RS/CQI 부재에 따른 MCS, UE 바이너리(1x1은 stock `oai-zmq-release`, 2x2는 `oai-zmq-local`), 수신 안테나 수다. 가르지 않았다. → §8.11에서 원인을 찾았다.


### 8.11 OAI 1x1 NACK의 원인: 보상되지 않은 CFO (2026-09-28, 워크스테이션 RTX 5090, `integration-0928`)

**무엇을 하려 했나.** §8.10의 새 판정이 잡은 OAI 1x1 게이트의 DL NACK 약 60%(ping은 통과)를 없애는 것. 후보로 적어 둔 세 가지(CSI-RS/CQI, UE 바이너리, 수신 안테나 수)를 하나씩 가르기 전에, 1x1과 2x2 게이트가 **채널도 다르다**는 점을 먼저 봤다.

**어디서 터졌나.** 1x1 게이트는 srsUE 게이트와 같은 legacy 토폴로지(`use_cases/configs/topologies/ocudu_docker/topology.ocudu-docker.cuda.yaml`)를 쓴다. 이 채널에는 일부러 넣은 **CFO 125 Hz**가 있다(TDL 1탭 −3 dB, 위상 0.125 rad, CFO 125 Hz). 2x2 fixture(unitary/reference 행렬)에는 CFO가 없다. 증상(QPSK 첫 전송은 복호, 64QAM만 실패, SSB SINR 34.5 dB)은 심볼마다 위상이 도는 잔여 CFO와 맞는다: 15 kHz에서 125 Hz는 심볼당 약 0.056 rad, DMRS에서 10심볼 떨어지면 0.5 rad 넘게 돈다.

**추적 (소스, 핀 `2b69bde6`).**
- UE는 초기 동기에서 주파수 오프셋을 잰다. 로그: `Got synch: ... carrier off 250 Hz`(PSS 격자라 거칠다).
- `--cont-fo-comp`가 꺼져 있으면(기본 0) UE는 이 오프셋을 **라디오 재튜닝**(`nrue_ru_set_freq`, `executables/nr-ue.c:222-224`)으로만 고친다. 그런데 ZMQ 라디오의 `zmq_set_freq`는 아무것도 하지 않는다(`radio/zmq/zmq_radio.cpp:270-273`, `return 0`). TRS 기반 보정(`csi_rx.c:1027`, `trs_freq_correction`)도 같은 재튜닝 경로이고, 1x1은 CSI-RS가 꺼져 있어 애초에 돌지 않는다.
- 게이트가 주던 `--ue-fo-compensation`은 초기 동기의 추정 플래그일 뿐(`nr_initial_sync.c:417`) 샘플을 돌리지 않는다.
- 소프트웨어 보정은 `--cont-fo-comp`가 켜졌을 때만 일어난다: 매 DL 심볼 FFT 전에 `nr_fo_compensation`으로 샘플을 돌리고(`slot_fep_nr.c:105-113`), PBCH로 오프셋을 계속 추적하며(`phy_procedures_nr_ue.c:1020-1032`), UL은 그만큼 미리 보상한다(`nr-ue.c:354-361`).
- 결론: ZMQ 경로에서는 채널 CFO가 DL 샘플에서 **한 번도 제거되지 않았다.** srsUE는 자체 CFO 추적으로 같은 채널을 통과한다.
- §8.10의 수신 이득 의존성(0 dB 77% → −24 dB 39%)은 원인이 아니었다: FO 보상을 켜면 0 dB에서도 NACK 0이다.

**수정 (게이트 설정, OAI 소스 무수정).** `oai-gate-defaults.sh`에 `oai_gate_ue_fo_comp`를 추가해 OAI 1x1·2x2 게이트가 UE에 `--cont-fo-comp 1`을 기본으로 준다. `OCUDU_NATIVE_OAI_UE_CONT_FO_COMP=0|1|2|3`으로 바꾸거나 끌 수 있고(0 = 끔), 적용 값은 `gate-defaults.log`, 1x1 `run-params.json`(`ue_cont_fo_comp`), 2x2 `run-params.txt`, `nrue-radio.log`에 남는다. 로컬 패치가 아니라 기존 UE 옵션이므로 `oai-local-patches.lock.json`에는 넣지 않았다.

**라이브 (GPU에 다른 프로세스 없음, UE RX 이득 −12 dB 기본, 게이트 25 s 창).** HARQ 비트 수가 적은 것은 게이트 트래픽이 attach + ping 3회뿐이기 때문이다.

| 실행 | `cont_fo_comp` | DL NACK | 결과 |
|---|---|---|---|
| 기준 (`150012Z`, §8.10) | 끔 | 50/84 (0.595) | 실패 |
| 기준 재현 (`k-fo0-b`) | 끔 | 45/75 (0.600) | 실패 |
| `k-oai1x1-fo1`, `k-fo1-b`, `k-fo1-c` | 1 | 0/29, 0/29, 0/31 | 통과 |
| `k-fo3-a`, `k-fo3-b` (UL 사전 보상 없음) | 3 | 0/27, 0/29 | 통과 |
| `k-fo1-g0` (UE RX 이득 0 dB) | 1 | 0/29 | 통과 |
| **기본값만 (`k-def-a`, `k-def-b`)** | 1 (기본) | **0/29, 0/27** | **통과**, 실시간 |
| 음성 대조군: 끔 (`k-off`) | 0 | 46/74 (0.622) | 실패 (판정이 잡음) |
| 회귀: srsUE 1x1 (`k-srsue`) | — | — | 통과 |
| 회귀: OAI 2x2 DL rank 2, 12 dB (`151800Z`) | 1 (기본) | 0.0 | 통과, 148.3 Mb/s, 실시간 0.9998 |

**가르지 않은 것.** CFO 하나로 NACK이 전부 사라져서 CSI-RS 켜기, 패치 UE, 수신 1안테나 2x2, 고정 MCS 실행은 돌리지 않았다. 1x1 게이트는 여전히 CSI-RS 없이 CQI 0으로 돈다(링크 적응 없음). 게이트 트래픽이 적어(HARQ 비트 약 30) NACK 1% 수준의 차이는 이 게이트로 볼 수 없다.

**다른 게이트에 주는 영향.** Sionna 등 도플러·CFO가 있는 채널에 OAI UE를 붙이는 실행은 모두 같은 문제를 겪는다. `--cont-fo-comp`는 이제 두 OAI 게이트의 기본값이다. S8·S11·S12의 OAI 1x1 "통과"는 이 채널에서 DL NACK이 높았던 실행이다(실시간 여부·브로커 지연 수치는 유효). S12의 Spark 2x2 12 dB NACK 89%는 CFO가 없는 2x2 fixture에서 난 것이라 이 원인과 별개다(수신 레벨 문제, `oai-zmq-rx-gain.patch`).

실행 드라이버와 로그는 `~/ocudu-work/perf-platform/int-j/k-*.log`(git 밖).
