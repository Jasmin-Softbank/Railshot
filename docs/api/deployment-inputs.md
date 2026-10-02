# 배포 입력 계약

## 필수 입력

| 입력 | 의미 |
|---|---|
| `cluster_name` | 영문 소문자로 시작하는 소문자·숫자·하이픈 이름, 최대 63자 |
| `db_nodes`, `etcd_nodes`, `proxy_nodes` | 데이터베이스(최소 2), etcd(홀수 최소 3), 중계(최소 1) 서버 그룹 |
| 호스트별 `private_ip`, `site` | 중복되지 않는 내부 도달 주소와 장애 영역 이름 |
| `ansible_host`, `ansible_user` 및 인증 수단 | SSH 원격 접속과 관리자 권한 |
| `client_cidrs` | DB 접속을 허용할 네트워크 목록 |
| `vault_postgres_password`, `vault_replication_password`, `vault_patroni_api_password` | 최소 16자 비밀번호, 별도 암호화 파일로 제공 |

## 인증서 파일

모든 `*_src`는 Ansible 실행 호스트의 파일 경로입니다. 개인키는 저장소에 넣지 않습니다. 인증서 유효기간·발급기관·호스트 이름을 확인해야 합니다.

- etcd 서버: `etcd_ca_src`, `etcd_cert_src`, `etcd_key_src`. 인증서는 노드 내부 주소를 포함하고 etcd 구성원간 서버·클라이언트 인증에 사용할 수 있어야 합니다.
- Patroni의 etcd 접속: `etcd_ca_src`, `patroni_etcd_cert_src`, `patroni_etcd_key_src`.
- Patroni 관리 통신: `patroni_api_ca_src`, `patroni_api_cert_src`, `patroni_api_key_src`. 인증서는 `private_ip` 주소와 `inventory_hostname` 이름을 모두 허용해야 합니다.
- PostgreSQL: `postgres_tls_ca_src`, `postgres_tls_cert_src`, `postgres_tls_key_src`. 복제 연결의 인증서 검증을 위해 각 노드 `private_ip`가 인증서 SAN에 필수입니다. 클라이언트가 HAProxy 서비스 이름으로 `sslmode=verify-full`을 사용한다면 모든 DB 인증서에 해당 공통 서비스 이름도 포함해야 합니다. SAN은 인증서가 허용하는 주소·이름 목록입니다.
- 중계 서버에도 `patroni_api_ca_src`가 필요합니다.

## 선택 입력

| 입력 | 기본값 | 설명 |
|---|---|---|
| `replication_mode` | `synchronous` | `asynchronous` 선택 가능 |
| `synchronous_mode_strict` | `true` | 동기 복제본 부재 시 쓰기 차단 |
| `synchronous_node_count` | `1` | 요구 동기 복제본 수 |
| `postgres_version` | `'16'` | 이번 구현은 16만 지원 |
| `postgres_data_dir` | `/var/lib/postgresql/16/patroni` | 데이터 경로 |
| `backup_enabled` | `false` | 기본은 백업 미구성 |
| `retention_full` | `7` | 보관할 전체 백업 개수, 일수가 아님 |
| `pgbackrest_repo_path` | 없음 | 백업 활성화 시 별도 마운트 저장 경로 필수 |

운영 정책 입력을 생략할 수 있습니다. 역할 기본값은 inventory 입력보다 우선순위가 낮습니다. 배포 실행별 변경은 별도 YAML 변수 파일로 제공할 수 있습니다. 비밀번호를 명령행 문자열에 직접 쓰지 마세요.

이번 버전의 지원 포트는 PostgreSQL 5432, Patroni 관리 8008, etcd 2379/2380이며 관리 계정 이름은 patroni로 고정 검증합니다. 임의 변경은 지원하지 않습니다. `client_cidrs`에는 PostgreSQL이 실제로 관측하는 HAProxy의 출발지 주소도 포함해야 합니다. 단순 TCP 중계는 원래 클라이언트 주소를 PostgreSQL에 전달하지 않습니다.
