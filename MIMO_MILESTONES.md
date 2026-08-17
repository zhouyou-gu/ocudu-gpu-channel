# MIMO 재구현 마일스톤

기준 코드: pre-MIMO 베이스라인 (이 트리, 12,755줄). 이전 시도(`ocudu-gpu-channel-audit`, +29,296줄)는 참고 자산으로만 사용하고 구조는 계승하지 않는다.

미션 파일은 `AGENT_GOAL.md`를 따른다. 2026-08-17 사용자 지시로 MIMO 개정이 정본에 병합되었고, 병렬 사본 `AGENT_GOAL.mimo.md`는 삭제되었다.

---

## 0. 설계 결정 요약

세 줄로 요약한다.

```text
Device        = ZMQ transport port (socket + TX ring). 기존 코드 그대로.
RadioNode     = 여러 Device의 공통 sample epoch / throttle / 출력 소유자.
PhysicalLink  = RadioNode 쌍 사이의 공동 H / RNG / 절대시간 / correlation 소유자.
```

### 0.1 유지하는 것 — RadioNode overlay

기존 `Device`를 leaf port로 강등하고 상위에 `RadioNode`를 얹는다. `DeviceConfig` 파서, 엔드포인트 필드, puller, `IqRing`, backpressure, strict counter는 손대지 않는다. 기존 SISO YAML은 implicit singleton RadioNode로 lowering되어 의미가 보존된다.

### 0.2 버리는 것 — `RadioNodeCoordinator`

generation ticket / prepare-on-first-request / cached-row dispatch / all-reply barrier / ack / commit / abort / rearm / partial-delivery fault. Nr개 독립 REP 소켓 위에 분산 트랜잭션을 얹은 구조이며, 이전 시도 복잡도의 본체다.

### 0.3 대체 — producer / RX ring

RadioNode마다 **producer 스레드 1개**:

1. 모든 incoming physical link의 **모든 Nt 소스 포트**에 공통 가용한 window 확인
   → `count = min(batch, 공통 가용량, 모든 RX 출력 ring의 여유)`
2. 결합 채널을 **정확히 1회** 호출 → `Nr`개 행 산출
3. 각 행을 해당 RX 포트의 출력 `IqRing`에 push
4. 모든 소스 포트 커서를 **같은 `count`만큼, 한 문장에서** 전진 + 채널 시간 전진 + throttle

`PortRepWorker`는: REQ 수신 → 자기 ring에서 pop(비면 대기, 절대 zero-fill 금지) → 전송. 그게 전부다.

### 0.4 단일 엔진 유지

두 번째 API(`process_mimo_superposition`)를 만들지 않는다. 기존 시그니처를 최소 확장한다.

```cpp
struct SuperpositionInput {
  std::string link_key;
  const ModelConfig* model;
  std::span<const IqSample> samples;
  int rx_port = 0;   // 추가: 이 lane이 더해질 출력 행
  int tx_port = 0;   // 추가: source dedup 키
};

void process_superposition(dst_key, inputs, rx_model, rate,
                           std::span<IqBuffer> outputs);   // 1행 -> Nr행
```

`Nr=1`, `rx_port=0`이면 오늘과 동일한 동작이다. 1×1 레거시는 별도 경로가 아니라 `Nt=Nr=1`인 같은 경로로 내려간다.

### 0.5 GPU 경로 재사용

- `apply_channel_kernel`은 이미 `grid.y = n_links`로 lane별 병렬이다. 2×2 = lane 4개 → **커널 수정 없음**.
- `superpose_kernel`만 `grid.y = Nr`로 확장하고, prepare 시 lane을 행별로 정렬해 `[row_begin[r], row_begin[r+1])` 구간만 합산한다. **약 5줄 diff.**
- Phase 2 D4의 source dedup(`DeviceLinkState::src_index`)이 여기서 처음 실제로 쓰인다. lane이 Nt·Nr개여도 H2D는 **Nt×count**만 올라간다.
- 고정 복소 계수는 새 필드가 필요 없다. `tap_gain_amp / tap_cos_phi / tap_sin_phi`가 이미 `a·e^{jφ}`다.

---

## 1. 커서 정렬 논점 검증

"포트 간 커서가 어긋나면 큰일난다"는 지적은 **맞다.** 다만 그것이 coordinator를 요구하지는 않는다. 코드 근거로 정리한다.

### 1.1 논점이 성립하는 지점

pre-MIMO 브로커를 그대로 두고 2×2를 scalar link 4개로 표현하면, lane은 **두 개의 서로 다른 destination device 서버 스레드**로 쪼개진다.

```text
ue0_p0 서버 스레드 : lane(0,0), lane(0,1)
ue0_p1 서버 스레드 : lane(1,0), lane(1,1)
```

`src/broker.cpp:512-539`에서 각 서버는 **자기 시점에** 가용량을 샘플링한다.

```cpp
std::size_t common = dev.batch;
for (k : incoming) { ... common = min(common, avail); }
```

두 서버가 같은 소스 ring 집합을 보더라도 **샘플링 순간이 다르다.** 서버 A가 t1에 `common=1000`, 서버 B가 t2>t1에 `common=1500`을 얻으면 `cursor(0,*)=1000`, `cursor(1,*)=1500`이 된다. 이후 영구적으로:

- 행 0은 `y_0[n] = H[0,:]·x[n]`, 행 1은 `y_1[n'] = H[1,:]·x[n']`, `n' ≠ n`
  → **하나의 `x`에 대한 `y = Hx`가 아니게 된다.** rank-2 검출의 전제가 깨진다.
- physical link의 Jakes 절대시간(`slot_start_samples`)과 lane별 `delay_line`이 서로 다른 count로 전진 → correlation과 coherent LOS가 무의미해진다.
- 커서 격차가 소스 ring 용량을 넘으면 `broker.cpp:519-523`의 복구 경로가 뒤처진 커서를 `earliest_sequence()`로 점프시키고 `tx_sequence_gaps`를 올린다. **한쪽 행만 샘플을 잃고**, 행 간 상대 정렬이 영구히 깨진다.

즉 정확한 명제는 "초기 커서 정렬"이 아니라 — 초기 co-init은 `broker.cpp:483-492`에서 이미 공통 origin으로 맞춰진다 — **"sibling RX 포트가 매 epoch 동일한 window를 소비해야 하는데, per-destination-device 서버 모델에는 그것을 강제하는 장치가 없다"** 이다.

### 1.2 producer 모델이 해결하는 방식

RadioNode당 스레드가 **하나**이고, 그 스레드가 `count`를 한 번 정하고 채널을 한 번 호출하고 모든 소스 커서를 한 문장에서 전진시킨다. 두 번째 window를 고를 수 있는 스레드가 **존재하지 않는다.**

정렬이 프로토콜로 강제되는 성질이 아니라 **구조적 불변식**이 된다. coordinator도 같은 불변식을 달성하지만, Nr개의 request-driven 스레드를 generation barrier로 직렬화해서 달성한다. 목적은 같고 기구가 훨씬 무겁다.

**결론: 이 지적은 "scalar link Nt·Nr개 + 기존 브로커 유지" 안에 대한 유효한 반박이고, producer 모델에 대한 반박은 아니다.** 요구되는 것은 group cursor의 단일 소유권이며, producer는 그것을 프로토콜 없이 스레드 하나로 제공한다.

### 1.3 producer 모델도 해결하지 못하는 잔여 위험 — TX 포트 원점 정렬

wire payload에는 타임스탬프가 없다. `IqSample`은 `{float i, float q}` 8바이트뿐이고 브로커는 `nbytes % 8 == 0`만 확인한다(`broker.cpp:122-126`). 따라서 **sequence index가 유일한 시간 기준이며 암묵적이다.**

`gnb0_p0`의 sequence k와 `gnb0_p1`의 sequence k가 같은 PHY 순간인지는 **브로커가 검증할 수 없다.** 두 포트가 같은 순간에 스트리밍을 시작했고 어느 쪽도 샘플을 흘리지 않았다는 **배포 전제(deployment precondition)** 다. 이건 coordinator를 써도 동일하게 남는다.

완화책 — 아래 M1 게이트에 포함한다.

1. validator가 sibling 포트의 `tx_timing_offset_samples` 불일치를 거부하고, 그 값을 RadioNode 공통 start offset으로 승격.
2. startup에 resolved 멤버십과 순서를 로그로 출력. 숫자 suffix를 파싱해 순서를 추정하지 않는다.
   `event=radio_node_resolved id=ue0 tx[0]=ue0_p0 tx[1]=ue0_p1 rx[0]=ue0_p0 rx[1]=ue0_p1`
3. **marker 테스트**: 포트 0에만 구별 가능한 패턴을 주입하고, 지정된 lane에서만 나타나는지 확인. 포트 swap과 원점 skew를 동시에 잡는다.

### 1.4 producer 모델이 새로 만드는 위험과 대응

| 위험 | 대응 |
|---|---|
| RX 버퍼링 1단 추가 → 지연 증가 | ring = 2 batch, producer가 room 없으면 생산 차단. 500 µs / 1 ms 게이트에서 **측정 필수**(M0 exit 조건) |
| RX peer 정지 → ring full → node 정지 | 오늘 서버가 `wait_req`에 멈추는 것과 동일 실패 모드. bounded stall detector + 진단 라인만 추가, fault 상태기계는 만들지 않는다 |
| 출력 room 조건이 새 deadlock을 만들 가능성 | B2.2 교훈 계승: 입력도 출력도 **full batch를 요구하지 않는다.** 공통 가용량 ≥1이면 그만큼 생산한다 |
| lane별 `delay_line` 중복 (같은 TX 포트를 읽는 lane들이 동일 내용 보유) | 정확성은 유지됨(기존 D4 주석이 명시). 2×2에서 40 KB/link 수준. 8×8에서 재검토 |

### 1.5 이전 시도에서 계승할 하드-원 사실

`ZMQ_SNDHWM=4`가 libzmq 4.3.5 REP 내부 non-mandatory ROUTER에서 routing-id envelope 단계의 reply를 조용히 버리면서 `zmq_send()`는 성공을 반환한다. strict REQ peer는 영원히 대기한다. **data-plane REP 소켓은 `ZMQ_SNDHWM=0`을 쓴다.** REQ 소켓은 기존 bounded 설정을 유지한다. 1줄이고, 재발견 비용이 크다.

---

## 2. 마일스톤

각 마일스톤은 exit 게이트를 통과하기 전에는 다음으로 넘어가지 않는다. 특히 **M0은 MIMO 수학 없이 단독으로 라이브 검증**한다. 이전 시도는 스키마·coordinator·행렬·CUDA를 한 덩어리로 넣어서 회귀 원인 분리가 불가능했다.

| | 마일스톤 | 상태 |
|---|---|---|
| M0 | 단일 엔진 리팩터 | 완료 (라이브 게이트 2건 환경 차단) |
| M1 | 차원 도입 + 고정 행렬 | 완료 |
| M2 | IID 확률적 페이딩 | 완료 |
| M3 | 공간 상관 + coherent LOS | 완료 |
| M4 | physical link 단위 runtime control | 완료 |
| M5 | 라이브 통합 (transport + 행렬 검증) | 완료 |
| **M6** | **rank-2 SU-MIMO 라이브 acceptance** | **M6.1–M6.2 완료 (2026-08-17). M6.3부터 진행** |
| **M7** | **massive MIMO** | **계획만, 전제 확인 필요** |

**M0~M5에서 배운 계획상의 교훈 하나** — 성공 기준이 transport 수준으로 정의되어 있었기 때문에, 전 게이트가 green이면서도 "선언한 `H`가 실제 rank-2 전송을 나르는가"는 한 번도 요구되지 않았다. 그리고 그 acceptance를 가능케 할 UE가 존재하는지는 M0 착수 전에 확인했어야 했다(한 시간짜리 조사였다). **이후 마일스톤은 최종 acceptance 경로를 먼저 정찰하고 시작한다.**

### M0 — 단일 엔진 리팩터 (MIMO 수학 0줄)

> 상세 설계: [`docs/plans/m0-single-engine-refactor.md`](docs/plans/m0-single-engine-refactor.md)

**범위**
- `RadioNodeConfig` 도입. `radio_nodes` 없는 기존 YAML은 implicit singleton으로 lowering.
- `run_server`를 RadioNode producer + 포트별 RX `IqRing` + 얇은 `PortRepWorker`로 교체.
- `process_superposition` 출력을 `std::span<IqBuffer>`로 일반화. `SuperpositionInput`에 `rx_port`/`tx_port` 추가. 모든 곳에서 `Nr=1`.
- data-plane REP 소켓 `ZMQ_SNDHWM=0`.

**건드리는 파일**: `config.h/.cpp`, `broker.h/.cpp`, `processing.h`, `cpu_backend.h/.cpp`, `cuda_backend.cu`, `ring.h/.cpp`(재사용만), `test_broker.cpp`

**Exit 게이트**
- 기존 ctest 전부 통과 (config, processing, ring, broker, control_server, runtime_update_parity, mutable_params, hardware_probe)
- `gpu-test-sequence.sh` 7/7
- Milestone A / B / C 라이브 스모크 통과, strict counter 0, gNB `Real-time failure in RF: overflow` 0
- **RX ring 지연 측정치 기록** — 500 µs / 1 ms 슬롯 예산 대비 정상 상태 ring 점유와 추가 지연

**비범위**: 행렬, 상관, 다중 포트 스키마

---

### M1 — 차원 도입 + 고정 행렬

> 상세 설계: [`docs/plans/m1-dimensions-and-fixed-matrix.md`](docs/plans/m1-dimensions-and-fixed-matrix.md)

**범위**
- `radio_nodes.tx_ports` / `rx_ports` 명시 블록 리스트. 작성 순서가 canonical matrix index. flow 리스트(`[p0, p1]`)는 거부.
- `links.from/to`가 RadioNode ID를 가리킴. 방향마다 physical link 하나.
- lane 전개 `Nt × Nr`, `lane = r * Nt + t`. CUDA는 lane을 행별 정렬 + `row_begin[]` 업로드.
- model-scope `fixed_mimo`: sparse `coefficients[{tap, rx, tx, real, imag}]`. 생략 lane은 0. 암묵적 `1/sqrt(Nt)` 정규화 없음.
- validator: 존재하지 않는 Device 참조, RadioNode/Device ID 충돌, 한 Device의 복수 parent, 중복 포트, sibling sample-rate 불일치, sibling `tx_timing_offset_samples` 불일치, 행렬 차원 불일치.
- `event=radio_node_resolved` startup 로그.

**Exit 게이트**
- 합성 2-port peer: identity `H` → 입력 그대로, swap `H` → 행 교환, known `H` → 해석적 기대값과 정확 일치
- **marker 테스트** (§1.3): 포트 0 전용 패턴이 지정 lane에서만 출현
- CPU ↔ CUDA 1e-3 parity
- 1×1 레거시 출력이 M0과 bit-exact
- 비대칭 차원(2×1, 1×2) 단위 테스트 통과

**비범위**: 확률적 페이딩의 상관, LOS 결합, runtime control

---

### M2 — IID 확률적 페이딩

> 상세 설계: [`docs/plans/m2-iid-stochastic-fading.md`](docs/plans/m2-iid-stochastic-fading.md)

**범위**
- physical link 하나의 seed에서 lane별 Jakes 파라미터 파생. **절대시간은 physical link가 단일 소유**.
- 기존 `apply_channel_kernel` 페이딩 경로 무수정.

**Exit 게이트**
- lane별 자기상관이 자기 시드가 뽑은 각도의 sum-of-sinusoids와 일치 (±0.10), 그리고 **lane 앙상블 평균**이 `J_0(2π f_d τ)`와 일치 (±0.15). J_0는 앙상블 성질이므로 단일 realization으로는 판정할 수 없다 — 상세는 `docs/plans/m2-iid-stochastic-fading.md` §4
- lane 간 교차상관 ≈ 0
- 청크 크기를 바꿔도 동일 realization (chunk invariance)

---

### M3 — 공간 상관 + coherent LOS

**범위**
- **생성기와 convolution 분리**: Jakes coarse grid `g_grid[lane][tap][gridpoint]`를 별도 소형 커널이 생성 → `L`을 곱해 상관 부여 → `apply_channel_kernel`은 grid를 읽기만.
  - grid 크기: lane × tap × ~11 포인트. 슬롯당 극소.
  - 이후 convolution 커널은 다시 건드리지 않는다. CDL로 확장해도 생성기만 바뀐다.
- `spatial_correlation`: `iid` / `kronecker`(`R_rx`, `R_tx`) / `full`. Hermitian, PSD, unit-diagonal 검증. factor는 prepare에서 complex-double로 1회 분해, hot path 재분해 없음.
- flatten 순서 `lane = r * Nt + t` 고정. 복소 TX correlation의 transpose/conjugation 규약을 스키마와 parity 테스트에서 **한 가지로** 고정한다.
- coherent LOS 평균 행렬: `H_ℓ = sqrt(P_ℓ/(K+1))·H_NLOS,corr + sqrt(P_ℓ K/(K+1))·H_LOS,coh`. lane마다 LOS 위상을 독립 draw하지 않는다.

**Exit 게이트**
- 경험적 공분산이 선언한 `R`과 허용오차 내 일치
- LOS 지배(K 큰 값)에서 포트 간 위상 관계가 선언한 행렬과 일치
- CPU ↔ CUDA parity 유지

---

### M4 — physical link 단위 runtime control

**범위**
- 기존 `BrokerLinkControl` shadow + atomic seqno + slot 경계 snap 메커니즘을 **lane별이 아니라 physical link별로 1개** 배치.
- 스냅샷 단위는 whole-link: tap layout + 행렬 + 상관. 부분 갱신 없음.
- `Nt`/`Nr`, 포트 멤버십, sample rate, fixed↔stochastic family는 runtime 변경 불가 (거부).

**Exit 게이트**
- 교체가 모든 lane에 같은 슬롯에서 원자적으로 적용됨
- 기존 `test_runtime_update_parity` 계열 통과
- warmup(delay-line zero-fill) 계약이 lane 전체에 일관 적용

---

### M5 — 라이브 통합

**범위 / 순서**
1. **1×1 라이브 회귀** — OCUDU gNB ↔ 새 브로커 ↔ srsUE. `rrc_connected` / `pdu_session_established` / `ping_ok` 전부 1, strict counter 0.
   → **M0 직후에 한 번, M4 이후에 다시.** 이 게이트를 통과하지 않은 상태로 다른 기능을 쌓지 않는다.
2. 멀티포트 OCUDU gNB ↔ 합성 2-port UE.
   → **선행 확인 필요**: OCUDU ZMQ의 다중 포트 `device_args` 문법. 이전 감사에 "indexed RF/ZMQ channel 노출 가능"이라고만 기록되어 있고 직접 확인한 바 없다. **소스로 확인하기 전에 이 게이트를 약속하지 않는다.**

**명시적 비게이트**
- srsUE `release_23_11` NR 경로는 `max_mimo_layers = 1`, DL 추정/복호가 `sf_symbols[0]` 기준이다. **이 UE는 rank-2 acceptance gate가 될 수 없다.** 독립 srsUE 프로세스 두 개는 2-port UE 하나가 아니다.
- 진짜 rank-2 주장은 MIMO 지원 UE PHY가 준비된 뒤 별도 게이트로 둔다 (`AGENT_GOAL.md` Non-Goals).
- **→ 그 조건이 2026-08-16에 충족됐다. M6을 참조할 것.** M5의 이 절은 srsUE에 대한 판정으로서 계속 유효하지만, "MIMO 지원 UE PHY가 준비된 뒤"라는 유보는 더 이상 열려 있지 않다.

---

### M6 — rank-2 SU-MIMO 라이브 acceptance (2026-08-16 추가)

> 상세 설계: [`docs/plans/m6-rank2-su-mimo-live.md`](docs/plans/m6-rank2-su-mimo-live.md)
>
> **OAI 핀: `2026.w33` = `2b69bde6aeafe892cda1531a0f0cbba2e37792cd`.** 조사 시점의 `develop` tip과 동일한 커밋이므로, 아래 근거는 전부 핀된 커밋 자체에서 확인한 것이다.

**왜 추가되는가**

M0~M5는 성공 기준을 **transport 수준**으로 잡았다. 그 결과 전 게이트가 green이면서도 "선언한 `H`가 실제로 rank-2 전송을 나르는가"를 요구하는 게이트가 하나도 없었다. 사용자 목표는 **rank-2 SU-MIMO가 동작하는 채널 에뮬레이터**이고, 최종적으로는 massive MIMO다. M6은 그 격차를 닫는다.

**M0~M5가 왜 낭비가 아닌가 (M6의 전제조건이기 때문)**

rank-2 수신은 2×2를 역행렬로 푸는 일이고, 그것이 성립하려면 `H`의 네 계수가 **하나의 realization·하나의 시간원점**을 가져야 한다. 두 포트가 독립 클록으로 흐르면 `H`는 고정 행렬이 아니고 어떤 수신기도 풀지 못한다. RadioNode 오버레이(공통 sample epoch, 단일 producer가 고르는 하나의 윈도, wire까지 같은 경계로 자르는 serve)가 정확히 그 전제를 만든다. **M6은 M0~M5 위에서만 성립한다.**

**핵심 판단: 막힌 곳은 우리 계층이 아니라 UE다 (소스 확인)**

| 계층 | rank 2 가능 여부 | 근거 |
|---|---|---|
| gNB (OCUDU) | **이미 가능** | `pdsch/pusch` `max_rank` 설정, 상류 srsRAN Project가 2×2 DL 공식 지원(4×4 릴리스도 있음) |
| 에뮬레이터 (이 저장소) | **이미 가능** | 2×2 행렬을 라이브에서 `max |y − Hx| = 4.1e−08`(DL) / `1.5e−07`(UL)로 검증 (M5.5) |
| UE (srsUE `eea87b1`) | **불가** | `rrc_nr.cc:105` `max_mimo_layers = 1`(하드코딩), `pdsch_nr.c:537` `// Antenna port demapping ... Not implemented`, 등화가 `srsran_predecoding_single(ce[0][0])` |

상세 근거는 [`docs/why-radionode-and-srsue-rank1-ko.md`](docs/why-radionode-and-srsue-rank1-ko.md) §2. 그 문서가 미재확인이라 표시했던 `max_mimo_layers = 1`은 2026-08-16에 소스에서 확인했다.

**수정안: srsUE를 패치하지 않는다. OAI nrUE를 추가한다.**

OAI `openairinterface5g` develop 브랜치를 직접 받아 확인한 사실:

| 확인 항목 | 결과 | 소스 |
|---|---|---|
| ZMQ 라디오 다채널 | **지원.** `tx_channels`/`rx_channels`가 주소 **목록**(STRINGLISTPARAM), 채널 수 = 안테나 수를 `AssertFatal`로 강제, **채널당 독립 폴 스레드** | `radio/zmq/zmq_radio.cpp`, `radio/zmq/README.md` |
| 와이어 프로토콜 | **바이트 수준 동일.** REP는 1바이트 요청 수신 후 페이로드 송신, REQ는 1바이트 요청 송신 후 수신. 페이로드는 **헤더 없는 cf32 인터리브** (`zmq_msg_init_size(n * sizeof(cf_t))`, 0번지부터 `cf_t`) | `radio/zmq/zmq_radio.cpp`, `radio/zmq/zmq_imported.cpp` |
| 수신 버퍼 상한 | `sample_size * 300000` — 우리 배치 23,040 대비 13배 여유 | `zmq_radio.cpp:56` |
| nrUE가 rank 2를 복호하는가 | **한다.** `dl_ch_estimates[(l * nb_antennas_rx) + aarx]`(=[layer][rx] 2차원 채널추정), `nr_dlsch_mmse`("For 2x2 MIMO matrix, we compute"), `nr_dlsch_layer_demapping(Nl, ...)` | `openair1/PHY/NR_UE_TRANSPORT/nr_dlsch_demodulation.c` |
| OCUDU와의 연동 | OCUDU 공식 튜토리얼 존재. OAI 2026.w17이 호환 ZMQ 라디오 도입, **2026.w25 이상** 권장 | `docs.ocudu.org/tutorials/oaiue/` |

부수 소득: OAI TX는 가득 찬 버퍼에서 **제자리 재시도를 하지 않고**(큐에 넣고 폴 스레드가 pop) 채널마다 스레드가 따로다. **M5.4를 일으킨 자기 교착 구조가 UE 쪽에는 없다.**

**범위: 이 저장소의 C++는 0줄 바뀐다**

| 계층 | srsUE 결합 | M6에서 |
|---|---|---|
| 에뮬레이터 코어 (`src/`, `include/`, `apps/`, `tests/`) | 0건 (참조는 설계 언급 주석 4줄뿐) | **변경 없음** |
| 2-port MIMO 게이트 + `gpu-test-sequence` | 0건 (합성 peer/도구) | **변경 없음** |
| 토폴로지 | UE는 ZMQ 엔드포인트일 뿐 | 포트 수 선언만 (설정) |
| 1×1 라이브 attach 하네스 | 바이너리 경로 / conf 템플릿 / 로그 판정 토큰 / TUN 이름 / USIM / workspace lock / 산출물 검증 | **여기만 신규 작성** |

**srsUE는 제거하지 않는다.** `native-workspace.lock.json`의 `git_sources`는 배열이므로 OAI를 **추가**한다. 1×1 srsUE attach 게이트는 회귀 안전망으로 유지하고, rank-2 게이트를 그 옆에 새로 세운다.

**단계 / Exit 게이트**

| 단계 | 내용 | 판정 |
|---|---|---|
| M6.1 | OAI nrUE 빌드 + workspace lock 항목 추가 | 핀 커밋으로 재현 가능한 빌드, `check-workspace.sh` 통과 — **완료 2026-08-16** |
| M6.2 | **1×1로 OAI 회귀** — OCUDU gNB 1T1R ↔ 브로커 ↔ OAI nrUE | `rrc_connected` / `pdu_session_established` / `ping_ok` 전부 1, strict counter 0. **UE 교체 자체를 rank 2와 분리해 검증한다** — **완료 2026-08-17** (`run-ocudu-oai-1x1.sh` `result=pass` + srsUE 1×1 무회귀 동시 통과; 차단 요인 3건의 규명은 `AGENT_PROGRESS.md` M6.2 절) |
| M6.3 | 2포트로 승격 — gNB 2T2R ↔ 2×2 행렬 ↔ OAI nrUE 2안테나, `maxMIMO_layers = 2` | gNB 로그에 rank 2 스케줄링, UE가 2 레이어 복호, attach + PDU + ping |
| M6.4 | **rank-2 acceptance** | `ri = 2`가 관측되고, rank 1 대비 처리량이 유의하게 증가하며, M5.5의 행렬 검증(`y = Hx`)이 같은 실행에서 동시에 통과 |

**뮤테이션 프로브** (FAIL 확인 후에만 게이트로 인정)
- 행렬을 특이(singular)에 가깝게 → rank 2 유지 실패 또는 처리량 붕괴
- 행렬을 대각으로 → M5.5 행렬 게이트 FAIL (이미 확인됨)
- `maxMIMO_layers = 1`로 되돌림 → `ri = 2` 관측 실패

**미션 관계**

`AGENT_GOAL.md`의 Non-Goal은 rank-2 **구현**을 금지한 적이 없다. 금지 대상은 *"transport-level multi-port flow alone"* 또는 *"several independent single-port UE processes"*에 근거한 **주장**이고, 같은 문장이 유효 조건을 명시한다 — *"a live rank-2 claim requires a UE PHY that jointly estimates and decodes the matrix channel."* OAI nrUE가 그 조건을 충족하므로 **M6은 미션 개정 없이 성립한다.** UE는 Scope의 *"UE-side endpoints ... and local test harnesses"*에 해당한다.

단, 아래 둘은 미션 개정이 필요하다. 에이전트는 자율로 진행하지 않는다.
- srsUE 자체를 패치하는 경로로 선회하는 경우 (UE PHY 확장은 현 Scope 밖)
- 아래 M7(massive MIMO)을 정식 목표로 승격하는 경우

---

### M7 — massive MIMO (계획만, 착수 전 전제 확인 필요)

**먼저 정정해야 할 전제**

**16×16 SU-MIMO는 3GPP NR에 존재하지 않는다.** 규격상 한 UE에 DL 최대 8 레이어(2 codeword × 4), UL 최대 4이며 실제 UE는 보통 4로 제한된다. massive MIMO는 **기지국 안테나 다수 + 빔포밍 + MU-MIMO**로 여러 UE에게 각 1~4 레이어를 주는 구성이다.

**따라서 massive MIMO에는 MIMO UE가 필요 없다.** 단일 안테나 UE 여러 대가 정상 구성이고, 그 형태는 이미 이 저장소의 토폴로지로 표현된다 — `gnb0`에 포트 N개, `ue0..ueK`에 각 1포트. **묶기는 gNB 쪽에서만 일어난다.**

**병목은 UE에서 gNB로 옮겨간다 (외부 조사)**
- srsRAN Project(OCUDU 상류): 최대 4T4R, **SU-MIMO만**. MU-MIMO는 연구에서 MAC+PHY 개조로 구현.
- OAI: 최대 8T8R(4T4R RU 2개). *"Massive MIMO configurations are currently not achievable end-to-end"*, 디지털 빔포밍 구현이 선행 요건.
- **end-to-end massive MIMO가 되는 오픈소스 스택은 현재 없다.**

**우리 쪽 실측 상한**

| 항목 | 현재 | massive MIMO 함의 |
|---|---|---|
| `kMaxCorrelatedLanes` | 16 | 공간상관 링크는 **4×4가 상한**. 8×8(64 lane)은 이 상수부터 |
| ZMQ 대역폭 | 23.04 MS/s × 8 B = 184.32 MB/s / 포트 / 방향 | 16포트 양방향 5.9 GB/s, 64포트 23.6 GB/s — 후자는 TCP 루프백의 벽 |
| OCUDU ZMQ 라디오 스레드 | 세션의 **모든** 채널을 단일 `radio` 워커가 서비스 (`create_prio_worker` → `single_worker`) | **2포트에서 이미 교착이 났다(M5.4).** ZMQ 기반 massive MIMO의 구조적 벽이며 **gNB 쪽에 남는다** |

**M7의 전략 분기 (사용자 결정 사항)**
- **(a) 스택보다 앞서 만든다** — 합성 peer + 해석적 기대로 M×N을 검증하고, 스택이 따라오면 그대로 쓴다. 지금까지의 검증 방식 그대로이며 연구 산출물로서 정당하다.
- **(b) 전송을 바꾼다** — 공유메모리/DPDK 등. 단일 `radio` 스레드 벽과 대역폭 벽 때문에 16포트 이상은 ZMQ로 어렵다.

**착수 전 필수 실측**: 합성 peer로 포트 수를 8 → 16 → 32로 올리며 **ZMQ가 언제 무너지는지**를 먼저 측정한다. 그 숫자가 (b)의 시점을 정한다. 추정으로 정하지 않는다.

---

## 3. 이전 시도에서 살릴 것 / 버릴 것

**살림**
- `ZMQ_SNDHWM=0` 진단 결과 (§1.5)
- 합성 멀티포트 peer 앱 (`apps/ocudu_mimo_transport_peer.cpp`)
- 상관 수학과 통계 테스트 (`tests/test_mimo_correlation.cpp`)
- OCUDU DL ZMQ IQ가 **precoding 이후 port-domain**이라는 소스 감사 결과 — `H` 적용 지점이 물리적으로 자연스럽다는 근거
- srsUE rank-1 한계 소스 감사

**버림**
- `RadioNodeCoordinator` 전체 (`radio_node.h/.cpp`)
- `process_mimo_superposition` 평행 API와 CPU/CUDA 양쪽 구현
- dense `[tap][rx][tx]` 전용 CUDA 커널, candidate history, device pointer swap
- `mimo_runtime_control.cpp` (M4에서 기존 control 메커니즘 재사용으로 대체)

---

## 4. 측정과 보고 규칙

`AGENT_HARNESS.md`의 "publish measured envelopes" 규칙을 그대로 적용한다. 모든 성능 수치는 하드웨어, sample rate, topology, model chain, backend, run duration을 함께 적는다.

이전 시도가 관측한 tail(500 µs 게이트에서 max 2–8 ms)은 host scheduling / IRQ / driver submit-or-sync 계열로 좁혀졌고 MIMO 연산 경로가 원인이 아니었다. 이 사안을 재조사 대상으로 다시 열지 않는다. deadline harness는 유지하되 observed-max miss는 **환경 게이트**로 분류하고, 유한 실행이 hard-real-time 보장을 성립시키지 않는다는 점을 계속 명시한다.
