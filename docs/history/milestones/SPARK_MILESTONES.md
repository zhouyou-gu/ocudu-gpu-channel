> Editorial note (2026-10-09): local paths and workflow narration were normalized for this [public edition](../../development/documentation.md#public-editions); measurements and conclusions are unchanged.

# DGX Spark(GB10)에서 CUDA 가속 OCUDU 검증 마일스톤

> Historical record; its claims apply to the original date, revision and setup. Migrated from `SPARK_MILESTONES.md` at `58d3156` without changing recorded measurements.


**목표: WG1의 기준 플랫폼(DGX Spark / GB10)에서 벤더 브랜치 `nvcuda_accel_02`를 빌드·자체 검증·라이브 attach까지 확인하고, 5090(C 트랙)·Orin(J 트랙) 결과와 같은 절차로 대조한다.**

[`CUDA_MILESTONES.md`](CUDA_MILESTONES.md)(RTX 5090, 디스크리트), [`JETSON_MILESTONES.md`](JETSON_MILESTONES.md)(Orin, 통합 + CMA 0)와 독립된 트랙이다. 단계 번호는 `S`. 판정 규율(음성 대조군 exit≠0 + 메커니즘 로그 확인, "무엇이 실제로 돌았는가" 판정, 실패 측정 보존)은 C·J 트랙과 같다.

## 왜 Spark인가

WG1이 문서에 적은 검증은 **이 기종**에서 나왔다. 앞의 두 플랫폼은 모두 WG 환경과 어긋났고, 그 어긋남이 결함을 드러냈다(5090: D1–D5, Orin: D6). Spark는 어긋남이 가장 작은 대조군이다.

| 축 | WG 기준 | 이 Spark | 5090 | Orin |
|---|---|---|---|---|
| GPU | GB10 `sm_121` | **GB10 `sm_121`, SM 48** | `sm_120` | `sm_87` |
| CUDA / 드라이버 | 13.0.88 / 580.95.05 | **13.0.88** / 580.178.04 | 12.8.93 / 595.71.05 | 12.6.68 / 540.4.0 |
| CPU | X925/A725 aarch64 | **같음, 20코어** | x86_64 | A78AE |
| managed 속성 CMA/hostPT/direct | 벤더 표 1/1/0 | **1/1/0 실측(09-24)** | 1/0/0 | 0/0/0 |

**첫 번째 수확(09-24):** WG1 제보서(이슈 #2)에 "GB10 값은 벤더 표로 추론했고 실측하지 않았다"고 적은 `1/1/0`을 **실측으로 확인**했다. 따라서 제보서의 결론 — `fully_coherent`(셋 다 1)는 GB10에서도 불만족이라 직접 하향 링크 리더는 GH200급에서만 켜진다 — 은 이제 실측 근거를 갖는다. 라이브에서 실제로 DEGRADED가 나는지는 S4에서 본다.

**예상:** Orin D6은 `ConcurrentManagedAccess=0`에서만 나는 문제라 Spark(1)에서는 재현되지 않아야 한다. 5090 D1–D5 중 D1·D2는 managed 그리드에서는 벤더의 GB10 경로를 타므로 벤더 테스트가 통과할 가능성이 높다. D3·D4는 플랫폼 무관(설정 전달·SINR 대입)이라 벤더 테스트로는 여전히 보이지 않을 것이다.

## 환경

컨테이너 `ocudu-minwoo`(DGX Spark 호스트 `spark-host`, 포트 2202, 워크스테이션에서 `ssh spark-minwoo`). 구성·생성 과정은 [`scripts/cuda/spark/README.md`](../../../scripts/cuda/spark/README.md). 작업 루트 `/workspace/ocudu-spark/{src,builds,install,tools,results}`, 레포 `/workspace/ocudu-cuda-rebuild`. GPU는 user-a·user-b 컨테이너와 **공유**한다 — 시간 측정을 기록할 때는 다른 GPU 프로세스가 없었음을 함께 기록한다.

## 단계

| 단계 | 내용 | Exit 게이트 | 상태 |
|---|---|---|---|
| **S0** | 컨테이너·소스 준비 | netns·TUN·rtprio, GB10 보임, 벤더 소스 `5830c9cb` 깨끗함, 레포 전송 | **완료 2026-09-24** |
| **S1** | **벤더 원본 빌드 + 자체 검증 (패치 없이)** — J1 스크립트를 `CUDA_ARCH=121`로 | 빌드 성공, WG 12 PHY + OFH 판정, 로그 판정기, **WG 문서의 "12/12, 252.53 s"와 대조**. 실패마다 메커니즘 | **완료 2026-09-24 — PHY 9/12, OFH 16/16. 실패 3건 = 5090의 D1·D2와 동일 → 하드웨어 무관 벤더 결함** |
| **S2** | C1 패치(`d2579af2`) 적용 재검증 + 음성 대조군 | PHY 14 / OFH 16, NC-D1–D5가 Spark에서 무엇을 보이는지(managed 경로에서는 D1·D2 대조군이 반응하지 않을 수 있음 — 그 자체가 기록 대상) | **완료 2026-09-24 — PHY 14/14, OFH 16/16, NC-D1–D5 전부 exit≠0 + 메커니즘 확인** |
| **S3** | aarch64 스택 + CPU 기준선 — Open5GS, srsUE, CPU OCUDU, 직결 ZMQ | CPU gNB 3/3, CUDA gNB 전 모드 `disabled` 3/3 | **완료 2026-09-25** — CUDA gNB `disabled` 3/3, `all` 3/3(09-24). CPU gNB(`a1916edc`) 4/4(09-25, 러너에 `cpu` stage 추가) |
| **S4** | 단계별 활성화 + 백엔드 판정 | `verify-stage-backends.py`로 selected/degraded/fallback. **lower-PHY TX가 GB10에서도 DEGRADED인지** 실측 | **완료 2026-09-24** — 5단계 전부 통과, silent fallback 0. TX direct 경로는 GPU PDSCH가 켜진 단계부터만 |
| **S5** | BLER/SINR 정합 + 이슈 6(16QAM CRC 상승) 재현 여부 | CPU 대비 BLER ≤1%p, SINR ≤0.5 dB, 이슈 6 판정 | **완료 2026-09-25** — C1: 할당 일치 PASS지만 합산 BLER 6배 → 원인 D8(1 PRB SINR) + D9(LDPC 계수). **C1+D8+D9: 교차 6회 ΔBLER −0.00%p, ΔSINR −0.19 dB, 16QAM `[0,17)` 1.90 vs 1.89%** |
| **S6** | 측정 — 20 MHz 1-layer와 WG 수치 구성(100 MHz 4-layer) | WG 표의 21.20×(PUSCH) 등 재현 여부. **여기서부터 성능 주장 가능** | **완료 2026-09-25** — 100 MHz 4L: 감도·PDSCH 문서와 일치, **PUSCH 22×(CPU 빅 코어 고정; 미고정 40×는 착시)**. 20 MHz 1L: PDSCH 문서와 일치, PUSCH는 고정 시 1.1×(미고정 2.3×는 착시). 라이브 20 MHz 1L에서는 GPU가 느리다 |
| **S7** | **채널 에뮬레이터 경유 + 멀티 gNB** — CUDA gNB를 emulator에 붙이고, CUDA gNB 프로세스 2개(2셀, 셀 간 간섭)를 한 GPU에서 | CUDA 1×1 emulator 게이트 통과, CPU 2셀 기준선 통과, CUDA 2셀에서 UE마다 자기 셀에 붙어 RRC/PDU/ping, 카운터 0, late 0 | **완료 2026-09-27** — 셋 다 통과. CUDA gNB 1개당 GPU 메모리 9,545 MiB. MPS 없이 시간 분할로 동작. zero-copy 브로커와 조합해도 통과했고(copy·zero-copy 각 2회), 브로커 p50이 30–35 µs, p99가 50–60 µs 줄었다 |
| **S8** | **셀 대역폭 확장** — 20 MHz를 넘는 라이브 셀을 emulator 경유로, 브로커 copy vs zero-copy | 대역폭마다 attach·PDU·ping, 브로커 p50/p99, 막히는 곳은 메커니즘까지 | **완료 2026-09-28** — srsUE는 20 MHz가 한계(30 MHz PRACH 불가, 40/50 MHz 크래시). **OAI nrUE로 30/40/50 MHz(n3 FDD 15 kHz)와 100 MHz(n78 TDD 30 kHz) 전부 통과**, CUDA gNB(`all`) 100 MHz도 통과. zero-copy 브로커 p50은 20→100 MHz에서 40–45 µs로 거의 그대로, copy는 60 → 90 µs |
| **S9** | **S8 관측의 원인 규명** — OAI 실행이 실시간보다 느린 이유, CUDA gNB 100 MHz의 브로커 p99 증가, zero-copy에서 ring 단계가 길어지는 이유 | 원인마다 증거와 되돌리는 조작 | **완료 2026-09-28** — (1) OAI ZMQ 드라이버가 TX 응답을 최대 10 ms 늦춤, 패치 후 20 MHz 256–382 → 1000 슬롯/s(실시간), starvation 3,335 → 1. (2) GPU 컨텍스트 time-slicing, MPS로 p99 195 → 80 µs(CPU gNB와 같음). (3) GPU가 만진 pageable 버퍼의 CPU memcpy가 3–6배 느려짐 |
| **S10** | **100 MHz가 패치 후에도 실시간의 0.72×인 이유** | 슬롯 경로를 홉별로 나누고, 원인을 되돌리는 조작으로 확인 | **완료 2026-09-28** — 원인은 바이트가 아니라 **CPU 배치**다. lock-step 고리에 파이프라이닝이 사실상 없어(리드 0–1 메시지) 슬롯마다 고리 한 바퀴를 기다리는데, 그 고리의 스레드 전환이 (a) 깊은 idle 상태(LPI-3, 탈출 지연 433 µs 선언)에 들어간 코어를 깨우고 (b) 절반은 A725 little 코어에서 돈다. **gNB·브로커·UE를 X925 big 코어에 나눠 고정하면 1,470 → 2,060–2,077 슬롯/s(실시간)**, CUDA gNB + MPS도 2,068. little 코어 고정은 1,522 |
| **S11** | **S9–S10 해결책을 게이트 기본값으로 + S8 재측정** — 패치 OAI ZMQ 모듈, 플랫폼 CPU 배치, CUDA gNB면 MPS를 환경 변수 없이 적용하고, 그 조건에서 20–100 MHz를 다시 잰다. 브로커 코어 수와 라이브·벤치 차이도 가른다 | 기본값만으로 실시간, 적용 내역이 실행마다 기록됨, srsUE 게이트 무회귀, 대역폭마다 copy·zero-copy 2쌍, 원인마다 대조 | **완료 2026-09-28** — 24/24 통과, **20–100 MHz 전부 실시간**(S8 0.3×), starvation 3,000대 → 1(CUDA gNB 5). 브로커를 3코어로 주니 100 MHz p99 115–135 → **65 µs**(CPU·CUDA gNB). 라이브 커널이 벤치보다 긴 이유는 little 코어 배치(이제 해결)와 CPU가 막 쓴 입력(+1.6 µs, GB10 일관성) |
| **S12** | **integration-0928 조합 검증** — GB10에서 prepare() 수정 확인, 1x1 회귀, OAI 2×2 rank 2(copy·zero-copy, CUDA gNB, 40–100 MHz, 덜 깨끗한 H) | 게이트 기본값만으로, 실행마다 다른 GPU 프로세스 기록, y=Hx 같은 실행에서 | **완료 2026-09-28** — 20 MHz 2×2 rank 2 copy·zero-copy 2쌍 전부 NACK 0, 148 Mb/s(air), y=Hx 통과, zero-copy가 브로커 p50 70 → 50 µs. **새 결함 2개:** (1) OAI UE가 OCUDU 기본 송신 레벨(12 dB 백오프)에서 64QAM의 69–89%를 NACK(rank 1·2, 1×1 게이트는 ping만 봐서 통과로 보였음) → 로컬 패치 `oai-zmq-rx-gain.patch` + 게이트 기본 UE RX gain −12 dB로 NACK 0. (2) CUDA gNB의 PDSCH 가속이 2포트 셀의 rank 1에서 NACK 45%(rank 2는 5.5%) — 미해결. 40–100 MHz 2×2는 부하 시 실시간 0.24–0.59(UE CPU) |
| **S13** | **CUDA gNB PDSCH 결함(D10), 넓은 대역 2×2 실시간, 50 MHz rank 2 비율** | CPU·GPU 출력 비교로 위치를 좁히고, 수정은 D-계열 규약(패치·lock·음성 대조군)으로, 라이브는 CPU gNB와 짝지어 | **완료 2026-09-28** — **D10:** GPU TB 인코더가 CPU 세그멘터의 filler 0을 "값 없음"으로 보고 자기 값을 써서, filler 0인 다중 CB TB(예: 9,474 B = BG1 9 CB)가 틀린 K로 부호화됐다. 2포트 문제가 아니었다(1포트도 같음, 게이트가 ping만 봐서 숨음). 수정 후 CUDA gNB 2×2 rank 2 148.2 · rank 1 74.1 Mb/s, NACK 0 = CPU gNB, d8 대조 NACK 45%. **100 MHz 2×2는 코어 배치로 안 풀린다**(0.26 → 최대 0.32): lock-step 한 바퀴가 0.8–0.9 ms(중계 + 장치 턴어라운드, 리드 0–1)로 0.5 ms를 넘는 구조 한계. **50 MHz rank 2 21%**는 스케줄러가 아니라 부하 중 UE가 RI=1을 보고한 결과(원인 미확정) |
| **S14** | **50 MHz 2×2 rank 2 비율 원인** — 부하 중 UE가 RI=1을 보고하는 이유를 UE 안에서 찾고 로컬 패치로 고친다 | 같은 조건 A/B(이전 UE ↔ 패치 UE) 2쌍, 20·100 MHz 회귀, 반복 점검(`check-oai-local-patches.sh`)에 결함 재현·수정 probe 추가 | **완료 2026-09-28** — 원인은 CSI 설정도 실시간 여부도 아니라 **OAI UE의 RI 추정이 초기화하지 않은 스택 배열에 누적**하는 것(`nr_csi_rs_ri_estimation`의 `csi_rs_estimated_A_MF`). 채널 추정값은 무부하·부하가 같은데 조건수 계산만 스택 잔여값 때문에 틀어진다. 로컬 패치 `oai-csi-ri-amf-init.patch`(memset 한 줄)로 **rank 2 비율 0.207 → 0.998**(2쌍), NACK 0, 50 MHz DL air 234 → 360 Mb/s. 20 MHz 0.999, 100 MHz 0.998 무회귀 |
| **S15** | **브로커 코드만으로 lock-step 한 바퀴 줄이기** — OAI·OCUDU 드라이버, gNB·UE 설정, 호스트 idle 설정은 그대로 두고 브로커가 소유한 구간만 줄인다 | 홉 추적으로 한 바퀴를 브로커·장치 몫으로 나누고, 바꾼 것마다 같은 바이너리의 노브로 번갈아 A/B, 출력 bit 동일(테스트 + 음성 대조군), 라이브 y=Hx·NACK·1x1·srsUE 회귀 | **완료 2026-09-29** — 100 MHz 2×2 무부하 **0.563–0.571 → 0.673–0.680**, 50 MHz 2×2 무부하 **0.866 → 0.996(실시간)**. 브로커 몫(방향당 p50)은 310 → 약 120 µs. 부하(200M) 중에는 0.25–0.26 그대로다: 그때 한 바퀴는 UE(평균 1.8 ms)·gNB(1.05 ms) 몫이라 브로커로는 못 줄인다. 기본값: 제자리 relay·RX ring 직접 행·다중 행 direct 출력은 코드 기본(`=0`으로 끔), spin 대기는 GB10 프로파일(`broker_env`)로. 브랜치 `broker-round` |
| **S17** | **lock-step 고리의 파이프라이닝 — gNB lower PHY를 ZMQ에서도 스레드 모드로 (로컬 패치)** | 원인·패치·노브별 A/B, 홉 추적과 schedstat 프로브로 메커니즘 확인, 부하·대역폭·1×1 회귀 | **완료 2026-10-02** — 100 MHz 2×2 무부하 0.65 → 0.82–0.85, 20 MHz 2×2 200M 0.89 → 0.98(late 0); 50/100 MHz 부하는 UE 턴어라운드(평균 1.2–1.3 ms)가 상한. 패치 `scripts/native/patches/ocudu-zmq-lower-phy-profile.patch` + 락·빌더 |

## 진행 기록

### S0 — 2026-09-24

- 컨테이너 생성과 검증은 `scripts/cuda/spark/README.md`.
- 레포: `git bundle`로 `/workspace/ocudu-cuda-rebuild`, HEAD `a247906`(= 워크스테이션 `cuda-rebuild`). J·S 트랙 미커밋 파일은 tar로 복사.
- 벤더 소스: `/workspace/ocudu-spark/src/ocudu-cuda` @ `5830c9cb`, `git status --porcelain` 0줄.
- `scripts/cuda/jetson/j1-build-and-validate.sh`를 `CUDA_ARCH`·`PLATFORM_ROOT`·`STAGE` 환경변수로 받게 바꿨다(기본값은 Jetson 값 그대로). Spark 실행: `CUDA_ARCH=121 PLATFORM_ROOT=/workspace/ocudu-spark STAGE=s1 J1_BUILD_JOBS=16`.

### S1 — 2026-09-24

**첫 시도(09:55, `s1-20260924T095544Z`) configure 실패 — 스크립트 쪽 설정 누락.** `CMake Error at lib/phy/upper/channel_coding/CMakeLists.txt:49: GNU does not support +crypto feature.` 원인 사슬:
1. GCC 13.3은 Cortex-X925/A725를 모른다 → OCUDU 기본 `MCPU=native`의 `-mcpu=native`가 crypto 없는 generic ARMv8로 떨어져 `HAVE_ARM_CRYPTO` 실패(PMULL `vmull_p64` 인라인 실패로 직접 확인).
2. 대안 1 `-mcpu=native+crypto`는 GCC가 거부한다(`unknown value 'native+crypto'`).
3. 대안 2 `-march=armv8-a+crypto`는 실제로 컴파일되지만(직접 확인), CMake가 **같은 캐시 변수 `HAVE_PLUS_CRYPTO`로 다시 검사**하므로 첫 실패 결과가 재사용되어 검사 자체가 돌지 않는다 → FATAL. OCUDU CMake의 잠재 결함(WG 추가분이 아니라 `66ee7ce702 phy: fix +crypto extension` 계열 코드).
4. **WG 문서가 이미 답을 적어 두었다**(`docs/phy_cuda_acceleration.md` 41–47, 155–184): DGX Spark 측정은 `-DMCPU=neoverse-v2`로 했다. 이 값이면 `-mcpu=neoverse-v2+crypto` 검사가 통과한다(직접 확인). C·J 트랙은 x86_64 / Orin(A78AE, GCC가 앎)이라 이 옵션이 필요 없었고, 스크립트가 그 configure를 그대로 옮겨 온 것이 누락의 원인이다.
- GCC 14.2(`g++-14`)로도 `-mcpu=native`에 crypto가 켜지지 않음을 확인했다. 진단용으로 컨테이너에 설치했다가 지웠다(툴체인은 이미지와 같은 GCC 13.3).
- 스크립트: `EXTRA_CMAKE_ARGS`(기본 빈 값)를 추가하고 캐시 기록에 `MCPU`를 넣었다. 실패한 빌드 폴더는 캐시된 검사 결과 때문에 지우고 다시 configure 했다.

**재시작(10:27, `s1-20260924T102751Z`):** `EXTRA_CMAKE_ARGS=-DMCPU=neoverse-v2`. CMake 캐시가 WG 문서의 "Expected DGX Spark values"와 **정확히 일치**: `CMAKE_BUILD_TYPE=Release`, `CMAKE_CUDA_ARCHITECTURES=121`, `ENABLE_CUDA=ON`, `MCPU=neoverse-v2`. 빌드 **2분 58초**(gNB 10:27:57 → 테스트 타깃 10:30:55, `-j16`). gNB `--help`에 `GPU acceleration:` 있음. PHY 12개, OFH 16개 등록.

#### S1 결과 — WG 기준 플랫폼에서도 벤더 자체 검증은 **PHY 9/12**

| # | 테스트 | Spark (GB10) | 5090 C0 | Orin J1 |
|---|---|---|---|---|
| 1 | `ofdm_demodulator_cuda_test` | Passed 5.8 s | Passed | Aborted (D6) |
| 2 | `ofdm_prach_demodulator_cuda_test` | Passed 60.9 s | Passed | Aborted (D6) |
| 3–6 | pdxch / LDPC enc / LDPC dec / PRACH det | Passed | Passed | Passed |
| 7 | `pusch_gpu_cpu_comparison_test` | Passed **237.9 s** | Passed 875.5 s | Passed 3450 s(오염) |
| 8 | `pdsch_gpu_e2e_test` | **Failed** — `:746` `compress_device_symbol()` false | Failed, 같은 지점 (D2) | SegFault (D6) |
| 9–10 | pusch_e2e_pipeline / resident_dematch | Passed | Passed | Passed |
| 11–12 | `srs_estimator_gpu_{latency,sensitivity}_baseline_4x4_n4` | **Aborted** — RX 포트 수 assertion | Aborted, 같은 assertion (D1) | Aborted |

**PHY 9/12, `Total Test time 319.55 s`. OFH 16/16(6.3 s). 로그 판정기 `fail`.** WG 문서(`docs/phy_cuda_acceleration.md:526-527`)의 "12/12, 252.53 s"와 다르다. 7번 하나가 237.9 s로 WG 전체 252.53 s와 같은 규모이므로 **시간은 문서와 맞고, 결과만 다르다.** (측정 중 다른 컨테이너의 GPU 사용 여부는 시작 시점에만 확인했다.)

**실패 3건은 5090과 정확히 같은 테스트·같은 지점이다 → C 트랙의 D1·D2는 "디스크리트 GPU 결함"이 아니라 이 커밋의 벤더 결함이다.** 제보서(이슈 #2)는 둘을 디스크리트 하드웨어에 묶어 설명했으므로 정정이 필요하다.

- **SRS (D1).** ctest 등록 명령이 `-R 1 -W 0 -P baseline_4x4_n4 -G host`이다. `-G host`(staged host grid)로 직접 실행하면 GB10에서도 rc 134, `-G visible`로 실행하면 통과한다(`matrix_rel_error=3.98e-7`, 이 1회 설정에서 GPU 0.375×). 즉 **벤더가 등록한 테스트 자체가 벤더가 제거한 경로를 부른다** — 5090 C0의 메커니즘(호스트/pinned 그리드가 snapshot API를 override하지 않음) 그대로이며 하드웨어와 무관하다. (첫 직접 실행에서 ctest 명령의 따옴표를 그대로 넘겨 인자가 무시되고 기본값 `visible`로 돌아 "통과"로 보였다 — 판정에서 제외.)
- **PDSCH (D2).** `pdsch_gpu_e2e_test.cpp:746`, `transmission=0 port=0 symbol=0`에서 `compress_device_symbol()` false — 5090과 같은 줄·같은 인덱스. GB10은 `DirectManagedMemAccessFromHost=0`이므로 `host_reads_device_memory_directly`가 거짓이고, 테스트가 CPU 비교로 managed 그리드를 호스트로 옮긴 뒤에는 `prepare_device_access()`가 정당하게 거부한다. OFH 압축 호출자에 벤더 주석이 약속한 host-copy 폴백이 없다는 C0 분석이 GB10에도 그대로 적용된다.

**언제 깨졌나 (커밋 날짜로 본 정황, 실행으로 확인한 것은 아님):**
- WG 문서의 검증 블록(526–539행) 마지막 수정: `522e867471` **2026-07-29**.
- 문서가 측정 커밋으로 적은 `9fd4047b43`(2026-06-02)은 `5830c9cb`의 조상이 **아니다**(`git merge-base --is-ancestor` rc 1) — 다른 이력선의 커밋이다.
- SRS의 managed 전용 snapshot 요구를 넣은 `35b27de4d6` "phy: keep host access off live CUDA-visible managed grids"는 **2026-08-27**(주석이 GB10 Xid 31을 이유로 든 바로 그 변경), residency 추적을 바꾼 `f4adba91b5`는 08-10, 핀 `5830c9cb`는 08-30.
- → "12/12"는 이 변경들 **이전**에 측정된 결과이고, 이후 재검증되지 않은 것으로 보인다. 확정하려면 `35b27de4d6^`에서 SRS 테스트를 돌려 보면 된다(S1 후속, 미실행).

#### 최신 WG1 코드에서는 해결됐나 — 2026-09-24

**아니다. 최신 `nvcuda_accel_02` HEAD `900d8d0e`(09-23)도 Spark에서 PHY 9/12, 같은 3건, 같은 지점.**

WG1 저장소 전체 브랜치를 받아 확인했다(`git fetch origin '+refs/heads/*:refs/remotes/origin/*'`).

| 브랜치 | 최신 | 내용 | D1·D2·D6 코드 포함 |
|---|---|---|---|
| `nvcuda_accel_02` | `900d8d0e` 09-23 | 핀 이후 4커밋: PRACH VkFFT 커널 디스크 캐시·plan 빌드를 슬롯 경로 밖으로·테스트·포맷 | 예 — 단 해당 파일 변경은 **공백 정렬 2파일뿐**(`phy_acceleration_runtime_options.h`, `srs_estimator_cuda_impl.cpp`) |
| `nvcuda_mr3_grid` | `40f3508f` 09-21 | OCUDU `main`(`f0642cf2`, 07-09) 위에 CUDA-visible 그리드·PRACH 버퍼를 **새로 쓴** 업스트림 MR 조각 | SRS 추정기·OFH 압축·PDSCH 통합·해당 테스트 **없음** |
| `nvcuda_mr6_ldpc_decoder`, `mr4_dlkernels`, `mr_uci_polar` | 08-25–09-23 | 커널 단위 MR 조각 | 없음 |

**실행 확인** (`s1head-20260924T110624Z`, 별도 체크아웃 `src/ocudu-cuda-head` @ `900d8d0e` 깨끗함, 같은 옵션 `MCPU=neoverse-v2`, gNB `ec8ee619…`, 시작 시 다른 GPU 프로세스 0): PHY **9/12** — `pdsch_gpu_e2e_test.cpp:746` 1건, SRS RX 포트 assertion 2건. OFH 16/16.

**부수 관찰 — 시간은 이제 WG 문서와 거의 같다:** `Total Test time 255.44 s`(WG 문서 252.53 s). 핀(319.55 s)과의 차이는 PRACH 복조 테스트가 60.9 s → 0.79 s로 줄어든 것 — 새 커밋의 VkFFT 커널 캐시 효과다. 즉 **WG 문서의 수치는 현재 코드의 속도와는 맞지만 통과 여부와는 맞지 않는다.**

**`mr3_grid`의 새 그리드 구현이 D6에 주는 것 (코드 읽기, 미실행):** `lib/cuda/adt/managed_vector.cpp`는 prefetch·advise가 `cudaErrorNotSupported`이면 `cudaGetLastError()`로 **오류를 지우고** 성공으로 본다 — D6 지점 3(남는 오류)의 패턴은 고쳐졌다. 팩토리는 `prefer_device_residency`의 기본을 `ConcurrentManagedAccess`로 정한다 — Orin(0)에서는 device 선호를 끈다. 그러나 여전히 `NotSupported`만 예외로 보므로 Orin이 반환하는 **`cudaErrorInvalidDevice`(101)는 오류로 남는다.** 이 구현이 `accel_02`에 들어오지 않았으므로 현재 벤더 브랜치의 D6은 그대로다. Orin에서 `mr3_grid` 단위 테스트를 돌려 보면 판정할 수 있다(미실행).

**결론:** WG1 최신 코드 어디에도 D1·D2(Spark·5090 공통)와 D6(Orin)의 수정은 없다. 업스트림 MR 조각은 문제가 되는 계층(SRS·OFH·PDSCH 통합)을 아직 올리지 않은 상태다. 이슈 #2의 C1 패치(`d2579af2`)가 여전히 유일한 수정이다 — 그것이 Spark에서 12/12를 만드는지가 S2다.

### S2 — C1 패치 적용: **PHY 14/14, OFH 16/16** (2026-09-24)

`scripts/cuda/spark/s2-patched-validate.sh`(신규: C1의 build+validate+negative-controls를 Spark용으로 합친 것), 증거 `/workspace/ocudu-spark/results/s2-20260924T112006Z/`. 패치 sha256 `d2579af2` 확인, 별도 체크아웃 `src/ocudu-cuda-c1`(벤더 트리 무수정), `MCPU=neoverse-v2`, 시작 시 다른 GPU 프로세스 0. 빌드 2분 47초.

- **PHY 14/14** (`phy_ctest_exit=0`, 261.25 s) — S1에서 실패한 `pdsch_gpu_e2e_test`, SRS 2건 모두 통과. 패치가 추가한 회귀 2건(`pusch_gpu_cpu_cfo_interpolation_test` 5.2 s, `pusch_gpu_cpu_sync_sinr_test` 4.1 s) 통과. `pusch_gpu_cpu_comparison_test` 240.4 s.
- **OFH 16/16**, 로그 판정기 `phy_log_verdict=pass`, 두 회귀 마커 출력 확인.
- **즉 WG 문서의 "12/12"는 현재 코드에서 C1 패치로만 재현된다.**

**음성 대조군 (결함 하나씩 되돌림 → 반드시 exit≠0, 그리고 되돌린 메커니즘이 로그에 있어야 함):**

| 대조군 | exit | 메커니즘 | 판정 |
|---|---|---|---|
| NC-D1 SRS host-grid staging 제거 | 8 | RX 포트 수 assertion | CONTROL OK |
| NC-D2 OFH host-copy 폴백·hold 해제 제거 | 8 | 아래 — 자동 판정은 `absent`로 찍혔고 수동으로 확인 | **CONTROL OK (수동 판정)** |
| NC-D3 PUSCH 시간보간 전달 제거 | 8 | `CFO regression CRC or payload mismatch` | CONTROL OK |
| NC-D4 동기 경로 SINR에 EVM 대입 | 8 | `Synchronous GPU post-equalization SINR disagrees with CPU` | CONTROL OK |

대조군 뒤 패치 복원 diff 해시 `85701847…`로 원래와 같음(`restored_diff_sha256`).

**NC-D2는 5090과 다른 절반으로 실패한다 — 그리고 그것이 GB10에서 새로 드러난 사실이다.** D2 수정은 두 부분이다: (a) live read가 거부되면 owned snapshot으로 폴백, (b) **어느 경로든 잡은 read hold를 해제**. 패치된 테스트는 `compare_resource_grids` **전에** live 경로 압축을 한 번 부른다(패치 주석: "Its read hold must be released or that host read hangs").
- GB10에서 D2를 되돌리면 그 live 압축은 **성공**한다(GPU 생산자 직후라 pages가 device-resident). 그런데 벤더 `iq_compression_cuda.cpp`는 `release_device_grid_reading()`을 **한 번도 호출하지 않는다**(grep 확인 — 같은 그리드의 다른 소비자 `pdxch_baseband_modulator_cuda.cpp:155`, `pusch_demodulator_gpu_impl.cpp:2013`은 호출한다). 남은 hold 때문에 이어지는 호스트 비교가 `wait_for_device_read_holds_locked`의 5 ms 한도를 넘겨 거부되고, GPU 그리드를 0으로 읽는다: `pdsch_gpu_e2e_test.cpp:741` `nof_mismatches=53424`, `gpu=(0,0)`.
- 판정 스크립트의 메커니즘 문구가 (a)의 증상(`compress_device_symbol` false)만 찾도록 되어 있어 `absent`로 찍혔다. (b)의 증상(`nof_mismatches`)도 인정하도록 고쳤다(재실행은 안 함 — 이 기록이 수동 판정의 근거).
- **라이브 의미:** 벤더 테스트 원본은 비교 **후에** 압축을 부르므로(pages가 이미 host로 옮겨져 live read가 거부됨) (b)에 닿지 않는다. 하지만 실제 OFH 하향 경로에서는 GPU PDSCH 직후 압축이 live read로 **성공**하고, 그 hold가 남으면 다음 슬롯의 CPU 쓰기(PDCCH·SSB)가 막힌다. 즉 **GB10 라이브 하향에서 hold 누수는 실제로 도달 가능한 결함**일 가능성이 높다 — S4에서 C1 패치 유무로 확인할 항목.

### S3 — 스택 빌드 (진행 중, 2026-09-24)

`scripts/cuda/spark/s3-build-stack.sh`(신규): 워크스테이션 lock과 같은 커밋·옵션으로 CPU OCUDU `a1916edc`, srsRAN_4G `eea87b1d`, Open5GS `d9d3abdd`(+ 서브프로젝트 4개를 lock 커밋으로 체크아웃, `--wrap-mode=nodownload`), MongoDB 6.0.29 aarch64(ubuntu2204 빌드, 공식 sha256 사이드카 OK, noble에서 `mongod --version` 실행 확인).

- **첫 시도 실패 — CPU OCUDU configure, 같은 `+crypto` 오류, 다른 원인.** `a1916edc`의 crypto 재시도는 `-march=${MARCH}+crypto`이고 `MARCH` 기본값이 `native`라 GCC가 `-march=native+crypto`를 거부한다(벤더 브랜치는 `66ee7ce702`에서 재시도를 `-mcpu`로 바꿨다). `-DMARCH=armv9-a`를 추가(neoverse-v2와 같은 ISA, `-mcpu=neoverse-v2 -march=armv9-a+crypto` 경고 0 확인). 캐시 때문에 빌드 폴더를 지우고 재시작(`s3-stack-20260924T120638Z`).
- **감시 스크립트 결함:** 모니터의 `pgrep -f <스크립트 이름>`이 ssh로 넘긴 감시 명령 자신과 매칭되어, 스크립트가 끝나도 "실행 중"으로 보였다. 첫 시도의 실패를 30분 늦게 발견한 원인. S1·S2 모니터도 같은 구조였으나 완료 표시로 먼저 끝나 드러나지 않았다. 이후 감시는 tmux 창의 종료 표시(`S3_EXIT=`)로 판정한다.

**라이브 러너 재사용 준비:** `run-ocudu-cuda-direct-zmq.sh`는 `OCUDU_NATIVE_ROOT` 아래 워크스테이션과 같은 폴더 구조(srsUE·5gc·mongod·add_users.py)를 쓰고, 격리는 `unshare --user --net --mount`다. 걸리는 두 가지:
1. CUDA gNB 선택기 `resolve-cuda-gnb.py`가 5090 lock(`sm_120`, `builds/c1-cuda-patched`)으로 검사 → `OCUDU_CUDA_WORKSPACE_LOCK` 환경변수로 다른 lock을 고를 수 있게 했다(미설정 시 기존과 같음). Spark lock `scripts/cuda/cuda-workspace.spark.lock.json`(소스 `src/ocudu-cuda-c1`, 빌드 `builds/s2-cuda-patched-sm121`, arch 121).
2. `scripts/native/env.sh`가 x86 sysroot·gnutls·bison 폴더의 존재를 검사 → Spark는 의존성이 시스템 apt에 있으므로 빈 폴더(README 포함)로 두었다.

### S3 — 라이브 직결 ZMQ: **`disabled`·`all` 모두 attach/PDU/ping 통과** (2026-09-24)

실행기 `scripts/cuda/spark/live-direct-zmq.sh <stage> [traffic_s]` → 워크스테이션 러너 `run-ocudu-cuda-direct-zmq.sh`(무수정) + Spark lock. gNB = S2의 패치 빌드(`builds/s2-cuda-patched-sm121`, C1 `d2579af2`).

**첫 통과까지 막힌 것 (순서대로):**
1. 선택기가 lock 대조에서 거부 — 패치 diff의 `index` 해시가 10자리(WG1 브랜치 전부를 받은 복제본이라 git 자동 약어가 길어짐)라 7자리 패치 파일과 바이트 불일치, 그리고 gNB 빌드 해시도 10자리(`5830c9cb78`)라 버전 정규식 불일치. `resolve-cuda-gnb.py`가 `core.abbrev=7`로 비교하고 해시 접두어를 받도록 고쳤다(워크스테이션 트리에서도 패치 비교 통과 확인).
2. `unshare: write failed /proc/self/uid_map` — 이 Ubuntu 24.04 호스트는 비특권 사용자의 user namespace를 막는다. 워크스테이션은 `docker exec`(root)로 돌렸다. → 컨테이너 root(sudo)로 실행.
3. `add_users.py`에 `python3-click` 없음 → 설치, `ocudu/Dockerfile`에 추가.
4. srsUE `RF device 'zmq' not found` — `srsue` 타깃만 빌드해 ZMQ 플러그인 `libsrsran_rf_zmq.so`가 없었다 → srsRAN_4G 전체 빌드(`s3-build-stack.sh` 수정).
5. srsUE가 `Waiting PHY to initialize`에서 60 s 안에 못 나옴 — FFTW wisdom이 없으면 GB10에서 **첫 PHY 초기화 260 s**(wisdom 있으면 2 s, 단독 실행으로 측정). 그런데 wisdom을 만들어도 러너 안에서는 안 읽혔다: `sudo -E`로 `HOME=/home/dev`가 유지되는데, user namespace 안의 root에게 dev(미매핑 uid) 소유의 `750` 홈은 **통과 불가**라 `fopen(r+)`가 조용히 실패한다(`nsenter`로 네임스페이스 안에서 `statx: Permission denied` 확인). → `HOME=/root`로 실행(`/root/.srsran_fftwisdom` 사용).
6. (운영 실수) 정리 명령의 `pkill -f <러너 이름>`이 그 명령을 실은 ssh 세션 자신과 매칭되어 세션이 끊겼다 — 모니터의 `pgrep -f` 문제와 같은 부류. 이후 정리는 `pkill -x <정확한 프로세스 이름>`만 쓴다.

**결과 (다른 GPU 프로세스 0에서 시작):**

| 런 | stage | 트래픽 | 결과 | keepalive | PUSCH | BLER | SINR p50 |
|---|---|---|---|---:|---:|---:|---:|
| `direct-zmq-20260924T122719Z` | disabled | 20 s | passed | 100 | 429 | 0.47% | 5.2 dB |
| `direct-zmq-20260924T122842Z` | **all** | 60 s | **passed** | 299 | 1238 | **1.29%** | 4.8 dB |
| `direct-zmq-20260924T123020Z` | disabled | 60 s | passed | 299 | 1229 | 0.49% | 5.2 dB |

`all` 런의 백엔드 판정(`verify-stage-backends.py`): **selected 7**(low-phy-rx, low-phy-tx, prach, PDSCH, PRACH, PUSCH, SRS), degraded 0, host_fallback 0, verdict OK. 매니페스트 4종 모두 `backend=CUDA`, UL/DL 그리드 `requested=managed`, `PDSCH CUDA synchronous executor selected for sequential PHY.`(= D5 수정이 라이브에서 동작; NC-D5 자체는 아직 안 돌림). gNB 기동 6 s(5090은 20–24 s).

**예상과 달랐던 것 1 — lower-PHY TX direct 경로가 GB10에서 탄다.** 로그에 `Lower-PHY TX GPU path selected: direct CUDA-visible downlink resource-grid reader.`와 `... host resource-grid staging fallback.`이 **둘 다** 찍혔다(각각 one-shot 래치라 최소 한 번씩). 5090에서는 direct가 한 번도 없었다. C4 추적의 결론("`fully_coherent` 불만족이면 direct는 GH200급에서만")은 **GB10에는 맞지 않는다** — `pages_device_resident`가 참으로 남는 슬롯(GPU PDSCH 뒤 호스트 쓰기가 없는 슬롯으로 추정)에서는 GB10도 direct로 읽는다. 비율은 로그로 알 수 없다(래치). 업스트림 초안의 해당 서술을 고쳤다.

**예상과 달랐던 것 2 — 이슈 6이 GB10에서도 재현된다.** 16QAM/TBS 528 할당의 CRC 실패율:

| | disabled 60 s | all 60 s |
|---|---|---|
| 16QAM tbs=528 | 6 / 482 = **1.24%** | 16 / 218 = **7.34%** |
| 그 외 할당 | 0 | 0 |

링크 적응이 QPSK로 물러나는 모양(16QAM tbs 528이 482 → 218건, QPSK tbs 528이 399건 등장)까지 5090 직결 관찰(host 0.6–1.6% → 가속 6–10%)과 같다. **이슈 6은 디스크리트 고유가 아니라 PUSCH 가속 경로의 성질**로 보인다. 단 Spark는 한 쌍(각 1회)뿐이다 — 5090처럼 반복 필요.

#### 반복 런 — 2026-09-24 (각 60 s, 시작 시 다른 GPU 프로세스 0)

| 런 | stage | 결과 | PUSCH | BLER | 16QAM tbs=528 CRC 실패 |
|---|---|---|---:|---:|---|
| `…122720Z` | disabled (20 s) | passed | 429 | 0.47% | 2 / 159 = 1.26% |
| `…123020Z` | disabled | passed | 1229 | 0.49% | 6 / 482 = 1.24% |
| `…123910Z` | disabled | passed | 1226 | 0.33% | 4 / 446 = 0.90% |
| `…122842Z` | all | passed | 1238 | 1.29% | 16 / 218 = 7.34% |
| `…124024Z` | all | passed | 1245 | 1.77% | 21 / 201 = 10.45% |
| `…124143Z` | all | passed | 1240 | 1.53% | 19 / 208 = 9.13% |

**S3 CUDA gNB 게이트: `disabled` 3/3, `all` 3/3.** 이슈 6은 세 번 모두 재현: 16QAM tbs=528 실패율 disabled 0.9–1.3% vs all 7.3–10.5%, 16QAM 표본이 ~450 → ~210으로 줄어드는 링크 적응 후퇴도 매번 같다. 5090 직결(0.6–1.6% → 6–10%)과 같은 범위.

### NC-D5 — 2026-09-24

`scripts/cuda/spark/s3-nc-d5.sh`(신규): 5090의 `d5-negative-control.sh`와 같은 편집(대입만 끄고 선언은 남김)으로 gNB를 재빌드하고, 통과한 `all` 런의 렌더 설정(`direct-zmq-20260924T124143Z/configs/gnb.yaml`, `pdsch_acceleration_mode: enabled` 확인)으로 gNB를 **직접** 기동한다. 러너는 쓸 수 없다 — 선택기가 lock과 다른 소스를 거부하는데, 대조군은 일부러 그렇게 만들기 때문이다(5090 NC-D5가 한 번 무효였던 이유와 같다). D5는 상위 PHY 구성 중 발현하므로 코어·UE가 필요 없다.

결과: **exit 134, 콘솔에 `OCUDU FATAL ERROR: Accelerated PDSCH block processor requested but no block-processor capable PDSCH configuration is active` → CONTROL OK.** 복원 후 diff가 lock 패치와 일치(`restored_diff_matches_lock=yes`), gNB 재빌드. 증거 `results/s3-nc-d5-20260924T130840Z/`. → **Spark에서 NC-D1–D5 전부 완료.**

### 이슈 6 위치 좁히기 — 2026-09-24

**라이브 스위치 실험** (`pusch` stage, 각 60 s, `live-direct-zmq.sh`가 진단용 `OCUDU_*` 변수를 넘기고 콘솔 첫 줄에 기록; 로그로 적용 확인):

| arm | 설정 | 16QAM tbs=528 CRC 실패 | BLER |
|---|---|---|---|
| E0 | 기본 | 16/193 = 8.29% | 1.29% |
| E1 | `OCUDU_NOISE_MODE=cv`(이전 잡음 추정) | 20/22 = 90.9% — **링크 붕괴(result=failed)** | 86% |
| E2 | `OCUDU_LDPC_BOXPLUS_ITERS=12` | 16/215 = 7.44% | 1.29% |
| E3 | `OCUDU_LDPC_BOXPLUS=0`(min-sum) | 21/180 = 11.67% | 1.69% |

- 디코더 아님: 반복 2배(E2)로 변화 없음, min-sum(E3)은 예상대로 약간 나쁨. 우리 할당은 코드블록 1개라 `auto`는 이미 boxplus(`nof_cbs < 192`).
- 반복 한도(6)·보간(`interpolate`, CPU·GPU 기본 동일)도 CPU와 같다.
- 이전 잡음 추정 모드는 이 링크에서 동작하지 않는다(해결책 아님, 민감도만 확인).

**오프라인 재현 — 같은 IQ를 CPU·GPU에 (`pusch_gpu_cpu_comparison_test -L -R 400 -P 15 -N 1 -S 3,4,5,6,7`, `-L` = 16QAM MCS 10·interpolate·MMSE·CFO 400 Hz):**

| SINR | CPU BLER | GPU BLER | EVM CPU → GPU |
|---|---|---|---|
| 3 dB | 100% | 100% | 64.0 → 65.4% |
| 4 dB | 100% | 100% | 58.3 → 59.6% |
| **5 dB** | **74.0%** | **88.5%** | 53.9 → 55.0% |
| **6 dB** | **0.2%** | **1.2%** | 50.1 → 50.9% |
| 7 dB | 0% | 0% | 47.0 → 47.6% |

증거 `results/issue6-offline-L.log`.

**판정:** 이슈 6은 라이브 링크·스케줄러가 아니라 **GPU PUSCH 경로의 디코딩 이전 단계**에 있다. GPU의 등화 후 EVM이 모든 SINR에서 CPU보다 0.6–1.5%p 높고, 그 차이는 SINR이 낮을수록 크다 — 추정 잡음에 비례하는 모양으로, **GPU 채널 추정의 잡음 억제(평활)가 CPU보다 약한 것**과 들어맞는다(추정, 커널 수준 미확인). 16QAM 폭포 구간에서 약 0.2–0.3 dB 손실에 해당하고, 가파른 폭포 때문에 6 dB에서 BLER이 6배가 된다 — 라이브의 1% 대 7–10%와 같은 크기.

**벤더 테스트가 못 보는 이유:** 기본 구성이 QPSK(MCS 0)·ZF·반복 10이라 폭포에 닿지 않고, 5 dB에서 BLER +14.5%p 차이도 `[WARN]`으로만 찍히고 통과한다.

다음(미실행): 채널 추정 단계 분리 — GPU 추정 채널 계수를 CPU 추정기와 같은 입력으로 비교, 또는 GPU 채널 추정만 CPU 결과로 대체하는 실험.

### 이슈 6 — 정정: 합산 지표는 할당 구성에 교란되어 있었다 (2026-09-24)

**앞 절들의 "16QAM tbs=528 실패율 1% vs 7–10%"는 교란된 지표다.** 그 수치는 모든 rv와 여러 PRB 할당을 합친 것이고, 가속 여부에 따라 **할당 구성 자체가 달라진다.** 가속 끔에서는 링크 적응이 더 강한 `16QAM [0,19)`(실패 0)를 많이 써서 비율이 희석되고, 가속 켬에서는 그 대신 QPSK로 가서 16QAM 표본이 약한 `[0,17)`에 몰린다.

**같은 할당·첫 전송(rv=0)만 비교** (라이브 5런):

| 할당 | 가속 끔 (2런) | 가속 켬 (3런) |
|---|---|---|
| 16QAM `[0,17)` tbs 528 CRC 실패 | 10 / 429 = 2.3% | 21 / 564 = 3.7% — 차이 작고 표본 대비 유의하지 않음 |

**같은 할당의 보고 SINR** — GPU가 일관되게 낮다:

| 할당 | 끔 평균 | 켬 평균 | 차 |
|---|---|---|---|
| 16QAM `[0,17)` | 4.91 | 4.83 | −0.08 dB |
| QPSK `[0,24)` | 4.59 | 4.50 | −0.09 dB |
| QPSK `[1,6)` (5 PRB) | 6.31 | 5.82 | **−0.49 dB** |
| QPSK `[0,1)` (1 PRB) | p50 8.5, sd 1.5 | p50 10.0, sd 2.8 | 분산 2배 |

**수정된 판정:**
1. 라이브에서 CPU·GPU의 차이는 첫 전송 CRC가 아니라 **보고 SINR**(좁은 할당일수록 큼)과 그로 인한 **링크 적응 경로의 차이**다. GPU FD 평활은 `cfg.nof_prb > 1`일 때만 돈다(`pusch_e2e_api.cu` Phase 1b) — 1 PRB 할당에서 GPU 추정이 평활되지 않는 것과 1 PRB SINR 분산 2배가 들어맞는다(인과 미확인).
2. 오프라인의 GPU 열세(`-L`, 5 dB BLER +14.5%p, EVM +0.6–1.5%p)는 **CFO 400 Hz가 있을 때만** 나타난다. 진단 복사본(`src/ocudu-cuda-diag`, `OCUDU_DIAG_CFO_HZ=0`)으로 CFO를 0으로 두면 CPU·GPU EVM이 **동일**(38.4/38.4%, 35.3/35.3%)하고 BLER도 같다. 둘 다 `compensate_cfo=false`(`td=interpolate`)인 조건에서 GPU가 보상 없는 위상 회전을 덜 따라간다 — 실재하는 차이이지만 CFO≈0인 ZMQ 라이브와는 **다른 현상**이다.
3. 같은 진단으로 배제한 것: FD 평활 커널의 in-place 레이스(스냅샷에서 읽게 바꿔도 변화 없음), FD 평활 자체(양쪽 끄면 차이가 오히려 커짐 — CFO 400 Hz 조건), LDPC 반복·알고리즘(라이브 E2/E3).
4. 5090 원 제보서의 같은 관찰도 같은 교란을 담고 있을 가능성이 높다(재분석 안 함).

진단 복사본 편집(환경변수 게이트, C1 패치 커밋 위): GPU FD 평활 `off|oop`, CPU FD `none`, 테스트 CFO 덮어쓰기. 결과 `results/issue6-diag-20260924T133312Z/`.

### "12/12"는 `35b27de4d6`에서 깨졌다 — 확정 (2026-09-24)

같은 Spark·같은 옵션(`MCPU=neoverse-v2`, 패치 없음)으로 두 커밋에서 벤더 검증을 돌렸다(`j1-build-and-validate.sh`, `SRC_DIR`/`EXPECT_COMMIT` 지정, 별도 체크아웃 `src/ocudu-cuda-bisect`):

| 커밋 | PHY | 실패 | 시간 | 증거 |
|---|---|---|---|---|
| `ca91f27e73` (2026-08-16, `35b27de4d6`의 부모, 핀의 조상) | **12/12**, OFH 16/16, 로그 판정 pass | — | 249.57 s | `results/s1pre35b-*` |
| `35b27de4d6` "phy: keep host access off live CUDA-visible managed grids" | **9/12** | `pdsch_gpu_e2e_test`, SRS 2건 (S1과 같은 3건) | 256.91 s | `results/s1at35b-*` |

WG 문서의 "12/12, 252.53 s"는 이 커밋 **이전** 상태와 정확히 맞는다(12/12, 249.57 s). 이 한 커밋(13파일 +961/−164: SRS 추정기에 owned-snapshot 요구, managed 그리드 host 접근 규칙, `owned_grid_snapshot_copy.cu` 등)이 D1(SRS host-grid 경로)과 D2(OFH 압축 폴백)를 함께 들여왔고, 이후 검증 블록이 갱신되지 않았다.

### S4 — 단계별 사다리 (2026-09-24, 각 60 s, 시작 시 다른 GPU 프로세스 0)

`scripts/cuda/spark/s4-ladder.sh`(신규). 5090 C4와 같은 누적 순서.

| stage | 결과 | gNB 기동 | PUSCH | BLER | SINR p50 | selected | degraded | TX 경로 |
|---|---|---|---:|---:|---:|---|---|---|
| low-phy-rx | passed | 1 s | 1224 | 0.25% | 5.15 | low-phy-rx | — | — |
| low-phy-tx | passed | 1 s | 1224 | 0.25% | 5.2 | low-phy-rx | **low-phy-tx** | staging |
| pusch | passed | 5 s | 1241 | 1.45% | 4.8 | +PUSCH | **low-phy-tx** | staging |
| pdsch | passed | 5 s | 1238 | 1.29% | 4.8 | +low-phy-tx, PDSCH | — | **direct**, staging |
| prach | passed | 5 s | 1240 | 1.45% | 4.8 | +prach, PRACH | — | **direct**, staging |

(`all` = 위 + SRS, S3에서 3/3.) **host fallback은 전 단계 0.**

**lower-PHY TX direct 경로는 GPU PDSCH가 켜진 단계부터만 나타난다.** `low-phy-tx`·`pusch` 단계에서는 DL 그리드를 CPU만 쓰므로 device-resident가 될 일이 없어 staging뿐(DEGRADED), `pdsch`부터 GPU가 그리드를 쓴 슬롯에서 direct가 잡힌다. 5090에서는 `pdsch`·`all`에서도 direct가 없었다(C4) — GB10에서 차이가 나는 조건이 "GPU 생산자 직후, 호스트 쓰기 전"이라는 해석과 맞는다. 비율은 one-shot 래치라 알 수 없다.

SINR p50이 5.15–5.2 → 4.8 dB, BLER 0.25 → 1.3–1.5%로 바뀌는 지점은 **PUSCH 가속을 켜는 단계**다 — 이슈 6 정정(보고 SINR 차이 → 링크 적응 경로 차이)과 일치.

### S6 — 벤더 스윕으로 성능 측정 (2026-09-24)

도구: 벤더 `scripts/cuda_accel/run_type1_dmrs_ul_dl_gpu_cpu_sweeps.sh`를 **WG 문서의 옵션 그대로**(`--quick --mcs 20 --pusch-snr-step 1.0 --pusch-frames 100 --rx-device-grid managed --resource-grid-memory managed --device-grid-memory managed`). 빌드는 S2의 C1 패치 빌드(`builds/s2-cuda-patched-sm121`, `MCPU=neoverse-v2`). 호스트 CPU governor는 20코어 모두 이미 `performance`(문서의 튜닝 스크립트가 하는 일). 시작 시 다른 GPU 프로세스 0. 성능 라벨: DGX Spark GB10 / 드라이버 580.178.04 / CUDA 13.0.88 / 벤치마크 프로그램(라이브 아님) / C1 패치 빌드 / 2026-09-24.

**100 MHz, 273 PRB, 4 layer** (`s6-100mhz-4l-20260924T141353Z`, 지연 반복 10 — 문서와 같음):

| | 문서(GB10, `9fd4047b43`) | 측정 |
|---|---|---|
| PUSCH 감도 10% BLER, 8 RX | CPU 13.1 / GPU 13.0 dB (−0.1) | CPU 13.2 / GPU 13.0 dB (−0.2) |
| PUSCH 지연 평균, 8 RX | 13122.3 / 618.9 µs = 21.20× | 13366.5 / **333.9** µs = **40.03×**, mismatch 0 |
| PDSCH 지연 p50, 4 포트 | 625.0 / 186.0 µs = 3.36× | 581.8 / 171.2 µs = 3.40× (GPU grid `direct`) |

**20 MHz급 1 layer** (`s6-20mhz-1l-20260924T141450Z`, 지연 반복 100; 스크립트는 30 kHz를 가정해 106 PRB를 "40MHz"로 표기 — 라이브 설정은 15 kHz·106 PRB):

| | 문서 | 51 PRB | 106 PRB |
|---|---|---|---|
| PUSCH 감도 CPU/GPU | — | 12.2 / 12.2 dB | 12.1 / 12.1 dB |
| PUSCH 지연 CPU/GPU | 225.8 / 262.3 µs = 0.86× | 401.1 / 172.9 µs = **2.32×** | 495.4 / 166.8 µs = 2.97× |
| PDSCH 지연 p50 CPU/GPU | 29.4 / 79.9 µs = 0.37× | 27.2 / 82.9 µs = 0.33× | 55.1 / 73.8 µs = 0.75× |

**해석:**
- 정확도: 모든 구성에서 GPU 감도가 CPU와 같거나 0.2 dB 이내로 낫다.
- 100 MHz 4L: 문서 수치를 재현하고, PUSCH GPU 지연은 문서의 절반. 문서 측정 커밋(`9fd4047b43`)이 핀의 조상이 아니라 다른 이력선이므로 이후 최적화 차이로 추정(미확인). 반복 10회라 분포는 없다.
- 20 MHz 1L PDSCH: 문서와 같게 GPU가 느리다(0.33×) — 작은 부하에서 GPU 실행 비용이 상쇄되지 않는다는 문서 설명 그대로.
- 20 MHz 1L PUSCH: **문서와 반대로 GPU가 2.3× 빠르다** — *09-25 정정: CPU 미고정의 착시, 빅 코어 고정 시 1.1× (아래 "S6 정정")*. 차이는 CPU 쪽(문서 225.8 µs vs 측정 401.1 µs)이다. 100 MHz CPU는 문서와 2% 이내이므로, 문서의 20 MHz 수치가 다른 조건(MCS·반복·구성)에서 나왔을 가능성이 높다 — 문서에 명령이 없어 확인 불가. **원인 미상으로 기록.**
- 이것은 벤치마크 프로그램의 처리 지연이다. 라이브 gNB 측정은 아래 "라이브 PUSCH 처리 시간"(09-25)에 있고, 20 MHz 1L 라이브에서는 GPU가 더 느리다.

### S3 마무리 — CPU gNB 라이브 기준선 (2026-09-25)

러너 확장: `run-ocudu-cuda-direct-zmq.sh`에 `cpu` stage를 추가했다. `cpu` stage는 가속 블록 없이 렌더링하고(렌더러에 stage를 비워 주면 평이한 렌더와 바이트 단위로 같다), gNB는 lock의 새 `cpu_baseline` 항목(`src/ocudu` @ `a1916edc`, `builds/ocudu-zmq-release`)에서 고른다. 이 gNB는 `resolve-cuda-gnb.py --cpu-baseline`으로 감사한다: 커밋, 트리 무수정, `ENABLE_CUDA`≠ON, ZMQ, 버전 문자열. 백엔드 판정은 `not_applicable`이다. 5090 lock에는 `cpu_baseline`이 없으므로 거기서 `cpu`를 고르면 크게 실패한다.

결과: CPU gNB 직결 ZMQ **4/4 통과**(단독 1회 + S5 배치 3회). attach/PDU/ping, keepalive 299/299, gNB 기동 1 s.

### S5 — CPU gNB 대비 BLER/SINR (2026-09-25, `results/s5-arms-20260925T033304Z`)

`scripts/cuda/spark/s5-arms.sh 3 60`: `cpu` → `disabled` → `all` 순서를 3회 번갈아 돌렸다(각 60 s, 매 런 시작 시 다른 GPU 프로세스 0). 9/9 통과. 판정기는 `s5-compare-arms.py`다. 이슈 6의 교훈대로 첫 전송을 할당(mod, PRB, TBS)이 같은 것끼리 비교하고, 양쪽 모두 30회 이상 나온 할당만 쓴다. 가중치는 두 조건 중 작은 쪽 횟수다.

**정정(09-25 같은 날):** 이 설정에서는 **재전송도 rv=0으로 나간다**(스케줄러 로그 `newtx=false rv=0`). 그래서 처음 판정기의 "rv=0 = 첫 전송"은 틀렸다. 판정기는 이제 같은 슬롯·HARQ의 스케줄러 결정 줄에서 `newtx=true`를 읽는다(매칭 누락 0). 다시 판정한 결과는 아래 수치이고, 결론은 같다(`disabled` +0.07%p/−0.03 dB, `all` +0.20%p/−0.16 dB, PASS). 증거는 `compare-newtx.txt`에 있다.

| 조건 | 전송 | CRC 실패 | 합산 BLER | 평균 SINR | PHY `t` p50/p99/max |
|---|---|---|---|---|---|
| `cpu` (a1916edc) | 3672 | 9 | 0.25% | 5.83 dB | 91 / 295 / 573 µs |
| `disabled` (CUDA gNB, 가속 끔) | 3676 | 11 | 0.30% | 5.81 dB | 92 / 299 / 592 µs |
| `all` | 3721 | 56 | **1.51%** | 5.49 dB | 195 / 503 / 1029 µs |

할당 일치 판정(기준 BLER ≤1%p, SINR ≤0.5 dB):
- `disabled` vs `cpu`: ΔBLER +0.07%p, ΔSINR −0.03 dB, 일치 신규 전송 3363 → **PASS**. 벤더 트리의 CPU 경로는 CPU OCUDU와 구별되지 않는다.
- `all` vs `cpu`: ΔBLER +0.20%p, ΔSINR −0.16 dB, 일치 신규 전송 761 → **PASS**. 16QAM `[0,17)`은 1.40% vs 1.69%다. 할당별로 보면 QPSK `[1,6)`의 ΔSINR이 −0.53 dB로 기준을 넘는다(09-24에 본 좁은 할당의 SINR 저하와 같다).

**합산 BLER이 6배인 이유**: `all`의 CRC 실패 56건 중 47건이 CPU가 한 번도 고르지 않은 할당에서 났다. 16QAM `[0,15)` tbs 528은 **신규 전송 13건이 13건 모두 실패**했다(PHY 로그의 47건은 신규 13건 + 같은 TB의 재전송 34건). 같은 TBS를 더 좁은 PRB에 실은, 부호율이 높은 할당이다. 인과는 아래 "D8"에서 확인했다. GPU가 1 PRB PUSCH의 SINR을 +2.4 dB 높게 보고하고, 스케줄러가 그 값으로 다음 TB의 MCS를 올린다.

→ S5 기준표로는 통과다. 하지만 "가속을 켜도 라이브 BLER이 같다"고 쓰면 틀린다. **합산 BLER이 0.25% → 1.5%로 오른다**고 써야 한다.

### 라이브 PUSCH 처리 시간 — `OCUDU_PUSCH_ACCELERATION_TIMING=1` (2026-09-25)

`all` 3런(각 60 s, 3/3 통과, BLER 1.37–1.53%로 타이밍을 켜지 않은 런과 같다). `PUSCH phase` 2745줄(GPU resident 경로), PHY `t` 3718줄. 20 MHz급 106 PRB, 1 layer, 15 kHz(슬롯 1 ms).

| 구간 | p50 | p99 | max |
|---|---|---|---|
| demod (호출) | 67.6 | 110.9 | 237.5 µs |
| decode (호출) | 161.4 | 423.4 | 926.1 µs |
| └ LDPC (CUDA event) | 113.8 | 336.0 | 866.4 µs |
| └ 완료 대기 | 92.9 | 312.8 | 487.8 µs |
| rate dematch / 설정 / D2H | 13.4 / 9.2 / 3.8 | 20.8 / 15.8 / 4.3 | µs |
| e2e (파이프라인) | 145.8 | 372.2 | 902.2 µs |
| **wall** | **245.3** | **519.3** | **1058.7 µs** |

- 비교: CPU gNB의 PHY `t`는 p50 91 / p99 295 µs다. **라이브 20 MHz 1L에서 GPU PUSCH는 CPU보다 2.2배 느리다.** 벤치마크(S6 20 MHz 1L, GPU 2.3× 빠름)와 방향이 반대다. 라이브 TB는 대부분 1 CB(tbs 11–528)라서, LDPC 커널 실행과 완료 대기가 지배한다.
- 실시간 여유: 1 ms를 넘은 것은 3718건 중 1건(1058.7 µs), 500 µs 초과는 77건이다. gNB 로그에 late/overflow/underflow는 0건이다(S5 `cpu`·`all` 런도 0건). gNB가 기한 초과를 보고하지 않았다. 단 **ZMQ 라디오는 실시간이 아니어서**(샘플 흐름이 처리 속도에 맞춰 늦춰진다) 이것으로 실시간 여유를 판정할 수는 없다(09-25 정정). 다만 p99 기준 처리 시간이 CPU의 1.8배다.
- 성능 라벨: DGX Spark GB10 / 580.178.04 / CUDA 13.0.88 / 라이브 직결 ZMQ / C1 패치 빌드 / 2026-09-25.

### D8 — GPU PUSCH가 1 PRB 할당의 SINR을 ~2.4 dB 높게 보고한다 (2026-09-25)

**라이브 인과** (`s5-la-trace.py`, S5 런 9개): 스케줄러가 새 528비트 TB의 폭(=MCS)을 정하는 시점에 가장 최근에 보고된 PUSCH SINR을 짝지었다. 결정 줄은 k2=4 슬롯 앞서 찍히므로, PHY 로그상의 직전 PUSCH가 아니라 결정 시점 기준으로 맞춰야 한다.

| 조건 | 선택 폭 | 신규 TB | 실패 | 결정 시점 최근 SINR (평균) | 그 SINR이 1 PRB PUSCH에서 온 비율 |
|---|---|---|---|---|---|
| `cpu` | 17 PRB | 641 | 9 | 8.42 dB | 611/641 |
| `all` | 17 PRB | 532 | 9 | 9.99 dB | 532/532 |
| `all` | **15 PRB** | 13 | **13** | **10.00 dB** | 13/13 |

1 PRB PUSCH의 보고 SINR은 결정론적 ZMQ 채널에서 두 값으로 양자화된다. CPU는 8.5 / 5.2 dB, GPU는 **10.0** / 4.3 dB다. 스케줄러 메트릭(`OCUDU_DIRECT_SCHED_METRICS_MS=100`, 러너에 새로 추가한 진단 노브)으로 보면, `all`의 UL OLLA는 평균 −0.54(CPU +0.23)로 끌려 내려가 있다. 그래서 대부분의 TB는 오히려 보수적인 MCS 8–10(QPSK 24/27 PRB)으로 나가고, 1 PRB 보고가 10 dB일 때만 MCS 12–14로 튀어 실패한다.

**오프라인 재현** (`pusch_e2e_pipeline_test`, 1 layer, MCS 4, 50회, CPU vs GPU CSI 비교표):

| PRB | 입력 5 dB | 입력 10 dB | 입력 20 dB |
|---|---|---|---|
| 1 | **+2.23** | **+2.48** | **+2.35** |
| 2 | −0.09 | −0.12 | −0.07 |
| 6 | −0.01 | −0.02 | −0.02 |
| 24 | −0.00 | −0.01 | −0.01 |

(GPU − CPU, dB. 복호는 모두 일치한다.)

**메커니즘:** `pusch_e2e_api.cu` Phase 1b의 FD 평활은 `if (cfg.nof_prb > 1)`일 때만 돈다. 그런데 바로 다음 Phase 2의 교차 검증 잡음 추정은 평활된 추정치를 전제로 맞춰져 있다. 진단 복사본으로 확인했다.
- `OCUDU_DIAG_GPU_FD=off`(모든 폭에서 평활 끔): **모든 폭이 +2.3~2.5 dB**가 된다.
- `OCUDU_DIAG_GPU_FD=all`(1 PRB도 평활, 진단 스위치 신설): 1 PRB가 **−0.11/−0.19/−0.18 dB**가 되고 EVM도 CPU와 같아진다.

평활 커널은 범위를 벗어나는 탭을 건너뛰고 다시 정규화하므로 1 PRB에서도 안전하다.

**수정은 두 단계였다.** 패치는 `scripts/cuda/patches/s-d8-c1-gpu-1prb-sinr.patch`(핀 대비 C1 + D8을 합친 diff)이고, lock은 `cuda-workspace.spark-d8.lock.json`이다. 검증 도구는 두 가지다.
- `s-d8-validate.sh`: PHY 14 + OFH 회귀, 1/2 PRB SINR 비교, S2 빌드를 음성 대조군으로.
- 라이브: `SPARK_LOCK=… S5_ARMS="cpu all" s5-arms.sh 3 60`.

1. **v1** (sha `519935b6`): SISO 경로의 `nof_prb > 1` 조건만 없앴다.
   - 오프라인 AWGN: 1 PRB가 −0.15~−0.19 dB로 맞았다.
   - 라이브(`s5-arms-20260925T042052Z`): `all`의 CRC 실패가 **0/3664**가 됐지만, 이번에는 1 PRB SINR이 CPU보다 **−2.2~−2.7 dB 낮게** 나왔다. 링크 적응이 보수적으로 가서(19/24/27 PRB) 가중 ΔSINR −0.89 dB로 **FAIL**이다.
   - 원인은 오프라인 `--ta-offset 1`(최대 1 µs 무작위 TA)로 재현했다. v1은 1 PRB −2.56 dB, 6 PRB −0.56 dB로, 라이브 값(1 PRB −2.2~−2.7, 5 PRB −0.5)과 같다. CFO(`--cfo-std 600`)는 영향이 없다.
   - 메커니즘: TA는 주파수 방향의 선형 위상 기울기다. GPU 평활은 회전된 탭을 그대로 평균해서 추정치를 줄이고, 그 잔차를 잡음으로 센다. CPU(`port_channel_estimator_helpers.cpp` `apply_fd_smoothing`)는 크기와 펼친 위상으로 외삽한 가상 파일럿을 쓰고 필터를 PRB 수에 맞게 만들기 때문에 이 문제가 없다.
2. **v2** (sha `2c0962bc`, 현재 lock): 평활 커널이 탭 창 안에서 파일럿당 위상 회전(`Σ h[i+1]·conj(h[i])`)을 추정하고, 출력 위치 기준으로 되돌린 뒤 필터링한다. v1의 조건 제거도 포함한다. 결과는 아래와 같다.

| 오프라인 GPU−CPU (dB, 입력 10 dB) | 1 PRB | 2 PRB | 6 PRB | 24 PRB |
|---|---|---|---|---|
| C1 | +2.55 | −0.07 | −0.02 | −0.01 |
| C1 + D8 v2, TA 0 | −0.08 | −0.07 | −0.02 | −0.01 |
| C1 + D8 v2, TA ≤1 µs | +0.62 | +1.81 | +0.06 | +0.09 |
| C1 + D8 v2, CFO σ 600 Hz | −0.16 | −0.07 | −0.02 | −0.01 |

TA가 있을 때의 +값은 CPU 쪽이 떨어진 결과다. TA ≤2 µs에서 CPU는 1 PRB 7.3 dB, GPU는 10.8 dB이고 참값은 10 dB다. EVM도 GPU가 같거나 낮다. 즉 "CPU와 같은가" 기준으로는 넘치지만 참값에는 GPU가 더 가깝다. 복호는 모든 조건에서 일치한다.

v2 검증(`d8-*` 두 번째 런): PHY 14/14, OFH 16/16, 로그 판정 pass, SINR 비교 PASS(1 PRB −0.09~−0.17, 2 PRB −0.00~−0.10). 음성 대조군(C1 빌드)은 1 PRB +2.2~+2.6으로 재현된다.

**라이브 v2** (`s5-arms-20260925T044210Z`, `cpu`/`all` 교차 3회, 6/6 통과):

| `all` 빌드 | `all` 합산 BLER | 같은 배치의 `cpu` | 가중 ΔBLER / ΔSINR | 판정 |
|---|---|---|---|---|
| C1 | 1.51% | 0.25% | +0.20%p / −0.16 dB | PASS (합산 6배) |
| C1 + D8 v1 | 0.00% | 0.22% | +0.00%p / −0.89 dB | FAIL |
| **C1 + D8 v2** | **0.54%** | 0.35% | **+0.21%p / −0.25 dB** | **PASS** |

v2에서는 링크 적응이 CPU와 같은 할당(17/19/24 PRB)을 고른다. 1 PRB 보고 SINR은 평균 7.01 / 최대 8.8 dB로, CPU(7.56 / 8.5)와 비슷하다. 15 PRB 선택은 사라졌다. 할당별로 남은 차이는 16QAM `[0,17)` 3.16% vs 1.95%(n 475/667)와 16QAM `[0,1)`의 ΔSINR −0.78 dB(n 52)다. PHY `t` p50은 198 µs로 v1 전과 같다.

**미해결:** MIMO 경로(`nof_tx_layers > 1`, wide 평활 커널)도 같은 조건이 있고, 2L2P 1 PRB에서 +0.82 dB다. wide 커널이 1 PRB에서 안전한지는 확인하지 않아서 이번 패치에서는 제외했다.

### S6 정정 — 20 MHz 1L PUSCH의 CPU 지연은 코어 배치가 지배한다 (2026-09-25)

S6에서 "원인 미상"으로 남긴 CPU 401.1 µs(문서 225.8 µs)를 같은 명령(`pusch_e2e_pipeline_test --prb 51 --sinr 25 --mcs 20 … --iterations 100`, C1 빌드)으로 `taskset`만 바꿔 2회씩 재현했다. governor는 모든 코어가 `performance`다. GB10의 코어는 A725(cpu 0–4, 10–14, 최대 2.8 GHz)와 X925(cpu 5–9, 15–19, 최대 3.9 GHz)다.

| CPU 배치 | CPU min / mean / max | GPU mean | CPU/GPU |
|---|---|---|---|
| 고정 없음 | 126–142 / 356–365 / 938–1002 µs | 179–184 µs | 2.0× |
| X925 1개(cpu 5) | 143 / **155** / 180 µs | 138–140 µs | **1.1×** |
| A725 1개(cpu 0) | 348 / **388** / 446 µs | 163–165 µs | 0.4× |
| X925 10개 | 108–131 / 305–372 / 1351–1475 µs | 180 µs | 1.7–2.1× |

- 빅 코어 하나에 고정하면 CPU 분포가 좁아지고(143–180 µs), GPU와 거의 같다(1.1×). 문서의 0.86×와 방향이 같다.
- 고정하지 않으면 평균이 리틀 코어 값에 가깝고, 최대가 1 ms에 이른다. 빅 코어 10개로 묶어도 비슷하다. 즉 코어 종류만이 아니라 스레드 이동과 깨어남이 섞인다.
- **따라서 S6의 "20 MHz 1L PUSCH는 문서와 반대로 GPU가 2.3× 빠르다"는 틀렸다.** CPU를 고정하지 않은 측정의 착시다. 이 부하에서 CPU(빅 코어)와 GPU는 비슷하다. 라이브에서 GPU가 느린 것(위 "라이브 PUSCH 처리 시간")과도 모순되지 않는다.
- **100 MHz 4L도 같다**(S6와 같은 명령, 반복 10, 2회씩):

| CPU 배치 | CPU mean | GPU mean | 배율 |
|---|---|---|---|
| 고정 없음 | 12238 / 13038 µs | 369 / 348 µs | 33.2× / 37.5× |
| X925 1개(cpu 5) | 6779 / 6774 µs | 302 / 308 µs | **22.4× / 22.0×** |
| X925 10개 | 7107 / 7139 µs | 347 / 328 µs | 20.5× / 21.8× |

  **S6의 "PUSCH 40× (문서 21.2×)"는 CPU 미고정의 착시였고, CPU를 빅 코어에 두면 20.5–22.4×로 문서 값을 재현한다.** 고정하지 않으면 CPU 평균이 두 배로 늘고 GPU도 약간 느려진다. 문서의 CPU 절대값(13122 µs)은 우리의 미고정 값과 비슷하고 GPU 절대값(618.9 µs)은 두 배다. 배율만 맞는 것은 우연일 수 있다. 앞으로 CPU/GPU 비교는 **`taskset -c 5`(X925 하나)로 고정해서** 잰다.

#### D8 v3 — MIMO 경로까지 (2026-09-25, `d8-20260925T052309Z`, `s5-arms-20260925T053012Z`)

기울기 보정 평활을 `__device__` 도우미 `fd_smooth_pilot_slope_comp()` 하나로 모았다. SISO, MIMO, MIMO wide 세 커널이 이 도우미를 쓰고, MIMO 경로의 `nof_prb > 1` 조건도 없앴다. 패치 sha256은 `10d89610`(현재 lock)이다.
- 오프라인 1 PRB SINR 차이(입력 10 dB): 2L2P는 +0.82 → −0.40 dB, 4L4P는 −0.25 dB다. SISO는 v2와 같다(−0.11).
- 20 dB MIMO 복호: TA가 없으면 모두 0 불일치다. TA ≤1 µs(2L2P)에서는 CRC 불일치가 C1과 D8 모두 1–3건으로, 회귀가 아니다.
- 검증: PHY 14/14, OFH 16/16, 판정 pass, SINR 비교 PASS, 음성 대조군 재현.
- **라이브(cpu/all 3회씩): BLER 0.27% / 0.81%, 가중 ΔBLER +1.01%p → FAIL**(ΔSINR −0.23 dB). 할당 구성은 v2와 같다. 차이는 거의 전부 16QAM `[0,17)`에서 났다(CPU 1.33% vs GPU 6.12%, n 677/474). SISO 계산은 v2와 같은데 v2 런에서는 3.16% vs 1.95%였다.

**이 BLER 차이는 D8 때문이 아니다.** 오프라인에서 17 PRB 16QAM(MCS 12)을 문턱 근처에서 600회씩 돌렸다. CPU/GPU BLER이다.

| | 6.0 dB | 6.5 dB | 6.0 dB, TA ≤1 µs | 6.5 dB, TA ≤1 µs |
|---|---|---|---|---|
| C1 | 30.8 / 28.7% | 0.2 / 0.3% | 34.8 / **45.3%** | 0.3 / **2.0%** |
| C1 + D8 | 33.5 / 32.5% | 0.2 / 0.3% | 31.8 / **36.3%** | 0.2 / **0.8%** |

TA가 없으면 두 빌드 모두 CPU와 같다. TA가 있으면 GPU 복호가 CPU보다 약하다. 이 약점은 **C1부터 있었고, D8이 오히려 줄였다.** 따라서 라이브 16QAM `[0,17)`의 초과 실패는 원래 있던 **"TA에 대한 GPU 복호 약점"**(결함 후보 **D9**)에 런 간 편차가 더해진 것이다. 라이브 S5 판정이 v2에서는 PASS(+0.21%p), v3에서는 FAIL(+1.01%p)로 흔들리는 것도 이 할당에 실패가 몰리기 때문이다.

- D9 추적 방향: GPU 등화와 복조가 TA로 인한 주파수 방향 위상 기울기를 CPU처럼 다루는지 확인한다. CPU는 TA를 추정해서 보간에 쓴다. 이 절의 오프라인 명령(`--prb 17 --mcs 12 --sinr 6.5 --ta-offset 1 --iterations 600`)이 재현 조건이다.
- 2000회(17 PRB, 6.25 dB, CPU/GPU BLER): C1은 TA 없음 4.1/3.8%, **TA 5.2/10.2%**. D8은 TA 없음 4.65/5.3%, **TA 5.25/6.4%**. D8이 TA 조건의 GPU 손해를 +5.0%p에서 +1.15%p로 줄였다. 남은 D9는 작다.
- S5 라이브 판정은 교차 3회로는 이 할당에서 흔들린다. 판정용으로는 반복을 늘리거나(≥6회), D9를 고친 뒤 다시 본다.

#### S5 재판정 — 교차 6회, C1 + D8 v3 (2026-09-25, `s5-arms-20260925T054535Z`)

`cpu`/`all` 6회씩, 12/12 통과. 가중 **ΔBLER +0.52%p, ΔSINR −0.27 dB, 일치 신규 전송 5435건 → PASS**. 합산 BLER은 CPU 0.41%, GPU 0.72%다. PHY `t` p50은 89 vs 224 µs다.

| 할당 | CPU n / BLER | GPU n / BLER | ΔSINR |
|---|---|---|---|
| 16QAM `[0,17)` tbs 528 | 1326 / 2.26% | 923 / **5.31%** | −0.18 |
| QPSK `[0,1)` tbs 21 | 1235 / 0% | 1690 / 0% | −0.60 |
| 16QAM `[0,1)` tbs 21 | 551 / 0% | 103 / 0% | −0.59 |
| 나머지 6개 할당 | 0% | 0% | −0.09 ~ −0.30 |

- 16QAM `[0,17)`의 +3.05%p는 표본이 커도 뚜렷하다. 이 할당의 GPU 복호 열세(D9)는 라이브에서도 실재한다.
- 1 PRB SINR은 여전히 −0.6 dB다(기준 0.5 dB를 할당별로는 넘는다). 가중 판정에서는 통과한다.
- **D9 — 라이브 조건에서 재현했고 범위를 좁혔다 (09-25).** `pusch_gpu_cpu_comparison_test -L`(15 kHz, `interpolate`, MMSE)에 진단 옵션 `-D <TA µs>`(수신 그리드에 선형 위상), `-C <CFO Hz>`, `-M <MCS>`를 추가했다. 별도 체크아웃 `src/ocudu-cuda-d9`(C1 + D8)와 `src/ocudu-cuda-d9c1`(C1만)을 쓰고, live lock 트리는 건드리지 않았다. 17 PRB, MCS 10, CFO 0, 2000회, CPU/GPU BLER이다.

  | | 4 dB | 4.5 dB | 4 dB, TA 1 µs | 4.5 dB, TA 1 µs |
  |---|---|---|---|---|
  | C1 | 45.2 / **56.5%** | 0.3 / **1.8%** | 43.3 / **61.1%** | 0.6 / **2.5%** |
  | C1 + D8 | 44.0 / **58.5%** | 0.3 / **2.1%** | 44.3 / **60.0%** | 0.3 / **1.9%** |

  - **D8과 무관하다**(C1에도 같은 차이). **TA가 없어도 있다**(TA는 조금 키울 뿐). 이 조건에서 **EVM과 보고 SINR은 CPU와 같다**(42.1 / 42.1%). 따라서 채널 추정이 아니라 **등화 이후(LLR 계산 또는 LDPC 복호)**에서 GPU가 문턱 근처 0.1–0.2 dB 약하다.
  - 09-24 이슈 6 진단의 "CFO 0이면 CPU·GPU 동일"은 5 dB에서만 본 것이라 문턱 차이를 놓쳤다.
  - **원인: GPU LDPC의 고정 min-sum 계수.** `pusch_codeblock_decoder_cuda_batch.cpp`는 `auto_scale=false, min_sum_scale=0.75, min_sum_offset=0.10`으로 고정한다. 벤더 주석은 "1L1P PRB/MCS 스윕에서 0.80보다 CPU 곡선에 가깝다"고 적었지만, 라이브 조건(`-L`)에서는 반대다. CPU는 0.8이다. 17 PRB, MCS 10, 2000회, 4 / 4.5 dB의 CPU/GPU BLER이다.

    | GPU 설정 | CPU | GPU |
    |---|---|---|
    | 벤더(0.75, offset 0.10) | 44.2 / 0.2% | **58.2 / 1.6%** |
    | `OCUDU_LDPC_SCALE=0.8`(offset 0) | 46.0 / 0.3% | 51.0 / 0.3% |
    | **`OCUDU_LDPC_SCALE=0.8 OCUDU_LDPC_OFFSET=0.1`** | 43.0 / 0.7% | **40.3 / 0.4%** |
    | `OCUDU_LDPC_SCALE=0.85`(offset 0) | 45.2 / 0.6% | 62.7 / 0.7% |
    | 진단: 계수만 0.8(offset 0.10 유지) | 43.2 / 0.8% | 39.5 / 0.6% |
    | 진단: 계수만 0.7 | 43.5 / 0.4% | 84.1 / 13.1% |

    계수 0.8 + offset 0.10이면 GPU가 CPU와 같거나 낫다. `OCUDU_LDPC_SCALE`은 offset을 0으로 되돌리니까 `OCUDU_LDPC_OFFSET=0.1`을 함께 줘야 한다. 두 변수 모두 벤더 코드에 이미 있고 라이브 러너가 넘기므로, **재빌드 없이 라이브 A/B가 가능하다**(실행 중). 진단 스위치 `OCUDU_DIAG_LDPC_SCALE`/`_PRINT_SCALE`은 `src/ocudu-cuda-d9`에만 있다.
  - 남은 확인: 큰 TB(BG1, 100 MHz 4L)의 감도와 지연에 계수 0.8이 손해를 주지 않는지 본다. 벤더가 0.75를 고른 근거가 그쪽일 수 있다.

### C1 + D8 + D9 통합 패치 (2026-09-25, `s-c1-d8-d9.patch`, lock `cuda-workspace.spark-d8.lock.json`)

D9 수정은 `pusch_codeblock_decoder_cuda_batch.cpp`의 `min_sum_scale`을 0.75에서 **0.80**으로 바꾸는 것이다(offset 0.10 유지, `OCUDU_LDPC_SCALE/OFFSET` 재정의는 그대로 동작). 라이브 A/B(`OCUDU_LDPC_SCALE=0.8 OCUDU_LDPC_OFFSET=0.1`, `s5-arms-20260925T062243Z`, 교차 6회, 12/12 통과) 결과는 다음과 같다.
- **가중 ΔBLER +0.00%p, ΔSINR −0.23 dB → PASS.**
- 16QAM `[0,17)`: CPU 1.90% vs GPU **1.90%**(이전 2.26% vs 5.31%). 합산 BLER은 0.35% / 0.50%다.
- BG1 확인(273 PRB, 4L8P, MCS 20, 150회, 12.5/13/13.5 dB, CPU/GPU BLER): 벤더 52.0/50.0, 33.3/34.0, 20.0/19.3%이고, 0.8+0.1은 51.3/50.7, 33.3/32.7, 21.3/18.0%다. 차이는 표본 편차 안이고 GPU 지연도 같다.

통합 패치 검증(`d8-*` 최신): OFH 16/16, 로그 판정 pass, SINR 비교 PASS, 음성 대조군 재현. **PHY는 13/14**였다. 실패한 `ofdm_demodulator_cuda.gpu_ci16_full_slot_batch_matches_cpu_with_phase_compensation`은 `get_owned_device_snapshot_cbf16()`을 읽는 `cudaMemcpyAsync`가 `cudaErrorInvalidValue`(1)를 돌려준 것이다. 10회 재실행 결과 C1 빌드 1/10, D8 빌드 0/10 실패로, **원래 있던 간헐적 실패**다(D8/D9가 건드린 파일과 무관, 결함 후보 **F1**, 추적 전).

**F1 재현 조건**(09-25, C1 빌드 `ofdm_demodulator_cuda_test`):

| 실행 방식 | 실패 |
|---|---|
| 해당 케이스 단독, 보통 실행 | 0/100 |
| 해당 케이스 단독, `CUDA_LAUNCH_BLOCKING=1` | 0/100 |
| 해당 케이스 단독, compute-sanitizer 아래 | 0/40 |
| 스위트 전체, 원래 순서 | 1/60 |
| 스위트 전체, `--gtest_shuffle` | 3/60 |

실패는 항상 같은 공통 도우미 줄(`:336`, 스냅샷 `cudaMemcpyAsync` → `cudaErrorInvalidValue`)이다. 소유 스냅샷을 읽는 두 케이스(`full_slot_batch…`, `split_slot_tail_batch…`) 중 **앞선 테스트 뒤에 도는 쪽**이 실패한다. 배제한 것(진단 트리 `src/ocudu-cuda-d9`의 테스트에 복사 직전 계측을 넣었고, 섞은 순서 100회 중 실패 5회):
- 복사 직전에 **남아 있던 CUDA 오류는 없다**(`cudaPeekAtLastError()=0`).
- 소스 포인터는 정상적인 장치 메모리다(`cudaPointerGetAttributes`: type 2, device 0).
- 스냅샷 버퍼는 그리드마다 따로 할당된다(전역 풀 없음).
- lower-PHY RX의 호스트 등록 캐시(`low_phy_puxch_rx.cu`, 버퍼보다 오래 남을 수 있음)도 원인이 아니다. `OCUDU_LOWPHY_RX_RUNTIME_HOST_REGISTRATION=0`에서 4/100, `=1`에서 3/100으로 같다.

남은 후보: 복사 대상(페이지 가능 `std::vector`)의 상태, 또는 드라이버 쪽. 추적을 멈춘다. 영향은 벤더 테스트의 간헐적 실패이고, 라이브 경로 영향은 확인하지 않았다.

**통합 패치 라이브**(`s5-arms-20260925T064648Z`, `cpu`/`all` 교차 6회, 12/12 통과):
- **가중 ΔBLER −0.00%p, ΔSINR −0.19 dB, 일치 신규 전송 5900건 → PASS.**
- 16QAM `[0,17)`: CPU 1.90% vs GPU **1.89%**. 합산 BLER은 0.34% / 0.52%다(차이는 할당 구성: GPU가 1 PRB 할당에서 SINR을 −0.3~−0.4 dB 낮게 보고해 조금 더 보수적이다).
- PHY `t` p50은 CPU 89 µs, GPU 226 µs다(20 MHz 1L, GPU가 느림).

**S 트랙 정합 요약**: C1만으로는 `all`의 합산 BLER이 CPU의 6배였다. C1 + D8 + D9를 넣으면 같은 할당에서 CPU와 구별되지 않는다. 남은 차이는 1 PRB SINR −0.3~−0.4 dB(D8 이후 잔여)와 처리 시간이다.

**S4 사다리 재실행, C1+D8+D9**(09-25, `SPARK_LOCK=…spark-d8…` `s4-ladder.sh`): 5단계 모두 attach·keepalive 299 통과. 백엔드 모양은 09-24 C1 사다리와 같다. `low-phy-tx`·`pusch` 단계는 TX가 staging(DEGRADED)이고, `pdsch`부터 direct+staging이다. 호스트 폴백 0. BLER은 0.25–0.73%다. D8/D9로 백엔드 선택이 바뀌지 않았다.

### 최종 빌드 성능 (C1+D8+D9, CPU를 X925 하나에 고정, 2026-09-25, `results/s6-final-*`)

| 부하 | CPU | GPU | 배율 | 문서 |
|---|---|---|---|---|
| PUSCH 100 MHz 273 PRB 4L 8RX, 평균 | 6787 / 6809 µs | 312 / 313 µs | **21.7×** | 21.20× |
| PUSCH 51 PRB 1L, 평균 | 155 / 157 µs | 141 / 140 µs | **1.1×** | 0.86× |
| PUSCH 106 PRB 1L, 평균 | 310 / 310 µs | 141 / 138 µs | **2.2×** | — |
| PDSCH 100 MHz 273 PRB 4L 4P, p50 | 577 / 581 µs | 171 / 171 µs | **3.4×** | 3.36× |

각 칸은 2회 반복이다. PDSCH CPU는 고정해도 미고정(S6 581.8 µs)과 같다. D8/D9는 벤치마크 지연을 바꾸지 않았다(C1 고정 22.0–22.4× → 21.7×, 표본 편차 수준).

### 통합 메모리에서 H2D/D2H는 사라졌나 — PDSCH 다중 UE 측정 (2026-09-25)

`pdsch_gpu_latency_benchmark`, C1+D8+D9 빌드, 100 MHz, 4 layer, 4 port, MCS 20, CPU는 X925(cpu 5–9)에 고정, 50회 p50. UE 수만큼 PDSCH PDU를 두고 272 PRB를 나눠 가진다.

| UE × PRB | CPU | GPU host 경로(복사) | GPU direct 경로 |
|---|---|---|---|
| 1 × 272 | 576 µs | 286 µs | **172 µs** |
| 4 × 68 | 583 µs | 459 µs | 340 µs |
| 16 × 17 | **610 µs** | 1069 µs | 994 µs |

- **복사는 피할 수 있을 뿐 사라지지 않았다.** host 경로의 그리드 복사는 UE 1개일 때 GPU 시간의 약 40%(114 µs)다. direct 경로에서만 없어진다. 라이브 TX는 호스트가 쓴 슬롯에서 staging을 탄다(S4). 포트 수에 따른 변화는 아래 포트 스윕을 보라. "포트 수에 비례해 staging 비용이 는다"는 앞선 추정은 측정으로 뒷받침되지 않는다.
- **UE가 늘면 PDU당 고정 비용이 지배한다.** direct 경로의 PDU당 약 60 µs는 `--sync-after-process=0`에서도 같다(990 vs 995 µs). 16 UE × 17 PRB에서는 GPU가 CPU보다 1.6배 느리다. MU 확장의 병목은 H2D/D2H가 아니라 PDU별 실행이다. 배치 처리가 필요하다.
- 라이브 20 MHz 1L PUSCH의 전송 구간은 H2D p50 8.9 µs(demod phase, `direct_grid=true` 2745/2745), D2H p50 3.8 µs로, 처리 시간(p50 245 µs)의 약 5%다.
- 미측정: PUSCH 다중 UE, 8 포트 이상, 실제 MU-MIMO, 라이브 TX에서 staging 경로가 쓰이는 슬롯 비율.

**포트 수 스윕**(같은 벤치마크, 1 layer, `--precoding=all-ports`, 272 PRB, UE 1개, 30회 p50):

| 포트 | CPU | GPU host 경로 | GPU direct 경로 |
|---|---|---|---|
| 1 | 141 µs | 112 µs | 101 µs |
| 4 | 153 µs | 124 µs | 130 µs |
| 8 | 170 µs | 134 µs | 170 µs |
| 16 | **세그폴트** | 85 µs(비정상) | 324 µs |
| 32, 64 | 세그폴트 | 세그폴트 | 세그폴트 |

- 1 layer를 모든 포트로 복제하는 구성에서는 포트가 늘수록 **direct 경로가 host 경로보다 느려진다**(8 포트 170 vs 134 µs). GPU가 managed 그리드에 직접 쓰는 비용이 포트 수와 함께 커진다. 4 layer 4 port(위 표)에서는 direct가 빨랐으니, 어느 경로가 나은지는 구성에 따라 달라진다.
- 16 포트 host 경로의 85 µs는 1 포트보다 빨라 물리적으로 맞지 않는다. 같은 구성에서 CPU가 세그폴트이므로, 이 벤치마크의 16 포트 이상 결과는 믿지 않는다.
- **결론: 이 벤치마크로는 massive MIMO 규모(≥16 포트)를 잴 수 없다.** 그런 규모에서의 H2D/D2H·그리드 비용은 미확인이다. 세그폴트는 벤치마크(또는 그리드 최대 포트 수)의 한계로 보이며, 추적하지 않았다.

**여러 UE의 배치 처리** (09-25, 같은 벤치마크, 16 UE × 17 PRB, 4L4P, direct 경로):

| 방식 | 전체 p50 | PDU당 |
|---|---|---|
| CPU | 610 µs | 38 µs |
| GPU, PDU별 처리(gNB 경로) | 995 µs | 62 µs |
| GPU, `--encoder-batch=1`(TB 배치 인코딩) | **140 µs** | **8.8 µs** |

- gNB의 PDSCH는 PDU마다 처리기를 하나씩 부른다(`pdsch_processor_pool.h`, 동시성은 `pdsch_acceleration_nof_lanes`, 라이브는 `lanes=1 pool_size=4`). 한 PDU 안의 코드블록은 배치된다(`cb_batch=all`).
- 여러 PDU를 한 번에 인코딩하는 경로(`ldpc_encoder_cuda_batch`, `transport_block.cu`)는 라이브러리에 있지만 **gNB 코드 어디에서도 호출되지 않는다**. 벤치마크만 하위 API를 직접 쓴다. 이 경로가 gNB 처리기의 일을 전부 포함하는지는 확인하지 않았다.
- 결론: 다중 UE에서 GPU가 CPU보다 느린 이유는 교차 PDU 배치가 gNB에 연결되지 않았기 때문이다. 연결하면 7배 가까운 여지가 있다.
- GPU PDSCH를 켜도 DMRS는 CPU가 매핑한다(`pdsch_processor_flexible_impl::map_reference_signals`, D7 스택). 그래서 슬롯마다 그리드 소유권이 CPU와 GPU를 오간다.

### S7 — 채널 에뮬레이터 경유 + 멀티 gNB (2026-09-27)

지금까지 Spark의 CUDA gNB 라이브는 직결 ZMQ(gNB↔srsUE)뿐이었다. 이번에 처음으로 channel emulator를 거쳐 붙였고, 이어서 CUDA gNB 프로세스 2개를 한 GPU에서 돌렸다. 빌드는 C1+D8+D9(`cuda-workspace.spark-d8.lock.json`, `builds/d8-cuda-patched-sm121`), 가속 단계 `all`, emulator 브로커는 `gb10-zero-copy` 트리(`/workspace/gpuch/zc9`)이고 host memory는 `copy`다.

**1×1 (`run-ocudu-cuda-1x1.sh`, 20260927T104658Z): 통과.** `tx_pulls=67097`, `rx_starvations=7`, 카운터 0. gNB 로그에서 GPU 경로 선택을 확인했다: UL 그리드 direct writer, DL 그리드 direct reader, PRACH buffer direct writer. lower-PHY TX는 host staging fallback이다(S4와 같음).

**멀티 gNB 러너.** `scripts/native/run-ocudu-multi-gnb.sh`(+`-inner.sh`, `render-multi-gnb-configs.py`)를 새로 만들었다. Docker 기반 `scripts/remote/ocudu-multi-gnb-smoke.sh`를 native로 옮긴 것이다. 네임스페이스·프로세스 관리는 native 멀티 UE 게이트 것을 그대로 쓴다. 구성은 다음과 같다.
- gNB 프로세스 2개. 셀마다 PCI 1/2, `gnb_id` 411/412, ZMQ 포트 2000–2001/2010–2011이 다르다. N2/N3 bind는 `127.0.0.11`/`.12`로 나눴다(같은 주소면 GTP-U 포트가 충돌한다).
- srsUE 2개, Open5GS 하나.
- 브로커 토폴로지는 `examples/configs/topologies/ocudu_docker/topology.multi-gnb.cuda.yaml`이다: serving 6 dB, intercell 20 dB, 링크 8개.
- `OCUDU_NATIVE_GNB_ACCELERATION`이 있으면 CUDA gNB(두 셀 모두), 없으면 CPU gNB다.
- 판정에 **UE가 자기 셀(PCI)에 붙었는지**를 넣었다. 두 UE가 한 셀에만 붙으면 2셀 시험이 아니기 때문이다.
- aarch64용 노브를 넣었다: `OCUDU_NATIVE_CUDA_ARCH`, `OCUDU_NATIVE_SKIP_WORKSPACE_LOCK`.

| 실행 | gNB | 결과 | UE0 → PCI | UE1 → PCI | rx_starvations | 브로커 p50 / p99 (gNB 노드) | GPU 메모리 |
|---|---|---|---|---|---|---|---|
| 20260927T105015Z | CPU × 2 | **통과** | 1 (기대 1) | 2 (기대 2) | 2 | 120 / 180 µs | — |
| 20260927T105519Z | **CUDA × 2** (`all`) | **통과** | 1 (기대 1) | 2 (기대 2) | 19 | 135 / 235 µs | **gNB당 9,545 MiB** (RSS 4.9 GB) |

- **두 CUDA gNB 모두 GPU 경로를 탔다.** 로그의 경로 선택 줄이 1×1과 같다. 두 gNB 로그 어디에도 late·overflow 이벤트가 없다.
- **MPS 없이 돌았다.** 두 프로세스가 GPU를 시간 분할로 나눴다. 브로커 호출이 CPU gNB 대비 p50 +15 µs, p99 +55 µs 늘었고, rx_starvations가 2 → 19로 늘었다. 셋 다 게이트 기준 안이다. 이 증가가 GPU 경합 때문인지는 분리하지 않았다.
- **GPU 메모리:** nvidia-smi 기준 CUDA gNB 하나가 9,545 MiB를 잡는다. 5090(09-11, 1개)에서 잰 GPU 전체 사용량 10.7 GB와 같은 규모다. GB10(통합 121 GB)에서는 여유가 크다. 32 GB인 5090에서는 3개 이상이 어렵다(추정).

**zero-copy 브로커와 조합 (CUDA gNB × 2, `all`, copy·zero-copy 교대로 2회씩).** 네 번 모두 통과했고, UE는 매번 자기 셀에 붙었다. 카운터는 0이다. zero-copy 실행에서는 브로커 `gpu_timings`의 D2H가 0.8 µs라 zero-copy가 실제로 돌았다. copy 실행은 7.8–7.9 µs다.

| 실행 | 브로커 | gNB 노드 n | gNB p50 / p99 | UE p50 / p99 | rx_starvations | ring 읽기 / emulator / ring 쓰기 (중앙값, µs) |
|---|---|---|---|---|---|---|
| 105519Z | copy | 34만 | 135 / 235 | 130 / 300–305 | 19 | 13.2 / 129.9 / 5.0 |
| 113123Z | **zero-copy** | 34만 | **100 / 175** | **100 / 255** | 20 | 20.9 / 96.1 / 13.4 |
| 113753Z | copy | 65만* | 100* / 225 | 130 / 305–310 | 18 | 12.8 / 113.2 / 4.8 |
| 114423Z | **zero-copy** | 64만* | **75* / 175–180** | **95 / 255** | 22 | 20.5 / 79.0 / 12.6 |

\* 두 번째 쌍은 gNB 노드 호출 수가 약 2배다. 샘플 수가 적은 조각 호출이 섞여 p50이 낮게 나오므로, 같은 쌍 안에서만 비교한다. UE 노드는 네 실행 모두 약 34만 회라 바로 비교할 수 있다.

- **같은 쌍 안에서 zero-copy가 p50 30–35 µs, p99 50–60 µs 짧다.** UE 노드는 p50 130 → 95–100 µs, p99 300–310 → 255 µs다. 1×1 라이브에서 줄어든 폭(p50 약 20 µs, p99 약 25 µs)보다 크다. 이 토폴로지는 노드마다 들어오는 링크가 2개이고, GPU를 CUDA gNB 2개와 나눠 쓴다. 이 두 조건 중 어느 쪽이 폭을 키웠는지는 분리하지 않았다.
- **브로커 한 슬롯(ring 읽기 + emulator + ring 쓰기)은 148 → 130 µs, 131 → 112 µs로 줄었다.** zero-copy에서 ring 단계가 길어지는 현상(Z8에서 본 것)이 여기서도 나타나지만, emulator 호출이 줄어든 폭이 더 크다.
- **rx_starvations는 모드와 무관하게 18–22다.** 브로커 모드가 원인이 아니다. CPU gNB 2셀은 2였으므로 CUDA gNB 쪽 타이밍과 관련 있어 보이지만, 확인하지 않았다.
- **GPU 메모리는 모드와 무관하게 gNB당 9,545 MiB다.**
- **아직 안 한 것:** MPS 켠 비교, 3셀 이상, 트래픽 부하에서 BLER/SINR, 장시간 실행.

### S8 — 셀 대역폭 확장 (2026-09-28)

지금까지의 라이브는 전부 20 MHz(n3 FDD, 15 kHz, 106 PRB, 23.04 MS/s) 셀이었다. 이번에 셀 대역폭을 올리면서 channel emulator를 거친 라이브를 돌렸다. 브로커는 `gb10-zero-copy`+Z8 트리(`/workspace/gpuch/zc9`, 빌드 `gpuch-zc9-release`)이고, 실행마다 `runtime.cuda_host_memory`를 `copy` 또는 `zero_copy`로 명시했다. 전체 표는 워크스테이션 `~/ocudu-work/perf-platform/compare-bw-live.md`에 있다. 모든 실행의 시작과 끝에 다른 GPU 프로세스가 없었다.

**렌더러.** `scripts/native/render-1x1-bw-configs.py`를 새로 만들었다. 기존 1×1 렌더러(srsUE legacy 또는 OAI)를 그대로 돌린 뒤, 대역폭에서 따라 나오는 값만 바꾼다: gNB `channel_bandwidth_MHz`·`srate`·`base_srate`, srsUE `srate`·`nof_prb`·`ssb_nr_arfcn`, 브로커 `sample_rate_hz`와 1 ms 배치. 밴드, ARFCN, 채널 모델, 코어는 그대로라서 대역폭만 바뀐다. 20 MHz 렌더는 기존 렌더러 출력과 바이트 단위로 같다(두 base 모두 확인). OAI 게이트(`run-ocudu-oai-1x1.sh`)에는 노브를 더했다: 렌더러, 채널 빌드·CUDA arch, gNB 바이너리·커밋(CUDA gNB용), UE 무선 인자, UE capability 파일, 브로커 기동 여유.

**srsUE: 20 MHz가 한계다.** srsUE NR은 15 kHz SCS만 받는다(`rrc_nr_procedures.cc`가 다른 MIB SCS를 거부한다). 그래서 30/40/50 MHz(15 kHz)를 시도했다.
- **30 MHz(160 PRB, 30.72 MS/s):** 셀 검색과 SIB1 복호까지 된다. 그다음 `Converting carrier to cell for PRACH (-5)`가 나오고, PRACH를 한 번도 보내지 않는다. NR PRACH가 LTE PRACH 코드를 쓰는데, 그 코드가 110 PRB를 넘으면 에러를 낸다(`srsran_symbol_sz`). 버퍼도 30.72 MS/s 기준으로 잡혀 있다(`prach.cc`). 소스 한계라서 설정으로는 우회할 수 없다.
- **40/50 MHz(46.08/61.44 MS/s):** SFN 동기 중에 `srsran_pbch_nr_decode`에서 SIGSEGV가 난다(backtrace를 addr2line으로 풀었다).
- **FFTW 계획 시간:** 처음 실행하면 FFT 계획 한 개에 수 초에서 30 초가 걸려서, gate 창 안에 PHY 초기화가 끝나지 않는다. `/root/.srsran_fftwisdom`을 미리 채운 뒤에 쟀다.
- **SSB 위치:** srsUE는 SSB를 찾지 않고 `ssb_nr_arfcn`(기본 368410, 20 MHz 값)에 맞춘다. 첫 30–50 MHz 실행은 이 값 때문에 셀을 못 찾았다. 렌더러가 대역폭마다 gNB가 출력한 SSB ARFCN을 넣는다.
- 30 MHz에서 브로커는 attach 없이도 실시간으로 돌았다(962 슬롯/s). 여기서 copy p50/p99는 70/115 µs, zero-copy는 40–45/80–105 µs다.

**OAI nrUE: 100 MHz까지 통과.** OAI(`2b69bde`, M6과 같은 핀)를 Spark(aarch64)에서 빌드했다. 추가로 설치한 패키지는 `liblapacke-dev libblas-dev libnuma-dev libcap-dev xxd`이고, 빌드는 2분 남짓 걸렸다. **GB10에서 OAI nrUE를 처음 붙인 것이다.** 셀은 15 kHz n3 FDD 20/30/40/50 MHz와 30 kHz n78 TDD 100 MHz(273 PRB, 122.88 MS/s)다. 100 MHz 셀은 OCUDU 예제 `gnb_ru_ran550_tdd_n78_100mhz_4x2.yml`의 `dl_arfcn 637212`와 PRACH 159를 따랐다. 각 대역폭에서 copy와 zero-copy를 교대로 돌렸다. 대역폭마다 막힌 곳과 해결은 다음과 같다.
- **샘플레이트:** nrUE는 PRB 수로 샘플레이트를 정하고, `-E`를 주면 FFT의 3/4을 쓴다. 160 PRB와 270 PRB에 `-E`를 주면 46.08/92.16 MS/s가 되어 gNB(30.72/61.44)와 어긋난다. 실측으로 확인했다(`synch Failed`). 렌더러가 대역폭마다 `-E` 사용 여부를 정한다.
- **SSB 오프셋:** `--ssb`는 point A부터 센 SSB 첫 부반송파 번호다. gNB가 출력한 SSB ARFCN에서 계산한다. 계산식은 기존 20 MHz 값 486을 그대로 재현한다.
- **50 MHz:** OAI `uecap_ports1.xml`에는 15 kHz 50 MHz feature set이 없다. 그래서 max MIMO layers가 0이 되고, 첫 DCI 1_1에서 `max_mimo_layers > 0` assert가 난다. 15 kHz 40 MHz 항목을 복사해 50 MHz로 바꾼 capability 파일을 쓴다(`oai_uecap`).
- **Spark 전용 조치:** gate는 컨테이너 root가 매핑된 user namespace에서 돈다. 그래서 dev 소유 디렉터리에서 실행하면 nrUE가 cwd에 통계 파일을 못 만들고 abort한다. cwd를 `/tmp/s8-oai-cwd`로 옮겨서 해결했다. 이 밖에 x86 lock 검사 생략, `bc88865` 부재 허용, 26.04 배너 허용, `start_group` pgid 대기는 legacy 게이트와 같은 Spark 전용 미커밋 패치다.

| 대역폭 (MS/s) | gNB | 결과 (copy / zero-copy) | 브로커 UE 노드 p50/p99, copy | zero-copy | emulator 호출 중앙값 copy → zero-copy (gNB 노드) | rx_starvations |
|---|---|---|---|---|---|---|
| 20 MHz (23.04) | CPU | 2/2 · 2/2 | 60/100–105 | **40/80** | 55–58 → 34–36 µs | 3370–3405 |
| 30 MHz (30.72) | CPU | 2/2 · 2/2 | 60–70/110 | **40/80** | 56–61 → 33–34 µs | 3347–3391 |
| 40 MHz (46.08) | CPU | 2/2 · 2/2 | 75/120 | **40–45/85** | 60–73 → 34–37 µs | 3274–3309 |
| 50 MHz (61.44) | CPU | 2/2 · 2/2 | 70–90/130–140 | **45/85** | (조각 호출)* → 41–42 µs | 3243–3273 |
| 100 MHz (122.88, TDD) | CPU | 2/2 · 2/2 | 90/135–140 | **45/85** | 86–91 → 41 µs | 2981–3014 |
| 100 MHz (122.88, TDD) | **CUDA `all`** | 2/2 · 2/2 | 90–95/230–235 | **45/185–195** | 90–92 → 44–45 µs | 6716–7094 |

\* 50 MHz copy 두 번은 gNB 노드 호출의 절반가량이 샘플 수가 적은 조각 호출이라(gNB 노드 슬롯/s가 UE의 2배) 중앙값이 38–40 µs로 낮게 나온다. UE 노드와 p99로 비교한다.

- **zero-copy 브로커의 지연은 대역폭에 거의 무관하다.** p50은 20 MHz 40 µs에서 100 MHz 45 µs, p99는 80–85 µs다. copy는 p50 60 → 90 µs, p99 105 → 135–140 µs로 는다. 없앤 복사가 바이트 수에 비례하기 때문이다. 벤치의 대역폭 스케일링(ZERO_COPY_MILESTONES.md)이 라이브에서도 같은 방향으로 나타났다.
- **ring 단계는 zero-copy에서 대역폭과 함께 커진다.** gNB 노드 ring 읽기와 쓰기는 각각 20 MHz 10 µs에서 100 MHz 25–29 µs다(copy는 5 → 15/11 µs). Z8에서 관찰한 "zero-copy에서 ring 단계가 길다"는 현상이 바이트 수에 비례해 커진다. 100 MHz 한 슬롯(ring 읽기 + emulator + ring 쓰기)은 copy 112–118 µs, zero-copy 94–97 µs다.
- **CUDA gNB 100 MHz (C1+D8+D9, `all`):** 4회(copy·zero-copy 각 2회) 모두 통과했다. gNB 로그에서 GPU 경로 선택을 확인했다: UL 그리드 direct writer, PDSCH direct grid writer, PUSCH owned CUDA snapshot. GPU 메모리는 gNB 9,751 MiB, 브로커 176–180 MiB다. 브로커 p99가 CPU gNB 대비 +50–100 µs다. GPU를 CUDA gNB와 나눠 쓰는 것과 관련 있어 보이지만, 분리하지 않았다. zero-copy는 여기서도 p50 90 → 45 µs, p99 230 → 185–195 µs다.
- **OAI 실행은 실시간보다 느리다.** 루프가 255–365 슬롯/s로 돈다(srsUE 실행은 약 965/s). `top` 스냅샷에서 nrUE `radio` 스레드가 코어 하나를 100% 쓴다. rx_starvations 약 3,000(CUDA gNB 약 7,000)은 대역폭과 모드에 무관하다. UE 쪽 페이싱이 원인으로 보이지만, 확인하지 않았다. 워크스테이션(x86)에서 OAI 루프 속도를 같이 재지 않았으므로 aarch64 탓인지는 모른다.
- **부수:** 09-26 게이트에서 남은 srsUE 프로세스가 컨테이너에서 이틀 동안 코어 하나의 약 10%를 쓰고 있었다. 06:06에 정리했다. 그 전의 srsUE 실행(20–50 MHz)은 이 프로세스와 겹쳤다.
- **아직 안 한 것:** 트래픽 부하(iperf)에서 브로커 지연, OAI 루프 속도의 원인, 100 MHz에서 BLER/SINR, CUDA gNB 30–50 MHz, 워크스테이션(5090)과 같은 대역폭 비교.

## S9 — S8 관측의 원인 규명 (2026-09-28)

S8에서 원인을 확인하지 않은 세 가지를 Spark(GB10)에서 풀었다. 모든 실행에서 시작·종료 시점에 다른 사용자의 GPU 프로세스는 없었다. 실행 스크립트와 원자료는 Spark `/workspace/gpuch/s9/`에 있다. 도구: 호스트의 `perf`(7.0.0-1019-nvidia)와 Nsight Systems 2025.3.2를 컨테이너 `/opt`에 복사해 컨테이너 root로 썼다(컨테이너의 `/usr/local/cuda/bin/nsys`는 설치되지 않은 스텁이다).

### 1. OAI 실행이 실시간보다 느린 이유 — OAI ZMQ 드라이버의 10 ms 대기

- **누가 바쁜가.** 20 MHz 실행 중 시스템 전체 `perf record`(6 s)에서 샘플의 58%가 idle이었다. 스레드별 CPU는 gNB `radio` 95–100%, 브로커 ZMQ I/O 약 6%, nrUE 전체 약 17%였다. 계산에 묶인 프로세스가 없다 → 기다림에 묶인 것이다. S8에서 "nrUE radio 스레드가 코어 하나를 쓴다"고 적은 것은 잘못 읽은 것이다. 그 `radio` 스레드는 gNB(OCUDU)의 ZMQ 라디오 스레드이고, 폴링 루프라 속도와 상관없이 항상 코어 하나를 쓴다(100 MHz perf: `send_response` 19%, `receive_response` 5%, 나머지는 poll·시계·재예약).
- **원인(코드).** OAI `radio/zmq/zmq_radio.cpp`의 `tx_poll_thread`는 브로커의 REQ를 받았을 때 보낼 TX 샘플이 아직 큐에 없으면 `reply_requested`만 세우고 `zmq_poll(..., 10)`으로 돌아간다. REP 소켓은 응답하기 전에는 새 입력이 오지 않으므로 이 poll은 매번 10 ms 타임아웃까지 잔다. lock-step 체인에서는 이 지연이 gNB RX → gNB TX → UE RX로 그대로 전파된다. 브로커의 `rx_starvations`(입력 레인이 비어 기다린 슬롯)는 이 지연의 증상이다.
- **확인(되돌리는 조작).** 응답이 밀려 있는 동안에는 20 µs마다 큐를 다시 보도록 드라이버만 고쳐(`scripts/native/patches/oai-zmq-tx-reply-poll.patch`) 별도 모듈 디렉터리로 빌드하고, 게이트에 `OCUDU_NATIVE_OAI_SHLIBPATH`로 넘겼다. 핀된 OAI 소스와 빌드는 그대로다. 20 MHz, copy 브로커, 원본/패치 교대 2쌍, 4회 모두 통과:

| UE 드라이버 | 슬롯/s (gnb0 / ue0) | rx_starvations | 브로커 p50 / p99 (µs) |
|---|---|---|---|
| 원본 | 355 / 256, 382 / 316 | 3,335, 3,375 | 50–60 / 100 |
| **패치** | **1,048 / 999, 1,000 / 1,000** | **1, 1** | 60 / 95 |

  패치 후에는 브로커의 실시간 페이싱(`throttle_us` > 0)이 속도를 정한다. 워크스테이션 M6.4의 0.275× 실행도 같은 드라이버라 같은 원인일 가능성이 높지만, 거기서는 확인하지 않았다.
- **100 MHz는 패치 후에도 실시간의 약 0.72×다**(슬롯 0.5 ms 기준 실시간 2,000/s 대비 1,410–1,480/s, `throttle_us` ≈ 0). `runtime.rx_ring_batches` 2 → 4는 효과가 없었다(1,475 → 1,481/s). 바쁜 스레드는 gNB `radio` 95%(대부분 폴링), 브로커 ZMQ I/O 60%(대부분 커널 TCP), gNB `phy_worker` 58%로 어느 것도 포화가 아니다. 슬롯마다 방향별 491 KB(cf32)를 TCP loopback으로 네 번 주고받는 lock-step 체인의 지연 합이 원인으로 추정되지만 분리하지 않았다. → S10에서 풀었다: 바이트 수보다 CPU 배치(깊은 idle 상태와 little 코어)가 원인이었다.

### 2. CUDA gNB 100 MHz의 브로커 p99 증가 — GPU 컨텍스트 time-slicing

- **재현(패치된 UE, 100 MHz, 교대 2쌍).** zero-copy 브로커 p99: CPU gNB 80 µs, CUDA gNB 195–200 µs. copy 브로커: 140 vs 240–245 µs. p50은 같다(45 / 90 µs).
- **분해(nsys, 브로커, 8 s, 각 약 24,000 호출).** 커널 실행 시간은 같다(`apply_channel_kernel` p50 6.7 / 7.0, p99 16.3 / 16.3 µs). CPU 쪽 API 시간도 같다(`cudaLaunchKernel` p99 16.8 / 16.3 µs). 달라진 것은 **launch → 커널 시작 대기**뿐이다: p99 6–17 µs → **149–161 µs**. 그 결과 `cudaStreamSynchronize` p99가 23 → 169 µs가 된다. CPU 경합이면 API 시간이, 메모리 대역폭이면 커널 시간이 늘어야 하므로 둘 다 아니다. 다른 프로세스(gNB)의 GPU 컨텍스트가 시간 조각을 쓰는 동안 브로커 커널이 기다리는 것이다.
- **확인.** `scripts/cuda/with-cuda-mps.py`로 gNB와 브로커를 한 MPS 서버의 클라이언트로 돌렸다(MPS `ps`에 `gnb`, `ocudu-gpu-channel` 모두 확인). MPS/비 MPS 교대 2쌍, 4회 모두 통과:

| | 브로커 p50 / p99 (µs, gnb0·ue0) | 슬롯/s |
|---|---|---|
| MPS 없음 | 45 / 195–200 | 1,483, 1,493 |
| **MPS** | **45 / 80** | 1,534, 1,528 |

  MPS에서 p99가 CPU gNB 실행과 같아졌다. S7의 2-gNB 실행에서 본 p99 +55 µs도 같은 메커니즘으로 보이지만 거기서 MPS로 확인하지는 않았다.

### 3. zero-copy에서 ring read/push가 길어지는 이유 — GPU가 만진 버퍼의 CPU 복사

- zero-copy(`direct_in`, `direct`)에서는 GPU가 브로커의 입력 창을 직접 읽고 출력 행에 직접 쓴다. 다음 슬롯에 CPU가 ring → 입력 창(쓰기), 출력 행 → RX ring(읽기)을 `memcpy`하는데, 그 캐시 라인이 GPU 쪽에 있다.
- **마이크로벤치** `scripts/cuda/spark/s9-coherence-bench.cu`(pageable 버퍼, 2,000회, 코어 5 고정, GPU 유휴):

| 크기 | CPU만 쓴 버퍼 | GPU가 읽은 창에 CPU 쓰기 | GPU가 쓴 행을 CPU 읽기 |
|---|---|---|---|
| 184 KB (20 MHz 1 ms) | 1.6 µs | 7.5 µs | 9.3 µs |
| 983 KB (100 MHz 1 ms) | 12.6–13.1 µs | 40.5 µs | 44.8 µs |

  라이브에서 본 증가(20 MHz copy 3.5–6 → zero-copy 9–14 µs, 100 MHz 약 10 → 26–29 µs)와 크기·방향이 맞는다. GB10의 CPU–GPU 일관성(coherence) 비용으로 보이며, 원인 수준(스누프·무효화)까지는 확인하지 않았다. 슬롯 전체(read + process + push)는 여전히 zero-copy가 짧다(S8: 100 MHz 94–97 vs 112–118 µs). 이 복사까지 없애려면 ring을 거치지 않는 경로가 필요하다(`ZERO_COPY_MILESTONES.md` 다음 후보 3).
- 라이브 커널·H2D가 벤치보다 큰 이유는 따로 보지 않았다(**열림**).

### 바뀐 것

- `scripts/native/patches/oai-zmq-tx-reply-poll.patch` — 위 1의 드라이버 수정. 업스트림 제보 후보.
- `scripts/native/run-ocudu-oai-1x1-inner.sh` — `OCUDU_NATIVE_OAI_SHLIBPATH`(nrUE 모듈 디렉터리), `OCUDU_NATIVE_BROKER_WRAPPER` / `OCUDU_NATIVE_GNB_WRAPPER`(프로파일러 래퍼). 기본값에서는 동작이 같다.
- `scripts/cuda/spark/s9-coherence-bench.cu` — 위 3의 마이크로벤치.
- **실험 기록:** 첫 100 MHz 실행 1회는 실행 중에 inner 스크립트를 고쳐 bash 구문 오류로 실패했다(측정에서 제외). nsys `--kill=none` 실행은 gNB가 남아 GPU를 잡아서 직접 종료했다. 이후 `--kill=sigterm`을 썼다.

## S10 — 100 MHz가 실시간에 못 미치는 이유 (2026-09-28)

S9에서 OAI ZMQ 드라이버를 고친 뒤에도 100 MHz(n78 TDD 30 kHz, 122.88 MS/s, 메시지당 61,440샘플 = 491 KB)는 실시간(0.5 ms 슬롯, 2,000/s)의 약 0.72×였다. 바쁜 스레드가 없다는 것까지가 S9의 결론이었다. 모든 실행은 CPU gNB(`a1916edc`), zero-copy 브로커, 패치된 UE 드라이버, 100 MHz이고, 시작·종료 시점에 GPU 프로세스는 없었다(CUDA gNB 실행은 자기 것만). 실행 스크립트와 원자료는 Spark `/workspace/gpuch/s10/`, 트리는 `zc10`(= `zc9` + 아래 브로커 변경), 빌드 `builds/gpuch-zc10-release`.

### 1. 홉 분해 — 고리에 파이프라이닝이 없다

- 브로커에 `OCG_HOP_TRACE_DIR`를 추가했다. ZMQ 전송(요청 송신·응답 수신·ring 적재, RX 요청 수신·행 pop·응답 송신)과 producer 슬롯(시작·끝)마다 타임스탬프 하나를 남긴다. 분석은 `scripts/cuda/spark/s10-hop-trace.py`.
- **리드(장치가 넘긴 TX 샘플 − 브로커가 돌려준 RX 샘플)가 gNB·UE 모두 0–1 메시지**다. 두 라디오 모두 RX를 받아야 다음 TX를 내놓는다(OAI `zmq_rx_stream::receive`는 TX가 RX 끝까지 정렬되기를 기다리고, 브로커의 TX 요청 → gNB 응답 `pull_rtt`가 슬롯 주기와 같다). 그래서 **슬롯마다 gNB → 브로커 → UE → 브로커 → gNB 고리 한 바퀴를 통째로 기다린다.** 이 고리가 500 µs 안에 돌아야 실시간이다.
- 기준 실행은 6회 모두 1,466–1,506 슬롯/s였다. 그중 하나(`base100`)에서 브로커가 응답을 보낸 뒤 장치의 다음 RX 요청까지 `rx_turn` 평균 441–469 µs, 브로커 안에서 TX 수신 → RX 송신 relay p50 237–252 µs(방향별). 20 MHz(1 ms 메시지)에서는 같은 고리가 1 ms 안에 들어가 브로커가 실시간으로 페이싱한다.

### 2. 고리가 느린 이유 — ZMQ 전송 한 번이 느린 게 아니라 코어가 잠들어 있다

마이크로벤치(`scripts/cuda/spark/s10-zmq-split.cpp`, 1 B 요청 → 491 KB 응답, 3,000회, GPU·게이트 미실행):

| 조건 | 왕복 p50 / p99 (µs) |
|---|---|
| 원시 TCP loopback(ZMQ 없이, 블로킹 소켓) | 35 / 40 |
| ZMQ, 서버 코어 7,8 / 클라이언트 5,6 | 235–340 / 812–831 |
| 같은 조건 + 네 코어에 `sched_yield` 스피너(`s10-yieldspin.c`) | **97 / 101** |
| ZMQ `SO_SNDBUF`/`SO_RCVBUF` 4 MB | 차이 없음(332–584) |
| ZMQ `ipc://` | 더 느림(768 / 3,168) |

- ZMQ는 메시지 하나를 앱 스레드 ↔ I/O 스레드로 네 번 넘긴다. 491 KB 교환에서는 그 사이 코어가 쉬는 시간이 길어, `menu` 거버너가 가장 깊은 LPI-3(선언 탈출 지연 433 µs, 목표 체류 2,542 µs)을 고른다. 3,000회 왕복 동안 네 코어의 LPI-3 진입이 **약 5,250회**(왕복당 1.75회)였고, 원시 TCP는 같은 횟수에서 약 45회였다. 코어를 깨어 있게 두면 왕복이 1/3이 된다. 메모리 복사 자체는 빠르다(다른 코어가 방금 쓴 491 KB 복사 11–19 µs), 페이지 폴트도 없다.
- 컨테이너의 `/sys`가 읽기 전용이라 idle 상태를 끄는 직접 대조군(`cpuidle/state3/disable`, `pm_qos_resume_latency_us`)은 못 했다. 호스트 sysctl·sysfs는 바꾸지 않았다.
- 스피너 주의: `SCHED_IDLE` 스피너는 같은 코어에서 깨어난 스레드를 약 6 ms 늦췄다(커널은 `PREEMPT_LAZY` 빌드, 7.0.0-1019-nvidia). `sched_yield` 스피너는 그렇지 않다.

### 3. 라이브 확인 — 한 번에 하나씩 (100 MHz, zero-copy, 브로커 슬롯/s gnb0 / ue0)

| 조건 | 슬롯/s | 초당 LPI-3 진입(big / little 코어) | 브로커 p99 (µs) |
|---|---|---|---|
| 기준(6회) | 1,466–1,506 / 1,424–1,456 | 3,832 / 5,586 | 75–80 |
| 브로커 대기 poll 50 → 5 µs(`OCG_BROKER_POLL_US`, 3회) | 1,575–1,612 / 1,527–1,566 | 3,491 / 4,532 | 75–80 |
| OAI RX 대기 100 → 10 µs(`oai-zmq-rx-poll.patch`) | 1,526 / 1,464 | – | 80 |
| 브로커 ZMQ I/O 스레드 2개(`OCG_ZMQ_IO_THREADS`) | 1,451 / 1,394 | – | 80 |
| **20코어 전부 `sched_yield` 스피너**(2회) | 1,816–1,850 / 1,758–1,790 | **0 / 0** | 60–80 |
| 스피너 + 두 poll 수정(2회) | 1,965–1,998 / 1,916 | – | 55–90 |
| 세 프로세스를 big 코어 10개(5–9, 15–19)에 함께 | 1,759 / 1,703 | 2,487 / 956 | 95 |
| **gNB 5–9 / 브로커 15,16 / UE 17–19로 나눠 고정** | **2,077 / 1,995** | 1,024 / 806 | 115–130 |
| 같은 고정 + 두 poll 수정(2회) | **2,060–2,062 / 1,998** | 667 / 647 | 85–185 |
| 같은 나눔을 little 코어에(gNB 0–4 / 브로커 10,11 / UE 12–14) + poll 수정 | 1,522 / 1,378 | 271 / 1,208 | 205–245 |
| CUDA gNB(`all`) + MPS, 고정 없음 | 1,489 / 1,440 | – | 80 |
| **CUDA gNB(`all`) + MPS, 위 big 코어 나눔** | **2,068 / 1,990** | – | 210 |

- 모든 실행 attach·PDU·ping 통과(아래 예외 하나). 고정한 실행에서는 브로커가 실시간 페이싱(`throttle_us` > 0)에 걸린다. 즉 여유가 생겼다.
- **판정:** 원인은 고리의 스레드 전환이 (a) 깊은 idle 상태에서 코어를 깨우는 비용과 (b) little(A725) 코어에서 도는 시간이다. 둘 다 풀어야 실시간이 된다: 코어를 깨워 두기만 하면 +24%(1,830), big 코어로 모으기만 하면 +19%(1,759), little 코어에 모으면 idle 진입은 줄어도 그대로(1,522), **big 코어에 프로세스별로 모으면 실시간**이다. S9의 가설(491 KB × 4번 전송의 바이트 비용)은 주 원인이 아니다. 같은 491 KB 전송이 코어를 깨워 두면 97 µs다.
- **남는 비용:** 브로커를 코어 2개에 가두면 emulator 호출 p99가 80 → 85–185 µs(CUDA gNB 210 µs)로 늘었다. 브로커 워커 스레드 6개와 ZMQ I/O 스레드가 두 코어를 나눠 쓴 탓으로 보이며, 코어 3–4개 배분은 시험하지 않았다. 스피너와 고정을 함께 쓰면 gNB가 진행하지 못해 attach가 실패했다(1회, 측정에서 제외).
- **veth(UE 경로 MTU 1500)는 원인이 아니다:** UE 쪽(veth)과 gNB 쪽(loopback)의 `rx_turn`이 비슷했다(441 vs 469 µs). 그래서 MTU는 시험하지 않았다.
- **권장:** 100 MHz 라이브는 `OCUDU_NATIVE_GNB_WRAPPER` / `_BROKER_WRAPPER` / `_NRUE_WRAPPER`로 big 코어를 나눠 준다(`taskset -c 5-9` / `15,16` / `17-19`). 호스트 쪽 해결(LPI-3 끄기 또는 `pm_qos_resume_latency_us`)은 공유 장비 설정이라 관리자 합의가 필요하다.

### 바뀐 것

- `src/broker.cpp` — 진단 노브 세 개. 기본값에서는 동작이 같다. `OCG_BROKER_POLL_US`(대기 poll 간격, 설정하면 스레드 timer slack 1 µs), `OCG_ZMQ_IO_THREADS`, `OCG_HOP_TRACE_DIR`(홉 트레이스 CSV).
- `scripts/native/run-ocudu-oai-1x1-inner.sh` — `OCUDU_NATIVE_NRUE_WRAPPER`(nrUE 명령 앞에 붙는 래퍼; CPU 고정용).
- `scripts/native/patches/oai-zmq-rx-poll.patch` — OAI RX 대기 poll 간격 노브(`OAI_ZMQ_RX_POLL_US`). S9 패치 위에 적용.
- `scripts/cuda/spark/s10-hop-trace.py`, `s10-zmq-split.cpp`, `s10-yieldspin.c` — 위 분석과 마이크로벤치.

## S11 — 해결책을 게이트 기본값으로, 그리고 S8 재측정 (2026-09-28)

**왜:** S9–S10에서 찾은 해결책(패치한 OAI ZMQ 드라이버, big 코어 나눠 고정, CUDA gNB의 MPS)은 전부 환경 변수로 켜야 했다. 하나라도 빠뜨리면 실행은 통과하면서 0.3×·0.72× 고리를 다시 재게 되고, S8 표가 그렇게 만들어졌다. 그래서 (1) 셋을 게이트 기본값으로 만들고, (2) 그 조건에서 S8 표를 다시 재고, (3) S10에 남은 두 질문 — 브로커 코어 수, 라이브가 벤치보다 느린 이유 — 을 풀었다. 트리 Spark `/workspace/gpuch/zc11`(= `cfba087` + Spark 전용 로컬 수정: x86 workspace lock 검사 생략, legacy 게이트 arch 121·gNB 버전 정규식), 빌드 `builds/gpuch-zc11-release`, 실행 스크립트와 원자료 `/workspace/gpuch/s11/`, 전체 표 워크스테이션 `~/ocudu-work/perf-platform/compare-bw-live-s11.md`. 모든 실행에서 시작·종료 시점 `nvidia-smi`에 다른 GPU 프로세스는 없었고, 실행 뒤 남은 MPS 서버도 없었다.

### 1. 게이트 기본값 (`cfba087`)

OAI 1×1 게이트(`run-ocudu-oai-1x1.sh`)가 아래 셋을 기본으로 적용하고, 실행마다 `reports/.../run-params.json`과 `logs/.../gate-defaults.log`(`event=oai_gate_defaults oai_zmq_module=... platform=... gnb_cpus=... mps=...`)에 실제로 쓴 값을 남긴다.

- **OAI ZMQ 모듈:** `scripts/native/build-oai-zmq-patched.py`가 핀 OAI 소스의 `radio/zmq`를 임시 디렉터리로 복사해 `oai-zmq-module.lock.json`에 적힌 패치(`oai-zmq-tx-reply-poll.patch`, sha256 검사)를 적용하고, release 빌드의 `flags.make`·`link.txt` 그대로 컴파일·링크해 `builds/oai-zmq-patched`에 둔다(`manifest.json` 포함). `-ffile-prefix-map`으로 두 번 빌드해도 바이트가 같다(Spark 모듈 sha256 `c7304857…`). 게이트는 실행 전 `--verify`로 lock 해시, **패치 파일 자체의 sha256**, manifest의 패치 목록, 모듈 바이트를 대조하고 하나라도 다르면 실행을 거부한다(패치 파일에 한 줄 덧붙이면 `does not match the lock`, exit 1 확인). `OCUDU_NATIVE_OAI_ZMQ_MODULE=stock`이면 원본 모듈. `oai-zmq-rx-poll.patch`는 고정 뒤 이득이 없어(S10: 2,060 vs 2,077) 기본에 넣지 않았다. 패치 파일 이름은 그대로다.
- **CPU 배치:** `platform-profile.py`가 호스트를 `platform-profiles.json`과 대조한다(GB10: GPU 이름 `NVIDIA GB10`이고 최고 클럭 코어가 5–9, 15–19). 맞으면 inner 스크립트가 gNB·브로커·nrUE를 `taskset`으로 고정하고, 맞는 프로파일이 없으면(워크스테이션 5090에서 확인) 아무것도 하지 않는다. `OCUDU_NATIVE_PLATFORM=none`으로 끄고, 역할별 `OCUDU_NATIVE_{GNB,BROKER,NRUE}_CPUS`가 프로파일보다 우선한다. 실행 중 샘플한 실제 affinity가 요청과 같았다(`runs/*/affinity.txt`).
- **MPS:** gNB 바이너리가 CUDA를 링크하면(`ldd`에 `libcudart`/`libcuda`) 게이트가 자신을 `with-cuda-mps.py` 아래에서 다시 실행한다. `OCUDU_NATIVE_MPS=off|on`. 실행 도중 래퍼에 SIGTERM을 보낸 대조에서 gNB·UE·5GC·mongod·브로커가 모두 내려가고 MPS 데몬이 `quit`로 종료됐다(`daemon_quit: true`, 남은 `nvidia-cuda-mps-*` 0).
- **대역폭 렌더러:** `render-1x1-bw-configs.py`가 20 MHz 외 대역폭에서 nrUE 무선 인자(`nrue-radio.args`)와, 기본 capability에 그 대역폭이 없을 때(50 MHz) `uecap.xml`을 함께 쓰고 게이트가 그것을 읽는다. S10 러너에서 50 MHz attach가 실패한 원인(capability 파일을 넘기지 않음)이 이것으로 없어졌다.
- 워크스테이션 `oai-2x2` 브랜치의 OAI 1×1 하네스 수정 두 커밋(`0d9edb7` 기준 앵커·버전 정규식·pgid 경쟁·UE 작업 디렉터리, `31ce47b` 브로커 경로)을 이 브랜치로 가져왔다(`02b796d`, `f9c8960`).

### 2. S8 재측정 — 기본값만으로 (게이트 24회 + srsUE 1회, 전부 통과)

UE 노드 기준, 같은 대역폭의 S8 값과 비교(전체 표는 `compare-bw-live-s11.md`):

| MHz | 슬롯/s S8 → S11 (실시간) | starvation S8 → S11 | zero-copy p50/p99 S8 → S11 | copy p50/p99 S8 → S11 | zero-copy emulator 호출 S8 → S11 |
|---|---|---|---|---|---|
| 20 | 262–267 → 1,000 (1,000) | 3,370–3,399 → 1 | 40/80 → 35/45 | 60/100–105 → 50/80–85 | 34–36 → 31–32 µs |
| 30 | 260–263 → 1,000 | 3,347–3,391 → 1 | 40/80 → 35/45–50 | 60–70/110 → 55/70 | 33–34 → 32–33 |
| 40 | 254–258 → 1,000 | ~3,300 → 1–2 | 40–45/85 → 35/50 | 75/120 → 70/85–100 | 34–37 → 33–34 |
| 50 | 253–254 → 999–1,000 | ~3,260 → 1 | 45/85 → 40/100–120 | 70–90/130–140 → 75–80/115–160 | 41–42 → 38 |
| 100 | 339–349 → 1,981–1,999 (2,000) | ~3,000 → 1–3 | 45/85 → 40/130–135 | 90/135–140 → 80/205–210 | 41 → 39–40 |
| 100, CUDA gNB + MPS | 실시간 미만 → 1,992–1,997 | ~7,000 → 5 | 45/185–195 → 40/130–135 | 90–95/230–235 → 80/205–210 | – → 38–39 |

- **바뀐 것:** 전 대역폭이 실시간이 됐고 starvation이 사라졌다(S8의 수천 건은 느린 고리의 증상이었다). p50은 big 코어 고정으로 약 5 µs 내려갔다. CUDA gNB의 p99는 MPS로 CPU gNB와 같아졌다.
- **새로 드러난 것:** 100 MHz의 p99가 S8보다 커졌다(zero-copy 85 → 130–135, copy 135–140 → 205–210). 실시간이 되면서 브로커가 초당 호출을 약 6배 처리하는데, S10 배치는 브로커에 코어 2개만 줬기 때문이다 → 아래 3. 50 MHz ue0 p99 100–120도 같은 배치(2코어)에서 잰 값이다.
- srsUE 20 MHz 회귀(게이트 무변경, 기본 `auto` = zero-copy, 고정 없음): 통과, 969 슬롯/s, starvation 1, p50/p99 40/75.

### 3. 브로커 코어 수 (4a)

**시도:** 100 MHz, zero-copy, big 코어 10개 안에서 브로커에 2·3·4·5코어를 주고 UE 몫을 줄였다(CPU gNB 2회씩, CUDA gNB + MPS 2회씩, 교차).

| 배치 gNB / 브로커 / UE | 슬롯/s ue0 | p99 gnb0 / ue0 (CPU gNB) | p99 (CUDA gNB) |
|---|---|---|---|
| A 5–9 / 15,16 / 17–19 (S10) | 1,997–1,999 | 115 / 130–135 | 115 / 130–135 |
| **B 5–9 / 15–17 / 18,19** | 1,998–1,999 | **65 / 65** | **65 / 65** |
| C 5–9 / 15–18 / 19 | 1,986 | 70 / 65–70 | 70 / 65 |
| D 5–9 / 15–19 / 0–4 (UE little) | 1,786–1,796 | 65 / 65 | – |

**어디서 왜:** p50(40 µs)과 emulator 호출 중앙값(37–40 µs), GPU 단계는 A와 B가 같고 꼬리만 달랐다. 그래서 브로커 스레드의 실행 대기를 쟀다(`/proc/<pid>/task/*/schedstat`, 5 s). A에서 워커 스레드 7개가 초당 0.43 코어·초, ZMQ I/O 스레드가 0.09 코어·초를 runqueue에서 기다렸고(문맥 전환당 11.9 µs, ZMQ I/O 4.7 µs), B에서는 0.11·0.02(3.2 µs, 1.2 µs)였다. 브로커가 실제로 쓰는 CPU는 둘 다 약 1.1코어인데, 워커들이 50 µs poll로 자주 깨어나고 ZMQ I/O가 0.5코어를 쓰니 2코어에서는 emulator를 부르는 스레드가 순서를 기다린다.

**결과:** 프로파일을 B로 바꿨다(`87ac35c`). UE는 big 코어 2개가 필요하다. 1개(C)나 little 코어(D)면 실시간 아래로 내려간다. 바꾼 뒤 환경 변수 없이 돌린 확인 실행: CPU gNB 1,999 슬롯/s·p99 65/65, CUDA gNB(MPS 자동) 1,998·65/65.

### 4. 라이브 emulator 호출이 벤치보다 긴 이유 (4b)

**배경:** Z8 라이브에서 H2D 7–8 µs(벤치 3), 커널 23–24 µs(벤치 17)였다. 이제 배치를 통제한 라이브(20 MHz, zero-copy)는 H2D 4.0–4.5, 커널 19.4–20.0, 호출 31–32 µs로 줄었지만 벤치보다 여전히 컸다.

**시도:** 라이브와 벤치가 다른 점 세 가지를 벤치에 측정용 노브로 넣었다(`87ac35c`, 기본 꺼짐): `OCG_BENCH_PACE`(1 ms 간격 호출), `OCG_BENCH_REFRESH`(매 슬롯 CPU가 입력을 새로 씀; 벤치는 같은 버퍼를 계속 재사용), `OCG_BENCH_THREADS`(노드별 스레드 동시 실행). 라이브 실행의 토폴로지 파일 그대로, 브로커 코어 15,16, 10 s × 3회 교차.

| 조건 (zero-copy, gnb0) | H2D | 커널 p50 | 호출 p50 |
|---|---|---|---|
| 벤치 기본 | 3.2 | 17.8 | 26.9 |
| + 1 ms 간격 | 3.2 | 18.1 | 27.3 |
| + 입력 새로 씀 | 3.2 | **19.4** | 28.5 |
| + 노드 스레드 동시 | 3.8 | 18.1 | 27.8 |
| 셋 다 | 3.5 | **20.5** | **30.3** |
| 라이브(고정) | 4.0–4.5 | 19.4–20.0 | 30.9–31.9 |
| 셋 다, little 코어 0,1에서 | 5.5 | **22.8–23.3** | 35.5–36.3 |

- **입력을 CPU가 막 썼다는 것(+1.6 µs 커널)**이 가장 크다. GPU가 CPU 캐시에 남은 새 데이터를 읽는 비용으로, S9 3번(방향이 반대인 같은 현상)과 같은 GB10 일관성 비용으로 본다. zero-copy라서 생기는 비용이고 copy 모드 커널에는 없다(copy: 기본 13.4, 셋 다 13.3, 라이브 14.3–15.4는 스레드 동시 실행 14.4로 설명).
- **little 코어가 예전 격차의 나머지다.** 같은 조건을 little 코어에서 돌리면 커널 23 µs, H2D 5.5 µs로 예전 라이브(Z8 23–24 / 7–8, 오늘 고정 없는 srsUE 게이트 23.7 / 7.6)와 같다. `kernel_us`는 `h2d_done` → `kernel_done` 이벤트 사이인데 그 사이에 CPU가 커널 4–6개를 차례로 올린다(`cuda_backend.cu` 1360–1436). 커널이 짧아 GPU가 다음 실행을 기다리므로 CPU가 느리면 "커널 시간"이 늘어난다. 이 부분은 플랫폼 프로파일로 이미 없어졌다.
- 1 ms 간격(유휴 뒤 GPU 재기동)은 +0.3 µs로 작다. 200 ms 샘플의 SM 클럭은 모든 조건에서 2,496–2,548 MHz였다.
- **남은 차이:** 라이브 H2D가 벤치(셋 다)보다 0.5–1 µs 크다. 라이브 브로커에는 ZMQ I/O와 puller 스레드가 같은 코어에 있다. 따로 가르지 않았다.
- 참고: 브로커와 벤치는 호출 뒤 `last_timings()`를 읽는데 이 값은 프로세서 전체에 하나라서, 노드 스레드가 동시에 돌면 다른 노드의 값을 읽을 수 있다. 두 노드의 값이 비슷해 중앙값 판정에는 영향이 없다고 봤다.

### 남은 것

- 호스트 idle 상태 설정(LPI-3)은 여전히 공유 장비 합의가 필요하다. 지금은 CPU 배치만으로 실시간이다.
- 50 MHz ue0 p99(100–120 µs)는 2코어 배치에서 잰 값이다. B 배치로 다시 재지 않았다.
- 워크스테이션에서 패치 모듈을 쓰려면 거기서 `build-oai-zmq-patched.py`를 한 번 돌려야 한다(x86 release 빌드의 플래그를 그대로 쓴다). 워크스테이션에서는 프로파일이 맞지 않아 CPU 배치가 적용되지 않는다(5090 프로파일은 따로 정해야 한다).

- **통합 후 이름 (2026-09-28, `integration-0928`):** S11의 `oai-zmq-module.lock.json`은 `oai-local-patches.lock.json`의 `zmq_module` 항목이 됐고(UE 패치도 같은 파일), 게이트 기본값 코드는 `oai-gate-defaults.sh`로 옮겨 OAI 2×2 게이트도 같이 쓴다. 빌드 manifest 필드가 바뀌었으므로 Spark의 `builds/oai-zmq-patched`는 `build-oai-zmq-patched.py`로 한 번 다시 빌드해야 한다. 자세한 대응표는 `docs/plans/m6-rank2-su-mimo-live.md` §8.8.

## S12 — integration-0928 조합 검증 (2026-09-28)

**왜:** 두 브랜치(`gb10-zero-copy`, `oai-2x2`)를 합친 `integration-0928`이 GB10에서 한 번도 라이브로 돌지 않았다. 특히 OAI 2×2(rank 2)는 워크스테이션 5090의 copy 경로에서만 돌았고, zero-copy·CUDA gNB·넓은 대역과의 조합은 없었다. 트리 Spark `/workspace/gpuch/int0928`(브랜치 `s12-spark`), 빌드 `builds/gpuch-int0928-release`, 원자료 `/workspace/gpuch/s12/runs/`, 표 워크스테이션 `~/ocudu-work/perf-platform/compare-s12.md`. 모든 실행에서 시작·종료 `nvidia-smi`에 다른 GPU 프로세스는 없었다.

**게이트 정리(코드):** Spark 트리에 늘 남기던 미커밋 수정 3개(x86 workspace lock 생략, sm_121, 26.04 배너)를 multi-gNB 게이트의 `OCUDU_NATIVE_SKIP_WORKSPACE_LOCK`·`OCUDU_NATIVE_CUDA_ARCH`로 옮겼다(`30563f3`). 2×2 게이트가 `OAI2X2_BW_MHZ`·`OAI2X2_CUDA_HOST_MEMORY`(1×1 대역폭 렌더러의 `apply_bandwidth`를 공유, 1×1 렌더 결과는 바이트 동일)와 `OCUDU_NATIVE_GNB_ACCELERATION`(CUDA gNB)을 받는다(`48019eb`, `3d53608`). 2×2 요약기의 실시간 비율이 30 kHz 셀에서 20배 틀리던 것을 고쳤다(프레임당 20슬롯).

**1. prepare() 스트림 순서 수정(GB10):** 다른 프로세스에서 1블록 busy 커널을 돌리며 `test_matrix_profile_history`·`test_processing`을 직접 실행했다. 수정 전 빌드(zc9) **10/10 실패**, 수정 후 0/10. 유휴 GPU에서는 둘 다 통과. ctest 12/12와 9단계 시퀀스는 유휴·경쟁 두 조건 모두 통과.

**2. 1×1 회귀:** OAI 1×1, srsUE 1×1 모두 통과(실시간, p50/p99 35/55 µs).

**3. 2×2 rank 2, 20 MHz — 여기서 결함이 드러났다.**
- **증상:** 첫 실행(패치 UE, OCUDU 기본 12 dB 백오프, zero-copy)에서 attach·PDU·ping은 통과했지만 PDSCH NACK 89%, iperf 0.5 Mb/s, RLF 1회. 워크스테이션의 같은 조건은 118 Mb/s였다.
- **추적:** (a) 같은 실행의 wire capture에서 y=Hx 통과(오차 8e-10) → 채널은 정상. (b) 24 dB에서는 원본·패치 UE 모두 NACK 0 → 레벨 문제. (c) rank 1(12 dB)도 NACK 69% → 패치한 2레이어 MMSE가 아니라 공통 경로. (d) 같은 gNB에 srsUE는 NACK 0 → gNB는 정상. (e) Spark의 OAI 1×1(12 dB)도 NACK 89%, **워크스테이션의 OAI 1×1도 78%** → aarch64 전용이 아니고, OAI 1×1 게이트는 ping만 봐서 통과로 보였다(S8·S11의 OAI 결과도 같은 상태였다). (f) 백오프 스윕: rank 1 NACK 12 dB 0.69 · 15 dB 0.43 · 18 dB 0.010 · 21 dB 0.007 · 24 dB 0, rank 2도 같은 경계. (g) 계측 UE: 채널 보상 뒤 int16 포화 없음(최대 5–7k), 채널 추정 매끈함, OAI FFT 단독 시험은 입력 61 dB까지 SNR이 오르고 포화 없음, 캡처 최대 샘플 0.30(full scale 대비). 그런데 추출한 RE를 float MRC로 등화해도 64QAM EVM이 0.86–0.91(24 dB는 0.001). (h) gNB는 그대로 두고 **UE의 ZMQ 입력만 −12 dB로 줄이면 NACK 69% → 0, 9.7 → 74.1 Mb/s.**
- **판정:** OAI UE 수신 체인이 입력 레벨에 따라 망가진다. ZMQ 라디오에는 AGC가 없어서 gNB가 보낸 레벨(약 55 dB 디지털 전력)이 그대로 들어오고, OAI의 목표 레벨 `TARGET_RX_POWER`는 50 dB다. FFT 뒤 어느 단계가 망가지는지는 좁히지 못했다.
- **조치(로컬 패치):** `oai-zmq-rx-gain.patch`가 ZMQ 라디오에 `zmq.[n].rx_gain_db`(기본 0, 변화 없음)를 더한다. 무선 장비의 RF 수신 이득 역할이다. OAI 1×1·2×2 게이트가 패치 모듈일 때 −12 dB를 기본으로 넘기고 기록한다(`OCUDU_NATIVE_OAI_UE_RX_GAIN_DB`로 변경, 0이면 끔). 점검 스크립트의 `--probe`에 0 dB(NACK 0.89) vs −12 dB(0.0) 대조를 넣었다. 먼저 의심한 aarch64 RX 변환의 비포화 절단은 실제 결함이지만 이번 원인이 아니었다(`oai-zmq-rx-saturate.patch`, 잠재 결함으로 유지).
- **결과(기본값, unitary H):** copy·zero-copy 2쌍 전부 rank 2 99.9%, NACK 0, 130–132 Mb/s(실시간 0.88–0.89 → air 148 Mb/s), y=Hx 통과. zero-copy가 브로커 p50/p99 70/90 → **50/70 µs**, emulator 호출 68 → 50 µs. 덜 깨끗한 H(reference)도 rank 2 99.9%, NACK 0 — 워크스테이션에서 보던 RI 흔들림(28 dB)은 이 레벨에서는 나오지 않았다.
- **남은 것:** 20 MHz 2×2도 iperf 중에는 실시간 0.88–0.91이다. UE가 코어 2개(18,19)를 다 쓴다(179%).

**4. CUDA gNB + 2×2(MPS 자동):** `all` 가속에서 NACK 45%, 실시간 0.30, iperf 실패. 가속 단계를 누적으로 나눠 보니 disabled·lower-PHY·PUSCH까지는 CPU gNB와 같고(NACK 0, 132–134 Mb/s), **PDSCH 가속을 켜는 순간** 망가진다: rank 2 NACK 5.5%(87 Mb/s), **rank 1 NACK 45%**. GPU 그리드 매핑(`OCUDU_PDSCH_DISABLE_DEVICE_MAP=1`)을 꺼도 같다. 2포트 셀에서 1레이어를 두 포트에 싣는 경로(precoding)가 의심되지만 확인하지 않았다. 1포트 셀의 CUDA gNB(S7, S11)는 정상이었다. **미해결.**

**5. 넓은 대역 2×2(zero-copy, 100 MHz는 copy도):** 전부 attach, NACK 0, y=Hx 통과. 부하(200M) 중 실시간 비율은 40 MHz 0.47, 50 MHz 0.51, 100 MHz 0.24–0.25(air-time 환산 304 / 234 / 452–474 Mb/s). 50 MHz는 rank 2 비율이 21%로 떨어졌다(원인 미확인). 부하 없이는 40 MHz도 실시간(브로커 1,000–1,036 배치/s). 부하 중 UE는 코어 2개로 179%, 3개로 230%를 쓰고 실시간은 0.52–0.59까지만 오른다. gNB를 코어 3–4개로 줄이면 시작하지 못했다. 즉 넓은 대역 2×2의 실시간은 CPU 예산 문제이고, 플랫폼 프로파일(1×1 기준)을 2×2용으로 다시 나눠야 한다 — 미해결.

**6. 패치 점검(`check-oai-local-patches.sh --probe`, Spark):** 정적 11/11, probe 5/6. 실패 1건은 ZMQ reply-poll의 "패치 후 > 0.9" 기준이었다. 이 probe는 2×2에 200M 부하를 거는데, Spark의 2코어 UE는 패치 후에도 0.89에 머문다(원본 0.29). 기준을 0.7로 낮췄다.

**Spark 상태:** gate 프로세스·MPS·GPU 앱 없음. `iperf3`를 컨테이너에 설치했다(2×2 게이트 필요). 계측 UE 트리 `src/oai-s12dbg`·빌드 `builds/oai-s12dbg`는 남겨 두었다.

## S13 — CUDA gNB PDSCH 결함(D10), 넓은 대역 2×2, 50 MHz rank 2 (2026-09-28)

**왜:** S12에서 세 가지가 미해결로 남았다. (1) CUDA gNB의 PDSCH 가속을 켜면 2포트 셀에서 rank 1 NACK 45%(rank 2 5.5%), (2) 40–100 MHz 2×2가 부하 중 실시간 0.24–0.59, (3) 50 MHz 2×2의 rank 2 비율 21%. 트리 Spark `/workspace/gpuch/int0928`(`integration-0928`), 실행 스크립트·원자료 `/workspace/gpuch/s13/`, 요약 `s13-summary.txt`, 표 워크스테이션 `~/ocudu-work/perf-platform/compare-s13.md`. 모든 실행 시작 시 `nvidia-smi`에 다른 GPU 프로세스가 없었다. CUDA gNB 실행은 모두 게이트 기본 MPS(`mps=on`, MPS 서버 1개)로 돌았다.

### 1. D10 — GPU TB 인코더가 filler 0을 무시했다 (해결)

- **처음 드러난 곳:** 2×2 게이트(20 MHz, unitary H, zero-copy)에 CUDA gNB(PDSCH 가속까지 누적)를 붙이면 rank 1에서 NACK 45%, iperf 1.5 Mb/s.
- **첫 가설(그리드·매핑 경로)은 틀렸다:** 환경 변수로 경로를 하나씩 끈 6회(rank 1 강제, 8 s iperf)가 전부 NACK 0.450–0.452였다 — direct CUDA-visible 그리드(기본), sidecar 그리드(`OCUDU_PDSCH_DIRECT_DEVICE_GRID=0`, 로그에서 경로 전환 확인), GPU 디바이스 매핑 끔, direct-grid encode 끔, 인코드 캐시 끔, host TB CRC. S12의 "매핑을 꺼도 같다"는 그리드 경로가 바뀌지 않은 실행이었다(로그상 direct 경로 그대로). 매퍼 가드도 코드상 정상이다(PRG 1개·가중치가 모두 같은 실수일 때만 디바이스 경로).
- **실패의 모양:** gNB 로그에서 HARQ 재전송(rv>0)이 뒤따른 새 전송을 실패로 세면, 실패는 106 PRB 64QAM 할당 중 **TBS 9,474 B(9 CB)에서만** 났다(슬롯 2·3에서 61–75%, 다른 슬롯 0%). 같은 할당의 10,247 B(10 CB)는 전부 성공. 두 TB의 차이는 LDPC filler다: 9,474 B는 C=9, Z=384, K=8448, **F=0**, 10,247 B는 C=10, F=224.
- **오프라인 재현:** 벤더의 `pdsch_gpu_e2e_test`(CPU·GPU 자원 그리드 비교)는 45개 케이스가 예약 RE(CSI-RS 모양)·PMI 코드북 가중치를 넣어도 전부 통과했다. TBS를 강제로 9,474 B로 주면 **1포트·2포트 모두 불일치**, 10,247 B는 둘 다 일치. 즉 2포트 문제가 아니라 TB 크기 문제이고, 1포트 셀(S7, S11)도 같은 TB가 나오면 틀린다 — 그 게이트들은 ping만 봤다.
- **원인:** `lib/phy/cuda/src/transport_block.cu`의 `tb_encoder_configure`와 `tb_batch_encoder_configure`는 CPU 세그멘터가 넘긴 모양(BG, Z, C, F)으로 GPU 설정을 덮어쓰는데, `nof_filler_bits > 0`일 때만 F를 덮었다. F=0은 "주어지지 않음"으로 보고 GPU가 스스로 계산한 F를 남겨, K = K' − F가 틀린 채 부호화했다.
- **수정(D10):** CPU가 모양을 주면(Z > 0) F를 0까지 포함해 그대로 따른다. 두 곳 모두. `scripts/cuda/patches/s-d10-tb-encoder-zero-filler.patch`(d8/d9 위의 D10 단독), 결합 패치 `s-c1-d8-d9-d10.patch`, lock `cuda-workspace.spark-d10.lock.json`(체크아웃 `src/ocudu-cuda-d10`, 빌드 `builds/d10-cuda-patched-sm121`, `resolve-cuda-gnb.py` 감사 통과). 회귀 케이스: `pdsch_gpu_e2e_test`에 강제 TBS 9,474 B(1포트·2포트)와 10,247 B(F>0 대조)를 추가.
- **음성 대조군:** D10 트리에서 `transport_block.cu`만 d8 것으로 되돌리면 정확히 9,474 B 두 케이스만 실패(46 통과), 되돌린 걸 원복하면 48/48(전체 스위트 60/60).
- **라이브(20 MHz 2×2, unitary H, zero-copy, 게이트 기본값):**

| 실행 | gNB | rank | NACK | DL (air) | 실시간 |
|---|---|---|---|---|---|
| l1 | CUDA d10 | 2 | 0 | 148.22 Mb/s | 0.87 |
| l2 | CPU | 2 | 0 | 148.16 | 0.86 |
| l3 | CUDA d10 | 1 | 0 | 74.11 | 1.00 |
| l4 | CPU | 1 | 0 | 74.11 | 1.00 |
| l5 | CUDA d10 | 2 | 0 | 148.16 | 0.88 |
| l6 (대조) | CUDA **d8** | 1 | **0.45** | — | 0.39 |

  CUDA gNB가 CPU gNB와 같아졌다. l1의 meta 후처리는 실행 중에 러너를 고쳐서 깨졌지만 게이트 자체와 요약은 정상이다.
- **범위:** D10은 PDSCH TB 인코더 결함이다. D8/D9와 같은 WG1 CUDA 코드(`5830c9cb`) 계열이므로 Jetson(`j2c`)과 워크스테이션 C1 빌드에도 같은 결함이 있다. → 2026-09-28 적용 완료: 워크스테이션 C1+D10(`CUDA_MILESTONES.md` D10 절, lock `cuda-workspace.c1-d10.lock.json`), Jetson J2d(`JETSON_MILESTONES.md` J9, lock `cuda-workspace.jetson-d10.lock.json`). 두 곳 모두 수정 전 결함을 재현했다.

### 2. 넓은 대역 2×2 실시간 — 코어 배치로는 안 풀린다 (구조 한계, 미해결)

- **배치 시도(100 MHz, CPU gNB, zero-copy, 200M 15 s):** 기본 프로파일 0.257, UE 큰 코어 6개(gNB 0–6) 0.296, UE 5개 + 브로커 3개 0.316, gNB를 큰 코어 4개로 줄이면 시작 실패(S12와 같음). UE 스레드는 각 15–26%로 포화된 것이 없다. 2×2 프로파일 변형은 이득이 작아 추가하지 않았다.
- **부하와 무관:** iperf 1M(거의 무부하)에서도 0.58(1×1 100 MHz는 실시간).
- **홉 분해(브로커 `OCG_HOP_TRACE_DIR`, 포트 0 기준, 메시지 = 61,440샘플 = 0.5 ms, 실시간 = 2,000 msg/s):**

| 구간 (p50, µs) | 부하 | 무부하 |
|---|---|---|
| 브로커 produce(채널 처리, 2포트) | 183 / 185 | 182 / 183 |
| 생산 스레드 깨어남 gNB / UE | 81 / 191 | 80 / 195 |
| 장치 턴어라운드 rx_turn gNB / UE | 418 / 495 | 410 / 489 |
| DL / UL 중계 | 493 / 376 | 490 / 371 |
| 처리율 | 1,111 msg/s | 1,245 msg/s |

  리드(TX pulled − RX served)는 0–1 메시지로 파이프라이닝이 없어, 한 바퀴(중계 + 장치 턴어라운드)가 0.8–0.9 ms 걸린다. 2포트라 메시지 바이트가 1×1의 2배(방향마다 포트 2개 × 491 KB)이고 브로커 produce만 183 µs다. **판정:** CPU 배치 문제가 아니라 lock-step 고리의 직렬 지연이다. 줄일 후보는 브로커 run-ahead(파이프라이닝), 2포트 produce(183 µs) 단축, 장치 턴어라운드 — 모두 구조 변경이라 이번에 하지 않았다. DL actor 8개 시도는 인자 공백 때문에 실행되지 않았다(스레드 비포화라 우선순위 낮음).

### 3. 50 MHz 2×2 rank 2 21% — UE가 부하 중 RI=1을 보고 (원인 미확정 → S14에서 규명)

- S12의 50 MHz 실행을 다시 보면 CSI 보고 3,601건 중 RI=2가 90%인데 스케줄 결정은 rank 2가 21%(1,617/7,815)다. 시간별로 나누면 **무부하 구간 CSI는 전부 RI=2**, iperf 부하 20 s 동안에는 RI=1이 190/264, 122/343이다. 스케줄러는 최신 RI를 그대로 따랐다(모든 슬롯 같은 비율).
- 100 MHz(n78 TDD 30 kHz, 부하)에서는 rank 2 99.8%, 20 MHz도 99.9% — 50 MHz(n3 FDD 15 kHz, 270 PRB)만이다. gNB 설정은 대역폭 외 차이가 없다(CSI는 OCUDU 기본).
- **후보(미확인):** 부하 중에만 떨어지므로 PDSCH가 CSI-IM/ZP-CSI-RS 영역과 겹쳐 UE의 간섭 측정이 오르는 경우(270 PRB에서 기본 CSI 자원 대역폭 설정), 또는 OAI UE의 RI 추정. 설정이 INFO 로그에 안 찍혀 이번엔 가르지 못했다. MAC pcap/RRC 덤프로 CSI 자원 대역을 확인하는 것이 다음 단계.

**Spark 상태:** 게이트 프로세스·MPS·GPU 앱 없음, 이 트랙의 tmux 세션 모두 종료. 새 체크아웃 `src/ocudu-cuda-d10`·빌드 `builds/d10-cuda-patched-sm121`. d8 빌드의 `pdsch_gpu_e2e_test` 바이너리는 진단 중 환경 변수 노브를 넣어 다시 빌드한 것이다(소스는 원복, lock 감사 대상인 gNB 바이너리·소스 diff는 그대로).

## S14 — 50 MHz 2×2 rank 2 비율의 원인 (2026-09-28)

**배경.** S12·S13에서 50 MHz(n3 FDD, 15 kHz, 270 PRB) OAI 2×2만 부하 중 rank 2가 21%였다. 20 MHz와 100 MHz는 99.8% 이상이다. S13은 이것을 "부하 중 UE가 RI=1을 보고"까지 좁혔고, 후보로 PDSCH와 CSI-IM/ZP-CSI-RS의 겹침(간섭 측정 상승) 또는 OAI UE의 RI 추정을 남겼다. 이번 트랙은 그 둘을 가르고 고치는 것이다. 모든 실행은 `integration-0928` Spark 트리(`/workspace/gpuch/int0928`, `c786ad8` 기준), 게이트 기본값(패치 ZMQ 모듈, 로컬 UE, big 코어 배치), unitary H, zero-copy, CPU gNB다. 실행마다 다른 GPU 프로세스는 없었다(`s14/runs/*/meta.txt`).

**1. 재현과 조건 좁히기 (gNB 로그만으로).**

| 조건 (50 MHz) | 부하 중 CSI 보고(ACK와 다중화된 PUCCH F2) RI=2 / 전체 | 스케줄 rank 2 비율 |
|---|---|---|
| iperf 200M (2회) | 74 / 369, 73 / 368 | 0.20 |
| iperf 120M | 74 / 369 | 0.20 |
| iperf 60M | 460 / 460 | 1.00 |
| iperf 20M | 525 / 525 | 1.00 |
| 200M + UE 코어 7개 (2회) | 74 / 370, 72 / 364 | 0.20 |
| 200M + MCS 상한 20 | 73 / 365 | 0.20 |
| 200M + gNB 백오프 24 dB | 25 / 394 | 0.07 |
| 200M + UE RX gain −24 / −6 dB | 25 / 389 · 93 / 355 | 0.07 · 0.27 |

- RI=1은 부하 구간의 보고에서만 나온다(부하 중 보고는 전부 HARQ-ACK와 다중화된 F2, 무부하 보고는 CSI 단독 F2로 전부 RI=2). 같은 다중화 보고가 20·100 MHz에서는 RI=2라서 다중화·PUCCH 복호 문제는 아니다.
- 60M 이하에서는 PDSCH가 42–84 RB로 부분 대역이고, 120M 이상에서는 CSI-RS 슬롯(짝수 프레임 슬롯 2)에도 270 RB 전 대역이다. **전 대역 PDSCH가 있는 CSI-RS 슬롯의 측정만** 망가진다.
- UE 코어 수와 MCS는 영향이 없어 UE CPU 부족이나 TB 크기는 아니다. 레벨을 낮추면 더 나빠지고 올리면 조금 나아진다(나중에 보니 스택 잔여값의 크기에 따른 부수 효과).

**2. gNB 송신은 깨끗하다 (wire capture).** 부하 중 40 ms를 브로커에서 캡처(`c50-load`, skip 10 s)해 워크스테이션에서 OFDM 격자로 풀었다(`ofdm.py`).
- CSI-RS(슬롯 2, 심볼 4, RB당 2 RE, 540 RE)의 두 포트는 fd-CDM2 관계(포트1 = 포트0 · [+1, −1])를 **오차 1.5e−4**로 지킨다. PDSCH가 CSI-RS RE를 침범하지 않는다(레이트 매칭 정상). 처음 본 "오염"은 내 FFT 창이 한 심볼 어긋난 분석 착오였고, 창을 CP 안으로 맞추자 사라졌다.
- 같은 캡처로 y=Hx를 확인하면 DL 두 행 모두 최대 오차 1.3e−7이다. **UE 입력은 정확히 Hx**이고 잡음도 없다. 그러므로 원인은 UE 안이다.

**3. UE 안에서 찾기 (계측 UE).** 로컬 UE 패치 위에 계측만 더한 `src/oai-s14dbg`(빌드 `builds/oai-s14dbg`)를 `OAI2X2_NRUE_DIR`로 붙였다. RI 추정(`nr_csi_rs_ri_estimation`)에서 RB 구간별 조건수 투표, 조건수 분포, 세 RB의 추정 채널과 det/numer를 찍었다.
- 무부하: 조건수 3,240 RE 전부 0–4 dB, count +3,240 → RI=2. 부하(전 대역 PDSCH): 5–15 dB 이상이 대부분, count −1,400 ~ −2,700 → RI=1. 망가짐이 RB 구간에 고르게 퍼져 있다(국소적 충돌이 아님).
- 결정적 증거: **추정 채널 H는 무부하와 부하가 같다**(예: h00 ≈ (1672,164)/(1680,104), h01 ≈ (−652,−448)/(−672,−424)). 그런데 같은 H에서 계산한 det/numer가 무부하 2,818,929 / 2,819,155(조건수 0 dB)에서 부하 7,213,069 / 33,658,662, 250,142,562 / 567,637,577로 제멋대로다. 무부하에서도 호출마다 값이 4배로 늘었다(2.8e6 → 11.3e6) — 누적이다.
- **원인:** `csi_rs_estimated_A_MF`(HᴴH를 담는 스택 VLA)를 0으로 초기화하지 않고, `nr_a_sum_b()`가 그 위에 `x += y`로 더한다(`openair1/PHY/NR_UE_TRANSPORT/csi_rx.c`, 핀 `2b69bde6`의 `nr_csi_rs_ri_estimation`). 시작값이 같은 스레드에서 직전에 돈 처리(부하 중에는 PDSCH 처리)의 스택 잔여값이라, 전 대역 PDSCH가 있으면 조건수가 무작위가 된다. 20 MHz도 부하 중 조건수 분포가 절반쯤 흐려져 있었지만(705/1,272 RE만 0–4 dB) 과반을 넘겨 RI=2였고, 100 MHz도 우연히 넘는다 — 대역폭별 차이는 스택 배치의 우연이다.

**4. 수정과 확인.** `scripts/native/patches/oai-csi-ri-amf-init.patch`(sha256 `ade6931e…`): 누적 전에 `memset(csi_rs_estimated_A_MF, 0, sizeof(...))` 한 줄. `oai-local-patches.lock.json`의 UE 패치 목록에 추가해 `build-oai-ue-local.sh`가 MMSE 패치와 함께 적용한다(Spark `builds/oai-zmq-local` 재빌드, nr-uesoftmodem `d390f09b…`). 이전 UE(`58f68c88…`, MMSE 패치만)는 A 쪽 대조로 `builds/oai-zmq-local-s13`에 복사해 두었다.

| 실행 (50 MHz, 200M, 12 dB) | UE | rank 2 비율 | 부하 중 CSI RI=2 | NACK | DL air | 실시간 |
|---|---|---|---|---|---|---|
| ab-prev-1 | 이전 | 0.207 | 75 / 372 | 0 | 234.2 Mb/s | 0.48 |
| ab-new-1 | 패치 | **0.998** | 276 / 276 | 0 | **360.0** | 0.39 |
| ab-prev-2 | 이전 | 0.207 | 75 / 371 | 0 | 234.3 | 0.48 |
| ab-new-2 | 패치 | **0.998** | 276 / 276 | 0 | **359.2** | 0.39 |
| new-20 (20 MHz) | 패치 | 0.999 | 656 / 656 | 0 | 148.1 | 0.85 |
| new-100 (100 MHz) | 패치 | 0.998 | 155 / 170 | 0 | 435.5 | 0.26 |
| stock-bo24 (24 dB) | stock | 0.069 | — | 0.001 | 204.9 | 0.52 |
| new-bo24 (24 dB) | 패치 | 0.998 | — | 0.006 | 352.9 | 0.38 |

- 50 MHz 실시간 비율이 0.48 → 0.39로 내려간 것은 rank 2로 UE 복호 부하가 늘어서다(S13의 구조 한계와 같은 원인, 이 패치와 무관).
- 실시간 여부와의 관계: 원인이 스택 잔여값이라 실시간 여부와 무관하다. 부하가 낮으면(60M 이하) CSI-RS 슬롯의 PDSCH가 부분 대역이라 잔여값이 달라 드러나지 않았을 뿐이다.
- **반복 점검:** `check-oai-local-patches.sh` 정적 점검 15/15 통과(새 패치의 sha256·핀 적용·로컬 UE manifest 포함). `--probe`에 "CSI RI init" 항목을 추가했다: 50 MHz, 24 dB, 200M에서 stock UE는 rank 2 비율 < 0.50(실측 0.069), 로컬 UE는 > 0.90(실측 0.998)이어야 한다. 이번에 probe 전체는 돌리지 않았고, 두 문턱은 위 stock-bo24·new-bo24 실행으로 확인했다.
- **다른 장비 주의:** lock의 UE 패치 목록이 바뀌어 워크스테이션·Jetson의 `builds/oai-zmq-local`은 manifest가 맞지 않는다. 게이트가 로컬 UE를 거부하므로 각 장비에서 `build-oai-ue-local.sh`를 한 번 다시 돌려야 한다.

**Spark 상태:** 게이트 프로세스·MPS·GPU 앱 없음, 이 트랙의 tmux 세션 종료. 남긴 것: 계측 트리 `src/oai-s14dbg`·빌드 `builds/oai-s14dbg`, A 대조 `builds/oai-zmq-local-s13`, 실행 스크립트와 원자료 `/workspace/gpuch/s14/`. 표는 `~/ocudu-work/perf-platform/compare-s14.md`(git 밖), 캡처 분석 스크립트는 워크스테이션 scratchpad.

## S15 — 브로커 코드만으로 lock-step 한 바퀴 줄이기 (2026-09-29)

**하려던 것:** S10·S13에서 넓은 대역 2×2가 실시간에 못 미치는 것은 gNB → 브로커 → UE → 브로커 → gNB 고리가 메시지마다 한 바퀴를 통째로 기다리기 때문이라고 판정했다. 이 실험의 변경 범위는 **브로커·에뮬레이터 코드로 한정**했다. OAI·OCUDU ZMQ 드라이버, gNB·UE 설정, 호스트 idle·sysctl은 건드리지 않았다. 브랜치 `broker-round`(worktree `~/ocudu-work/ocudu-broker-round`, `integration-0928` `9c7f1d7`에서 분기), Spark 트리 `/workspace/gpuch/br15`, 빌드 `builds/gpuch-br15-release`. 모든 라이브 실행은 OAI 2×2 게이트(unitary H, zero-copy, CPU gNB, 게이트 기본값)이고, 실행마다 다른 GPU 프로세스가 없음을 기록했다(전부 없음).

### 1. 한 바퀴가 어디에 쓰이나 — 먼저 잰 것

기존 `s10-hop-trace.py`는 2포트 id(`gnb0_p0`)를 읽지 못해서 `scripts/cuda/spark/s15-critical-path.py`를 새로 썼다. 단계마다 누적 샘플 수로 같은 샘플 경계를 맞추고, 방향마다 브로커 단계와 장치 몫을 나눈다. 장치가 RX 요청을 늦게 보낸 시간(`wait_req`)은 장치 몫이다. 창(`[skip, until]`)을 받아 iperf 부하 구간만 따로 볼 수 있고, gNB TX → UE RX → UE TX → gNB RX → gNB TX 의존 사슬을 거꾸로 따라가 한 바퀴를 나눈다.

100 MHz 2×2 무부하, 기준 브로커(방향당 p50, µs):

| 단계 | DL | UL | 내용 |
|---|---|---|---|
| in_ring | 8 | 9 | 수신 메시지 → TX ring |
| wake_prod | 40 | 45 | 입력 도착 → producer가 깨어남(50 µs sleep poll + timer slack) |
| produce | 182 | 182 | ring → 입력 창 복사 55–68 + 채널 호출 93(커널 38) + RX ring push 20 |
| wake_rep | 34 | 41 | 행 준비 → REP 워커가 깨어남 |
| send | 10 | 10 | `zmq_send`(491 KB 복사) |
| **브로커 몫** | **310** | **315** | |
| 장치 turnaround | gNB 446 | UE 455 | RX 응답 송신 → 그 장치의 다음 TX 도착 |

- 한 바퀴는 메시지 2개를 전진한다(lead 1, 사슬 `advance` p50 2). 그래서 브로커 몫이 한 바퀴에 두 번 들어간다. 기준 루프는 메시지당 914 µs(평균)였다.
- 채널 호출 93 µs 중 커널은 38 µs다. 나머지 대부분은 **2행 노드의 출력이 `device_output`을 거쳐 호출자 행으로 CPU 복사**되는 부분이다. Z8의 direct 출력은 1행 노드에만 적용됐다(`sp.rows == 1`). 커널이 행이 연속이라고 가정(`out_dev + r * count`)했기 때문이다.

### 2. 바꾼 것 — 하나씩 A/B (100 MHz 2×2 무부하 1M, 게이트 `rt_factor`)

| 변경 | 노브 | rt (실행별) | 브로커 몫 p50 (DL/UL µs) | 판정 |
|---|---|---|---|---|
| 기준 | — | 0.563, 0.570, 0.571, 0.569 | 310 / 315 | — |
| **spin 대기** — 워커가 sleep 대신 소켓을 non-blocking으로 폴링하고 `sched_yield` | `OCG_BROKER_SPIN=1` | 0.599, 0.605 | 254 / 255 | 채택(프로파일) |
| **복사 줄이기** — puller는 ZMQ 메시지에서 바로 ring으로, producer는 TX ring을 제자리에서 읽고, REP는 RX ring에서 바로 송신 | `OCG_BROKER_FEWER_COPIES` | 아래와 함께 0.620, 0.616 | 209 / 214 | 채택(기본) |
| **행을 RX ring에 직접** — 채널이 RX ring 꼬리에 바로 씀 | `OCG_BROKER_DIRECT_ROWS` | (위와 함께) | | 채택(기본) |
| spin + 두 복사 변경 | | 0.641, 0.652, 0.623, 0.644 | 136–206 / 139–241 | |
| **다중 행 direct 출력** — 커널이 행 포인터 표로 호출자 행에 직접 씀 | `OCG_ZC_MULTIROW_DIRECT` | 0.680, 0.665 | 140–204 / 115–117 | 채택(기본) |
| + spin 예산 50 µs | `OCG_BROKER_SPIN_US=50` | 0.638, 0.644 | 176–179 / 183–184 | 기각 |
| + ZMQ I/O 스레드 2 | `OCG_ZMQ_IO_THREADS=2` | 0.683, 0.667, 0.670, 0.686 | 117–131 / 115–148 | 기각(잡음 수준) |
| 소켓은 block, 내부 대기만 spin | `OCG_BROKER_SPIN=2` | 0.666, 0.659 | 121–130 / 150–156 | 기각 |
| **최종 기본값**(코드 기본 + 프로파일 spin) | 없음 | **0.673, 0.680, 0.679** | 116–205 / 116–127 | |

- 다중 행 direct 출력으로 채널 호출 p50이 95 → 50 µs가 됐다(커널 35, 메타 H2D 4).
- **spin의 대가:** 브로커의 고정 코어 3개(15–17)를 100% 쓴다. 그래서 코드 기본값으로 두지 않고 `platform-profiles.json`의 `spark-gb10`에 `broker_env: {OCG_BROKER_SPIN: "1"}`로 넣었다. `platform-profile.py`가 `OCUDU_NATIVE_BROKER_ENV`로 내보내고, OAI 1x1·2x2 게이트가 브로커 앞에 붙이며 `gate-defaults.log`·`run-params`에 기록한다. `OCUDU_NATIVE_BROKER_ENV=`(빈 값)로 끈다. Jetson은 재지 않았으므로 넣지 않았다.
- **spin이 전부는 아니다:** 워커 10개 + ZMQ I/O 스레드가 코어 3개를 나눠 써서, 한쪽 방향의 REP 깨어남·송신 꼬리가 p90 약 220 µs까지 갔다(실행마다 방향이 바뀜). spin 예산과 모드 2는 이 경쟁을 줄이려 했지만 sleep·block으로 돌아가는 지연이 더 컸다.
- 남은 브로커 몫(최종, 방향당 p50 약 120 µs): in_ring 12, produce 49(채널 호출 46), REP 깨어남 24, 송신 23. 송신이 10 → 23 µs로 는 것은 GPU가 쓴 메모리를 CPU가 처음 읽는 일관성 비용(S9)이 push에서 `zmq_send` 복사로 옮겨 왔기 때문이다.

### 3. 브로커만으로 어디까지 가나 — 바닥

| 조건 | 루프(메시지당 평균) | 브로커 몫(메시지당) | 장치 몫 | 브로커 몫이 0이면 |
|---|---|---|---|---|
| 100 MHz 무부하, 기준 | 914 µs | 약 313 | 약 600 | — |
| 100 MHz 무부하, 최종 | 737 µs | 약 133 | 약 604 | 약 0.83배 |
| 100 MHz 부하 200M | 약 2,100 µs | 약 130–150 | 약 1,950 | 약 0.25배 |

- **부하 중에는 장치가 한 바퀴를 정한다.** iperf 구간(12–24 s)만 보면 UE turnaround 평균 1.7–1.8 ms(p90 4.4–4.8 ms), gNB 1.05–1.1 ms이고, UE의 RX 요청이 행 준비보다 평균 724 µs 늦다. 브로커 produce를 190 → 95 µs로 줄이고 깨어남을 없애도 초당 실시간 비율은 0.24–0.26 그대로였다(기준·최종 모두). 이 구간은 브로커 코드로 줄일 수 없다.
- **조각 단위 처리는 드라이버를 바꾸지 않고는 불가능하다.** 장치는 포트마다 TX를 0.5 ms = 61,440샘플짜리 ZMQ 메시지 하나로 보낸다(홉 추적에서 메시지의 99.9%가 정확히 61,440). ZMQ는 메시지를 통째로만 넘기므로 브로커가 더 일찍 시작할 수 없다. REP 응답은 요청 하나에 메시지 하나라서, 응답을 쪼개려면 장치가 RX 요청을 더 보내야 한다.

### 4. 대역폭별 결과 (최종 기본값, 기준은 노브를 모두 끈 같은 바이너리)

| 조건 | 기준 | 최종 | 비고 |
|---|---|---|---|
| 2×2 100 MHz 무부하 | 0.563–0.571 | 0.673–0.680 | NACK 0 |
| 2×2 100 MHz 부하 200M | 0.242–0.258 | 0.259(최종), 0.277–0.279(다중 행 direct 전 노브) | 장치 한계 |
| 2×2 50 MHz 무부하 | 0.866 | **0.996, 0.996** | 실시간 도달 |
| 2×2 50 MHz 부하 | 0.363 | 0.372 | |
| 2×2 20 MHz | — | 무부하 1.000, 부하 0.894 | S12와 같음 |
| 1x1 100 MHz(OAI) | 1,984 슬롯/s | 2,001 슬롯/s | 둘 다 실시간, NACK 0 |

### 5. 정확성

- **출력 bit 동일:** 새 broker 테스트는 2포트 gNB·UE 노드를 고정 2×2로 잇고 포트마다 다른 램프를 lock-step으로 흘려, 노브를 끈 실행과 켠 실행의 RX 스트림을 비교한다. 4포트 2,460만–2,620만 샘플 bit 동일. **음성 대조군:** REP의 ring view를 한 샘플 밀면 `port 0 sample 0 differs`로 실패한다. 새 processing 테스트는 2행 노드를 copy·zero-copy·다중 행 direct에서 bit 단위로 비교한다. **GB10 음성 대조군:** direct 행 포인터를 뒤바꾸면 실패(rc 1), 원복하면 통과.
- **ring 테스트:** `view()`/`reserve()`/`commit()`과, ring을 비울 때 꼬리 위치를 유지하는 것을 검사한다(예전 `discard_before`는 비울 때 `start_`를 0으로 돌려, 예약된 꼬리가 움직일 수 있었다).
- ctest: 워크스테이션 5090 12/12(노브 각각·전부 켠 상태 포함), GB10 12/12 무부하 3회, GPU 경쟁(`busy`) 중 10/10. 9단계 시퀀스: GB10 무부하 통과, 경쟁 중 + `OCG_BROKER_SPIN=1` 통과.
- **라이브 y=Hx**(워크스테이션 host python, UE 2번 TX 포트는 M6.4와 같이 선언된 무송신): 100 MHz DL 최대 오차 4.6e-8·2.4e-8, UL 2.0e-10·7.7e-11. 20 MHz도 통과. 모든 2×2 실행 NACK 0 또는 0.0006–0.0013(몇 실행).
- 회귀: OAI 1x1 20 MHz 기본값 3/3·노브 끔 3/3·spin만 끔 1/1 통과(NACK 0), 100 MHz 둘 다 통과. srsUE legacy 1x1 통과(starvation 1).

### 6. 무엇이 터졌고 왜

- **분석기가 너무 느렸다.** 첫 버전은 단계마다 시간 목록을 다시 만들어 O(n²)라 10분을 넘겼다. 캐시해서 4초가 됐다.
- **다중 행 direct의 첫 수정이 제한을 새로 만들었다.** 행 포인터 표 크기(16)를 넘는 노드를 direct가 아닐 때도 거부했다. 연속 행(`base + r * count`)을 기본으로 두고 direct일 때만 표를 쓰게 고쳤다.
- **batch 2 시작이 워크스테이션에서 실행됐다.** ssh 인용이 깨져 heredoc 본문이 로컬에서 돌았다. `run.sh`가 없어 아무 실행도 되지 않았고, 워크스테이션 `ocudu-integration`에 빈 `hop/` 디렉터리만 생겨 지웠다. 이후에는 스크립트를 로컬에서 쓰고 복사했다.
- **Spark 빌드 디렉터리가 root 소유**(게이트가 만듦)라 dev로는 빌드가 안 됐다. 컨테이너 root로 빌드했다.
- **batch 4에서 게이트 5개가 시작하지 못했다.** 50 MHz 무부하 실행의 teardown에서 open5gs `5gc`가 abort(core dump)하고, 자식 `open5gs-scpd`가 남아 게이트의 `flock` fd를 물고 있었다. 이후 게이트는 전부 `another native OAI gate is running`으로 거부됐다. 남은 프로세스를 끄고 나머지를 batch 4b로 다시 돌렸고, 실행 전마다 우리 컨테이너의 남은 코어망·gNB·UE 프로세스를 기록하고 끄는 점검을 넣었다. **게이트 teardown 누수 자체는 고치지 않았다**(범위 밖, 기록만).
- **OAI 1x1 20 MHz가 한 번 실패했다**(`ue_stack_blocker_no_attach`: RRC 연결, Registration Request 4회, AMF 도착은 25 s 실행의 21 s째). 바로 이어서 기본값·노브 끔을 3회씩 번갈아 돌렸고 모두 통과했다. 재현되지 않아 원인은 모른다.

### 7. 커밋 (`broker-round`, push 안 함)

| 커밋 | 내용 |
|---|---|
| `2757db1` | 노브 3개(spin, 복사 줄이기, RX ring 직접 행), `IqRing::view/reserve/commit`, ring·relay parity 테스트, `s15-critical-path.py` |
| `9e49bb2` | 다중 행 direct 출력(`OutputRowTable`), 2행 parity 테스트 |
| `f2085e0` | spin 예산, 분석 창, `s15-summary.sh` |
| `0725977` | spin 모드 2, 의존 사슬 분석 |
| `70a9a99` | 기본값: 제자리 relay·직접 행·다중 행 direct 켬, GB10 프로파일 `broker_env`(spin), 게이트가 브로커 env를 넘기고 기록 |

**남은 것:** 부하 중 한 바퀴의 대부분은 OAI UE(2레이어 100 MHz 복호)와 gNB 몫이라, 브로커만으로는 무부하 약 0.83배·부하 약 0.25배가 바닥이다. 남은 브로커 몫 약 120 µs/방향 중 REP 깨어남 24 µs는 코어 3개에서 스레드가 경쟁해서 생긴다(워커 구조를 바꿔야 함). 송신 23 µs는 일관성 비용이다(`zmq_msg_init_data`로 복사를 없애도 커널 TCP 복사가 같은 비용을 낸다). 게이트 teardown의 open5gs 누수와 1회성 1x1 실패는 기록만 했다. 표 전체는 `~/ocudu-work/perf-platform/compare-s15.md`(git 밖), 원자료는 Spark `/workspace/gpuch/s15/`(홉 추적 `hop/`는 gzip).

## S17 — lock-step 고리의 파이프라이닝: gNB lower PHY를 ZMQ에서도 스레드 모드로 (2026-10-02)

**왜:** S15는 브로커 몫만 줄여 100 MHz 2×2 무부하 0.57 → 0.68×에서 멈췄고, 바닥은 0.83×였다. 한 바퀴의 나머지는 "장치 턴어라운드"였는데, 그 정체는 gNB가 UL 블록 하나를 받아야 DL 블록 하나를 만드는 직렬 구조였다. 해결 범위: 드라이버·gNB 쪽 로컬 패치.

**원인 (코드 읽기):** OCUDU는 `device_driver == "zmq"`이면 lower PHY를 blocking/sequential로 강제한다(세 곳: `ru_sdr_config_cli11_schema.cpp` autoderive, `ru_sdr_config_translator.cpp` fill_sdr_worker_manager_config, `split_8_o_du_application_unit_impl.cpp` fill_worker_manager_config). TX·RX·상위 PHY가 `phy_worker` 하나에서 돌아 `ul_process()`의 receive가 끝나야 `dl_process()`가 돈다. 코드상 DL은 마지막 RX보다 1 ms(`rx_to_tx_max_delay = srate_kHz + tx_time_offset`)까지 앞설 수 있지만 단일 스레드라 그 여유를 쓰지 못한다. OAI nrUE는 이미 UL을 RX보다 3슬롯(`NR_UE_CAPABILITY_SLOT_RX_TO_TX`) 앞서 쓰고 요청도 받자마자 다음 것을 보내므로 UE 쪽은 손댈 것이 없다. 브로커 run-ahead는 1 batch(`rx_high_water`).

**패치 `scripts/native/patches/ocudu-zmq-lower-phy-profile.patch`** (락 `ocudu-gnb-local-patches.lock.json`, 빌더 `build-ocudu-gnb-local.sh`, 모두 환경변수 미설정이면 stock 동작):

| 변수 | 효과 | 왜 필요했나 |
|---|---|---|
| `OCUDU_ZMQ_LOWER_PHY_PROFILE=single\|dual\|triple` | 세 강제 지점을 건너뛰고 그 스레드 프로파일 유지 | 본 목적 |
| `OCUDU_LPHY_DL_WAIT_MS` | `dl_process`가 UL 타임스탬프를 기다리는 wall-clock 상한(stock 2슬롯)을 교체 | 첫 dual 런: 고리가 실시간보다 느리니 2슬롯 탈출이 매 슬롯 터져 DL이 wall-clock으로 혼자 앞서감 → RF overflow 1,280만 건, RAR 창 놓침, attach 실패 |
| `OCUDU_LPHY_RX_TO_TX_MAX_MS` | DL 선행 상한 1 ms를 조절 | 2 ms에서 "Downlink data late" 82 → 6 |
| `OCUDU_ZMQ_RADIO_RT_PRIO=k` | `radio` 워커(모든 ZMQ 요청/응답)를 SCHED_FIFO max−k로 | **결정적.** 스레드 모드에서 lower_phy tx/rx가 FIFO 98/97로 10 µs 폴링하며 같은 5코어의 SCHED_OTHER `radio`를 밀어냄: 2 s당 런큐 대기 평균 373 ms, 최대 751 ms(`/proc/<tid>/schedstat`). 고리 전체(gNB·UE·브로커)가 100 ms 배수로 멈춤(런당 550여 회, 합 62 s/90 s) |
| `OCUDU_ZMQ_IO_RT_PRIO=k` | libzmq I/O 스레드를 SCHED_FIFO max−k로(`zmq_ctx_set`) | 같은 기아(337 ms/2 s) |

**측정 (Spark GB10, br15 트리/빌드, 게이트 기본값 + 홉 추적, 100 MHz n78 TDD 2×2 unitary, zero-copy, CPU gNB a1916edc):**

| 런 | gNB | rt (무부하 1M) | 비고 |
|---|---|---|---|
| base-1/3, pr-base-1 | stock, sequential | 0.653 / 0.664 / 0.667 | S15 최종값 0.67–0.68과 같음 |
| off-1 | 패치 바이너리, 변수 없음 | 0.668 | sequential 그대로 → 기본값 무해 |
| dual-1 | dual, 탈출 2슬롯 | 실패 | RF overflow 폭주, attach 안 됨 |
| dualw-1/2, triplew-1/2 | + DL_WAIT 5000 | 0.267 / 0.310 / 0.262 / — | 중앙값 한 바퀴 743 → 668 µs, lead 2–3이지만 수백 ms 멈춤 반복 |
| dualw-rt2-1, triplew-rt2-1 | + RX_TO_TX 2 ms | 0.321 / 0.244 | late 82 → 6 |
| pr-dual-c10 / c6 / c8 | 코어만 변경 | 0.479 / 0.342 / 0.226 | 코어를 늘리면 완화, 브로커를 small 코어로 보내면 악화 |
| **rt-dual-1, rt-triple-1** | **+ RADIO_RT 2, IO_RT 3** | **0.799 / 0.794** | radio 런큐 대기 0.7 ms/2 s, lead p50 4, 한 바퀴 p50 634 µs, NACK·ping 정상 |
| rt-dual-2, rt-triple-2, base-4 | 반복 | 0.793 / 0.779 / 0.663 | 재현됨 |
| rt-dual-l1 / base-l1 | 부하 200M | 0.549 (health FAIL) / 0.252 (pass) | dual: DL late 1,702, NACK 16 %, ping 손실 81 %: 상위 PHY 풀 고갈 |
| ue3-1/2, ue7-1 | UE 3코어 / 2 big + 5 small | 0.784 / 0.787 / 0.769 | UE 코어 수는 무관 |
| ue5s-1/2, ue4-1 | UE 5 big, 브로커 small 코어 | 0.658 / 0.653 / 0.673 | 브로커를 small 코어로 보내면 손해 |
| f50-rt-i / f50-base-i | 50 MHz 무부하 | 0.9998 / 0.991 | 둘 다 실시간 |
| f50-rt-l / f50-base-l | 50 MHz 200M | 0.086 (FAIL) / 0.403 (pass) | dual: UL processor busy 1,020, DL processor 풀 고갈 1,075 |
| f20-rt-l / f20-base-l | 20 MHz 200M | 0.947 (DL air 4.5 Mb/s!) / 0.894 (147.5 Mb/s) | dual: 풀 고갈로 트래픽이 안 흐름 → CPU 부족이 아니라 RT 폴링 스레드가 상위 PHY를 밀어냄 |
| x1-rt-100 / x1-base-100 | 100 MHz 1×1 무부하 | 0.999 / 1.000 | 회귀 없음 |
| hw1/hw2/hw3 (batch 7) | 브로커 응답 창 1/2/3 batch | 0.805·0.812 / 0.798·0.790 / 0.774 | 2슬롯 응답은 10 %만 발생, 효과 없음; 부하(hw2-l1) 0.712 FAIL |
| neon-1/2, neon-hw2-1/2 (batch 8) | OAI 모듈 NEON 변환 (custom shlibpath) | 0.795 / 0.723 / 0.743 / 0.804 | UE 메시지 왕복 469 → 236 µs로 반감했지만 rt 불변 → UE 수신 경로는 병목 아님 |
| neon-base | 순차 gNB + NEON | 0.707 | 순차 모드에는 +5 % |
| **v45-100i, v4-100i, v45-100i-2** | v4(유휴 슬립, radio max−1) ± v5(단일 복사) | **0.820 / 0.848 / 0.822** | radio 워커 CPU 95 → 64 % |
| **v45-20l, v4-20l** | 20 MHz 2×2 200M | **0.979 / 0.982**, DL air 148 Mb/s, late 0, 풀 고갈 0 | 기준 0.894 → 부하에서 처음으로 기준을 넘김 |
| v45-100l | 100 MHz 2×2 200M | 0.316 (health pass, NACK 1 %, DL late 201) | 기준 0.252; 상위 PHY 연산 한계는 남음 |
| v45-50l | 50 MHz 2×2 200M | 0.394 (pass, DL late 155) | 기준 0.403; 이득 없음 |

**다음 벽 (0.80, 무부하):** 홉 추적의 "응답 송신 → 같은 포트의 다음 요청 도착"이 UE 469 µs, gNB 422 µs(p50). REQ/REP는 포트당 요청 하나만 걸 수 있어 메시지당 수신 비용(전송 491 KB + cf32→int16 변환 + ring push + 재요청)이 그대로 처리율 상한이 된다. UE 코어를 늘려도 안 변하므로 CPU가 아니라 경로 지연이다. 후보: NEON 변환(`oai-zmq-neon-convert.patch`, aarch64에서는 스칼라 cleanup 루프가 전부를 처리), ipc 전송, 2슬롯 응답.

**부하 조건의 교훈:** 순차 모드는 "아무도 기다리지 않는 자"가 없어 느려질 뿐 깨지지 않는다. 스레드 모드는 클록이 수신 속도로 흐르고 상위 PHY 풀(`nof_dl_processors = 4 × max_proc_delay`, UL processor)이 못 따라오면 슬롯을 버린다. 20 MHz에서도 깨진 것은 radio 워커가 RT 우선순위로 100 % 폴링(채널 task가 스스로 재큐잉, 유휴 없음)하고 lower_phy_rx가 1 µs 슬립 폴링(70 %)하여 같은 우선순위(max−2)의 상위 PHY 풀을 밀어내기 때문 → v4: 유휴 슬립 노브 `OCUDU_ZMQ_RADIO_IDLE_US`, `OCUDU_ZMQ_RX_POP_SLEEP_US`, radio 우선순위 max−1.

**결론 (CPU gNB a1916edc, GB10):**

| 조건 | stock (sequential) | 패치 (dual + RT radio + 유휴 슬립 + 단일 복사) |
|---|---|---|
| 100 MHz 2×2 무부하 | 0.65–0.67 | **0.82–0.85** |
| 100 MHz 2×2 200M | 0.25 (pass) | 0.32 (pass, NACK 1 %, DL late 201) |
| 50 MHz 2×2 무부하 | 0.99 | 1.00 |
| 50 MHz 2×2 200M | 0.40 (pass) | 0.39 (pass, DL late 155) |
| 20 MHz 2×2 200M | 0.89 | **0.98**, DL air 148 Mb/s, late 0 |
| 100 MHz 1×1 무부하 | 1.00 | 1.00 |

- 무부하의 남은 0.15–0.2는 메시지당 수신 왕복(REQ/REP 포트당 요청 하나)과 두 장치의 슬롯 처리 지연이 직렬로 겹치는 구간이다. 브로커 응답 창 확대(hw2/hw3), UE 코어 증설, NEON 변환은 각각 측정상 효과가 없었다(측정 표 참조). 다음 후보는 ZMQ 전송의 ipc 전환과 드라이버의 선요청(DEALER)이다.
- 부하의 50/100 MHz는 CPU gNB의 상위 PHY(PDSCH 인코딩·rank-2 PUSCH 디코딩) 연산이 슬롯을 넘기는 구간이라 lower PHY 파이프라이닝으로는 못 올린다. 순차 모드는 고리를 늦춰 이를 숨기고(0.25/0.40, pass), 스레드 모드는 실제 라디오처럼 늦은 슬롯을 버린다(DL late 수백). 상위 PHY를 GPU로 내린 CUDA gNB(D10)에 같은 패치를 적용한 결과는 아래 batch 9.
- 20 MHz 부하 0.98과 무부하 0.82는 **드라이버·gNB 쪽 직렬 대기를 없애면 에뮬레이터 몫은 슬롯 안**임을 보인다: 100 MHz 2×2 한 바퀴 p50 612 µs 중 브로커 몫 약 120 µs/방향.

**패치 적용법 (게이트):** `OCUDU_NATIVE_GNB_BINARY=$OCUDU_NATIVE_ROOT/builds/ocudu-zmq-local/apps/gnb/gnb` + 락의 `recommended` 환경변수 전체(게이트가 환경을 gNB 자식에 그대로 넘긴다). 빌더 `scripts/native/build-ocudu-gnb-local.sh`, 락 `scripts/native/ocudu-gnb-local-patches.lock.json`. Spark에는 수동 빌드 `builds/ocudu-lphy-release`(src/ocudu-lphy, 같은 패치)와 CUDA 변형 `builds/cuda-lphy-sm121`(src/ocudu-cuda-lphy, `ocudu-zmq-lower-phy-profile-cuda.patch` + D10 락 패치)이 있다.

**batch 9 — CUDA gNB (D10 + 같은 패치 `ocudu-zmq-lower-phy-profile-cuda.patch`, 빌드 `builds/cuda-lphy-sm121`, MPS on, 100 MHz 2×2):**

| 런 | 모드 | rt | 비고 |
|---|---|---|---|
| cu-base-i | sequential | 0.679 | CPU gNB 기준과 같음 |
| cu-rt-i, cu-rt-i-2 | dual + 전체 노브 | 0.804 / 0.833 | |
| cu-base-l | sequential, 200M | 0.293 (pass) | gNB 턴어라운드 p50 1,002 µs |
| cu-rt-l, cu-rt-l-2 | dual, 200M (-2는 broker hw2) | 0.336 (pass, NACK 0.45 %) / 0.343 | gNB 턴어라운드 p50 607 µs, **UE 턴어라운드 평균 1.3 ms, p90 3.5 ms** |

- 부하 중에는 CPU gNB(v45-100l: gNB 669 µs, UE 평균 1.2 ms)든 CUDA gNB든 **UE 턴어라운드(평균 1.2–1.3 ms, p90 3.2–3.5 ms)가 한 바퀴를 정한다.** 2코어의 OAI nrUE가 273 PRB rank-2 TB를 슬롯마다 복호하지 못한다(S15에서 1.7–1.8 ms로 본 것과 같은 항). 상위 PHY를 GPU로 내려도 gNB 몫만 1,002 → 607 µs로 줄고 전체는 0.29 → 0.34다.
- 따라서 이 박스에서 100 MHz 2×2 부하를 실시간으로 만들려면 UE 쪽(코어 또는 UE PHY 가속)이 남은 조건이고, 에뮬레이터·gNB 쪽 직렬 대기는 이번 트랙으로 제거됐다.
