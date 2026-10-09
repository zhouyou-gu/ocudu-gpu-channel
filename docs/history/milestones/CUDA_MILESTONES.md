# CUDA 가속 OCUDU gNB 통합 마일스톤 (이 워크스페이스의 정본 로드맵)

> Historical record; its claims apply to the original date, revision and setup. Migrated from `CUDA_MILESTONES.md` at `58d3156` without changing recorded measurements.


미션: [`AGENT_GOAL.md`](../../../AGENT_GOAL.md). 이 트리는 `ocudu-gpu-channel` 브랜치 `miso-siso-sionna-rt`의 `f2e02f9` 포크다 — rank-1 MISO/SIMO(R0–R3)와 Sionna RT 통합이 모두 들어간 상태이며, CUDA gNB 작업은 하나도 들어있지 않다.

목표는 **CUDA 가속 OCUDU gNB를 선택 가능한 추가 프로파일로 통합**하는 것이다. 지금 이 프로젝트에서 GPU 위에 있는 것은 채널 에뮬레이터(브로커 커널)와 Sionna RT뿐이고, gNB는 ZMQ로 받은 IQ를 CPU에서 처리한다. 통합의 목적은 `브로커 CUDA 채널 → CUDA lower-PHY OFDM → CUDA PUSCH 등화/LDPC → device LLR`을 **한 GPU 위에 놓는 것**이지 현재 fixture를 빠르게 만드는 것이 아니다(§성능 주장 경계).

## 절대 제약 (모든 단계에 적용, 위반 시 그 단계는 실패)

1. **기존 파일 무수정.** 모든 신규 기능은 **새 파일**로만 들어간다. 기존 게이트 러너·이너·검증기·공유 검사기·공유 lock은 `f2e02f9` 기준 **바이트 동일**을 유지한다. 매 단계 exit 게이트에 `git diff --stat f2e02f9 -- <기존파일목록>`이 빈 출력임을 증거로 포함한다.
2. **CPU가 기본값.** 어떤 환경변수도 설정하지 않은 상태에서 기존 게이트는 CUDA 이전과 동일하게 동작한다.
3. **벤더 패치는 격리·해시락·제보.** OCUDU 소스 수정은 단일 패치 파일로만 존재하고, 해시로 검증되며, 업스트림 WG1에 제보한다. 그 패치로부터 파생된 모든 주장에 패치 적용 사실을 명시한다.
4. **결함 복원 시 실패하는 회귀.** 수정 하나마다 음성 대조군을 만들고, 그것이 **exit 코드 0이 아님**을 확인한다. 출력 문구만으로 판정하지 않는다.

## 플랫폼 격차 (이 프로젝트의 1순위 리스크)

WG는 단일 플랫폼에서만 검증했다. 우리는 그 어느 축과도 일치하지 않는다.

| 축 | WG 검증 환경 | 이 워크스테이션 |
|---|---|---|
| GPU | GB10 (DGX Spark), **통합 메모리** | RTX 5090, **디스크리트** |
| CPU | aarch64 Cortex-X925/A725 | x86_64 Core Ultra 9 285K (P/E 혼합) |
| CUDA | 13.0.88 / 드라이버 580.95.05 | 12.8.93 / 드라이버 595.71.05 |
| 아키 | `sm_121` | `sm_120` |
| 대역폭·레이어 | 100 MHz · 4 layer · 8 RX | **20 MHz · 1 layer · 1 RX** |

통합 vs 디스크리트가 특히 위험하다. WG 문서 자체가 디스크리트에서 정책이 갈린다고 명시한다 — `ul_cuda_visible_grid_mode: auto`는 디스크리트에서 **pinned**를 고르고(통합은 managed), `pdsch_acceleration_mode: auto`는 디스크리트에서 **host를 선호**한다. managed 전용 API를 전제한 코드 경로가 있다면 우리 하드웨어에서만 깨진다.

## 성능 주장 경계 (선언하고 시작한다)

WG 문서의 자체 실측이다.

| 워크로드 | CPU | GPU | 배수 |
|---|---:|---:|---:|
| PUSCH 100 MHz 273 PRB 4-layer 8 RX | 13122.3 µs | 618.9 µs | **21.20×** |
| PDSCH 100 MHz 273 PRB 4-layer 4-port | 625.0 µs | 186.0 µs | 3.36× |
| OFH BFP RX 100 MHz 4-port 9-bit | 1335.5 µs | 18.4 µs | 72.58× |
| **PUSCH 20 MHz 1-layer** | **225.8 µs** | **262.3 µs** | **0.86×** |
| **PDSCH 20 MHz 1-layer** | **29.4 µs** | **79.9 µs** | **0.37×** |

마지막 두 행이 이 프로젝트의 게이트 구성이다. **20 MHz 1-layer에서 GPU가 느린 것은 예상된 결과이며 실패로 치지 않는다.** 실패는 (a) attach/PDU/ping 게이트 파손, (b) BLER/SINR 정합 이탈, (c) late/dropped slot 발생, (d) 기존 게이트 회귀뿐이다. **속도 향상 주장은 C7(100 MHz·다중 layer) 이전에는 하지 않는다.**

## 단계

| 단계 | 내용 | Exit 게이트 | 상태 |
|---|---|---|---|
| **C0** | **하드웨어 격차 확정 (패치 없이)** — WG 소스를 그대로 빌드하고 **WG가 문서에 적어둔 자체 검증**을 우리 하드웨어에서 돌린다. 통합 작업은 하지 않는다 | 빌드 성공 + 아키 120 확인. WG 문서의 12개 CUDA PHY 테스트와 8개 OFH 압축 테스트를 문서의 `ctest -R` 정규식 그대로 실행하고 **테스트별 판정 기록**. 실패는 각각 **소스 수준 메커니즘까지** 규명(단순 "실패" 기록은 불가) | **완료 2026-09-10** |
| **C1** | **벤더 패치 + 업스트림 제보** — C0가 찾은 결함만 고친다. 단일 패치 파일, 해시락 | WG 12+8 테스트 전부 통과. 수정마다 음성 대조군이 **exit≠0**. 패치가 SPDX 헤더를 훼손하지 않음. **WG1에 재현 절차 포함 이슈 제출 완료** | **수정·검증 완료 2026-09-10 / 제보 제출 대기** |
| **C1-D10** | **D10 역이식 (RTX 5090)** — GB10(S13)에서 찾은 GPU TB 인코더 결함을 C1 계열에 적용 | 결함 재현(수정 전 강제 TBS 9,474 B 실패) → 수정 → WG 14+OFH 16 통과, 음성 대조군, 라이브에서 CPU gNB와 NACK·처리량 동일 | **완료 2026-09-28** — 아래 D10 절 |
| **C2** | **부가 프로비저닝** — CUDA 전용 lock, 빌드 스크립트, 프로파일 해석기. 전부 신규 파일 | `git diff f2e02f9` 가 기존 파일에 대해 **빈 출력**. 기존 공유 검사기가 무수정으로 통과하고 `OCUDU_NATIVE_GNB_PROFILE` 에 반응하지 않음. 기존 1×1 CPU 게이트 라이브 무회귀 | **완료 2026-09-10** |
| **C3** | **부가 CUDA 게이트** — 신규 러너. 기존 게이트 무수정 | 바이트 동일 증명. CUDA 게이트가 **전 모드 `disabled`** 로 attach+PDU+ping 3/3 (parity 대조군). 기존 1×1 게이트 재실행 green | **완료 2026-09-11** |
| **C4** | **단계별 가속 활성화** — `low_phy_rx` → `low_phy_tx` → `pusch` → `pdsch(enabled)` → `prach` → `srs`. 각 단계 별 run | 단계마다 C3 게이트 재통과. CPU 대비 BLER 차 ≤1%p, SINR 차 ≤0.5 dB. **런타임 로그로 백엔드가 실제 선택됐음을 확인**(조용한 CPU 폴백 배제). late/dropped slot 0 | **완료 2026-09-11 / lower-PHY TX는 하드웨어 제약으로 degraded, 종결** |
| **C5** | **측정** — 브로커 p99, gNB KPI, GPU 점유 | CPU/CUDA 교차 실행 쌍. MPS + P코어 고정을 **기준·가속·공유서비스에 동일 적용**하고 실제 스레드 affinity 기록. 실패 측정 보존, 허용 기준 불변. **속도 향상 주장 없음** — 회귀 없음과 측정 봉투 기록만 | 미착수 |
| **C6** | **Sionna 동시 실행** — 브로커 + Sionna RT + CUDA gNB 3 프로세스 | 신규 CUDA Sionna 러너(기존 `run-ocudu-sionna-rank1.sh` 무수정). live_ready, y=Hx UL 4행 ≤1e-4, 3프로세스 커널 동시성을 nsys로 확인. 브로커 p99가 1 ms 슬롯 내이거나 초과분과 원인 기록 | 미착수 |
| **C7** | **관측점 노출 (이 통합의 실제 목적)** — 상주 PUSCH의 등화 후 심볼/LLR을 device에서 읽는 훅, `OCUDU_PUSCH_ACCELERATION_TIMING` 단계별 타이밍을 Web UI에 노출 | device LLR 덤프와 CPU 디코더 재현 일치. Web UI에 GPU 단계 타이밍 패널. **선택적 확장**: 대역폭/레이어를 올린 구성에서 속도 향상 측정 — 여기서부터만 성능 주장 가능 | 미착수 |

C3까지가 "붙여도 아무것도 안 깨진다"의 증명이고, C4부터가 실제 가속이며, C7이 목적이다.

## 단계별 상세

### C0 — 하드웨어 격차 확정

**왜 이게 1번인가.** 이전 시도는 빌드 → 통합 → 라이브 순서로 갔고, 가장 심각한 결함을 **라이브 attach 실패 시점에** 발견했다. WG는 문서에 자기네 검증 절차(12개 테스트 + 결과 `100% passed ... 252.53 sec`)를 적어두었다. 그것을 **통합 이전에** 우리 하드웨어에서 돌리면 벤더 격차가 먼저 드러난다.

빌드:
```bash
cmake -S <wg-src> -B <build> -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON \
  -DENABLE_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=120 -DENABLE_ZEROMQ=ON
```
`CMAKE_CUDA_ARCHITECTURES=120`은 WG 문서의 "RTX 50xx / Blackwell client GPUs: 120" 표에 근거한다. 아키를 틀리면 런타임에 `no kernel image is available for execution on the device`로 죽는다.

WG 문서가 지정한 검증(정규식을 문서에서 그대로 옮길 것):
```bash
ctest --test-dir <build> --output-on-failure -j1 -R '^(ofdm_demodulator_cuda_test|...|srs_estimator_gpu_sensitivity_baseline_4x4_n4)$'   # 12개
ctest --test-dir <build> --output-on-failure -j1 -R '^ofh_iq_compression_cuda/'                                                          # 8개
```

**기록해야 할 것:** 테스트별 pass/fail, 실패의 소스 수준 메커니즘, 그리고 WG 환경(252.53초/12개)과의 대조. 이 단계에서는 **아무것도 고치지 않는다.**

주의: OFH 8개는 우리 빌드 구성에서 등록되지 않을 수 있다(`ofh_iq_compression_cuda_test_NOT_BUILT`). 그 경우 왜 빌드되지 않는지(어느 CMake 옵션에 걸려 있는지)를 규명하는 것이 C0의 산출물이다.

**보조 판정기 필요.** WG의 PUSCH 비교·pipeline 테스트는 **실패 문구를 출력하면서 exit 0을 반환할 수 있다.** ctest 결과만 믿지 말고 로그 문자열을 채점하는 별도 판정기를 C0에서 함께 만든다.

#### C0 결과 — 2026-09-10

**벤더 브랜치는 우리 하드웨어에서 자기 문서의 검증을 통과하지 못한다. 9/12.**

환경: RTX 5090 (compute_cap 12.0, **디스크리트**), 드라이버 595.71.05, CUDA 12.8.93, `sm_120`. WG 기준 환경은 GB10(**통합**), aarch64, 드라이버 580.95.05, CUDA 13.0.88, `sm_121`.

빌드는 **패치 없이 성공**한다 — gNB `42c0c9a5…`, 버전 `26.04.0 (5830c9c)`, `--help`에 `GPU acceleration:` 존재. 따라서 **패치는 빌드 요건이 아니라 런타임 정확성 요건이다.**

| # | 테스트 | 결과 | 시간 |
|---|---|---|---:|
| 1–6 | `ofdm_demodulator` / `ofdm_prach_demodulator` / `pdxch_baseband_modulator` / `ldpc_encoder` / `ldpc_decoder` / `prach_detector` | Passed | ≤0.9 s |
| 7 | `pusch_gpu_cpu_comparison_test` | Passed | **875.53 s** |
| 8 | `pdsch_gpu_e2e_test` | **Failed** (57 중 1) | 0.53 s |
| 9 | `pusch_e2e_pipeline_test` | Passed | 18.26 s |
| 10 | `pusch_resident_dematch_scramble_test` | Passed | 0.17 s |
| 11 | `srs_estimator_gpu_latency_baseline_4x4_n4` | **Aborted** | 0.26 s |
| 12 | `srs_estimator_gpu_sensitivity_baseline_4x4_n4` | **Aborted** | 0.26 s |

참고: WG는 GB10에서 **12개 전부를 252.53초**에 통과했다고 문서에 적었다. 우리는 7번 하나가 875.53초로 그 전체 스위트의 3.5배다. OFH는 exit 0이며 16개가 등록됐다(WG 문서는 8개라고 적었다 — 개수 불일치이며 실패는 아니다).

**세 실패의 근본 원인은 하나다: CUDA-visible managed grid 전용 API를 강제 요구하는 경로가, 디스크리트 GPU의 pinned/host grid에서 그 API를 못 얻는다.**

- **SRS 2건.** `srs_estimator_cuda_impl.cpp:362`가 `get_owned_device_snapshot_cbf16()`와 `..._ready_event()`를 필수로 요구한다. 둘 다 `resource_grid_reader.h:151,170`에서 기본값이 `nullptr`인 virtual이고, **managed grid(`resource_grid_cuda_visible_impl.h:336-338`)만 override한다.** `resource_grid_pinned_impl.h`는 **하나도 override하지 않는다.** 벤치마크는 이 GPU에서 `grid=host`로 돌므로 둘 다 nullptr → `cudaGetLastError()`(375행)가 **CUDA 에러를 버리고** → `return {}` → 빈 채널 행렬(RX 포트 0개) → 두 계층 위 `srs_estimator_gpu_benchmark_helpers.h:510`에서 기준값 4포트와 불일치 assertion. 실패 메시지가 진짜 원인을 전혀 시사하지 않는다.
  호스트 grid 경로가 **원래 존재했다는 유물 증거**: `srs_estimator_cuda_impl.h:52-53`에 `staged_grid_source`/`staged_grid_reader` 멤버가 여전히 선언돼 있고 `.cpp`에서 사용 횟수 **0**이다.
- **PDSCH 1건.** `pdsch_gpu_e2e_test.cpp:746`에서 `compress_device_symbol()`이 `transmission=0 port=0 symbol=0`에 false를 반환한다. `iq_compression_cuda.cpp:478` → `resource_grid_cuda_visible_impl.h:318` → `prepare_device_access()`(703행)로 이어지고, 이 함수는 `host_reads_device_memory_directly || pages_device_resident`가 아니면 false다. 테스트가 CPU 비교를 먼저 하면서 managed grid를 host로 마이그레이션시키므로 게이트가 **정당하게** 거부한다.
  **게이트는 결함이 아니다.** 벤더 주석(713–718행)이 `"Host copy is the fallback for that slot only"`라고 명시하는데, **OFH 압축 호출자가 그 폴백을 구현하지 않고 그냥 false를 반환한다.**

부수 산출물: 출력 문구 판정기 `scripts/cuda/verify-phy-log.py`. 알려진 합격 로그와 알려진 불합격 로그 양쪽으로 **판정기 자체를 먼저 검증**했다. 이번 실행에서는 스크립트 순서 결함으로 OFH 실행이 `LastTest.log`를 덮어써 PHY 통과분이 채점되지 않았다(실패분은 채점돼 SRS abort 2건을 검출). 스크립트는 수정했다.

증거: native `results/cuda-rebuild/c0-20260910T113322Z/` (`c0-result.json`, `phy-validation.log`, `ofh-validation.log`, 빌드 로그, 해시).

### C1 — 벤더 패치

수정마다 다음 셋을 갖춘다: ① 소스 수준 근본 원인, ② 결함을 복원하면 **exit≠0**로 실패하는 회귀 테스트, ③ 업스트림 제보.

**WG 테스트가 못 잡는 결함이 있다는 것을 전제로 한다.** WG의 `pusch_gpu_cpu_comparison_test`는 CPU·GPU 양쪽 보간 전략을 `average`로 고정하고 CFO를 `0.0F`로 두며 deferred 경로만 탄다. 따라서 (a) 설정이 GPU로 전달되지 않는 결함, (b) CFO 의존 결함, (c) 동기 경로 결함은 **구조적으로 관측 불가능**하다. C1의 회귀 테스트는 이 세 구멍을 메우도록 설계한다 — 즉 CPU와 **다른** 설정, **0이 아닌** CFO, **동기** 경로를 포함해야 한다.

#### C1 결과 — 2026-09-10

**PHY 14/14, OFH 16/16 통과. 음성 대조군 5/5 전부 exit≠0. 업스트림 제보서 작성 완료, 제출은 미완.**

D5 편입 후(`d2579af2`, gNB `063e6cb9`) **재검증 완료 2026-09-11**: `phy_ctest_exit=0` `100% tests passed, 0 tests failed out of 14`, `ofh_ctest_exit=0` `... out of 16`, 로그 판정기 `phy_log_verdict=pass`, 추가 회귀 2건 모두 `PASS`. D5가 `upper_phy_factories.cpp`를 건드리므로 회귀 없음을 재확인한 것이며, 최초 C1(`78cd5725`) 기준의 통과 수치와 동일하다. 증거 `results/cuda-rebuild/c1-20260911T040019Z/`.

패치: `scripts/cuda/patches/c1-ocudu-cuda-discrete-and-config.patch`, sha256 `d2579af2…`, base `5830c9cb`.
프로덕션 7파일 **+88/−35**, 테스트 4파일 +64/−13, **SPDX 헤더 훼손 0줄**. gNB `063e6cb9…`.

> **2026-09-11 개정.** 최초 C1(sha256 `78cd5725…`, gNB `535d12ad…`)은 D5를 의도적으로 제외했다. C4에서 D5가 도달 가능해졌고 실제로 필요했으므로 패치에 편입했다 — 아래 D5 행과 NC-D5가 그것이다.

**결함 5건.** C0가 벤더 테스트로 찾은 2건, C1이 사각지대 테스트를 추가해 드러낸 2건, C4의 라이브 게이트가 드러낸 1건이다.

| ID | 발견 경로 | 내용 | 수정 방향 |
|---|---|---|---|
| D1 | C0 (벤더 테스트) | SRS가 managed 전용 snapshot API를 강제 요구 | `supports_device_grid_reading()`으로 분기 — **managed는 벤더의 GB10 수정을 그대로 보존**, host/pinned만 기존 `pusch_device_grid_reader_cuda`로 스테이징 |
| D2 | C0 (벤더 테스트) | host consumer가 managed grid를 마이그레이션한 뒤 OFH 압축에 폴백이 없음 | live read 거부 시 owned snapshot으로 폴백 — **벤더 주석(713–718행)이 이미 명시한 동작**. 어느 hold를 잡았든 해제 |
| D3 | C1 사각지대 테스트 | 설정된 PUSCH 시간보간이 상주 GPU 경로에 전달되지 않음 | 가속 config·생성자로 전략 전달, 적용된 모드를 로그 |
| D4 | C1 사각지대 테스트 | 동기 경로가 post-eq SINR 자리에 EVM 환산값 대입 | 같은 파일의 나머지 3개 방출 지점과 일치시킴 |
| D5 | C4 라이브 게이트 | ZMQ 순차 PHY는 codeblock executor를 주지 않는데 `pdsch_acceleration_mode: enabled`가 그것을 필수로 요구 → 상위 PHY 구성 중 `report_fatal_error`로 gNB가 아예 안 뜸 | 가속 요청 + flexible 구성 + executor 부재일 때만 `inline_task_executor`를 대입. 강제 CUDA PDSCH는 이미 코드블록을 동기 배치로 처리하므로 태스크 큐를 새로 만들지 않고 순차 계약을 지킨다. 대입 사실을 로그로 남긴다 |

**D3는 고치기 전에는 테스트가 불가능하다.** CPU와 다른 전략을 요구하려면 D3가 결여시킨 config 필드가 필요하기 때문이다. 이 순환이 이 결함이 살아남은 주된 이유로 보인다.

**벤더 테스트가 D3/D4를 못 보는 이유 3가지** (`pusch_gpu_cpu_comparison_test.cpp`): ① 246·257행이 CPU·GPU 양쪽을 `average`로 고정 → 설정 유실이 구조적으로 관측 불가, ② 363행 `CFO=0.0F` → 보간 차이가 결과를 안 바꿈(`OCUDU_TIME_INTERP=linear`를 강제해도 통과하는 것을 실측 확인), ③ 256행이 deferred 경로(`"gpu"`)만 탐 → 동기 경로 미도달.

**음성 대조군 — 결함 복원 시 반드시 실패해야 한다.**

| 대조군 | 결함 복원 대상 | 테스트 | exit |
|---|---|---|---:|
| NC-D1 | SRS host/pinned 스테이징 제거 | `srs_estimator_gpu_latency_baseline_4x4_n4` | **8** |
| NC-D2 | OFH host-copy 폴백 제거 | `pdsch_gpu_e2e_test` | **8** |
| NC-D3 | 보간 전달만 제거 | `pusch_gpu_cpu_cfo_interpolation_test` | **8** |
| NC-D4 | EVM 대입 복원 | `pusch_gpu_cpu_sync_sinr_test` | **8** |
| NC-D5 | inline executor 대입 제거 | 라이브 게이트 `acceleration=all` (ctest로는 관측 불가) | **2** |

대조군 종료 후 패치가 해시로 정확히 복원됐음을 확인했다(D1–D4는 `78cd5725…`, D5 편입 후는 `d2579af2…`).

**NC-D5는 한 번 무효였다가 다시 만들었다.** 첫 판은 `run-ocudu-cuda-1x1.sh`를 통해 돌렸는데, 그 런처의 첫 동작이 소스를 lock된 패치와 대조하는 감사다. 대조군은 소스를 의도적으로 lock과 다르게 만들므로 감사가 게이트 실행 **전에** 막았고, exit≠0은 나왔지만 그것은 감사가 동작한다는 증거이지 D5가 필요하다는 증거가 아니었다. 다시 만든 대조군은 감사를 우회해 기존 게이트를 직접 부르고, **exit≠0만으로는 합격시키지 않는다** — gNB 콘솔에 해당 fatal error 문자열이 실제로 찍혔는지까지 확인해야 `CONTROL OK`가 된다. (규칙 3의 "출력 문구만 보지 않는다"가 대조군 자신에게도 적용된다.)

**D5는 최초 C1에서 제외했다가 C4에서 편입했다.** 제외 판단 자체는 옳았다 — PHY 테스트로는 관측되지 않고 라이브 gNB 구성에서만 발현하므로, C1 시점에는 검증할 수단이 없었다. C4의 라이브 게이트가 그 수단이 되자마자 필요성이 실측으로 확인됐다.

**직결 런 — 2026-09-13.** 제보서의 attach 주장은 그때까지 브로커를 사이에 두고 잰 것이었다. `scripts/cuda/run-ocudu-cuda-direct-zmq.sh`(신규, 기존 게이트·렌더러 무수정. CUDA 렌더 결과에서 srsUE의 `device_args`만 브로커 포트 2101/2100 → gNB 포트 2001/2000으로 바꾼다)로 gNB↔srsUE를 ZMQ로 직결해 7회 실행, **전부 attach/PDU/ping 통과**, 네임스페이스 안 프로세스는 gNB·srsue·mongod·Open5GS뿐. `all` 기동 20–24초, `disabled` 1–2초. 증거 `results/cuda-rebuild/direct-zmq-20260913T{115445,115719,115924,120101,120255,120409,120603}Z/` (각각 `summary.json`, `backends.json`, `kpis.json`, `logs/processes.txt`).

**직결에서 드러난 것 — `pusch_acceleration_mode`가 켜지면 16QAM/TBS 528 할당의 CRC 실패율이 오른다.** 60초 트래픽(약 1,240 PUSCH/런) 기준: host 0.24/0.49%, `low-phy-rx` 0.49%, `low-phy-tx` 0.65%, **`pusch` 1.51%, `all` 1.44/1.37%**. 실패는 전 arm에서 16QAM/TBS 528·SINR 4.4–5.4 dB·`iter=6.0`에만 있고, 그 할당의 실패율이 host 0.6–1.6% → 가속 6–10%다. 링크 적응이 QPSK로 물러서므로(16QAM 비중 60–79% → 16–26%) 전체 BLER 차는 약 1%p에 그친다. 보고 SINR p50도 4.8 vs 5.2 dB. **C4의 "BLER 차 ≤1%p"는 브로커 fixture(SINR ~1.4 dB, 전부 QPSK, 0% vs 0%)에서 충족한 것이고, 16QAM에 도달하는 직결 fixture에서는 전체 차가 0.9–1.3%p로 경계에 걸리며 rung별 차는 크다.** 제보서에 결함이 아닌 관찰로 추가했고, PUSCH 경로 내부(채널추정 보간·LLR·fp16 상주 디코드) 어디인지는 추적하지 않았다 — C7 이전에 볼 항목.

**업스트림 제보: 제출 완료 2026-09-14.** 이슈 #2 — https://gitlab.com/ocudu/work_groups/wg1_hw_accel/cuda_accelerated_ocudu/-/work_items/2 (계정 `MinwooEun`, 본문 = 제보서 전문, 첨부 = 패치 + repro 설정 2개). 제출본은 `docs/upstream/submission/`(제보서 사본, 패치 `nvcuda_accel_02-discrete-gpu-and-config.patch`, `repro/` 설정 2개). 제보서는 `docs/upstream/wg1-discrete-gpu-report.md`에 작성했다 — 결함 5건의 메커니즘·재현·영향, 벤더 테스트 사각지대 3가지, 부수 관찰 2건(문서는 OFH 8개라 하나 16개 등록됨; `gtest_discover_tests`가 POST_BUILD라 타겟 미빌드 시 플레이스홀더 하나만 매칭돼 **아무것도 안 돌리고 green이 됨**). 제출은 minwoo의 명시 지시로 수행했다.

증거: native `results/cuda-rebuild/c1-20260910T122532Z/c1-result.json`, 대조군 `c1-nc-20260910T124543Z/`.

### C2 — 부가 프로비저닝

CUDA lock은 **별도 파일**(`native-workspace-cuda.lock.json` 등)로 만들고 공유 lock을 오버레이한다. 공유 lock에 항목을 추가하면 기존 게이트 4개가 부르는 공유 검사기의 동작이 바뀐다.

프로파일 해석기는 신규 파일이며, 소스 커밋·패치 SHA256·CMakeCache(`ENABLE_CUDA=ON`, `CMAKE_CUDA_ARCHITECTURES=120`)·`gnb --version` 커밋·`gnb --help`의 `GPU acceleration:` 항목을 검증한다.

### C3 — 부가 CUDA 게이트

기존 이너는 415행에서 gNB 경로를 하드코딩하고, 32개 `--` 인자 중 `--gnb-binary`만 없다. 기존 파일을 고칠 수 없으므로 **신규 러너**로 간다. 착수 전에 기존 이너 636줄 중 CUDA 1×1 시나리오가 실제로 쓰는 범위를 측정해 중복을 최소화한다.

설정 주입은 기존 훅 `OCUDU_NATIVE_CONFIG_RENDERER`가 이미 있으므로 렌더러 수정이 필요 없다.

**첫 라이브 실행은 반드시 전 모드 `disabled`다.** CUDA 바이너리의 host PHY로 CPU 빌드와 동일 결과(attach/PDU/ping)를 먼저 확인해야, 이후 실패가 "CUDA 때문"인지 "빌드 교체 때문"인지 분리된다.

### C4 — 단계별 활성화

WG 문서 기준 주의사항:
- 모든 선택자의 기본값은 `auto`이고, **auto가 곧 가속 켜짐은 아니다** — 디스크리트에서 `pdsch_acceleration_mode: auto`는 host를 선호한다. PDSCH를 실제로 켜려면 `enabled`가 필요하다.
- `ul_cuda_visible_grid_mode: auto`는 디스크리트에서 pinned를 고른다. lower-PHY 가속을 강제하면 managed가 필요할 수 있다.
- `ldpc_decoder_algorithm: auto`는 코드블록 배치에 따라 알고리즘이 바뀐다. 재현성이 필요하면 `boxplus` 고정.
- 환경변수 `OCUDU_*`는 프로파일링 훅이다. 게이트는 YAML 키만 쓰고 env 훅은 러너에서 `unset` 한다.

**지원 경계(WG 문서 명시)와 우리 fixture 대조:** 상주 PUSCH는 Type-1 DM-RS normal CP, 1–4 layer, 1/2/4/8 RX 포트, 기본 UCI 없음. transform-precoded는 1 layer. PDSCH direct device-grid는 1 layer 또는 layer=port. **HARQ 재전송은 항상 host LDPC**로 디코딩한다 — 따라서 "GPU에서 디코딩한 BLER"은 첫 전송에만 해당한다. 현 fixture(srsUE Type-1, 1 layer, 최대 4 RX)는 전부 범위 안이다.

#### C4 결과 — 2026-09-11

**전 모드 가속(`acceleration=all`)으로 attach/PDU/ping 3/3 통과.** `result=pass`, `tx_pulls=38304 rx_requests=38545 rx_starvations=9 tx_queue_overflows=0 tx_sequence_gaps=0 zmq_errors=0`. 브로커 `n=19354` 슬롯, `p50=60 µs p95=120 µs p99=135 µs p999=180 µs max=5005 µs`. late/dropped slot 0. gNB `063e6cb9…`, 증거 `results/{logs,reports}/ocudu-interop/20260911T035509Z/`.

**조용한 CPU 폴백이 아님을 런타임 로그로 확인:**

```
PDSCH acceleration manifest: requested=enabled backend=CUDA lanes=1 pool_size=4 device_grid_writer=true
PUSCH acceleration manifest: requested=enabled backend=CUDA resident_decode=true decoder_pool=5 device_uci=false
PRACH acceleration manifest: requested=enabled backend=CUDA
SRS   acceleration manifest: requested=enabled resolved=enabled backend=CUDA
PDSCH CUDA synchronous executor selected for sequential PHY.
Uplink/Downlink CUDA-visible resource-grid manifest: requested=managed
```

**막고 있던 것은 결함 2개였고, 그중 하나는 벤더 코드가 아니라 우리 게이트에 있었다.**

**(가) 게이트 — 브로커의 `--duration` 시계가 gNB 기동을 기다리지 않는다.** 브로커는 `--duration 15s`로 실행되고 그 시계는 **브로커 프로세스 시작 시점부터** 돈다. CPU gNB는 기동 후 **1.1초**에 ZMQ를 바인딩하지만, 전 모드 가속 CUDA gNB는 디바이스 초기화 때문에 **19.7초**가 걸린다. 그래서 브로커는 gNB의 라디오가 올라오기 **전에** 15초를 다 쓰고 스스로 종료했고, 그 결과가 `tx_pulls=0 rx_requests=0`, `process_latency_summary n=0`, `status=ue_stack_blocker_no_attach`였다.

이 결함은 **증상이 CUDA 결함처럼 보이도록 위장한다** — gNB는 정상 기동하고 CUDA 백엔드도 실제로 선택되는데 IQ만 한 바이트도 안 흐르기 때문이다. 2026-09-10에 도입한 `OCUDU_NATIVE_BROKER_DEADLINE_AFTER_GNB`는 이것을 고치지 못했다. 그것이 늦춘 것은 **쉘 측 대기 시한**(브로커가 멈췄는지 감시하는 상한)이고, 실제로 브로커를 종료시킨 것은 **브로커 자신의 `--duration`** 이었다. 두 설정값 어느 쪽에서도 결과가 같았으므로 그 노브는 **실질적으로 무효(inert)** 였다. 지금은 제거하고 `OCUDU_NATIVE_BROKER_STARTUP_ALLOWANCE_SECONDS`로 대체했다 — 브로커 `--duration` 자체를 늘리고, 쉘 시한도 같은 양만큼 늘린다. 미설정(0)이면 두 표현식 모두 변경 전과 문자 그대로 동일하다.

**(나) 벤더 — D5.** 위 C1 표 참조. 이것이 없으면 `pdsch`·`all` 단계에서 gNB가 상위 PHY 구성 중 죽는다.

#### 단계별 사다리 — 2026-09-11

`scripts/cuda/c4-staged-runs.sh`로 기준선 포함 **8회 연속 실행, 전부 `verdict=passed`**. 증거 `results/cuda-rebuild/c4-staged-20260911T042423Z/`.

```
stage          backend   tx   bler%  d_bler_pp  sinr_mean  d_sinr_db
cpu-baseline  (기준선)   27     0.0          —      0.789          —
disabled            OK   31     0.0        0.0      0.923      0.134
low-phy-rx          OK   27     0.0        0.0      0.822      0.033
low-phy-tx    DEGRADED   27     0.0        0.0      0.785     -0.004
pusch         DEGRADED   27     0.0        0.0      0.959      0.170
pdsch         DEGRADED   27     0.0        0.0      0.989      0.200
prach         DEGRADED   27     0.0        0.0      0.963      0.174
all           DEGRADED   30     0.0        0.0      0.980      0.191
```

**판정을 "게이트를 통과했는가"가 아니라 "무엇이 실제로 돌았는가"로 매긴다.** 요청한 경로를 조용히 host에서 돌리는 gNB도 attach하고 ping하고 카운터도 전부 깨끗하다 — 생존은 증거가 아니다. `scripts/cuda/verify-stage-backends.py`가 런타임 마커를 누적 대조해 세 결과를 구분한다: **selected**(요청한 경로가 돌았다) / **degraded**(GPU 경로이긴 한데 요청한 그것이 아니다) / **fallback**(host에서 돌았다 — 이건 실패).

- ✅ **silent host fallback은 어느 단계에도 없다.** 상위 PHY 매니페스트 4종(PUSCH·PDSCH·PRACH·SRS)이 해당 단계에서 전부 `backend=CUDA`, `GPU path unavailable` 경고 0건.
- ✅ **SINR은 전 단계 통과.** CPU 대비 최대 **+0.200 dB**(허용 0.5 dB).
- ⚠️ **lower-PHY TX가 `low-phy-tx` 이상 전 단계에서 DEGRADED.** `Lower-PHY TX GPU path selected: host resource-grid staging fallback.` — 직접 CUDA-visible 하향 grid 리더가 아니라 **host에서 staging**한다. **CPU 폴백은 아니다**(`ocudu_lowphy_tx_process_host_grid`, 여전히 CUDA). 그러나 데이터를 device에 유지하는 것이 이 통합의 목적이므로 통과로 치지 않는다.
  - 메커니즘: `pdxch_baseband_modulator_cuda.cpp:144`가 `supports_device_grid_reading() && prepare_device_grid_reading(stream)`을 요구한다. 전자는 managed grid에서 참이고 우리는 managed를 요청했으므로, 거부는 후자의 **residency 게이트 — D2와 같은 디스크리트 GPU 메커니즘**에서 온다.
  - `all`에서 PDSCH가 `device_grid_writer=true`인데도 direct 경로 로그가 **한 번도** 안 찍혔다(두 메시지가 서로 독립된 one-shot 래치이므로, 한 번이라도 탔다면 찍혔어야 한다).
  - **추적 완료 2026-09-11 — 결함이 아니라 하드웨어 제약이며, 로컬 패치로 해결 불가. 추적 종료.**
    - 조건은 `fully_coherent = concurrent_managed_access && host_uses_host_page_tables && direct_managed_access_from_host` 셋 **전부**다. RTX 5090 실측 `1 / 0 / 0` → 불만족. 벤더 자신의 속성 표에 따르면 **GB10도 `1 / 1 / 0`으로 불만족**이고, 만족하는 것은 GH200급(`1/1/1`)뿐이다. (GB10은 벤더 표와 동일 코드 경로에서의 **추론**이며 GB10 실측이 아니다.)
    - 불만족이면 `allow_concurrent_downlink_writes=false`가 되고, `prepare_host_write()`가 `pull_pages_home()`을 타서 **CPU가 DL grid에 쓸 때마다 페이지를 host로 끌어오고 device residency를 해제**한다. PDCCH·SSB는 CUDA 가속 대상이 아니라 CPU가 같은 grid에 쓰므로, 매 슬롯 ①GPU PDSCH 쓰기 → ②CPU 제어신호 쓰기(residency 해제) → ③TX 읽기 거부 순서가 된다.
    - **벤더는 이 경로가 켜지길 의도했다** — 팩토리가 하향에 `enable_concurrent_downlink_writes=true`를 실제로 넘긴다(`resource_grid_cuda_visible_impl.h:1596`). `fully_coherent`에서만 살아남게 만든 것이다.
    - **패치 불가:** `direct_managed_access_from_host=0`은 "CPU가 GPU-resident managed 페이지를 마이그레이션 없이 못 건드린다"는 하드웨어 사실이고, PDCCH·SSB는 CPU가 써야 한다. 양립 불가다. 유일한 올바른 해법은 CPU 생성 채널을 별도 버퍼에 쓰고 device에서 병합하는 것인데, `resource_grid` writer 인터페이스를 쓰는 모든 DL 채널 처리기를 건드리는 아키텍처 변경이지 호환 패치가 아니다. `OCUDU_CUDA_VISIBLE_GRID_FORCE_DEVICE_READ=1` 강제는 벤더 주석이 `Xid 31`의 원인으로 지목한 바로 그 거짓말이다.
    - **이 프로젝트 목적에는 영향이 작다.** 차원이 폭발하는 쪽은 상향(사용자 수 × RX 안테나 × LLR)이고 그 경로는 전부 device 상주다. 하향 격자는 사용자 수로 커지지 않고 대역폭 × 포트로만 커진다(20 MHz 1포트 ≈ 70 KB/슬롯, 100 MHz 4포트 ≈ 730 KB/슬롯). C7의 목표 경로는 상향이다.
    - **상향이 되는 이유는 보호 플래그가 아니다.** 팩토리는 `uplink_no_host_migrate=false`를 넘긴다(주석: PUCCH가 live grid를 host-read해야 하므로 설정하면 CSI가 0이 되고 UE가 RLF). RX 직접 경로가 되는 것은 그것이 **읽기가 아니라 device 쓰기**이기 때문이다 — 쓰기에는 residency 전제조건이 없다.
    - **제보 가치는 문서 쪽에 있다.** WG 문서는 discrete 지원을 명시하고(17행) `pdsch_acceleration_mode`의 discrete 동작까지 서술하는데(235행), `low_phy_tx_acceleration_mode`(244행)에는 이 구분이 없다. 사용자는 켠 줄 알지만 staging 경로가 돈다. GH200급을 요구하는 서술은 문서 어디에도 없다 — GH200/H100/H200은 `CMAKE_CUDA_ARCHITECTURES` 번호 목록(130–131행)으로만 등장한다.

**아직 C4가 아닌 것.**

1. ✅ 단계별 run 8/8 통과 + 백엔드 실제 선택 확인 + late/dropped 0
2. ✅ **SINR 차 ≤0.5 dB** — 전 단계 최대 0.200 dB
3. ✅ **BLER 차 ≤1%p — 충족 2026-09-11** (아래)
4. ⚠️ lower-PHY TX DEGRADED — 원인 규명 완료, 하드웨어 제약, 패치 불가로 종결(위)

#### BLER — 트래픽을 실은 페어 런, 2026-09-11

attach 게이트만으로는 PUSCH가 27–31건이라 CRC 1건이 BLER을 **3.7%p** 움직였다. 즉 그 측정은 1%p를 *표현할 수 없었다*. `scripts/cuda/c4-bler-run.sh`가 bounded 런에 keepalive ping(0.2초, ping의 비특권 하한)을 넣고 브로커 창을 늘려 표본을 확보한다.

| arm | PUSCH tx | BLER | 분해능 | SINR 평균 | CPU 대비 |
|---|---:|---:|---:|---:|---:|
| cpu-baseline | **1372** | 0.0% | ±0.073%p | 1.386 dB | — |
| all | **998** | 0.0% | ±0.100%p | 1.559 dB | +0.173 dB |

분해능 3.7%p → **0.073%p**. `C4 gate: PASS on every rung`. 증거 `results/cuda-rebuild/c4-bler-20260911T053952Z/`.

**이 결과가 보여주는 것과 보여주지 않는 것.** 보여주는 것은 **가속 경로가 오류를 새로 만들지 않는다**(약 1000건 기준)이고, 이것이 C4가 요구하는 바다. 보여주지 않는 것은 스트레스 하에서 두 경로가 같게 동작하는지다 — `legacy` 채널 모드는 IQ를 거의 깨끗하게 중계하므로 양쪽 0%가 정상이고, 0% 대 0%는 변별력이 없다. 오류를 실제로 만드는 비교(AWGN SNR 설정)는 별도 fixture이며 C5/C6 범위다.

**게이트 오버라이드 1건 추가.** keepalive는 원래 무한 런에서만 동작했다(주석: "bounded gate keeps the exact traffic profile it was proven with" — 의도된 설계 결정). bounded 런도 **옵트인**할 수 있게 풀었다. 기본값이 `0`이라 미설정 시 트래픽 프로파일은 이전과 동일하고, 합격 판정이 읽는 `ue-ping.log`는 수락 ping만 담은 채로 유지된다(keepalive는 자기 로그에 쓴다).

**과정에서 드러난 배선 결함.** 첫 시도는 `keepalive_replies=0`으로 **조용히 아무것도 하지 않았다** — `--ue-keepalive-seconds`가 Sionna 분기에만 전달되고 legacy 분기가 넘기는 `common_inner_args`에는 없었다. 스크립트가 ping 응답 수를 세어 보고하지 않았다면 27건짜리 결과를 1372건인 줄 알고 기록했을 것이다. 인자를 `common_inner_args`로 옮기고 Sionna 쪽 중복을 제거했다.

**부수 효과로 남은 것.** allowance를 30초로 두면 브로커가 15초가 아니라 45초를 돌므로, `disabled` 런의 측정 창이 CPU 기준선보다 길어진다(`tx_pulls` 18841 → 73242). attach 판정에는 영향이 없지만 **C5의 동일 조건 비교에는 영향이 있다** — C5는 기준선과 가속 런의 측정 창을 같게 맞추는 것부터 정해야 한다.

### C5 — 측정

측정 규율:
- 같은 fixture로 CPU·CUDA를 **교차 실행**한다(한쪽 몰아 돌리지 않는다).
- 하이브리드 CPU에서는 허용 CPU 집합을 기록하고 **실제 워커 affinity를 검증**한다. 기준·가속·공유서비스(MPS 데몬 포함)에 **동일하게** 적용한다.
- 변하지 않은 CPU 기준선이 허용 범위를 넘어 흔들리면, 가속 탓으로 돌리기 전에 배치와 환경 변동을 먼저 조사한다.
- **실패 측정을 지우지 않는다.** 허용 기준은 사후 조정하지 않는다.
- starvation·queue overflow·sequence gap·ZMQ error가 0이 아니면 strict realtime은 **실패로 처리**한다.
- 성능 수치에는 하드웨어·샘플레이트·토폴로지·모델 체인·백엔드·실행 시간 6개 라벨을 전부 붙인다.

**계측 해상도를 바꿔야 한다면 opt-in 노브로 만들고 기본값은 기존 값을 유지한다.** 브로커 히스토그램 버킷을 무조건 바꾸면 이전 측정과의 비교 가능성이 사라진다.

### C6 — Sionna 동시 실행

RTX 5090 한 장에 브로커 CUDA 커널 + Sionna RT(OptiX) + CUDA gNB 세 컨텍스트가 올라간다. 브로커의 1 ms 슬롯 예산이 이미 빠듯하다.

- 신규 CUDA Sionna 러너를 만든다(기존 `run-ocudu-sionna-rank1.sh`는 무수정).
- nsys는 **MPS 하에서 기본 하드웨어 추적이 실패**한다(`CUPTI_ERROR_HARDWARE_BUSY`). 소프트웨어 추적(`cuda-sw,nvtx`)을 명시하고, 네임스페이스를 프로파일러 주입 **전에** 준비하며, 모든 GPU 클라이언트가 준비된 뒤 수집을 시작하고, 런타임 정리까지 프로파일러 수명을 유지한다.
- 커널 시간 구간의 교집합은 **동시 실행의 증거이지 속도 향상이나 SM 점유율이 아니다.**

### C7 — 관측점 노출

이 통합의 목적이다. 상주 PUSCH는 device LLR을 fp16으로 유지하고 CRC 실패 시 export한다. 등화 후 심볼/LLR을 device에서 직접 읽을 수 있으면 신경 수신기·학습 채널추정 실험을 붙일 수 있고, Sionna RT가 이미 GPU에 있으므로 "학습된 채널 → 에뮬레이션 → 상주 수신기"가 프로세스 경계 없이 이어진다.

WG의 "AI-RAN tensors" 인터페이스가 나오면 자체 훅 대신 그것을 쓴다.

### D10 — GPU TB 인코더의 filler 0 결함, RTX 5090 역이식 (2026-09-28)

**배경:** S13(Spark)에서 CUDA gNB를 2×2 게이트에 붙이자 PDSCH 가속을 켠 순간 rank 1 NACK 45%가 났다. 원인은 벤더 `lib/phy/cuda/src/transport_block.cu`의 `tb_encoder_configure`·`tb_batch_encoder_configure`였다. CPU 세그멘터가 LDPC filler 0을 넘기면 "값 없음"으로 보고 GPU가 스스로 계산한 filler를 써서, filler가 0인 다중 코드블록 TB(예: 9,474 B = BG1 9 CB, Z=384)가 틀린 K로 부호화된다. 하드웨어와 무관한 벤더 결함이라 C1 계열(워크스테이션)에도 있어야 한다.

**재현 (수정 전):** 체크아웃 `src/ocudu-cuda-c1d10`(pin `5830c9cb` + C1 패치 `d2579af2`)에 D10의 **테스트 부분만** 넣고(`pdsch_gpu_e2e_test`에 강제 TBS 9,474 B 1포트·2포트, 10,247 B F>0 대조) 빌드했다. 결과: 9,474 B 두 케이스만 실패, 나머지 58개 통과(10,247 B 대조 포함). 5090 C1 빌드에도 결함이 있다. 이 실행이 음성 대조군을 겸한다.

**수정:** D10의 `lib/` 부분을 얹었다. 패치 `scripts/cuda/patches/c1-d10-ocudu-cuda-discrete.patch`(pin 대비 C1 + D10 전체, sha256 `1696d8a2…`), lock `scripts/cuda/cuda-workspace.c1-d10.lock.json`, 빌드 `builds/c1-d10-cuda-patched`(`c1-cuda-patched`는 C1 증거 빌드로 그대로 둔다). D8/D9는 이 계열에 넣지 않았다. `resolve-cuda-gnb.py` 감사 통과(gNB sha256 `3891425a…`).

**오프라인:** C1 검증 스크립트 그대로 WG 14개 PHY 테스트 exit 0, OFH 16/16, 로그 판정 pass, 블라인드스팟 마커 두 개 PASS(`results/cuda-rebuild/c1-d10-20260928T161542Z`).

**라이브 (OAI 2×2 게이트, unitary H, 20 MHz, 기본값, 다른 GPU 프로세스 없음):**

| gNB | rank | NACK | DL (Mb/s) | 실시간 비율 |
|---|---|---|---|---|
| CPU | 2 | 0.0 | 80.0 | 0.999 |
| CUDA C1+D10 (`all`, MPS) | 2 | 0.0 | 80.0 | 0.993 |
| CPU | 1 | 0.0 | 74.1 | 0.999 |
| CUDA C1+D10 | 1 | 0.0 | 74.1 | 1.000 |
| CUDA C1 (대조, 결함 있음) | 1 | **0.447** | 1.3 | 0.999 |

- rank 2의 80 Mb/s는 이 실행의 iperf 제시율 상한이다(셀 용량은 148 Mb/s). CPU와 CUDA가 같은 조건이다.
- `91c581c`(UE 로컬 패치 3번째, CSI RI 누산기 초기화)가 lock에 들어간 뒤 워크스테이션 `builds/oai-zmq-local`을 다시 빌드하고(`check-oai-local-patches.sh` 통과) rank 2 짝을 반복했다: CPU NACK 0.0 · 80.0 Mb/s, CUDA C1+D10 NACK 0.0 · 80.0 Mb/s, 둘 다 health=pass. 위 표는 그 커밋 전(16:36–16:40 UTC) 실행이다.
- srsUE legacy 1×1 CUDA 게이트(`run-ocudu-cuda-1x1.sh`, `all`, C1+D10 lock): **통과**, rx_starvations 18.
- **처음 실패한 것:** CUDA gNB 2×2 실행 두 번(D10, C1 대조 모두)이 `gNB did not start`로 끝났다. 2×2 게이트의 gNB 시작 대기 기본값이 20 s인데 CUDA gNB는 PHY 초기화에 그보다 오래 걸린다. `OCUDU_NATIVE_GNB_START_TIMEOUT_SECONDS=90`으로 다시 돌려 통과했다(1×1 CUDA 러너는 자체적으로 늘려 둔다).

## 이 통합이 주는 것과 주지 않는 것

**주는 것**
1. GPU 상주 end-to-end 파이프라인 — 채널·gNB PHY가 한 GPU 위에.
2. AI-RAN 진입점 — device 텐서 접근.
3. 스케일 상한 확장 — 100 MHz·다중 layer로 갈 때의 20×급 이득 경로.
4. CSI/링크어댑테이션 연구의 관측점 — 등화 후 데이터 직접 관측.

**주지 않는 것 (정직하게)**
- 현 게이트 구성(20 MHz·1 layer)에서 **지연 감소 없음**. 오히려 늘어날 가능성이 크다.
- **PUCCH 전부와 HARQ 재전송은 CPU**로 남는다.
- **에뮬레이터 자체는 변하지 않는다** — 채널 커널·y=Hx 판정·strict counter는 gNB와 무관하다.

## 참고

- WG 레포: `gitlab.com/ocudu/work_groups/wg1_hw_accel/cuda_accelerated_ocudu` (브랜치 `nvcuda_accel_02`) — OCUDU 공식 GitLab 네임스페이스의 하드웨어 가속 WG1(DeepSig 주도). **main 미병합 작업 브랜치이지 릴리스가 아니다.**
- WG 문서: `docs/phy_cuda_acceleration.md`(550줄, 빌드·YAML·벤치·검증), `lib/phy/cuda/README.md`(커널 라이브러리·런타임 노트)
- 라이선스: **BSD-3-Clause-Open-MPI** (DeepSig Inc + Software Radio Systems Limited). 본문이 "with or without modification"을 명시적으로 허용한다. 조건(저작권 고지 유지·바이너리 배포 시 문서 재현·이름 이용 보증 금지)은 **재배포 시** 발동한다.
- 이 레포: [`AGENT_GOAL.md`](../../../AGENT_GOAL.md)(미션·제약), [`RANK1_MILESTONES.md`](https://github.com/zhouyou-gu/ocudu-gpu-channel/blob/44bfdc328f2d8cebf2b2cff417d8c01b89e0900e/RANK1_MILESTONES.md)(rank-1 근거·srsUE 제약·MCS 캡), `docs/plans/sionna-live-channel.md`(GPU placement)
