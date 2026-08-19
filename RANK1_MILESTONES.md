# Rank-1 MISO/SIMO 마일스톤 (이 워크스페이스의 정본 로드맵)

미션: [`AGENT_GOAL.md`](AGENT_GOAL.md). 근거 문서: 상사 전달 보고서 [`docs/mimo-integration-report.html`](docs/mimo-integration-report.html)의 로드맵에서 **Sionna 단계를 제외**한 것 (Sionna는 다른 팀원 담당, 후일 병합).

이 트리는 `ocudu-gpu-channel-mimo-claude`의 2026-08-17 HEAD 포크다 (공개 브랜치의 포크 기점 커밋은 `34f669e`). 따라서 M0~M5의 전 자산 — RadioNode 오버레이, producer 단일 윈도, `fixed_mimo`, 비대칭 차원 단위테스트(M1), wire-capture 행렬 검증(M5.5), 네이티브 라이브 하네스 — 을 그대로 물려받고, 보고서가 "Not implemented"로 지목한 항목들의 상당수는 **이미 구현되어 있다**. 남는 것은 비대칭(2×1/1×2) 구성과 srsUE 라이브 통합이다. rank-2/OAI 자산(M6 게이트)은 이 트리에서 사용하지 않지만 제거하지도 않는다(부모 트리와의 diff를 최소로 유지).

| 단계 | 내용 | Exit 게이트 | 상태 |
|---|---|---|---|
| R0 | **비대칭 토폴로지** — 2-port gNB 노드 ↔ 1-port UE 노드 선언, DL 1×2 row / UL 2×1 column `fixed_mimo` | validator 통과, 기존 ctest 전체 green (비대칭 단위테스트 포함), 합성 peer로 브로커 관통 + strict counter 0 | **완료 2026-08-17** |
| R1 | **결정론적 2×1/1×2 채널 증명** — 보고서의 "Deterministic channel" 게이트 | branch isolation(antenna-0-only / antenna-1-only), DL 위상 스윕의 코히어런트 합/상쇄, UL branch 독립성, CPU↔CUDA parity | **완료 2026-08-17** (위상 스윕은 뮤테이션 확인된 신규 단위테스트; branch isolation·코히어런트 합은 CUDA 브로커 실측이 해석 예측과 0.05~3% 정합) |
| R2 | **라이브 2×1 DL + 1×2 UL** — OCUDU gNB 2T2R ↔ 브로커 ↔ srsUE(nof_antennas=1) | attach + PDU + ping, strict counter 0, gNB RF failure 0, wire-capture로 y=Hx 벡터 검증 동시 통과. 1×1 srsUE 게이트 무회귀 | **완료 2026-08-17** (`run-ocudu-rank1-2x1.sh` `result=pass`; 1×1 무회귀 pass; 캡처 실측: UL 두 행 y=Hx ≤4.6e-05, DL row 3.7e-08. 알려진 구조적 사실: 이 구성에서 gNB port1은 방사하지 않음 — SSB/공용채널 port0 + CSI-RS off + rank-1 [1,0] 프리코딩. DL 두-branch 콘텐츠 증명은 R0/R1 합성이 담당) |
| R3 | **4×1 / 1×4 확장** | R1/R2와 동일 게이트를 4포트에서 반복; 주장은 계속 rank-1 | **완료 2026-08-17**: `run-ocudu-rank1-4x1.sh` `result=pass` — full 4T4R(DL 1×4 + UL 4×1) 라이브 attach+PDU+ping, strict counter 0. 합성 y=Hx 4행 ≤1.3e-07 + 라이브 UL 4행 ≤4.6e-05. UL-4R flaky의 **원인 규명 완료**: CSI-off 환경에서 gNB UL 링크어댑테이션이 무노이즈 채널의 포화 SNR 추정(~11dB)을 믿고 과공격 MCS(64QAM tcr 0.93)를 반복 → 상시 ~20% PUSCH KO → 등록 크리티컬 구간이 연속 KO에 걸리면 실패(bimodal=확률). `pusch.max_ue_mcs: 9`가 사슬을 절단(3/3 완주, KO 0) — 두 gNB fixture에 근거 주석과 함께 정착, R2 게이트도 cap 반영 재통과. 기각된 가설(전부 실측): 채널/브로커(무죄), rx_ring, srsUE 설정, resync, tx/rx 원점 위상(성패 공통 상수), 그리고 epre=-inf는 SR-DTX의 정상 로그(SR 308/308 검출)였음 |

**Oracle precoding 실험 (2026-08-17, 보고서의 "fixed or explicitly oracle-selected rank-1 precoding" 항목)**: 프리코더는 개념상 gNB/RU 소속이지만 srsRAN에 주입 노브가 없으므로, 에뮬레이터가 물리 행 h=[0.80+0.10j, 0.25−0.15j]에 대한 **oracle 가중 w의 유효 스칼라 c=h·w를 선언**하고(명시적 oracle 라벨 — closed-loop PMI가 아님), DL에 고정 −30dB AWGN을 두어 |c|²가 실제 SNR이 되게 했다. 라이브 srsUE 결과: 수신 전력이 |h·w|²·P_tx+noise를 4케이스 모두 비율 1.000으로 따르고, MRT 대비 상대 이득이 **port0-only 실측 −0.54dB(예측 −0.53) · naive 복제 [1,1]/√2 실측 −1.24dB(예측 −1.24)** 로 0.01dB 정합, **반정합 깊은 널(예측 −38.7dB)은 셀 서치 자체가 실패**. 즉 (a) 2×1 MRT 어레이 이득 +0.54dB의 라이브 실측, (b) "복제는 diversity가 아니다"의 정량 증명, (c) 위상 반정합의 자기소거 — 보고서의 phase-sweep evidence gate가 라이브에서 닫혔다. 대표 fixture: `examples/native/topology.ocudu.rank1-2x1-oracle-mrt.cuda.yaml`. 주장 경계: 가중 선택은 채널 지식(oracle)에 의한 것이며 UE 피드백 기반 빔 제어를 시사하지 않는다.

**게이트 통합과 측정 (2026-08-17 후반)**: 두 라이브 게이트(R2/R3)는 이제 **같은 실행에서 행렬 판정을 함께 채점**한다 — 브로커가 shutdown 시 flush한 wire 캡처(500ms, 등록+ICMP 창)를 `verify-mimo-matrix-capture.py`가 선언 벡터로 재계산하고, gNB의 무방사 DL 포트(>0)는 `--allow-silent-source`로 근거와 함께 선언된다(검사 완화가 아니라 실측 사실의 선언; DL 다중-branch 콘텐츠 증명은 합성 게이트 담당). 통합 후 첫 실행: R2 `matrix_capture_status=passed`(UL row 오차 ≤2.2e-05), R3 passed(UL 4행 4.6e-05/2.2e-05/1.7e-05/2.8e-05). ping은 10Hz×60발 0% 손실을 요구한다. 상시 게이트 위생: `gpu-test-sequence.sh` 9/9가 이 트리에서 통과.

**측정 라벨**: 노드 process 지연은 이제 브로커가 **모든 슬롯**을 히스토그램에 누적해 종료 시 `event=process_latency_summary`로 발행한다. 종전 수치는 1 Hz heartbeat가 발행하는 "마지막 슬롯" 값 18개에서 뽑은 것이라, n=18의 p99는 사실상 최대값이었고 꼬리를 나타내지 못했다. 아래는 전 슬롯 기준 재측정이다.

- **재측정 (Intel Core Ultra 9 285K + RTX 5090 1기, 23.04 MS/s, batch 23040=1 ms 슬롯, CUDA 백엔드, fixed_mimo(1탭)+tdl 체인, 라이브 Docker 게이트 60 s 런, 5 µs 버킷)**

| 구성 | 노드 | n (슬롯) | p50 | p95 | p99 | p99.9 |
|---|---|---|---|---|---|---|
| 2×1 | gnb0 | 57,753 | 80 µs | 135 µs | 205 µs | 340 µs |
| 2×1 | ue0 | 54,794 | 75 µs | 150 µs | 230 µs | 380 µs |
| 4×1 | gnb0 | 54,439 | 115 µs | 200 µs | 285 µs | 675 µs |
| 4×1 | ue0 | 54,284 | 120 µs | 210 µs | 310 µs | 650 µs |

  GPU 커널 p50은 2×1 10.6 µs, 4×1 12.9 µs (heartbeat 표본, 커널 자체는 슬롯 간 변동이 작다). p50/p95/p99/p99.9는 모두 1 ms 슬롯 예산 안이다. **다만 두 구성 모두 관측 최대값이 5 ms 오버플로 버킷에 걸린다** — 전체 5만여 슬롯 중 극소수(p99.9가 675 µs이므로 0.1% 미만)이며 런 시작/종료 구간으로 보이나, "관측 max까지 예산 내"라고는 더 이상 말할 수 없다. 이 이상치의 출처 규명은 미해결 항목이다.

  종전 라벨(Threadripper PRO 7965WX, heartbeat n=18: 2×1 p99 131.9 µs, 4×1 p99 619.1 µs)은 표본이 부족해 양방향으로 틀렸다 — 2×1은 꼬리를 과소평가했고(131.9 → 205), 4×1은 이상치 하나를 p99로 보고했다(619.1 → 285).

**주장 경계 (보고서 writing-requirements 준용)**: 모든 결과는 "2×1/4×1 DL MISO, 1×2/1×4 UL SIMO"로 기술한다. "end-to-end 4×4 MIMO", rank>1, UE 수신 빔포밍, PMI 폐루프, MU-MIMO를 주장하지 않는다. 고정 DL 가중치의 이득은 위상이 채널과 정합할 때만 성립하므로, 검증된 프리코더 메타데이터 없이 "diversity"라 부르지 않는다.

**R2가 확정한 사실** (전부 실측/소스 근거, `AGENT_PROGRESS.md` R2 절 상세):
- **srsUE는 2T2R 셀에 attach한다** — 단 세 가지 gNB 적응이 필수이며 각각 근거가 있다: ① `ss2_type: ue_dedicated` + `dci_format_0_1_and_1_1: true` (OCUDU validator가 fallback DCI + DL 안테나 2를 금지, `du_cell_config_validation.cpp:290`; srsUE는 1_1/0_1을 실제로 지원 — 1×1 대조로 증명) ② `csi_rs_enabled: false` + `nof_cell_csi_res: 0` (srsUE가 2포트 NZP-CSI-RS FD-CDM2 매핑 미구현 — "Resource mapping is invalid or not implemented"로 connection setup 전체가 죽음) ③ `mac_enable: disable` (OCUDU가 >1 DL 안테나에서 MAC pcap 거부). 종전의 "srsUE용 common/false 오버라이드" 통념은 반전됨 — 그 오버라이드는 1×1에서만 유효한 선택이었다.
- gNB 2포트 ↔ 1포트 UE는 direct 연결이 불가능하다(포트1 요청을 응답할 상대가 없음) — 브로커가 필수 경유다.
- 이 라이브 구성에서 **gNB port1은 방사하지 않는다**(SSB/공용채널은 port0, CSI-RS off, rank-1 PDSCH 프리코딩 [1,0] — M6.3/R2 캡처 실측). DL의 두-branch 코히어런트 콘텐츠를 라이브로 실으려면 gNB/RU 측 oracle 프리코딩 실험이 필요하며 그것은 R3 이후의 별도 항목이다.
- 두 트리(rank1/mimo-claude)는 `~/ocudu-native-workspace`를 공유한다. rank1 게이트들은 전용 채널 빌드 `builds/ocudu-gpu-channel-rank1-cuda-release`를 쓴다(CMake 캐시가 소스 경로를 고정하므로 공유 불가). 라이브 게이트를 두 트리에서 동시에 돌리지 말 것.
