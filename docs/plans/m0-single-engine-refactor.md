# M0 — 단일 엔진 리팩터 상세 설계

상위 문서: [`MIMO_MILESTONES.md`](../../MIMO_MILESTONES.md) · 미션: [`AGENT_GOAL.mimo.md`](../../AGENT_GOAL.mimo.md)

**M0의 목표는 MIMO 기능이 아니다. 브로커 스레드 구조를 producer/ring 모델로 바꾸고, 그 변경만 단독으로 라이브 검증하는 것이다.** 이 마일스톤이 끝난 시점에 `Nt = Nr = 1`이고 출력은 여전히 행 하나이며, 관측 가능한 동작은 pre-MIMO와 같아야 한다.

이렇게 쪼개는 이유: 브로커 재작성이 전체 계획에서 유일한 고위험 구간이다. 이전 시도는 스키마·coordinator·행렬·CUDA를 한 덩어리로 넣어서, 라이브 회귀가 났을 때 원인을 분리할 수 없었다.

---

## 1. 범위

### 들어가는 것

1. 브로커 내부에 `RadioNodeRuntime` / `PortRuntime` 도입 (설정 스키마 변경 없음 — 모든 Device가 implicit singleton RadioNode)
2. `run_server` → RadioNode **producer 스레드** + 포트별 **RX 출력 `IqRing`** + 얇은 **`PortRepWorker`**
3. `ChannelProcessor::process_superposition` 출력을 다중 행으로 일반화 (M0에서는 항상 1행)
4. data-plane REP 소켓 `ZMQ_SNDHWM=0`
5. bounded stall detector + 신규 진단 이벤트

### 들어가지 않는 것

- `radio_nodes` YAML 파싱 (M1). M0에서 파서를 추가하면 소비자 없는 죽은 코드가 된다.
- lane 전개, 행렬, 상관, `grid.y = Nr` 커널 확장 (M1~M3)
- CUDA 커널 본문 수정. **M0의 CUDA 변경은 시그니처 배관뿐이다.**

---

## 2. 브로커 구조

### 2.1 현재 (pre-MIMO)

Device당 스레드 2개: `run_puller`(TX REQ → TX ring), `run_server`(RX REP ← 요청마다 채널 계산 → 송신).

`run_server` 하나가 **7가지 책임**을 겸한다: REP FSM, incoming 목록 소유, epoch co-init, 공통 window 선택, 커서 전진, 채널 호출, throttle.

### 2.2 M0 이후

```text
                 ┌───────────────────────┐
Device TX 엔드포인트 →│ PortPullerWorker      │→ TX IqRing        (변경 없음)
                 └───────────────────────┘
                             │ committed frontier
                             ▼
        ┌──────────────────────────────────────────────┐
        │ RadioNode producer 스레드 (노드당 1개)         │
        │  공통 window · 커서 · 채널 1회 호출 · throttle │
        └──────────────────────────────────────────────┘
                 │ row 0                    │ row Nr-1
                 ▼                          ▼
          RX 출력 IqRing[0]  ...      RX 출력 IqRing[Nr-1]
                 │                          │
        ┌────────────────┐         ┌────────────────┐
        │ PortRepWorker 0│  ...    │ PortRepWorker  │
        │ RX REP 소켓만   │         │ RX REP 소켓만   │
        └────────────────┘         └────────────────┘
```

스레드 수: `nodes + 2 × ports`. M0(전부 singleton)에서는 `3 × devices` — 오늘의 `2 × devices`보다 노드당 1개 많다. 노드가 곧 포트이므로 producer가 얇고, GPU 호출 횟수는 동일하다.

### 2.3 소유권 이동표

| 상태 / 동작 | 현재 소유자 | M0 소유자 |
|---|---|---|
| `rx_rep` 소켓, REP FSM | `run_server` | `PortRepWorker` |
| incoming link 목록 | `run_server` | producer |
| `epoch_set`, 커서 co-init | `run_server` | producer |
| 공통 `serve` 크기 선택 | `run_server` | producer |
| 소스 커서 전진 | `run_server` (읽기 직후) | producer (채널 호출 후 1회) |
| `process_superposition` 호출 | `run_server` | producer |
| `throttle_anchor`, `served` | `run_server` | producer |
| `send_samples` 재시도 | `run_server` | `PortRepWorker` |
| RX 출력 버퍼 | 없음 (요청 시 계산) | 포트별 `IqRing` |

---

## 3. producer 루프

의사코드. 기존 `run_server`(`src/broker.cpp:422-613`)에서 **보존해야 하는 불변식을 주석으로 표시**한다.

```cpp
bool epoch_set = false;
bool throttle_anchored = false;
std::uint64_t served = 0;

while (!stop_requested) {
  // (A) 출력 room. 모든 RX row가 최소 1샘플을 받을 수 있어야 한다.
  //     [불변식] full batch를 요구하지 않는다 — B2.2 lock-step deadlock 교훈.
  std::size_t room = batch;
  for (r : rx_ports) room = min(room, rx_ring[r].free_capacity());
  if (room == 0) { stall_check(); sleep(50us); continue; }

  // (B) 최초 1회 공통 origin. [불변식] earliest_sequence(), 절대 frontier 아님.
  //     frontier로 맞추면 lock-step 라디오의 head-start IQ를 버려 relay가 잠긴다.
  if (!epoch_set) {
    for (p : src_ports) cursor[p] = tx_ring(p).earliest_sequence();
    epoch_set = true;
  }

  // (C) 모든 incoming physical link의 모든 Nt 소스 포트에 공통 가용한 양.
  std::size_t avail = batch;
  for (p : src_ports) {
    if (cursor[p] < tx_ring(p).earliest_sequence()) {   // 복구 경로 보존
      cursor[p] = tx_ring(p).earliest_sequence();
      stats.tx_sequence_gaps++;
    }
    avail = min(avail, tx_ring(p).next_sequence() - cursor[p]);
  }
  const std::size_t count = min({batch, avail, room});
  if (count == 0) { starvation_check(); sleep(50us); continue; }

  // (D) window 읽기. 커서는 아직 움직이지 않는다.
  for (p : src_ports) tx_ring(p).read(cursor[p], window[p]);

  // (E) 채널 1회 호출 → Nr행. [불변식] 노드당 슬롯당 정확히 1회.
  processor->process_superposition(node_id, lanes, rx_model, rate, out_rows);

  // (F) 커밋: 모든 커서를 같은 count만큼, 한 루프에서.
  //     ★ 이것이 sibling 포트 정렬을 구조적으로 보장하는 지점이다.
  for (p : src_ports) cursor[p] += count;

  // (G) throttle. [불변식] served >= rate 마다 anchor 재기준 (곱 overflow 방지)
  if (!throttle_anchored) { anchor = now(); throttle_anchored = true; }
  else sleep_until(anchor + ns(served * 1e9 / rate));
  served += count;
  if (served >= rate) { anchor += ns(served * 1e9 / rate); served = 0; }

  // (H) 행을 각 RX ring에 push. [불변식] zero-fill 금지 — 실제 샘플만.
  for (r : rx_ports) rx_ring[r].push(out_rows[r].first(count));
}
```

`(F)`가 핵심이다. 커서를 전진시키는 코드가 프로그램 전체에 **한 곳**이고 **한 스레드**이므로, sibling 포트가 서로 다른 window를 소비하는 상태 자체가 표현 불가능하다. coordinator가 generation barrier로 강제하던 것을 구조로 대체한다.

## 4. `PortRepWorker`

```cpp
while (!stop_requested) {
  recv_dummy_request(rep_socket);          // 100ms 타임아웃 루프, 기존과 동일
  std::size_t n = 0;
  while (!stop_requested && (n = rx_ring.pop_upto(reply_buf, batch)) == 0) {
    sleep(50us);                           // [불변식] 비면 대기. zero-fill 금지.
  }
  if (n == 0) break;
  while (!stop_requested && !send_samples(rep_socket, span(reply_buf, n))) {
    /* transient timeout: 같은 데이터 재전송, REP FSM 보존 */
  }
}
```

책임은 소켓 FSM뿐이다. window 선택, 커서, 채널, throttle, epoch 없음.

**`ZMQ_SNDHWM = 0`** (data-plane REP만). 이전 시도에서 재현된 libzmq 4.3.5 동작: 유한 REP send HWM에서 내부 non-mandatory ROUTER가 routing-id envelope 단계의 reply를 조용히 버리면서 `zmq_send()`는 성공을 반환한다. strict REQ peer는 영원히 대기한다. REQ 소켓은 기존 bounded 설정 유지.

---

## 5. RX ring 사이징과 지연 예산

**이것이 M0에서 새로 생기는 유일한 물리적 변화다.** pre-MIMO는 RX측 버퍼가 0이고 요청 시 계산한다.

| 항목 | 값 | 근거 |
|---|---|---|
| ring capacity | `2 × batch` | 소비자가 부분 소비 중이어도 producer가 full batch를 push할 수 있어야 함 |
| producer high-water | `1 × batch` | 실제 run-ahead 상한. `room` 계산 시 `capacity - occupancy` 대신 `high_water - occupancy` 사용 |
| 기준 rate에서 추가 단방향 지연 | **≤ 1 ms** (batch 23040 @ 23.04 MS/s) | high-water 1 batch |
| 설정 노브 | `runtime.rx_ring_batches` (기본 2) | 측정 후 조정 가능 |

RTT로는 최대 2 ms가 붙을 수 있다(양방향). 이건 그냥 전파 지연이 늘어난 것과 물리적으로 동등하지만, **NR 절차 타이밍에는 영향을 줄 수 있다.** 진행 기록상 이 시스템에서 가장 마진이 얇은 지점은 Msg3 PUSCH다 (CFO knee가 ~155–190 Hz로 관측된 지점). 따라서:

> **M0 exit 조건**: 라이브 1×1 attach에서 Msg3가 통과해야 하고, 정상 상태 ring 점유와 추가 지연을 실측해 기록한다. Msg3가 실패하면 `rx_ring_batches`를 낮추거나 high-water를 batch 이하로 내려 재측정한다.

TX측 ring이 이미 2,457,600 샘플(≈107 ms)인 것과 비교하면 작지만, TX ring은 backpressure 버퍼이고 이건 **지연 경로에 직접 들어간다**는 점이 다르다. 그래서 별도 게이트로 둔다.

---

## 6. 데드락 분석

producer가 기다리는 조건은 두 가지다: 입력 가용(C), 출력 room(A).

- **A는 외부 의존**: 이 노드의 peer가 자기 RX를 pull해야 비워진다.
- **C는 외부 의존**: 상대 노드의 peer가 TX를 채워야 늘어난다.

브로커 내부에 순환 대기가 없다 — producer는 다른 producer를 기다리지 않는다. 순환은 **peer들 사이**에만 존재할 수 있고(OCUDU는 TX 버퍼 full이면 블록, RX 비면 블록), 그건 pre-MIMO에도 있던 구조다.

그 순환을 푸는 것이 **부분 진행 규칙**이다. 입력도 출력도 full batch를 요구하지 않고 `count = min(batch, avail, room)`으로 **가능한 만큼** 진행한다. B2.2에서 고정 batch serve가 lock-step 라디오의 부분 chunk를 좌초시켜 relay 전체를 잠갔던 것과 같은 함정이므로, 출력 room 조건에도 동일 규칙을 적용한다.

**peer 사망 시**: 해당 RX ring이 차고 producer가 멈추고 소스 ring이 차고 puller가 backpressure한다. pre-MIMO에서 서버가 `wait_req`에 멈추는 것과 동일한 실패 모드다. fault 상태기계를 만들지 않고 **bounded stall detector + 진단 라인**만 추가한다:

```text
event=node_stall node=ue0 phase=output_room|input_data waited_ms=2000 rx_ring=[23040/46080, 0/46080]
```

---

## 7. `ChannelProcessor` API 변경

```cpp
struct SuperpositionInput {
  std::string link_key;
  const ModelConfig* model = nullptr;
  std::span<const IqSample> samples;
  int rx_port = 0;   // 신규: 이 lane이 누산될 출력 행
  int tx_port = 0;   // 신규: source dedup 키 (CUDA src_index 매핑용)
};

// 다중 행 진입점 (유일한 가상 함수)
virtual void process_superposition(const std::string& dst_key,
                                   const std::vector<SuperpositionInput>& inputs,
                                   const ModelConfig* rx_model,
                                   std::uint64_t sample_rate_hz,
                                   std::span<std::span<IqSample>> outputs) = 0;

// 기존 단일 행 호출부를 그대로 살리는 비가상 편의 오버로드.
// 두 번째 엔진이 아니라 1행 어댑터다.
void process_superposition(const std::string& dst_key,
                           const std::vector<SuperpositionInput>& inputs,
                           const ModelConfig* rx_model,
                           std::uint64_t sample_rate_hz,
                           std::span<IqSample> output)
{
  std::span<IqSample> rows[1] = {output};
  process_superposition(dst_key, inputs, rx_model, sample_rate_hz, rows);
}
```

**호출부 (grep 확인 완료, 총 7곳)**

| 파일 | 처리 |
|---|---|
| `src/broker.cpp` | 다중 행 진입점으로 교체 (M0에서 행 1개) |
| `apps/ocudu_gpu_channel_bench.cpp` | 편의 오버로드로 무변경 |
| `tests/test_processing.cpp` (4곳) | 편의 오버로드로 무변경 |
| `tests/test_runtime_update_parity.cpp` | 편의 오버로드로 무변경 |

즉 **M0에서 테스트 코드 변경 0줄**이다. 이건 의도된 설계다 — 기존 회귀 스위트가 리팩터의 안전망 역할을 그대로 해야 한다.

**백엔드 측 M0 변경**
- `cpu_backend.cpp`: 루프가 `output` 대신 `outputs[edge.rx_port]`에 누산. `rx_model`은 행마다 적용하되 상태 키는 `dst + ">rx"` 유지(행 1개이므로 동작 동일).
- `cuda_backend.cu`: `outputs.size() == 1` 검증 후 기존 경로 그대로. **커널 무수정.** `device_output` 크기, `row_begin[]`, `grid.y` 확장은 M1.

---

## 8. 작업 순서 (커밋 단위)

각 단계마다 `ctest` 8/8을 통과시키고 넘어간다.

| # | 내용 | 검증 |
|---|---|---|
| M0.1 | git baseline 커밋 (pre-MIMO 트리 그대로) | — |
| M0.2 | `processing.h` 다중 행 시그니처 + 편의 오버로드, CPU/CUDA 배관 | ctest 8/8, 테스트 코드 무변경 |
| M0.3 | 브로커 내부 `PortRuntime` / `RadioNodeRuntime` 도입 (implicit singleton lowering). 스레드 구조는 아직 그대로 | ctest 8/8 |
| M0.4 | RX 출력 `IqRing` + `PortRepWorker` + producer 스레드로 교체 | ctest 8/8, 특히 `broker`(loopback + multi_ue_lockstep) |
| M0.5 | REP `ZMQ_SNDHWM=0`, stall detector, `event=node_stall` / `event=radio_node_resolved` | ctest 8/8 |
| M0.6 | `runtime.rx_ring_batches` 노브 + ring 점유/지연 계측 라인 | ctest 8/8 |

`tests/test_broker.cpp`의 `scenario_multi_ue_lockstep`이 M0.4의 핵심 회귀다 — 이 시나리오가 원래 B2.2 deadlock을 재현하려고 만들어진 것이므로, 부분 진행 규칙이 깨지면 여기서 잡힌다.

---

## 9. Exit 게이트

M0은 아래를 **전부** 통과해야 M1으로 넘어간다.

1. `ctest` 8/8 (config, mutable_params, control_server, runtime_update_parity, hardware_probe, processing, ring, broker) — CPU 빌드와 CUDA 빌드 양쪽
2. `scripts/remote/gpu-test-sequence.sh` 7/7
3. `ocudu-attach-smoke.sh` — `rrc_connected=1`, `pdu_session_established=1`, `ping_ok=1`, gNB `Real-time failure in RF: overflow` 0
4. `ocudu-multi-ue-smoke.sh`, `ocudu-multi-gnb-smoke.sh` 통과
5. strict counter 전부 0 (`tx_queue_overflows`, `tx_sequence_gaps`, `zmq_errors`)
6. **신규**: RX ring 정상 상태 점유와 추가 단방향/왕복 지연 실측치 기록. 하드웨어·sample rate·topology·model chain·backend·run duration 라벨 포함 (`AGENT_HARNESS.md`의 measured-envelope 규칙)

3~5는 pre-MIMO에서 이미 green이었던 항목이다. **M0에서 red가 나면 그것은 producer/ring 전환이 만든 회귀이고, 다른 어떤 것도 아니다.** 이 원인 분리 가능성이 M0을 단독 마일스톤으로 두는 이유 전부다.
