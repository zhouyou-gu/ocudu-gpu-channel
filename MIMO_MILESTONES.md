# MIMO 재구현 마일스톤

기준 코드: pre-MIMO 베이스라인 (이 트리, 12,755줄). 이전 시도(`ocudu-gpu-channel-audit`, +29,296줄)는 참고 자산으로만 사용하고 구조는 계승하지 않는다.

미션 파일은 `AGENT_GOAL.mimo.md`를 따른다. 원본 `AGENT_GOAL.md`는 변경하지 않았다.

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

**범위**
- physical link 하나의 seed에서 lane별 Jakes 파라미터 파생. **절대시간은 physical link가 단일 소유**.
- 기존 `apply_channel_kernel` 페이딩 경로 무수정.

**Exit 게이트**
- lane별 자기상관이 `J_0(2π f_d τ)`와 일치 (기존 Bessel 테스트 방식 재사용, ±0.15)
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
- 진짜 rank-2 주장은 MIMO 지원 UE PHY가 준비된 뒤 별도 게이트로 둔다 (`AGENT_GOAL.mimo.md` Non-Goals).

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
