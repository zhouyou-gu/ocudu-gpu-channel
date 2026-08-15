# M3 — 공간 상관 + coherent LOS 상세 설계

상위 문서: [`MIMO_MILESTONES.md`](../../MIMO_MILESTONES.md) · 선행: [`m2-iid-stochastic-fading.md`](m2-iid-stochastic-fading.md)

**M3의 목표는 lane 간 관계를 선언한 것과 일치시키는 것이다.** M2는 lane마다 독립인 realization을 정확히 만들고 그 소유권을 physical link에 두었다. M3은 그 위에 관계를 얹는다 — 산란 성분에는 2차 통계(상관 행렬 `R`), LOS 성분에는 결정론적 위상 관계. 이 마일스톤이 끝난 시점에 한 물리 링크의 lane 벡터는 선언한 `R`을 공분산으로 갖고, LOS 지배 구간에서 포트 간 위상차가 선언한 행렬과 일치한다.

M2가 확률 생성기를 상관 없이 단독 검증했으므로, 여기서 통계 게이트가 실패하면 원인은 mixing이지 생성기가 아니다. 그 분리를 유지하는 것이 이 계획의 작업 순서를 결정한다(§4).

---

## 1. 지금 상태 — 코드에서 확인한 다섯 가지

계획을 세우며 실제로 읽고 확인한 것만 적는다. M2 계획이 검증하지 않은 수치 하나(`σ ~ 0.03`)를 주석에서 옮겨 적었다가 구현 중 정정한 일이 있었다.

### 1.1 생성기가 convolution 안에 있다 — M3의 본체는 이것을 빼내는 일이다

- **디바이스**: `src/device_channel.cu`의 `apply_channel_kernel`이 블록 시작에서 `g_grid_i/q[tap][gridpoint]`를 **shared memory에 협력적으로 재료화**한 뒤(83–166행) 각 스레드가 자기 샘플로 보간한다(189–212행).
- **호스트**: `include/ocudu_gpu_channel/delay.h`의 `apply_tdl_step_fading`이 같은 grid를 호출마다 `std::vector`로 만든다(312–342행).

둘 다 **lane 하나만 보고** grid를 만든다. 상관은 같은 시각·같은 tap·같은 grid point에서 **lane들을 동시에** 봐야 부여할 수 있으므로, 이 구조로는 표현할 방법이 없다. 커널 안에서 lane 루프를 돌게 만드는 대안은 §3에서 기각한다.

부수적 사실 하나: 디바이스는 한 edge의 **모든 블록이 같은 grid를 다시 만든다.** `count = 23040`, 블록당 256 스레드면 edge마다 90개 블록이 동일한 계산을 90번 한다. 분리하면 이 중복이 사라지지만, **성능 주장은 측정 뒤에 적는다**(하네스 규칙). M3.7에서 잰다.

### 1.2 한 링크의 lane들은 노드의 lane 배열에서 연속이 아니다

`resolve_topology`는 링크마다 `r` major / `t` minor로 lane을 밀어 넣은 뒤 `(dst_node, rx_port)`로 **stable sort** 한다(`src/config.cpp:1432`). 결과적으로 한 **행**의 lane은 연속이지만(M1.6의 `row_begin[]`이 여기 의존한다), 한 **링크**의 lane은 행마다 다른 링크의 lane과 교차한다. 노드에 링크 A·B가 들어오면 배열은 `A(r0,t*) B(r0,t*) A(r1,t*) B(r1,t*)` 순이다.

따라서 mixing에는 **링크별 lane 인덱스 테이블**이 따로 필요하다: `ℓ = r*Nt + t` → 노드 lane 배열에서의 위치. 정렬을 바꿔서 해결하려 들면 안 된다 — 그 정렬이 M1.6의 행 구간과 CPU↔CUDA 합산 순서 고정의 근거다.

**그 테이블은 `ResolvedTopology`가 한 번만 만든다.** 백엔드마다 따로 계산하게 두면 안 된다 — 그것이 정확히 M1.4가 막으려던 실패다(`config.h`의 `ResolvedTopology` 주석: 세 소비자가 lane을 각자 유도하면 반드시 어긋나고 **그 어긋남은 조용하다**). 링크 그룹은 lane 집합의 두 번째 view이므로 lane 집합과 같은 자리에서 나와야 한다. 구체적으로 `ResolvedTopology`에 `struct LinkLaneGroup { std::string physical_link_key; int nt, nr; std::vector<int> lane_index; }`를 두고 `lane_index[ℓ] = lanes[] 안의 위치` (`ℓ = r*Nt + t`)로 채운다. 이 자리는 M3.3의 첫 커밋에 포함된다.

### 1.3 `fixed_mimo`는 계수 0인 lane을 아예 만들지 않는다

`src/config.cpp:1408`에서 live하지 않은 lane은 `continue`로 건너뛴다. M1.5b의 의도적 설계다("생략 lane은 0이지 1이 아니다"). 그런데 상관 행렬은 **전체 `Nt·Nr` lane 벡터** 위에 정의된다. 둘을 함께 선언하면 `H`를 두 가지 방식으로 동시에 말하는 셈이 된다 — 하나는 결정론적 고정 행렬, 하나는 확률 행렬의 공분산. §2.6에서 처리한다.

### 1.4 LOS 위상이 lane마다 독립 draw다

`prepare_tdl_fading_state`(`delay.h`)가 tap마다 `tap_phi_los`를 그 lane의 RNG에서 뽑는다. 즉 오늘의 LOS는 lane 간 위상 관계가 **무작위**다. coherent LOS는 정확히 이것을 금지한다("lane마다 LOS 위상을 독립 draw하지 않는다", `MIMO_MILESTONES.md` M3).

### 1.5 시드와 시간의 소유권은 이미 물리 링크에 있다

M2가 남긴 것이고 M3은 **바꾸지 않는다**. `physical_link_seed(base_link_key)` → `lane_fading_seed(link_seed, r, t, step)`가 lane별 독립 draw `w`를 만들고, M3은 그 `w` 위에 `L`을 곱한다. 시드 파생식을 건드리지 않으므로 M2의 재현성·툴체인 독립성이 그대로 상속된다. `PhysicalLinkClock`도 그대로다 — lane들이 같은 시각을 본다는 것이 `L` 곱셈이 의미를 갖기 위한 전제인데, 그것은 이미 구조적으로 성립한다.

---

## 2. 설계

### 2.1 생성기와 convolution 분리

```
[생성기]  w[lane][tap][grid]  (lane별 독립 Jakes, M2 그대로)
             ↓  g = L·w        (tap마다, grid point마다, lane 축을 가로질러)
[버퍼]    g[lane][tap][grid]
             ↓
[convolution]  grid를 읽기만 한다
```

- **디바이스**: 새 커널 `generate_fading_grid_kernel`이 노드별 전역 버퍼에 `g`를 쓴다. `apply_channel_kernel`은 블록 시작에서 자기 lane의 grid를 shared로 **복사**한다 — 계산에서 로드로 바뀔 뿐이라 per-sample 수학은 손대지 않는다. 이 커널 변경은 M3에서 한 번 하고 동결한다("이후 convolution 커널은 다시 건드리지 않는다").
- **호스트**: `process_superposition`이 lane을 shaping하기 **전에** 링크별 선행 패스로 `g`를 만든다. `apply_tdl_step_fading`은 grid를 만들지 않고 받는다.

**상태의 자리.** `PhysicalLinkFadingState`를 `include/ocudu_gpu_channel/link_clock.h` 옆에 둔다(파일명은 `physical_link.h`로 바꾸고 `PhysicalLinkClock`을 함께 옮기는 것을 권고 — 시계와 생성기는 같은 소유자의 상태다). 물리 링크당 하나, 슬롯당 갱신, lane은 참조만. M2.3에서 시계에 적용한 것과 같은 형태이고 이유도 같다: **어긋난 상태를 표현할 수 없게 만든다.**

디바이스 버퍼는 목적지 노드별(`CudaSuperposeState`)로 잡는다. 한 링크의 lane은 전부 같은 목적지 노드로 들어오므로 노드 단위 할당으로 충분하다. 크기는 `lanes × taps × grid_points × 2 float`: 4×4 링크에 TDL-A(23 tap, grid 11)이면 32 KB로 무시할 수준이다.

### 2.2 `spatial_correlation` 스키마

모델 스코프. `fixed_mimo`의 형제다.

```yaml
models:
  h:
    chain: [...]
    spatial_correlation:
      kind: kronecker          # iid | kronecker | full
      rx:                      # Nr x Nr. 상삼각(i < j)만 쓴다.
        - i: 0
          j: 1
          re: 0.3
          im: 0.1
      tx:                      # Nt x Nt. 같은 규칙.
        - i: 0
          j: 1
          re: 0.7
          im: 0.0
```

**행렬을 통째로 쓰지 않고 상삼각 성분만 쓴다.** 두 가지 이유다. (a) 이 프로젝트의 YAML 파서는 flow 스타일(`[ ... ]`, `{ ... }`)을 전역 거부하므로 계획 초안에 적었던 중첩 배열 문법은 애초에 파싱되지 않는다. (b) 더 중요하게, 대각을 1로 고정하고 하삼각을 켤레 미러로 채우면 **Hermitian이 아닌 행렬과 대각이 1이 아닌 행렬을 아예 표현할 수 없다.** 검증으로 잡는 것보다 표현 불가능하게 만드는 편이 낫다는 이 워크스페이스의 원칙 그대로다. 남는 검증은 PSD 하나뿐이고, 그건 어떤 문법으로도 막을 수 없다.

- `kind: iid`가 기본값이자 M2의 동작(`R = I`)이다. 블록을 쓰지 않은 기존 YAML은 전부 여기에 해당한다.
- validator: Hermitian(대칭 + 켤레), 대각 1, PSD, 차원이 노드의 `Nt`/`Nr`와 일치, 상관 선언 모델의 chain에 fading이 켜진 tdl 스텝이 있을 것.
- **인수분해는 LDLᴴ을 쓴다.** Cholesky는 PSD-but-singular 행렬에서 실패하는데, 완전 상관(`0.999…`가 아니라 정확히 `1.0`)은 극단 케이스 테스트로 실제로 쓰고 싶은 입력이다. LDLᴴ은 `D ≥ 0`이면 항상 존재하고, `L' = L·√D`가 그대로 mixing 행렬이 되며, **PSD 판정과 인수분해가 한 루틴에서 나온다**(`D_i < -eps`면 거부). prepare에서 complex double로 1회, hot path에서 재분해 없음.

### 2.3 flatten 순서와 Kronecker 규약 — 한 가지로 고정한다

lane 벡터 `h`의 순서는 **`ℓ = r*Nt + t`** (rx가 바깥, tx가 안쪽). `resolve_topology`가 링크 안에서 이미 이 순서로 만든다(§1.2).

그 순서에서 이 프로젝트가 쓰는 정의는:

```
E[h hᴴ] = R_rx ⊗ R_tx        (선언한 tx 블록을 transpose도 conjugate도 하지 않고 그대로)
```

**왜 명시하는가.** 문헌은 보통 `vec(H)`(열 스택, `t`가 바깥)로 쓰고 `E[vec vecᴴ] = R_txᵀ ⊗ R_rx`를 얻는다. 순서를 바꾸면 ⊗의 좌우가 바뀌지만 **transpose는 `R_tx`에 붙어 따라온다.** 실수 대칭 `R_tx`에서는 차이가 없고 복소 `R_tx`에서만 갈라진다 — 그래서 조용히 틀리기 딱 좋다. 규약은 위 한 줄이고, exit 게이트가 정확히 그 식을 측정한다. 3GPP 스타일로 정의된 `R_tx`를 가져오는 사용자는 conjugate해서 넣어야 하며(실수 행렬이면 no-op), 그 사실을 스키마 문서에 적는다.

### 2.4 coherent LOS

`MIMO_MILESTONES.md` M3의 합성식:

```
H_ℓ = sqrt(P_ℓ/(K+1))·H_NLOS,corr + sqrt(P_ℓ·K/(K+1))·H_LOS,coh
```

오늘 커널의 per-tap `los_factor` / `rayleigh_factor`가 이미 그 두 스칼라다(`device_channel.cu:204-207`). 바뀌는 것은 **`H_LOS,coh`의 lane 성분이 어디서 오는가**뿐이다: lane RNG의 독립 draw(`tap_phi_los`)가 아니라 **선언된 행렬**에서 온다.

**기하에서 유도하지 않는다.** `AGENT_GOAL.mimo.md`의 비목표가 안테나 배열 기하·빔포밍·프리코더·CDL을 사용자의 명시적 확장 없이는 제외한다. 따라서 LOS 위상은 안테나 간격에서 계산하는 것이 아니라 `fixed_mimo`와 같은 sparse 복소 계수 형태로 **선언**한다:

```yaml
    los_matrix:
      coefficients:
        - { rx: 0, tx: 0, re: 1.0, im: 0.0 }
        - { rx: 0, tx: 1, re: 0.0, im: 1.0 }
```

**선언이 없을 때의 기본값은 전 lane 위상 0(rank-1 all-ones)이다.** LOS는 물리적으로 rank-1 성분이므로 기본값으로 타당하고, "독립 draw 금지"의 최소 형태다.

이것은 M2까지의 동작을 바꾼다 — LOS tap을 가진 프로파일(TDL-D/E)의 출력이 달라진다. `gpu-test-sequence` `[7/7]`은 TDL-A(NLOS)라 영향이 없지만, 변경 사실은 M3.5 커밋에 명시한다.

### 2.5 무엇을 건드리지 않는가

- `physical_link_seed` / `lane_fading_seed` (M2.2) — mixing은 그 **위에** 얹힌다.
- `PhysicalLinkClock` (M2.3).
- convolution의 per-sample 수학. grid를 만들지 않고 읽는 것만 바뀐다.
- runtime control로 `R`을 바꾸는 것은 **M4**다. M3의 `R`은 prepare에서 고정된다.
- `rx_model`. 수신 모델은 lane이 없다.

### 2.6 `fixed_mimo`와 동시 선언 — 거부한다

`fixed_mimo`는 결정론적 `H`를, `spatial_correlation`은 확률 `H`의 공분산을 말한다. 동시에 선언하면 "상관된 무작위 채널에 고정 계수를 곱한 것"이 되는데, 그것은 두 knob 중 어느 쪽도 광고하지 않는 의미다. 게다가 §1.3 때문에 lane 집합 자체가 줄어들어 mixing이 `R`의 부분행렬 위에서 일어난다.

**validator가 `fixed_mimo` + `kind != iid` 조합을 거부한다.** 수학적으로는 살릴 수 있다(줄어든 lane 집합의 공분산은 `R`의 해당 부분행렬이므로 marginal로는 정당하다). 그러나 살리는 순간 "선언한 `R`과 일치한다"는 exit 게이트가 "선언한 `R`의 어떤 부분행렬과 일치한다"로 약해진다. 뒤집기 쉬운 결정이므로 §7에 열어 둔다.

**M3 종료 시점 재검토 결과 (2026-08-15): 통합하지 않는다.** 두 knob은 겉모습만 닮았고 **없는 항목의 의미가 정반대**다. `fixed_mimo`에서 안 쓴 lane은 **0**이다 — "아무도 적지 않은 경로는 존재하지 않는다"가 M1.5b의 명시적 결정이고, 그래서 그 lane은 아예 생성되지 않는다. `los_matrix`에서 안 쓴 lane은 **에러**다 — 절반만 쓰고 나머지가 조용히 기본값이 되는 것을 막기 위해 전량 선언을 요구한다. 통합하면 둘 중 하나의 규칙을 다른 쪽에 강요하게 되고, 그것은 중복을 없애는 대신 의미를 하나 망가뜨리는 거래다. 게다가 `fixed_mimo`는 M1의 해석적 게이트(identity / swap / known `H`)를 지탱한다. **중복은 표기뿐이고 의미는 다르므로 둘로 남긴다.**

**이 거부는 문제를 푸는 것이 아니라 경계를 긋는 것이다.** 그리고 M3이 `los_matrix`(§2.4)를 들여오면 워크스페이스에 **lane별 복소 행렬을 YAML로 선언하는 knob이 둘**이 된다 — `fixed_mimo`(결정론적 `H` 자체)와 `los_matrix`(`H`의 LOS 성분). 둘은 "페이딩이 곱해지는가"만 다르다. 지금 합치지 않는 이유는 `fixed_mimo`가 M1의 해석적 게이트(identity / swap / known `H`)를 지탱하는 **검증 도구**이고 그 게이트를 흔들 이유가 없어서다. 그러나 knob 두 개가 같은 것을 두 방식으로 말하는 상태는 그대로 두면 굳는다. **M3 종료 시점에 통합 여부를 한 번 재검토하고, 미룬다면 왜 미루는지 기록한다.**

---

## 3. 어디서 상관을 곱하는가 — 대안 비교

| 안 | 방법 | 평가 |
|---|---|---|
| A | convolution 커널 안에서 lane 축을 돈다 | 블록 구조가 edge 단위(`blockIdx.y = k`)라 lane 간 상태를 볼 수 없다. 볼 수 있게 재구성하면 **모델이 바뀔 때마다 convolution 커널이 바뀐다** — 밀레스톤이 명시적으로 피하려는 것. 기각 |
| B | 생성기를 별도 커널로 분리, 전역 grid 버퍼, convolution은 읽기만 | 채택. 커널 경계가 한 번만 바뀌고 이후 CDL로 확장해도 생성기만 바뀐다 |
| C | 호스트에서 grid를 만들어 H2D | slot당 수십 KB로 대역폭은 무시할 수준이지만, Phase 2가 없앤 호스트 stage 비용을 부분적으로 되돌린다. 기각. 단 **CPU 참조 경로는 사실상 이 형태**이고, 그래서 두 백엔드의 수학이 같은 함수에서 나오도록 §4의 순서를 잡는다 |

---

## 4. 작업 순서 (커밋 단위)

**분리와 상관 도입을 한 커밋에 넣지 않는다.** 겹치면 통계 게이트가 실패했을 때 원인이 갈린다 — M2가 커널을 건드리지 않아 얻은 성질을 여기서 스스로 버릴 이유가 없다.

| # | 내용 | 검증 |
|---|---|---|
| M3.1 | 이 문서 | — |
| M3.2 | `spatial_correlation` / `los_matrix` 스키마 + validator + LDLᴴ 인수분해 | `ctest` 8/8, 거부 경로 단위 테스트(비-Hermitian / 비-PSD / 대각≠1 / 차원 불일치 / fading 없는 모델) |
| M3.3 | **생성기 분리만.** mixing 없음(`L = I`). `ResolvedTopology`의 링크별 lane 인덱스 그룹(§1.2)도 여기서 만든다 | **출력이 M2와 bit-exact**여야 한다. 이 커밋의 유일한 게이트가 그것이다 |
| M3.4 | mixing 적용 — CPU 참조 먼저, 그다음 CUDA | 공분산 게이트, CPU↔CUDA parity 1e-3, `iid` 경로는 여전히 M2와 bit-exact |
| M3.5 | coherent LOS | LOS 위상 게이트, TDL-D/E 출력 변경 명시 |
| M3.6 | 통계 게이트 + 뮤테이션 프로브 정리 | Exit 게이트 전체 |
| M3.7 | perf 측정 (`perf-fanin-sweep.sh`에 상관 config 추가) | 측정치를 기록한다. **수치를 먼저 주장하지 않는다** |

`iid` 경로의 bit-exact를 M3.4 이후에도 유지하려면 **`L = I`를 곱셈으로 처리하지 말고 분기로 건너뛴다.** 곱셈을 통과시키면 부동소수 연산 순서가 바뀌어 마지막 비트가 달라지고, "M2가 건드려지지 않았다"는 가장 값싼 회귀 증거를 잃는다.

---

## 5. Exit 게이트

`MIMO_MILESTONES.md` M3과 동일하며, 판정 방법을 명시한다.

| 게이트 | 판정 |
|---|---|
| 경험적 공분산이 선언한 `R`과 일치 | lane 쌍마다 `\|R̂_ij − R_ij\| < tol`. **tol은 실측에서 정한다**(아래) |
| LOS 지배(K 큰 값)에서 포트 간 위상 관계가 선언 행렬과 일치 | lane 쌍의 위상차 vs `arg(H_LOS,i) − arg(H_LOS,j)` |
| CPU ↔ CUDA parity 유지 | 상관 2×2 페이딩, 2슬롯, 1e-3 |
| `iid` 경로가 M2와 동일 | bit-exact (M3.3·M3.4 양쪽에서) |
| lane별 자기상관 유지 | 대각 1인 `R`이면 `Σ_j\|L_ij\|² = 1`이므로 전력과 스펙트럼이 보존된다 |
| `ctest` 8/8 CPU + CUDA, `gpu-test-sequence.sh` 7/7 | 비페이딩 결정론 수치는 불변이어야 한다 |

**허용오차를 정하는 방법 — M2의 교훈을 그대로 적용한다.** `R̂`도 유한 시간·유한 `M`에서 편차를 갖는다. 단일 realization을 앙상블 평균과 비교해 놓고 허용오차를 통과할 때까지 넓히는 실수를 반복하지 않으려면, **먼저 `iid`(`R = I`)에서 `R̂`의 오차 분포를 실측하고** — 그 값이 이 추정기의 잡음 바닥이다 — 허용오차를 그 몇 배로 잡은 뒤 **실측치를 테스트 주석에 적는다.** M2의 교차상관 게이트가 이미 그 바닥을 하나 재 두었다(400 fading cycle에서 최악 쌍 0.065).

**자기상관에 대한 예측 하나가 새로 생긴다.** `g_i = Σ_j L_ij w_j`이고 `w_j`가 독립 Jakes이면 `g_i`의 자기상관은 `Σ_j |L_ij|² R_{w_j}(τ)`, 즉 여러 realization의 가중 평균이다. 따라서 **상관을 주면 lane별 J₀ 일치가 오히려 좋아진다** — 게이트가 느슨해지는 것이 아니라 검증 가능한 예측이 하나 늘어난다. M3.4에서 이것도 확인한다.

**뮤테이션 프로브** (전부 FAIL을 확인한 뒤에만 게이트로 인정한다):

- 비-identity `R`을 선언하고 mixing을 끄면 → 공분산 게이트 FAIL
- `R_rx`와 `R_tx`를 맞바꾸면 → **`Nt ≠ Nr`인 비대칭 토폴로지에서** 공분산 게이트 FAIL (정사각에서는 통과할 수 있으므로 프로브는 비대칭에서 돌린다)
- `R_tx`를 conjugate해서 넣으면 → 복소 `R_tx`에서 FAIL (§2.3 규약이 실제로 고정되었다는 증거)
- 선언 `H_LOS`의 한 lane 위상을 바꾸면 → LOS 게이트 FAIL
- lane flatten 순서를 `t*Nr + r`로 뒤집으면 → 비대칭에서 FAIL

---

## 6. 성능 — 실측 (M3.7)

**측정 환경**: 이 컨테이너, 4× RTX 5090 (driver 570.211.01), CUDA 12.8.93, sm_120 빌드, `ocudu-gpu-channel-bench`, 23.04 MS/s, `batch_samples: 23040`, CUDA 백엔드, 각 3 s. 게이트는 30 kHz SCS의 500 µs 슬롯 기준.

### 6.1 생성기 분리는 커널 시간을 줄였다

분리 전(`47b21de`)과 후를 같은 토폴로지·같은 기계에서 비교했다.

| 토폴로지 (모델 체인) | 분리 전 `kernel_us` p99 | 분리 후 p99 | 차이 |
|---|---|---|---|
| `topology.tdl-a.cuda.yaml` (1×1, TDL-A 23탭 + Jakes 100 Hz) | 61.856 µs | **53.599 µs** | −13% |
| `topology.perf-tdl-a-fanin-8.cuda.yaml` (1 gNB + 8 UE, 16 에지, 동일 체인) | 90.655 µs | **69.536 µs** | −23% |

§1.1이 예측한 대로다 — 분리 전에는 한 에지의 블록 ~90개가 같은 grid를 각각 다시 만들었고, 그 중복이 사라졌다. 에지가 많을수록 이득이 큰 것도 같은 이유다. **줄이려고 한 작업이 아니라 상관을 표현하려고 한 구조 변경의 부수 효과**이므로, 이 수치는 목표가 아니라 결과로 읽어야 한다.

### 6.2 mixing 비용

같은 2×2 TDL-A 토폴로지를 상관 선언만 넣고 빼서 비교했다(`examples/topology.mimo-2x2-correlated.cuda.yaml`, 링크당 4 lane).

| | `kernel_us` p99 | `gpu_process_us` p99 |
|---|---|---|
| `kind: iid` (mixing 커널 미실행) | 59.615 µs | 144.900 µs |
| `kronecker` (R_rx 0.7 / R_tx 0.4) | **63.776 µs** | 146.131 µs |

mixing은 **+4.2 µs (+7%)**이고 500 µs 슬롯 예산의 1% 미만이다. `iid`는 커널을 아예 띄우지 않으므로 상관을 쓰지 않는 토폴로지는 비용이 0이다.

### 6.3 M3.7에서 함께 고친 것 — 벤치가 MIMO를 재지 못하고 있었다

`ocudu_gpu_channel_bench`가 `config.devices`를 돌며 device id로 `process_superposition`을 불렀다. M1부터 dst 키는 **노드** id이므로, 다중 포트 토폴로지에서는 모든 조회가 빗나가 **커널 카운트 0으로 수천만 번 공회전**했다 — 아무것도 재지 않으면서 초록색으로 끝났다. M1이 남긴 구멍이고, M3.7이 그것을 밟았다. 이제 `resolve_topology()`의 노드를 돌고 RX 포트마다 출력 행을 넘긴다. 1×1에서는 노드가 곧 포트이므로 출력 형식은 그대로다.

## 7. 확정된 결정 (2026-08-15, 사용자 승인)

권고안 그대로 확정. 근거는 각 항목에 남긴다.

1. **`fixed_mimo` + `spatial_correlation` 동시 선언 — 거부**(§2.6). 허용하면 exit 게이트가 "선언한 `R`" 대신 "그 부분행렬"을 검증하게 된다. `fixed_mimo` 자체는 그대로 남는다.
2. **`kind: full` — M3에서 제외**, `iid` / `kronecker`만 구현한다. 4×4면 16×16 = 256 성분을 YAML로 써야 하고 쓸 계획이 없다. 파서는 `full`을 **명시적으로** "deferred"라고 거부한다(오타로 오해되지 않게).
3. **LOS 기본값 — 미선언 시 전 lane 위상 0(rank-1 all-ones)**(§2.4). 단, **선언한다면 전 lane을 선언해야 한다** — 절반만 쓰고 나머지가 조용히 기본값이 되는 상태를 없애기 위해 validator가 부분 선언을 거부한다(구현하며 추가한 규칙).

---

## 8. 비고 — M0 부채는 그대로다

multi-UE / multi-gNB 라이브 게이트는 여전히 환경 차단 상태다(unprivileged LXC + lock-step 가상시간의 지터 부재; `AGENT_PROGRESS.md` M0 섹션). M3의 exit 게이트도 전부 합성·단위 테스트로 판정되므로 이 제약에 걸리지 않지만, **M3이 그 부채를 줄여주지도 않는다.**
