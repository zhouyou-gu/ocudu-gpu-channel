# M4 — physical link 단위 runtime control 상세 설계

상위 문서: [`MIMO_MILESTONES.md`](../../MIMO_MILESTONES.md) · 선행: [`m3-spatial-correlation-and-los.md`](m3-spatial-correlation-and-los.md) · 기존 컨트롤 플레인: [`runtime-mutable-channel.md`](runtime-mutable-channel.md) (v1) / [`-v2`](runtime-mutable-channel-v2.md) / [`-v3`](runtime-mutable-channel-v3.md)

**M4의 목표는 채널을 바꾸는 단위를 lane에서 physical link로 올리는 것이다.** Phase 3이 만든 shadow + atomic seqno + 슬롯 경계 snap은 그대로 쓴다. 바뀌는 것은 **그것이 몇 개 있고 누가 소유하느냐**다. M0이 커서를, M2가 시드와 시간을, M3이 확률 생성기를 물리 링크로 올린 것과 같은 이동이며, 이유도 같다: **lane마다 하나씩 있으면 lane마다 다를 수 있다.**

M3까지는 그 어긋남이 이론이었다. M4에서는 아니다 — `H`를 슬롯 경계에서 통째로 갈아끼우는데 lane 절반만 갈리면, 그 슬롯의 출력은 **두 채널 행렬이 섞인 것**이고 어느 쪽도 아니다.

---

## 1. 지금 상태 — 코드에서 확인한 다섯 가지

### 1.1 제어 주소가 lane이다

`ChannelProcessor::collect_control_links()`가 반환하는 키가 **lane 키**다(`cpu_backend.cpp` / `cuda_backend.cu`). 브로커는 그 맵을 그대로 `ControlServer`에 넘기고, `ControlServer`는 REQ의 `link_id` 문자열로 조회한다(`control_server.cpp:521`).

결과가 둘이다. (a) 2×2 링크 하나가 **컨트롤 엔드포인트 4개**로 보인다. (b) 주소에 `#r1t0` suffix가 들어간다 — **M2가 realization에서 걷어낸 바로 그 문자열 포맷 의존이 제어 표면에는 그대로 남아 있다.** 운영자가 `H`를 바꾸려면 lane 키 표기법을 알아야 하고, 그 표기가 바뀌면 스크립트가 깨진다.

### 1.2 스냅도 lane마다 따로 일어난다

`snap_mutable_params`가 CPU는 `apply_chain_to_link` 안에서 lane마다, CUDA는 에지 루프 안에서 에지마다 호출된다. `BrokerLinkControl::current_slot`도 lane마다 하나다.

따라서 **"한 링크의 모든 lane이 같은 슬롯에 갈린다"는 오늘 보장되지 않는다.** 컨트롤러가 lane 4개에 같은 값을 네 번 보내고 `take_effect_at_slot`을 맞춰 주면 되긴 하지만, 그것은 규율이지 불변식이 아니다 — M0/M2/M3이 매번 지운 종류의 것이다.

### 1.3 두 백엔드가 노출하는 집합이 다르다

CPU는 `states_` 전체를 노출한다 — **수신 모델 상태(`"<node>>rx"`)까지 포함**된다. CUDA는 `link_slots_`만 노출한다 — lane뿐이다. 즉 `link_id: "ue0>rx"`인 REQ가 **CPU에서는 성공하고 CUDA에서는 `unknown link_id`로 거부된다.** M4 이전부터 있던 불일치이고, M4가 주소 체계를 손대는 김에 정리해야 한다.

### 1.4 M3의 상관은 prepare에서 고정이다

호스트는 `PhysicalLinkFading::mixing`, 디바이스는 노드별 `DeviceCorrelationGroup` 배열에 있다. 둘 다 `prepare()`에서 한 번 만들어지고 이후 읽기만 한다. 밀레스톤이 M4의 스냅샷 단위를 "tap layout + 행렬 + 상관"이라고 적었으므로 **`R`이 런타임 교체 대상이 된다** — 인수분해를 어디서 돌릴지가 설계 항목이 된다(§2.4).

### 1.5 warmup 계약이 lane 단위 delay_line을 지운다

v2.2의 profile swap은 교체 시점에 cross-slot 링을 zero-fill하고 `warmup_until_slot`을 세운다. 그 링은 **lane마다 하나**다(CPU `steps.front().delay_line`, 디바이스 `DeviceLinkState::delay_line`). 링크 단위 스왑이면 **그 링크의 모든 lane을 같은 슬롯에 지워야** 하고, 그렇지 않으면 일부 lane만 옛 tap layout의 잔향을 물고 간다.

---

## 2. 설계

### 2.1 `PhysicalLinkRuntime` — 링크가 소유하는 것을 한 곳에

`physical_link.h`에는 이미 링크 소유 상태가 둘 있다(`PhysicalLinkClock`, `PhysicalLinkFading`). M4가 세 번째를 더한다:

```cpp
struct PhysicalLinkRuntime {
  PhysicalLinkClock  clock;    // M2.3 절대시간
  PhysicalLinkFading fading;   // M3.3 lane grid + mixing
  BrokerLinkControl  control;  // M4  shadow + seqno + slot gate
  MutableParams      live;     // M4  스냅된 현재 값
  std::uint32_t      live_seqno = 0;
  std::uint64_t      next_slot  = 0;
};
```

**스냅은 슬롯당 링크당 정확히 한 번**, lane을 shaping하기 전에 돈다 — M3.3이 생성기를 위해 연 그 자리(`process_superposition`의 링크 선행 패스)에 스냅이 함께 들어간다. lane은 `link->live`를 **읽기만** 한다.

이렇게 하면 exit 게이트 "모든 lane에 같은 슬롯에서 원자적으로 적용"이 **테스트로 지키는 성질이 아니라 표현 불가능한 반대 상황**이 된다. lane이 자기 `live`를 갖지 않으므로 lane끼리 다른 값을 볼 방법이 없다.

### 2.2 주소는 base link key — 그리고 1×1은 안 깨진다

`collect_control_links()`가 `LaneConfig::physical_link_key`로 키를 바꾼다. **`Nt = Nr = 1`이면 lane 키와 base 키가 문자 그대로 같으므로(M1의 의도적 예외), 기존 1×1 배포의 `link_id`는 한 글자도 바뀌지 않는다.** 바뀌는 것은 다중 포트 토폴로지뿐이고, 그건 오늘 컨트롤 플레인으로 제어된 적이 없다.

와이어 프로토콜(메시지 타입, 필드 이름, 응답 형식)은 건드리지 않는다.

### 2.3 `fixed_mimo` × 런타임 tap 갱신 — 함정을 먼저 적는다

v1의 `tap0_gain_db` / `tap0_phase_rad`는 체인 첫 tap을 **덮어쓴다**. 그런데 `fixed_mimo` 토폴로지는 **행렬 계수를 바로 그 tap의 gain/phase에 접어 넣어** lane별 모델 클론을 만든다(M1.5b). 즉 링크 단위로 `tap0_gain_db`를 갱신하면 **모든 lane이 같은 값이 되어 고정 행렬이 지워진다.**

오늘도 lane 하나에 REQ를 보내면 같은 일이 일어나므로 새로 생긴 함정은 아니다. 그러나 주소가 링크가 되면 **한 번의 REQ가 행렬 전체를 지운다.** 권고: **`fixed_mimo` 모델에 tap 스코프 런타임 갱신(`tap0_*`, `profile_swap`)을 거부한다.** 스칼라(`path_loss_db`, `awgn_snr_db`, `cfo_hz`)는 lane 공통이므로 허용해도 안전하다. §7 결정 1.

### 2.4 상관의 런타임 교체

`R`은 hot path에서 인수분해하지 않는다(M3 §2.2의 약속). 경로:

1. **컨트롤 스레드**가 REQ의 새 `R`을 파싱하고 `lane_mixing_matrix()`(LDLᴴ)를 돌린다. 비-PSD·차원 불일치는 **REQ 응답에서 거부**한다 — validator와 같은 루틴이므로 로드 타임과 런타임의 판정 기준이 갈릴 수 없다.
2. 성공하면 결과 `L`을 shadow에 넣고 seqno를 올린다.
3. **스냅(슬롯 경계)**이 `PhysicalLinkFading::mixing`을 교체한다. 호스트는 vector swap 한 번.
4. **디바이스**는 그 링크의 `DeviceCorrelationGroup` 하나만 targeted `cudaMemcpyAsync`로 올린다(구조체 ~2 KB, serve 스트림에 실려 커널 앞에 선다). 노드 배열 전체를 다시 올리지 않는다.

`R` 교체에는 **warmup이 필요 없다** — 상관은 grid를 만든 뒤 섞는 선형 사상이고 cross-slot 상태를 갖지 않는다. tap layout 교체(v2 profile swap)만 delay-line zero-fill이 필요하다. **이 구분을 스냅 경로에 명시**한다. 둘을 뭉뚱그려 항상 zero-fill하면 상관만 바꿔도 무해한 warmup 구간이 생긴다.

### 2.5 런타임에 바꿀 수 없는 것 (거부)

밀레스톤이 지정한 목록이며, 거부는 **REQ 응답에서** 이유와 함께 한다.

| 대상 | 왜 거부인가 |
|---|---|
| `Nt` / `Nr` | lane 집합·행 개수·디바이스 버퍼 크기가 전부 prepare에서 잡힌다 |
| 포트 멤버십 | 포트가 곧 ZMQ 엔드포인트다. 재배선은 재시작이지 파라미터가 아니다 |
| `sample_rate_hz` | 슬롯 크기·grid stride·delay 샘플 환산이 전부 여기에 걸린다 |
| fixed ↔ stochastic 전환 | `fixed_mimo`는 모델 클론을 만들고 zero lane을 삭제한다. lane 집합이 바뀌는 것은 위 Nt/Nr와 같은 문제다 |

### 2.6 수신 모델 엔트리 — 두 백엔드를 하나로

§1.3의 불일치를 M4가 정리한다. 권고: **수신 모델은 컨트롤 표면에서 제외**하고 두 백엔드를 CUDA 쪽에 맞춘다. 이유는 이름 그대로다 — `<node>>rx`는 링크가 아니라 **노드의 행**이고, M4가 세우려는 규칙("제어 단위는 physical link")의 예외를 첫날부터 만들 이유가 없다. 수신 모델의 런타임 제어가 필요해지면 `node_id` + 행 인덱스를 쓰는 **별도의 주소 공간**으로 들여야 한다. §7 결정 2.

### 2.7 무엇을 건드리지 않는가

- 와이어 프로토콜의 메시지 타입·필드·응답 형식(v1~v3).
- 부분 갱신 없음 — 스냅샷 단위는 whole-link(밀레스톤 명시).
- 커널. M3이 커널 경계를 한 번 바꾸고 동결했다.
- `PhysicalLinkClock`(M2.3), 시드 파생(M2.2), lane 정렬(M1.4).

---

## 3. 작업 순서 (커밋 단위)

| # | 내용 | 검증 |
|---|---|---|
| M4.1 | 이 문서 | — |
| M4.2 | `PhysicalLinkRuntime`로 control/live 승격, 링크당 1회 스냅, 주소를 base link key로 | **1×1 출력이 M3과 bit-exact**, 기존 `test_runtime_update_parity` 통과(키만 base로), `ctest` 8/8 |
| M4.3 | lane 전체 원자성 + warmup: 스왑이 모든 lane의 delay-line을 같은 슬롯에 지운다 | 2×2에서 스왑 슬롯 일치 테스트, warmup 창 일치 테스트 |
| M4.4 | 상관 런타임 교체(컨트롤 스레드 LDLᴴ + 디바이스 그룹 targeted 업로드) | 교체 전/후 공분산이 각각 선언값과 일치, 비-PSD REQ 거부 |
| M4.5 | 거부 목록(§2.5) + 수신 모델 주소 정리(§2.6) | 거부 경로 단위 테스트, 두 백엔드의 컨트롤 맵이 **같은 키 집합**임을 단언 |
| M4.6 | 라이브·브로커 검증 | 2×2 브로커에 실제 REQ를 쏴서 공분산이 바뀌는 것을 확인, `gpu-test-sequence` `[8/8]` 계열에 편입 |

M4.2의 게이트가 M3.3과 같은 형태인 것에 주의 — **소유권 이동은 동작을 바꾸지 않아야 하고, 그 증거는 이전 커밋과의 지문 비교다.**

---

## 4. Exit 게이트

`MIMO_MILESTONES.md` M4와 동일하며 판정 방법을 명시한다.

| 게이트 | 판정 |
|---|---|
| 교체가 모든 lane에 **같은 슬롯**에 적용 | 2×2에서 스왑 직전/직후 슬롯의 lane별 출력을 비교 — 경계가 lane마다 다르면 FAIL |
| 기존 `test_runtime_update_parity` 계열 통과 | 키를 base link key로 바꾼 것 외에 변경 없이 |
| warmup 계약이 lane 전체에 일관 적용 | 스왑 후 warmup 창 동안 모든 lane이 동시에 warmup, `control_warmup_begin/end`가 링크당 1회 |
| 상관 런타임 교체가 실제로 통계를 바꾼다 | 교체 전 구간과 후 구간에서 각각 공분산을 재고 선언값과 비교 |
| 거부 목록이 실제로 거부된다 | Nt/Nr·포트·sample rate·family·비-PSD 각각 REQ 응답에서 |
| 두 백엔드의 컨트롤 맵 키 집합이 동일 | 같은 토폴로지로 CPU/CUDA를 prepare해 키 집합 비교 |
| 1×1 경로가 M3과 bit-exact | 지문 A/B |
| `ctest` 8/8 · `gpu-test-sequence` 8/8 | 결정론 수치 불변 |

**뮤테이션 프로브** (FAIL을 확인한 뒤에만 게이트로 인정):

- 스냅을 링크당 1회에서 lane당 1회로 되돌리면 → 원자성 게이트 FAIL
- warmup zero-fill을 lane 하나만 건너뛰면 → warmup 게이트 FAIL
- 상관 교체 후 디바이스 그룹 업로드를 건너뛰면 → 교체 후 공분산 게이트 FAIL(호스트만 바뀌므로 CPU/CUDA parity에서도 FAIL)
- 거부 규칙을 하나 지우면 → 해당 거부 테스트 FAIL

**허용오차는 실측에서 정한다.** 공분산 추정기의 잡음 바닥은 M2의 IID 게이트가 400 fading cycle에서 0.065로 재 두었고, M3의 게이트가 그 위에서 0.15를 쓴다. 교체 전/후 구간은 각각 더 짧으므로 **그 구간 길이에서 다시 재고** 그 값을 테스트 주석에 적는다.

---

## 5. 성능

측정 전에는 적지 않는다. 잴 것은 둘이다.

- **스냅 비용**: 링크당 1회로 줄어들면 lane 수만큼 나눠지므로 2×2에서 1/4이 되어야 한다. 다만 오늘도 스냅은 seqno 비교 한 번이 대부분이라 **차이가 측정 한계 아래일 가능성이 높다** — 그러면 그렇게 적는다.
- **교체 지연**: REQ 수신에서 실제 적용 슬롯까지. `take_effect_at_slot`이 이미 결정론적 타이밍을 주므로, 재는 것은 "예약한 슬롯에 정확히 적용되는가"이지 평균 지연이 아니다.

---

## 6. 열려 있는 결정

1. **`fixed_mimo` 모델의 tap 스코프 런타임 갱신** — 거부 권고(§2.3). 허용하면 REQ 한 번이 고정 행렬을 지운다. 스칼라 파라미터는 어느 쪽이든 허용.
2. **수신 모델(`<node>>rx`)의 컨트롤 노출** — 제외 권고(§2.6), 두 백엔드를 CUDA에 맞춤. 오늘 CPU에서만 되는 기능이라 어느 쪽으로 정하든 한쪽 백엔드의 동작이 바뀐다.
3. **`R` 교체 REQ의 모양** — 새 메시지 타입(`correlation_swap`)과 기존 `profile_swap` 확장 중. 신규 타입을 권고한다: `profile_swap`은 tap layout이고 warmup을 동반하는데 `R` 교체는 warmup이 필요 없어서(§2.4), 한 메시지에 넣으면 그 구분이 흐려진다.

---

## 7. 비고 — M0 부채는 그대로다

multi-UE / multi-gNB 라이브 게이트는 여전히 환경 차단 상태다(unprivileged LXC; `AGENT_PROGRESS.md` M0 섹션). M4의 exit 게이트는 합성·단위 테스트와 로컬 브로커로 판정되므로 이 제약에 걸리지 않지만, **M4가 그 부채를 줄여주지도 않는다.**
