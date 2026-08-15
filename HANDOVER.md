# 세션 인수인계 — 2026-08-15

**이 파일은 재개용 요약이다. 정본은 `AGENT_PROGRESS.md`이고, 둘이 어긋나면 `AGENT_PROGRESS.md`가 이긴다**(`AGENT.md`의 우선순위 규칙). 여기에는 다음 세션이 5분 안에 상황을 복구하고 바로 손을 대기 위한 것만 적는다.

## 0. 먼저 읽을 것

`AGENT.md` → `AGENT_GOAL.mimo.md` → `AGENT_HARNESS.md` → `AGENT_PROGRESS.md` 순서(`AGENT.md`가 강제하는 순서). 그다음 이 파일.

**미해결 사용자 결정**: `AGENT_GOAL.md`(원본)와 `AGENT_GOAL.mimo.md`(MIMO 조항) 중 **어느 것이 이 워크스페이스를 지배하는지 아직 미확정**이다. 실질은 `.mimo`가 지배해 왔다(M1~M4의 설계 판단이 그 파일의 Constraints에서 나왔다). 에이전트는 이 파일을 자율로 수정하지 않는다.

## 1. 지금 어디까지 왔나

베이스라인 `bc88865` 이후 **약 50 커밋**. 마일스톤:

| | 상태 |
|---|---|
| M0 단일 엔진 리팩터 | green 5 / **환경 차단 2** (multi-UE·multi-gNB Docker 게이트, unprivileged LXC) |
| M1 차원 + 고정 행렬 | **전 게이트 green** |
| M2 IID 확률적 페이딩 | **전 게이트 green** |
| M3 공간 상관 + coherent LOS | **전 게이트 green** |
| M4 physical link 단위 runtime control | **전 게이트 green** |
| M5 라이브 통합 | M5.1–M5.3 완료, **M5.4 실패(원인 미규명)** |

**상시 게이트 상태** (전부 최근 통과):
- `ctest` 8/8 — CPU 트리(`build/`)와 CUDA 트리(`build-cuda/`) 양쪽
- `scripts/remote/gpu-test-sequence.sh` **9/9**
- `scripts/native/run-ocudu-legacy-1x1.sh` — 라이브 1×1 attach `status=passed`

## 2. 유일한 열린 작업: M5.4

**한 줄 요약**: 실제 2안테나 OCUDU gNB ↔ 브로커 ↔ 2-port 합성 peer 게이트가 실패한다. **그런데 같은 게이트가 2026-08-13에 두 번 통과했다** — 당시엔 폐기된 코디네이터 브로커였다. **바뀐 변수는 우리 브로커다.**

| | 08-13 통과 (구 브로커) | 08-15 실패 (M0~M4 브로커) |
|---|---|---|
| gnb 포트별 `pulls` | 23573 / 23482 (≈1180/s) | 331 / 328 후 **영구 정지** |
| gNB overflow | **0** | 약 2,000,000 |
| gNB 설정 | ←— 바이트 동일 —→ | |

상세와 두 가설, 실험 순서는 `AGENT_PROGRESS.md`의 **"M5.4 — 실패. 과거에는 통과한 게이트다"** 절에 있다. **다음 세션은 거기서부터 시작하면 된다.**

가장 싼 첫 실험 두 개:
1. `runtime.rx_ring_batches`를 4~8로 올려 게이트 재실행(가설 b 확인/기각, 30분).
2. audit 트리의 구 브로커를 빌드해 **오늘 fixture로** 실행(“변수는 브로커”를 직접 확인).

계측은 이미 들어가 있다 — puller의 누적 취득량 `acquired=`가 heartbeat/worker_summary에 나온다.

## 3. 명령어

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

# 실패 중인 2-port 게이트 (약 1분)
bash scripts/native/run-ocudu-mimo-2port-no-core.sh
# 결과: ~/ocudu-native-workspace/results/{reports,logs}/ocudu-mimo-2port-native/<타임스탬프>/
```

## 4. 환경

- 이 컨테이너 = **Threadripper PRO 7965WX (24C/48T) + RTX 5090 ×4**. `.config`의 `REMOTE_HOST=127.0.0.1` — **"remote"가 곧 이 기계다.** 별도의 RTX 워크스테이션을 찾을 필요 없다.
- **Docker는 이 컨테이너에서 불가**(unprivileged LXC). 네이티브 하네스(rootless netns)를 쓴다.
- OCUDU/srsRAN 소스: `~/ocudu-native-workspace/src/{ocudu,srsRAN_4G}` (핀: `a1916edcd`, `eea87b1`). gNB 바이너리: `~/ocudu-native-workspace/builds/ocudu-zmq-release/apps/gnb/gnb`.
- 폐기된 MIMO 시도(참조용): `/home/ubuntu/ocudu-gpu-channel-audit`, 브랜치 `mimo-patched`.

## 5. 이 세션에서 배운 것 중 다음 세션이 반드시 지킬 것

`AGENT_HARNESS.md`에 durable rule로 승격해 두었다. 요지만:

- **새 테스트는 실패시켜 보기 전까지 아무것도 증명하지 않는다.** 이 세션의 모든 게이트를 뮤테이션 프로브로 확인했다.
- **통과하던 기존 테스트도 아무것도 검증하지 않고 있을 수 있다** (M2: 단일 lane Bessel 게이트가 시드 운을 채점하고 있었다).
- **계기가 실제로 측정했는지 먼저 확인하라** (M3.7: 벤치가 커널 카운트 0으로 수천만 번 공회전하며 초록색으로 끝나고 있었다).
- **소유권을 옮길 땐 "옛 소유자마다 뭐가 달랐나"를 먼저 물어라** (M4.2: `live`를 링크로 올리자 `fixed_mimo` 행렬이 초기화 단계에서 지워졌다).
- **대조 실험의 도구가 그 일을 할 수 있는지 먼저 확인하라** — 이 세션에서 `ocudu-zmq-sink`로 gNB TX를 뽑아 잘못된 결론을 냈고, 1안테나로 반복해서야 도구가 무효임을 알았다.

## 6. 알려진 부채

- **M0 라이브 게이트 2개**(multi-UE / multi-gNB) — unprivileged LXC + lock-step 가상시간의 지터 부재. 베이스라인에서도 동일 재현되므로 M0 결함이 아니다.
- **`docs/index.html`에 MIMO가 없다** — 출하 기술 문서가 pre-MIMO 시스템을 설명한다(M0~M4가 문서에 존재하지 않음). 외부에서 보면 이 프로젝트는 아직 SISO 에뮬레이터다. 데드라인이 있다면 이게 코드보다 급할 수 있다.
- **`fixed_mimo`와 `los_matrix`** — lane별 복소 행렬을 선언하는 knob이 둘이다. 없는 항목의 의미가 정반대(0 vs 에러)라 M3 종료 시 통합하지 않기로 판단했다(`docs/plans/m3-*.md` §2.6).
