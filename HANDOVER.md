# 세션 인수인계 — 2026-08-16

**이 파일은 재개용 요약이다. 정본은 `AGENT_PROGRESS.md`이고, 둘이 어긋나면 `AGENT_PROGRESS.md`가 이긴다**(`AGENT.md`의 우선순위 규칙). 여기에는 다음 세션이 5분 안에 상황을 복구하고 바로 손을 대기 위한 것만 적는다.

## 0. 먼저 읽을 것

`AGENT.md` → `AGENT_GOAL.mimo.md` → `AGENT_HARNESS.md` → `AGENT_PROGRESS.md` 순서(`AGENT.md`가 강제하는 순서). 그다음 이 파일.

**미해결 사용자 결정**: `AGENT_GOAL.md`(원본)와 `AGENT_GOAL.mimo.md`(MIMO 조항) 중 **어느 것이 이 워크스페이스를 지배하는지 아직 미확정**이다. 실질은 `.mimo`가 지배해 왔다(M1~M5의 설계 판단이 그 파일의 Constraints에서 나왔다). 에이전트는 이 파일을 자율로 수정하지 않는다.

## 1. 지금 어디까지 왔나

베이스라인 `bc88865` 이후 약 51 커밋. **M0~M5의 코드 작업이 끝났다.**

| | 상태 |
|---|---|
| M0 단일 엔진 리팩터 | green 5 / **환경 차단 2** (multi-UE·multi-gNB Docker 게이트, unprivileged LXC) |
| M1 차원 + 고정 행렬 | **전 게이트 green** |
| M2 IID 확률적 페이딩 | **전 게이트 green** |
| M3 공간 상관 + coherent LOS | **전 게이트 green** |
| M4 physical link 단위 runtime control | **전 게이트 green** |
| M5 라이브 통합 | **전 게이트 green** — M5.4 해결 + M5.5 행렬 게이트 (2026-08-16) |

**상시 게이트 상태** (전부 2026-08-16에 통과):
- `ctest` 8/8 — CPU 트리(`build/`)와 CUDA 트리(`build-cuda/`) 양쪽
- `scripts/remote/gpu-test-sequence.sh` **9/9**
- `scripts/native/run-ocudu-legacy-1x1.sh` — 라이브 1×1 attach `status=passed`
- `scripts/native/run-ocudu-mimo-2port-no-core.sh` — 라이브 2-port `status=passed`. **행렬 검증 포함**: 캡처한 두 wire에서 `y = Hx`를 독립 재계산, max 오차 DL 4.1e-08 / UL 1.5e-07 (허용 1e-4), 행별 교차항 기여 0.12~0.77

## 2. 지난 세션에 닫은 것: M5.4

**원인**: 브로커가 gNB를 **실시간보다 빠르게 먹였고**(측정: 332 ms 동안 1.76배), 다중 포트 ZMQ 라디오는 그것을 흡수하지 못하고 교착한다. OCUDU의 RX 채널은 가득 찬 원형 버퍼를 **제자리에서 재시도**하는데, 그 스레드가 세션의 모든 TX/RX 채널을 서비스하는 **단 하나의 `radio` 워커**이고 RX 스트림은 포트를 **순차로** pop한다 — 그래서 가득 찬 포트가 굶은 형제 포트에 필요한 스레드를 붙잡고 영원히 배수되지 않는다. 포트가 하나면 같은 루프가 스스로 풀린다(1×1이 멀쩡했던 이유).

**수정 2건** — (1) `RealTimePacer`(`include/ocudu_gpu_channel/pacing.h`): producer throttle이 한 배치를 넘는 지각을 버린다. (2) REP 응답을 producer의 윈도 경계로 자른다(`PortRuntime.rx_slots`) — 형제 응답 크기가 구조적으로 같아진다.

소스 근거·측정치·뮤테이션 프로브는 `AGENT_PROGRESS.md`의 **"M5.4 — 원인 규명 완료, 게이트 통과 (2026-08-16)"** 절에 있다.

## 2b. 같은 날 닫은 것: M5.5 — 게이트가 "옮긴 것"만 채점하고 있었다

M5 exit 게이트 표의 핵심 행(**각 수신 행이 두 송신 포트 모두에 의존**)이 라이브에서 판정되지 않고 있었다. peer의 마커 오라클이 `--self-test` 경로에만 있어서, 라이브 실행은 `marker_checks=0` 옆에 `marker_mismatches=0`을 적고 통과했다. 지금은 브로커가 포트별로 **두 wire를 소켓 경계에서** 유한 구간 캡처하고(`--wire-capture-dir/-samples/-skip`), `scripts/native/verify-mimo-matrix-capture.py`가 토폴로지 YAML에서 읽은 `H`로 `y = Hx`를 독립 재계산해 비교한다. 뮤테이션 프로브 3개(대각 행렬 / lane 1개 제거 / 형제 epoch를 **1샘플** 어긋내기) 전부 FAIL 확인.

## 3. 다음에 손댈 것 (우선순위)

**M6이 열려 있다 — `MIMO_MILESTONES.md`의 M6/M7 절이 정본.**

0. **M6 rank-2 SU-MIMO 라이브 acceptance.** 막힌 곳은 UE이고 이 저장소가 아니다(gNB도 에뮬레이터도 이미 rank 2를 한다). 수정안: srsUE를 패치하지 않고 **OAI nrUE를 추가**한다 — ZMQ 라디오 다채널·와이어 프로토콜 바이트 동일·nrUE의 2×2 MMSE와 layer demapping을 전부 소스로 확인했다. **이 저장소 C++는 0줄 바뀐다.** 단계는 M6.1 빌드 → M6.2 1×1 회귀 → M6.3 2포트 → M6.4 acceptance.
1. **지배 mission 파일 확정**(§0) — 에이전트가 자율로 정하지 않는다.
2. M0 라이브 부채 2건(multi-UE / multi-gNB) — unprivileged LXC 환경 차단이고 베이스라인에서도 동일 재현되므로 M0 결함이 아니다. 호스트 설정이 바뀌지 않으면 풀 수 없다.
3. GH Pages 공개 — 워크플로는 `workflow_dispatch` 게이트 상태이고 레포는 private. 사용자가 공개를 결정하면 켠다.
4. CDL(TR 38.901 §7.7.1) — 빔포밍 use case가 생기면. 오늘의 행렬은 선언하거나 뽑는 것이지 배열 기하에서 합성하지 않는다.

## 4. 명령어

```bash
cd /home/ubuntu/ocudu-gpu-channel-mimo-claude
source ~/ocudu-loopback-workspace/tools/env.sh   # CUDA 12.8 툴체인 PATH

# 빌드 + 단위 테스트 (두 트리)
cmake --build build -j8      && ctest --test-dir build --output-on-failure
cmake --build build-cuda -j8 && ctest --test-dir build-cuda --output-on-failure

# 상시 GPU 시퀀스 9/9 (약 2분, loopback 러너)
bash scripts/remote/gpu-test-sequence.sh

# 라이브 1×1 attach (Docker 불필요, 약 2분)
bash scripts/native/run-ocudu-legacy-1x1.sh

# 라이브 2-port transport 게이트 (약 1분)
bash scripts/native/run-ocudu-mimo-2port-no-core.sh
# 결과: ~/ocudu-native-workspace/results/{reports,logs}/ocudu-mimo-2port-native/<타임스탬프>/
```

## 5. 환경

- 이 컨테이너 = **Threadripper PRO 7965WX (24C/48T) + RTX 5090 ×4**. `.config`의 `REMOTE_HOST=127.0.0.1` — **"remote"가 곧 이 기계다.** 별도의 RTX 워크스테이션을 찾을 필요 없다.
- **Docker는 이 컨테이너에서 불가**(unprivileged LXC). 네이티브 하네스(rootless netns)를 쓴다.
- OCUDU/srsRAN 소스: `~/ocudu-native-workspace/src/{ocudu,srsRAN_4G}` (핀: `a1916edcd`, `eea87b1`). gNB 바이너리: `~/ocudu-native-workspace/builds/ocudu-zmq-release/apps/gnb/gnb`.
- 폐기된 MIMO 시도(참조용): `/home/ubuntu/ocudu-gpu-channel-audit`, 브랜치 `mimo-patched`.

## 6. 이 프로젝트에서 반복해서 값을 한 규율

`AGENT_HARNESS.md`에 durable rule로 승격해 두었다. 요지만:

- **새 테스트는 실패시켜 보기 전까지 아무것도 증명하지 않는다.**
- **통과하던 기존 테스트도 아무것도 검증하지 않고 있을 수 있다** (M2: 시드 운을 채점하던 Bessel 게이트, M5.4: 아무것도 매치하지 않던 validator 정규식).
- **계기가 실제로 측정했는지 먼저 확인하라** (M3.7: 커널 카운트 0으로 초록색으로 끝나던 벤치).
- **소유권을 옮길 땐 "옛 소유자마다 뭐가 달랐나"를 먼저 물어라** (M4.2).
- **대조 실험의 도구가 그 일을 할 수 있는지 먼저 확인하라** (M5.4의 무효한 격리 실험).
- **peer를 탓하기 전에 그 peer의 단일 스레드 지점을 읽어라** (M5.4: 하나뿐인 `radio` 워커).

## 7. 알려진 부채

- **M0 라이브 게이트 2개**(multi-UE / multi-gNB) — unprivileged LXC + lock-step 가상시간의 지터 부재. 베이스라인에서도 동일 재현되므로 M0 결함이 아니다.
- ~~`docs/index.html`에 MIMO가 없다~~ — **해소됨(2026-08-16)**. Part VII(§22–§25)이 다중 포트 오버레이 전체를 다루고, §1/§2/§4/§8/§9의 거짓이 된 서술을 고쳤으며, §18.4(다중 포트 게이트)와 §20.1(다중 포트 실측)을 추가했다. 내부 앵커 94개 전부 resolve.
- **`fixed_mimo`와 `los_matrix`** — lane별 복소 행렬을 선언하는 knob이 둘이다. 없는 항목의 의미가 정반대(0 vs 에러)라 M3 종료 시 통합하지 않기로 판단했다(`docs/plans/m3-*.md` §2.6).
