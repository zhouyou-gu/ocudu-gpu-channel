# 세션 인수인계 — 2026-08-16

**이 파일은 재개용 요약이다. 정본은 `AGENT_PROGRESS.md`이고, 둘이 어긋나면 `AGENT_PROGRESS.md`가 이긴다**(`AGENT.md`의 우선순위 규칙). 여기에는 다음 세션이 5분 안에 상황을 복구하고 바로 손을 대기 위한 것만 적는다.

## 0. 먼저 읽을 것

`AGENT.md` → `AGENT_GOAL.mimo.md` → `AGENT_HARNESS.md` → `AGENT_PROGRESS.md` 순서(`AGENT.md`가 강제). 그다음 이 파일. M6 작업이면 [`docs/plans/m6-rank2-su-mimo-live.md`](docs/plans/m6-rank2-su-mimo-live.md)까지.

**미해결 사용자 결정**: `AGENT_GOAL.md`(원본)와 `AGENT_GOAL.mimo.md`(MIMO 조항) 중 어느 것이 지배하는지 미확정. 실질은 `.mimo`가 지배해 왔다. 에이전트는 자율로 정하지 않는다. **M6은 이 결정과 무관하게 진행 가능하다**(§3).

## 1. 지금 어디까지 왔나

베이스라인 `bc88865` 이후 **59 커밋**. 트리 clean.

| | 상태 |
|---|---|
| M0 단일 엔진 리팩터 | green 5 / **환경 차단 2** (multi-UE·multi-gNB, unprivileged LXC) |
| M1 차원 + 고정 행렬 | 전 게이트 green |
| M2 IID 확률적 페이딩 | 전 게이트 green |
| M3 공간 상관 + coherent LOS | 전 게이트 green |
| M4 physical link 단위 runtime control | 전 게이트 green |
| M5 라이브 통합 (transport + **행렬 검증**) | 전 게이트 green |
| **M6 rank-2 SU-MIMO 라이브 acceptance** | **M6.1 완료. M6.2부터가 다음 작업** |
| M7 massive MIMO | 계획만, 전제 확인 필요 |

**상시 게이트** (전부 2026-08-16 통과):
- `ctest` 8/8 — CPU 트리(`build/`) + CUDA 트리(`build-cuda/`)
- `scripts/remote/gpu-test-sequence.sh` **9/9**
- `scripts/native/run-ocudu-legacy-1x1.sh` — 라이브 1×1 attach `result=pass`
- `scripts/native/run-ocudu-mimo-2port-no-core.sh` — 라이브 2-port `status=passed`, **행렬 검증 포함**
- `scripts/native/check-workspace.sh` — `lock_validation=ok debs=94 archives=3 git_sources=8`

## 2. 이번 세션에 한 일 (커밋 순)

| 커밋 | 내용 |
|---|---|
| `8679bca` | **M5.4 해결.** 2-port 게이트 정지의 원인은 우리였다 — producer throttle의 무한 캐치업이 라디오를 실시간보다 1.76배 빠르게 먹였고, 다중 포트 ZMQ 라디오는 그걸 흡수 못 하고 교착한다. `RealTimePacer`(한 배치 넘는 지각은 버림) + REP 응답을 producer 윈도 경계로 자르기 |
| `040b2a3` | **M5.5.** 게이트가 "옮긴 것"만 채점하고 있었다(`marker_checks=0` 옆의 `marker_mismatches=0`). 브로커 wire capture + 독립 checker가 토폴로지에서 읽은 `H`로 `y=Hx`를 재계산. 뮤테이션 프로브 3건 |
| `83d6e9e` | `docs/index.html`이 드디어 MIMO를 설명한다. Part VII(§22–§25) 신규 + 거짓이 된 §1/§2/§4/§8/§9 수정 |
| `c9dbb74`, `c97541b` | **M6/M7 계획.** M6 상세 설계 문서, OAI 핀 `2026.w33` |
| `dab400c`, `0d5c5a2` | **M6.1 완료.** OAI nrUE 빌드 성공 (2분 09초) |

## 3. 다음 작업: M6.2 — 1×1로 OAI 회귀

**정본은 [`docs/plans/m6-rank2-su-mimo-live.md`](docs/plans/m6-rank2-su-mimo-live.md).** 요지만:

**왜 rank 2가 지금까지 안 됐나** — 막힌 계층은 UE 하나다. gNB(OCUDU)도 에뮬레이터도 이미 rank 2를 한다. srsUE만 못 하고, 그것도 **두 층이 동시에** 막혀 있다: `rrc_nr.cc:105`의 `max_mimo_layers = 1` 하드코딩(→ gNB가 애초에 rank 2를 스케줄 안 함) + `pdsch_nr.c:537`의 `// Antenna port demapping ... Not implemented`. 어느 한쪽만 고쳐선 우회 불가.

**수정안** — srsUE를 패치하지 않고 **OAI nrUE를 추가**한다. 핀 커밋에서 직접 확인했다: ZMQ 라디오 다채널 지원(채널당 폴 스레드), 와이어 프로토콜 바이트 동일(1바이트 더미 요청 → 헤더 없는 cf32), `nr_dlsch_demodulation.c`에 실제 2×2 MMSE + layer demapping. **이 저장소 C++는 0줄 바뀐다** — 에뮬레이터 코어에 srsUE 결합이 아예 없다(주석 4줄뿐).

**M6.2가 할 일** — OCUDU gNB 1T1R ↔ 브로커 ↔ **OAI nrUE 1R** 회귀 게이트. **UE 교체 자체를 rank 2와 분리해서 검증한다**(M5.4가 두 변수를 동시에 바꾼 대가를 이미 치렀다).

필요한 것:
- OAI UE conf 템플릿 (srsUE ini와 형식이 다름)
- 로그 판정 토큰 — OAI는 `RRC Connected` / `PDU Session Establishment successful`과 **다른 문자열**을 쓴다
- TUN 인터페이스 이름 (srsUE는 `tun_srsue`, OAI는 `oaitun_ue1` 계열)
- USIM 값(imsi/k/opc)을 open5gs `subscriber.csv`와 정합
- 게이트 스크립트 + 산출물 검증

**srsUE 1×1 게이트는 그대로 유지한다** — 회귀 안전망. `git_sources`가 배열이라 OAI는 **추가**된 것이지 교체가 아니다.

## 4. 명령어

```bash
cd /home/ubuntu/ocudu-gpu-channel-mimo-claude
source scripts/native/env.sh          # 네이티브 툴체인 + autotools 재배치
source ~/ocudu-loopback-workspace/tools/env.sh   # CUDA 12.8 (GPU 시퀀스용)

# 빌드 + 단위 테스트 (두 트리)
cmake --build build -j8      && ctest --test-dir build --output-on-failure
cmake --build build-cuda -j8 && ctest --test-dir build-cuda --output-on-failure

# 상시 GPU 시퀀스 9/9 (약 2분)
bash scripts/remote/gpu-test-sequence.sh

# 라이브 1×1 attach — srsUE (약 2분)
bash scripts/native/run-ocudu-legacy-1x1.sh

# 라이브 2-port + 행렬 검증 (약 1분)
bash scripts/native/run-ocudu-mimo-2port-no-core.sh

# OAI nrUE 재빌드 (약 2분, tmux 불필요)
bash scripts/native/build-oai-ue.sh

# 워크스페이스 무결성
bash scripts/native/check-workspace.sh
```

## 5. 환경

- 이 컨테이너 = **Threadripper PRO 7965WX (24C/48T) + RTX 5090 ×4**. `.config`의 `REMOTE_HOST=127.0.0.1` — **"remote"가 곧 이 기계다.**
- **Docker 불가**(unprivileged LXC). 네이티브 하네스(rootless netns)를 쓴다.
- 핀된 외부 소스 (`native-workspace.lock.json`, `git_sources` 8개):
  - OCUDU gNB `a1916edcd` — **수정 금지** (미션 Non-Goal + 게이트가 실행 전 거부)
  - srsRAN_4G `eea87b1` (release_23_11)
  - **OAI `2026.w33` = `2b69bde6aeafe892cda1531a0f0cbba2e37792cd`** ← 신규. **이 커밋이 소스 리뷰한 커밋과 동일하다** (근거와 핀이 같은 물건)
- **OAI 빌드에서 알아둘 것 세 가지** (전부 lock과 스크립트에 기록됨):
  - `-DCMAKE_PREFIX_PATH=<sysroot>/usr` **필수** — CMake의 `find_library`/`find_path`가 `CPATH`/`LIBRARY_PATH`를 안 읽어서, sysroot에 **처음부터 있던** `libsctp.so`가 없는 것처럼 보인다
  - `-DAVX512=OFF` **필수** — Zen4가 `__AVX512F__`를 켜서 `zmq_simd.h`가 AVX-512 분기를 타는데 Ubuntu noble의 SIMDe는 0.7.2뿐이라 함수가 없다. 대안은 최신 SIMDe vendoring(23.04 MS/s에서 UE PHY는 병목이 아니므로 지금은 불필요)
  - **autotools 재배치** — OAI가 asn1c를 `autoreconf`로 빌드하는데 Debian autoconf/automake/libtool이 절대경로를 박아 넣는다. env로 되는 건 `scripts/native/env.sh`에, 안 되는 3개 파일은 sysroot 사본을 in-place 패치(`debian_overlay.relocation_patches`에 기록). **sudo 안 씀** — deb는 `apt-get download`로 user-space 오버레이에
- 폐기된 MIMO 시도(참조용): `/home/ubuntu/ocudu-gpu-channel-audit`, 브랜치 `mimo-patched`

## 6. 이 프로젝트에서 반복해서 값을 한 규율

`AGENT_HARNESS.md`에 durable rule로 승격돼 있다. 요지만:

- **새 테스트는 실패시켜 보기 전까지 아무것도 증명하지 않는다.**
- **통과하던 기존 검사도 아무것도 안 재고 있을 수 있다** — M2의 시드 운 Bessel 게이트, M5.4의 아무것도 매치 못 하던 validator 정규식, M5.5의 `marker_checks=0`
- **계기가 실제로 측정했는지 먼저 확인하라.** 0은 모든 임계를 만족한다
- **peer를 탓하기 전에 그 peer의 단일 스레드 지점을 읽어라** (M5.4: 하나뿐인 `radio` 워커)
- **트래픽을 세는 게이트는 연산을 채점한 것이 아니다** (M5.5)
- **추정으로 넘기지 말고 재라** — M6.1을 "긴 tmux 작업"으로 넘길 뻔했는데 실측 2분 9초였고, 재는 김에 돌려보니 **두 군데서 빌드가 깨져 있었다**(SIMDe 부재, AVX-512 분기)
- **최종 acceptance 경로를 먼저 정찰하고 시작하라** — M0~M5는 성공 기준이 transport 수준이라 rank 2를 요구하는 게이트가 없었고, 그걸 복호할 UE가 존재하는지는 한 시간이면 확인됐을 일을 다섯 마일스톤 뒤에 물었다

## 7. 알려진 부채 / 열려 있는 결정

- **M0 라이브 게이트 2건**(multi-UE / multi-gNB) — 환경 차단. multi-UE는 **베이스라인 커밋에서도 동일 재현**을 확인했으므로 MIMO 회귀가 아니다(lock-step 가상시간에 실지터가 없음). 미검증 완화안 하나가 기록돼 있다: UE별 `[rf] freq_offset`
- **지배 mission 파일 미확정** — 사용자 결정
- **GH Pages 미공개** — 레포 private, 워크플로는 `workflow_dispatch` 게이트
- **`fixed_mimo`와 `los_matrix`** — 없는 항목의 의미가 정반대(0 vs 에러)라 M3 종료 시 통합하지 않기로 판단(`docs/plans/m3-*.md` §2.6)
- **M7 착수 전 필수 실측** — 합성 peer로 포트 8→16→32에서 ZMQ가 언제 무너지는지. `kMaxCorrelatedLanes=16`(4×4 상한), 184.32 MB/s/포트/방향, 그리고 **OCUDU의 단일 `radio` 워커**(M5.4의 그 벽)가 한계다
