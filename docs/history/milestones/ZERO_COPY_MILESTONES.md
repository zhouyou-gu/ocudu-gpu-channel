# 통합 GPU용 zero-copy channel emulator 마일스톤

> Historical record; its claims apply to the original date, revision and setup. Migrated from `ZERO_COPY_MILESTONES.md` at `58d3156` without changing recorded measurements.


**목표: 통합 GPU(DGX Spark GB10, Jetson Orin)에서 channel emulator가 슬롯마다 하는 CPU↔GPU IQ 복사를 없애, emulator 처리 지연(`gpu_process_us`, 라이브 브로커 `process_latency_summary`)을 줄인다. 출력은 복사 경로와 bit 단위로 같아야 한다.**

[`SPARK_MILESTONES.md`](SPARK_MILESTONES.md)(S 트랙), [`JETSON_MILESTONES.md`](JETSON_MILESTONES.md)(J 트랙)와 독립된 트랙이다. 단계 번호는 `Z`, 브랜치는 `gb10-zero-copy`(PR #2 브랜치 `a07b8a9`에서 분기). 판정 규율은 다른 트랙과 같다: 성능 주장은 같은 소스·같은 조건의 A/B로만 하고, 실패 측정도 보존하며, 공유 GPU에서 잰 시간은 다른 GPU 프로세스가 없었음을 함께 기록한다.

## 왜 하는가

emulator의 CUDA 백엔드는 디스크리트 GPU(RTX 5090) 기준으로 짜여 있다. 슬롯마다 pinned 호스트 버퍼(`cudaHostAlloc`)에 IQ를 모은 뒤 `cudaMemcpyAsync`로 H2D 복사하고, 커널이 끝나면 다시 D2H 복사한다. GB10은 CPU와 GPU가 같은 LPDDR5X를 쓰지만 코드가 복사를 직접 요청하므로 복사는 그대로 일어난다(버스 대신 같은 DRAM 안에서).

## Z0 기준선 (2026-09-26, 복사 경로)

같은 소스 `a07b8a9`, CUDA 13.0, 빅 코어 4개 고정, 11개 설정 × 10 s × 3회. 전체 표는 워크스테이션 `~/ocudu-work/perf-platform/compare.md`.

| 설정 | 단계 (p50, µs) | 5090 | GB10 |
|---|---|---|---|
| mvp-2edge | H2D | 14.1 | 8.4 |
| | kernel | 6.6 | 13.3 |
| | D2H | 11.0 | 11.4 |
| | **gpu_process_us** | **44.8** | **43.6** |
| tdl-a | kernel | 51.6 | 64.5 |
| | **gpu_process_us** | **86.2** | **93.5** |
| fanin-N64 | **gpu_process_us** | 47.5 | 54.4 |

라이브 legacy 1×1 브로커는 두 플랫폼 모두 p50 65 µs.

**기대 상한:** GB10 mvp-2edge에서 H2D + D2H ≈ 20 µs가 없앨 수 있는 최대치다 → 44 → 약 24 µs. tdl-a처럼 커널이 무거우면 비중이 작아 약 20%. GB10 커널이 5090의 약 2배인 문제는 이 트랙으로 풀리지 않는다.

## 설계

- **노브:** `runtime.cuda_host_memory: copy | zero_copy | auto`. 기본값은 처음에 `copy`였고 Z7에서 `auto`로 바꿨다. `auto`는 `cudaDevAttrIntegrated=1`이고 `cudaDevAttrCanMapHostMemory=1`일 때만 zero-copy를 쓴다. `zero_copy`를 명시하면 디스크리트에서도 허용해서 음성 대조군으로 잴 수 있다.
- **zero-copy 경로:** 호스트 버퍼를 `cudaHostAllocMapped`로 잡고, 커널은 `cudaHostGetDevicePointer` 포인터로 그 메모리를 직접 읽고 쓴다.
  - `host_source_iq`(원시 IQ, device-channel 경로 입력), `host_next_slot_start`, `host_output`: device 쪽 포인터가 같은 메모리의 별칭이다. H2D·D2H 복사가 없어진다.
  - `host_staged`(host-stage 경로 입력): `mapped_staged` 별칭으로 읽는다. `device_staged`는 device-channel 경로의 GPU 전용 중간 버퍼라 device 메모리로 남긴다.
  - 슬롯 끝의 `cudaStreamSynchronize`는 그대로 둔다. 호스트가 다음 슬롯 입력을 쓰기 전에 GPU가 이번 슬롯을 끝냈음을 보장하는 장치다.
- **아직 남는 복사:** 슬롯마다 올라가는 작은 메타데이터(`steps`, `step_meta`, `rx_steps`; pageable `std::vector`에서 H2D), 호스트가 edge IQ를 `host_source_iq`로 모으는 복사, `host_output` → 호출자 버퍼 `std::copy`. Z4에서 다룬다.
- **관측:** `ProcessorTimings.zero_copy`가 실제로 쓴 모드를 보고한다. zero-copy에서 `h2d_us`는 메타데이터만, `d2h_us`는 약 0이다. 비교 지표는 `gpu_process_us`(호출 전체 벽시계)다.

## 단계

| 단계 | 내용 | Exit 게이트 | 상태 |
|---|---|---|---|
| **Z0** | 기준선 — 복사 경로 5090 vs GB10 | 단계별 시간표, 라이브 p50 | **완료 2026-09-26** (위 표) |
| **Z1** | 프로토타입 — `cuda_host_memory` 노브 + 큰 IQ 버퍼 zero-copy | 로컬(5090) 빌드, ctest 통과, **copy vs zero_copy bit 동일**(device-channel 경로 + host-stage 경로, 4슬롯, 페이딩·CFO·AWGN·rx 잡음 모델 포함) | **완료 2026-09-27 (5090)** — ctest 12/12, bit 동일 통과, 음성 대조군 exit 1. 5090 A/B는 아래 |
| **Z2** | Spark 빌드 + 정확성 | sm_121 빌드, ctest 통과, `gpu-test-sequence.sh` 9단계를 `zero_copy`로 통과, 오류 카운터 0 | **완료 2026-09-27** — ctest 12/12(GB10에서 bit 동일), 9/9 통과, `auto` → zero-copy 확인 |
| **Z3** | **A/B 측정** — 같은 바이너리로 `copy` vs `zero_copy` | GB10 11개 설정 × 3회: `gpu_process_us`·`kernel_us` p50/p99. **커널이 mapped 메모리 때문에 느려지는지**가 핵심 관측. 5090에서 `zero_copy`는 음성 대조군(느려져야 정상) | **완료 2026-09-27** — GB10 11개 설정 전부 `gpu_process_us` p50 **−5~−12 µs(−6~−25%)**, 커널은 +6~+11 µs. 5090은 Z1에서 대조군 확인 |
| **Z4** | 남은 복사 제거 (Z3 결과 보고 결정) | 메타데이터를 mapped 메모리로, 입력 모으기와 출력 `std::copy` 제거 검토(GB10 `pageableMemoryAccessUsesHostPageTables=1`이라 호출자 버퍼 직접 접근 가능성). 단계마다 bit 동일 + A/B | **완료 2026-09-27** — 부분별 분해 측정. 기본값을 `in,out,direct`로(메타데이터 mapped는 역효과라 제외). 기본 2 edge 호출 46.1 → **31.8 µs(−31%)**. 입력 모으기 제거는 미착수(아래 Z8) |
| **Z5** | 라이브 — legacy 1×1 게이트를 `zero_copy`로 | attach·PDU·ping 통과, 카운터 0, 브로커 p50이 65 µs 대비 얼마나 줄었는지(같은 날 `copy` 1회와 짝지어 측정) | **완료 2026-09-27** — copy/zero-copy 짝 측정 2라운드(Z3 코드 4회 + 최종 코드 4회) 전부 통과. 최종 코드: ue0 p50 55–65 → **40–45 µs**, p99 100 → **80–85 µs** |
| **Z6** | Jetson Orin 확인 | Orin은 통합이지만 `ConcurrentManagedAccess=0`(D6). mapped pinned는 managed와 다른 경로라 동작해야 하지만 캐시 동작과 성능은 실측으로 판정. bit 동일 + A/B | **완료 2026-09-28** — ctest 12/12(bit 동일, `direct` 계열은 pageable 미지원으로 건너뜀), 9/9 통과, `auto` → zero-copy `in,out`. 클럭 고정 A/B 8개 설정 전부 −6 ~ −30%, 라이브 p50 415–455 → 345–360 µs(4회 pass) |
| **Z8** | **입력 직접 읽기 + 브로커 ring 복사 제거** (Z4 이후 남은 최대 병목) | 커널이 호출자/ring 버퍼에서 바로 읽어 `host_prep`의 입력 모으기 복사를 없애고, 브로커 ring read/push 복사를 줄임. 사용자 수×대역폭에서 슬롯 예산(500 µs) 안으로 들어오는지 | **완료 2026-09-27** — N64 gNB 934 → **177 µs**(예산 안), mvp 46.6 → **25.9 µs**. ring 단계 30–40 → 4–14 µs. 라이브 p50 60–65 → **40 µs** |
| **Z7** | 기본값 결정 + 정리 | Z3–Z6 근거로 `auto`를 기본으로 할지 결정, 문서·예제 YAML 갱신, PR 준비(push는 확인받고) | **완료 2026-09-28** — 기본값 `auto`. Jetson ctest 12/12, 기본 설정에서 Jetson `cuda_zero_copy=1`, 5090 `0`. push 안 함 |

## 위험

1. **커널이 느려질 수 있다.** mapped 메모리를 GPU가 읽고 쓸 때 캐시·일관성 동작이 `cudaMalloc` 메모리와 다르다. 복사 20 µs를 아껴도 커널이 그만큼 느려지면 이득이 없다. Z3에서 `kernel_us`를 따로 본다.
2. **출력 in-place 갱신.** rx 잡음 모델은 `host_output`을 제자리에서 읽고 다시 쓴다. zero-copy에서는 이 읽기·쓰기가 mapped 메모리에서 일어난다.
3. **디스크리트 회귀 금지.** 기본값 `copy`와 `auto`의 통합 GPU 한정으로 5090 경로는 바뀌지 않는다. Z3에서 5090 `copy` 수치가 Z0와 같은지 확인한다.
4. **공유 GPU.** Spark와 Jetson GPU는 다른 사용자 컨테이너와 공유한다. 측정 시점의 다른 GPU 프로세스 유무를 기록한다.

## 진행 기록

### Z1 — 2026-09-27

- 브랜치 `gb10-zero-copy`를 `a07b8a9`에서 분기했다(로컬 worktree `~/ocudu-work/ocudu-cuda-pr`).
- 변경: `include/ocudu_gpu_channel/config.h`(`CudaHostMemory`, `RuntimeConfig::cuda_host_memory`), `src/config.cpp`(파서), `include/ocudu_gpu_channel/processing.h`(`ProcessorTimings::zero_copy`), `src/cuda_backend.cu`(할당·해제·슬롯 경로).
- 테스트: `tests/test_processing.cpp` (h) copy vs zero_copy bit 동일, `tests/test_config.cpp` 노브 파싱.
- 검증(워크스테이션 RTX 5090, CUDA 13.0, sm_120, Release): ctest **12/12**. 새 parity 테스트의 **음성 대조군**: host-stage 경로가 `mapped_staged` 대신 채워지지 않은 `device_staged`를 읽게 일부러 바꾸면 `FAIL: zero-copy parity: host-stage path must be bit-identical to copy`, exit 1. 되돌린 뒤 다시 통과.
- **5090 A/B (음성 대조군, 같은 바이너리, 코어 2-5 고정, 5 s × 2회, p50 µs, 다른 GPU 프로세스 없음):**

| 설정 | 모드 | H2D | kernel | D2H | gpu_process_us |
|---|---|---|---|---|---|
| mvp-2edge | copy | 14.1 | 6.6 | 10.9–11.0 | **44.7** |
| | zero_copy | 1.9 | **34.7–35.1** | 0.35 | **48.5–49.0** |
| tdl-a | copy | 14.1 | 51.4 | 7.7 | **85.7–85.9** |
| | zero_copy | 1.9 | **3697–3711** | 0.6 | **3808–3821** |

  - `copy` 수치가 Z0(44.8 / 86.2)와 같다 → 기본 경로 회귀 없음.
  - 디스크리트에서 zero-copy는 예상대로 느리다. mvp는 복사 25 µs를 없애고 커널이 28 µs 늘었다.
  - **tdl-a는 43배 느려졌다.** `apply_channel_kernel`이 23탭 × 분수 지연 필터 8탭만큼 같은 입력 샘플을 반복해서 읽는데, 그 읽기가 전부 PCIe를 건넌다. **GB10에서도 mapped 메모리 읽기가 GPU 캐시에 잘 안 잡히면 같은 현상이 작게라도 나타날 수 있다 — Z3의 1순위 관측 항목.** 느리면 대안은 입력을 한 번만 읽어 shared memory에 올리는 커널 쪽 변경이다.

### Z2 — 2026-09-27 (Spark `ocudu-minwoo`, GB10 sm_121, CUDA 13.0.88)

- 복사본: `/workspace/gpuch/zc` = `a07b8a9` clone + 로컬 diff(`git diff | sha1sum` = `7511dde3…`, 워크스테이션과 같음). 빌드 `/workspace/gpuch/zc-builds/cuda-release`.
- ctest **12/12** — (h) copy vs zero_copy bit 동일이 GB10에서도 통과.
- **9단계 시퀀스:** 기본값만 `ZeroCopy`로 바꾼 시험용 복사본 `/workspace/gpuch/zc-default`(config.h에 `Z2 TEST-ONLY flip` 표시, 커밋 대상 아님)로 `gpu-seq-body.sh` 실행 → **GPU TEST SEQUENCE PASSED**, 로그 `/workspace/gpuch/zc-gpu-seq-20260927T072836Z.log`. 같은 빌드의 벤치가 YAML에 키 없이 `cuda_zero_copy,1`을 보고해 zero-copy로 돌았음을 확인했다. 수치는 09-26 복사 경로 실행과 같다(TDL-A 3.33, 2×2 상관 9.54, iid 6.96) — bit 동일과 일치.
- 빠른 확인(코어 16-19, 5 s × 2회): `auto`가 GB10에서 zero-copy를 고른다(`cuda_zero_copy,1`).

### Z3 — 2026-09-27 (Spark, 같은 바이너리 A/B)

- 하네스 `/workspace/gpuch/perf-zc-compare.sh`(Z0 하네스에서 CPU 백엔드 대신 `cuda_host_memory` copy/zero_copy를 교차 실행), 코어 5-8(Z0와 같음), 11개 설정 × 10 s × 3회, 66회 모두 ok. 결과 `/workspace/gpuch/perf-zc/perf-zc-20260927T073041Z`, 워크스테이션 사본 `~/ocudu-work/perf-platform/runs/spark-zc/`, 표 `~/ocudu-work/perf-platform/compare-zc.md`(분석 `analyze-zc.py`). 모든 csv가 요청한 모드를 보고했다. nvidia-smi에 다른 GPU 프로세스는 없었다(`gpu_util_before`는 직전 벤치 실행의 잔여값).

| 설정 | copy gpu_process p50 | zero_copy | Δ | H2D | kernel | D2H |
|---|---|---|---|---|---|---|
| mvp-2edge | 43.7 | **34.7** | −9.0 (−21%) | 8.8 → 3.3 | 13.3 → 20.3 | 11.5 → 0.8 |
| multi-ue-4edge | 45.2 | **34.6** | −10.6 (−23%) | 9.8 → 2.8 | 13.3 → 19.3 | 11.2 → 0.8 |
| graph-6edge | 49.1 | **44.4** | −4.7 (−10%) | 13.1 → 4.1 | 17.4 → 27.8 | 7.8 → 0.8 |
| multi-gnb-8edge | 46.6 | **39.8** | −6.9 (−15%) | 13.4 → 2.8 | 13.4 → 24.3 | 8.7 → 0.8 |
| stress-16edge | 47.5 | **35.6** | −11.9 (−25%) | 9.0 → 2.9 | 13.3 → 19.9 | 12.5 → 0.8 |
| tdl-a | 92.3 | **86.3** | −6.0 (−6%) | 8.4 → 3.3 | 64.5 → 71.4 | 8.5 → 0.8 |
| tdl-a-fanin8 | 96.6 | **87.9** | −8.7 (−9%) | 8.9 → 2.9 | 64.5 → 71.6 | 11.0 → 0.8 |
| fanin-N1 | 43.7 | **34.8** | −8.9 (−20%) | 8.4 → 3.3 | 13.4 → 20.4 | 11.5 → 0.8 |
| fanin-N4 | 46.8 | **35.1** | −11.7 (−25%) | 8.8 → 2.8 | 13.4 → 19.6 | 12.5 → 0.8 |
| fanin-N16 | 47.5 | **37.2** | −10.3 (−22%) | 8.7 → 3.2 | 13.7 → 20.4 | 7.8 → 0.8 |
| fanin-N64 | 54.4 | **43.4** | −10.9 (−20%) | 9.1 → 3.9 | 18.3 → 23.8 | 11.8 → 0.8 |

- 반복 간 p50 폭은 대부분 1 µs 이내라 차이는 잡음보다 훨씬 크다. copy 수치는 Z0(mvp 43.6, tdl-a 93.5, N64 54.4)와 맞다.
- **판정:** GB10에서 zero-copy는 모든 설정에서 emulator 호출 시간을 줄인다. 복사 약 17–22 µs가 사라지고, **커널이 +6~+11 µs 늘어** 순이득은 5–12 µs다. 5090에서 본 43배 붕괴(Z1)는 GB10에서는 없다 — mapped 메모리 읽기가 GPU에서 충분히 빠르다.
- 꼬리도 줄었다: fanin-N64 p99 407.6 → 212.7, fanin-N16 116.4 → 74.3, stress-16edge 78.6 → 62.3.
- 남은 H2D ~3 µs는 작은 메타데이터 복사, 커널 증가분은 mapped 메모리 접근 비용이다. 커널 증가분이 설정과 무관하게 거의 일정(+6~+7 µs, 1개 출력 행)한 것으로 보아 출력 쓰기 쪽이 주 원인일 가능성이 있다 — 추정이며 nsys로 확인하지 않았다. Z4의 첫 후보는 출력만 device 메모리 + D2H로 되돌리는 혼합 모드와 메타데이터 mapped화다.
- 벤치의 `model_mix_latency`도 같은 폭으로 줄었다(mvp 58.1 → 49.2).

### Z4 — 2026-09-27 (부분별 zero-copy 분해, Spark)

- 코드: `ZeroCopyParts`(in/out/meta/direct). `in`=`source_iq`·`next_slot_start`·`staged`, `out`=`output`, `meta`=`steps`·`step_meta`·`rx_steps`, `direct`=출력 행이 1개일 때 커널이 **호출자 버퍼에 직접** 쓴다(`cudaDevAttrPageableMemoryAccess=1` 필요; GB10과 이 워크스테이션 5090 모두 1). 측정용 스위치 `OCG_ZC_PARTS`(지원 노브 아님). `ProcessorTimings`에 `host_prep_us`(호출 시작→첫 GPU 명령), `host_out_us`(출력 복사), `call_us`(호출 전체 = 브로커 `process_us`) 추가, 벤치에 같은 행과 `--per-node` 추가.
- parity 테스트가 copy vs {기본, in,out / in,out,meta / in / out / meta / in,out,meta,direct / in,out,direct} 전부를 4슬롯 bit 동일로 검사한다. 5090·GB10 모두 ctest 12/12.
- 실험 드라이버 `~/ocudu-work/perf-platform/zc-scale.py`, `zc-final.py`, 분석 `analyze-zc-scale.py`, 표 `compare-zc-scale.md`. 결과 `/workspace/gpuch/zc-scale-20260927T075744Z`(229회), `/workspace/gpuch/zc-final-20260927T081914Z`(60회), 모두 rc=0, 모드 불일치 0. 코어 5-8, 5 s × 3회, 중앙값. 코드 diff sha1 `e1dae4f6…`(zc4) / `d6b06493…`(zc5, 최종).

**부분별 효과 (mvp-2edge, `call_us` p50):**

| 모드 | call | host_prep | H2D | kernel | D2H | host_out |
|---|---|---|---|---|---|---|
| copy | 46.4 | 2.0 | 8.5 | 13.4 | 11.6 | 4.7 |
| in만 | 42.1 | 2.0 | 2.8 | 18.0 | 8.5 | 4.8 |
| out만 | 38.8 | 2.1 | 10.5 | 15.4 | 0.8 | 5.7 |
| meta만 | **48.4 (악화)** | 2.1 | 7.0 | **21.3** | 7.8 | 4.8 |
| in,out | 36.8 | 2.0 | 3.3 | 20.7 | 0.8 | 5.6 |
| in,out,meta | 37.8 | 2.0 | 0.8 | 24.4 | 0.8 | 5.1 |
| **in,out,direct (최종 기본)** | **31.8** | 1.8 | 3.2 | 20.8 | 1.0 | **0.4** |

- **out이 in보다 이득이 크다:** D2H 복사(−11 µs)를 없애는 대가로 커널은 +2 µs. in은 H2D(−6 µs)를 없애고 커널 +4.6 µs.
- **meta는 역효과:** H2D는 1.5 µs 줄지만 커널이 +8 µs. 모든 GPU 스레드가 스텝 정보를 매번 읽는데, 그 읽기가 mapped 메모리로 간다. 그래서 기본값에서 뺐다.
- **direct:** 출력 `std::copy`(4–30 µs, 대역폭에 비례)가 사라진다.
- 최종 기본값 판정(`zc-final`): `in,out,direct`가 fan-in(N16 gNB 150.1 vs 155.9, N64 708.6 vs 757.2)과 mvp에서 가장 빠르고, tdl-a(83.0 vs 80.9)·122M(64.8 vs 62.7)에서는 meta 포함판보다 약 2 µs 느리다.

### Z5 — 2026-09-27 (라이브 legacy 1×1, Spark)

- 러너가 브로커 빌드 폴더를 `${native_root}/builds/ocudu-gpu-channel-cuda-release`로 고정해 두어, 다른 소스 트리에서는 CMake 캐시 불일치로 실패했다. `OCUDU_NATIVE_CHANNEL_BUILD`로 바꿀 수 있게 했다(Spark 복사본에만 적용, 저장소 반영은 PR 때).
- zero-copy 트리는 기본값만 바꾼 시험용 복사본(`zc-default`, `zc5-default`)이고, 같은 빌드의 벤치가 `cuda_zero_copy,1`을 보고했다. 라이브 로그의 `gpu_timings`에서도 D2H가 0.8–1.0 µs로 zero-copy가 돈 것이 보인다. copy 트리는 2.4–7.6 µs.
- 8회 전부 **통과**(rrc/PDU/ping, 카운터 0, rx_starvations 1–2 — 09-26 기준 게이트와 같은 수준). 최종 코드 전에 9단계 시퀀스도 다시 통과(`zc5-gpu-seq.log`).

| 코드 | 모드 | 실행 | gnb0 p50 / p99 | ue0 p50 / p99 |
|---|---|---|---|---|
| Z3 (in,out) | copy | 075358Z | 65 / 100 | 65 / 105 |
| | zero-copy | 075445Z | 55 / 90 | 55 / 90 |
| | copy | 075531Z | 55 / 100 | 60 / 105 |
| | zero-copy | 075617Z | 55 / 90 | 55 / 90 |
| 최종 (in,out,direct) | copy | 082700Z | 45* / 100 | 55 / 100 |
| | zero-copy | 082748Z | 45 / 80 | **40 / 80** |
| | copy | 082835Z | 45* / 100 | 65 / 100 |
| | zero-copy | 082921Z | 45 / 80 | **45 / 85** |

  \* 이 두 copy 실행의 gnb0은 호출 수가 약 1.6배(n=16.8k vs 10.7k)로, 샘플 수가 적은 조각 호출이 섞여 p50이 낮게 나왔다. 조각이 적은 ue0과 p99로 비교한다. 히스토그램은 5 µs 단위다.
- **판정:** 라이브에서 브로커의 emulator 호출은 p50 약 15–20 µs, p99 약 20 µs 줄었다(ue0 55–65 → 40–45, p99 100 → 80–85).
- **브로커의 다른 단계:** 같은 로그에서 ring 읽기(`read_us`)와 ring 쓰기(`push_us`)는 각각 중앙값 30–40 µs로 모드와 무관하게 그대로다. 둘 다 184 KB IQ를 CPU로 복사하는 단계다. 이제 emulator 호출(약 40 µs)보다 크다 → Z8.

## 무엇을 줄였나 — 슬롯 하나의 IQ 경로 (라이브 브로커, 1 ms = 23,040샘플 = 184 KB, 방향마다)

| # | 단계 | 누가 | copy | zero-copy (최종) |
|---|---|---|---|---|
| 1 | ZMQ 수신 → TX ring | 브로커 puller, CPU 복사 | 있음 | 있음 |
| 2 | TX ring → 입력 창 (`read_us`) | 브로커, CPU 복사 | 있음 | 있음 |
| 3 | 입력 창 → `host_source_iq` (`host_prep_us`) | emulator, CPU 복사 | 있음 | 있음 |
| 4 | `host_source_iq` → GPU 메모리 (H2D) | DMA | **있음** | **없음** — 커널이 mapped 메모리를 직접 읽음 |
| 5 | 커널 (TDL·페이딩·중첩·수신 잡음) | GPU | device 메모리 | mapped 메모리 읽기/쓰기, **+6~8 µs** |
| 6 | GPU 메모리 → `host_output` (D2H) | DMA | **있음** | **없음** — 커널이 호출자 버퍼에 직접 씀 |
| 7 | `host_output` → 호출자 행 (`host_out_us`) | emulator, CPU 복사 | **있음** | **없음** (direct) |
| 8 | 호출자 행 → RX ring (`push_us`) | 브로커, CPU 복사 | 있음 | 있음 |
| 9 | RX ring → ZMQ 응답 | 브로커 REP, CPU 복사 | 있음 | 있음 |

mvp-2edge 호출 하나로 본 이득(−14.3 µs): H2D IQ −5.3, D2H −9.7, 출력 복사 −4.4, 기타 enqueue/이벤트 −2.2, 커널 +7.5. 커널 증가분은 GPU가 자기 메모리 할당(`cudaMalloc`) 대신 호스트 매핑 메모리를 읽고 쓰는 비용이다. 부분별 측정에서 읽기(in +4.6, meta +8)가 쓰기(out +2)보다 비싸다. 원인은 추정이고 nsys나 ncu로 확인하지 않았다.

## 사용자 수·대역폭에 따른 스케일링 (Spark, `call_us` p50, µs)

**대역폭** (1 링크, 1 ms 배치, 샘플 수 = 샘플레이트/1000):

| 샘플레이트 | mvp copy | mvp zero-copy | 절감 | TDL-A copy | TDL-A zero-copy | 절감 |
|---|---|---|---|---|---|---|
| 23.04 MS/s (184 KB) | 46.1 | 31.8 | −14 (−31%) | 95.6 | 83.0 | −13 (−13%) |
| 46.08 MS/s | 58.0 | 37.6† | −20 (−35%) | 119.7 | 94.3† | −25 (−21%) |
| 92.16 MS/s | 98.6 | 52.3† | −46 (−47%) | 182.2 | 126.6† | −56 (−31%) |
| 122.88 MS/s (983 KB) | 120.7 | 64.8 | −56 (−46%) | 215.4 | 145.9† | −70 (−32%) |

† `in,out,meta,direct` 측정값(최종 기본과 1–2 µs 차이, `zc-final`에서 확인한 범위).

- **대역폭이 커질수록 절감이 커진다.** 없앤 복사 세 개(H2D, D2H, 출력 복사)가 모두 바이트 수에 비례하고, 커널 증가분은 그보다 느리게 는다(mvp에서 +7 → +22 µs, 복사 절감은 −19 → −72 µs).

**사용자 수** (gNB 1개 + UE N개; gNB 노드는 N개 UE의 업링크를 한 호출에서 중첩):

| N | gNB 노드 copy | zero-copy | 절감 | 그중 `host_prep`(입력 모으기) | UE 노드 하나 copy → zero-copy |
|---|---|---|---|---|---|
| 1 | 46.8 | 32.8† | −14 | 2 | 46.1 → 33.0† |
| 4 | 77.2 | 59.7† | −18 | 13–19 | 48.8 → 33.8† |
| 16 | 212.4 | 150.1 | −62 (−29%) | 73–100 | 49.5 → 35.1† |
| 64 | 933.7 | 708.6 | −225 (−24%) | **511–526** | 62.6 → 41.4† |

**사용자 × 대역폭** (gNB 노드): N16 @ 92.16 MS/s 924 → 650 µs(−274, `in,out,meta,direct`), N4 @ 92.16 MS/s 222 → 137 µs.

- **사용자가 늘어도 절감(µs)이 커진다.** gNB의 H2D 복사가 N에 비례하기 때문이다(N64에서 209 µs → 0). UE 노드는 각자 −10~−21 µs를 얻고, 브로커는 노드마다 스레드를 따로 돌린다.
- **다만 비율은 줄어든다(−31% → −24%).** 남은 시간의 대부분이 `host_prep`, 즉 N개 UE의 IQ를 CPU로 `host_source_iq`에 모으는 복사이기 때문이다. N64에서 526 µs(호출의 56–72%), N16 @ 92M에서 507 µs다. 이 복사는 zero-copy가 건드리지 않는다. 이 모델에는 샘플 단위 CPU 작업이 이 복사 말고 없다는 것은 코드를 읽고 판단했다.
- **30 kHz 슬롯 예산(500 µs):** N64 gNB(709 µs)와 N16 @ 92M(650 µs)은 zero-copy 후에도 초과한다. `host_prep`만 없애도 둘 다 예산 안으로 들어올 수 있다(추정) → Z8.

### Z8 — 2026-09-27 (입력 직접 읽기 + ring 블록 복사, Spark)

**입력 직접 읽기 (`direct_in`).** 채널 커널(`apply_channel_kernel`, `update_delay_line_kernel`)이 소스 IQ를 평면 버퍼(`base + src_index * count`) 대신 `DeviceSourceTable`로 찾는다. 이 테이블은 `__grid_constant__` 커널 파라미터로 넘기고, 최대 128개 소스 포인터를 담는다. direct 레이아웃에서는 `ptr[s]`가 호출자가 넘긴 입력 span 그 자체다. GB10은 pageable memory access가 되므로 GPU가 브로커 입력 창을 그대로 읽는다. 호스트는 `host_source_iq`로 모으는 복사도, 그 H2D도 하지 않는다. `build_steps`(snr_db 전력 추정)도 같은 span을 읽는다. 소스가 128개를 넘는 노드는 기존 평면 레이아웃으로 돌아간다. 기본 분할은 `in,out,direct,direct_in`이다(pageable access가 없으면 `in,out`).

- **테스트:** parity에 3개 UE(서로 다른 IQ) → gNB fan-in 케이스를 추가했다. 소스 테이블의 항목이 모두 다른 버퍼라서 잘못된 `src_index`가 불일치로 드러난다. 분할은 `direct_in` 단독과 `in,out,direct,direct_in`을 추가했다. **음성 대조군:** direct 레이아웃이 항상 `ptr[0]`을 읽게 바꾸면 `FAIL: zero-copy parity: 3-source fan-in must be bit-identical to copy`, exit 1이 나온다. 5090·GB10 ctest 12/12.
- **측정** `zc-z8.py`(`/workspace/gpuch/zc-z8-20260927T085106Z`, 72회 rc=0, 코어 5-8, 5 s × 3회), 표 `~/ocudu-work/perf-platform/compare-zc-z8.md`. 코드 sha1 `cae9dac8…`(ring 수정 전). gNB 노드 `call_us` p50:

| 설정 | copy | Z5 (in,out,direct) | **Z8 (+direct_in)** | host_prep: copy → Z8 |
|---|---|---|---|---|
| mvp-2edge | 46.6 | 32.1 | **25.9 (−44%)** | 2.0 → 0.1 |
| tdl-a | 95.9 | 82.0 | **77.9 (−19%)** | 2.0 → 0.1 |
| mvp @ 122.88 MS/s | 122.8 | 64.6 | **31.5 (−74%)** | 21.7 → 0.1 |
| N4 | 77.4 | 54.8 | **32.1 (−59%)** | 18.5 → 0.4 |
| N16 | 213.0 | 146.7 | **48.0 (−77%)** | 98.7 → 1.5 |
| N64 | 934.4 | 713.8 | **176.7 (−81%)** | 523.7 → 7.1 |
| N4 @ 92.16 MS/s | 230.2 | 136.7 | **40.4 (−82%)** | 91.9 → 0.4 |
| N16 @ 92.16 MS/s | 926.1 | 646.4 | **144.7 (−84%)** | 507.5 → 1.5 |

  - **500 µs 슬롯 예산:** Z5에서 넘던 N64(714)와 N16 @ 92M(646)이 177·145 µs로 예산 안에 들어왔다.
  - **커널도 Z5보다 빨라졌다**(mvp 21.1 → 16.8, N16 57.6 → 36.9, 122M 38.9 → 22.3). 입력을 mapped pinned 버퍼 대신 호출자의 pageable 메모리에서 읽기 때문이다. GB10에서는 GPU의 pageable 읽기가 mapped pinned 읽기보다 싸다는 뜻이다. 원인은 추정이고 프로파일러로 확인하지 않았다.
  - 남은 H2D ~3 µs는 step 메타데이터다(Z4에서 mapped화는 역효과로 판정).

**브로커 ring 블록 복사.** `IqRing::push`/`read`가 샘플마다 `% capacity`를 계산하며 한 개씩 복사하고 있었다. Z5에서 본 ring 단계 30–40 µs의 원인이다. 이제 wrap 전후 최대 두 번의 `std::copy`로 복사한다. 동작은 같고, CPU 백엔드에도 적용된다. `test_ring`에 무작위 참조 모델 비교를 추가했다(용량 1/3/7/16/61, 각 4000 연산, push·read·discard). 음성 대조군으로 wrap 뒤 구간을 한 칸 밀면 기존 wrap 테스트가 exit 1을 낸다.

**라이브 (legacy 1×1, `zc9`/`zc9-default`, 코드 sha1 `e0409e2e…`):** 9단계 시퀀스 통과, 게이트 4회 모두 통과.

| 실행 | 모드 | gnb0 p50 / p99 | ue0 p50 / p99 | ring read / push (중앙값) | emulator process |
|---|---|---|---|---|---|
| 090009Z | copy | 65 / 105 | 60 / 105 | 5.6 / 3.8 | 57.8 |
| 090056Z | zero-copy | **40 / 80** | **40 / 80** | 10.9 / 11.1 | 37.4 |
| 090144Z | copy | 65 / 105 | 65 / 105 | 5.9 / 3.5 | 57.2 |
| 090229Z | zero-copy | **40 / 80** | **40 / 80** | 10.5 / 13.8 | 39.1 |

- 이번 copy 실행은 조각 호출 없이 n이 약 10.6k로 zero-copy 실행과 같아서, 두 노드 모두 바로 비교할 수 있다. **p50 60–65 → 40 µs, p99 105 → 80 µs.**
- ring 단계는 Z5의 30–40 µs에서 copy 3.5–6 µs, zero-copy 9–14 µs로 줄었다. 브로커가 슬롯 하나에 쓰는 CPU+GPU 시간(read + process + push)은 Z5 이전 약 115–135 µs에서 **zero-copy 약 60 µs**가 됐다.
- **관측만 한 것:** zero-copy에서 ring 단계가 copy보다 5–10 µs 길다(두 번 모두). GPU가 방금 읽은 입력 창과 방금 쓴 출력 행을 CPU가 다시 복사하는 데 드는 일관성 비용으로 추정하지만, 확인하지 않았다.
- 라이브 H2D 7–8 µs(벤치 3 µs)와 커널 23–24 µs(벤치 17 µs)는 벤치보다 크다. 라이브에서는 puller·REP·producer 스레드가 같은 CPU에서 함께 돈다. 차이의 원인은 따로 분리하지 않았다.

**다음 후보 (측정 근거 순):**
1. 라이브 커널·H2D가 벤치보다 큰 원인 분리. 스레드 배치와 CPU 고정을 본다.
2. step 메타데이터 H2D 약 3–8 µs. pinned(비 mapped) 버퍼에서 async로 보내거나, 커널 파라미터로 넘기는 방법이 있다.
3. ring을 거치지 않는 경로. puller가 받은 버퍼를 GPU가 직접 읽고, 출력을 RX ring 저장소에 직접 쓴다. 브로커 구조를 바꿔야 한다.
4. Z6 Jetson 검증 후 `auto` 기본화(Z7).

### Z6 — 2026-09-28 (Jetson AGX Orin, sm_87, CUDA 12.6, MODE_30W)

- **복사본:** `jetson-minwoo:/workspace/gpuch/zc6` = `8d916f3` bundle clone, 코드 diff 없음. 빌드 `builds/zc6-release`(`-j4`). 기본값만 `ZeroCopy`로 바꾼 시험용 복사본 `zc6-default`(config.h `Z6 TEST-ONLY flip`과 `test_config.cpp` 기본값 검사 한 줄, 커밋 대상 아님). 두 트리 모두 게이트 스크립트에 Jetson 전용 패치를 적용했다(Spark 패치와 같음: arch 87, x86 lock 검사 생략, gNB 버전 정규식, `start_group` pgid 폴링, 빌드 `-j4`).
- **조건:** `MODE_30W`(코어 8/12 online, CPU 최대 1.728 GHz, GPU 최대 612 MHz). `jetson_clocks`는 적용하지 않았다(공유 장비). 측정 중 다른 GPU 작업은 없었다: user-a 브로커 미실행, `nvidia-smi` 프로세스 없음, 호스트 tegrastats 로그를 보존했다. 09-25 라이브 실행에서 남은 우리 컨테이너의 mongod 2개는 종료했다.
- **장치 속성:** `Integrated=1`, `CanMapHostMemory=1`, **`PageableMemoryAccess=0`**, `ConcurrentManagedAccess=0`(D6). 따라서 **Orin의 `auto`는 zero-copy를 고르고, 분할은 `in,out`이다.** `direct`·`direct_in`은 쓸 수 없고, `OCG_ZC_PARTS=direct_in`은 `needs pageable memory access`로 거부된다. 폴백은 설계대로 동작한다. mapped pinned 메모리는 managed 메모리와 다른 경로라서 D6(`cudaErrorInvalidDevice`)은 나타나지 않았다.

**정확성**

| 항목 | 결과 |
|---|---|
| ctest | **12/12**. parity (h)가 copy vs {기본(=in,out), in,out, in,out,meta, in, out, meta}를 4슬롯 bit 동일로 통과했다. 3-소스 fan-in 케이스도 포함한다. `direct` 계열은 `zero-copy parity: direct skipped (no pageable memory access)`로 건너뛰었다(3회) |
| 9단계 시퀀스 (`zc6-default`, zero-copy) | **GPU TEST SEQUENCE PASSED** — TDL-A 3.32/3.27, 2×2 상관 9.65, iid 6.96, correlation_swap 9.26, 카운터 0. 같은 빌드의 벤치가 `cuda_zero_copy,1`을 보고했다 |

**A/B 측정** (같은 바이너리, 모드는 반복마다 교차, 5 s × 3회, 중앙값). 표 전체는 `~/ocudu-work/perf-platform/compare-zc-jetson.md`, 원자료는 `runs/jetson-zc/zc-jetson-20260928T054543Z`(1차 246회 + 2차 72회, 모두 rc=0, 모드 불일치 0).

- 1차 측정(코어 4-7, CPU 클럭 자유)에서는 같은 설정이 반복마다 최대 1.6배 흔들렸다. 예를 들어 tdla-46M copy의 커널이 647–1087 µs였다. **GPU 커널 시간이 CPU 클러스터 클럭을 따라 움직인다**(메모리 클럭 연동으로 추정). 그래서 2차는 코어 7에서 busy loop를 돌려 클러스터를 1.728 GHz에 붙잡고, 벤치를 코어 4-6에서 돌렸다. 아래는 2차 수치다(`call_us`, 전 노드).

| 설정 | copy p50 | zero-copy p50 | Δ | p99 copy → zc | H2D | kernel | D2H |
|---|---|---|---|---|---|---|---|
| mvp-2edge | 251.4 | **183.2** | −68 (−27%) | 264 → 197 | 49.7 → 10.0 | 86.8 → 97.4 | 39.1 → 2.3 |
| multi-gnb-8edge | 379.2 | **295.3** | −84 (−22%) | 404 → 318 | 74.0 → 11.3 | 134.3 → 147.0 | 34.0 → 2.3 |
| fanin-N16 | 266.6 | **200.4** | −66 (−25%) | 1567 → 1174 | 52.0 → 12.5 | 96.2 → 104.7 | 33.2 → 2.2 |
| tdl-a | 475.3 | **447.7** | −28 (−6%) | 490 → 461 | 31.3 → 11.0 | 351.3 → 364.5 | 22.1 → 1.6 |
| mvp @ 122.88 MS/s | 816.9 | **575.3** | −242 (−30%) | 850 → 606 | 146.2 → 14.5 | 247.0 → 259.4 | 126.3 → 2.3 |
| TDL-A @ 46.08 MS/s | 727.5 | **676.7** | −51 (−7%) | 749 → 693 | 46.3 → 11.9 | 539.7 → 553.7 | 30.2 → 1.6 |
| TDL-A @ 122.88 MS/s | 1632.9 | **1497.3** | −136 (−8%) | 1885 → 1744 | 83.7 → 15.3 | 1186.8 → 1189.4 | 67.6 → 1.6 |
| N4 @ 92.16 MS/s | 651.4 | **463.6** | −188 (−29%) | 1708 → 1272 | 120.1 → 16.2 | 201.0 → 219.5 | 97.3 → 2.3 |

- **판정:** Orin에서도 zero-copy(`in,out`)는 모든 설정에서 호출 시간을 줄인다. 1차 측정 19개 설정도 방향이 모두 같았다(−5 ~ −60%). 예외는 tdla-46M 한 행(+39%)인데, 위의 클럭 흔들림 때문이고 2차에서는 −7%다. 복사 경로가 GB10보다 훨씬 비싸서(mvp H2D 50 µs + D2H 39 µs, GB10은 8 + 11) 절감 폭(µs)이 GB10보다 크다. **커널 증가분은 +3 ~ +18 µs로 GB10(+6 ~ +11)과 비슷하다.** 5090(Z1)에서 본 붕괴는 없다.
- **`auto` = `zc`:** 1차 11개 설정에서 `auto`와 `zc`는 1–5 µs 안에서 같다.
- **분할별(1차, mvp):** in만 216, out만 208, in,out 182, in,out,meta **177**. **GB10과 반대로 Orin에서는 meta를 mapped로 두는 편이 3–12 µs 빠르다**(2차 8개 설정 모두 −1 ~ −12 µs). 기본 분할은 바꾸지 않았다. 플랫폼별 분할은 Z7 후보다.
- **남는 비용:** `host_prep`(입력 모으기)와 `host_out`(출력 `std::copy`)이 Orin에서는 zero-copy 뒤에도 남는다. GB10은 이를 `direct`·`direct_in`으로 없앴는데, 이 둘은 pageable access를 요구한다. 예: mvp 16 + 28 µs, 122M 124 + 139 µs, N16 gNB 노드 439 µs(1차). Orin에서 이것을 없애려면 브로커 ring 저장소 자체를 mapped pinned로 잡아야 한다(다음 후보 3과 같은 구조 변경).

**라이브 (legacy 1×1, `zc6` copy / `zc6-default` zero-copy 교차, CPU gNB `a1916edc` + srsUE):** 4회 모두 **pass**(attach·PDU·ping, 카운터 0). 게이트 빌드의 모드는 벤치로 확인했다(`cuda_zero_copy,0` / `,1`).

| 실행 | 모드 | gnb0 p50 / p99 | ue0 p50 / p99 | ring read / push | rx_starvations |
|---|---|---|---|---|---|
| 060716Z | copy | 455 / 1695 | 450 / 1720 | 21.0 / 18.3 | 33 |
| 060925Z | zero-copy | **360 / 1455** | **350 / 1415** | 22.7 / 17.2 | 18 |
| 061132Z | copy | 420 / 1770 | 415 / 1670 | 20.6 / 17.7 | 32 |
| 061214Z | zero-copy | **355 / 1375** | **345 / 1390** | 19.3 / 18.6 | 28 |

- p50 415–455 → **345–360 µs(약 −20%)**, p99 1670–1770 → **1375–1455 µs**. starvation도 줄었다(32–33 → 18–28).
- **30 W Orin은 23.04 MS/s 라이브에서 여유가 없다.** zero-copy를 써도 p50이 500 µs 슬롯 예산의 70%이고, p95는 약 1 ms로 예산을 넘는다. Spark(p50 40 µs)와 달리 emulator 호출 자체가 크다. 커널만 약 90 µs다(GPU 612 MHz, SM 8개). attach는 통과하지만, 더 높은 대역폭이나 다중 UE 라이브는 MAXN 없이 기대하기 어렵다(MAXN 전환은 user-a 합의 필요, 이번에는 바꾸지 않았다).

**Z7에 대한 결론:** 통합 GPU 두 종(GB10, Orin) 모두에서 zero-copy가 bit 동일이고, 벤치·라이브 모두 빠르다. `auto`는 두 플랫폼에서 의도대로 해석된다(GB10 `in,out,direct,direct_in`, Orin `in,out`). 디스크리트는 `auto` → copy로 경로가 바뀌지 않는다. **`auto`를 기본값으로 하는 근거는 이것으로 충분하다.** 남은 Z7 작업은 기본값 전환(`config.h`와 `test_config` 기본값 검사), README·예제 갱신, PR 정리다. 플랫폼별 meta 분할(Orin에서 in,out,meta)은 선택 사항이다.

### Z7 — 2026-09-28 (기본값 `auto`)

- **결정:** Z3–Z6에서 두 통합 GPU 모두 bit 동일이고 벤치·라이브 모두 빨랐다(GB10 mvp 46.6 → 25.9 µs, Orin 251 → 183 µs, 라이브 p50 GB10 60–65 → 40, Orin 415–455 → 345–360 µs). 디스크리트는 Z1에서 느려지는 것을 확인했고 `auto`가 거기서 `copy`를 고른다. 그래서 `RuntimeConfig::cuda_host_memory` 기본값을 `Auto`로 바꿨다.
- 변경: `include/ocudu_gpu_channel/config.h`(기본값과 주석), `tests/test_config.cpp`(기본값 검사 `Auto`, `copy` 파싱 검사 추가), `README.md`(노브 표, Orin 수치). 예제 YAML은 `cuda_host_memory`를 쓰지 않으므로 바꿀 것이 없다. 라이브 렌더러(`render-multi-gnb-configs.py` 등)는 모드를 명시하므로 A/B 측정은 영향이 없다.
- **검증:**
  - Jetson(`/workspace/gpuch/zc7`, sm_87, CUDA 12.6): ctest 12/12. `cuda_host_memory`가 없는 `topology.mvp.cuda.yaml`로 벤치 → `cuda_zero_copy,1`.
  - 워크스테이션 5090(CUDA 13.0, sm_120): 같은 벤치 → `cuda_zero_copy,0`(copy 유지).
- **별도 발견 — 5090 ctest 간헐 실패 (Z7과 무관):** 5090에서 `matrix_profile_history`가 간헐적으로 실패한다(`FAIL: long-delay CUDA echo must match CPU`, `FAIL: delayed echo must survive a gain/phase or identical matrix update`). `processing`도 한 번 실패했다. 같은 GPU에서 다른 브로커 프로세스(OAI 2×2 트랙)가 돌고 있었다. 8회 반복 기준 실패 횟수: `a07b8a9`(zero-copy 이전) 6/8, `6fab580` 3/8, `285fad6` 5/8. zero-copy 이전부터 있던 문제이고, 단독 실행이던 Z1 때는 12/12였다. GPU를 공유할 때 드러나는 타이밍 의존으로 보이지만 원인은 확인하지 않았다. OAI 트랙이 끝나 GPU가 빈 뒤 `c3bde7f`로 12회 반복하니 12/12 통과했다. 실패는 GPU 공유 조건에서만 난다. 원인은 아래 절에서 찾았다(백엔드의 stream 순서 버그, `7f26454`로 수정).


### 5090 ctest 간헐 실패의 원인 — 2026-09-28 (`fix-contention-flake` `3e110a0`, 이 브랜치 `7f26454`)

- **원인:** `prepare()`가 노드 상태를 올릴 때 legacy default stream의 동기 `cudaMemcpy`를 썼다(`src/cuda_backend.cu`의 `device_link_states`, `device_row_begin`, `device_correlation_groups` 업로드). 원본이 pageable 메모리(`std::vector`)면 `cudaMemcpy`는 스테이징 버퍼에 복사만 하고 DMA가 끝나기 전에 돌아올 수 있다. 슬롯 경로의 `sp.stream`은 `cudaStreamNonBlocking`이라 legacy stream 작업 뒤로 순서가 보장되지 않는다. 그래서 첫 슬롯의 snap-refresh D2H가 아직 도착하지 않은 상태(전부 0, `has_tdl=0`)를 읽었다. 그러면 호스트 사본의 `has_tdl`이 0이 되어 그 뒤 계수 갱신이 이 edge에 전혀 반영되지 않는다. 늦게 도착한 초기 DMA는 갱신된 상태를 다시 덮어썼다.
- **왜 GPU 공유 때만 나나:** 다른 CUDA 컨텍스트가 있으면 time-slicing 때문에 그 DMA가 늦어진다. 블록 1개짜리 바쁜 커널만 다른 프로세스에서 돌려도 매번 재현된다. CPU만 부하를 주면 재현되지 않는다. `CUDA_LAUNCH_BLOCKING=1`로도 사라지지 않았다(커널 순서 문제가 아니라 복사 순서 문제). `compute-sanitizer initcheck`은 0건이었다(메모리는 초기화돼 있었고, 도착 시점만 늦었다).
- **증거:** 계측 로그. 경합 시 첫 snap-refresh 직후 호스트 사본은 `gain=0 has_tdl=0 n_taps=0`이었고 장치 쪽은 `gain=1 n_taps=1`이었다. 유휴 시에는 둘 다 1이었다. `prepare()` 끝에 `cudaDeviceSynchronize()`를 넣는 실험만으로 경합 시 10/10 통과로 바뀌었다(넣지 않으면 5/5 실패).
- **수정:** 세 업로드를 `sp.stream`의 `cudaMemcpyAsync`로 바꾸고, `prepare()` 끝에서 `cudaStreamSynchronize(sp.stream)`로 기다린다.
- **판정 (5090, 다른 프로세스에서 블록 1개짜리 바쁜 커널을 돌리는 조건, 전체 ctest):** 수정 후 **30/30 통과**, 수정 전(음성 대조군, 같은 소스에서 수정만 뺌) **0/30**(매번 `matrix_profile_history`와 `processing` 실패). 유휴 GPU에서 수정본은 3/3 통과.
- **영향:** 플랫폼과 무관하게 CUDA 백엔드 전체에 해당한다. copy·zero-copy 모두 같은 업로드 경로를 쓴다. 증상이 나려면 `prepare()`와 첫 슬롯 사이 간격이 초기 DMA보다 짧아야 한다. 라이브 브로커는 기동 뒤 gNB 연결까지 수 초가 걸려 걸릴 가능성이 낮지만, 원리상 가능하다. 특히 CUDA gNB가 같은 GPU를 쓰는 구성(S7 다중 gNB, S8 100 MHz CUDA gNB)이 그렇다. 걸리면 조용히 틀린다: 그 edge는 초기 계수에 고정되고 제어 갱신이 무시된다. 첫 슬롯의 `row_begin`이 늦으면 superpose 행 경계도 틀릴 수 있다. 라이브에서 관측된 적은 없다.
