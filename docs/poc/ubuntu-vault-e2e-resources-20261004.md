# 임시 Ubuntu Vault E2E 자원 원장과 시험 상태 — 2026-10-04

이 문서는 승인된 단일 시험 VM의 소유 자원과 최종 검증 범위를 기록합니다. 자원 생성·삭제는 Cloud 콘솔 담당자가 수행했으며, 상세 원장은 권한 `0600`인 `/private/tmp/railshot-vault-e2e-20261004/resource-ledger.json`에 있습니다. 이 문서에는 개인키, 토큰, 시험 비밀값, SSH 접속 설정을 기록하지 않습니다.

## 현재 상태

- 프로젝트 `nate2402`에서 새 VM은 삭제됐습니다. 전체 instance 목록에서 신규 VM ID 부재와 기존 VM `205a0517-45cf-4b8c-a5c4-8d18e5680345`의 Active·기존 Floating IP `10.26.4.245` 유지를 확인했습니다.
- 초기 SSH 연결 뒤 `guest.check`와 `runtime.install`이 성공했습니다. runtime 단계는 7분 47초에 종료했고 K3s Node, Cilium agent/operator/Envoy, CoreDNS가 모두 Ready 상태임을 관측했습니다.
- 합성 canary Secret은 Kubernetes API readback과 SQLite 암호문 저장을 통과했습니다. SQLite 최신 row에서 시험 평문이 없고 `k8s:enc:aescbc:v1:` 표식이 있음을 확인했으며, canary namespace는 삭제했습니다.
- 중앙 제어 plane에서는 PGP 수신자 분리 초기화, Transit·감사·정책, 제한 helper, 상호 TLS 보관, Raft snapshot과 보관 SQLite 백업·검증을 통과했습니다. 이어 격리 복원, systemd 감시, 중앙 재시작 뒤 수동 unseal, 초기 root 폐기 뒤 후속 발급도 완료했고, 실제 Vault CLI 형식 검증을 고친 뒤 서비스 회귀와 Docker CLI 실기 각각 9개를 통과했습니다.
- 이후 대상 Vault 구성 전에 SSH 연결이 시간 초과로 끊겼고 콘솔 세션도 만료됐습니다. 01:36:38 UTC에 동일 VM의 hostname과 Kubernetes Node Ready를 다시 확인했으며, 콘솔은 `nate2402` 프로젝트로 다시 선택했습니다.
- 접속 경로가 복구되어 backend와 recovery 담당자가 남은 시험을 완료했습니다. 이번 실행 중 새 자원을 추가하거나 기존 자원을 재연결하지 않았고, 시험 뒤 이번 VM 및 전용 Floating IP 정리를 완료했습니다.
- 논리 환경 `01`은 초기화 영수증이 없는 `unknown` 실패, `02`는 custody CLI 형식 불일치 실패를 시험 기록으로 보존합니다. 두 실패를 성공으로 덮어쓰지 않았으며, 시험 중 유지한 두 local PV 데이터는 최종 VM·루트볼륨 삭제와 함께 정리됐습니다.
- 논리 환경 `03`은 독립된 빈 local PV에서 02:37:10–02:41:42 UTC에 native `secrets.configure`와 `secrets.verify`를 성공했습니다. 이어 실제 `prepare → apply → verify`, ESO Secret 동기화, 일반값·비밀값 주입·변경·삭제, AppRole 교체와 이전 자격 거부, Vault Pod 교체 뒤 기존 값 조회까지 통과했습니다. 부모의 독립 읽기 검수도 2026-10-04T02:53:03.688835Z에 통과했습니다.

## 이번 실행이 생성한 자원

| 유형 | 식별자 | 상태와 정리 조건 |
|---|---|---|
| VM | `36e3f96e-ee0c-45e7-bb0b-6694ab192cec` | `railshot-vault-e2e-20261004-01`; `m1.4c8g`(4 vCPU, 8 GiB), Ubuntu 24.04 이미지 `608319cc-d48c-4c85-902f-601f5047e601`. 전체 instance 목록에서 삭제를 확인했습니다. |
| 루트 볼륨 | `d3b1b802-00fb-42a2-892c-6791ba65ba50` | 80 GiB `__DEFAULT__`, delete-with-instance 옵션을 사용했고 전체 volume 목록에서 부재를 확인했습니다. |
| 포트 | `1b7c97b8-d2e3-4cdd-826a-d6ee5805c4a8` | VM 기본 포트였으며 전체 port 목록에서 부재를 확인했습니다. |
| Floating IP | `725bd39d-4440-4ff9-ace9-3ff49ea42fca` | 시험 전용으로 VM에 연결했고 Release 뒤 전체 Floating IP 목록에서 부재를 확인했습니다. |

## 정리 결과

이번 실행이 만든 네 자원은 모두 삭제·반납을 확인했습니다. 기존 VM·Floating IP는 목록에서 그대로 남아 있음을 확인했습니다. 재사용 네트워크·보안 그룹·키 쌍은 이번 실행에서 변경하거나 삭제하지 않았습니다.

| 항목 | 현재 결과 | 최종 확인 |
|---|---|---|
| VM | 삭제 확인 | 전체 5개 instance 목록에서 신규 ID 부재, 기존 `205a0517-45cf-4b8c-a5c4-8d18e5680345` Active·`10.26.4.245` 유지 확인 |
| 루트 볼륨 | 삭제 확인 | 전체 5개 volume 목록에서 신규 ID 부재, 기존 `4ef0dd31` In-use 유지 확인 |
| 포트 | 삭제 확인 | 2페이지 전체 17개 port 목록에서 신규 ID·`172.28.102.8` 부재, 기존 `51f8d846` 유지 확인 |
| Floating IP | 반납 확인 | 전체 9개 Floating IP 목록에서 신규 ID·`10.26.3.90` 부재, 기존 `98dbaa4b`·`10.26.4.245` Active 유지 확인 |

## 보존하는 재사용 자원

다음 자원은 이번 실행이 만들지 않았으므로 정리 대상이 아닙니다.

| 유형 | 식별자 또는 이름 | 처리 |
|---|---|---|
| 프로젝트 | `89d7fa23cfd14fcfbba0e9a6ad507320` | 재사용만 합니다. |
| 네트워크 | `c43fea0a-8e58-4475-a23a-760d119328c9` (`railshot-pgtest-1002-net`) | 재사용만 합니다. |
| 보안 그룹 | `ddfc5c62-14af-44e3-b6a6-35ef3905943f` (`railshot-vault-test-1004-sg`) | 기존 SSH 제한과 기본 egress를 그대로 사용합니다. 규칙을 변경하거나 그룹을 삭제하지 않습니다. |
| 키 쌍 | `aolda-waffle` | 기존 키 쌍을 참조할 뿐 개인키 자료를 복사·기록·삭제하지 않습니다. |
| 기존 VM / Floating IP | `205a0517-45cf-4b8c-a5c4-8d18e5680345` / `10.26.4.245` | 이번 시험 전에 있던 자원으로, 보존합니다. |

## 미완료 검증과 적용 경계

이번 자원 생성과 현재까지의 통과 항목은 제품 적용 가능 판정이 아닙니다. 최신 [대상 runtime 준비 기록](ubuntu-vault-e2e-backend-20261004.md)의 env01·env02 실패 기록은 보존합니다. env03의 실제 delivery·환경변수 주입·AppRole 자격 교체와 이전 자격 폐기는 통과했지만, 중앙 제어 plane의 완료 근거와 Vault CLI 형식 검증은 최신 [중앙 Vault·복구 보관 시험 기록](ubuntu-control-vault-20261004.md)의 범위로 한정됩니다.

env03은 단일 VM의 독립 빈 local PV 시험입니다. 제품 UI에서 프로젝트 revision을 저장한 뒤 GitOps 배포까지 연결하는 경로, 운영 CSI 내구성, 외부 공개 HTTP, 멀티클라우드 환경 간 이식은 이 시험에 포함되지 않았습니다.

중앙·대상 시험 담당자는 합성 비밀값과 자격 자료를 VM 내부에서만 사용했고 로컬로 반출하지 않았다고 확인했습니다. 로컬 Docker 시험의 private 임시 파일도 종료 처리에서 정리됐다고 확인했으며, 이 문서는 비밀값을 기록하지 않습니다.
