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

## 2. 다음 작업: R0부터 (RANK1_MILESTONES 표 참조)

R0 비대칭 토폴로지 → R1 결정론적 2×1/1×2 증명 → R2 라이브 srsUE(최대 리스크: PoC UE가 2T2R 셀에 attach하는가) → R3 4포트 확장.

## 3. 명령어 (부모와 동일 + 이 트리 경로)

```bash
cd /home/ubuntu/ocudu-gpu-channel-rank1
source scripts/native/env.sh
cmake -S . -B build && cmake --build build -j8 && ctest --test-dir build --output-on-failure
bash scripts/native/run-ocudu-legacy-1x1.sh   # 1x1 srsUE 무회귀 (그대로 유효)
```

주의: 네이티브 게이트들은 `~/ocudu-native-workspace`를 공유하고 `repo_root`를 스크립트 위치에서 유도하므로 이 트리에서 실행하면 이 트리 기준으로 돈다. 부모 트리와 **동시에** 라이브 게이트를 돌리지 말 것(포트/워크스페이스 충돌).
