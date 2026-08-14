# M1 — 차원 도입 + 고정 행렬 상세 설계

상위 문서: [`MIMO_MILESTONES.md`](../../MIMO_MILESTONES.md) · 선행: [`m0-single-engine-refactor.md`](m0-single-engine-refactor.md)

**M1의 목표는 확률적 페이딩이 아니다. 차원을 도입하고, 결정론적 고정 행렬 하나를 정확히 적용하는 것이다.** 이 마일스톤이 끝난 시점에 `Nt`/`Nr`가 1보다 클 수 있고, `y = Hx`가 해석적 기대값과 정확히 일치해야 하며, 1×1 레거시 출력은 M0과 bit-exact여야 한다.

M0이 스레드 구조를 단독 검증했듯, M1은 **차원과 행렬을 페이딩 없이 단독 검증**한다. 이전 시도가 스키마·coordinator·행렬·CUDA를 한 덩어리로 넣어 회귀 원인을 분리하지 못했던 지점이다.

---

## 1. 범위

### 들어가는 것

1. `radio_nodes` YAML 스키마와 파서. 브로커의 implicit singleton lowering을 명시 선언으로 대체(공존).
2. `links.from/to`가 RadioNode ID를 참조. 방향마다 physical link 하나.
3. lane 전개 `Nt × Nr`, `lane = r * Nt + t`. 노드의 incoming lane을 행별로 정렬.
4. model-scope `fixed_mimo`: sparse `coefficients`.
5. `superpose_kernel`을 `grid.y = Nr`로 확장 + `row_begin[]` 업로드. `device_output`을 `Nr × count`로.
6. CPU `rx_model` 상태 키를 행별로 분리 (M0에서 코드 주석으로 남긴 항목).
7. validator 규칙 일습.
8. `event=radio_node_resolved`의 `implicit=false` 경로.

### 들어가지 않는 것

- 확률적 페이딩의 lane별 파라미터 파생 (M2)
- 공간 상관, coherent LOS (M3)
- physical link 단위 runtime control (M4)
- `apply_channel_kernel` 본문 수정. **이미 `grid.y = n_links`로 lane별 병렬이므로 무수정.**

---

## 2. 스키마

### 2.1 `radio_nodes`

```yaml
radio_nodes:
  - id: gnb0
    tx_ports: [gnb0_p0, gnb0_p1]      # ← 거부. flow list는 순서 오독을 부른다.
```

**flow 리스트는 거부한다.** 반드시 block 리스트로 쓴다:

```yaml
radio_nodes:
  - id: gnb0
    tx_ports:
      - gnb0_p0
      - gnb0_p1
    rx_ports:
      - gnb0_p0
      - gnb0_p1
```

**작성 순서가 canonical matrix index다.** `tx_ports[0]`가 `t=0`, `rx_ports[1]`이 `r=1`. 이 규약이 스키마·커널·테스트·로그에서 단 하나로 고정된다. 숫자 suffix를 파싱해 순서를 추정하지 않는다 — `p0`/`p1`이라는 이름은 사람을 위한 것이고 기계는 순서만 본다.

`tx_ports`와 `rx_ports`는 독립이다. 같은 Device가 양쪽에 등장하는 것이 정상(2.2 참조)이고, `Nt != Nr`인 비대칭 노드도 허용한다.

### 2.2 Device는 여전히 transport port

`DeviceConfig` 스키마는 **변경하지 않는다.** M0에서 확립한 대로 Device = ZMQ 엔드포인트 쌍 + TX ring이고, RadioNode가 그 위에 얹힌다. 기존 SISO YAML은 `radio_nodes` 블록이 없으면 M0의 implicit singleton lowering을 그대로 탄다.

한 Device는 정확히 하나의 RadioNode에 속한다. 여러 노드가 같은 Device를 주장하면 validator가 거부한다.

### 2.3 `fixed_mimo`

model scope에 붙는다. link scope가 아니다 — 행렬은 physical link의 성질이고, 같은 모델을 여러 링크가 공유할 수 있다.

```yaml
models:
  h_swap:
    fixed_mimo:
      coefficients:
        - { tap: 0, rx: 0, tx: 1, real: 1.0, imag: 0.0 }
        - { tap: 0, rx: 1, tx: 0, real: 1.0, imag: 0.0 }
    chain:
      - type: tdl
        taps:
          - delay_samples: 0.0
            gain_db: 0.0
```

- **sparse.** 생략된 lane의 계수는 **0**이다. 1로 채우지 않는다 — 명시하지 않은 경로는 존재하지 않는 경로다.
- **암묵적 `1/sqrt(Nt)` 정규화 없음.** 전력 정규화를 원하면 계수에 직접 쓴다. 숨은 스케일링은 해석적 기대값 검증을 불가능하게 만든다.
- `tap`은 chain의 tdl step 내 tap 인덱스다. 다중 tap 프로파일에 tap별 서로 다른 행렬을 줄 수 있다.
- 계수는 `a·e^{jφ}` 형태로 기존 `tap_gain_amp / tap_cos_phi / tap_sin_phi`에 접힌다. **새 device 필드가 필요 없다.**

---

## 3. lane 전개

physical link `A → B`에 대해 `Nt = |A.tx_ports|`, `Nr = |B.rx_ports|`이고 lane 수는 `Nt × Nr`.

```text
lane = r * Nt + t          (r: RX 포트 인덱스, t: TX 포트 인덱스)
```

이 flatten 순서를 M3의 상관 행렬까지 그대로 쓴다. **한 곳에서만 정의하고 모든 소비자가 그 정의를 참조한다.**

### 3.1 lane별 상태 키

현재 `link_key(link)` = `"from>to:model"`이다. lane마다 독립적인 CFO 위상·delay line·페이딩 상태가 필요하므로 키를 확장한다:

```text
"gnb0>ue0:h_swap"        (M0, 1x1)
"gnb0>ue0:h_swap#r1t0"   (M1, lane)
```

1×1에서는 `Nt = Nr = 1`이므로 `#r0t0` 하나뿐이다. **레거시 bit-exact 요구를 지키려면 1×1 경로의 키가 M0과 동일해야 하므로, `Nt == 1 && Nr == 1`일 때는 suffix를 붙이지 않는다.** 이 조건 분기는 키 생성 함수 한 곳에만 존재한다.

### 3.2 행별 정렬과 `row_begin[]`

한 노드에 여러 physical link가 들어오면(간섭) lane이 링크별로 섞인다. prepare에서 노드의 전체 incoming lane을 **`rx_port` 기준으로 안정 정렬**하고, 행 경계를 올린다:

```text
row_begin[r] .. row_begin[r+1]   ← 행 r에 누산될 lane 구간
row_begin[Nr] == total_lanes
```

안정 정렬이므로 같은 행 안에서 lane의 상대 순서는 YAML 순서를 따른다. 부동소수 합산 순서가 결정되므로 CPU↔CUDA parity와 재현성이 유지된다.

---

## 4. 백엔드 변경

### 4.1 CPU

이미 `outputs[lane.rx_port]`에 누산한다(M0.2). 남은 것은 하나:

- **`rx_model` 상태 키를 행별로 분리.** M0은 모든 행에 `"<node>>rx"` 하나를 썼고, 코드에 M1 항목으로 표시해 두었다. `Nr > 1`에서 sibling 행이 수신 체인의 CFO 위상과 delay line을 공유하면 안 된다. 키를 `"<node>>rx#r<N>"`로 바꾸되, **`Nr == 1`이면 `"<node>>rx"`를 유지**한다(§3.1과 같은 이유).

### 4.2 CUDA

`apply_channel_kernel`은 **무수정.** 이미 `grid.y = n_links`로 lane별 병렬이고, 2×2는 lane 4개일 뿐이다.

`superpose_kernel`만 확장한다 (현재 `src/cuda_backend.cu:179`):

```cpp
__global__ void superpose_kernel(IqSample* dst, std::size_t count,
                                 const int* row_begin, int nr,
                                 const IqSample* staged,
                                 const GpuStep* steps, const int* step_meta)
{
  const std::size_t idx = blockIdx.x * blockDim.x + threadIdx.x;
  const int r = blockIdx.y;                       // ← grid.y = Nr
  if (idx >= count || r >= nr) return;
  float acc_i = 0.0F, acc_q = 0.0F;
  for (int k = row_begin[r]; k != row_begin[r + 1]; ++k) {   // ← 행 구간만
    ...기존 본문 그대로...
  }
  dst[static_cast<std::size_t>(r) * count + idx] = {acc_i, acc_q};
}
```

동반 변경:
- `device_output` 크기 `count` → `Nr × count`, `host_output`도 동일.
- `row_begin[]`을 prepare에서 1회 업로드 (`Nr + 1` 정수). hot path에 없다.
- `apply_steps_kernel`(rx_model)도 `grid.y = Nr`로 행마다 적용.
- M0.2에서 넣은 `outputs.size() != 1` 거부를 해제.

**source dedup이 여기서 처음 실제로 쓰인다.** lane이 `Nt·Nr`개여도 `DeviceLinkState::src_index` 덕분에 H2D는 **`Nt × count`**만 올라간다. 2×2에서 lane 4개, 업로드 2개 분량.

---

## 5. validator

M1에서 추가하는 거부 규칙. 각각 테스트 케이스를 동반한다.

| 규칙 | 이유 |
|---|---|
| 존재하지 않는 Device 참조 | 오타가 조용히 포트를 누락시키는 것을 막는다 |
| RadioNode ID와 Device ID 충돌 | `links.from`이 어느 쪽을 가리키는지 모호해진다 |
| 한 Device가 복수 RadioNode에 소속 | 한 TX ring에 두 노드의 커서가 붙는다 |
| 한 노드 안 포트 중복 | 같은 포트가 두 matrix index를 갖게 된다 |
| sibling 포트 sample-rate 불일치 | 노드는 공통 sample epoch의 소유자다 |
| sibling 포트 `tx_timing_offset_samples` 불일치 | §1.3의 원점 정렬 완화책. 일치하면 그 값을 노드 공통 start offset으로 승격 |
| `fixed_mimo` 계수의 `rx`/`tx`가 차원 밖 | 행렬 차원 불일치를 조용히 넘기지 않는다 |
| `fixed_mimo` 계수의 `tap`이 chain의 tap 수 밖 | 위와 동일 |
| 같은 `{tap, rx, tx}` 중복 | 마지막 값이 이기는 동작을 금지 |
| `radio_nodes`가 있는데 flow 리스트 사용 | §2.1 |

---

## 6. 작업 순서 (커밋 단위)

각 단계마다 `ctest`를 통과시키고 넘어간다.

| # | 내용 | 검증 |
|---|---|---|
| M1.1 | 이 문서 | — |
| M1.2 | `RadioNodeConfig` 스키마 + 파서 + validator. 소비자 없음 | 신규 config 테스트, 기존 ctest 무회귀 |
| M1.3 | 브로커 lowering을 스키마 우선 / implicit fallback으로. `implicit=false` 로그 | ctest, 1×1 무변화 |
| M1.4 | lane 전개 + 행별 정렬 + lane 키 (`Nt=Nr=1`이면 M0 키 유지) | **1×1 bit-exact vs M0** |
| M1.5 | `fixed_mimo` 파싱 + CPU 적용 + 행별 rx_model 키 | identity / swap / known-H 해석 테스트 |
| M1.6 | CUDA `superpose_kernel` `grid.y = Nr` + `row_begin[]` + 출력 크기 | CPU↔CUDA parity 1e-3 |
| M1.7 | marker 테스트, 비대칭 차원(2×1, 1×2) 테스트 | 전체 exit 게이트 |

M1.4의 **1×1 bit-exact**가 이 마일스톤의 안전망이다. 여기서 어긋나면 lane 전개가 레거시 경로의 의미를 바꾼 것이고, 그 뒤 어떤 행렬 테스트도 신뢰할 수 없다.

---

## 7. Exit 게이트

`MIMO_MILESTONES.md` M1과 동일하며, 여기서는 각 항목의 판정 방법을 명시한다.

1. **합성 2-port peer**: identity `H` → 입력 그대로 통과. swap `H` → 행 교환. known `H` → 해석적 기대값과 **정확히** 일치 (고정 행렬이므로 확률 없음).
2. **marker 테스트** (`MIMO_MILESTONES.md` §1.3): 포트 0에만 구별 가능한 패턴을 주입하고, 지정된 lane에서만 나타나는지 확인. **포트 swap과 원점 skew를 동시에 잡는다** — 이 두 개는 단위 테스트로는 구분되지 않는 실패 모드다.
3. **CPU ↔ CUDA parity** 1e-3.
4. **1×1 레거시 bit-exact vs M0.**
5. **비대칭 차원** 2×1, 1×2 단위 테스트.

`event=radio_node_resolved`가 `implicit=false`로 멤버십과 순서를 출력하는지도 확인한다 — 포트 순서가 로그에 남지 않으면 marker 테스트 실패 시 원인 분리가 불가능하다.

---

## 8. 비고: 라이브 검증의 현재 한계

M0에서 확인된 대로 이 환경에서는 multi-UE 라이브 attach가 불가능하다(unprivileged LXC + lock-step 가상시간의 지터 부재; `AGENT_PROGRESS.md` M0 섹션 참조). M1의 exit 게이트는 **전부 합성·단위 테스트로 판정 가능하도록** 설계되어 있으므로 이 제약에 걸리지 않는다.

다만 `MIMO_MILESTONES.md` M5의 라이브 게이트는 여전히 미해결 부채로 남아 있고, M1이 그 부채를 줄여주지 않는다는 점을 분명히 해 둔다.
