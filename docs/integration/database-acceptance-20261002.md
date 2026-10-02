# 별도 PostgreSQL HA 환경 실제 검증 — 2026-10-02

이 기록은 전용 임시 AWS VM에서 수행한 설치·TLS 연결·동기 복제·동일 primary 기간의 백업과 격리 복원 결과다. **K3s에 DB를 배포하거나 가입시키지 않았다.** 검증 후 임시 자원을 모두 제거했다. 단일 AWS 리전·가용영역의 최소 구성이므로 다중 클라우드·거점 장애 내성이나 운영 준비 완료의 증거로 사용하지 않는다.

## 개발 의도와 소스

- 화균 원본: [README at 67d19efc](https://github.com/Jasmin-Softbank/Railshot/blob/67d19efc81b01b2a55b6dd54c198088b018bdd1f/README.md). 준비된 Ubuntu 24.04 VM에 PostgreSQL 16 / Patroni / etcd / HAProxy를 배치하고, Kubernetes·클라우드 자원 생성은 원본 구현 범위에서 제외한다. 단일 Patroni 클러스터를 여러 거점에 배치하는 모델이며 기본 정책은 synchronous / strict / 동기 복제본 1개다.
- [Slack 회의 정본](https://softbankhackathon2026.slack.com/files/USLACKBOT/F0C63236BL1/___________________): 2026-10-01 2:45–2:47 DB를 Kubernetes 밖에 두는 합의와 화균의 HA 담당, 2:49:49–2:51:07 입력·배치 예시 논의, 2:53:19 이후 DB/Patroni 담당 범위를 참조했다. 로컬 전사 `2026-10-01-huddle.md` SHA256: `3ca906b02b9ffb5014a9fff5ceadaf6719a69281fad71a5b9f37f5d95fac7903`.
- 성공한 설치 실행 소스: `bbe3679fa436168b7a2b8d1da171eb08f7742728`. 실행 전 203개 파일 SHA manifest를 보존했고 실행 후 그 체크아웃의 203개 파일이 그대로임을 확인했다.
- 통합 [PR #18](https://github.com/Jasmin-Softbank/Railshot/pull/18)의 최초 검증 head `8268b463a8f156f35b213b451abe9cca086ffcf9` 및 최신 integration을 반영한 head `974c90e9c5bb1b0578a725460b74667083048d62`와 **DB 실행 관련 58개 파일**(executor/API/guest/roles/playbooks/tasks/ansible.cfg)의 SHA가 모두 일치한다. 체크아웃 전체가 동일하다는 의미는 아니다. K3s 진입점·설정·회귀검사는 이전 integration 변경으로 차이가 있다.
- 첫 실패 실행은 개발 중 소스였으며 확정 커밋의 성공 검증과 구분한다. 당시 API/DB 핵심 파일의 관측 해시, native/HTTP unknown 기록을 따로 보존했다.

## 전용 검증 환경

AWS `ap-northeast-2` / `ap-northeast-2a`, Ubuntu 24.04. DB1·DB2는 PostgreSQL+Patroni+etcd, DB3는 etcd+HAProxy이며 별도 VM 1대는 복원 전용이다. DB 클러스터 3대는 t3.medium, 복원은 t3.small. PostgreSQL 실제 버전은 16.15, etcd 3.4.30, pgBackRest 2.50이었다.

- 소유 태그 `RailshotOwner=remaining-db-20261002`로 이번 실행의 임시 자원을 구분했다. 원장에 VM·EBS·SG ID를 보존했다.
- SSH는 공개 22번 포트 없이 SSM forwarding을 사용하고, SSM으로 읽은 호스트 키를 StrictHostKeyChecking으로 검증했다. DB SG에는 자기 SG에서 오는 2379/2380/5432/8008만 허용했다.
- 복원 VM은 별도 SG에 ingress가 없고 egress는 HTTP/HTTPS만 허용하여 DB/etcd/Patroni 포트로 원본 클러스터에 접속할 수 없게 했다. 저장소는 운영 네트워크 연결 대신 컨트롤러를 경유한 SSH/SSM으로 복사했다.
- 모든 EBS는 암호화된 20 GiB gp3이며 DeleteOnTermination=true. DB1·DB2와 복원 VM에는 별도의 백업 저장소 EBS를 마운트했다.
- IMDSv2 필수, 인스턴스 shutdown 동작 terminate, cloud-init 120분 자동 종료 타이머를 설정했다. 정리 직전 네 VM의 종료 타이머 active를 확인한 뒤 수동 종료했다. 종료된 VM에는 실행 중 타이머가 남지 않는다.
- 네 VM에서 k3s/containerd/dockerd/crio 실행 파일·서비스 및 K3s/Kubernetes 가입 경로가 없음을 확인했다. DB 전용 VM에 K3s 설치·가입을 수행하지 않았다.
- CA/개인 키/인증서, 3개 Vault 비밀, API 토큰은 저장소 밖 private 디렉터리와 0600 파일로만 취급했다. 이 보고서에는 비밀을 포함하지 않는다.

## 설치 두 번과 수동 재조정

1. 첫 요청 `remaining-db-install-20261002`는 etcd 3노드의 native gRPC health는 성공했지만, Patroni가 이용하는 mTLS HTTP `/v3/cluster/member/list`가 404를 반환하여 설치가 실패했다. etcd YAML에 gRPC gateway 활성화가 빠져 있었다. API는 성공으로 바꾸지 않고 `DATABASE_CONFIGURE_FAILED` / durable `unknown`을 기록했다.
2. 첫 worker가 자연 종료했고 자식 Ansible/SSM이 남지 않았음을 확인했다. 원래 state와 unknown 기록을 삭제하지 않았다. DB 소유 마커, 빈 PostgreSQL data_dir, etcd 구성·멤버·health를 확인하고 snapshot을 보존했다. Patroni를 멈춘 뒤 검토된 정본 template과 정확히 같은 설정을 한 etcd 멤버씩 반영했다. 의미상 변경은 `enable-grpc-gateway: true`뿐이며, 각 재시작 후 quorum과 v3 응답을 검사했다. cluster ID `13280836708306583053`와 3개 멤버를 유지했다.
3. 이 증거와 운영자 재조정 결론을 기록한 뒤 별도 state/request `remaining-db-after-gateway-20261002`로 재개했다. 09:46:12–09:50:06 UTC 약 234초 후 native HA playbook+nonce-bound readiness가 성공했다. HTTP 상태 `succeeded`, `guest_ready=true`, `database_ready=true`였으며 `runtime_ready=false`, `application_ready=false`, `public_http_verified=false`를 유지했다.
4. 같은 HTTP 요청 재전송은 200으로 동일 record를 반환했다. API를 종료·재시작한 뒤 GET 및 동일 POST도 원래 created_at/updated_at/result와 완전히 일치했고 새 worker가 시작되지 않았다. 이 검증은 API의 durable replay이며, 성공 후 Ansible을 두 번째로 전부 재실행한 시험은 아니다.

## 데이터와 복원 결과

| 항목 | 실제 관측 |
|---|---|
| HAProxy 쓰기·읽기 | DB3:5432를 거쳐 sslmode=verify-full로 연결. TLS 1.3, pg_stat_ssl.ssl=true, 연결된 서버는 DB1 primary |
| 시험 데이터 | public.railshot_validation의 id=1, marker=`remaining-db-20261002-after-gateway` 생성 및 조회 |
| 동기 복제 | DB1 recovery=false, DB2 recovery=true. DB2 state=streaming / sync_state=sync. 양쪽 시험 행 동일, sent/replay LSN 모두 `0/30777E0` |
| 클러스터 식별 | DB1·DB2 system_identifier=`7692005233209740573` |
| Full backup | primary DB1의 팀 jasmin-backup wrapper 성공. pgBackRest set `20261002-095152F`, status code=0, error=false, WAL start/stop=`000000010000000000000005`, backup LSN `0/5000028`–`0/5000100` |
| 팀 복원 playbook | 별도 복원 VM의 빈 `/var/lib/postgresql/16/isolated-restore`에 원본 `playbooks/restore.yml` 1회 실행, rc0, ok15/changed2/failed0. 이 단계는 파일 복원만 수행 |
| 별도 WAL/SQL 검사 | 검증자가 로컬 UNIX socket, port55432, listen_addresses=''로만 WAL recovery를 실행. target-action=shutdown의 종료를 확인한 후 이 격리 사본에서만 recovery.signal을 제거하여 SQL 비교를 수행 |
| 복원 SQL | recovery=false, 시험 행 1개 및 marker·system_identifier가 원본과 일치. TCP listen_addresses='' 확인. 검사 뒤 pg_ctl fast stop 정상 종료 |

보조 검증 스크립트의 오류도 구분하여 남겼다. 첫 저장소 전송은 ext4 `lost+found` 권한 때문에 source tar rc2가 되어 복원 playbook을 실행하지 않았다. 같은 백업의 `archive`/`backup` 디렉터리만 복사한 두 번째 전송은 성공했다. 파일 복원 후 첫 별도 PG 기동은 복원된 설정의 절대 hba_file/ident_file이 원래 data_dir를 가리켜 시작 전에 실패했다. 제품 playbook을 바꾸지 않고 검증자의 기동 옵션을 복원 사본의 기존 hba/ident 파일로 지정하여 성공했다. immediate recovery의 의도된 shutdown 때문에 pg_ctl의 첫 start 반환값은 1이었고, WAL 로그의 consistency 도달·shutdown 및 후속 SQL/정상 종료로 복구 결과를 판단했다.

## 해석의 한계

- 강제 failover, 네트워크 분리, DCS 과반수 상실, 다른 AZ/거점/클라우드 장애는 주입하지 않았다.
- HAProxy 한 대는 단일 장애점이다. 이번 시험이 운영 고가용성 또는 다중 거점 장애 내성을 증명하지 않는다.
- 로컬 pgBackRest 저장소는 노드별로 나뉜다. 백업과 복원은 DB1이 primary를 유지한 같은 기간의 사본으로만 검증했다. primary 전환을 가로지르는 재해 복구나 공유/원격 백업은 검증하지 않았다.
- 원본 restore playbook의 제품 계약은 파일 복원까지다. WAL 기동·설정 경로 조정·SQL 비교는 이번 검증자가 격리 VM에서 수행한 추가 검증으로 구분한다.
- 검증용 데이터만 생성했다. 기존 production/화균 보유 VM 또는 소유가 불명확한 VM에는 접속·수정하지 않았다.

## 정리 결과

최종 AWS 조회 시각 `2026-10-02T09:59:19.191767+00:00`에 소유 태그 기준 non-terminated EC2 0, EBS 0, SG 0, 두 SG에 연결된 ENI 0을 확인했다. 아래 네 인스턴스는 모두 terminated이며 7개 EBS와 2개 SG도 제거됐다. 로컬 API/컨트롤러/해당 VM SSM forwarding 프로세스는 0개다. 검증 로그·실패/성공 durable 기록·source/evidence manifest는 private 디렉터리에 보존했다.

| 역할 | EC2 instance ID | 제거된 EBS ID |
|---|---|---|
| db1 | `i-0e694307369a5d2f7` | `vol-01ad12854eceb7e19`, `vol-083da161f56549a2d` |
| db2 | `i-0b61683cbdc540e3b` | `vol-07e0be872a21dda43`, `vol-02ea45d5575e4576b` |
| db3 | `i-05c78ee1c7c27ec15` | `vol-0fd4962119a97d73e` |
| restore | `i-054fc0b0b8bc03c8e` | `vol-08b720844ab87d644`, `vol-08ee43589ab58671d` |

제거된 SG: `sg-01b7455be1d0509fc`, `sg-01cbf7d656ee99f8a`. ENI는 위 SG 연결 조회에서 잔존 0으로 확인했다.

## Private 증거 SHA256

다음은 별도로 보존한 원본 증거의 해시다. 비밀·인증서·원시 Vault·전체 private 설정을 저장소에 복사하지 않는다.

| 증거 파일 | SHA256 |
|---|---|
| `second-source-manifest.json` | `7f366bc0b6b4d3a47a478469324fa0401151728a8dc6385d3470e60ed9d7ea33` |
| `source-post-run-readback.json` | `b65a50f5d5d125bffcb611a904ad8c098653cd97684c005ae8a3ed1da6f372c1` |
| `root-source-comparison.json` | `c3964945ca39e8a00b00fd7cd8c8f34bb6ef03c889f715da292d98ffb1d8ccf4` |
| `first-install-result.json` | `d26e700e603ca8cf055e0f4f62e85b7a6b180c559b99fadf0756fd0ed3725203` |
| `first-native-record.json` | `490150b2f2c20f7b53ff017acc696c4a7f019778887061f1f7bba9bb370cbe22` |
| `first-http-record.json` | `006d3ab9d24d14889294952eaaeda48ef088033b78d72e526ee81e4b5cd52d68` |
| `operator-reconciliation.json` | `9eff2947d0677c7a7b062bf1ddf9834e5e115b9e243e445f7bfa2c0890a44739` |
| `etcd-snapshot-status.txt` | `e4fa13d317dc5e0acf7590e841866a22f5f5236ae2108233d41a3e9bee9fbe5e` |
| `gateway-maintenance.log` | `f33ebeaaf5494720ba8cd58fceb7551bd442d4c17e4c07a73c058bfdddcdd605` |
| `second-status.json` | `b9f6b98231a8fddd9bea6f90e88dcdf326616c54de43386863b38176ced27add` |
| `same-request-replay.json` | `3e897f2840c07f577c4cde19db40181b30776c1402fba0fcd4801dabeb286c90` |
| `restart-replay.json` | `8255ec27e1caee83c6a168331f30f99aa94152fcdab5f87f2645a145150d8709` |
| `live-sql-verification.json` | `c6a90d20c349efcd607fec1da234d59a384a67ad2f1d55537b7f44db8b398f72` |
| `backup-info.json` | `95618efa8b81340e71e3db55a8b297edffc48185b698fbfa20dce4b8376432e2` |
| `repository-transfer-attempts.json` | `def81a7f2ee79bd9cf1fe7adea6dc407c49fb765b97e3f4e553a18367ba3b61b` |
| `original-restore-playbook.log` | `78c24f7180f41c4e4da163e6eba6e1ad341a48ca43d774a74017864fa729970e` |
| `restore-file-receipt.json` | `19cb1fe2a2e15d889ea20cb0b40a4a4903d62a62a403a7ad51e23ab101341e6b` |
| `isolated-start-reconciliation.json` | `15462cf55f4f1dacbf2827b38b381300496d129205c34882fa5aae54147365ce` |
| `isolated-restore-sql.json` | `06ad723608a1c154736ac1eadeaeeb7759070f9b22053d219f035e95b73a8c9b` |
| `database-isolation-readback.json` | `a565b20a38c7de7b247b84f7257e22284cb341417ebd4b745bda982991b084a9` |
| `pre-cleanup-final-readback.json` | `29224034f27b70455773beb049c17eda847cc4a692b8514e782ea7eda3c4a648` |
| `resources.json` | `719e0c4004e232684c43b86cd1de3302b15707538a27808687ae6e80898a7c89` |
| `cleanup-receipt.json` | `305f46b18e932bc4001423d9de653f2c49327023000acfd113df36398587bebf` |
| `process-cleanup-readback.json` | `0e197093480d81f1052c1ccfd80080117fde0c7f61de4c494838c92d88cef72d` |
| `root-source-comparison-final.json` | `288fed90f904062f2546272b9b48d8b2a37ab8c606b4aba64b8778461371e416` |
