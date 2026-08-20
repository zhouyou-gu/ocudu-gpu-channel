# RadioNode 단이 필요한 이유와 srsUE의 MIMO 한계

기준 문서: [`docs/plans/m0-single-engine-refactor.md`](plans/m0-single-engine-refactor.md)
상위 문서: [`MIMO_MILESTONES.md`](../MIMO_MILESTONES.md) · 미션: [`AGENT_GOAL.md`](../AGENT_GOAL.md)

M0 설계 문서를 읽을 때 반복해서 나오는 두 질문에 답한다.

1. `Device`가 이미 있는데 왜 그 위에 `RadioNode` 단을 얹는가
2. srsUE에서 **정확히** 어떤 동작이 MIMO를 지원하지 않는가

두 질문은 독립이 아니다. 2번의 답이 1번의 검증 전략을 결정한다.

---

## 1. RadioNode 단이 왜 필요한가

### 1.1 기존 `Device`가 무엇인지부터

`MIMO_MILESTONES.md` §0의 3줄 정의가 전부다.

```text
Device       = ZMQ transport port (소켓 + TX ring). 순수 전송 단위.
RadioNode    = 여러 Device의 공통 sample epoch / throttle / 출력 소유자.
PhysicalLink = RadioNode 쌍 사이의 공동 H / RNG / 절대시간 / correlation 소유자.
```

`Device`에는 "이 포트가 어느 라디오에 속하는가"라는 개념이 없다. pre-MIMO에서는 필요하지 않았다 — 라디오 하나 = 포트 하나였기 때문이다. MIMO는 그 등식이 깨지는 지점이고, **깨진 뒤에도 누군가는 "이 포트들은 한 라디오다"를 소유해야 한다.** 그 소유자가 RadioNode다.

### 1.2 없으면 정확히 무엇이 깨지는가

2×2를 scalar link 4개로 표현하고 pre-MIMO 브로커를 그대로 두면, lane이 **서로 다른 두 개의 destination device 서버 스레드**로 쪼개진다.

```text
ue0_p0 서버 스레드 : lane(0,0), lane(0,1)
ue0_p1 서버 스레드 : lane(1,0), lane(1,1)
```

구 `run_server`에서 각 서버는 **자기 시점에** 공통 가용량을 샘플링한다.

```cpp
std::size_t common = dev.batch;
for (k : incoming) { ... common = min(common, avail); }
```

두 서버가 같은 소스 ring 집합을 보더라도 **샘플링 순간이 다르다.** 서버 A가 `t1`에 `common=1000`, 서버 B가 `t2 > t1`에 `common=1500`을 얻으면 `cursor(0,*)=1000`, `cursor(1,*)=1500`이 되고, 이후 영구적으로 다음이 성립한다.

| 파손 | 내용 |
|---|---|
| **행렬 방정식 자체가 성립하지 않음** | 행 0은 `y₀[n] = H[0,:]·x[n]`, 행 1은 `y₁[n'] = H[1,:]·x[n']`, `n' ≠ n`. 하나의 `x`에 대한 `y = Hx`가 아니다. rank-2 검출의 전제가 무너진다. |
| **채널 상태가 행마다 다른 시간을 삶** | physical link의 Jakes 절대시간(`slot_start_samples`)과 lane별 `delay_line`이 서로 다른 count로 전진한다. 공간 상관과 coherent LOS는 "같은 시각의 한 realization"을 전제하므로 무의미해진다. |
| **한쪽 행만 샘플을 잃음** | 커서 격차가 소스 ring 용량을 넘으면 복구 경로가 뒤처진 커서를 `earliest_sequence()`로 점프시키고 `tx_sequence_gaps`를 올린다. 행 간 상대 정렬이 영구 파손된다. |

즉 정확한 명제는 "초기 커서 정렬"이 아니다 — 초기 co-init은 이미 공통 origin으로 맞춰진다. 정확히는 **"sibling RX 포트가 매 epoch 동일한 window를 소비해야 하는데, per-destination-device 서버 모델에는 그것을 강제하는 장치가 없다"** 이다.

이것은 미션 제약에도 못 박혀 있다 (`AGENT_GOAL.md` Constraints):

> All ports of one radio share one sample epoch, and all matrix coefficients between two radios belong to one channel realization with one time origin; **per-port state that can drift independently is not an acceptable implementation of a matrix channel.**

### 1.3 왜 프로토콜이 아니라 계층인가

정렬 문제를 푸는 방법은 두 가지가 있었다.

| | 방식 | 결과 |
|---|---|---|
| 이전 시도 | `RadioNodeCoordinator` — Nr개 request-driven 스레드를 generation ticket / prepare-on-first-request / all-reply barrier / ack / commit / abort / rearm으로 직렬화 | 독립 REP 소켓 Nr개 위에 얹은 분산 트랜잭션. 이전 시도 복잡도의 본체 |
| 현재 | RadioNode당 **producer 스레드 1개**가 `count`를 한 번 정하고, 채널을 한 번 호출하고, 모든 소스 커서를 **한 문장에서** 전진 | 정렬이 강제해야 할 성질이 아니라 **표현 불가능한 상태**가 됨 |

M0 문서 producer 루프의 `(F)` 단계가 그 지점이다.

```cpp
// (F) 커밋: 모든 커서를 같은 count만큼, 한 루프에서.
//     ★ 이것이 sibling 포트 정렬을 구조적으로 보장하는 지점이다.
for (p : src_ports) cursor[p] += count;
```

커서를 전진시키는 코드가 프로그램 전체에 **한 곳**이고 **한 스레드**이므로, 두 번째 window를 고를 수 있는 주체가 존재하지 않는다. coordinator도 같은 불변식을 달성하지만 generation barrier라는 훨씬 무거운 기구로 달성한다.

RadioNode 단은 그러니까 **"group cursor의 단일 소유권"을 담을 그릇**이다. 부수적으로 epoch co-init, 공통 window 선택, throttle anchor, 출력 행 소유가 전부 같은 자리로 모인다.

### 1.4 책임 재배치

구 `run_server` 하나가 겸하던 7가지 책임 중 6개가 노드로 가고, 포트에는 소켓 FSM만 남는다.

| 상태 / 동작 | pre-MIMO 소유자 | M0 소유자 |
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

### 1.5 왜 `Device`에 `ports[]`를 넣지 않았는가

이전 계획의 `PhysicalNode → ClockDomain → BasebandPort[]` 개념도 자체는 정확하다. 다만 그것을 곧바로 `DeviceConfig::ports[]`로 구현하면 blast radius가 다음과 같이 퍼진다.

- config parser와 모든 example topology
- 브로커의 Device 생성, 소켓 연결, ring과 diagnostic indexing
- link resolution과 control ID migration
- 기존 SISO smoke script의 endpoint lookup
- 기존 processor prepare key와 telemetry key

Overlay 방식은 `Device`를 leaf port로 **강등만** 하고 위에 노드를 얹으므로, 파서에는 `radio_nodes`와 group link만 추가하면 된다. 기존 SISO YAML은 implicit singleton RadioNode로 lowering되어 의미가 그대로 보존된다.

### 1.6 왜 M0에서는 스키마 없이 런타임 타입만 넣는가

`radio_nodes` YAML 파싱은 M1이다. M0에서 파서를 추가하면 소비자 없는 죽은 코드가 된다.

M0의 목적은 MIMO가 아니라 **브로커 스레드 구조 전환만 단독으로 라이브 검증**하는 것이다. 종료 시점에 `Nt = Nr = 1`, 출력 1행, 관측 가능한 동작은 pre-MIMO와 동일해야 한다. 그래서 M0에서 red가 나면 원인이 producer/ring 전환 하나로 확정된다 — 이 원인 분리 가능성이 M0을 단독 마일스톤으로 두는 이유 전부다.

현재 트리의 `src/broker.cpp`에는 이미 `RadioNodeRuntime` / `PortRuntime`과 stall detector가 들어가 있고, 진단 배열에 다음 주석이 달려 있다.

> (In M0 the two are the same length -- node index equals port index -- but they are indexed distinctly so M0.4 can split them.)

---

## 2. srsUE에서 정확히 무엇이 MIMO를 지원하지 않는가

### 2.1 범위부터 좁힌다

"srsRAN이 MIMO를 지원하지 않는다"는 명제가 아니다. srsRAN 4G의 **LTE** srsUE에는 TM1~TM4가 별도로 문서화되어 있다. 문제 삼는 범위는 이 프로젝트가 실제로 물려 쓰는 **`srsRAN_4G release_23_11`의 archived srsUE NR prototype 경로**다.

그리고 반드시 분리해야 하는 두 가지가 있다.

1. RF/ZMQ 계층이 indexed endpoint를 여러 개 열 수 있는가 → **가능할 수 있다**
2. 한 UE 세션의 NR PHY가 실제 rank-2를 estimate / decode / encode 하는가 → **불가**

1번이 2번을 함의하지 않는다. 소켓이 2개 열린다고 PHY가 행렬을 역산하지 않는다. 이전 감사가 "indexed RF/ZMQ channel 노출 가능"이라고만 기록해 둔 것을 multi-port 지원으로 읽으면 안 되는 이유가 이것이다.

### 2.2 소스 근거 세 지점

아래는 이전 시도의 소스 감사 기록(`ocudu-gpu-channel-audit/docs/plans/mimo-radio-node-overlay-architecture.md` §3.2)에 고정된 upstream 라인 참조를 옮긴 것이다. 이 문서를 쓰는 시점에 upstream 소스를 다시 열어 재확인하지는 않았다.

| 위치 | 사실 | 의미 |
|---|---|---|
| `srsue/src/stack/rrc_nr/rrc_nr.cc` L99-106 | NR RRC가 `max_mimo_layers = 1` 설정 | UE capability 자체가 1 layer. gNB 스케줄러는 rank 1 이상을 절대 스케줄하지 않고 DCI/DMRS port 할당도 single-layer로 고정된다. **에뮬레이터가 완벽한 2×2 `H`를 만들어 줘도 애초에 복호할 rank-2 전송이 발생하지 않는다.** |
| `lib/src/phy/ue/ue_dl_nr.c` L220-236 | DL estimation/decoding 핵심 경로가 `sf_symbols[0]` 사용 | 수신 체인이 **첫 번째(port 0) 버퍼 하나만** 본다. 채널 추정 → 등화 경로에 `Nr` 차원 배열이 흐르지 않는다. UE에 RX 포트를 2개 물려도 두 번째 포트 샘플은 추정기에 진입조차 못 한다 — MRC도, 행렬 역산도 그 경로에 없다. |
| `srsue/src/phy/nr/cc_worker.cc` L31-50 | NR UL worker가 첫 TX buffer 기준으로 초기화 | 상향도 단일 포트. 2-stream UL 없음. |

핵심은 **두 층이 동시에 막혀 있다**는 점이다. 상위(RRC capability)에서 rank 1로 협상하므로 rank-2 전송이 만들어지지 않고, 설령 만들어져도 하위(DL PHY)가 port 0 버퍼 하나만 읽으므로 복호할 수단이 없다. 어느 한쪽만 고쳐서 우회할 수 있는 종류가 아니다.

### 2.3 "srsUE 두 개 = 2-port UE 하나"가 성립하지 않는 이유

두 독립 srsUE 프로세스는 서로 다른 RNTI, RACH 절차, RRC/MAC/HARQ 상태, 그리고 **서로 다른 channel estimation state**를 가진다. 각각이 자기 수신 방정식 하나씩을 푼다고 해서 두 프로세스가 공동으로 2×2 행렬을 역산하지 않는다. 그것은 2-port UE 하나가 아니라 UE 둘이며, 성격상 MU-MIMO 실험에 가깝다.

참고로 `AGENT_PROGRESS.md`의 B2.2 기록은 그 독립성의 반대 증거이기도 하다 — srsUE 두 개를 동시 기동했더니 lock-step 가상시간 때문에 동일 PRACH occasion에 동일 preamble을 쏴서 gNB가 하나의 C-RNTI만 할당했고, contention resolution이 붕괴했다. 두 프로세스는 서로의 존재를 전혀 모르는 별개 UE다.

### 2.4 그래서 게이트가 어떻게 갈리는가

`AGENT_GOAL.md` Non-Goals가 이것을 명시한다.

> Claiming live multi-layer (rank > 1) operation on the basis of transport-level multi-port flow alone, or on the basis of several independent single-port UE processes; a live rank-2 claim requires a UE PHY that jointly estimates and decodes the matrix channel.

| srsUE로 | 가능 여부 |
|---|---|
| 1×1 live attach / PDU / ping regression | 가능, 이미 검증됨 → M0 및 M5의 첫 게이트 |
| indexed ZMQ endpoint 2개를 열어 transport 관찰 | 설정·transport 실험으로는 가능할 수 있음 |
| 두 포트가 같은 sample epoch로 움직이는지 marker 검사 | synthetic marker와 함께 transport 시험 가능 |
| 한 srsUE NR 세션의 true rank-2 2×2 복호 증명 | **불가** |
| srsUE 두 프로세스를 한 2-port UE로 간주 | **불가** |

따라서 `MIMO_MILESTONES.md` M5의 "명시적 비게이트"가 나온다. 이 UE는 rank-2 acceptance gate가 될 수 없다. 대신:

- **행렬 정확성**은 합성 2-port peer(`apps/ocudu_mimo_transport_peer.cpp`)로 검증한다 — M1의 identity `H` / swap `H` / known `H` 해석해 일치 + marker 테스트.
- **라이브 회귀**는 srsUE 1×1로 계속 돌린다 (M0 직후 1회, M4 이후 1회).
- **진짜 rank-2 주장**은 MIMO-capable UE PHY가 준비된 뒤 별도 게이트로 미룬다.

### 2.5 이 제약 때문에 목표를 2×1로 낮추지 않는다

local UE가 rank-1이라는 이유로 첫 MIMO 구현을 2×1로 만들면 transport만 통과하고 정작 핵심인 RX 행 동기화, matrix output commit, 수신 상관을 하나도 검증하지 못한다. 그래서 첫 MIMO acceptance target은 처음부터 **2×2**이며, production 자료구조는 `Nt`/`Nr`를 runtime dimension으로 가진다.

한편 gNB 쪽 경계는 유리하게 확인되어 있다. 고정 OCUDU 커밋의 DL ZMQ IQ는 **precoding과 OFDM이 끝난 TX baseband-port IQ**이므로 (resource-grid mapper가 precoding 적용 후 `i_tx_port`에 기록 → port별 OFDM 변조 → port별 ZMQ channel), 에뮬레이터가 그 port vector에 `H`를 적용하는 경계는 물리적으로 자연스럽다. 막혀 있는 쪽은 UE 뿐이다.

---

## 3. 두 질문의 연결점

srsUE가 rank-1이라는 사실은 **RadioNode 단이 필요 없다는 뜻이 아니라, RadioNode 단의 정확성을 srsUE로 증명할 수 없다는 뜻이다.**

- 정렬 불변식은 UE가 그것을 활용하든 말든 에뮬레이터가 구조적으로 보장해야 한다 (미션 Constraints).
- 그 보장의 **정확성 검증**은 합성 2-port peer가 맡는다 (M1 exit 게이트).
- srsUE의 역할은 **"구조 전환이 기존 1×1 라이브 경로를 깨지 않았다"**는 회귀 게이트 하나로 축소된다.

M0 exit 조건이 Msg3 PUSCH 통과와 RX ring 추가 지연 실측에 걸려 있는 이유가 그것이다. RX ring은 M0이 새로 도입하는 유일한 물리적 변화이고 (pre-MIMO는 RX 버퍼 0, 요청 시 계산), 지연 경로에 직접 들어간다. 진행 기록상 이 시스템에서 마진이 가장 얇은 지점이 Msg3 PUSCH이므로 (CFO knee ~155–190 Hz 관측), 1×1 srsUE 회귀는 MIMO를 증명하지는 못해도 **이 전환이 안전한지는 정확히 증명한다.**
