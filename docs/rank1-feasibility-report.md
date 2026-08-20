# Rank-1 MISO/SIMO 구현 결과 보고 — Feasibility 실증 중심

**대상 워크스페이스**: `ocudu-gpu-channel-rank1` (브랜치 `rank1-miso-simo`)
**기준 문서**: 통합 평가 보고서 `docs/mimo-integration-report.html` (2026-08-16 스냅샷, upstream 공개본과 동일) — **Sionna RT 항목 제외** (별도 팀원 담당, 후일 병합)
**보고 시점**: 2026-08-17 · 모든 수치는 실측이며 출처(게이트 실행 ID/로그)를 §8에 명기

---

## 0. 한 문단 요약

평가 보고서가 승인한 rank-1 목표 — **DL 2×1(→4×1) MISO + UL 1×2(→1×4) SIMO, srsUE는 1안테나 유지** — 를 실제 OCUDU gNB ↔ GPU 브로커 ↔ srsUE 5G의 **라이브 end-to-end로 전부 구현·통과**했다. 2포트와 4포트 구성 모두 attach + PDU 세션 + 사용자 평면 ping이 성립하고, **같은 게이트 실행 안에서 wire 캡처로 y = H·x를 재계산해 선언 채널 벡터와 ≤4.6×10⁻⁵ 오차로 일치함을 자동 판정**하며, GPU 처리 시간은 1 ms 슬롯 예산 안에 있다(4×1 kernel p50 12.9 µs). 평가 보고서가 "Not yet demonstrated" delivery gate로 지목했던 라이브 다중포트 증명이 닫혔고, 그 과정에서 발견된 통합 제약 4건은 모두 소스/실측 근거와 함께 해법이 fixture에 정착되었다.

---

## 1. Feasibility 판정 — 평가 보고서의 4대 질문에 대한 현재 답

평가 보고서(§01 Executive verdict)의 질문 구조를 그대로 갱신한다. 보고서는 원격 공개 스냅샷(`a801155`)을 평가했고, 그 이후의 로컬 작업이 아래 답을 바꾸었다.

| 질문 | 보고서 시점 답 (a801155) | **현재 답 (실측 근거)** |
|---|---|---|
| 이 프로젝트가 다중 안테나 채널을 에뮬레이트할 수 있는가? | **No** — scalar/SISO per node | **Yes** — RadioNode 다중포트 모델, 방향별 행/열 벡터(`fixed_mimo`), 라이브에서 y=Hx ≤4.6e-05 검증 |
| 2×1/1×2 rank-1이 라이브로 도는가? | Not yet demonstrated (delivery gate) | **UL 1×2는 Yes (다중 branch), DL 2×1은 절차만 Yes (단일 branch)** — 게이트 `run-ocudu-rank1-2x1.sh` `result=pass` + 행렬판정 `passed` (실행 `20260817T091805Z`). UL은 gNB 2개 수신 포트가 각자 독립 branch를 받아 행별로 판정된다. DL은 srsRAN이 port0에만 방사하므로(§6 잔여 한계) 라이브 판정이 사실상 1-branch이며, DL 다중 branch 증명은 합성 게이트와 oracle 실험이 담당한다. |
| 4×1/1×4로 확장되는가? | 계획 (2포트 통과 후 gate) | **UL 1×4는 Yes (4 branch 전부), DL 4×1은 절차만 Yes (단일 branch)** — 게이트 `run-ocudu-rank1-4x1.sh` `result=pass`, UL 4행 전부 y=Hx 통과 (실행 `20260817T091856Z`). DL은 TX 1·2·3이 `--allow-silent-source`로 선언되어 라이브 행렬판정이 `y=h₀x₀` 스칼라 검사로 축약된다. |
| srsUE는 rank-1로 유지되는가? | 요구사항 | **유지** — `nof_antennas = 1` 그대로, 1×1 회귀 게이트 병행 green, 모든 결과는 rank-1 MISO/SIMO로만 기술 |

**Feasibility의 정의를 좁혀 말하면**: "실제 OCUDU 스택과 실제 srsUE가, 우리가 선언한 다중포트 채널 벡터를 통과해, 표준 절차(RA→RRC→NAS 등록→PDU→사용자 평면)를 완주하는가" — 이 정의로 **2포트·4포트 모두 Yes이며, 각 Yes는 반복 실행 가능한 게이트 스크립트로 잠겨 있다.** 단, 이 Yes는 *절차 완주*에 대한 것이다. 채널 내용 측면에서 라이브 다중 branch가 증명된 방향은 **업링크(1×2 / 1×4 SIMO)뿐**이며, 다운링크의 다중 branch 합성은 합성 게이트(R0/R1)와 oracle 유효채널 실험이 증명한다 — 이유는 §6 잔여 한계에 있다.

---

## 2. pre-MIMO 코드에서 현재까지의 작업 경로

### 2.1 계보

```
pre-MIMO baseline (공개 스냅샷 a801155 계열)
  · 노드당 1 스트림(scalar), 방향 엣지당 스칼라 TDL 채널
  · 1×1 OCUDU↔srsUE 라이브 attach, TR 38.901 TDL-A..E, GPU 커널(183×), 런타임 컨트롤
        │
        ▼  MIMO 재구축 (부모 트리 ocudu-gpu-channel-mimo-claude, M0~M5)
  · M0  RadioNode 오버레이: Device를 leaf 포트로, 노드당 producer 1스레드가
        공통 샘플 윈도를 단독 소유 → 포트 간 커서 정렬이 "프로토콜"이 아닌 "구조적 불변식"
  · M1  차원 도입: radio_nodes의 tx_ports/rx_ports 작성 순서 = 행렬 인덱스, fixed_mimo 희소 계수
  · M2~M3  IID 페이딩 → 공간상관(Kronecker) + coherent LOS
  · M4  physical link 단위 원자적 런타임 컨트롤 (부분 갱신된 포트 벡터가 불가능한 상태)
  · M5  라이브 통합: 실제 gNB 2T2R 멀티포트 transport + wire 캡처 기반 y=Hx 행렬 검증(M5.5)
        │
        ▼  rank1 포크 (이 트리, 2026-08-17, 평가 보고서 −Sionna 목표에 정렬)
  · R0  비대칭 토폴로지: 2포트 gNB 노드 ↔ 1포트 UE 노드 (DL 1×2 행 / UL 2×1 열)
  · R1  결정론적 채널 증명: branch isolation, 코히어런트 위상 스윕(뮤테이션 검증 단위테스트)
  · R2  라이브 2×1/1×2: OCUDU 2T2R ↔ 브로커 ↔ srsUE attach+PDU+ping + y=Hx
  · R3  4×1/1×4 확장 + UL 안정성 원인 규명
  · +   게이트에 행렬판정 통합, 성능 라벨 실측, oracle precoding 실험
```

### 2.2 왜 이 경로가 feasibility에 유리했는가

평가 보고서 §07이 요구한 데이터모델 변화(포트 배열 엔드포인트, 방향별 벡터 채널, grouped slot barrier, 원자적 벡터 활성화)는 **부모 트리의 M0~M5가 이미 그대로 구현**한 것과 구조적으로 동형이었다. 따라서 rank1 작업의 본질은 재설계가 아니라 (a) 비대칭(N×1/1×N) 구성 검증, (b) 실제 srsUE와의 라이브 통합, (c) 통합 제약의 규명이었고, **에뮬레이터 코어 C++ 변경은 위상 스윕 단위테스트 1건 외에 없다** — 채널 엔진의 성숙도가 그대로 feasibility의 토대가 되었다.

---

## 3. 요구사항 충족 매핑 (평가 보고서 controlling brief 기준)

### 3.1 Goal / Intended experiments

| 보고서 요구 | 충족 방식 · 증거 |
|---|---|
| OCUDU gNB + srsUE 5G를 실시간 GPU ZMQ 에뮬레이터로 통과 | R2/R3 라이브 게이트 pass (attach+PDU+ping), strict counter 전부 0 |
| srsUE를 1 TX/RX 스트림, 1 레이어로 유지 | `nof_antennas = 1` 불변; UE conf는 1×1 회귀 게이트와 **byte-identical 렌더** |
| 다중 OCUDU 포트로 gNB측 어레이/다이버시티 이득 | UL 1×2/1×4: gNB가 실제로 2/4 수신 포트를 등화(각 branch가 선언 계수로 정확 스케일). DL: oracle 실험으로 MRT 어레이 이득 +0.54 dB 실측 (§5.4) |
| DL 2×1 먼저 검증 후 4×1 | R2(2×1) → R3(4×1) 순차 게이트, 각각 pass |
| UL 1×2 먼저, 후 1×4; 각 RX 포트에 독립 branch | UL 열벡터의 행별 독립 계수; 라이브 y=Hx가 행마다 별도 판정(4행 전부 ≤4.6e-05) |
| 고정/oracle-선택 rank-1 프리코딩과 유효 채널 연구 | Oracle 실험 완료: 4개 가중의 유효 채널을 라이브로 실측, 예측과 0.01 dB 정합 (§5.4) |
| 합성 peer로 계수·정렬 증명 후 라이브 | R0/R1 합성(위상 스윕, branch isolation, y=Hx) → R2/R3 라이브의 순서 준수 |

### 3.2 Technical requirements

| 요구 | 충족 · 증거 |
|---|---|
| 기존 1×1 attach 경로를 회귀 테스트로 보존 | `run-ocudu-legacy-1x1.sh` 무변경 유지, rank1 트리에서 `result=pass` 재확인 |
| 프리코딩/빔 가중은 OCUDU/RU에, 전파·페이딩·지연·노이즈·중첩은 이 프로젝트에 | 준수. 에뮬레이터 공급 가중은 전부 "oracle" 라벨 (§5.4) |
| 모든 gNB 안테나 스트림을 하나의 샘플/슬롯 경계에 정렬, 원자적 갱신 | producer 단일 윈도(구조적 불변식) + M4 원자 컨트롤; 라이브 y=Hx 통과 자체가 정렬의 실측 증명 |
| strict 검증에서 starvation/gap/overflow/ZMQ 오류 0 | 전 게이트에서 `tx_queue_overflows=0 tx_sequence_gaps=0 zmq_errors=0` (starvation은 soft 신호로 기록, 데이터 무결 카운터와 구분) |
| CPU/CUDA 정확도 + 완전 라벨된 성능 | ctest 8/8 (CPU/CUDA 두 트리), 성능은 §5.3의 완전 라벨 표기 |

### 3.3 Writing/claim requirements — 무엇을 주장하지 않는가

보고서의 요구대로, 다음은 **구현하지 않았고 주장하지 않는다**: rank>1 SU-MIMO(부모 트리의 별도 워크스트림), UE 수신 다이버시티/빔 스티어링, 자동 PMI 폐루프 빔 제어, 동일-PRB MU-MIMO, "end-to-end 4×4 MIMO"라는 표현. 검증되지 않은 복제를 "diversity"라 부르지 않는다 — 오히려 oracle 실험이 **복제가 MRT보다 −1.24 dB 나쁨을 정량 증명**했다(§5.4). 모든 결과는 "2×1/4×1 DL MISO, 1×2/1×4 UL SIMO"로 기술한다.

---

## 4. Feasibility 실증 I — 라이브 end-to-end 게이트

Feasibility의 1차 증거는 "실제 스택이 실제로 완주하는가"이다. 두 게이트 모두 반복 실행 가능한 단일 명령이며, 다음을 **한 실행 안에서** 판정한다:

1. **절차 완주**: RA → RRC → NAS 등록 → PDU 세션 → 사용자 평면 ping **60발 0% 손실**
2. **전송 무결**: 브로커 strict counter 0, gNB `Real-time failure in RF` 0
3. **채널 내용**: 브로커가 shutdown 시 flush한 500 ms wire 캡처를 독립 checker가 **선언된 토폴로지의 H로 y=Hx 재계산** — 트래픽 계수가 아니라 연산을 채점
4. **출처 증거**: 소스 커밋 핀, 바이너리/설정 SHA-256, 채널 소스 manifest를 JSON으로 보존

| 게이트 | 실행 ID | 결과 | 라이브 y=Hx (허용 1e-04) | 라이브 활성 TX branch |
|---|---|---|---|---|
| 2×1 DL + 1×2 UL | `20260817T091805Z` (native) | **pass** | UL row0/row1 ≤ 2.2e-05, DL row 3.7e-08 | UL **2/2**, DL **1/2** |
| 4×1 DL + 1×4 UL | `20260817T091856Z` (native) | **pass** | UL 4행 = 4.6e-05 / 2.2e-05 / 1.7e-05 / 2.8e-05 | UL **4/4**, DL **1/4** |

**독립 재현 (2026-08-19, 컨테이너 하네스, 다른 호스트·다른 5GC·다른 네트워크 스택)**: 위 결과는
`scripts/remote/ocudu-rank1-{2x1,4x1}-smoke.sh`로 재현되었다. attach + PDU + ping 250발 + strict counter 0,
그리고 같은 실행 안에서의 행렬 판정:

| 게이트 | 결과 | UL y=Hx (행별 max) | DL y=Hx |
|---|---|---|---|
| 2×1 DL + 1×2 UL | **pass** | 4.62e-05 / 2.12e-05 | 4.85e-08 |
| 4×1 DL + 1×4 UL | **pass** | 4.61e-05 / 2.16e-05 / 1.59e-05 / 2.68e-05 | 5.31e-08 |

원본 native 실행과 행별로 자릿수까지 일치한다 — 결과가 특정 호스트나 특정 하네스의 산물이 아님을 뜻한다.
| 1×1 srsUE 회귀 | 동일 일자 | **pass** | (기존 경로 무회귀) |
| 상시 GPU 시퀀스 | 동일 일자 | **9/9 pass** | (합성 relay·AWGN·그래프·2셀·TDL-A 등) |

마지막 열이 이 표의 해석을 결정한다: 라이브 다중 branch 증거는 업링크에만 있다. 다운링크 행은 `--allow-silent-source`로 선언된 무방사 포트를 제외하므로 단일 branch 검사이며, 통과해도 다중 branch 합성을 증명하지 않는다(에뮬레이터가 무방사 포트를 정확히 0으로 기여시킨다는 것까지는 증명한다). UL 행별 RMS가 각 선언 계수 크기 |h_r|과 상대오차 ~6×10⁻⁷(fp32 바닥)로 일치한다 — 즉 **gNB의 2/4개 수신 포트 각각이 자신의 독립 branch를 정확히 받고 있으며, OCUDU가 그것을 실제로 결합해 복호한다.** 이것이 보고서가 "the strongest end-to-end diversity target"이라 부른 UL SIMO의 실증이다.

---

## 5. Feasibility 실증 II — 정량 증거

### 5.1 채널 정확도 (합성, 통제 조건)

- 4포트 합성 캡처: UL 4×1 **4행 전부 max|y−Hx| ≤ 1.3e-07**, DL 1×4 행 1.24e-07, off-diagonal share 1.18 (네 소스 전부 기여)
- 코히어런트 위상 스윕(동일 파형 2 lane, 뮤테이션으로 유효성 확인된 단위테스트): φ=0 → 진폭 2×, φ=π → 0, φ=π/2 → 1+j — 정확 일치
- 라이브 브로커 branch isolation: tx1-only 수신 전력 실측 0.0280165 vs 예측 0.02802 (**0.05%**)

### 5.2 라이브 브로커 해석 폐쇄성

합성 peer의 마커 구조(port1 = −conj(port0), 결정론적 상관)까지 모델에 넣으면 **코히어런트 합성 전력의 실측값(0.0935)이 예측과 소수 4자리에서 재현**된다 — 채널 엔진의 동작이 해석적으로 완전히 닫혀 있다는 뜻이다.

### 5.3 실시간 예산 (완전 라벨)

**측정 방법 정정.** 이전 판의 p50/p99는 1 Hz heartbeat가 발행하는 "마지막 슬롯" 값 18개에서 계산한 것이었다.
20 s 런은 약 2만 슬롯을 처리하므로 n=18의 p99는 사실상 그 18개의 최대값이며, 실시간 예산 논의가 필요로 하는
꼬리를 나타내지 못한다. 브로커가 이제 **모든 슬롯**을 5 µs 버킷 히스토그램에 누적하고 종료 시
`event=process_latency_summary`로 발행하므로, 아래 수치는 전 슬롯 기준이다.

Intel Core Ultra 9 285K · RTX 5090 1기 · 23.04 MS/s · batch 23040(=1 ms 슬롯) · CUDA 백엔드 ·
fixed_mimo(1탭)+TDL 체인 · 라이브 Docker 게이트 60 s 런:

| 구성 | 노드 | n (슬롯) | process p50 | p95 | p99 | p99.9 | GPU kernel p50 |
|---|---|---|---|---|---|---|---|
| 2×1 | gnb0 | 57,753 | 80 µs | 135 µs | 205 µs | 340 µs | 10.6 µs |
| 2×1 | ue0 | 54,794 | 75 µs | 150 µs | 230 µs | 380 µs | — |
| 4×1 | gnb0 | 54,439 | 115 µs | 200 µs | 285 µs | 675 µs | 12.9 µs |
| 4×1 | ue0 | 54,284 | 120 µs | 210 µs | 310 µs | 650 µs | — |

p99.9까지 1 ms 슬롯 예산 안이며, 4포트 p99는 슬롯의 31%다. **정직한 단서 하나**: 두 구성 모두 관측
최대값이 5 ms 오버플로 버킷에 걸린다. p99.9가 675 µs이므로 해당 슬롯은 전체의 0.1% 미만이고 런
시작/종료 구간으로 보이지만, 이전 판의 "관측 최대치 포함 예산 내"라는 문장은 더 이상 성립하지 않는다.
이 이상치의 출처 규명은 미해결 항목이다.

이전 판의 수치(Threadripper PRO 7965WX, heartbeat n=18)는 표본 부족으로 **양방향 모두** 틀렸다:
2×1은 꼬리를 과소평가했고(p99 131.9 → 205 µs), 4×1은 이상치 하나를 p99로 보고했다(619.1 → 285 µs).

### 5.4 Oracle precoding 실험 — "이득은 위상 정합에서만 나온다"의 라이브 증명

프리코더는 gNB/RU 소속이나 srsRAN에 주입 노브가 없으므로, 물리 행 h에 대해 실험자가(채널 지식으로, 즉 **oracle**) 고른 가중 w의 **유효 채널 c = h·w를 에뮬레이터가 선언**하고, DL에 고정 −30 dB AWGN을 두어 |c|²가 실제 SNR이 되게 했다. 각 케이스는 완전한 라이브 attach 런이다:

| Oracle 가중 | 예측 (vs MRT) | **실측** | 라이브 결과 |
|---|---|---|---|
| MRT (정합) | 0 dB | 기준 | attach+PDU+ping |
| port0-only | −0.53 dB | **−0.54 dB** | attach+PDU+ping |
| 동일 복제 [1,1]/√2 | −1.24 dB | **−1.24 dB** | attach+PDU+ping |
| 반정합 깊은 널 | −38.7 dB | (셀 소멸) | **셀 서치 실패** |

절대 수신 전력도 4케이스 전부 |h·w|²·P_tx+noise와 비율 1.000. **2×1 MRT 어레이 이득 +0.54 dB의 라이브 실측**이며, 복제-비-다이버시티 원칙과 위상 반정합의 자기소거가 실험으로 확정됐다. 가중 선택은 oracle이며 closed-loop PMI를 시사하지 않는다.

---

## 6. Feasibility 실증 III — 규명된 통합 제약과 해법 (전부 소스/실측 근거)

Feasibility 평가에서 가장 중요한 부분은 "되긴 되는데, **어떤 조건에서** 되는가"이다. 라이브 통합 과정에서 발견된 제약 4건은 각각 근본 원인까지 규명되었고, 해법이 fixture에 근거 주석 + 렌더러 invariant로 잠겨 있다:

| # | 제약 (발견 증상) | 근본 원인 (근거) | 해법 (정착 위치) |
|---|---|---|---|
| 1 | 2T2R + srsUE용 통념 PDCCH 설정에서 gNB가 기동 거부 | OCUDU validator가 fallback DCI(SS#2)와 DL 안테나 >1의 조합을 금지 (`du_cell_config_validation.cpp:290`) | `ss2_type: ue_dedicated` + `dci_format_0_1_and_1_1: true`. **srsUE가 DCI 0_1/1_1을 실제 지원함을 1×1 대조로 증명** — "srsUE는 common/fallback 필요"라는 통념은 1×1에서만 유효했던 선택임이 판명 |
| 2 | 2T2R에서 RRC 접속 직후 연결 붕괴 루프 | srsUE PoC가 2포트 NZP-CSI-RS(FD-CDM2) 리소스 매핑 미구현 → connection setup 절차 전체 실패 (srsUE 로그 `Resource mapping is invalid or not implemented`) | `csi_rs_enabled: false` + `nof_cell_csi_res: 0` |
| 3 | 다중 DL 안테나에서 gNB 기동 거부 | OCUDU가 >1 DL 안테나와 MAC pcap의 조합을 무효 구성으로 거부 | `pcap.mac_enable: disable` |
| 4 | UL 4R에서 등록이 확률적으로 실패 (bimodal) | CSI-off 환경에서 gNB UL 링크어댑테이션이 무노이즈 채널의 포화 SNR 추정(~11 dB)을 신뢰, 64QAM(코드율 0.93)까지 반복 시도 → **상시 ~20% PUSCH KO**(성공 런도 동일 통계) → 등록 크리티컬 구간이 연속 KO에 걸리면 실패. bimodal은 상태가 아니라 생존 확률 | `pusch.max_ue_mcs: 9` — 적용 후 full 4T4R **3연속 완주, KO 0** |

#4의 규명 과정은 방법론 관점에서도 feasibility 신뢰도를 보여준다: 채널/브로커(실패 런의 wire에서도 y=Hx 정확), 링 크기, UE 설정 적용, 재동기, 스트림 원점 위상이 **각각 직접 실측으로 기각**되었고, 겉보기 실패 신호였던 `epre=-inf`는 UE가 보낼 SR이 없는 occasion의 정상 DTX 로그였음이 교차 검증(UE 전송 로그 308회 ↔ gNB 검출 308회, grant된 PUSCH 841:841 대응)으로 판명되었다. **에뮬레이터는 실패 런에서도 무죄였다** — 제약은 전부 통합 상대(스택 정책/UE 구현 범위)에 있었고, 그 경계가 이제 문서화된 지식이다.

### 정직한 잔여 한계

- **DL에서 gNB port 1 이상은 방사하지 않는다**: srsRAN은 SSB/공용 채널을 port 0에만 싣고, CSI-off + rank-1에서 PDSCH를 [1,0,…]으로 프리코딩한다(캡처 실측). 따라서 라이브 DL의 다중-branch 코히어런트 합성 내용은 합성 게이트(R0/R1)와 oracle 유효채널 실험이 증명을 담당하며, 게이트의 행렬 판정은 이 사실을 `--allow-silent-source`로 **명시 선언**(허용이 아니라 실측 사실의 기록)한다.
- `max_ue_mcs: 9`는 CSI-off의 파트너 설정으로서 UL 처리량에 상한을 만든다. 이 워크스트림은 처리량을 주장하지 않으므로 결과 경계 안에 있다.
- 4포트 실시간 p99 여유(§5.3)는 8포트+ 확장 전 재측정 대상이다.

---

## 7. 다음 단계

1. **Sionna RT 병합 접점** (담당 팀원 합류 시): 본 트리의 wire-capture/행렬판정 인프라가 Sionna record/replay 검증 게이트의 그대로 쓸 수 있는 채점 도구이며, 방향별 벡터 채널 + 원자 활성화(M4)가 보고서의 coefficient-horizon 계약이 요구하는 수신측 구조다.
2. gNB측 4포트+ 확장 시 실시간 재측정, 필요 시 공간상관/CDL(부모 트리 자산 보유).
3. rank>1은 부모 트리(`ocudu-gpu-channel-mimo-claude`)의 OAI-nrUE 워크스트림에서 별도 진행 중 — 본 트리의 범위 밖.

---

## 8. 증거 색인

| 증거 | 위치 |
|---|---|
| 라이브 게이트 (**재실행 가능, 지원 경로**) | `scripts/remote/ocudu-rank1-2x1-smoke.sh`, `ocudu-rank1-4x1-smoke.sh`, `ocudu-attach-smoke.sh` — 컨테이너 하네스. 자체 Docker 네트워크·5GC를 띄우고 빈 서브넷을 스스로 고르며 필요한 Python을 자체 프로비저닝하므로, 이 저장소 + GPU 워크스테이션만으로 재현된다. |
| 라이브 게이트 (원본, 재현 불가) | `scripts/native/run-ocudu-rank1-{2x1,4x1}.sh`, `run-ocudu-legacy-1x1.sh` — 최초 실행 경로의 기록. `bootstrap-workspace.sh`가 프로비저닝을 의도적으로 비활성화하고 있고 `/home/ubuntu`·`/opt/conda` 경로가 하드코딩되어 있어, 이 저장소만으로는 실행할 수 없다. |
| 게이트 실행 산출물 (요약 JSON·행렬 리포트·로그·소스 핀) | `~/ocudu-native-workspace/results/{reports,logs}/rank1-2x1/20260817T091805Z`, `rank1-4x1/20260817T091856Z` |
| 채널/게이트 fixture (제약 근거 주석 포함) | `examples/native/ocudu/gnb_zmq_b210_fdd_{2t2r,4t4r}_rank1_srsue.yaml`, `examples/native/topology.ocudu.rank1-{2x1,4x1}.cuda.yaml`, `topology.ocudu.rank1-2x1-oracle-mrt.cuda.yaml` |
| 독립 행렬 checker | `scripts/native/verify-mimo-matrix-capture.py` (H는 토폴로지에서 읽고 브로커 출력은 신뢰하지 않음) |
| 위상 스윕 단위테스트 (뮤테이션 검증) | `tests/test_processing.cpp` R1 절 |
| 작업·규명 전체 서사 (원인 규명, 기각 가설 포함) | `AGENT_PROGRESS.md` "Rank-1 Workstream" 절 |
| 로드맵·상태·측정 라벨 | `RANK1_MILESTONES.md` |
| 커밋 이력 | `rank1-miso-simo` 브랜치, 포크 기점 `34f669e` 이후 (R0–R2: `06d27d1`, R3 규명: `4d98e2a`, 게이트 통합: `e31fe51`, oracle: `e630bba`). 공개 브랜치는 upstream `216a28b` 위로 graft되었으므로, 이 해시가 정본이다 — 포크 이전 사설 트리의 해시는 공개 브랜치에 존재하지 않는다. |

*본 문서의 모든 주장은 위 산출물에서 재현 가능하다. 여기 없는 것은 주장하지 않는다.*
