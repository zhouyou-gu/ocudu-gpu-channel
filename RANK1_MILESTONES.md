# Rank-1 MISO/SIMO 마일스톤 (이 워크스페이스의 정본 로드맵)

미션: [`AGENT_GOAL.md`](AGENT_GOAL.md). 근거 문서: 상사 전달 보고서 [`mimo-integration-report.html`](mimo-integration-report.html)의 로드맵에서 **Sionna 단계를 제외**한 것 (Sionna는 다른 팀원 담당, 후일 병합).

이 트리는 `ocudu-gpu-channel-mimo-claude`의 2026-08-17 HEAD(`1ddb240`) 포크다. 따라서 M0~M5의 전 자산 — RadioNode 오버레이, producer 단일 윈도, `fixed_mimo`, 비대칭 차원 단위테스트(M1), wire-capture 행렬 검증(M5.5), 네이티브 라이브 하네스 — 을 그대로 물려받고, 보고서가 "Not implemented"로 지목한 항목들의 상당수는 **이미 구현되어 있다**. 남는 것은 비대칭(2×1/1×2) 구성과 srsUE 라이브 통합이다. rank-2/OAI 자산(M6 게이트)은 이 트리에서 사용하지 않지만 제거하지도 않는다(부모 트리와의 diff를 최소로 유지).

| 단계 | 내용 | Exit 게이트 | 상태 |
|---|---|---|---|
| R0 | **비대칭 토폴로지** — 2-port gNB 노드 ↔ 1-port UE 노드 선언, DL 1×2 row / UL 2×1 column `fixed_mimo` | validator 통과, 기존 ctest 전체 green (비대칭 단위테스트 포함), 합성 peer로 브로커 관통 + strict counter 0 | **완료 2026-08-17** |
| R1 | **결정론적 2×1/1×2 채널 증명** — 보고서의 "Deterministic channel" 게이트 | branch isolation(antenna-0-only / antenna-1-only), DL 위상 스윕의 코히어런트 합/상쇄, UL branch 독립성, CPU↔CUDA parity | **완료 2026-08-17** (위상 스윕은 뮤테이션 확인된 신규 단위테스트; branch isolation·코히어런트 합은 CUDA 브로커 실측이 해석 예측과 0.05~3% 정합) |
| R2 | **라이브 2×1 DL + 1×2 UL** — OCUDU gNB 2T2R ↔ 브로커 ↔ srsUE(nof_antennas=1) | attach + PDU + ping, strict counter 0, gNB RF failure 0, wire-capture로 y=Hx 벡터 검증 동시 통과. 1×1 srsUE 게이트 무회귀 | **완료 2026-08-17** (`run-ocudu-rank1-2x1.sh` `result=pass`; 1×1 무회귀 pass; 캡처 실측: UL 두 행 y=Hx ≤4.6e-05, DL row 3.7e-08. 알려진 구조적 사실: 이 구성에서 gNB port1은 방사하지 않음 — SSB/공용채널 port0 + CSI-RS off + rank-1 [1,0] 프리코딩. DL 두-branch 콘텐츠 증명은 R0/R1 합성이 담당) |
| R3 | **4×1 / 1×4 확장** | R1/R2와 동일 게이트를 4포트에서 반복; 주장은 계속 rank-1 | **부분 완료 2026-08-17**: 합성 1×4/4×1 y=Hx 완벽(UL 4행 ≤1.3e-07, DL off-diag 1.18) + **DL 4×1 라이브 완주**(4T2R 셀, attach+PDU+ping — fixture `gnb_zmq_b210_fdd_4t2r_rank1_srsue.yaml`/`topology.ocudu.rank1-4x1dl.cuda.yaml`). **UL 4R 라이브는 차단**: gNB 4-RX에서 간헐 epre=-inf/UL KO로 등록 실패 — 이분법으로 UL 4-RX 단독 고립(DL 4T 무관), rx_ring_batches 8도 무효, 채널측은 합성으로 무죄. 용의 구간은 OCUDU ZMQ 4-포트 수신 경로와 부분-윈도 서빙의 상호작용. 다음 세션 과제 |

**주장 경계 (보고서 writing-requirements 준용)**: 모든 결과는 "2×1/4×1 DL MISO, 1×2/1×4 UL SIMO"로 기술한다. "end-to-end 4×4 MIMO", rank>1, UE 수신 빔포밍, PMI 폐루프, MU-MIMO를 주장하지 않는다. 고정 DL 가중치의 이득은 위상이 채널과 정합할 때만 성립하므로, 검증된 프리코더 메타데이터 없이 "diversity"라 부르지 않는다.

**R2가 확정한 사실** (전부 실측/소스 근거, `AGENT_PROGRESS.md` R2 절 상세):
- **srsUE는 2T2R 셀에 attach한다** — 단 세 가지 gNB 적응이 필수이며 각각 근거가 있다: ① `ss2_type: ue_dedicated` + `dci_format_0_1_and_1_1: true` (OCUDU validator가 fallback DCI + DL 안테나 2를 금지, `du_cell_config_validation.cpp:290`; srsUE는 1_1/0_1을 실제로 지원 — 1×1 대조로 증명) ② `csi_rs_enabled: false` + `nof_cell_csi_res: 0` (srsUE가 2포트 NZP-CSI-RS FD-CDM2 매핑 미구현 — "Resource mapping is invalid or not implemented"로 connection setup 전체가 죽음) ③ `mac_enable: disable` (OCUDU가 >1 DL 안테나에서 MAC pcap 거부). 종전의 "srsUE용 common/false 오버라이드" 통념은 반전됨 — 그 오버라이드는 1×1에서만 유효한 선택이었다.
- gNB 2포트 ↔ 1포트 UE는 direct 연결이 불가능하다(포트1 요청을 응답할 상대가 없음) — 브로커가 필수 경유다.
- 이 라이브 구성에서 **gNB port1은 방사하지 않는다**(SSB/공용채널은 port0, CSI-RS off, rank-1 PDSCH 프리코딩 [1,0] — M6.3/R2 캡처 실측). DL의 두-branch 코히어런트 콘텐츠를 라이브로 실으려면 gNB/RU 측 oracle 프리코딩 실험이 필요하며 그것은 R3 이후의 별도 항목이다.
- 두 트리(rank1/mimo-claude)는 `~/ocudu-native-workspace`를 공유한다. rank1 게이트들은 전용 채널 빌드 `builds/ocudu-gpu-channel-rank1-cuda-release`를 쓴다(CMake 캐시가 소스 경로를 고정하므로 공유 불가). 라이브 게이트를 두 트리에서 동시에 돌리지 말 것.
