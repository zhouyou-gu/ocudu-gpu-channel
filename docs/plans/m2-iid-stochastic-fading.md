# M2 — IID 확률적 페이딩 상세 설계

상위 문서: [`MIMO_MILESTONES.md`](../../MIMO_MILESTONES.md) · 선행: [`m1-dimensions-and-fixed-matrix.md`](m1-dimensions-and-fixed-matrix.md)

**M2의 목표는 상관이 아니다. lane마다 독립인(IID) 페이딩을 정확히 만들고, 그 소유권을 physical link에 두는 것이다.** 이 마일스톤이 끝난 시점에 각 lane은 자기 Jakes realization을 갖고, lane 간 교차상관은 0이며, 절대시간은 lane이 아니라 physical link가 소유한다.

M1이 고정 행렬을 페이딩 없이 단독 검증했듯, M2는 **확률적 생성기를 상관 없이 단독 검증**한다. M3의 공간 상관은 이 위에 얹힌다.

---

## 1. 지금 상태 — 무엇이 이미 맞고 무엇이 아닌가

M1의 lane 키 확장 덕분에 **lane별 독립 realization은 이미 성립한다.** 시드가 키 문자열에서 파생되기 때문이다.

```cpp
// src/cpu_backend.cpp:72
const std::uint64_t fading_seed = hash(key + ":fading:" + std::to_string(i));
// src/cuda_backend.cu:246
const std::uint64_t fading_seed = hash(link_key_value + ":fading:0");
```

`key`가 M1부터 `"gnb>ue:h#r1t0"` 형태이므로 lane마다 다른 시드가 나온다. **그러나 이것은 우연이지 설계가 아니다.** 세 가지 문제가 있다.

### 1.1 시드가 키 문자열 포맷에 묶여 있다

`lane_key()`의 suffix 포맷(`#r1t0`)을 바꾸면 모든 realization이 바뀐다. 재현성이 **문자열 표기**에 의존한다. M1에서 `Nt=Nr=1`일 때 suffix를 생략하는 예외를 둔 것도 같은 이유였는데, 그 예외가 지금은 "1×1은 레거시와 같은 realization"이라는 바람직한 결과를 내지만, 그것도 **문자열이 같아서 생긴 결과**다.

### 1.2 physical link가 시드의 소유자가 아니다

`MIMO_MILESTONES.md`는 **PhysicalLink = 공동 H / RNG / 절대시간 / correlation의 소유자**로 정의한다. 지금은 lane이 각자 독립 해시를 갖는다. M3에서 lane 간 상관을 주려면 **하나의 링크 시드에서 lane들을 파생**시켜야 한다 — 독립 해시 N개로는 상관 구조를 표현할 수 없다.

### 1.3 절대시간이 lane마다 따로 누적된다

`TdlFadingState::slot_start_samples`와 `DeviceLinkState::slot_start_samples`가 lane마다 하나씩 있고 각자 `+= count` 한다. 오늘은 producer가 노드당 슬롯당 한 번, 모든 lane을 같은 `count`로 처리하므로 **결과적으로** 동기화된다. 하지만 그것은 불변식이 아니라 부수효과다. lane 하나가 어떤 이유로든 다른 count를 받으면 같은 physical link의 lane들이 서로 다른 시각의 Jakes 값을 쓰게 되고, **그 순간 `y = Hx`가 하나의 `x`에 대한 것이 아니게 된다** — M0 §1.1이 커서에 대해 지적한 것과 정확히 같은 실패다.

---

## 2. 설계

### 2.1 link seed → lane seed 파생

physical link마다 시드 하나를 정의하고, lane 시드를 그 시드에서 파생한다.

```cpp
// 한 곳에서만 정의한다. lane_key()와 같은 취급.
std::uint64_t physical_link_seed(const std::string& base_link_key);
std::uint64_t lane_fading_seed(std::uint64_t link_seed, int rx_port, int tx_port, int step_index);
```

- `physical_link_seed`는 **lane suffix가 없는 base link key**(`"gnb>ue:h"`)에서 파생한다. 포트 수를 바꿔도 링크의 정체성은 유지된다.
- `lane_fading_seed`는 링크 시드와 `(r, t, step)`을 섞는다. 문자열이 아니라 정수 연산으로 — 키 포맷 변경이 realization을 바꾸지 못하게 한다.
- 두 백엔드가 같은 함수를 호출한다. 지금처럼 각자 해시식을 적어 두면 언젠가 갈라진다.

**1×1 호환**: `r = t = 0`, `step = 0`에서 나오는 값이 M1까지의 `hash(base_key + ":fading:0")`과 같을 필요는 없다 — M2는 realization이 바뀌어도 되는 마일스톤이다. 다만 **바뀐다는 사실을 명시**하고, 기존 페이딩 테스트(결정성·정지성·LOS)가 새 시드에서도 통과하는지 확인한다. 바뀌지 않아야 하는 것은 `fading` 없는 경로의 bit-exact 뿐이다.

### 2.2 절대시간의 단일 소유

`slot_start_samples`를 lane별 상태에서 **physical link별 상태**로 승격한다.

두 가지 구현안이 있다.

| 안 | 방법 | 평가 |
|---|---|---|
| A | lane 상태에 남기되, 노드 producer가 슬롯마다 모든 lane에 같은 값을 써 넣는다 | 최소 변경. 그러나 "쓰는 사람이 규율을 지킨다"에 의존 — 오늘과 같은 부수효과 |
| B | `PhysicalLinkState`를 도입해 `slot_start_samples`를 거기 두고 lane이 참조한다 | 구조적 불변식. lane이 자기 시각을 가질 수 없다 |

**B를 택한다.** M0에서 커서를 producer 한 곳으로 모은 것과 같은 이유다 — 어긋난 상태를 *표현할 수 없게* 만드는 편이, 어긋나지 않도록 관리하는 것보다 낫다.

CUDA 쪽은 `DeviceLinkState::slot_start_samples`가 커널이 읽는 필드다. lane마다 복사본을 두되 **producer가 아니라 prepare/advance 경로가 physical link의 값에서 브로드캐스트**하도록 바꾼다. 커널 시그니처는 건드리지 않는다.

### 2.3 `apply_channel_kernel` 무수정

`MIMO_MILESTONES.md` §2 M2가 명시한 대로 페이딩 커널 본문은 손대지 않는다. M2는 **파라미터 파생과 시간 소유권**만 바꾼다. 커널이 읽는 `tap_alpha` / `tap_phi` / `slot_start_samples`의 *값*이 달라질 뿐이다.

이 제약이 M2를 단독 검증 가능하게 만든다 — 통계 게이트가 실패하면 원인은 파생식이지 커널이 아니다.

---

## 3. 작업 순서 (커밋 단위)

| # | 내용 | 검증 |
|---|---|---|
| M2.1 | 이 문서 | — |
| M2.2 | `physical_link_seed` / `lane_fading_seed` 도입, 두 백엔드가 공유 | ctest 8/8, 기존 페이딩 테스트 통과 |
| M2.3 | `slot_start_samples`를 physical link 소유로 승격 (안 B) | ctest 8/8, chunk invariance 테스트 |
| M2.4 | lane별 자기상관 / lane 간 교차상관 통계 테스트 | Exit 게이트 |

---

## 4. Exit 게이트

`MIMO_MILESTONES.md` M2와 동일하며, 판정 방법을 명시한다.

1. **lane별 자기상관이 `J₀(2π f_d τ)`와 일치** — ~~기존 Bessel 테스트 방식을 재사용하고 허용오차 ±0.15. 각 lane을 독립적으로 판정한다.~~

   **구현하며 정정(M2.2).** lane 하나를 J₀와 ±0.15로 판정하는 것은 물리적으로 불가능하다. J₀는 sub-ray 각도에 대한 **앙상블 평균**이고, `M = 20`짜리 realization 하나는 자기 곡선 `(1/M)Σ cos(2π f_d cos(α_m) τ)`를 정확히 따른다 — 실측으로 τ = 5 ms에서 J₀와 −0.32 ~ +0.51까지 벌어진다. 이 계획이 인용한 "σ ~ 0.03"은 Phase 1.5 주석에서 넘어온 값인데, 그것은 시간 평균 오차만 센 것이고 **각도 draw 노이즈는 시간 평균으로 줄지 않는다**(한 realization 안에서 α는 고정이다). 실제로 재시드만으로 기존 단일 lane 게이트가 깨졌고, 커널은 정확했다.

   판정 방식을 둘로 나눈다. (a) lane마다 **자기 시드가 뽑은 각도의** sum-of-sinusoids 예측과 ±0.10 일치 — 생성기의 시간 전개·grid 보간·슬롯 간 연속성을 조이게 검사하고, 예측을 `lane_fading_seed`가 뽑는 각도로 계산하므로 **파생식 자체를 고정**한다. (b) 한 물리 링크의 **16 lane 평균**이 J₀와 ±0.15 일치 — 이 마일스톤이 말하는 분포 성질. M2가 (b)를 싸게 만든다: 링크의 lane들이 바로 그 앙상블이다.
2. **lane 간 교차상관 ≈ 0** — 2×2의 4개 lane 쌍에 대해 정규화 교차상관의 절댓값이 임계값 이하. IID의 정의이고, M3에서 이 값이 **선언한 `R`과 일치**하도록 바뀐다. 즉 M2의 이 게이트는 M3에서 대체된다.
3. **chunk invariance** — 같은 총 샘플 수를 한 번에 처리한 결과와 여러 청크로 나눠 처리한 결과가 일치. 절대시간 소유권이 깨지면 여기서 잡힌다.
4. **CPU ↔ CUDA parity** — 페이딩은 결정론적 시드에서 나오므로 두 백엔드가 같은 realization을 내야 한다. 기존 parity 방식 재사용.
5. **비페이딩 경로 bit-exact** — `fading` 없는 모델의 출력이 M1과 bit-exact. M2가 시드 파생을 바꾸므로, 페이딩을 쓰지 않는 경로가 영향받지 않았음을 명시적으로 확인한다.

### 검증 방법에 관한 주의

M1.6에서 새로 추가한 CUDA 테스트가 **가드 매크로 이름이 틀려 컴파일에서 빠진 채 통과**한 사례가 있다. M2의 통계 테스트는 특히 이 함정에 취약하다 — 허용오차가 느슨하면 아무것도 검증하지 않으면서 통과하기 쉽다.

**모든 신규 통계 테스트는 뮤테이션 프로브를 동반한다.** 최소한:
- 자기상관 테스트: `f_d_max`를 2배로 바꾸면 FAIL해야 한다
- 교차상관 테스트: 두 lane에 같은 시드를 주면 FAIL해야 한다
- chunk invariance: 청크 경계에서 `slot_start_samples`를 일부러 어긋내면 FAIL해야 한다

---

## 5. 비고 — M0 부채는 그대로다

multi-UE / multi-gNB 라이브 게이트는 여전히 환경 차단 상태다(unprivileged LXC + lock-step 가상시간의 지터 부재; `AGENT_PROGRESS.md` M0 섹션). M2의 exit 게이트는 전부 합성·단위 테스트로 판정되므로 이 제약에 걸리지 않지만, **M2가 그 부채를 줄여주지도 않는다.**
