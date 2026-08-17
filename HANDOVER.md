# 세션 인수인계 — 2026-08-17

**이 파일은 재개용 요약이다. 정본은 `AGENT_PROGRESS.md`이고, 둘이 어긋나면 `AGENT_PROGRESS.md`가 이긴다**(`AGENT.md`의 우선순위 규칙). 여기에는 다음 세션이 5분 안에 상황을 복구하고 바로 손을 대기 위한 것만 적는다.

## 0. 먼저 읽을 것

`AGENT.md` → `AGENT_GOAL.md` → `AGENT_HARNESS.md` → `AGENT_PROGRESS.md` 순서(`AGENT.md`가 강제). 그다음 이 파일. M6 작업이면 [`docs/plans/m6-rank2-su-mimo-live.md`](docs/plans/m6-rank2-su-mimo-live.md)까지.

**지배 mission 파일 확정 (2026-08-17)**: 사용자 지시로 MIMO 개정이 정본 `AGENT_GOAL.md`에 병합되었고, 병렬 사본 `AGENT_GOAL.mimo.md`는 삭제되었다. 이 결정은 더 이상 열려 있지 않다.

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
| **M6 rank-2 SU-MIMO 라이브 acceptance** | **M6.1–M6.2 완료 (08-17). M6.3(2포트 승격)부터가 다음 작업** |
| M7 massive MIMO | 계획만, 전제 확인 필요 |

**상시 게이트** (전부 2026-08-16 통과):
- `ctest` 8/8 — CPU 트리(`build/`) + CUDA 트리(`build-cuda/`)
- `scripts/remote/gpu-test-sequence.sh` **9/9**
- `scripts/native/run-ocudu-legacy-1x1.sh` — 라이브 1×1 attach `result=pass`
- `scripts/native/run-ocudu-mimo-2port-no-core.sh` — 라이브 2-port `status=passed`, **행렬 검증 포함**
- `scripts/native/run-ocudu-oai-1x1.sh` — **신규 (08-17)**: 라이브 1×1 OAI nrUE attach `result=pass`
- `scripts/native/check-workspace.sh` — `lock_validation=ok debs=94 archives=3 git_sources=8`

## 2. 이번 세션에 한 일 (커밋 순, 2026-08-17)

| 커밋 | 내용 |
|---|---|
| `fec3548` | **미션 확정.** 사용자 지시로 MIMO 개정을 정본 `AGENT_GOAL.md`에 병합, `AGENT_GOAL.mimo.md` 삭제. §0의 열린 결정이 닫힘 |
| `1b9d6cc` | **M6.2 완료.** OAI nrUE가 CUDA 브로커를 통해 1×1 attach/PDU/ping 완주 (`result=pass`, strict counter 0, gNB RF failure 0). 차단 요인 3건 전부 **브로커 없는 direct 대조**로 규명 후 수정: ① userns의 ns-scoped CAP_SYS_NICE → `setpriv`로 떨궈 OAI 폴백 경로 ② OAI가 SIB1 이전 CLI 캐리어로 UL 위상보상을 굳힘 → `--CO -95000000` (없으면 UL 겉보기 +817 Hz, Msg3 전멸) ③ srsUE 전용 `pdcch.dedicated` 오버라이드에 OAI 귀머거리 + `--uecap_file` 상대경로 기본값 미로딩 → 렌더에서 오버라이드 제거 + 절대경로. srsUE 1×1 무회귀 동시 확인. 에뮬레이터 C++ 0줄 변경 |

## 3. 다음 작업: M6.3 — 2포트 승격

**정본은 [`docs/plans/m6-rank2-su-mimo-live.md`](docs/plans/m6-rank2-su-mimo-live.md).** 요지만:

**M6.2가 확정한 것 (2026-08-17)** — OAI nrUE가 CUDA 브로커를 통해 1×1 attach/PDU/ping을 완주한다(`run-ocudu-oai-1x1.sh` `result=pass`). UE 교체라는 변수는 rank 2와 분리되어 소거됐다. 규명된 차단 요인 3건은 전부 M6.3에도 그대로 적용된다:
1. **rootless RT**: userns 안의 ns-scoped CAP_SYS_NICE 때문에 OAI가 SCHED_FIFO를 시도하다 abort → `setpriv --bounding-set -sys_nice`로 기동
2. **UL 위상보상**: OAI는 38.211 위상보상 주파수를 SIB1 이전의 CLI UL 캐리어(DL+`--CO`)로 굳힌다 → band 3 FDD는 `--CO -95000000` 필수 (없으면 UL에 겉보기 +817 Hz, Msg3 전멸)
3. **PDCCH/능력**: fixture의 srsUE 전용 `pdcch.dedicated` 오버라이드에 OAI가 귀머거리가 된다(렌더에서 제거, `render_gnb_oai`에 근거 기록) + `--uecap_file` 절대경로 필수 (기본이 상대경로라 미로딩 → maxMIMO layers 0 → DCI 1_1에서 AssertFatal)

**M6.3 정찰 결과 (2026-08-17, 상세는 `AGENT_PROGRESS.md` [M6.3 scout] 절)** — 2×2 인프라는 전부 동작한다: 2T2R gNB(코어 포함, **mac pcap은 disable 필수**), OAI 2안테나 2채널 ZMQ(콤마 리스트 CLI), rank-1 강제(`max_rank: 1`) 시 **attach+PDU 완주**. "첫 실패 후보"였던 OAI 2채널 ZMQ는 문제가 아니었다.

**실제 차단 지점: rank-2 PDSCH 복호 하나 — 그리고 용의 구간이 3단계로 좁혀졌다.** 소거 완료(전부 실측): MCS 마진(QPSK도 전멸) · CSI-RS 다중화 · DCI/DMRS 신호 구성(OAI 자체 gNB와 동일 조합) · 레이어 매핑 순서(양쪽 소스 표준) · PUCCH-ACK 오독(UL PUSCH 60/0인데 SRB1 RLC max-retx → DL이 진범) · **채널 추정+보간 전체**(핀 트리에 DEBUG_PDSCH 임시 패치로 복소 덤프, 원복 완료 — srsRAN의 프리코딩된 DMRS는 W에 의해 포트별 짝/홀 k'만 남는 표준 상쇄 패턴이고, OAI의 per-RE 추정+선형보간이 부호까지 정확한 −h/2를 산출함을 샘플 수준에서 확인) · OAI 자체 rank-2 수신(dlsim: identity/코드북 PMI, DMRS AddPos 0/1/2 전부 BLER 0).

**남은 용의: MMSE 등화 / LLR 생성 / 디매핑 입력 — 딱 3단계.** 다음 수는 실패한 rank-2 PDSCH 한 개의 `rxdataF_comp`(등화 후 성상도) 덤프: 깨끗한 QPSK 구름이면 LLR/디매핑, 쓰레기면 MMSE. **이게 풀리기 전에 M6.3 게이트 스크립트를 쓰지 말 것** — 실패를 자동화할 뿐이다. 재현 도구는 scratchpad가 아니라 이 문단의 레시피가 정본이다: 2×2 direct 대조는 M6.2 게이트의 렌더 산출물에서 gNB만 2T2R(안테나 2, 포트 4, mac pcap disable, `max_rank`/`max_ue_mcs` 노브)로 바꾸고 OAI에 `--ue-nb-ant-rx/tx 2` + `uecap_ports2.xml` + zmq 채널 2개를 주면 된다.

**srsUE 1×1 게이트는 그대로 유지한다** — 회귀 안전망(08-17 재확인 `result=pass`). OAI는 **추가**이지 교체가 아니다.

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

# 라이브 1×1 attach — OAI nrUE (약 2분, M6.2 게이트)
bash scripts/native/run-ocudu-oai-1x1.sh

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
