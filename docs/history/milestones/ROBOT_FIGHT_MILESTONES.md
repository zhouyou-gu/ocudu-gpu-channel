# 로봇 파이팅 데모 — 실시간 채널 에뮬레이터 검증 마일스톤

> Historical record; its claims apply to the original date, revision and setup. Migrated from `ROBOT_FIGHT_MILESTONES.md` at `58d3156` without changing recorded measurements.


**목표: 두 로봇의 두뇌(제어기)를 gNB 뒤에, 몸(물리 시뮬레이터)을 UE 뒤에 두고, 제어 루프 전체가 채널 에뮬레이터를 통과하게 한다. 채널은 Sionna RT가 로봇의 실제 위치로부터 실시간으로 만든다(twin). 에뮬레이터의 실시간이 깨지면 그 결과가 로봇의 승패로 나타나야 한다.**

레퍼런스: J. Duan(@DJiafei, 2026-09-23) — Unitree G1 두 대를 MuJoCo 링에 넣고 2 sim-초마다 `state → LLM → joint targets`로 Opus 5.5와 GPT-6 Astra를 겨루게 한 데모. 거기서 선수는 LLM이고 링은 고정이다. **여기서는 두뇌를 양쪽에 똑같이 두고, 선수는 각 로봇과 두뇌를 잇는 무선 링크 — 즉 그 링크를 만드는 에뮬레이터와 그것이 GPU 시간을 받는 방식이다.**

단계 번호는 `R`. 판정 규율은 C·J·S 트랙과 같다: exit≠0만으로 판정하지 않고 로그에서 메커니즘을 확인한다, 실패 측정을 보존한다.

## 무엇을 검증하는가

지금까지의 실시간 증거는 카운터다(1.0× wall-clock, `rx_starvations ≈ 1`, 브로커 p99). 카운터는 "실시간이 아니면 무엇이 어떻게 되는가"에 답하지 못한다. 이 데모는 실시간 위반을 **되돌릴 수 없는 물리 결과**(밀림, 넘어짐, 링 아웃)로 번역한다. 카운터가 증명이고 로봇이 증거다.

Sionna twin이 붙으면 검증 범위가 IQ 경로(브로커)에서 **채널 갱신 경로**(위치 → Sionna → control plane → 슬롯 경계 적용)까지 넓어진다. 이 경로가 같은 wall-clock에서 제때 도는지는 아직 아무도 시험하지 않았다.

## 불변식 — 아무도 기다리지 않는다

이 시뮬이 실시간 검증이 되려면 모든 구성 요소가 wall-clock 하나에 각자 붙어 있고, **누구도 남을 기다리지 않아야** 한다. 어기면 그 판은 무효다.

| 구성 요소 | 시계 | 늦으면 |
|---|---|---|
| gNB / srsUE | wall-clock 1.0× | 슬롯 버림(원래 동작) |
| 브로커 | `--strict-realtime` **켬** | 늦은 슬롯·늦은 control update는 **드롭**, 지연 아님 |
| Sionna 브리지 | 자기 주기(예: 10–20 Hz) | 그 주기의 갱신을 건너뜀, 브로커는 옛 채널로 계속 |
| 물리 시뮬(MuJoCo) | wall-clock 스텝(sim 시간 = 실제 시간) | 명령 없으면 **마지막 명령 유지 또는 0 토크**, 물리는 계속 |
| 두뇌 | 상태가 오면 계산, 안 오면 옛 상태 | 낡은 상태로 판단 |

- 물리 시뮬이 링크를 기다리는 lock-step 모드는 **검증 경기에서 금지**(디버그 도구로만). 링크가 늦어도 결과가 같아져 실시간이 사라진다.
- 시스템 전체가 0.7×로 같이 느려지는 것도 무효다. 경기마다 real-time factor를 기록하고 1.0이 아니면 그 판은 버린다.
- 제어 주기는 링크가 아플 만큼 빠르게(50–100 Hz) 잡는다. 2 s 주기(LLM)는 지연 수십 ms에 둔감해서 에뮬레이터가 무엇을 하든 결과가 안 바뀐다.

## 설계 요약

```
brain-A ──(N6/tun)── gNB ═══╗
                            ║  ocudu-gpu-channel 브로커 (Sionna twin)
brain-B ──(N6/tun)── gNB ═══╣  links: gnb→ueA, ueA→gnb, gnb→ueB, ueB→gnb, ueA↔ueB crosstalk
                            ║
             ueA (srsUE) ═══╝               ┐
             ueB (srsUE) ═══╝               ├─ MuJoCo 한 인스턴스 (링 + 로봇 2대)
                                            ┘
   ueX tun ⇄ UDP ⇄ robot-X: state 업링크(50–100 Hz), 명령 다운링크, seq + timestamp
   MuJoCo 로봇 위치(10–20 Hz) ──► Sionna 브리지(run_bridge.py) ──► 브로커 control plane
```

- 물리 시뮬은 UDP 소켓 뒤에 있어 교체 가능하다. 측정은 MuJoCo(CPU, 에뮬레이터와 GPU를 다투지 않음), 영상은 필요하면 Isaac.
- 로봇은 처음엔 바퀴형 스모봇(differential drive + 앞판). 밸런스 붕괴 같은 채널 외 잡음이 없어 N을 크게 돌릴 수 있다. G1은 데모용으로 나중에.
- UE 스택은 srsUE(Spark 20 MHz에서 실시간 확인된 조합, 2 UE는 `srsue-ra-contention.patch` 필수). gNB는 R4까지 CPU, R5에서 CUDA gNB를 GPU 경합원으로 추가.
- 비교 축은 **GPU 스케줄링**이다. 장애물 vs 깔끔한 링은 결과가 씬으로 정해져 시스템에 대해 말해 주는 게 없다. 스케줄링 배틀은 물리 채널과 두뇌가 대칭이고 다른 것은 "B의 브로커가 GPU 시간을 제때 받느냐"뿐이다. S9에서 이미 본 인과(CUDA gNB p99 195 → 80 µs, time-slicing → MPS)를 로봇이 밀리는 것으로 번역한다.

## 환경

플랫폼 **DGX Spark**(`ssh spark-minwoo`, 컨테이너 `ocudu-minwoo`, GB10 sm_121, CUDA 13.0.88, 드라이버 580.178.04). 레포 `/workspace/gpuch/int0928`(integration-0928), 네이티브 루트 `/workspace/ocudu-spark`, 채널 빌드 `builds/gpuch-int0928-release`. Sionna venv `/workspace/sionna-venv`(sionna-rt 2.0.1, mitsuba 3.8.0, drjit 1.3.1, variant `cuda_ad_mono_polarized`), OptiX `/usr/lib/aarch64-linux-gnu/libnvoptix.so.1`. 게이트 래퍼 `/workspace/gpuch/r0-sionna-1x1.sh`. GPU는 다른 컨테이너와 공유 — 시간 측정마다 다른 GPU 프로세스가 없었음을 기록한다.

## 단계

| 단계 | 내용 | Exit 게이트 | 상태 |
|---|---|---|---|
| **R0** | **Spark에서 Sionna 검증** — venv 고정, cuda variant 확인, `run-ocudu-sionna-1x1.sh` 게이트, `channel_generation_ms` 실측 | Sionna 1x1 live gate PASS(rrc/pdu/ping, 카운터 0), 갱신 1회당 solve 시간 | **완료 2026-09-29** — live-ready rrc/pdu/ping 1/1/1, 106 s 동안 Sionna 갱신 10 Hz 적용(control batch 1,043), solve 49–52 ms, 브로커 kernel 23.6 µs, ping RTT 20–39 ms(avg 29) |
| **R1** | **링 씬** — 링 + 기둥/차폐물 Mitsuba XML, gNB 1 + UE 2 + crosstalk 시나리오 JSON, 커버리지 맵으로 LOS/NLOS 비대칭 확인 | 브리지 dry-run에서 링 위치별 탭이 물리적으로 말이 됨, 6링크 solve 시간이 갱신 주기 예산 안 | **완료 2026-09-29** — `scenes/robot_ring` + `robot-ring.json`(4링크, FDD) / `robot-ring-crosstalk.json`(6링크). GB10 solve 46–53 ms(링크 수 무관, 방향당 1회), 10 Hz 예산 안. LOS −0.5…−2.9 dB, 기둥 그림자 −27…−36 dB(refraction on; 끄면 outage). 발견: 4-UE 커밋 이후 Sionna multi-UE 렌더러가 2-UE 시나리오를 거부 → R4 전 수정 필요 |
| **R2** | **로봇 아레나** — MuJoCo 스모봇 2대, ring-out 규칙, 두뇌 프로세스, UDP 프로토콜(seq+timestamp), 위치 PUB. 무선 없음 | 로컬 루프로 경기 완주, MuJoCo wall-clock 스텝, 명령 부재 정책 정의, 동일 두뇌 승률 50±x% 노이즈 플로어(N≥30) | **완료 2026-09-29** — `examples/robot_fight/` (protocol/arena/brain/fight), 테스트 6개 통과. 30판 노이즈 플로어 5:5:20(승률 0.5, 95% CI 0.24–0.76), RTF 1.000. 핸디캡: +100 ms 편도 → 11:2:5(승률 0.85), +50 ms·손실 10/30% → 잡음 안. 무승부 67–83%는 **R2b에서 해결**(가장자리 타이브레이크 + `reactive` 정책: 24판 배치 무승부 0, ring_out 100 %); netns 모뎀 완료(Spark root netns 테스트 통과). **링크 민감도는 미달**: 편도 ≤100 ms·손실 20 %·정전 20 % duty 어느 것도 승률을 재현 가능하게 바꾸지 못함 → R5 1차 지표는 링크 지표, 경기는 데모(R2b 절) |
| **R3** | **위치 → Sionna live** — 브리지 `update_positions`를 외부 입력(ZMQ SUB)으로 교체, MuJoCo → 브리지 → 브로커 | 로봇이 기둥 뒤로 가면 KPI 패널의 채널이 따라옴, 위치→적용 지연 < 예산 | **부분 완료 2026-09-29** — 브리지 `--position-endpoint` + 게이트 env 배선, 단위 테스트 11개, Spark dry-run에서 원 궤도 추종 오차 0 m, solve 유지(링 씬 6링크 53 ms). MuJoCo→KPI 실물 연결은 R2/R4에서 |
| **R4** | **무선 폐루프** — srsUE 2대 attach(`run-ocudu-sionna-multi-ue.sh` 기반), 제어 루프가 tun 통과, RTT/손실 로그를 브로커 슬롯 로그와 조인 | strict-realtime on, RTF = 1.0 로그, 경기 완주, RTT 분포, 무효 판 규칙 적용 | **부분 완료 2026-09-29 (R4a 무선 절반)** — 2-UE Sionna 렌더러 수정, 절대 수신 잡음 바닥(`OCUDU_NATIVE_SIONNA_AWGN_SNR_DB`, 기본 40), 게이트 훅(`MUE_UE_EXEC`/`ROOT_EXEC`/duration/strict/핀/캡처/UE metrics CSV). 링 씬 walk 시나리오 PASS: LOS UE 34 dB, 그림자 UE 31→16 dB(r=0.42), ping 32/37 ms. **R4b(아레나 절반) 완료(조건부) 2026-09-29** — 에뮬레이터 링크 위에서 32판 전부 완주(16:16, RTF 1.000 전 경기), RTT p50 21 ms / 편도 8.4 ms / stale 0, Sionna 외부 위치 10 Hz, 조인 타임라인(`analyze_run.py`). 조건: 링을 기둥 동쪽 LOS에 두었고(그림자 진입 시 UL이 먼저 죽어 두 UE 동시 RLF), strict-realtime은 attach starvation 때문에 off. 발견: UL PUSCH SINR(2.5–5 dB)은 잡음 knob과 무관, 동시 `cmake -j20`만으로 RTT 4배 · R4c(09-30): UL PUSCH SINR 5–8 dB 바닥은 에뮬레이터 밖(직결 ZMQ·OAI UE에서 동일, `docs/plans/r4c-ul-noise.patch`), 브로커 heartbeat에 초당 starvation/gap/overflow 델타 추가 |
| **R5** | **스케줄링 배틀** — 브로커 2개(셀 2개), GPU 경합원(Sionna 버스트 + hog/CUDA gNB), 스케줄링 off vs on, N판 | 스케줄링 유무로 승률이 뒤집히고 브로커별 `rx_starvations`/`node_stall`/`process_us` p99가 그 이유를 설명 | **완료 2026-09-30** — R5b: 밸런스 봇, hog(200 µs×2×16, MPS) 아래 **a protected / b plain 77판 63:12, ue0 승률 0.84(0.74–0.91), ue1 넘어짐 59회**; 대조 plain/plain 0.33, protected/protected 0.48, 경합 없음 0.57. 원인: b 브로커 슬롯당 2.2 ms quantum 대기 → RTT 20 → 37 ms, a는 MPS+high로 0.4 ms. R5a: — `cuda_stream_priority` 손잡이, `ocudu-gpu-hog`, 2셀·2브로커 게이트 `run-ocudu-robot-fight.sh` 12런 PASS. 컨텍스트 간 time slicing은 안에서 못 이기고(2 ms 커널 세입자 → 모두 2.3 ms/슬롯), 경합원을 브로커와 한 MPS 컨텍스트에 두고 브로커 스트림을 high로 하면 p99 1,493 → 211 µs(Sionna), 1,866 → 859 µs(200 µs hog). FIFO는 해롭다. 아레나 결합은 R5b |
| **R6** | **데모 패키징** — 영상, KPI 패널 동기 재생, 선택: LLM 두뇌 / G1 / Isaac | 발표용 영상 1편 | 부분 완료 2026-09-30 (R6a) — `render_replay.py`: 게이트 런 로그에서 실시간 속도의 리플레이 MP4(링 + 링크 패널 동기) + 런 요약 PNG. R4b 8판(78.5 s), R5b 32판(5:19) 렌더. 3D(MuJoCo EGL)는 R6c에서 완료; LLM·G1은 미착수. (R6b) 라이브 웹 UI에 gNB KPI 패널: robot-fight 게이트가 gNB마다 metrics 릴레이를 띄우고(기본 on) 팔로워가 8080/8081에 셀별 KPI를 붙임 |
| **R7** | **지연 보상 제어** — 모델 기반 예측, 같은 게인, 정책 좌우 교환 | 실환경 반복 비교와 단절·손실 한계 검증 | **완료 2026-09-30 (측정 범위 한정)** — 새 시드·속도 변동 0·300초씩 반복: 공통 44경기 보상33승/일반10승/무승부1, 넘어짐8/13. AWGN/RAN 9조건 및 로컬 96경기 검증. [보고서·그래프](../../reports/experiments/robot-fight-r7/README.md) |

R0–R2는 서로 독립(병렬 가능), R3부터 직렬.

## 위험

- **Sionna solve 시간 vs 로봇 속도.** 3.5 GHz에서 파장 8.6 cm, 로봇 1 m/s면 채널이 ~40 ms마다 바뀐다. R0 실측 1x1(2링크) 49 ms → 6링크는 ~150 ms(≈6 Hz)로 예상. 줄이는 손잡이: `samples_per_source`(200k) 축소, 같은 주파수면 방향당 재추적 공유, 씬 축소. R1에서 예산을 정한다.
- **한 GPU에 CUDA 세입자 3–4개**(브로커, Sionna, CUDA gNB, [Isaac]). 이것은 위험이 아니라 R5의 주제다. R4까지는 세입자를 브로커 + Sionna로 제한한다.
- srsUE 2 UE는 contention 패치가 있어야 같은 C-RNTI로 합쳐지지 않는다(S16).
- 게이트 tmux를 죽이면 고아가 남는다(S16 관찰). 종료는 exit 마커로 기다린다.

## 진행 기록

### R0 — 2026-09-29 (Spark)

- Sionna는 Spark에 **설치돼 있지 않았다**(검증 전무). `/workspace/sionna-venv`에 프로젝트 핀 `sionna-rt==2.0.1` 설치 → mitsuba 3.8.0 / drjit 1.3.1 aarch64 휠로 해결, `mi.variant()` = `cuda_ad_mono_polarized`, `dr.has_backend(CUDA)` = 1. `jitc_llvm_init(): LLVM API initialization failed` 경고는 LLVM CPU 백엔드 부재이고 CUDA 경로에는 무해.
- 내장 street-canyon, 1 TX/1 RX, depth 3, 200k samples: 첫 solve 0.96 s(JIT), 이후 **18 ms**, 경로 11개.
- 프로젝트 브리지 dry-run(`examples/configs/sionna/scenarios/simple_street/ocudu-docker.json`, 10 Hz, 8 s): `channel_generation` **48.6 / 48.9 / 49.7 ms(min/median/max)**, 방향당 24 ms(downlink·uplink 순차, 1842.5 / 1747.5 MHz). 10 Hz 갱신에 맞는다.
- Spark 트리를 `c786ad8` → `9043202`(integration-0928 HEAD)로 올림. 이전 미커밋 diff(S14 부분, 이미 `91c581c`로 커밋된 내용)는 `/workspace/gpuch/int0928-uncommitted-0929.diff`에 보관.
- 라이브 게이트 `r0-sionna-1x1.sh`(tmux `r0`, 결과 `results/{logs,reports}/ocudu-sionna-1x1/20260929T122820Z`): **PASS**. `live-ready.json` rrc_connected/pdu_session_established/ping_ok = 1/1/1, ping 3/3 RTT 20.2/29.5/38.7 ms. 106 s 라이브 동안 브리지 iteration ~1,040(10 Hz), 라이브 solve 52 ms(방향당 25–26 ms), `control_transaction` 0.48 ms, generate→ack 52.7 ms. 브로커 `gpu_timings` h2d 7.1 / kernel 23.6 / d2h 1.0 µs, rx_ring peak_one_way 1,000 µs, room_stall 0. GPU 세입자는 브로커(176 MiB) + Sionna(731 MiB)뿐.
- `node_stall`(ue0/gnb0 input_data 2–5.7 s)은 t=1–4 s, 즉 첫 atomic profile update 이후 라디오가 뜨는 attach 구간에서만 났고 이후 없음. `rx_starvations=7` 한 줄 관측 — 같은 구간으로 추정, R4에서 슬롯 로그와 조인해 확인.
- 관찰: 브로커 로그에 `control_warmup_begin/end`가 갱신마다 찍힌다(1,825회). 브리지가 `profile_swap` 경로를 쓰므로 갱신마다 delay line이 zero-fill되고 warmup에 들어간다 — [sionna-live-channel.md](../../development/designs/sionna-live-channel.md)가 지적한 S0 호환 경로 그대로다. 10 Hz에서는 attach·ping에 문제가 없었지만, 로봇 제어 루프(50–100 Hz)에서 갱신마다 채널이 끊기는 것이 BLER에 보이는지 R4에서 본다.
- 하네스 교훈: sionna 모드 게이트는 duration 없이 Ctrl-C까지 돈다. `tmux send-keys C-c`는 래퍼 셸까지 죽여 exit 마커가 안 남고 게이트의 종료 요약(`event=stats`)도 안 나온다. 다음부터는 게이트 PID에 `kill -INT`를 보내고 마커는 래퍼가 아니라 폴링 쪽에서 남긴다. 고아 프로세스는 없었다(cleanup trap 정상).

### R1 — 2026-09-29 (링 씬, Spark GB10)

- **씬** `examples/configs/sionna/scenes/robot_ring/`(생성기 `build_robot_ring.py`, stdlib만; `scene.xml` + `meshes/*.ply` + `manifest.json` + `README.md`): 60×60 m 콘크리트 바닥, 내경 4 m·높이 0.3 m 금속 펜스(48분할), 0.8×0.8×3 m 콘크리트 기둥 3개(x=0, y=−1.6/0/+1.6), 15 m 동쪽의 7.5 m 금속 마스트. gNB 안테나 (15, 0, 8). 로봇 UE 안테나 높이 0.4 m(펜스 위). `resolve_scene("robot_ring")`으로 잡힌다.
- **함정 하나:** 처음엔 마스트를 8 m로 만들고 gNB를 그 윗면(z=8.0)에 놓았더니 **gNB 링크 전부 ray 0개(outage)**, UE↔UE만 경로가 있었다. 메시 면 위에 놓인 소스는 아무 경로도 못 만든다. 마스트를 7.5 m로 낮춰 안테나가 0.5 m 뜨게 한 뒤 정상.
- **시나리오** `examples/configs/sionna/scenarios/robot_ring/robot-ring.json`: gnb0 고정, ue0(+x, LOS 쪽)·ue1(−x, 그림자 쪽) 각각 y 방향 5 m pingpong 1 m/s(dry-run·커버리지용, R3가 실제 위치로 교체), DL/UL 4링크. `robot-ring-crosstalk.json`은 UE↔UE crosstalk 양방향을 더한 6링크 변형. **crosstalk를 기본에서 뺀 이유:** 네이티브 fixture는 FDD band 3(DL 1842.5 / UL 1747.5 MHz)라 한 UE의 상향이 다른 UE의 하향 대역에 들어올 수 없는데, 에뮬레이터의 crosstalk는 베이스밴드 주입이고 2–4 m 거리의 UE↔UE 경로는 물리 −47 dB → 어댑터 클램프 **+20 dB**(gNB 신호보다 20 dB 이상 큼)라 켜면 하향을 통째로 재밍한다. TDD 단일 캐리어 실험에서만 쓴다.
- **solve 시간(GB10, dry-run 10 Hz, `channel_generation` min/med/max ms, 정상 상태):** 200k/d3 47.9/55.9/113 · 100k/d3 42.8/46.0/68.7 · 50k/d3 43.1/45.3/86.6 · 20k/d3 45.7/49.6/86.6 · 200k/d2 47.1/48.7/82.6 · 50k/d2 44.8/49.8/64.8. **`samples_per_source`·`max_depth`는 씬이 작아 시간에 거의 영향이 없고**, 링크 수도 무관하다 — Sionna는 (주파수, 배열) 그룹당 한 번 풀고(DL 그룹 ~25 ms + UL 그룹 ~25 ms) 링크는 후처리라 6링크 = 2링크 시간. 위험 항목의 "6링크 ~150 ms" 예상은 빗나갔다(좋은 쪽). 기본값은 **200k / depth 3 유지**(시간 이득이 없으니 경로 수를 줄일 이유가 없다). 첫 solve는 JIT 때문에 0.1–1.6 s.
- **그림자를 outage가 아니라 감쇠로 만드는 방법(sweep2, ue1을 기둥 0 뒤 (−2, −1.6)에 고정):** LOS+specular만: ue1 ray 0(−100 dB). diffuse_reflection on: 그대로 0. **diffraction on: −26.7 dB(DL)/−22 dB(UL)이지만 solve 69 ms(d1)–88 ms(d2)로 10 Hz 예산 초과.** **refraction(투과) on: solve 그대로(47.7 ms)에 콘크리트 기둥 −27.1/−25.6 dB** — 이걸 기본으로 채택(`propagation.refraction: true`). 기둥 재질로 핸디캡 깊이를 정할 수 있다: glass −15.2, wood −20.7, concrete −27.1, plasterboard −28.4, brick −39.9 dB(DL 최강 탭, LOS −2.4 기준).
- **커버리지(`scenes/robot_ring/coverage_grid.py`, 0.5 m 격자 177점, 브리지와 같은 trace, 결과 Spark `/workspace/gpuch/r1/ring-coverage-refr.{csv,json,png}`):** LOS(+x) 66점 −0.54…−2.88 dB(중앙값 −1.6), 그림자(−x) 최악 −35.6 dB, 기둥 사이 틈은 LOS(−2.7…−3.4). 즉 **동쪽 절반 ≈ −2 dB, 서쪽은 기둥 뒤 0.8 m 폭 띠 세 줄이 −27…−36 dB이고 그 사이는 LOS** — 피켓 무늬. refraction 끈 첫 실행에서는 그 띠 31점이 outage였다. LOS/NLOS 차 **≈25–33 dB**.
- **이 차이가 20 MHz srsUE 링크에 의미가 있으려면 잡음 바닥이 있어야 한다.** Sionna 모드 토폴로지는 `sionna_rt` 모델 체인에 tdl 탭만 있고 AWGN 단계가 없다(fixed-TDL multi-UE 토폴로지는 `awgn snr_db 30/15`가 있음). 잡음이 없으면 −27 dB 탭도 float IQ 경로에서는 그냥 디코딩된다. **R4에서 sionna 모델 체인에 `awgn` 단계를 넣어 LOS에서 SNR ~25 dB, 그림자에서 ~0 dB가 되게 잡는다.** 절대 레벨 자체는 검증 범위 안: 브리지 기본 `--gain-offset-db 60`에서 LOS 최강 탭 −0.5…−3 dB는 legacy fixture(−3 dB)와 같고, 마스트를 15 m 밖에 둔 이유가 이것이다(펜스 옆이면 0 dB를 넘는다).
- **발견(스코프 밖, R4 차단 요인):** `scripts/native/render-sionna-multi-ue-configs.py`가 4-UE 옵션 커밋 `9043202` 이후 `legacy.UES` 전체(ue0–ue3)와 비교해서 **2-UE Sionna 시나리오를 거부한다** — 기존 `sionna-multi-ue-sutd.json`도 같은 이유로 거부됨(`run-ocudu-sionna-multi-ue.sh`는 `OCUDU_NATIVE_MUE_UE_COUNT=2` 고정인데 렌더러는 그 슬라이스를 안 본다). `tests/test_robot_ring_scene.py`는 2-UE 슬라이스로 형태 검사를 통과시키고 이 사실을 주석에 남겼다.
- 테스트: `tests/test_robot_ring_scene.py`(시나리오 링크 집합·refraction·씬 해석·지오메트리·생성기 결정성·렌더러 형태) + 기존 `test_sionna_launcher/test_sionna_rt_adapter/test_build_osm_scene/test_demo_topology` 모두 `/usr/bin/python3 -m unittest`로 OK. 워크스테이션 5090 대조 실행은 Sionna venv가 호스트에도 컨테이너에도 없어 생략.

### R2 — 2026-09-29 (로봇 아레나, 워크스테이션 로컬)

**만든 것.** `examples/robot_fight/` — `protocol.py`(와이어 포맷), `arena.py`(MuJoCo 월드 + 심판 + 로봇별 UDP 서버 + 위치 PUB), `brain.py`(로봇당 제어기 프로세스), `fight.py`(N판 러너 + 핸디캡 프록시 + 요약), `README.md`. 테스트 `tests/test_robot_fight_protocol.py`(5) + `tests/test_robot_fight_smoke.py`(5 s 헤드리스 1판, RTF 0.9–1.1 확인) — venv `~/ocudu-work/venvs/robot`(mujoco 3.14.0, numpy 2.5.3, pyzmq 27.2, pytest)에서 6/6 통과.

**프로토콜 (R3·R4 공유).**
- UDP 제어면: 32 B 헤더 `magic RF, version 1, kind(1 STATE/2 CMD), robot_id, seq u32, t_send_us u64, echo_seq u32, t_echo_us u64`. STATE(로봇→두뇌, 100 Hz, 92 B): sim time, 내 pose/속도, 상대 pose/속도(심판이 주는 전역 관측), 링 반지름, 가장자리까지 거리, flags(running/over/won/lost). CMD(두뇌→로봇, 50 Hz, 44 B): 좌/우 바퀴 각속도 + `ttl_ms`. 아레나는 STATE마다 가장 새 CMD를 echo하므로 **RTT = now − t_echo_us**를 시계 동기 없이 양쪽에서 잰다. 편도는 같은 시계일 때만 유효.
- 위치면: ZMQ PUB(아레나 bind, 브리지 connect), 기본 `tcp://127.0.0.1:5570`, 20 Hz, `{"event":"positions","t_unix_ms","frame":"arena","nodes":{"ue0":{"position_m":[x,y,z],"velocity_mps":[..]},"ue1":…}}`. 링 중심 원점, z 위, 미터, z = 안테나 높이 0.3 m. SUB 쪽으로 실측 19 Hz, R3의 `parse` 계약과 일치. `ipc://` 엔드포인트도 그대로 됨(R3 게이트 배선).

**아레나 설계.** 링 반지름 **2.0 m**(4 m 링에선 40 s 안에 아무도 못 밀어냄), 스폰 ±0.5R 마주 보기 + 시드 지터. 봇: 30×24×8 cm 섀시 4 kg(2 kg은 바닥 밸러스트), 반지름 6 cm 구동 바퀴 2개(속도 액추에이터 ±30 rad/s → 1.8 m/s), 캐스터는 바닥에서 4 mm 띄움(**처음엔 캐스터가 무게를 받아 바퀴가 헛돌았다** — 6 s에 0.5 m), 앞판은 낮고(섀시 중심 아래 1 cm) 미끄럽게(μ 0.05; 그전엔 접촉이 걸려 상대가 뒤집혔다). 심판: 몸 중심이 링 밖 → ring_out, 기울기 up<0.3 → fall, 시간 제한(60 s) → draw. **Wall-clock 스텝**: 1 ms 루프마다 물리를 `sim0 + 경과 wall`까지 전진, `rtf`/`late_loops`(>2 ms 뒤처짐)/`max_lag_ms` 기록. `--lockstep`은 디버그 전용, 결과 JSON에 `lockstep: true`로 남는다. 명령 부재 정책 `--stale-policy` **coast**(기본, 모터 드라이버 off = 바퀴가 자유 회전, 밀리는 상태) / zero(제동) / hold(마지막 명령 유지) — 처음 fresh 명령 이후부터 stale 구간을 센다.

**두뇌.** `pusher`: 같은 봇끼리 정면 밀기는 교착이라(head-on 프로브: 6 s 동안 둘 다 제자리) **측면 돌아가 옆구리 밀기** — 상대 진행 방향에 수직인 0.7 m 지점으로 간 뒤 상대 heading에서 55° 이상 벗어나면 돌진, 접촉 1.5 s 지나면 0.6 s 후진하며 반대 측면으로. 가장자리 0.45 m 안에서 바깥 방향이면 감속. 시드로 파라미터 ±10 % 지터 + heading 잡음. `--policy module:callable` 훅(LLM은 나중). 수신 스레드 분리(도착 시각이 틱에 양자화되지 않게), RTT 샘플은 명령당 첫 echo만.

**측정 (워크스테이션, 6판 병렬, 각 프로세스 wall-clock).**

| 배치 | 설정 | ue0:ue1:draw | ue0 승률(결정된 판) | 이유 | ue1 RTT p50 |
|---|---|---|---|---|---|
| 노이즈 플로어 | 링 2.5 m, v 1.2, 60 s, N=30 | 5:5:20 | 0.50 (CI 0.24–0.76) | ring_out 10, timeout 20 | 10 ms |
| +50 ms 편도 (ue1) | 같은 설정, N=12 | 1:1:10 | 0.50 | | 106 ms |
| +100 ms 편도 (ue1) | 같은 설정, N=12 | 3:0:9 | 1.00 (CI 0.44–1) | | 206 ms |
| 손실 10 % (ue1) | 같은 설정, N=12 | 3:1:8 | 0.75 | | 6 ms, cmd gap 257 |
| 손실 30 % (ue1) | 같은 설정, N=12 | 1:2:9 | 0.33 | stale 4.2회/0.13 s | 7 ms, cmd gap 812 |
| 노이즈 플로어 t2 | **링 2.0 m, v 1.6**(현재 기본값), N=18 | 2:1:15 | 0.67 (CI 0.21–0.94) | | 5.5 ms |
| +50 ms t2 | N=18 | 1:2:15 | 0.33 | | 106 ms |
| +100 ms t2 | N=18 | **11:2:5** | **0.85 (CI 0.58–0.96)** | ring_out 13 | 206 ms |

- RTF 모든 판 1.000(min 0.9999), late_loops 판당 13–62(1 ms 루프가 2 ms 넘게 밀린 횟수, max_lag 10–60 ms — 6판 병렬 시 OS 스케줄링), 두뇌 deadline miss ≤0.3회/판. CPU: 아레나 ~12 %/프로세스, 두뇌 ~4 %, 6판 병렬 전체 부하 ~30 %(24코어).
- 로컬 루프 RTT p50 5.5–10 ms = STATE 주기(10 ms) 양자화 + 두뇌 틱(20 ms)의 합이고 링크 자체는 0.6 ms(편도 p50). 즉 무선 없이 제어 루프의 기본 지연이 ~10 ms — R4에서 무선 RTT(R0 ping 29 ms)가 그 위에 얹힌다.
- **정책이 링크에 민감한가**: +100 ms 편도(RTT 206 ms)에서 승률 0.85로 명확히 뒤집힘. +50 ms(RTT 106 ms)와 손실 30 %는 잡음 안. 50 Hz 제어에서 이 정책의 민감도 문턱은 RTT 100–200 ms 사이 — R4의 무선 RTT ~30 ms는 이 정책으로는 그대로 보이지 않는다. 링크 차이를 보이려면 (a) 정책을 더 빠른 반응에 의존하게(회피/카운터, 저속 마찰), (b) 제어 주기를 100 Hz로, (c) 스케줄링 배틀(R5)에서 드롭·starvation을 유도해 stale 구간을 만들기 — 셋 중 R4 착수 전에 결정.
- **무승부 67–83 %가 가장 큰 결함.** 동등한 봇은 대부분 60 s 안에 결판이 안 난다. 승률 통계의 N을 잡아먹으므로 R4 전에 정책 손질(측면 진입 각·후진 규칙) 또는 규칙 변경(제한 시간 뒤 가장자리 거리로 판정) 필요. 후자는 한 줄이고 잡음이 적어 추천.
- 결과: `results/robot-fight/r2-{noise-floor,handicap-d50,handicap-d100,handicap-l10,handicap-l30,t2-nf,t2-d50,t2-d100}/` (fight별 arena/brain JSONL + summary.json/md; git 제외).

**R4가 알아야 할 것.** 아레나는 UE 쪽(로봇마다 소켓 하나, `--robot-bind ue0=<ue0 tun ip>:6000,ue1=<ue1 tun ip>:6001` — srsUE 둘이 각자 netns면 아레나를 둘로 쪼개거나 두 netns에 닿는 곳에서 bind), 두뇌는 gNB/N6 쪽에서 `--robot <ue ip>:port`로 보낸다. 아레나는 첫 CMD에서 두뇌 주소를 배우므로 NAT/tun 뒤여도 된다(`--state-dest`로 고정 가능). 위치 PUB `--positions-endpoint`는 브리지가 있는 netns에서 닿는 `ipc://` 경로로. 편도 지연 로그는 아레나·두뇌가 같은 호스트 시계일 때만 의미 있다. 두뇌는 STATE의 over 플래그에서 종료.

### R3 — 2026-09-29 (브리지 외부 위치 입력, Spark dry-run)

**인터페이스.** `scripts/sionna_rt/run_bridge.py`에 `--position-endpoint <zmq>`(SUB, connect), `--position-frame-offset x,y,z`(아레나 원점을 씬 미터로; 모든 라이브 위치에 더함), `--position-timeout-s`(기본 1.0) 추가. 플래그가 없으면 `update_positions`는 이전과 같은 코드 경로(스크립트 Motion)를 탄다. 메시지는 R2가 정한 그대로:

```json
{"event":"positions","t_unix_ms":1790000000000,"frame":"arena",
 "nodes":{"ue0":{"position_m":[x,y,z],"velocity_mps":[vx,vy,vz]},"ue1":{...}}}
```

- 갱신마다 소켓을 **끝까지 비우고 가장 새 프레임만** 쓴다(latest-wins). 느린 trace 뒤에 낡은 위치가 줄줄이 재생되는 일이 없다. 밀려난 프레임은 `messages_dropped`로 센다.
- 프레임에 없는 노드는 그대로(스크립트 경로), 시나리오에 없는 노드 id는 무시하고 한 번만 경고. `velocity_mps`가 없으면 정지로 본다(도플러 0). 속도는 기존 `velocity_at`과 같은 경로로 Sionna tx/rx `.velocity`에 들어간다.
- 타임아웃이 지나면 **마지막 위치를 유지**하고 `stale: true`로 표시한다. 갱신 루프는 어떤 경우에도 피드를 기다리지 않는다(불변식 "아무도 기다리지 않는다"). 첫 프레임 전에는 `pending: true`이고 스크립트 위치를 쓴다.
- `sionna_rt_update` 레코드에 `position_source`("scripted"|"external") 와 `position_status`(endpoint, offset, `messages_received/invalid/dropped`, `last_sample_t_unix_ms`, `last_sample_age_ms`(poll 이후), `last_sample_publish_age_ms`(퍼블리셔 시각 기준 — 피드 지연), `stale`, `pending`, `ignored_nodes`, `node_sources`)가 추가됐다. `environment`에는 `position_endpoint/frame_offset_m/timeout_s`.

**게이트 배선.** `run-ocudu-legacy-1x1.sh`(→ `run-ocudu-sionna-1x1.sh`)와 `run-ocudu-multi-ue.sh`(→ `run-ocudu-sionna-multi-ue.sh`)에 `OCUDU_NATIVE_SIONNA_POSITION_ENDPOINT`, `OCUDU_NATIVE_SIONNA_POSITION_OFFSET`(`x,y,z`), `OCUDU_NATIVE_SIONNA_POSITION_TIMEOUT_S` → inner `--sionna-position-{endpoint,offset,timeout-s}` → 브리지 인자. 비어 있으면 브리지 명령줄은 이전과 동일. **브리지는 `unshare --net` 안에서 돌므로 `tcp://127.0.0.1`은 그 네임스페이스의 loopback이라 호스트 퍼블리셔에 닿지 않는다.** 그래서 게이트는 endpoint가 `ipc:///…` 절대 경로일 때만 받는다 — control/telemetry 소켓과 같은 규칙. 마운트 네임스페이스는 `/run/netns`만 bind-mount하므로 나머지 파일시스템은 공유되고, **아레나가 바깥에서 `ipc://` 소켓을 bind**하면 안쪽 브리지가 connect한다(connect가 bind보다 먼저여도 ZMQ가 재접속). 권장 경로: `ipc://${OCUDU_NATIVE_ROOT}/run/arena/positions.sock`(예: Spark `ipc:///workspace/ocudu-spark/run/arena/positions.sock`). `scripts/native/README.md`에 기록.

**테스트.** `tests/test_sionna_positions.py`(11개, Sionna·pyzmq 없이 가짜 transport로): 피드 없음→스크립트+pending, offset 적용·속도 전달·gNB는 스크립트 유지, latest-wins와 dropped 카운트, 타임아웃→마지막 위치 유지+stale, 미지 노드 1회 경고, 깨진 프레임 6종이 마지막 샘플을 덮지 않음, 인자 기본값/환경 레코드/타임아웃 검증. 워크스테이션 `python3 -m unittest tests.test_sionna_positions tests.test_sionna_rt_adapter tests.test_sionna_launcher` → 44 OK; Spark venv에서도 OK. 게이트 스크립트 4개 `bash -n` 통과.

**Spark dry-run(GB10, venv, 다른 GPU 프로세스 없음).** 퍼블리셔 `/workspace/gpuch/r3_publisher.py`: ue0가 반경 3 m 원을 0.5 rad/s로, ue1 정지, 미지 노드 `robotX` 포함, 20 Hz. 브리지 10 Hz `--dry-run`, 판정 `/workspace/gpuch/r3_check.py`(퍼블리셔 로그의 `t_unix_ms`와 레코드의 `last_sample_t_unix_ms`를 맞춰 위치 비교).

| 런 | 시나리오 | 갱신 | external | 위치 오차 max | solve 중앙/최대 | received/dropped/invalid | 비고 |
|---|---|---|---|---|---|---|---|
| A (10 s) | `ocudu-docker-multi-ue.json`, offset `-70,0,1.5` | 97 | 96 | **0.0 m** (96 매칭) | 43.5 / 463 ms(첫 갱신 JIT) | 196 / 100 / 0 | 첫 갱신만 scripted(SUB 접속 전), `robotX` 1회 경고, stale 없음 |
| B (8 s, 피드 3 s 후 중단) | 같음 | 80 | 79 | — | — | 48 | 3 s 이후 `stale: true`, age 5.6 s까지 증가, ue0 마지막 위치 유지 |
| C (6 s) | **R1 `robot-ring.json`**(링 씬, gNB 1 + UE 2, 6링크) | 61 | 60 | **0.0 m** (60 매칭) | **52.9 / 109 ms** | 119 / 59 / 0 | 방향별 17.3 / 33.6 ms, `publish_age` 44 ms |

- 20 Hz 피드를 10 Hz로 소비하니 절반이 dropped로 찍히는 게 정상이다(latest-wins). R4에서 피드 주기는 브리지 갱신 주기와 같거나 약간 높게 둔다.
- 링 씬 6링크가 53 ms로 1x1(49 ms)과 거의 같다 — Sionna는 방향(주파수)당 한 번 풀고 링크 수는 후처리라, 위험 항목의 "6링크 ~150 ms" 예상은 **빗나갔다(좋은 쪽으로)**. 10 Hz 예산 안.
- 위치→채널 적용 지연의 R3 몫은 poll 직후 resolve라 <1 ms; 전체 지연은 피드 주기 + solve(~50 ms) + control ack(0.5 ms) + 슬롯 경계. R4에서 브로커 `control_update` 슬롯과 조인해 측정.
- 남은 것(R2/R4): 실제 MuJoCo 아레나가 `ipc://` 소켓을 bind해 위 메시지를 보내고, 게이트 env로 브리지에 연결한 뒤 KPI 패널에서 채널이 따라오는 것을 확인. 게이트에는 이미 `OCUDU_NATIVE_SIONNA_DURATION_SECONDS`가 있어 sionna 1x1 게이트를 자동 종료시킬 수 있다(R0 교훈의 Ctrl-C 문제 회피).

### R4a — 2026-09-29 (무선 절반: 2-UE Sionna 게이트를 링 씬에서, Spark GB10)

R4의 무선 쪽 준비다. 아레나·두뇌(R2)를 붙이는 R4b는 별도. 결과 디렉터리는 전부 Spark `/workspace/ocudu-spark/results/{logs,reports}/ocudu-multi-ue/<ts>`, 래퍼 `/workspace/gpuch/r4a-run.sh <tag> [ENV=..]`, 분석 JSON `/workspace/gpuch/r4a-*-analysis.json`. GPU에 다른 프로세스 없음(런마다 `r4a-<tag>.gpu-before` 기록).

**고친 것 두 가지(R1이 찾은 차단 요인).**
1. `render-sionna-multi-ue-configs.py`가 4-UE 커밋 `9043202` 이후 2-UE 시나리오를 거부했다(테이블 전체 ue0–ue3와 비교, 그리고 `validate_subscriber`를 옛 시그니처로 호출해 TypeError). 이제 `--ue-count`(게이트의 `OCUDU_NATIVE_MUE_UE_COUNT`) 슬라이스와 비교하고 그 슬라이스의 가입자 fixture를 쓴다. 4-UE Sionna도 시나리오가 ue0–ue3를 이름 짓기만 하면 통과한다(self-test + `tests/test_sionna_multi_ue_renderer.py`).
2. Sionna 모드 토폴로지에 잡음이 없어 −27 dB 그림자도 무잡음으로 디코딩됐다. **`awgn snr_db` 스텝으로는 못 고친다** — 두 백엔드 모두 σ를 *현재(페이딩 후) 입력 전력*에서 유도하므로(`cpu_backend.cpp` Awgn, `cuda_backend.cu` build_steps) 그림자 UE도 같은 SNR을 받는다. 그래서 노드마다 `rx_model`(합산 수신 신호에 한 번 적용)에 **절대 `noise_power`** 를 둔다. `profile_swap`은 링크의 선행 tdl만 갈아끼우므로 바닥은 갱신 후에도 남는다(라이브 2,391회 갱신 동안 유지 확인). 코드 변경 없음(`noise_power`는 YAML-only 절대값으로 이미 있었다; `topology.sionna-multi-gnb.cuda.yaml`의 "reference power 1.0으로 SNR 변환" 주석은 코드와 맞지 않는다 — config.cpp는 부호 검사만 한다).

**잡음 바닥의 근거.** `noise = tx_power / 10^(ref_snr/10)`, ref_snr = 0 dB 탭 링크가 보는 SNR(`OCUDU_NATIVE_SIONNA_AWGN_SNR_DB`), tx_power는 **wire capture로 잰 실제 송신 레벨**(새 `OCUDU_NATIVE_MUE_WIRE_CAPTURE_SAMPLES`, `wire-capture-power.py`; 활성 샘플 = 피크 전력의 1e-3 초과, 유휴 슬롯 제외). 보정 런 `20260929T130021Z`(잡음 off, capture 4.6M 샘플 @100 s):

| 포트 | 방향 | 활성 비율 | 활성 평균 |x|² | 피크 진폭 |
|---|---|---|---|---|
| gnb0 | tx_in (OCUDU gNB 송신) | 5.0 % | **1.12e-2 (−19.5 dB)** | 0.39 |
| ue0 / ue1 | tx_in (srsUE 송신) | 0.7 % | **2.8e4 / 3.3e4 (+44/+45 dB)** | 313 |
| ue0 (LOS, −2.5 dB) | rx_out | 5.0 % | 1.13e-2 (−19.5 dB) | 0.39 |
| ue1 (그림자, −27 dB) | rx_out | 3.6 % | 1.9e-5 (−47.2 dB) | 0.015 |

srsUE의 ZMQ 라디오는 `[rf] tx_gain = 50`을 수치로 곱한다(10^(50/20) ≈ 316 = 피크 313), 그래서 UL은 DL보다 64 dB 크다. 워크스테이션 c4 캡처(CUDA gNB 4T4R)의 gNB 송신 −20.2 dB와 일치. 렌더러 상수 `TX_POWER_DL = 1.12e-2`, `TX_POWER_UL = 3.0e4`, env로 덮어쓸 수 있다. UL 기준은 ping만 있는 런의 PUCCH/SRS/작은 PUSCH 활동이라 광대역 PUSCH 기준으로는 보수적(잡음이 상대적으로 큼)일 수 있다 — R4b에서 트래픽이 생기면 gNB 쪽 UL BLER로 재확인.

**ref_snr 선택 — 실측(같은 링 씬, srsUE가 보고한 `dl_snr`).** 처음 목표(LOS 25 / 그림자 0 dB)는 srsUE가 못 버틴다.

| 런 | ref | 경로 | ue0 (LOS −2.5 dB) | ue1 (기둥 쪽) | 판정 |
|---|---|---|---|---|---|
| `130021Z` cal | off | robot-ring 1 m/s | 38 (포화) | 38, 그림자에서도 38 | PASS, 무잡음 |
| `130540Z` n1 | 26.6 | 〃 | **27** (공칭 24) | RRC만, PDU 실패, snr −10…+10 | FAIL |
| `131059Z` n2 | 34.6 | 〃 | 31 (공칭 32) | RRC 없음. gNB는 preamble 8을 검출(metric 45, 15 dB)해 RAR을 보냈지만 UE가 못 받음 — 첫 1 s 안에 기둥 그림자로 들어가며 RAR/Msg3이 그림자에 떨어진다(1 m/s면 LOS 틈이 레그당 ~0.5 s) | FAIL |
| `131640Z` n3 | 40 | 〃 | 34 (공칭 37.5) | RRC 없음(같은 이유) | FAIL |
| `132216Z` w1 | 34.6 | **robot-ring-walk** 0.2 m/s, LOS에서 출발 | 31 | attach 됨. LOS 틈 중앙 6.9 / 그림자 9.5 (재동기 상태가 섞임), out-of-sync 5,540줄(10 s마다 60–390), RRC Release 1회, ping 863 ms | PASS지만 불건전 |
| `132714Z` w2 | **40** | 〃 | **34** | **LOS 중앙 31 / 그림자 중앙 16, r(gain, snr) = 0.42**, out-of-sync 1,500줄(가장 깊은 −40…−44 dB 지점에서만 버스트), Release 없음, ping 37 ms, dl_bler 그림자 평균 3.2 % | **PASS — 기본값** |

- 보고값과 공칭값: 26.6 → 27(+3), 34.6 → 31(−1), 40 → 34(−3.5). srsUE의 추정이 고 SNR에서 눌린다. 그림자(공칭 ref − 27)는 40에서 16 dB로 읽힌다.
- **왜 1 m/s 시나리오로는 안 되나.** x = −2 선을 따라 기둥 그림자(|y| ∈ [1.5, 2.0], [−0.25, 0.25]…)와 LOS 틈이 ~1 m마다 번갈아 든다(R1 coverage: −27 ↔ −2.9 dB). srsUE의 초기 접속(PRACH → RAR → Msg3 → RRC)은 25 dB 계단을 몇 초마다 맞으면 끝나지 않는다. 그래서 `examples/configs/sionna/scenarios/robot_ring/robot-ring-walk.json`(R1 씬 그대로, 두 UE가 LOS 끝 y = ±3.4에서 출발해 0.2 m/s로 왕복)을 만들었다. R4b의 아레나는 로봇을 LOS에서 스폰하면 된다.
- 잡음 바닥이 GPU에 주는 비용: 브로커 kernel p50 19.6 → 23.9 µs(노드 3개 × AddNoise), p90 26 µs. `rx_starvations`는 cal 5 / w2 51(전부 attach 구간, heartbeat idle 52–53으로 세 장치가 같음).
- 회귀: 이전 기본 시나리오 `sionna-multi-ue-sutd.json`을 새 기본값(40)으로 `133230Z` — **PASS**(두 UE attach·ping, 카운터 0, starvation 5). ue0(차량) gain −16…−7.5 dB ↔ snr 20–30, r = 0.84. ue1(보행자)은 Sionna가 경로를 못 주는 구간(−100 dB)에서 out-of-sync — 바닥이 없을 때는 그 −100 dB 탭도 무잡음으로 디코딩됐으니, 이제 "통과"의 뜻이 물리적으로 맞아졌다.

**게이트 확장(`run-ocudu-multi-ue.sh` / `-inner.sh`, 기본값은 이전과 동일).** `OCUDU_NATIVE_SIONNA_AWGN_SNR_DB`(40, `off`), `OCUDU_NATIVE_SIONNA_TX_POWER_DL/UL`, `OCUDU_NATIVE_MUE_DURATION_SECONDS`(240, ≥160), `OCUDU_NATIVE_MUE_STRICT_REALTIME`, `OCUDU_NATIVE_MUE_UE_EXEC`(UE netns 안, ping 통과 직후, `{ue_id} {ue_ip} {ue_index} {ue_netns} {ue_gateway} {log_dir} {run_dir} {config_dir}`), `OCUDU_NATIVE_MUE_ROOT_EXEC`(스택 루트 ns, ogstun 직후, `{ue_ids} {ue_ips} …`), `OCUDU_NATIVE_MUE_WIRE_CAPTURE_SAMPLES/_SKIP_SECONDS`, `OCUDU_NATIVE_MUE_PIN_UES`. OAI 게이트와 같은 `platform-profile.py` 배치(spark-gb10: gNB 5-9 / 브로커 15-17 + `OCG_BROKER_SPIN=1` / srsUE 2대 18,19 공유 — 여섯 런 모두 rf_o/u/l = 0). srsUE마다 `--general.metrics_csv_enable`로 초당 metrics(`srsue-metrics-<ue>.csv` + `srsue-<ue>.start_unix_ms`). `attach-summary.json`에 control 카운터, 장치별 마지막 heartbeat, Sionna 갱신 수·position_source, rx_noise, `run-parameters.json`을 기록(판정은 그대로). 새 도구 `analyze-sionna-multi-ue-run.py`(metrics CSV ↔ 브리지 위치·링크 gain 조인, LOS/그림자 통계·상관), `wire-capture-power.py`. 문서 `scripts/native/README.md`.

**R4b가 쓸 것.** 아레나는 스택 루트 ns에서 `OCUDU_NATIVE_MUE_ROOT_EXEC`로(브로커·브리지·ogstun 10.45.1.1과 같은 netns → 위치 PUB는 `tcp://127.0.0.1`도 되지만 게이트는 `ipc://` 절대경로만 받는다: `OCUDU_NATIVE_SIONNA_POSITION_ENDPOINT=ipc:///workspace/ocudu-spark/run/arena/positions.sock`, offset은 링 원점이 씬 원점이라 `0,0,0`), 로봇 쪽 모뎀/릴레이는 `OCUDU_NATIVE_MUE_UE_EXEC`로 UE netns 안에서 `{ue_ip}`(10.45.1.2/.3)에 bind하고 두뇌는 `{ue_gateway}` 10.45.1.1. 시나리오는 `robot-ring-walk.json`을 외부 위치 입력으로 덮어쓰는 형태(스폰은 LOS 쪽). 브로커 ping RTT 기준선 30–37 ms.

**열린 것.** (1) legacy 1x1 Sionna 게이트는 "immutable legacy topology"를 그대로 profile_swap하므로 잡음 바닥 env를 받지 않는다 — 필요하면 렌더 후 `rx_model` 패치로. (2) 기둥 가장자리의 −40…−44 dB 지점에서는 40 dB 기준으로도 srsUE가 잠깐 동기를 잃는다; 더 부드러운 대비가 필요하면 R1의 재질 손잡이(glass −15 dB) 또는 ref 45. (3) 그림자에서의 BLER·MCS 저하는 트래픽이 있어야 보인다(지금은 ping뿐이라 MCS 0) — R4b의 50–100 Hz 제어 루프가 그 트래픽이다. (4) UL 잡음 기준의 보수성(위).

### R2b — 2026-09-29 (아레나 후속: 무승부 제거, 반응형 정책, netns 모뎀, 링크 민감도 실측)

**1. 무승부 제거 — 해결.** 제한 시간에 **몸 중심이 가장자리에 더 가까운 쪽이 진다**(`timeout_edge`); 두 로봇의 반경 차가 `--timeout-margin-m`(5 cm) 미만일 때만 진짜 무승부(`timeout`). 결과 JSON에 `decided_by`, `radial_m`. 새 기본 정책과 합쳐 24판 배치 전부 무승부 0, 결판 사유 100 % `ring_out`(평균 6.5–9.9 s). `tests/test_robot_fight_referee.py`(5)가 규칙을 고정한다.

**2. 반응형 정책 `reactive`(기본), 100 Hz 두뇌 / 200 Hz STATE / ttl 60 ms.** 같은 봇끼리 정면 밀기는 교착이므로 "누가 먼저 옆구리를 잡나, 누가 먼저 밀리는 걸 알아채나"로 설계했다. 우선순위: **escape**(접촉 중 밀리며 가장자리 0.6 m 안 → 접선 방향 탈출 0.45 s) → **edge guard**(바깥 방향으로 달리며 남은 거리 < v²/2a + 10 cm → 급제동·안쪽 선회; 늦은 두뇌는 지연×속도만큼 더 나간다) → **dodge**(0.9 m 안에서 정면 돌진해 오면 0.3 s 옆걸음) → **disengage**(정면 교착 0.35 s → 후진·측면 교체) → **push**(상대 옆을 잡았으면 상대 예측 위치를 지나 가장자리 쪽으로 밀기) → **flank**. 공격 목표는 상대 속도×0.12 s 리드. 두뇌 결과에 반사 횟수(`reflexes`). `pusher`(R2)는 `--policy pusher`로 남긴다.

**3. netns 모뎀 — 완료.** `examples/robot_fight/modem.py`(stdlib만): UE netns 안에서 TUN 주소에 UDP bind, 아레나와는 파일시스템 unix datagram 소켓(netns를 넘는다)으로 중계. 아레나 `--robot-unix ue0=/path.sock,…`. 첫 UDP 데이터그램에서 두뇌 주소를 배우고(`--brain`으로 고정 가능), 5 s마다 상태 JSON. **오버헤드**: 왕복 p50 24–27 µs vs 순수 UDP 루프백 6–7 µs → 릴레이 +18–20 µs 왕복(≈10 µs/방향); 라이브 경기에서 모뎀 경유 로봇의 편도 p50 639 µs vs UDP 569 µs. `tests/test_robot_fight_modem.py`: 바이트 왕복·오버헤드(<1 ms) + **실제 netns 케이스**(`unshare -n` 안의 모뎀+가짜 두뇌, 밖의 가짜 아레나) — 워크스테이션은 비특권이라 skip, **Spark 컨테이너 root에서 2/2 통과**. AF_UNIX 경로 107 B 제한 때문에 소켓은 짧은 런타임 디렉터리에 둔다. `fight.py --modem 1`은 netns 없이 릴레이만 끼워 검증.

**4. 링크 민감도 dose–response — 목표 미달, 정직하게 기록.** 워크스테이션, 24판/점, 6판 병렬, 25 s, RTF 전부 1.000, `--handicap`은 ue1의 양방향 프록시.

| 정책·물리 | 핸디캡(ue1) | ue0:ue1 | ue0 승률 (95 % CI) | 비고 |
|---|---|---|---|---|
| reactive, 1.8 m/s | 없음 | 12:12 | 0.50 (0.31–0.69) | 노이즈 플로어, 무승부 0 |
| | 편도 +20 ms | 14:10 | 0.58 (0.39–0.76) | |
| | 편도 +40 ms | 18:6 | 0.75 (0.55–0.88) | |
| | 편도 +60 ms | 14:10 | 0.58 (0.39–0.76) | |
| | 편도 +100 ms | 14:10 | 0.58 (0.39–0.76) | RTT 203 ms |
| | 편도 +100 ms **ue0에** | 11:13 | 0.46 (0.28–0.65) | 대조 |
| | 손실 5 % / 20 % | 16:8 / 12:12 | 0.67 / 0.50 | stale 거의 없음(100 Hz + ttl 60) |
| | 정전 60/100/150 ms per 500 ms | 17:7 / 7:17 / 8:16 | 0.71 / **0.29** / 0.33 | coast: 정전 중 힘이 빠진 쪽이 오히려 이김 |
| | 정전 100 ms, stale=zero / hold / zero-ue0 | 10:14 / 13:11 / 11:13 | 0.42 / 0.54 / 0.46 | 정책 무관 |
| pusher(R2), 50 Hz | 없음 / +60 / +100 ms | 14:9 / 13:7 / 14:9 | 0.61 / 0.65 / 0.61 | R2의 "0.85@+100 ms"(N=18) 재현 안 됨 |
| reactive, **2.6 m/s**(`--wheel-max-rad-s 45`) | 없음 / +60 / +100 ms / 정전 100 | 12:12 / **19:5** / 12:12 / 14:10 | 0.50 / 0.79 (0.60–0.91) / 0.50 / 0.58 | +60만 유의, +100은 0.50 → 단조 아님 |

- **결론: 이 로봇·이 물리에서는 편도 지연 ≤100 ms(RTT ≤200 ms), 손실 20 %, 20 % duty 정전 어느 것도 승률을 재현 가능하게 바꾸지 못한다.** 약 20개 배치 중 유의해 보이는 점(+40 ms 0.75, 2.6 m/s +60 ms 0.79)은 이웃 점이 0.5로 돌아가므로 다중 비교 잡음으로 본다. 결판의 ~80 %가 접촉 중 밀려 나가는 것(`pushed`)이고 반사는 판당 1–3회뿐이라 **승패는 밀기 물리가 정한다**, 반응 시간이 아니라.
- 정전(outage)에서는 손실한 쪽이 이기는 경향까지 보였다(coast 0.29): 밀리는 중에 힘이 빠지면 자유 회전해 밀려 나가는 게 아니라 공격자가 헛밀고 지나치는 "유도" 효과 — 물리는 진짜지만 데모 메시지에는 반대다.
- **R4·R5에 대한 함의.** (a) 경기는 워크로드·시각화이지 ≤100 ms 급 링크 차이를 보는 계측기가 아니다. R5의 GPU 스케줄링 효과는 S9에서 p99 150 µs 급이었다 — 슬롯 드롭·RLF로 번지지 않는 한 **경기로는 절대 보이지 않는다**. R5의 "스케줄링 off" 조건은 브로커가 실제로 슬롯을 놓쳐 UE가 굶는(starvation → 재접속) 수준의 부하여야 하고, 1차 지표는 링크 지표(RTT, stale 구간, `rx_starvations`)로 두고 승률은 그 위의 데모로 둔다. (b) 승률 자체를 링크에 민감하게 만들려면 물리를 바꿔야 한다 — 반응 시간이 결판을 정하는 규칙(예: 심판 신호 후 X ms 안의 대응, 또는 밀기 힘이 상대 접촉점 추적 정확도에 비례) — 스모 물리 위에서 파라미터를 조정해서는 나오지 않았다.

**파일.** 수정: `examples/robot_fight/arena.py`(타이브레이크, `--robot-unix`, `--timeout-margin-m`, `--wheel-max-rad-s`, STATE 기본 200 Hz), `brain.py`(`reactive` 기본, 100 Hz, ttl 60), `fight.py`(`--policy`, `--state-hz`, `--modem`, `--wheel-max-rad-s`, 핸디캡 `outage_ms`/`outage_period_ms`), `README.md`(netns 실행 레시피), `tests/test_robot_fight_smoke.py`. 신규: `examples/robot_fight/modem.py`, `tests/test_robot_fight_referee.py`, `tests/test_robot_fight_modem.py`. 테스트 12 통과 + 1 skip(netns, Spark root에서 통과). 결과: `results/robot-fight/r2b-sweep{,2,3,4}/`(git 제외). Spark `/workspace/gpuch/int0928`에 동기화.

### R2c — 2026-09-29 (반응 결정형 물리: 원격 밸런스 봇, 링크 민감도 달성)

R2b의 결론 — 스모 밀기 물리에서는 ≤100 ms 지연이 승률을 못 바꾼다 — 에 대한 답. **`--bot balance`**: 두 바퀴 역진자(축 위 베이스 1 kg, 0.95 m 높이에 2 kg 머리, CoM ≈ 0.6 m, 성장률 √(g/l) ≈ 4 rad/s, 바퀴 토크 모터 ±1.5 N m)이고 **로봇에 로컬 밸런스 제어기가 없다.** 두뇌가 링크 너머에서 100 Hz로 밸런스 루프를 닫는다(STATE v2에 `pitch`, `pitch_rate`, `wheel_left/right` 추가, 108 B; CMD는 바퀴 토크). 토크 하나하나가 링크를 건너므로 지연은 위상 여유를 깎고, stale 구간(토크 0 — `zero`/`coast` 모두 0 토크, 굶은 링크는 아무것도 주지 않는다)은 진자를 자유 낙하시킨다. 규칙은 그대로(넘어짐·링 아웃 = 패, 시간 초과 → 가장자리 타이브레이크). 아레나는 두 두뇌가 모두 명령을 보낼 때까지 봇을 세워 잡고(`hold_release_sim_s`), 넘어짐 판정은 몸 축이 50° 넘게 기울면.

**두뇌 `--policy balance`** = 선형 전상태 피드백 `u = k_pitch·pitch + k_pitch_rate·pitch_rate + k_v·(v − v_ref)` + 요 차동, 위에 전략(`strategy`: **`ram`** 기본 — 상대에게 돌진, 가장자리 근처에서 중심으로; `reactive`/`pusher`/`stand`)이 `v_ref`/`w_ref`를 준다. 기준값은 저역통과(τ 0.25 s) + slew 제한(1.5 m/s²)이라 전략의 목표 점프가 치명적 기울기를 요구하지 못하고, 밸런스 토크가 조향보다 우선한다. `fight.py --bot balance`는 두뇌 정책을 자동으로 `balance`로 바꾸고 `--policy`를 전략으로 넘긴다. 이득은 지연을 낀 오프라인 폐루프에서 격자 탐색(k_pitch 8, k_pitch_rate 4.5, k_v 3.0; 머리 높이 0.55/0.75/0.95 m 비교 후 0.95 채택 — 높을수록 느려서 지연 허용이 오히려 커진다).

**루프 허용 한계(오프라인, 네트워크 없음, `tests/test_robot_fight_balance.py`가 양 끝을 고정):**

| 조건 | 편도 지연 허용 | 단일 블랙아웃 허용 | 옆 밀기 허용(100 ms) |
|---|---|---|---|
| 정지 | 60 ms | 400 ms 이상(정지한 진자는 아무것도 안 건드린다) | 60 N |
| 0.3 m/s 주행 | 45 ms | 400 ms | 60 N |
| 0.6 m/s 주행 | 45 ms | 300 ms(400 ms에 넘어짐) | 60 N |

무선 기본 RTT ~30 ms(편도 15 ms)는 3–4배 여유 안.

**dose–response(로컬 프록시, ue1 핸디캡, 24판/점, 6판 병렬, 30 s, RTF 전부 1.000, `ram` 전략):**

| 핸디캡(ue1) | ue0:ue1 | ue0 승률 (95 % CI) | 패인 |
|---|---|---|---|
| 없음 | 11:13 | 0.46 (0.28–0.65) | 양쪽 ring_out(12/10), 넘어짐 0 |
| 편도 +20 ms (RTT 43) | 16:8 | 0.67 (0.47–0.82) | ue1 fall 13 |
| 편도 +40 ms (RTT 83) | 21:3 | **0.88 (0.69–0.96)** | ue1 fall 21, ue0 fall 3 |
| 편도 +60 ms (RTT 123) | 24:0 | **1.00 (0.86–1.0)** | ue1 fall 24, 1.9 s 안에 |
| 편도 +100 ms (RTT 203) | 24:0 | 1.00 (0.86–1.0) | ue1 fall 24, 1.6 s 안에 |
| 정전 60/100/150/200 ms per 500 ms | 9:15 / 6:18 / 10:13 / 6:18 | 0.38 / 0.25 / 0.44 / 0.25 | ue0 ring_out 15/16/12/13 |
| 단일 블랙아웃 200/400/600 ms @2.5 s (12판) | 7:5 / 7:5 / 9:3 | 0.58 / 0.58 / 0.75 | ue1 넘어짐은 600 ms에서 1회뿐 |

- **지연 축은 목표 달성:** 단조이고, +0/+20은 노이즈 안, **+40 ms에서 뒤집히고 +60 ms에서 결정적**(전부 넘어짐). 패인이 `fall`로 바뀌므로 "링크 때문에 졌다"가 로그에 그대로 찍힌다. 이제 경기가 링크를 잰다.
- **블랙아웃 축은 반대로 움직인다**(R2b와 같은 "축 늘어짐" 효과): 20 % duty 정전이나 ≤600 ms 단일 정전은 봇을 넘어뜨리지 못하고(정지에 가까운 진자는 400 ms 무토크에도 선다) 대신 정지시켜서, 돌진해 온 ue0이 튕겨 링 밖으로 나간다. 즉 **스케줄링 실패가 "몇백 ms 정전"의 형태이면 이 물리로도 승률에 안 보이고, "지속적 지연 증가"(슬롯 밀림 누적, 큐잉)의 형태이면 40 ms부터 보인다.** R5는 경합이 브로커에 어느 쪽 형태로 나타나는지(gpu_timings p99가 수십 ms로 올라가 RTT를 밀어올리나, 아니면 starvation 버스트인가)를 먼저 확인해야 하고, 정전형이면 승률 대신 링크 지표가 1차 지표다(R2b 결론 유지).
- 노이즈 플로어 0.46, 무승부 0, 평균 결판 10.5 s.

**기본값 변경 권고 — R5용으로 `--bot balance`를 기본으로 뒤집을 것을 권고하되, 이 커밋에서는 뒤집지 않았다** (`fight.py`/`arena.py` 기본은 여전히 `sumo`, R4b 게이트 작업이 현재 기본을 쓰고 있어서). 게이트가 아레나를 띄울 때 `--bot balance`를 넘기면 된다. **프로토콜 버전은 1 → 2로 올렸다**(STATE 108 B): 아레나·두뇌·모뎀 테스트를 같이 갱신했고, 옛 두뇌와 새 아레나는 버전 검사에서 거부된다 — Spark 복사본을 같이 동기화해야 한다.

**파일.** 수정: `examples/robot_fight/protocol.py`(v2 STATE), `arena.py`(`--bot`, `--torque-max`, `--hold-max-s`, 역진자 모델, `lean()`, 세워 잡기, 봇별 넘어짐 임계), `brain.py`(`balance` 정책, `ram` 전략, 기준값 slew/저역통과, 토크 우선), `fight.py`(`--bot`, `--torque-max`, 전략 자동 매핑, 핸디캡 `outage_once_ms/at_ms`, 요약에 `bot`·`lost_by`), `README.md`, `tests/test_robot_fight_protocol.py`, `tests/test_robot_fight_modem.py`. 신규: `tests/test_robot_fight_balance.py`. 테스트 14 통과 + 1 skip(netns). 결과: `results/robot-fight/r2c-sweep/`(git 제외). Spark에 동기화하지 않음(R4b 진행 중인 트리를 건드리지 않기 위해).

### R4b — 2026-09-29 (아레나 절반: 에뮬레이터 링크 위에서 실제 경기, Spark GB10)

R4a의 게이트 훅에 R2/R2b의 아레나·두뇌·모뎀을 얹어 **제어 루프 전체가 에뮬레이터를 통과하는 경기**를 돌렸다. 새 파일은 `examples/robot_fight/launch/{root_exec.sh,ue_exec.sh,README.md}`(게이트 훅용 글루, 아레나 패키지 로직은 손대지 않음)와 `examples/robot_fight/analyze_run.py`(경기·브리지·브로커·srsUE 로그를 경기 창별로 조인). Spark 쪽: 로봇 venv `/workspace/robot-venv`(mujoco 3.14.0 aarch64 headless 동작, numpy 2.5.3, pyzmq 27.2), 커밋 `bd5d945`의 프라이빗 클론 `/workspace/gpuch/r4b`(빌드 `builds/gpuch-r4b-release`), 래퍼 `/workspace/gpuch/r4b-run.sh <tag> [ENV=..]`, 슬롯 대기 러너 `/workspace/gpuch/r4b-go.sh`(다른 게이트·빌드·hog가 없을 때만 시작). robot_fight 테스트 13/13 Spark root에서 통과(netns 모뎀 포함). 결과 사본 `results/robot-fight/r4b/`(git 제외).

**배치.** 스택 루트 ns: `root_exec.sh`가 (1) 모뎀이 뜰 때까지 **스폰 위치 사전 게시**(`RF_PREPUB`, 브리지 ipc 엔드포인트에 (±1, 0, 0.3) m를 5 Hz로) — 없으면 attach 동안 브리지가 시나리오의 scripted 경로(그림자 지나감)를 추적한다 — (2) 두 모뎀 소켓이 생기면 경기 루프: 경기마다 새 `arena.py`(로봇당 unix 소켓, 위치 PUB 20 Hz, 20 s 제한, wall-clock) + 두뇌 2개(`--bind 10.45.1.1:0`, `--robot 10.45.1.{2,3}:600{0,1}`, reactive 100 Hz, STATE 200 Hz, ttl 60 ms). UE netns: `ue_exec.sh`가 ping 통과 직후 `modem.py`를 tun 주소에 bind. 아레나·모뎀·두뇌는 같은 호스트 시계라 편도 지연 필드가 유효하다. 종료: 게이트 teardown → 고아 0(세 런 모두 확인).

**런 3회(각 300 s, 핀 spark-gb10, AWGN ref 40, 시나리오 `robot-ring-walk.json`, 위치 외부 입력).**

| 런 | 다른 점 | 게이트 | 경기 | 링크 |
|---|---|---|---|---|
| a1 `140713Z` | 오프셋 (0, 0.8, 0) = 기둥 사이 틈에 스폰, strict on | FAIL(strict, starvation 1,115) | 12판, 처음 3판 정상(RTT 21–23 ms, SNR 30–33), **4판째 두 UE 동시 RLF**, 이후 두뇌가 STATE를 못 받아 전부 무승부 | 로봇이 기둥 그림자로 들어가며(gain −14 dB) **UL이 먼저 죽는다**: ul_buff 141 → 24,012 B, UL MCS 8 → 1.2, PUSCH KO 22 %, gNB "100 consecutive undecoded CSIs" → RLF(ue1 14:08:16, ue0 :19) → RRC Release |
| a2 `143415Z` | `TX_POWER_UL=3.0e6` (의도와 반대: 렌더러는 `noise = tx_power/10^(ref/10)`라 UL 잡음이 **+20 dB**) | FAIL | 9판, 1판째 15 s 만에 RLF | attach 전 scripted 위치가 그림자(gain −44 dB)라 ue1 PUSCH KO 82 %로 시작 |
| **a3 `144912Z`** | 오프셋 **(2.0, 0, 0)** = 링을 기둥 동쪽 LOS로, `TX_POWER_UL=3.0e2`(UL 잡음 −20 dB), 사전 게시, strict off | **PASS** — rrc/pdu/ping 2/2, 카운터 0, starvation 599(시점 미상, 아래) | **32판 전부 완주·`ring_out`, 16:16, RTF 1.000–1.0002 전부**, 무승부 0 | RLF 0, out-of-sync 0, SNR 34–39 dB 양쪽, 모뎀 릴레이 22.7k/40.4k 데이터그램, arena_errors 0 |

**a3 경기 창별 지표(30판, 31–32판 제외 — 아래).** 두뇌 RTT p50 **20.0–22.1 ms**(p99 34–115 ms), CMD 편도 p50 **8.0–9.1 ms**(p99 10–13 ms), stale 구간 **0**(ttl 60 ms 안에 항상 다음 명령이 옴), STATE seq gap 0, 브리지 갱신 경기당 41–137회(=10 Hz, `position_source` external 100 %, 아레나 샘플 나이 p50 0.1–0.3 ms), 브로커 kernel p50 22–28 µs / p99 24–67 µs, srsUE dl_snr 평균 33–39 / 최소 25–35 dB, dl_mcs 26–28, dl_bler 0. UL: ul_brate 250–400 kbps(200 Hz STATE + 릴레이), ul_buff ≤ 314 B(적체 없음), gNB PUSCH KO 3–7 %.

**로컬(R2b d0, 워크스테이션 루프백) 대비.** RTT p50 3.0 → **21 ms**(+18 ms = 슬롯 구조·두 홉 tun/GTP), p99 6.7 → 35–115 ms, 편도 0.6 → 8.4 ms, stale 0.2회/판 → 0, 승률 0.50 → 0.50(16:16), 결판 시간 6.6 → 8.2 s 평균. R2b가 예측한 대로 20 ms급 RTT는 승패를 바꾸지 않는다.

**31–32판(14:53:50–14:54:21)의 외부 경합 — 우연한 대조군.** 다른 에이전트의 게이트(`ocudu-robot-fight/20260929T145337Z`)가 14:53:37에 같은 컨테이너에서 `cmake --build -j20`을 시작했다. 그 두 판만 RTT p50 21 → **85–97 ms**, p99 189–563 ms, 편도 8.4 → 22–29 ms, 브로커 kernel p99 71–168 µs, gNB PUSCH KO 5 → 14.7 %, PUSCH SINR p10 −0.4 → −8.3 dB. 경기는 완주했고(승패 ue0 2판) stale 0. **CPU 경합만으로 루프 RTT가 4배가 된다** — R5의 "스케줄링 off" 조건이 어떤 모습인지 미리 보여준 셈. 이 두 판은 대조군이지 a3 정상 지표에서는 제외.

**발견·교훈.**
1. **그림자에서는 UL이 먼저 죽는다.** DL은 40 dB 기준에서 그림자 −14…−20 dB를 BLER 50 %로 버티지만, UL PUSCH SINR은 LOS에서도 gNB 보고값 **2.5–5 dB**(MCS 2–3)라 여유가 없다. 200 Hz STATE 스트림(~300 kbps)은 LOS에서만 산다. R2b의 stale-정책 실험이 보여준 "굶은 쪽이 오히려 이긴다"는 여기서는 나타나지 않았다 — 링크가 죽으면 **양쪽** 두뇌가 동시에 STATE를 잃어 경기가 정지하기 때문(제어 루프의 두 방향이 같은 셀을 지난다).
2. **`OCUDU_NATIVE_SIONNA_TX_POWER_UL`은 gNB가 보고하는 PUSCH SINR을 움직이지 않는다.** a1(잡음 3.0)과 a3(0.03) 모두 PUSCH 중앙값 2.6–2.8 dB, R4a ping-only 런(잡음 3.0)도 4.5 dB. 넓은 할당일수록 낮게 읽힌다(3 PRB 5–8 dB, 24 PRB 2.1 dB). 즉 UL의 바닥은 에뮬레이터 잡음이 아니라 다른 곳(srsUE ZMQ 송신 품질·타이밍/CFO 미보정·에뮬레이터 UL 경로의 ISI 후보)에 있다. a3가 통과한 이유는 잡음 조작이 아니라 **링을 LOS로 옮긴 것**이다. R4a의 열린 항목 (4)는 "UL 기준이 보수적"이 아니라 "UL SINR이 잡음 knob과 무관"으로 바뀐다 — R5 전에 gNB PUSCH 로그로 원인을 잡아야 한다.
3. **트윈 불일치.** MuJoCo 링에는 기둥이 없어 로봇이 Sionna 기둥을 관통한다(메시 안 = 경로 0). 링 4 m 안에 기둥 3개(R1)는 데모용 그림자 대비에는 너무 촘촘하다 — 링 밖(서쪽 림 바로 뒤)에 기둥 하나가 낫다(R1 씬 손질 과제).
4. **strict-realtime은 이 게이트에서 못 켠다.** starvation은 attach 구간에서 정상적으로 수십 회(R4a 51) 나고, a3의 599는 시점을 알 수 없다(heartbeat idle은 t=20 s 이후 고정 54–55). 브로커에 시각 있는 starvation 이벤트가 없다 — R5의 1차 지표로 쓰려면 per-second starvation 카운터를 heartbeat에 넣어야 한다.
5. 브리지 solve는 외부 위치 입력에서 **88 ms 중앙값 / p90 118 / 최대 284 ms**(R3 dry-run 53 ms보다 느림: 두 로봇이 동시에 움직이며 Doppler·경로 수가 변함) — 10 Hz를 아슬아슬하게 맞추고 `messages_dropped`로 20 Hz 피드의 절반을 버린다. 5 Hz 갱신이면 여유가 생긴다.

**R4 exit 판정.** 실시간(RTF 1.0 전 경기), 완주 ≥ 5(32), 외부 위치 추적 10 Hz, 조인된 타임라인 — 충족. strict-realtime on은 위 4번 이유로 불가, 그림자 횡단은 링크가 죽어 불가(1번) → **완료(조건부)**: LOS 배치에서의 폐루프이며, 그림자 대비는 UL 원인 규명 뒤에.

**재현.** `bash /workspace/gpuch/r4b-run.sh <tag> OCUDU_NATIVE_SIONNA_TX_POWER_UL=3.0e2` (래퍼 기본: 오프셋 `RF_OFFSET=2.0,0,0`, `RF_STRICT=0`, `RF_DURATION=300`, `RF_TIME_LIMIT=20`, `RF_BOT`/`RF_POLICY` 패스스루). 정확한 env는 `examples/robot_fight/launch/README.md`. Spark의 robot_fight 패키지는 v1(`bd5d945`)이다 — R2c의 v2(`--bot balance`)를 쓰려면 패키지 전체를 한 번에 동기화하고 모뎀 테스트를 다시 돌린다.

### R5a — 2026-09-29 (스케줄링 배틀의 기반: 브로커 스케줄링 손잡이, 2셀·2브로커 게이트, Spark GB10 경합 측정)

R5의 인프라 절반. 아레나(R2c/R4b)를 붙이는 R5b는 별도. 결과 디렉터리 `/workspace/ocudu-spark/results/{logs,reports}/ocudu-robot-fight/<ts>`, 래퍼 `/workspace/gpuch/r5-run.sh <tag> [ENV=..]`, 배치 `r5-batch.sh`·`r5-batch2.sh`, 표는 `examples/robot_fight/summarize-robot-fight-runs.py <attach-summary.json>...`. 모든 런 GPU에 다른 프로세스 없음(`r5-<tag>.gpu-before`).

**만든 것.**
1. **브로커 손잡이 `runtime.cuda_stream_priority: default|high|low`** (`config.h/.cpp`, `cuda_backend.cu`). `default`는 이전과 같은 `cudaStreamCreateWithFlags`; `high`/`low`는 `cudaDeviceGetStreamPriorityRange`의 양 끝으로 `cudaStreamCreateWithPriority`, 시작 시 `event=cuda_stream_priority node=… requested=… effective=…`. 출력은 세 값 모두 비트 동일(`test_processing` (i) 케이스, GB10에서 확인; `test_config`가 파싱·거부를 확인). CPU 쪽 실시간 클래스는 코드가 아니라 게이트의 `chrt -f`(아래 — 켜지 말 것).
2. **`ocudu-gpu-hog`** (`apps/ocudu_gpu_hog.cu`): 정해진 길이의 spin 커널을 쉬지 않고 던지는 합성 GPU 세입자(`--kernel-us`, `--duty`, SM당 2블록). 3초 스모크: GB10 48 SM, 2,000 µs 커널 527/s, gpu_share 1.00.
3. **2셀·2브로커 게이트** `run-ocudu-robot-fight.sh` + `-inner.sh` + `render-robot-fight-configs.py`(+ `tests/test_robot_fight_renderer.py`, `examples/configs/sionna/scenarios/robot_ring/robot-ring-fight.json`). 셀 a = gnb0(PCI 1, 2000/2001) ↔ 브로커 a ↔ ue0(netns ue1, 10.45.1.2), 셀 b = gnb1(PCI 2, 2010/2011, N2/N3 127.0.0.12) ↔ 브로커 b ↔ ue1. 브로커마다 자기 토폴로지·control/telemetry ipc 소켓·**자기 Sionna 브리지**(한 씬 파일을 셀별 2노드 시나리오로 쪼갬; 위치 피드는 하나를 둘이 SUB). **gnb1은 gnb0의 마스트 위, 두 로봇은 LOS 쪽에 마스트 축 대칭으로 고정**(x=2, y=±1.2) — 두 브로커 사이에 셀 간 링크가 없으니 공존 비용이 0이고, 두 링크가 통계적으로 같아야 스케줄링만이 변수가 된다(첫 두 런은 R4a walk 경로를 그대로 써서 ue1이 기둥 그림자에서 ping 100 % 손실 — 비대칭 확인 후 폐기). R4a의 훅(`MUE_UE_EXEC`/`ROOT_EXEC`)·잡음 바닥·위치 피드·핀 그대로. 판정은 attach·PCI 캠핑·전송 카운터·브로커 종료로, **측정값(브로커별 rx_starvations, node_stall, gpu_timings, 경합 전/후 창, UE ping 50발 burst, Sionna 갱신 수, hog 속도)은 기록만** 한다. 손잡이는 README 표.

**측정 지표에 대한 교훈.** 브로커의 `gpu_timings.kernel_us`는 스트림 이벤트로 재므로 **GPU가 일을 시작한 뒤**만 본다. 다른 컨텍스트의 time slice를 기다리는 시간은 그 앞에 있어서 `cpu_stage_timings.process_us`(에뮬레이터 호출 벽시계)에만 보인다 — 아래 표의 `proc`. 예: hog 아래 kernel p99 30 µs인데 proc p50 2,330 µs.

**결과 — 두 매트릭스(대칭 시나리오, 240 s, 경합은 두 UE attach 직후 시작, 이후 20 s 뒤 ping 50발).** proc = process_us p50/p99(µs, 경합 창), ping = p50/p90 ms, starv = rx_starvations.

| 런 | 셀 a | 셀 b | 경합 | a proc | b proc | a ping | b ping | a/b starv | 뜻 |
|---|---|---|---|---|---|---|---|---|---|
| `140748Z` s-i | plain | plain | 없음(Sionna ×2 자기 컨텍스트) | 176 / 1,507 | 168 / 1,901 | 31.9/39.8 | 32.5/42.3 | 246/254 | 기준선. p99 1.5–1.9 ms = Sionna solve(각 ~45 ms)가 자기 컨텍스트에서 돌 때 모든 컨텍스트가 그 slice를 기다림 |
| `141210Z` s-ii-own | plain | plain | hog 2,000 µs, 자기 컨텍스트 | 2,309 / 4,243 | 2,329 / 4,252 | 74/94 | 76/95 | 12/13 | **항상 바쁜 컨텍스트 하나가 있으면 나머지 모든 컨텍스트가 슬롯마다 quantum(≈2 ms)을 기다린다.** ping 2.4배, Sionna 갱신 2,390 → 1,577 |
| `141635Z` s-ii-mps | plain | plain | hog MPS 클라이언트 | 2,340 / 5,345 | 2,337 / 4,349 | 71/96 | 75/96 | 4/12 | 브로커가 MPS 밖이면 hog가 MPS 안이든 밖이든 같음 |
| `142101Z` s-iii-mps | **protected**(MPS+high+핀+FIFO) | plain | hog MPS | **3,167 / 6,398** | 2,290 / 4,409 | **122/210** | 73/90 | 20/6 | **스트림 우선순위는 선점이 아니다.** 같은 MPS 컨텍스트 안에서 high 스트림은 SM이 빌 때 먼저 잡을 뿐, 2 ms짜리 hog 블록이 도는 동안은 기다린다 → kernel_us p50 2,478 µs. plain보다 나쁨 |
| `142527Z` s-iii-own | protected | plain | hog 자기 컨텍스트 | 2,432 / 3,640 | 2,266 / 3,470 | **111/178** | 69/91 | 4/11 | proc는 같은데 ping은 나쁨 → 원인은 CPU 쪽(다음 줄) |
| `142954Z` s-iv-mps | protected | protected | hog MPS | 3,878 / 7,119 | 3,836 / 6,440 | 105/191 | 227/368 | 248/3,832 | 둘 다 hog와 같은 컨텍스트 → 둘 다 최악 |
| `143554Z` t1 | protected(FIFO 없음) | plain | hog MPS, **SM 25 %** | 1,753 / 4,351 | 2,352 / 4,138 | 100/162 | 74/97 | 1,424/9 | SM 상한은 MPS 안 브로커를 조금 돕지만(1.75 ms), MPS 컨텍스트 자체가 b·Sionna 컨텍스트와 time slice를 나누므로 여전히 ms 단위 |
| `144020Z` t2 | protected | plain | **hog 200 µs**, MPS | **377 / 859** | 540 / **1,866** | 30/38 | 28/39 | 14/22 | **세입자의 커널 길이가 대기를 정한다.** 짧은 커널이면 MPS+high가 p99를 2.2배 줄임 |
| `144449Z` t3 | protected | plain | 없음, **Sionna를 MPS 클라이언트로** | **110 / 211** | 112 / **1,493** | 29/38 | 29/38 | 2/1 | **핵심 결과.** 실제 경합원(Sionna 버스트)이 브로커와 같은 MPS 컨텍스트에 있으면 high 스트림 브로커의 p99가 0.21 ms, 자기 컨텍스트 브로커는 1.49 ms(7배) |
| `144913Z` t4 | plain | plain | 없음, Sionna MPS | 141 / 1,195 | 134 / 1,004 | 31/41 | 31/38 | 40/35 | t3의 대조군: MPS 밖 브로커는 둘 다 1 ms대. s-i보다 나은 건 컨텍스트가 4 → 3개가 되어서 |
| `145337Z` t5 | protected(FIFO 없음) | plain | hog 자기 컨텍스트 | 2,384 / 3,563 | 2,298 / 4,107 | **126/159** | 81/103 | 327/9 | FIFO를 빼도 ping이 나쁨 → 긴 커널 세입자가 밖에 있을 때 **MPS 클라이언트라는 것 자체가 손해**(클라이언트→서버 홉 + 서버 컨텍스트 대기) |
| `145803Z` t6 | plain | plain | hog MPS, SM 25 % | 2,269 / 3,907 | 2,271 / 3,862 | 82/93 | 62/87 | 2/1 | SM 상한은 MPS 밖 브로커에게 아무 의미 없음 |

**무엇이 격리하고 무엇이 아닌가.**
- GPU의 **컨텍스트 간 time slicing은 안쪽에서 못 이긴다.** MPS 클라이언트·스트림 우선순위·SM 상한 어느 것도 다른 컨텍스트의 quantum을 줄이지 못하고, 그 대기는 세입자 커널 길이(2 ms → 2.3 ms/슬롯, 200 µs → 0.4–0.5 ms)로 정해진다.
- 통하는 것은 **경합원을 브로커와 같은 MPS 컨텍스트에 넣고 브로커 스트림을 high로** 두는 것뿐이다: Sionna를 MPS 클라이언트로(t3) p99 1,493 → 211 µs, 짧은 커널 hog(t2) 1,866 → 859 µs. 그래서 게이트 기본값을 `OCUDU_NATIVE_RF_SIONNA_MPS=1`로 바꿨다(plain 브로커도 손해 없음, t4).
- **protected = MPS 클라이언트 + `cuda_stream_priority: high` + 프로필 브로커 코어 핀(15-17, `OCG_BROKER_SPIN=1`)**. `chrt -f`는 **넣지 말 것**: 스핀하는 브로커 스레드 ~8개가 3코어에서 FIFO면 서로를 굶긴다(s-iii-own ping 111 ms). 기본값 `OCUDU_NATIVE_RF_PROTECTED_RT_PRIORITY=0`.
- R5b(아레나)용 권장 셀: **경합 = Sionna 버스트(기본) + `OCUDU_NATIVE_RF_CONTENTION=busy OCUDU_NATIVE_RF_HOG_KERNEL_US=200`**, a protected / b plain. 이 조합에서 두 링크의 ping 중앙값은 같고(28–30 ms) 차이는 슬롯 p99(0.86 vs 1.87 ms)와 starvation에 있다 — 로봇 결과로 번역되려면 R2c의 반응 결정형 물리가 그 꼬리를 봐야 한다. 2 ms 커널 hog는 "GPU를 독점하는 무례한 세입자" 시나리오로 따로 보여줄 것(양쪽 다 죽고 ping 2.4배).

**R5 exit 게이트 문구 수정.** 이 트리에 `control_updates_dropped_realtime`·late 카운터는 없다. 판정 지표는 브로커별 **`rx_starvations`, `node_stall`, `process_us` p99(경합 창)**, UE ping burst, 그리고 로봇 결과.

**열린 것.** (1) `nvidia-smi compute-policy --set-timeslice`(SHORT/MEDIUM/LONG)가 Spark 드라이버에 있다 — 컨텍스트 간 quantum 자체를 줄이는 유일한 손잡이인데 GPU 전역 설정이라 공유 장치 규칙상 적용하지 않았다(읽기 옵션 없음). 다음 실험 후보 1순위. (2) CUDA gNB를 경합원으로 쓰는 `cudagnb` 셀은 코드만 있고 미실행(두 gNB 모두 가속되어 대칭이므로 "경합 강도" 축이지 "격리" 축이 아님). (3) 워크스테이션 컨테이너에 cmake가 없어 C++ 빌드·ctest는 Spark(sm_121)에서만 돌렸다(config·processing PASS). (4) 게이트 스크립트는 `env -u`를 옵션→대입 순서로 써야 한다(첫 런 실패 원인). (5) hog가 있으면 브리지 solve도 절반으로 준다(갱신 2,390 → 1,577/240 s): 위치→채널 지연도 스케줄링의 피해자라 R5b 타임라인에 함께 기록할 것.

### R4c — 2026-09-30 (R4b 후속: UL SINR 바닥의 원인, 브로커 heartbeat의 초당 starvation 카운터)

**과제 1 — "UL PUSCH SINR이 에뮬레이터 UL 잡음과 무관"의 원인.** 결론: **그 바닥은 에뮬레이터가 만드는 것이 아니다.** 증거 넷.

1. *잡음은 올바른 자리에, 올바르게 적용된다.* a3 렌더 토폴로지(`configs/ocudu-multi-ue-native/20260929T144912Z/topology.yaml`)는 `gnb0_p0`에 `rx_model: rx_noise_gnb0`(`noise_power 3.0e-02`), UE에 `1.12e-06`을 준다. 두 백엔드 모두 `rx_model`의 절대 `noise_power`를 노드 합산 수신 신호에 한 번 더한다(`cpu_backend.cpp` Awgn 분기, `cuda_backend.cu` 1614–1631, `profile_swap`이 갈아끼우는 것은 링크 체인이지 노드의 `rx_model`이 아님). UE 쪽 DL SNR이 knob을 정확히 따라가는 것(R4a 26.6/34.6/40 → 27/31/34 dB)이 그 증거다.
2. *UL에서는 잡음이 신호보다 40–60 dB 아래다.* 와이어 캡처(R4a `130021Z/wire-capture`)의 UE TX 활성 평균 전력 3.0e4(+44 dB)에 대해 a1 잡음 3.0(+5 dB) → SNR 40 dB, a3 잡음 0.03 → 60 dB. 이 잡음이 5 dB 바닥을 만들 수는 없다. UL 파형 자체도 깨끗하다: CP 상관 ρ = 1.000(DL도 1.000, UE RX는 0.999 = 30 dB — 그것이 knob의 DL 효과), 슬롯 안 심볼 시작이 명목값보다 ~300샘플(13 µs) 앞 = N_TA,offset 그대로, srsUE는 서브프레임마다 `0.99/max_peak`로 피크 정규화하므로 클리핑도 없다(`ue_ul_nr.c:149,251`).
3. *에뮬레이터가 없어도 같다.* gNB `PUSCH: … sinr=` 분포를 할당 폭별로 뽑으면(`pusch_stats.py`, 중앙값):

   | 경로 | 1 PRB | 5 PRB | 17–19 PRB | 24 PRB |
   |---|---|---|---|---|
   | **직결 ZMQ, 에뮬레이터 없음**(Spark `direct-zmq-20260925T{033038,033652,062750}Z`) | 8.5 | 6.7 | 4.9 | — |
   | multi-UE legacy(잡음 없음, `111803Z`/`112225Z`) | 7.4–7.5 | 6.7 | 5.0–5.3 | — |
   | multi-UE Sionna + 잡음 ref 40(`132714Z`, a3 `144912Z`) | 8.0–8.4 | 6.0–6.5 | 5.0–5.5 | — |
   | OAI nrUE 1x1(`oai-1x1/20260928T121922Z`) | 4.7 | 4.9 | — | 5.0 |
   | multi-gNB 2셀(`104644Z`, 셀 간 간섭 있음) | — | 1.8–2.1 | — | −0.4 |

   직결 케이블(브로커 프로세스 자체가 없음)에서 이미 1 PRB 8.5 / 17 PRB 4.9 dB다. 에뮬레이터를 끼우고 잡음을 20 dB 바꿔도 ±1 dB 안이며, UE 스택을 OAI로 바꿔도 같다.
4. *보고값이 낮게 편향돼 있지만 여유가 30 dB인 것도 아니다.* a3에서 16QAM TBS 528은 보고 4.9 dB에서 `crc=OK`지만 LDPC 3–6회 반복이 필요하고, 넓은 QPSK 할당(73–105 PRB)은 중앙값 −6 dB로 `KO`(2,198건). 즉 "잡음 없는 링크의 30 dB"는 어디에도 없다 — 무엇인가가 gNB의 PUSCH 수신 체인 안(또는 srsUE UL 파형과 gNB 기대의 어긋남)에서 신호를 깎는다.

**귀속.** 바닥은 **OCUDU PUSCH 체인 쪽**(기본 보고 방식 `post_equalization`, `du_low_config.h:41`)이거나 srsUE UL 파형과 gNB 사이의 불일치이지 우리 에뮬레이터가 아니다. 직결 런에서 재현되므로 우리 트리에서 고칠 수 있는 것이 없다. R4a의 열린 항목 (4)와 R4b 발견 2는 이렇게 닫힌다: **`OCUDU_NATIVE_SIONNA_TX_POWER_UL`/UL 잡음 knob은 현재 값 범위(ref ≥ 26 dB)에서 UL에 사실상 무력하다**(UL SNR 40–60 dB, 바닥은 5 dB 다른 곳). 기둥 그림자에서 UL이 먼저 죽는 것(R4b 발견 1)도 같은 이유 — 출발점 여유가 ~5 dB라 −14 dB면 끝난다. UL을 그림자에 민감하게 만들려면 ref를 ~5 dB까지 내려야 하는데, 그러면 원인 미상의 바닥 위에 우리 잡음을 얹는 꼴이라 원인이 잡히기 전엔 하지 않는다.

**다음 진단 단계(OCUDU/srsUE 쪽, 로컬 실험).** `docs/plans/r4c-ul-noise.patch`(**미적용**, 진단용): gNB fixture에 `expert_phy.pusch_sinr_calc_method`를 `evm` / `channel_estimator`로 바꿔 한 값씩 직결 런. `evm`이 20–30 dB로 읽히면 바닥은 추정기 편향이고 피해는 링크 적응(UL MCS 2–3)뿐 → gNB 설정으로 완화 가능; 셋이 같으면 손상이 실제이고 다음 프로브는 srsUE UL 파형(DMRS·위상 보상)과 gNB 기대의 대조다. fixture 해시가 바뀌므로 명시적 실험 knob으로만. 워크스테이션 컨테이너에 python3/cmake가 없어 이 세션에서는 돌리지 않았다(게이트는 `/usr/bin/python3` 필요). 프로젝트 규칙대로 로컬 패치·지속 점검으로 다루고 업스트림 보고는 마지막에.

**과제 2 — heartbeat 초당 health 카운터(`src/broker.cpp`, +32).** `WorkerDiag`에 `starvations`/`sequence_gaps`(producer 소유)와 `overflows`(puller 소유)를 relaxed atomic으로 추가하고 `AtomicStats` 증가 지점 세 곳에서 함께 올린다. `event=heartbeat` 줄 끝에 기존 필드는 그대로 두고 ` starvations=<직전 heartbeat 이후 델타> starvations_total=<n> gaps=… gaps_total=… overflows=… overflows_total=…`를 붙인다(heartbeat 스레드만 만지는 이전값 벡터로 델타 계산). 샘플(호스트 CPU 빌드, `topology.local.cpu.yaml` + zmq source/sink 4 s):

```
event=heartbeat t=3 dev=ue0 ring=599040/614400 rx_ring=0/46080 puller[…] producer[…] rep[…] starvations=0 starvations_total=0 gaps=0 gaps_total=0 overflows=0 overflows_total=0
event=stop tx_pulls=7767 rx_requests=7715 rx_starvations=0 tx_queue_overflows=0 tx_sequence_gaps=0 zmq_errors=0
```

빌드·테스트: 호스트 스크래치 빌드(`~/ocudu-work/builds-scratch/r4c-host`, CPU-only — 호스트에 nvcc가 없고 컨테이너에 cmake가 없어 sm_120 CUDA 빌드는 이 세션에서 불가; 변경은 backend 무관한 `broker.cpp`뿐) ctest `broker`/`ring`/`config`/`processing` 4/4 PASS. 분석기 반영: `examples/robot_fight/analyze_run.py`의 `hb_delta`가 경기 창 안 `starvations/gaps/overflows`(및 발생 초)를 합산하고, 구형 로그면 `null`; `scripts/native/analyze-sionna-multi-ue-run.py`에 `load_broker_health`(`--steady-after-s`, 기본 60: attach 구간과 정상 상태 분리)와 `== broker <dev>:` 줄 추가. a3 로그(구형 브로커)에서는 둘 다 "카운터 없음"으로 정직하게 떨어지는 것 확인. **R5부터 starvation의 1차 지표는 이 heartbeat 델타다** — `event=stop` 총합은 attach의 수십 회와 섞인다.

파일: `src/broker.cpp`, `examples/robot_fight/analyze_run.py`, `scripts/native/analyze-sionna-multi-ue-run.py`, `docs/plans/r4c-ul-noise.patch`(신규, 미적용). 재검사 스크립트 `pusch_stats.py`, `cp_timing.py`, `ul_edges.py`는 스크래치(레포 밖).

### R5b — 2026-09-29/30 (스케줄링 배틀: 밸런스 봇, 브로커 a protected / b plain, Spark GB10)

R5a의 게이트에 R2c의 원격 밸런스 봇을 얹어 **같은 GPU에서 스케줄링만 다른 두 브로커** 아래 경기를 돌렸다. 트리 `/workspace/gpuch/int0928`을 `b3cd457`로 올리고(번들 fetch, git 레포 유지) robot_fight 패키지 v2를 양쪽 sha256 대조, Spark root에서 robot_fight 테스트 15/15(netns 모뎀 포함). 래퍼 `/workspace/gpuch/r5b-run.sh <tag> [ENV=..]`(r5-run.sh + R4b 훅, `RF_BOT=balance RF_POLICY=balance` 기본), 대기 러너 `r5b-go.sh`, 배치 `r5b-sweep.sh`·`r5b-battle.sh`. 새 분석기 `examples/robot_fight/analyze_battle.py`(경기 창 × 브로커 a/b `process_us`·kernel·stall·starvation(heartbeat 델타가 있으면) × 두뇌 RTT × 브리지 갱신, Wilson CI). 결과 사본 `results/robot-fight/r5b/`(git 제외). 모든 런 GPU에 다른 프로세스 없음, 게이트 전부 PASS, 종료 후 고아 0.

**세션 재시작.** 09-30 05:20 UTC에 Claude 세션이 재시작돼 sweep 대기가 끊겼다. 네 셀은 Spark에서 끝까지 돌았고(`r5b-q1..q4.out`, `SWEEP_DONE`) 출력에서 복구했다 — 재실행 없음.

**1. 기준선(`151114Z`, a·b plain, 경합 없음, 300 s, 밸런스 봇, 20 s 제한):** 22판, 12:9:1, ue0 승률 0.57(0.37–0.76), RTF 1.0000–1.0002, RTT p50 19.5 / p99 30 ms 양쪽, 편도 8.8 ms, stale 0, 브로커 proc 111/238(a) · 111/262(b) µs, 브리지 10 Hz 양쪽. **밸런스 봇은 20 ms RTT 링크 위에서 선다**: 넘어짐은 22판 중 3회(ue1 fall 3 — 밀기 충돌 중 넘어짐, R2c 로컬 0/24보다 잦음), 나머지는 ring_out·가장자리 타이브레이크. 진자 손잡이는 바꾸지 않았다.

**2. 비대칭 경합점 탐색 — hog 큐 깊이 sweep(`r5b-sweep.sh`, A protected / B plain, hog MPS 클라이언트, scripted UE, 180 s).** `ocudu-gpu-hog`에 `--streams S --queue-depth K`(스트림당 K개 커널을 이벤트로 유지, `--queue-depth 1`은 이전과 같음)를 넣고 게이트에 `OCUDU_NATIVE_RF_HOG_STREAMS/_QUEUE_DEPTH`로 배선했다. proc = `process_us` p50/p99(경합 창, µs), ping = p50/p90 ms.

| 런 | hog | a(protected) proc | b(plain) proc | a ping | b ping | starv a/b | hog 자체 큐 지연 p50 |
|---|---|---|---|---|---|---|---|
| R5a t3 `144449Z` | 없음(Sionna MPS) | 110 / 211 | 112 / 1,493 | 29 / 38 | 29 / 38 | 2 / 1 | — |
| q1 `151722Z` | 200 µs ×1×8 | 209 / 1,227 | **2,191 / 2,306** | 29 / 37 | **70 / 86** | 3 / 1 | 1.7 ms |
| q2 `152107Z` | 200 µs ×2×16 | **399 / 838** | 2,206 / 2,314 | 28 / 37 | 65 / 85 | 3 / 4 | 3.8 ms |
| q3 `152452Z` | 200 µs ×4×32 | 415 / 1,025 | 2,231 / 2,328 | 30 / 39 | 67 / 88 | 6 / 2 | 12.3 ms |
| q4 `152837Z` | 100 µs ×4×64 | 261 / 660 | 2,228 / 2,390 | 31 / 38 | 68 / 87 | 6 / 28 | 13.1 ms |

- **비대칭은 큐 깊이가 아니라 컨텍스트 소속에서 온다.** plain 브로커(자기 컨텍스트)는 hog가 MPS 컨텍스트를 항상 바쁘게 만드는 순간 슬롯마다 time-slice quantum(≈2.2 ms)을 기다리고, 깊이 8이든 256이든 같다(2.19–2.23 ms p50). protected 브로커는 hog와 같은 MPS 컨텍스트에 있어 quantum을 기다리지 않고, high 스트림이라 hog의 큐(3.8–13 ms)도 건너뛴다 → p50 0.2–0.4 ms, p99 0.66–1.2 ms. 큐 깊이는 protected 쪽 꼬리만 조금 움직인다(q2 최소).
- 링크로 번역하면 **a RTT 28–31 ms vs b 65–70 ms**(+~19 ms 편도), starvation·stall은 양쪽 0에 가깝다 — 즉 경합은 "정전"이 아니라 **지속적 지연 증가**로 나타난다. R2c가 예측한 바로 그 형태라 밸런스 봇이 볼 수 있다(+20 ms 편도 → 0.67).
- 채택: **q2**(200 µs × 2 × 16, MPS 클라이언트). (a) 많은 스트림·높은 duty, (b) Sionna+hog(기본이 이미 Sionna MPS), (c) CPU 압박은 필요 없었다 — GPU 스케줄링만으로 비대칭이 났다. `nvidia-smi compute-policy --set-timeslice`는 건드리지 않았다(GPU 전역).

**3. 배틀(`r5b-battle.sh`, q2 경합, 밸런스 봇, 300 s/런, 20 s 제한, 경합은 attach 직후 시작이라 모든 경기가 경합 중).** ue0 = 셀 a, ue1 = 셀 b.

| 셀 | 런 | 경기 | ue0:ue1(무) | ue0 승률 (95 % CI) | 넘어짐 ue0/ue1 | a proc p50/p99 (µs) | b proc p50/p99 | a RTT p50/p99 (ms) | b RTT p50/p99 | starv a/b | RTF |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 기준 plain/plain, 경합 없음 | `151114Z` | 22 | 12:9 (1) | 0.57 (0.37–0.76) | 0 / 3 | 111/238 | 111/262 | 19.5/30 | 19.5/31 | –/– | 1.0000–1.0002 |
| **a protected / b plain** | `052100Z` | 32 | 21:9 (2) | 0.70 (0.52–0.83) | 2 / 20 | 413/784 | 2,207/2,292 | 20.0/32 | 36.5/66 | 105/71 | 1.0000–1.0003 |
| **a protected / b plain** | `053241Z` | 45 | 42:3 (0) | 0.93 (0.82–0.98) | 2 / 39 | 387/716 | 2,213/2,301 | 20.0/31 | 36.9/72 | 109/68 | 1.0000–1.0003 |
| **합산 a protected / b plain** | | **77** | **63:12 (2)** | **0.84 (0.74–0.91)** | **4 / 59** | | | | | | |
| 대조 plain/plain, 같은 경합 | `052651Z` | 31 | 10:20 (1) | 0.33 (0.19–0.51) | 15 / 6 | 2,318/2,402 | 2,320/2,385 | 37.5/69 | 37.5/68 | 45/30 | 1.0000–1.0003 |
| 대조 protected/protected, 같은 경합 | `053831Z` | 25 | 11:12 (2) | 0.48 (0.29–0.67) | 3 / 3 | 226/608 | 218/604 | 19.9/32 | 19.9/32 | 70/55 | 1.0000–1.0002 |

- **승률이 뒤집힌다.** 경합 없이 0.57, 둘 다 보호 0.48, 둘 다 무보호 0.33(잡음 안, 아래), **a만 보호 0.84(0.74–0.91)** — 77판 중 ue1이 59판을 **넘어져서** 졌다(기준선 3/22, 둘 다 보호 3/25). 두 번째 런은 42:3. 패인이 `fall`이라 "링크 때문에 졌다"가 로그에 있다.
- **메커니즘의 증거는 브로커 로그에 있다.** 같은 GPU, 같은 hog, 같은 씬·잡음·두뇌인데 셀 b의 에뮬레이터 호출이 슬롯마다 2.2 ms(quantum)를 기다려 UE ping·두뇌 RTT가 20 → 37 ms(p99 66–72)가 되고, 셀 a는 MPS 컨텍스트 안 high 스트림이라 0.4 ms에 머물러 RTT 20 ms 그대로다. 밸런스 루프의 위상 여유(R2c: 편도 45–60 ms 한계, +20 ms에서 넘어짐 시작)가 b에서만 깎였다. starvation(`event=stop` 총합 68–109, attach 포함; R4c의 heartbeat 델타는 이 빌드에 없음)과 node_stall 0은 양쪽 비슷하다 — 즉 승패를 가른 것은 **꼬리가 아니라 중앙값의 +17 ms**다.
- 둘 다 무보호(0.33)는 양쪽이 같은 37.5 ms RTT에서 서로 밀다 넘어지는 잡음이 큰 경기(넘어짐 21/31)다. 0.5와의 차이(CI 0.19–0.51)는 경계선이고 방향이 반대라 스케줄링 효과로 읽지 않는다. 둘 다 보호는 기준선과 구분되지 않는다(넘어짐 6/25, RTT 19.9).
- 부수 관찰: 브리지 갱신은 hog 아래에서도 2,990/300 s(10 Hz) 유지 — Sionna가 MPS 클라이언트라 hog와 한 컨텍스트에서 돈다. RTF는 전 경기 1.0000–1.0003(아레나가 아무도 기다리지 않음 확인).

**정직한 한계.** (1) 이 경합원은 합성 hog다. 실제 세입자(CUDA gNB, Sionna)가 같은 비대칭을 내려면 그것도 브로커의 MPS 컨텍스트 밖에 있어야 한다 — R5a s-ii처럼 "무례한 자기 컨텍스트 세입자"가 있으면 protected도 quantum을 기다린다(안에서 못 이김). (2) 시간 기준: 브로커 로그의 `t=`를 파일 생성 시각으로 절대시간에 맞췄다(±1 s). (3) 승률 뒤집힘의 크기는 봇의 지연 허용 한계(R2c 진자 0.95 m)에 달려 있다 — 더 튼튼한 봇이면 같은 +17 ms가 안 보인다. 그래서 1차 지표는 여전히 `process_us`·RTT이고, 경기는 그것을 사람이 보게 하는 층이다.

**재현.** `bash /workspace/gpuch/r5b-go.sh <tag> OCUDU_NATIVE_RF_BROKER_A_SCHED=protected OCUDU_NATIVE_RF_BROKER_B_SCHED=plain OCUDU_NATIVE_RF_CONTENTION=busy OCUDU_NATIVE_RF_HOG_MPS=1 OCUDU_NATIVE_RF_HOG_KERNEL_US=200 OCUDU_NATIVE_RF_HOG_STREAMS=2 OCUDU_NATIVE_RF_HOG_QUEUE_DEPTH=16 OCUDU_NATIVE_RF_DURATION_SECONDS=300 OCUDU_NATIVE_RF_SKIP_CTEST=1` → 분석 `python3 examples/robot_fight/analyze_battle.py --log-dir <log_dir> --markdown r.md`. 파일: `apps/ocudu_gpu_hog.cu`(streams/queue-depth), `examples/robot_fight/run-ocudu-robot-fight{,-inner}.sh`(두 knob), `scripts/native/README.md`(표), 신규 `examples/robot_fight/analyze_battle.py`.

### R6a — 2026-09-30 (오프라인 리플레이 영상)

**무엇을 만들었나.** `examples/robot_fight/render_replay.py <log_dir> --out <mp4> [--fight N …] [--max-fights K] [--speed 1.0] [--fps 25] [--summary-png <png>]`. 게이트 런의 로그 디렉터리(multi-UE 게이트: `broker.log`/`sionna-status.jsonl` 하나, robot-fight 게이트: `broker-{a,b}.log`/`sionna-status-{a,b}.jsonl`, 셀당 로봇 하나)를 읽어 **영상 시계 = 아레나 wall-clock**(1.0×)으로 렌더한다 — 12 s 경기는 12 s 영상이고, 에뮬레이터가 실시간이 아니었다면 로봇과 패널이 눈에 띄게 어긋난다. 왼쪽은 링 위에서 본 아레나(펜스, R1 매니페스트의 기둥과 gNB 방향을 아레나 프레임으로 옮겨 그림, 두 로봇의 방향·2 s 궤적, 밸런스 봇은 pitch 막대와 각도, 경기 종료 시 패자 깜빡임과 승자 배너), 오른쪽은 같은 시간축의 링크 패널 네 줄: ① 두뇌→로봇 편도 지연(명령마다 점) + 그 경기의 두뇌 RTT p50/p99 + stale 구간 음영, ② srsUE DL SNR + out-of-sync/release 마커, ③ 각 로봇 브로커의 슬롯당 에뮬레이터 호출 시간(`cpu_stage_timings process_us`, 1 s 표본, 1 ms 슬롯선) + heartbeat에 초당 starvation이 있으면(R4c 이후) 막대, ④ Sionna solve ms와 브리지가 쓴 아레나 위치의 나이. 헤더에 경기 번호, wall-clock, 아레나 RTF, 로봇별 브로커의 스케줄링 프로파일(`run-parameters.json`의 `broker_sched`; 보호된 쪽이 항상 A=파랑), 경합 종류. `--summary-png`는 런 전체 한 장(승자 색 경기 타임라인 + 위 네 지표의 런 전체 띠, 경합 시작선).

- 브로커 로그에는 절대 시각이 없어 `t`(브로커 기동 후 초)를 `<log>.birth_unix_ms` 사이드카(Spark에서 `stat -c %W`)로 정렬한다. 복사본 만들 때 같이 만든다: `results/robot-fight/replay-data/<run>/`에 R4b·R5b 런의 필요한 파일만(1.3 GB srsUE 내부 로그는 링크 이벤트만 걸러낸 `*.events.log`) tar over ssh로 가져왔다.
- 아레나→씬 오프셋은 `--frame-offset` 또는 파라미터가 없으면 **첫 경기 스폰 위치와 브리지의 첫 external 위치를 비교해 추론**한다(두 런 모두 2.0,0,0으로 추론 — R4b·R5b 래퍼 기본값과 일치).
- 렌더 속도 워크스테이션 ≈ 12 fps(1280×720 Agg) → 300 s 런이 ≈ 11 분. 코덱 libx264(imageio-ffmpeg).

**렌더 결과(`results/robot-fight/videos/`, git-ignored).**
- `r4b-20260929T144912Z-fights1-8.mp4` — R4b(스모봇, 브로커 1개, 경합 없음) 1–8판, 1,962 프레임 78.5 s. 보이는 것: 두 로봇이 링을 가로질러 밀치고, 편도 지연 8–9 ms 띠가 양쪽 같고, RTT p50 21–22 ms, SNR 33–42 dB, 에뮬레이터 호출 ~100 µs, Sionna solve 40–150 ms·위치 나이 수십 ms. `r4b-…-summary.png` 런 전체.
- `r5b-20260930T052100Z-all.mp4` — R5b 첫 배틀 런(밸런스 봇, A=ue0 브로커 a **protected**, B=ue1 브로커 b **plain**, 경합 busy hog 200 µs·MPS·Sionna in MPS) 32판 전부, 7,987 프레임 **5:19**. 보이는 것: 패널 ③에서 B의 브로커 호출이 처음부터 끝까지 **~2.2 ms(슬롯선 위)**, A는 0.1–1 ms 아래에 머문다; 경기 결과 21:9(+무승부 2), 낙상 22회 중 **B(ue1)가 20회** — 배너가 "A (ue0) wins — fall"로 반복되고 B의 pitch 막대가 커지다 넘어진다. 편도 지연 중앙값 자체는 양쪽 비슷하고(패널 ①), 차이는 B의 RTT p99(60–120 ms)와 브로커 꼬리다. `r5b-…-summary.png` 런 전체(승자 색 띠가 파랑 위주). 수치 해석·CI는 R5b 절이 정본이다.

**한계.** MuJoCo 3D 오프스크린은 이 워크스테이션에 EGL/OSMesa가 없어 불가(`MUJOCO_GL=egl` → OpenGL 플랫폼 라이브러리 없음, `osmesa` → 미설치) — 2D 톱다운만. 경기 사이(아레나 재시작)에는 위치 피드가 없어 브리지 위치 나이가 수 초로 튀므로 패널 ④는 250 ms에서 잘라 그린다. 편도 지연은 아레나·두뇌가 같은 호스트 시계일 때만 의미 있다(게이트 구성은 그렇다). 두뇌 RTT는 명령별 로그가 없어 경기당 p50/p99 선으로만 표시. R5b 첫 런의 브로커는 R4c 이전 빌드라 초당 starvation 막대가 없다(제목에 표시). LLM 두뇌·G1·Isaac 영상은 미착수.

**파일.** 신규 `examples/robot_fight/render_replay.py`, `tests/test_robot_fight_replay.py`(합성 런 생성 `make_synthetic_run`, 1셀·2셀 레이아웃 프레임/요약 검사, 3 테스트 OK), 로봇 venv에 matplotlib·imageio·imageio-ffmpeg 추가. 데이터 사본 `results/robot-fight/replay-data/{20260929T144912Z,20260930T052100Z}/`, 영상 `results/robot-fight/videos/`.

### R6b — 2026-09-30 (라이브 웹 UI의 gNB KPI 패널, robot-fight 게이트)

- **왜 비어 있었나.** 웹 UI의 KPI 블록(throughput/MCS/BLER/SINR, UE별)은 gNB 자체의 remote-control WebSocket을 구독한다. 그 소켓은 gNB가 스택 netns의 loopback(127.0.0.1:8001)에 바인드하므로 밖에서 못 보고, `gnb-metrics-relay.py`가 run dir의 AF_UNIX 소켓으로 재수출해야 하며, 서버에 `--gnb-metrics-endpoint ws+unix://…`를 줘야 한다. 이 세 가지(gNB yaml의 `metrics.enable_json` + `remote_control`, 릴레이, UI 인자)를 하는 게이트는 legacy 1x1뿐이었고, robot-fight 게이트(와 그 기반인 multi-UE)는 하나도 없었다 — 그래서 텔레메트리·Sionna 패널만 뜨고 KPI는 `disabled`.
- **추가한 것.** `render-robot-fight-configs.py --gnb-metrics-ports A,B`: 두 gNB yaml에 rank1 렌더러의 `render_gnb_metrics(port)` 블록을 각각 다른 포트(loopback을 공유하므로)로 덧붙임, 셀 메타데이터에 `metrics_port`. 게이트 `OCUDU_NATIVE_GNB_METRICS`(**이 게이트만 기본 1** — 데모 게이트라서; 다른 게이트는 그대로 off), `OCUDU_NATIVE_GNB_METRICS_PORT_{A,B}`(8001/8002): inner가 두 gNB가 뜬 뒤 릴레이를 gNB마다 하나씩 `start_group`(`gnb-metrics-a.sock`/`-b.sock`, `event=gnb_metrics_relay`), cleanup 순서에 `gnb-metrics-relay`를 gNB 앞에 삽입, `run-parameters.json`에 `gnb_metrics: {a: {gnb, socket, port, endpoint}, b: …}` 기록, `_WEB_UI=1`이면 게이트의 자체 UI에도 셀 a 소켓 전달. 0이면 렌더 결과와 프로세스 목록이 이전과 동일.
- **팔로워** `examples/robot_fight/robot-fight-webui-follow.sh`(Spark `/workspace/gpuch/webui-follow.sh`, tmux `webui`, root): 가장 새 런의 `telemetry-{a,b}.sock`에 UI 두 개(8080=셀 a, 8081=셀 b)를 붙이고, `run-parameters.json`의 `gnb_metrics`를 읽어 `--gnb-metrics-source gnb0=…`/`gnb1=…`를 준다(소켓 파일이 아니라 리포트를 읽으므로 gNB가 뜨기 전에 UI가 시작돼도 backoff 재접속으로 붙는다). 소켓이 root 소유라 UI도 root로 돌려야 한다(dev로 돌린 첫 시도는 `telemetry_connected=false`). 워크스테이션에서는 `ssh -L 8080:127.0.0.1:8080 -L 8081:127.0.0.1:8081 spark-minwoo`.
- **검증(Spark, 런 `20260930T055225Z`, R5b 데모 셀 a protected / b plain / busy hog 200 µs×2×16 MPS, 밸런스 봇).** 릴레이 두 개 기동(`event=gnb_metrics_relay gnb=gnb0 port=8001`, `gnb1 port=8002`), `/api/status` → `ran_gnbs`: **8080 `gnb0` connected PCI 1** dl_mcs 28, dl_brate 169–236 kb/s, ul_brate 263–338 kb/s, pusch_snr 4.9–5.1 dB, dl_nof_nok 0; **8081 `gnb1` connected PCI 2** dl_mcs 27–28, dl_brate 125–438 kb/s, ul_brate 248–678 kb/s, pusch_snr 3.7–3.9 dB. 즉 포트마다 자기 셀의 UE가 뜬다(PUSCH SNR 4–5 dB는 R4c의 OCUDU 수신 체인 바닥 그대로).
- **파일.** 수정 `examples/robot_fight/render-robot-fight-configs.py`(+`parse_metrics_ports`, self-test 확장), `run-ocudu-robot-fight.sh`, `run-ocudu-robot-fight-inner.sh`, `scripts/native/README.md`(knob 표 3행); 신규 `examples/robot_fight/robot-fight-webui-follow.sh`. 렌더러 self-test·`test_robot_fight_renderer` OK, `bash -n` clean.

### R6c — 2026-09-30 (3-D MuJoCo 리플레이 뷰)

**무엇을 더했나.** 사용자가 `libegl1`/`libosmesa6`를 설치해 이 워크스테이션에서 `MUJOCO_GL=osmesa` 오프스크린 렌더가 된다(`egl`은 여전히 EGLError → osmesa 사용). `render_replay.py`에 `--view 2d|3d|both`(기본 `both`)를 추가했다. 3-D 뷰는 아레나가 쓰는 **바로 그 MJCF**(`arena.build_model_xml`, 런의 봇 타입은 스폰 로그의 `bot`)를 다시 만들고, 로그의 pose로 물리 없이 관절값만 세운다: free joint = (x, y, z, quat(yaw, pitch)) — pitch는 `arena.Arena.lean()`과 같은 정의(+ = 진행 방향으로 기울기, up·heading = sin pitch; 단위 테스트로 역변환 확인)이고 스모봇이 넘어진 경우는 `up`으로 기울인다; 바퀴는 진행 속도/반지름으로 회전. 씬 매니페스트의 펜스(48 세그먼트 박스 링), 기둥 3개, gNB 마스트+안테나 박스를 시각 전용 geom(`contype=0`)으로 아레나 프레임에 옮겨 넣고, 로봇 색은 2-D와 같이 A 파랑/B 빨강(보호된 쪽이 A), 하늘 그라디언트·그림자 광원 추가. 카메라는 고정 자유 카메라: 링 옆(마스트 방향에서 90°)에서 −33° 내려다보며 링을 가로질러 보는 위치 — 마스트 쪽(−x 뒤)에서 보면 3 m 기둥이 링을 가린다는 것을 첫 프레임에서 확인하고 옮겼다(기둥은 왼쪽, 마스트 방향은 오른쪽). `both`는 왼쪽 열을 3-D(위)·2-D 톱다운(아래)로 나누고 오른쪽 링크 패널은 그대로; 승자 배너·낙상/링아웃 플래시는 3-D 뷰에도 그린다. `--view 2d`는 MuJoCo를 import하지 않아 OpenGL 없는 호스트에서도 그대로 돈다.

**테스트.** `tests/test_robot_fight_replay.py`에 `test_3d_frames_osmesa` 추가(오프스크린 렌더 불가 시 skip): 합성 2셀 런에서 raw 3-D 프레임 480×640이 비어 있지 않음, `both` 10프레임 1280×720, `3d` 3프레임. 4 테스트 OK(`MUJOCO_GL=osmesa`).

**렌더 속도.** `both` 1280×720: R4b 8판 1,962프레임이 4분 25초 → **≈ 7.4 fps(0.135 s/프레임)**; 첫 프레임(figure 생성 포함)은 0.48 s. 3-D osmesa 640×480 자체는 ≈ 90 ms/프레임이라 병목이고, 2-D만은 ≈ 12 fps. 300 s 런(7,987 프레임)은 ≈ 18–20분.

**결과물(`results/robot-fight/videos/`, git-ignored, 기존 2-D 영상 옆에 `-3d` 접미사).**
- `/home/minwoo/ocudu-work/ocudu-integration/results/robot-fight/videos/r5b-20260930T052100Z-all-3d.mp4` — R5b 첫 배틀 런 32판 전부, 5:19, 실시간 속도. 3-D에서 B(빨강) 역진자가 기울다 넘어지는 장면과 패널 ③의 B 브로커 호출 ~2.2 ms가 같은 시간축에 보인다.
- `/home/minwoo/ocudu-work/ocudu-integration/results/robot-fight/videos/r4b-20260929T144912Z-fights1-8-3d.mp4` — R4b 스모봇 1–8판, 78.5 s.

**파일.** `examples/robot_fight/render_replay.py`(Scene3D, `--view`), `tests/test_robot_fight_replay.py`. `fcbcb6e`에 포함해 게시함.


### R7 — 2026-09-30 완료: 지연 보상 제어기와 검증

동일한 밸런스 봇·균형 게인에 `balance`와 `balance_comp`를 적용하고,
정책을 좌우로 교환해 공통 시드끼리 비교했다. Spark GB10, 23.04 MSamples/s,
CPU gNB 2개·서로 다른 셀의 srsUE 2개·CUDA 브로커 2개·Sionna+AWGN 구성이다.

| 비교 | 공통 시드 경기 수 | 보상 승 | 일반 승 | 무승부 | 보상/일반 넘어짐 |
|---|---:|---:|---:|---:|---:|
| 기본, 180초씩 | 20 | 15 | 3 | 2 | 0 / 1 |
| GPU 경합, 180초씩 | 24 | 23 | 1 | 0 | 0 / 11 |
| GPU 경합 재실험, 새 시드·속도 변동 0·300초씩 | 44 | 33 | 10 | 1 | 8 / 13 |

보상 제어기는 이번 비교에서 더 자주 이겼지만, 짧은 첫 실험의 넘어짐 0회가
재실험에서도 유지되지는 않았다. 같은 정책끼리의 대조군과 로컬 단절·손실
96경기도 완료했다. 잘못 전달됐던 AWGN/RAN 조건은 실제 생성 설정까지 감사해
재검증했다: 요청 9개 중 7개 통과, K2=8 및 K1=7/K2=8은 gNB 지원 범위 밖으로
실행 전 거부됐다. 지원하지 않는 값을 UE/gNB 소스 수정으로 강제하지 않았다.

구현과 이번 검증 캠페인을 완료한 상태다. strict realtime은 꺼져 있었고,
연속 PHY 동기 보장이나 단절 면역성을 입증한 결과는 아니다. 8개 경기 런 모두
UE별 RRC/PDU 수립 1회와 콘솔상 재접속 표시 없음이 관측됐다.

**[최종 보고서·그래프·실행 방법](../../reports/experiments/robot-fight-r7/README.md)** 에 실제 설정,
승패·넘어짐·RTT, 무효 경기, 프로브 결과, 관측 한계를 모았다.
[경기 JSON](../../reports/experiments/robot-fight-r7/evidence/robot-fight-r7-results.json),
[프로브 JSON](../../reports/experiments/robot-fight-r7/evidence/robot-fight-r7-probes.json),
[단절·손실 JSON](../../reports/experiments/robot-fight-r7/evidence/robot-fight-r7-robustness.json),
[설정·연결 감사](../../reports/experiments/robot-fight-r7/evidence/robot-fight-r7-audit.json).
