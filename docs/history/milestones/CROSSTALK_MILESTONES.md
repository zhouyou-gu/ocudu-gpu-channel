# X 트랙 — UE↔UE "crosstalk" 수정 (2026-10-01)

> Historical record; its claims apply to the original date, revision and setup. Migrated from `CROSSTALK_MILESTONES.md` at `58d3156` without changing recorded measurements.


## 왜

멀티-UE 토폴로지에 UE↔UE 엣지(`ue1.tx → ue0.rx`, 이름 `crosstalk`)를 넣으면 UE가
붙지 않거나 30 s 안에 떨어진다는 보고가 있었다. 원인은 브로커가 아니라 두 입력이었다.

1. **존재하지 않는 경로.** 네이티브 fixture는 FDD band 3(DL 1842.5 / UL 1747.5 MHz)이다.
   한 UE의 상향 반송파는 다른 UE의 하향 수신기에 들어올 수 없다. 에뮬레이터의 포트는
   "한 반송파의 베이스밴드"이고 브로커는 반송파를 모르므로, 토폴로지에 그어진 선은
   무조건 더해진다. `topology.graph.cuda.yaml`(5-18, 업스트림), `topology.sionna-2gnb-2ue.cuda.yaml`
   (8-21), `multi-gnb-sutd.json`, `sionna_SUTD_test.json`, `robot-ring-crosstalk.json`(9-29)이
   같은 선을 적었다.
2. **송신 스케일 64 dB.** srsUE ZMQ 드라이버는 `[rf] tx_gain = 50`을 수치로 곱한다
   (`rf_zmq_send_timed_multi`, 피크 313, 활성 평균 +44 dB). OCUDU gNB 하향은 −19.8 dB.
   gNB 수신은 UE 송신만, UE 수신은 gNB 송신만 보므로 평소엔 드러나지 않고, 송신기 종류를
   한 수신기에서 섞는 crosstalk 엣지에서만 터진다.

## 재현 (Spark GB10, srsUE 2대, 잡음 바닥 40 dB 기준)

| 런 | 기하 | 링크 | crosstalk 탭 | 피해 UE rx 활성 전력 | 결과 |
|---|---|---|---|---|---|
| 단일 셀 `ocudu-multi-ue/20261001T065827Z` | 링 씬, UE 간 4 m | 4 | — | −21 dB | 유지 |
| 단일 셀 `…/20261001T070229Z` | 〃 | 6 | +18 dB | **+62 dB** | 둘 다 tti 3054에 SR 실패 → release, PRACH 7회 무응답. 게이트는 `passed` |
| 2셀 `ocudu-multi-gnb/20261001T084057Z` (A3) | 거리 캐니언, 10 m 사이트, 셀 간 150 m | 8 | — | −16 dB | 유지, 각자 셀, ping 0 % 손실 |
| 2셀 `…/20261001T084550Z` (B3) | 〃 | 10 | −19 dB | **+17 dB** | 유지(상대 UE 송신 duty 0.8 %, ping만). 가짜 간섭이 33 dB 위에 있어도 안 깨진 경우 |

A3/B3를 만드는 과정에서 2-gNB 게이트의 별개 결함 둘을 고쳤다: 4-UE 커밋 이후 렌더러가
항상 실패(`validate_subscriber` 시그니처), 두 셀이 같은 PRACH 루트라 lock-step 동시 PRACH를
양쪽 gNB가 검출해 유령 UE가 생김(A2 `…/20261001T083425Z`, gnb0에 rnti 0x4602 PUSCH crc=KO).

## 수정 (X0–X2)

| # | 내용 | 파일 | 상태 |
|---|---|---|---|
| X0 | 게이트 판정 엄격화: SR 실패 0, attach 후 PRACH 재시도 0, RLF 0, ping 전부 응답. `OCUDU_NATIVE_ATTACH_STRICT` (기본 1) | `run-ocudu-multi-ue-inner.sh`, `run-ocudu-multi-gnb-inner.sh` | 구현, 합성 로그 테스트 통과 |
| X1 | 장치별 `tx_scale_db`(브로커 `type: gain` 스텝으로 선행 tdl 뒤에 접혀 profile_swap에도 남음; 링크 키는 선언 모델명 `declared_model` 유지). Sionna 렌더러가 UE 포트에 `ue_tx_scale_db = (23−30) − 10log10(3e4/1.12e-2) ≈ −71.3 dB`를 쓰고 gNB 잡음 바닥을 스케일 후 UL로 잡음. 노브 `OCUDU_NATIVE_SIONNA_UL_POWER_OFFSET_DB`. srsUE `tx_gain`으로는 불가(음수 = 자동, 실측 40 dB 적용; 하한 0) | `config.{h,cpp}`, 백엔드, `render-sionna-multi-ue-configs.py`, `run-ocudu-multi-ue.sh`, `topology.sionna-2gnb-2ue.cuda.yaml` | 구현, ctest 12/12 |
| X2 | 포트 반송파 라벨 `tx_carrier`/`rx_carrier`(TDD 단축 `carrier`), 라벨이 다른 포트를 잇는 엣지는 시작 전에 거부(라디오 노드는 lane 단위). FDD 예제에서 UE↔UE 엣지 제거, 렌더러·예제에 라벨, 브리지 기본 `CROSSTALK_LINKS=()`, `check_feed` 기본 8링크, `robot-ring-crosstalk.json` 삭제. `graph.yaml`은 `carrier: shared`(단일 반송파 예제)로 유지 | 위 파일들 + tests | 구현, ctest 12/12, 파이썬 테스트 통과 |

## 검증 (Spark GB10, 트리 `/workspace/gpuch/xtree` = integration-0928 `442513f` + 이 변경, 빌드 `gpuch-xt-release`, strict 판정)

| 런 | 게이트 | 결과 | 비고 |
|---|---|---|---|
| V1 `ocudu-multi-ue/20261001T094515Z` | Sionna multi-UE, robot-ring-walk 4링크, 잡음 40 dB | **pass**, 두 UE clean, ping 3/3, starvation 37 | UE 포트 `tx_scale_db −71.279`. gNB rx 활성 −28.9 dB(이전 +48.9), gNB 잡음 바닥 −66.5 dB(= 2.2e-7). UE 쪽은 변화 없음(−21.4 dB) |
| V2 `ocudu-multi-ue/20261001T094830Z` | 같은 게이트 + UE↔UE 링크 시나리오 | **시작 전 거부** | 브로커 `event=fatal … link ue0->ue1 (port ue0_p0->ue1_p0) connects tx carrier "n3-ul" to rx carrier "n3-dl"`. 어제처럼 조용히 돌지 않음 |
| V3 `ocudu-multi-gnb/20261001T094844Z` | 2-gNB Sionna, 저사이트 8링크, 예제 토폴로지 기본값 | **pass**, 각자 셀, clean, starvation 2 | gNB rx 활성 −22.2 dB, 바닥 −66.5 dB |
| V4a `ocudu-multi-ue/20261001T095340Z` | legacy multi-UE(고정 TDL) | **pass**, clean | 라벨만 추가된 토폴로지 회귀 |
| V4b `ocudu-multi-gnb/20261001T095655Z` | legacy 2-gNB(고정 TDL) | **pass**, clean | PRACH 루트 분리 적용 |
| V5 `ocudu-multi-ue/20261001T100228Z` | V1 재실행, outer 게이트 수정 후 | **pass**, `result=pass` 출력, exit 0 | |
| ctest (워크스테이션 5090, CUDA 13.0) | 12/12 | tx_scale CPU=CUDA 1e-3 이내, profile_swap 후에도 −20 dB 유지, carrier 거부 테스트 포함 |
| 파이썬 unittest / 렌더러 self-test | 37 + self-test pass | |
| 예제 토폴로지 26개 | 새 브로커가 전부 파싱 | `carrier`/`tx_carrier` 키 포함 |

덤으로 고친 것: Sionna 모드 멀티-UE 게이트가 통과해도 결과 줄 없이 exit 143으로 끝나던 버그
(`set -e` 아래 `[[ -n web_pid ]] && wait`의 마지막 명령은 면제가 아님). 브로커가 토폴로지를
거부하면 게이트가 `event=fatal` 줄을 그대로 보여준다.

## 후속 (2026-10-01 오후)

- **Spark `int0928` 트리 정렬:** ad8427c로 올림. b3cd457 대비 달랐던 4개 파일(`scripts/native/README.md`,
  `run-ocudu-multi-gnb-inner.sh` 등)은 Spark 브랜치
  `spark-local-1001`(61a8c57)에 보존, 스냅샷 `/workspace/gpuch/int0928-uncommitted-1001.diff`.
- **트래픽 하의 wire 레벨 재측정 — 실패, 상수 유지.** iperf3 UL 2×20 Mbit/s를 걸자 ~30 s 뒤 중계가 완전히 멈췄다
  (`ocudu-multi-ue/20261001T105444Z`: 모든 puller `recv_reply`, 모든 producer `wait_data`, gNB "Waiting for data",
  `node_stall waited_ms=158000`). 같은 설정의 다른 런(`…T104716Z`)은 UE 시작 직후 t≈6 s에 같은 모양으로 멈췄다.
  ping만 있는 런은 오늘 하루 한 번도 멈추지 않았다. `TX_POWER_DL/UL` 상수는 그대로 둔다. 캡처 skip은 "중계된 샘플 수"
  기준이라 멈춘 런에서는 0 샘플이 남는다. 브로커 lock-step 데드락(B2.2 계열)으로 Track D에서 분석 중.

## X3/X4 — TDD에서 UE↔UE가 물리적으로 존재할 때 (2026-10-01 오후, Spark)

새 게이트 `run-ocudu-oai-multi-ue.sh`: OCUDU gNB **TDD n78 20 MHz 30 kHz**(dl_arfcn 632628, 51 PRB,
23.04 MS/s, 7D/1S/2U) + OAI nrUE 2대 + Sionna, 모든 포트 `carrier: n78`. 설계·런 표는
`docs/plans/x3-tdd-multi-ue.md`.

| 런 | 링크 | 결과 | 피해 UE 수신: 상대 UE UL 슬롯 / gNB DL 슬롯 / 무음 |
|---|---|---|---|
| `ocudu-oai-multi-ue/20261001T110816Z` | 4 (베이스라인, 양쪽 LOS) | **pass**, 둘 다 clean | −59.5 / −33.6 / −59.5 dB |
| `…/20261001T111857Z` | 6 (+ue0↔ue1, 탭 중앙값 +4 dB) | **pass**, 둘 다 clean | **−36.9** / −33.6 / −59.5 dB |

즉 같은 TDD 반송파에서는 UE↔UE 엣지가 있어도 상대 UL이 **내 UL 슬롯에만** 떨어지고(gNB TX ∩ UE TX
슬롯 = 0) DL 슬롯은 그대로라 attach가 유지된다. 어제 FDD에서 깨진 것과 대비되는, 물리에 맞는 동작이다.
UE↔UE가 성능에 영향을 주려면 셀 간 TDD 패턴이 달라야 한다(X5, 미착수).
제약: OAI nrUE는 AGC가 없어 기둥 그림자(−25 dB)에서 동기를 잃는다(`…/20261001T110320Z`, srsUE는 버팀).

## OAI nrUE 송신 스케일 (2026-10-01 오후)

OAI 1x1 게이트에 wire capture 노브를 넣어 측정: nrUE UL wire 레벨 **1.17e-5 (−49.3 dB)**, PUSCH+PUCCH
혼합(`oai-1x1/20261001T110037Z`). OAI는 UL을 RE당 고정 진폭으로 내보내므로 srsUE(+44 dB)와 반대로 gNB보다
29.8 dB 조용하다. 23/30 dBm 기준 UE 포트 `tx_scale_db` **+22.8 dB**. OAI 1x1/2x2 렌더러에 라벨·스케일 적용,
스케일 적용 상태로 1x1 게이트 2/3 통과(1회 PRACH TA 오검출 14 µs, 미해결 리스크;
`OCUDU_NATIVE_OAI_UE_TX_SCALE_DB=off`로 복귀 가능).

## 트래픽 하 중계 정지 — 원인은 gNB (2026-10-01 오후, Track D)

두 런(`ocudu-multi-ue/20261001T104716Z`, `…T105444Z`)의 heartbeat를 1초 단위로 추적하면 **먼저 멈춘 것은
OCUDU gNB의 송신**이다(gNB TX "Waiting for data"가 RX보다 먼저, UE TX는 lock-step 선행분 68,833샘플 =
2.99 ms를 다 보내고 RX 대기, 브로커 링은 전부 0). gNB는 "sequential baseband" 프로파일로 lower-PHY 송수신과
upper-PHY가 **스레드 하나**에서 돌아, UE 2대의 PUSCH 복호 + DL이 1 ms 슬롯에 안 들어가면(iperf 2×20 Mbit/s,
또는 동시 attach 버스트) 멈춘다. M5.4의 "OCUDU ZMQ 라디오 자체 데드락"과 같은 계열. 브로커 측 조치:
`OCG_BROKER_WEDGE_TIMEOUT_MS`(기본 10 s, 0 = 끔) — 모든 워커의 진행이 멈추면 `event=relay_wedged`를 찍고
`zmq_errors`를 올린 뒤 정지해 런이 조용히 무효가 되지 않게 한다(`scenario_multi_ue_lockstep_wedge_fails_fast`
테스트). 근본 해결은 gNB 쪽: `ru_sdr` lower-PHY 스레드 프로파일을 sequential에서 바꾸거나 UL 부하를 제한.
멈춘 시점에 `gdb -p <gnb> -batch -ex "thread apply all bt"`로 막힌 지점을 확정할 것.

## 중계 정지의 진짜 원인 — OCUDU main_pool 고갈 (2026-10-01 저녁, Track E, `docs/plans/x6-gnb-realtime-traffic.md`)

멈춘 순간 gNB를 gdb로 떠 보니(`/workspace/gpuch/xt/gnb-threads-20261001T115426Z.txt`) phy_worker는
`sync_task_executor::execute ← mac_cell_processor::handle_slot_indication ← lower-PHY dl_process`에서,
main_pool 스레드 둘은 각각 RLC AM 버퍼 상태 처리와 타이머 처리의 `sync_task_executor::defer`에서 대기 중이었다.
OCUDU는 **ZMQ 모드에서 DU-high 셀 실행기를 main_pool 위의 동기 strand로** 만들고(`du_high_executor_mapper.cpp`,
`is_sync = !rt_mode`), 풀 크기는 `min(5, 가용 코어 − 3)`이라 gNB를 5코어에 고정하면 **2개**다. 동기 대기자 둘이
풀을 다 차지하면 슬롯 인디케이션이 영영 실행되지 않아 DL이 멈추고, lock-step 중계가 따라 멈춘다. CPU 부하도,
lower-PHY 프로파일도 아니다(ZMQ는 sequential을 소스 3곳에서 강제하므로 YAML로 못 바꾼다; 노브
`OCUDU_NATIVE_GNB_LOWER_PHY_PROFILE`은 기록용으로만 남겼다).

조치: `expert_execution.threads.main_pool.nof_threads`를 쓰는 노브 `OCUDU_NATIVE_GNB_MAIN_POOL_THREADS`, GB10
프로파일 기본값 5(`platform-profiles.json` `gnb_env`). 결과: 기본 풀은 4런 중 3런 정지, 풀 5는 1/1에서 2×20 Mbit/s
UL로 200 s 유지(`ocudu-multi-ue/20261001T120308Z`, ue0 UL 20.0 Mbit/s 내내). 업스트림 OCUDU에 알릴 사항.

트래픽 하 wire 레벨: gNB −24.7…−23.9 dB, ue0 풀레이트 PUSCH +39.9…+42.0 dB — 둘 다 상수보다 ~5 dB 낮지만
UL/DL 비(64.6 vs 64.3 dB)는 그대로라 **상수는 유지**(한쪽만 바꾸면 스케일이 4.9 dB 틀어지고, 둘 다 바꾸면 multi-UE 게이트 잡음
바닥 보정이 움직인다). 남은 UE 쪽 문제: 그림자 쪽 srsUE가 풀버퍼 UL(MCS 28)에서 RLF — 별도 트랙.

## X5 — 패턴이 다른 두 TDD 셀의 UE↔UE 간섭(CLI) (2026-10-01 저녁, Track F, `docs/plans/x5-tdd-cli.md`)

새 게이트 `run-ocudu-oai-two-cell-tdd.sh`: X3의 TDD n78 20 MHz 셀 둘(PCI 1/2, PRACH 루트 1/70), OAI nrUE 각 1대,
Sionna, 모든 포트 `carrier: n78`, 노브 `OCUDU_NATIVE_TDD_PATTERNS=same|"7D2U;3D6U"`. 기하는 링 씬에서
gNB (±12, 6, 8), UE (±2, 0.8, 0.4) — 기둥 틈 4 m. 탭: serving −7.4, 셀 간 −47/−52(기둥 차폐), **UE↔UE +6.0 dB**.
GB10에서 gNB 2대 + nrUE 2대가 실시간(ping RTT 22–28 ms, starvation ≤ 15/200 s).

| 런 | 패턴 | 링크 | 판정 | 피해 ue0: 자기 DL 슬롯 수신 (ue1 송신 있음 / 없음) | ue0 CQI | gnb0 DL nok | RTT max |
|---|---|---|---|---|---|---|---|
| `ocudu-oai-two-cell-tdd/20261001T115703Z` | 동일 | 8 | pass | — / −36.4 dB (겹침 0) | 15 ×147 | 0 | 41 ms |
| `…/20261001T120703Z` | 동일 | 10 (+UE↔UE) | pass | — / −36.4 (겹침 0; ue1 UL은 ue0의 UL 슬롯에만 −24.4) | 15 ×146 | 0 | 51 ms |
| `…/20261001T121059Z` | 상이, 대조 | 8 | pass | −44.1 (gnb0 자체 S 슬롯) / −36.4 | 15 ×146 | 0 | 42 ms |
| `…/20261001T121443Z` | **상이** | **10** | pass (attach 유지) | **−24.3 / −36.4** (DL 슬롯 266개 중 11개 피격; 빈 DL 슬롯 4–6에서 −32.2 vs 대조 −59.5) | **0 ×27, 5 ×3**, 15 ×116 | **3** (0.15 %) | **87 ms** |

읽는 법: 어제 FDD의 "가짜 재머"(존재하지 않는 경로, 60–80 dB)와 달리, 여기서는 **존재하는 경로**로 상대 UE의 UL이
피해 UE의 DL 슬롯 3.5/7.4개에 자기 하향보다 큰 레벨로 떨어지고, 그 결과가 CQI 붕괴·DL NACK·RTT 스파이크로 스택에
나타난다. SSB/SIB1/PDCCH가 실리는 슬롯 0–2는 두 패턴 모두 DL이라 동기와 제어는 살아남는다. 미실행: gNB↔gNB 12링크,
2D7U, CLI 하의 UL 트래픽.

## OAI 2x2 레벨, PRACH TA 오검출, 그리고 브로커 CFO 2배 버그 (2026-10-01 저녁, Track G, `docs/plans/x7-oai-levels-prach.md`)

- **2x2 포트별 레벨**: 1x1과 채널별 0.3 dB 이내로 같다(OAI는 RE당 고정 진폭). 기본 1레이어 UL은 포트 1이 무음,
  UL rank 2면 두 포트가 같은 레벨. 상수 공유 유지, 2x2 게이트 wire capture 노브 추가, 스케일 적용 상태로 4런 통과
  (NACK 0, DL 80 Mb/s, UL-MIMO 60 Mb/s).
- **PRACH TA 14 µs**: 레벨 문제가 아니다. gNB에 도달한 UL의 CFO가 +601 Hz = PRACH 서브캐리어 간격의 0.48이라
  Zadoff-Chu 부엽이 주엽 대비 −0.7 dB로 u⁻¹ 샘플 떨어진 곳에 생기고, 그 프리앰블(17, u=60)에서는 13.3 µs 뒤였다.
  64개 중 22개 프리앰블이 취약, 레벨과 무관. 601 Hz의 출처 둘: (1) **브로커 버그** — `phase` 스텝이 `cfo` 코드 경로를
  공유해 링크의 cfo_hz를 한 번 더 곱했다. `phase`+`cfo`를 같이 쓰는 체인(legacy 1x1 `cuda_mvp`, OAI 1x1, graph,
  multi-gnb, mvp, stress 예제)에서 설정 125 Hz가 **250 Hz**로 나갔다. 두 백엔드 모두 수정, 회귀 테스트
  (`[phase, cfo 125]`의 측정 주파수 = 125 Hz, `[cfo 125]` 뒤 고정 회전과 일치) 추가. 그동안 "cold Msg3 허용 ~155 Hz"
  같은 기록은 실제로는 2배 CFO에서 잰 값이다. (2) OAI `--cont-fo-comp 1`이 UL을 상쇄하는 대신 +351 Hz로 미리
  회전시킨다 → `OCUDU_NATIVE_OAI_UE_CONT_FO_COMP=3`(DL만 보정)로 두면 UL CFO 0 Hz, 부엽 −12 dB(검증 런
  `oai-1x1/20261001T120048Z`).

## 남은 것

- **X5**: 완료(위 절, `run-ocudu-oai-two-cell-tdd.sh`). 미실행: gNB↔gNB 12링크, 2D7U, CLI 하의 UL 트래픽.
- 그림자 쪽 srsUE의 풀버퍼 UL RLF(MCS 28) — UE/링크 문제, 별도 트랙. gNB main_pool 결함은 업스트림 OCUDU 보고 대상.
- OAI 1x1 게이트 기본 `--cont-fo-comp`를 3으로 바꿀지 결정(현재 1).
- 업스트림 보고(FDD 예제의 UE↔UE 엣지): 초안만 있음, 미게시.
