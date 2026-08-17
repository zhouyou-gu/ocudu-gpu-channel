# 세션 인수인계 — 2026-08-17 (rank1 워크스페이스)

## 오늘 세션 최종 요약 (2026-08-17 마감)

- **R0~R3 전부 완료** + UL-4R flaky 원인 규명(`pusch.max_ue_mcs: 9`) + 라이브 게이트 2종에 y=Hx 행렬판정 통합 + 측정 라벨 + oracle precoding 실험(예측과 0.01dB 정합, 반정합 널은 셀 소멸).
- **보고 산출물**: `docs/rank1-feasibility-report.{md,html,en.html}` — 상사 평가 보고서의 4대 질문을 실측 근거로 재답변하는 feasibility 중심 보고서 3종(한/영).
- **GitHub 공유 준비됨**: `~/bin/gh` 설치, 비밀정보 스캔 통과(.config는 ignored). 사용자 `gh auth login` + repo 이름/공개여부 결정 대기. 상사 문서(mimo-integration-report.html)가 커밋에 포함돼 있어 private 권장.
- 이 트리의 계획 항목은 전부 소진 상태 — 다음 작업은 사용자 지시(예: GitHub push) 또는 §"R3 완료" 항목의 후속 연구(8포트+ 재측정, 공간상관/CDL)뿐이다.


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
- **R3 완료 (원인 규명 포함)**: full 4T4R 게이트 `run-ocudu-rank1-4x1.sh` `result=pass`. UL-4R flaky의 뿌리는 CSI-off에서 gNB UL 링크어댑테이션의 과공격 MCS(상시 ~20% KO, bimodal=확률)였고 `pusch.max_ue_mcs: 9`로 절단 — fixture에 근거와 함께 정착, R2 게이트 cap 반영 재통과. epre=-inf는 SR-DTX 정상 로그였음(SR 308/308 검출). 상세는 `AGENT_PROGRESS.md` [R3 root cause] 절. 잔여 개선 3건 중 2건 완료(2026-08-17 후반): **행렬 검증 게이트 통합**(두 게이트가 매 실행 y=Hx 채점, `--allow-silent-source`로 무방사 DL 포트 선언) + **지연 라벨 실측**(RANK1_MILESTONES '측정 라벨' 절). 잔여 마지막 1건(oracle 프리코딩)도 완료 — MRT/port0/복제/반정합-널 4케이스 라이브, 예측과 0.01dB 정합, 널은 셀 소멸 (RANK1_MILESTONES 'Oracle precoding 실험' 절)

## 3. 명령어 (부모와 동일 + 이 트리 경로)

```bash
cd /home/ubuntu/ocudu-gpu-channel-rank1
source scripts/native/env.sh
cmake -S . -B build && cmake --build build -j8 && ctest --test-dir build --output-on-failure
bash scripts/native/run-ocudu-legacy-1x1.sh   # 1x1 srsUE 무회귀 (그대로 유효)
bash scripts/native/run-ocudu-rank1-2x1.sh    # R2: 라이브 2x1 DL MISO + 1x2 UL SIMO (약 2분)
```

주의: 네이티브 게이트들은 `~/ocudu-native-workspace`를 공유하고 `repo_root`를 스크립트 위치에서 유도하므로 이 트리에서 실행하면 이 트리 기준으로 돈다. 부모 트리와 **동시에** 라이브 게이트를 돌리지 말 것(포트/워크스페이스 충돌).
