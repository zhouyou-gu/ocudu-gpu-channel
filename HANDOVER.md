# 세션 인수인계 — 2026-08-17 (rank1 워크스페이스)

**이 파일은 재개용 요약이다. 정본은 `AGENT_PROGRESS.md`이고, 둘이 어긋나면 `AGENT_PROGRESS.md`가 이긴다**(`AGENT.md`의 우선순위 규칙).

## 0. 이 워크스페이스의 정체

- **`/home/ubuntu/ocudu-gpu-channel-rank1` = `ocudu-gpu-channel-mimo-claude`의 2026-08-17 포크** (HEAD `1ddb240`, 브랜치 `rank1-miso-simo`). 사용자 지시로 생성.
- 미션: **rank-1 MISO/SIMO** — 상사 전달 보고서(`mimo-integration-report.html`)에서 **Sionna 부분만 뺀 목표**. DL 2×1(→4×1) MISO + UL 1×2(→1×4) SIMO, srsUE는 `nof_antennas=1` 유지. 정본 미션은 이 트리의 `AGENT_GOAL.md`(포크 시 사용자 지시로 개정됨), 로드맵은 `RANK1_MILESTONES.md`.
- **rank-2/OAI 워크스트림은 부모 트리(`ocudu-gpu-channel-mimo-claude`)에서 계속된다** — 여기서는 다루지 않는다. M6 하네스 파일들은 diff 최소화를 위해 남아 있을 뿐 이 트리의 게이트가 아니다.
- Sionna RT는 다른 팀원 담당. 이 트리는 후일 병합을 막을 설계만 피하면 된다.
- `MIMO_MILESTONES.md`는 부모 트리의 rank-2 로드맵 사본(참조용)이다. **이 트리의 정본 로드맵은 `RANK1_MILESTONES.md`다.**

## 1. 물려받은 것 (부모 M0~M5, 전부 green)

RadioNode 오버레이(공통 sample epoch, producer 단일 윈도), `fixed_mimo`/공간상관/coherent LOS, **비대칭 차원(2×1, 1×2) 단위테스트(M1)**, wire-capture `y=Hx` 검증(M5.5), 네이티브 라이브 하네스(1×1 srsUE attach 게이트, 2-port transport 게이트), `ctest` 8/8 두 트리, `gpu-test-sequence.sh` 9/9.

## 2. 현재 상태 (2026-08-17): R0–R2 완료, 다음은 R3

- **R2 라이브 게이트 통과**: `run-ocudu-rank1-2x1.sh` `result=pass` — gNB 2T2R ↔ 브로커(2×1 DL / 1×2 UL) ↔ srsUE attach+PDU+ping, strict counter 0. 1×1 무회귀 pass. 캡처 실측: **UL 두 행 y=Hx ≤ 4.6e-05 라이브 통과**, DL row 3.7e-08.
- 핵심 규명 3건(전부 소스/실측): OCUDU는 fallback DCI+DL2 금지 → `ue_dedicated`/1_1 필수이며 **srsUE가 1_1/0_1을 실제 지원**; srsUE는 2포트 CSI-RS(FD-CDM2) 미구현 → `csi_rs_enabled: false`+`nof_cell_csi_res: 0` 필수; MAC pcap은 DL2와 양립 불가. 상세와 캡처 레시피는 `AGENT_PROGRESS.md`의 Rank-1 Workstream 절.
- **R3 완료 (원인 규명 포함)**: full 4T4R 게이트 `run-ocudu-rank1-4x1.sh` `result=pass`. UL-4R flaky의 뿌리는 CSI-off에서 gNB UL 링크어댑테이션의 과공격 MCS(상시 ~20% KO, bimodal=확률)였고 `pusch.max_ue_mcs: 9`로 절단 — fixture에 근거와 함께 정착, R2 게이트 cap 반영 재통과. epre=-inf는 SR-DTX 정상 로그였음(SR 308/308 검출). 상세는 `AGENT_PROGRESS.md` [R3 root cause] 절. 남은 개선 후보: 캡처 행렬 검증의 게이트 통합(무방사 소스 허용 옵션), oracle 프리코딩 실험(DL 다중-branch 라이브 콘텐츠), 처리량/지연 라벨 실측

## 3. 명령어 (부모와 동일 + 이 트리 경로)

```bash
cd /home/ubuntu/ocudu-gpu-channel-rank1
source scripts/native/env.sh
cmake -S . -B build && cmake --build build -j8 && ctest --test-dir build --output-on-failure
bash scripts/native/run-ocudu-legacy-1x1.sh   # 1x1 srsUE 무회귀 (그대로 유효)
bash scripts/native/run-ocudu-rank1-2x1.sh    # R2: 라이브 2x1 DL MISO + 1x2 UL SIMO (약 2분)
```

주의: 네이티브 게이트들은 `~/ocudu-native-workspace`를 공유하고 `repo_root`를 스크립트 위치에서 유도하므로 이 트리에서 실행하면 이 트리 기준으로 돈다. 부모 트리와 **동시에** 라이브 게이트를 돌리지 말 것(포트/워크스페이스 충돌).
