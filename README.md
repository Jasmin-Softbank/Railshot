# Jasmin 하이브리드 PostgreSQL 구축

준비된 Ubuntu 24.04 가상머신에 PostgreSQL 16, Patroni, etcd, HAProxy를 구성하는 Ansible 코드입니다. 첨부 아키텍처의 저장소 구조를 따릅니다. Kubernetes 및 클라우드 자원 생성은 이번 구현 범위에 포함하지 않습니다.

## 현재 범위와 기본값

- 단일 Patroni 클러스터를 여러 거점에 배치합니다. 거점별 독립 대기 클러스터 구성은 지원하지 않습니다.
- 운영 정책은 선택 입력입니다. 기본 `replication_mode: synchronous`, `synchronous_mode_strict: true`, `synchronous_node_count: 1`입니다. 동기 복제본을 잃으면 쓰기를 차단하는 안전 우선의 잠정 기본값입니다. 가용성 요구에 맞는지 실제 시험이 필요합니다.
- `replication_mode: asynchronous`로 선택 변경할 수 있습니다. 장애 전환 시 미복제 데이터 유실 가능성이 있습니다.
- **`backup_enabled: false`가 기본입니다. 백업 미구성 상태이며 운영 준비 완료를 뜻하지 않습니다.** 저장소 마운트와 보관 정책을 정하고 명시적으로 활성화하세요.
- 초기 구축과 재실행을 지원합니다. 운영 중 버전 업그레이드·거점 승격·파괴적 초기화는 지원하지 않습니다.
- 예시는 3개 DB, 3개 etcd, 1개 접속 중계 서버입니다. 예시 중계 서버 하나는 단일 장애점입니다.

## 실행 준비

Python 가상환경에 `ansible-core==2.19.13`, `ansible-lint`를 설치하세요. 예시 주소는 실제 통신용이 아닌 문서용 주소입니다. 운영용 inventory는 예시를 복사하여 저장소 밖 또는 무시된 `inventories/production/`에 준비합니다.

필수 입력, 인증서 조건은 [입력 규격](docs/api/deployment-inputs.md)을 확인하세요. 비밀번호는 저장소에 기록하지 말고 Ansible Vault로 암호화한 별도 파일에 저장합니다. SSH 호스트 키도 사전에 확인해야 합니다.

```bash
cd infrastructure/ansible
ansible-playbook -i inventories/production/hosts.yml playbooks/site.yml   -e @/안전한/경로/vault.yml --ask-vault-pass
```

`-K`는 관리자 권한 비밀번호가 필요한 환경에서만 추가합니다. 실제 명령 실행은 대상 서버를 변경합니다. 문법 검사는 `../../ci/scripts/check-ansible.sh`로 실행합니다. `--check`는 전체 복제·장애 검증을 대신하지 못합니다.

## 실행 순서

`site.yml` → `preflight` → `common` → `etcd` → `patroni` → `routing` → `backup` → `verify`입니다. 역할의 기본값은 `roles/deployment_defaults/defaults/main.yml`에서 제공하므로 inventory의 선택 입력이 기본값을 덮어씁니다.

Ansible은 설치·구성을 수행하고, 운영 중 주 서버 선출과 장애 감지는 Patroni가 수행합니다. 기존 데이터가 존재하면 클러스터 소유권을 검사하며 자동 삭제·인수하지 않습니다. 실패 후 무조건 재실행하기 전에 오류와 기존 클러스터 상태를 확인하세요.

## 백업 범위의 제한

현재 백업 저장소는 노드별 로컬 마운트입니다. 주 서버가 바뀌면 WAL(복구에 필요한 변경 기록)과 백업이 노드별로 나뉠 수 있으므로, **같은 주 서버가 유지된 기간의 복원 검증만 지원합니다. 다중 거점 재해 복구 백업은 구현하지 않았습니다.** 공유·원격 저장소 설계와 장애 후 복원 시험이 추가로 필요합니다. 기존 클러스터에서 백업을 켜면 archive_mode 적용에 별도 재시작이 필요할 수 있습니다.

## 별도 복원

`playbooks/restore.yml`은 `restore_targets`의 빈 경로로만 복원하고 데이터베이스를 기동하지 않습니다. 운영 그룹과 겹치면 실패합니다. 대상에 PostgreSQL 16 및 pgBackRest가 사전 설치되어 있어야 합니다. `restore_confirm=true`, `restore_backup_set`, `restore_data_dir`, 마운트된 `pgbackrest_repo_path`가 필요합니다. 예시: `-e restore_confirm=true`는 문자열이므로 대신 `-e '{"restore_confirm":true}'` 또는 YAML 설정 파일을 사용하세요.

## 검증 상태

[검증 계획](docs/poc/validation-plan.md)과 [검증 결과](docs/poc/validation-results.md)를 분리합니다. 문법 통과는 실서버 동작 보장이 아닙니다. 복제·네트워크 단절·과반수 상실·백업 복원 시험 전에는 운영 검증 완료로 취급하지 않습니다.

약어: DB는 데이터베이스, SSH는 암호화 원격 접속, TLS는 통신 암호화·인증, SAN은 인증서의 허용 호스트 이름·주소 목록입니다.

## 고객 OpenStack 설치 프로그램

기존 데이터베이스 구축과 별도로 WireGuard 연결, 고객 측 OpenStack 인증·기능 진단·가상 머신 접근 준비를 수행하는 설치 프로그램을 추가했습니다. 사용법과 제한은 [설치 안내](docs/architecture/client-bootstrap.md), [등록 서버 규격 초안](docs/api/enrollment-contract.md), [로컬 인증정보 보관](docs/decisions/local-credentials.md), [검증 범위](docs/poc/bootstrap-verification.md)를 확인하십시오. 실제 고객 환경 동작은 아직 검증하지 않았습니다.
