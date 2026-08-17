# Rank-1 MISO/SIMO 마일스톤 (이 워크스페이스의 정본 로드맵)

미션: [`AGENT_GOAL.md`](AGENT_GOAL.md). 근거 문서: 상사 전달 보고서 [`mimo-integration-report.html`](mimo-integration-report.html)의 로드맵에서 **Sionna 단계를 제외**한 것 (Sionna는 다른 팀원 담당, 후일 병합).

이 트리는 `ocudu-gpu-channel-mimo-claude`의 2026-08-17 HEAD(`1ddb240`) 포크다. 따라서 M0~M5의 전 자산 — RadioNode 오버레이, producer 단일 윈도, `fixed_mimo`, 비대칭 차원 단위테스트(M1), wire-capture 행렬 검증(M5.5), 네이티브 라이브 하네스 — 을 그대로 물려받고, 보고서가 "Not implemented"로 지목한 항목들의 상당수는 **이미 구현되어 있다**. 남는 것은 비대칭(2×1/1×2) 구성과 srsUE 라이브 통합이다. rank-2/OAI 자산(M6 게이트)은 이 트리에서 사용하지 않지만 제거하지도 않는다(부모 트리와의 diff를 최소로 유지).

| 단계 | 내용 | Exit 게이트 | 상태 |
|---|---|---|---|
| R0 | **비대칭 토폴로지** — 2-port gNB 노드 ↔ 1-port UE 노드 선언, DL 1×2 row / UL 2×1 column `fixed_mimo` | validator 통과, 기존 ctest 전체 green (비대칭 단위테스트 포함), 합성 peer로 브로커 관통 + strict counter 0 | |
| R1 | **결정론적 2×1/1×2 채널 증명** — 보고서의 "Deterministic channel" 게이트 | branch isolation(antenna-0-only / antenna-1-only), DL 위상 스윕의 코히어런트 합/상쇄, UL branch 독립성, CPU↔CUDA parity | |
| R2 | **라이브 2×1 DL + 1×2 UL** — OCUDU gNB 2T2R ↔ 브로커 ↔ srsUE(nof_antennas=1) | attach + PDU + ping, strict counter 0, gNB RF failure 0, wire-capture로 y=Hx 벡터 검증 동시 통과. 1×1 srsUE 게이트 무회귀 | |
| R3 | **4×1 / 1×4 확장** | R1/R2와 동일 게이트를 4포트에서 반복; 주장은 계속 rank-1 | |

**주장 경계 (보고서 writing-requirements 준용)**: 모든 결과는 "2×1/4×1 DL MISO, 1×2/1×4 UL SIMO"로 기술한다. "end-to-end 4×4 MIMO", rank>1, UE 수신 빔포밍, PMI 폐루프, MU-MIMO를 주장하지 않는다. 고정 DL 가중치의 이득은 위상이 채널과 정합할 때만 성립하므로, 검증된 프리코더 메타데이터 없이 "diversity"라 부르지 않는다.

**알려진 리스크** (보고서 + 이 트리의 실측 지식):
- srsUE(PoC)가 2T2R 셀(CSI-RS 2포트)에 attach하는지는 미검증 — 보고서도 이를 delivery gate로 지목. srsUE용 PDCCH 오버라이드(`ss2_type: common`, `dci_format_0_1_and_1_1: false`)는 유지해야 한다(M6.2에서 OAI에만 제거했음).
- gNB 2포트 ↔ 1포트 UE는 direct 연결이 불가능하다(포트1 요청을 응답할 상대가 없음) — 브로커가 필수 경유다.
- OCUDU는 >1 DL 안테나에서 MAC pcap을 거부한다(`mac_enable: disable` 필요, M6.3 실측).
