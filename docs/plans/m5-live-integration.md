# M5 — 라이브 통합 상세 설계

상위 문서: [`MIMO_MILESTONES.md`](../../MIMO_MILESTONES.md) · 선행: [`m4-physical-link-runtime-control.md`](m4-physical-link-runtime-control.md)

**M5는 새 기계장치를 만들지 않는다.** M1~M4가 만든 다중포트 채널을 **실제 OCUDU gNB**에 물리고, 그것이 도는 것을 기록하는 마일스톤이다. 그래서 이 문서의 대부분은 "무엇을 만들 것인가"가 아니라 **"이미 있는 것을 어디서 가져와 무엇에 맞출 것인가"**다.

---

## 1. 지금 상태 — 확인한 다섯 가지

### 1.1 범위는 이미 정해져 있고, 절반은 끝나 있다

`MIMO_MILESTONES.md` M5가 두 단계를 지정한다.

1. **1×1 라이브 회귀** (M0 직후 한 번, M4 이후 다시) → **완료**. M4 직후 재실행했고 `20260815T103840Z` `status=passed`(rrc/pdu/ping 전부 1).
2. **멀티포트 OCUDU gNB ↔ 합성 2-port peer** → 남은 것은 이것 하나다.

그리고 2번에는 선행 조건이 붙어 있었다 — "OCUDU ZMQ의 다중 포트 `device_args` 문법을 소스로 확인하기 전에 이 게이트를 약속하지 않는다". **그 확인도 완료**했다(`AGENT_PROGRESS.md` 블로커 항목). 즉 M5는 지금 착수 가능한 상태다.

**주의**: 같은 절이 **명시적 비게이트**도 적어 두었다 — srsUE `release_23_11`은 rank-2 acceptance gate가 될 수 없고, **독립 srsUE 프로세스 두 개는 2-port UE 하나가 아니다.** M5의 다중포트 상대는 처음부터 **합성 peer**로 지정돼 있다.

### 1.2 게이트 스크립트는 이 트리에 있는데 부속물이 없다

`scripts/native/run-ocudu-mimo-2port-no-core.sh`와 `scripts/native/validate-mimo-2port-transport.py`는 **이미 여기 있다**(M0의 salvage에 함께 넘어왔다). 그런데 스크립트가 요구하는 세 가지가 없다:

| 필요한 것 | 현재 위치 |
|---|---|
| `apps/ocudu_mimo_transport_peer.cpp` (989행, 2포트 peer) | audit 트리 |
| `examples/native/ocudu/gnb_zmq_b210_fdd_2port_no_core.yaml` | audit 트리 |
| `examples/native/topology.ocudu.mimo-2port-transport.cuda.yaml` | audit 트리 |

세 가지 모두 `MIMO_MILESTONES.md` §3이 **"살림"**으로 지정한 자산이다(버릴 것은 `RadioNodeCoordinator`, 평행 API, 전용 커널 쪽이었다).

### 1.3 스키마가 그 사이에 달라졌다

audit 트리의 fixture는 **M1 이전 스키마**로 쓰여 있어 지금 파서가 거부한다.

- `radio_nodes:` 항목에 `role: gnb` — 우리 `RadioNodeConfig`에는 `role` 필드가 없다(id / tx_ports / rx_ports뿐).
- `fixed_mimo:` 블록 안에 `rx_ports: 2` / `tx_ports: 2` — 우리 스키마는 `coefficients:` 리스트만 받고, 차원은 **노드 선언에서 온다**(그게 M1.2의 결정이다: 차원은 한 군데에서만 말한다).

즉 복원은 복사가 아니라 **적응**이다. 계수 값·엔드포인트·의도는 그대로 두고 표기만 현재 스키마로 옮긴다.

### 1.4 스크립트가 fixture의 SHA256을 핀으로 검사한다

`audited_gnb_fixture_sha256` / `audited_topology_sha256`. §1.3의 적응은 곧 **핀 갱신**을 뜻한다. 핀은 "게이트가 감사된 그 파일을 돌렸다"는 보증이므로, **갱신 이유를 커밋과 스크립트 주석 양쪽에 남긴다** — 그러지 않으면 다음 사람이 핀을 무의미하게 여긴다. 바꾸는 범위는 **스키마 적응에 한정**하고, 계수·엔드포인트·안테나 수는 손대지 않는다.

### 1.5 gNB fixture가 소스 독해와 교차 확인된다

`gnb_zmq_b210_fdd_2port_no_core.yaml`이 이렇게 쓰여 있다:

```yaml
  device_args: tx_port0=tcp://127.0.0.1:2000,tx_port1=tcp://127.0.0.1:2002,
               rx_port0=tcp://127.0.0.1:2001,rx_port1=tcp://127.0.0.1:2003,base_srate=23.04e6
  nof_antennas_dl: 2
  nof_antennas_ul: 2
```

`ru_sdr_config_translator.cpp`를 읽고 유도한 문법과 **정확히 일치한다.** 소스 독해와 실제로 쓰였던 fixture가 서로를 확인해 준다.

---

## 2. 설계

### 2.1 왜 합성 peer인가 — 프로세스 두 개는 라디오 하나가 아니다

이 프로젝트의 다중포트 설계는 **"에뮬레이터가 여러 단일포트 엔드포인트를 하나의 논리 라디오로 취급한다"**이다. 그 취급이 의미를 가지려면 **상대편도 그 포트들을 하나의 라디오로 몰아야** 한다 — 같은 샘플 윈도우, 같은 시간 원점.

독립 프로세스 두 개는 그것을 하지 않는다. 각자 자기 페이스로 요청하고 각자 멈춘다. 그래서 M5의 상대는 **두 포트를 한 프로세스에서 구동하는 peer**다. 이것이 미션 성공 기준이 말하는 **"multi-port test peer"**이고, 밀레스톤이 srsUE를 비게이트로 못 박은 이유이기도 하다.

### 2.2 무엇을 측정하는가 — 포트별 마커

peer는 포트마다 **구별되는 누적 마커**를 송신한다(`cumulative_marker(port, ordinal)`, 포트 부호로 구분). 채널이 **dense**(비대각·비특이)이므로 각 수신 행은 **두 송신 포트 모두에 의존**해야 한다. 마커 검사가 그것을 판정한다:

- 행이 한 포트에만 의존하면 → 행렬이 대각으로 퇴화했거나 lane 하나가 누락된 것
- 행이 어긋난 순서로 섞이면 → 샘플 윈도우가 포트 간에 어긋난 것

즉 M0~M4가 구조적으로 보장한 **"모든 sibling 포트가 하나의 공통 윈도우에서 서비스된다"**를 실제 gNB 트래픽 위에서 판정한다.

### 2.3 왜 `fixed_mimo`부터인가

결정론적이기 때문이다. 통계 게이트가 아니라 **계수에서 계산한 해석적 기대값**으로 합격/불합격이 갈린다. 페이딩·상관은 이미 M2/M3에서 통계적으로 검증했으므로, 라이브 게이트에서까지 확률 판정을 껴안을 이유가 없다. 상관 페이딩 버전은 M5.5에서 **선택 사항**으로 둔다.

### 2.4 무엇을 주장하지 않는가

**transport-only.** UE 복조 없음, attach 없음, rank-2 주장 없음. 스크립트 헤더가 이미 그렇게 적혀 있고(`It is intentionally labelled transport-only and makes no UE/rank-2 claim`) 그 문구를 유지한다.

---

## 3. 작업 순서 (커밋 단위)

| # | 내용 | 검증 |
|---|---|---|
| M5.1 | 이 문서 | — |
| M5.2 | peer 앱 복원 + CMake 타깃 | 빌드, `--help`, peer 자체 셀프테스트가 통과 |
| M5.3 | fixture 2개 복원 + 현재 스키마 적응 + SHA 핀 갱신(사유 기록) | `validate_config` 통과, 브로커가 2포트 노드를 `implicit=false`로 resolve |
| M5.4 | 게이트 실행: 실제 2안테나 OCUDU gNB ↔ 브로커 ↔ 2-port peer | §4 전부 |
| M5.5 | (선택) `fixed_mimo` → `spatial_correlation` 버전 | 상관이 수신 전력에 나타나는지 |

M5.2는 **게이트가 아니다** — peer가 빌드되고 스스로를 검사할 수 있으면 된다. 게이트는 M5.4 하나다.

---

## 4. Exit 게이트

| 게이트 | 판정 |
|---|---|
| 실제 gNB가 2안테나로 기동 | gNB 로그에 ZMQ 라디오 4채널, `nof_antennas_dl/ul = 2` |
| 4개 엔드포인트 전부 IQ 흐름 | peer 요약 JSON의 포트별 수신·송신 카운트 > 0 |
| gNB 내부 실시간 위반 없음 | gNB 로그에 `Real-time failure in RF` **0건** |
| 브로커 데이터 무결성 | `tx_queue_overflows` / `tx_sequence_gaps` / `zmq_errors` 전부 0 |
| 노드가 다중포트로 해석됨 | `event=radio_node_resolved id=gnb0 tx[0]=... tx[1]=... implicit=false` |
| **각 수신 행이 두 송신 포트 모두에 의존** | peer 마커 검사 `marker_mismatches == 0`, 그리고 dense 계수에서 계산한 해석적 기대와 일치 |
| 1×1 경로 무회귀 | `run-ocudu-legacy-1x1.sh` 재실행 `status=passed` |
| 실시간 예산 | `gpu_process_us` p99 기록. 500 µs 슬롯 대비 여유를 수치로 남긴다 |

**뮤테이션 프로브** (FAIL 확인 후에만 게이트로 인정):
- 행렬을 대각으로 바꾸면(교차항 0) → 마커 의존성 게이트 FAIL
- 한 포트의 lane을 빼면 → 같은 게이트 FAIL
- peer를 두 개의 단일포트 프로세스로 대체하면 → 공통 윈도우가 깨져 마커 순서 FAIL (이것이 §2.1의 주장을 실증한다)

---

## 5. 리스크

**① 실제 gNB가 이 트리에서 2안테나로 뜬 적이 없다.** fixture는 있지만 여기서 실행된 기록이 없다. 1안테나 경로만 라이브로 검증돼 있다. 2안테나에서 처음 나오는 실패(대역폭·프로세싱 지연·PRACH 설정 등)는 M5의 실제 작업량이 될 수 있다.

**② SHA 핀.** §1.4. 적응 범위를 스키마로 한정하고 사유를 남긴다.

**③ 500 µs 예산.** 2포트면 에지가 4개(양방향), 샘플 처리량이 2배다. 현재 2×2 합성 측정이 `gpu_process_us` p99 **146 µs**이므로 여유는 충분하지만, **실제 gNB 트래픽에서 다시 잰다** — 합성 소스는 lock-step 페이싱이 다르다.

---

## 6. 비고 — 이 마일스톤이 닫지 않는 것

- **M0의 라이브 부채**(multi-UE / multi-gNB Docker 게이트, unprivileged LXC)는 그대로다. M5의 게이트는 Docker 없이 도는 네이티브 경로라 이 제약에 걸리지 않지만, 줄여주지도 않는다.
- **srsUE 다중포트 acceptance**는 여전히 비게이트다. 그것을 원한다면 필요한 것은 이 에뮬레이터의 변경이 아니라 다른 UE PHY다.
- **MU-MIMO 다중화 이득**도 범위 밖이다. 에뮬레이터는 그 채널을 제공하지만 OCUDU 스케줄러가 같은 PRB에 두 UE를 얹지 않는다(정책은 RR/QoS뿐, `resource_grid`가 사용 CRB를 비트맵으로 잠근다). 링크 **간** 상관이 필요해지는 것도 그때다 — 지금 `R`은 링크 **내부** lane 사이다.
