# Ubuntu 임시 VM 중앙 Vault·복구 보관 시험

시험일: 2026-10-04. 중앙 서비스의 아래 실검증을 완료했습니다. 대상 Kubernetes 환경의 결과와 임시 클라우드 자원 삭제는 별도이며, 이 문서로 전체 시험 완료를 주장하지 않습니다.

## 범위와 대상

사용자가 승인한 임시 VM `railshot-vault-e2e-20261004-01` (`36e3f96e-ee0c-45e7-bb0b-6694ab192cec`), Ubuntu 24.04.5, 4 CPU / 8 GiB / 80 GiB만 변경했습니다. 사설 주소 `172.28.102.8`, 연결 주소 `10.26.3.90`이며 기존 VM에는 접근하지 않았습니다. 콘솔에서 독립 확인한 ED25519 지문 `SHA256:iy8OZMRppzJBlmC2fcTmLJL3ANKN+8R+DD1EwFTUE1U`을 전용 known_hosts에 고정했습니다. 사용자 SSH 개인키는 VM에 복사하지 않았습니다.

중앙 Vault와 보관 서비스는 Kubernetes 밖의 Docker Compose로 실행했습니다. 중앙 포트는 사설 IP의 18200/19443에만 바인딩했고, 외부 인터페이스 `ens3`에서 두 목적지 포트로 들어오는 연결을 DOCKER-USER 규칙으로 차단했습니다. 방화벽 보안 그룹의 공개 포트는 추가하지 않았습니다. 이번 기록의 키·토큰·PGP 수신자는 모두 합성 시험용입니다.

Vault 이미지: `hashicorp/vault:2.1.1@sha256:47f14a6acb98f48d798a07df7c83f23a6e636e1cf724c5f8ff165cb32667a1e2`.

보관 서비스는 저장소 Dockerfile로 VM에서 직접 빌드했습니다. 최초 빌드의 manifest list digest는 `sha256:19926717bde95e1cf42e8ab31735f19458f5c728bfc95c43701ad377e122b6a6`입니다. 호스트 도구는 `/opt/railshot-control` 가상환경의 Python과 cryptography 50.0.2를 사용했습니다.

## 관측된 결과

| 항목 | 결과와 근거 |
|---|---|
| Compose 실제 기동 | Vault UID 100:1000, 읽기 전용 루트, 모든 capability 제거, no-new-privileges 및 소유권을 적용한 구성으로 컨테이너가 시작되었습니다. 보관 서비스도 읽기 전용 루트와 권한 제한 상태로 실행됐습니다. |
| 실제 PGP 초기화 | GnuPG로 서로 다른 수신자 6명을 생성하여 실제 bootstrap 스크립트로 Vault를 초기화했습니다. 복구 분할키 5개 수신자와 root 토큰 수신자를 분리했고, 암호문에서 메모리로 복호화한 분할키 3개로 봉인을 해제했습니다. |
| 중앙 구성 | Transit, 감사 기록, 발급 정책 및 부모 root에 종속되지 않는 주기 갱신 운영자 토큰 생성이 성공했습니다. 초기 root 평문 파일은 시험 중 관리자 전용 `/run`에만 보관했고, 최종 폐기 검증 후 삭제했습니다. |
| 실제 sudo 권한 경계 | uid 1100 `railshot-api`가 중앙 CA 개인키 읽기와 임의 sudo 실행을 거부당했습니다. `../escape` 환경명도 거부됐습니다. 고정 발급 helper만으로 `helper-env-a/b`를 발급했고, 독립 키·토큰과 uid 1100 소유의 제한된 출력 파일을 확인했습니다. |
| 실제 상호 TLS 보관 | 환경 A 저장 및 동일 요청 재시도, 내용 변경 충돌, 환경 B의 저장·조회 거부를 확인했습니다. runtime 인증서의 delivery export는 거부했고 manager 인증서의 암호화 응답을 복호화해 원문 일치를 확인했습니다. |
| 백업 묶음 | 실제 Raft snapshot과 보관 SQLite 백업으로 묶음을 생성·검증했습니다. 별도 관리자 전용 경로로 복원한 SQLite의 암호문 복호화 검증이 통과했습니다. |
| 격리 서비스 복원 | localhost 19200/19543의 별도 Vault·보관 서비스 컨테이너에서 복원했습니다. 원래 PGP 분할키 3개로 봉인 해제 후 기존 중앙의 Transit 암호문을 복호화했고, 보관 HTTPS에서 복원된 receipt 일치를 확인했습니다. UID 100/읽기 전용/권한 제거를 유지했고 시험 컨테이너 2개를 삭제했습니다. |
| 실제 systemd 감시 | `control-monitor.timer`의 enabled/active 상태와 service 정상 실행을 확인했습니다. 백업 manifest 수정시각을 25시간 전으로 바꿔 실패 신호를 확인하고, 원래 시각 복구 후 service success로 돌아왔습니다. 운영자 토큰 갱신도 실제 실행했습니다. |
| 중앙 재시작과 수동 봉인 해제 | 중앙 컨테이너 재시작 후 sealed 상태를 관측했습니다. PGP 암호문에서 메모리로 복호화한 원래 분할키 3개로 수동 unseal하고 active 상태 복귀를 확인했습니다. |
| 초기 root 폐기 | root의 revoke-self 후 lookup-self 403을 확인했습니다. 기존 운영자·환경 토큰은 조회와 갱신에 성공했고, `/run/railshot-control-bootstrap/root-token`을 삭제했습니다. |
| root 없는 후속 발급 | 실제 고정 sudo helper로 새 환경 `helper-post-root`를 발급했습니다. 새 환경 토큰으로 해당 Transit 키의 암호화·복호화를 확인했고, 감시 서비스도 root 폐기 후 성공했습니다. |

helper-only 검사는 API 계정에 대상 자동화용 SSH 자격을 넣기 **전에** 실행했습니다. 이후 다른 시험 단계에서 VM 안에서 만든 합성 SSH 키로 API 계정이 Ubuntu 계정에 접속할 수 있게 되었으므로, 그 이후의 시험 계정 권한을 helper-only 운영 경계와 혼동하면 안 됩니다.

## 발견한 시험 준비 문제

- 봉인 해제 응답 직후에는 Vault가 아직 active 역할로 전환 중이었습니다. 시험 준비 코드가 이 대기를 생략하여 최초 `configure`의 `sys/mounts` 조회가 500으로 실패했습니다. 초기화나 키 생성을 재실행하지 않고 active 전환 후 configure 단계만 다시 실행해 성공했습니다. 시험 코드에는 제한된 active 대기를 추가했습니다. 제품 bootstrap과 configure는 원래 별도 수동 명령입니다.
- 복원 출력의 상위 디렉터리로 공용 탐색 권한이 있는 `/srv/railshot-control`을 사용하자 보관 코드가 거부했습니다. 시험 출력 경로를 root 0700인 `backups` 아래로 바꿔 복원했습니다. 안전 검사를 완화하지 않았습니다.
- 격리 복원 시험의 최초 클라이언트가 중앙 주소를 loopback으로 지정하여 컨테이너 생성 전에 연결 거부를 받았습니다. 사설 IP 바인딩에 맞게 시험 주소를 수정했습니다.

- 격리 복원 최초 저장소 `isolated-vault`의 fresh init 요청은 30초 read timeout으로 실패했습니다. 완료 여부가 불확실한 같은 초기화를 재요청하지 않았고, 해당 컨테이너를 삭제한 다음 독립 저장소 `isolated-vault-attempt2`에서 시험했습니다. 두번째 시도는 180초 제한 안에 초기화·봉인 해제·snapshot-force 적용을 통과했습니다. 정확한 초기화 소요 시간은 별도 측정하지 않아 수치로 주장하지 않습니다.
- 두번째 저장소의 snapshot 적용 직후 고정 3초 대기로는 후속 unseal이 성공하지 않았습니다. snapshot이 적용된 저장소를 재초기화하지 않고 재기동했고, 원래 PGP 분할키의 진행도 1→2→봉인 해제를 확인했습니다. 이후 실제 암호문/receipt 검증이 통과했습니다. 운영 복원은 고정 대기보다 상태 확인을 사용해야 합니다.

## 실제 Vault CLI 형식 결함과 수정

후속 대상 환경 `railshot-vault-e2e-20261004-02`에서 초기화 자체는 완료됐지만 보관 감사에 `store` HTTP 422가 기록됐고 해당 환경 영수증은 없었습니다. 키·토큰 원문을 추출하지 않고 감사 메타데이터와 공식 [Vault 2.1.1 `newMachineInit`](https://github.com/hashicorp/vault/blob/v2.1.1/command/operator_init.go#L524-L528)을 비교해 원인을 확인했습니다. 기존 보관 검사가 unseal 숫자를 0/0만 허용하여 실제 CLI의 1/1을 거부한 구현 결함이었습니다. 이 실패를 초기화 시간 초과로 단정하지 않았습니다.

수정은 빈 unseal 배열 두 개와 복구 shares/threshold 5/3이 모두 명시된 경우에만 1/1을 추가 허용합니다. 기존 0/0은 유지하고 잘못된 숫자 쌍, boolean, 누락 조건, 비어 있지 않은 unseal 키를 차단합니다. 실제 localhost 상호 TLS를 포함한 서비스 회귀 9개와, 실제 CLI 초기화 JSON을 그대로 저장하는 로컬 Docker 실기 9개가 통과했습니다. 기존 초기화된 환경을 재초기화하지 않았습니다.

수정한 custody 이미지만 VM에서 재빌드·교체했습니다. 새 이미지 ID는 `sha256:ed8d3c1b878504253f27f272a059d2e17cfcd703b0322c67ad115ae6e5905f98`, 실제 실행 소스 SHA-256은 `8884570c8d09304c933f7cf16f233cea88b8c9dc16d9f59922f7616f0cb24f8d`이며 로컬 코드와 일치했습니다. 교체 후 custody와 중앙 Vault의 healthy 상태, 기존 암호문 ready 검증이 통과했습니다. 중앙 Vault를 다시 초기화하거나 초기 root를 되살리지 않았습니다. 후속 신규 대상 시험 결과는 별도 대상 보고서로 확인해야 합니다.

## 결과 범위와 운영 제약

일시적인 SSH 연결 장애 이후 동일 호스트키로 접속을 복구해 남은 중앙 시험을 완료했습니다. 대상 환경 작업이 멈춘 안전 구간을 확인한 뒤에만 중앙 재시작을 수행했습니다. Kubernetes 대상 Vault의 자동 unseal과 애플리케이션 주입은 별도 [대상 환경 시험 기록](ubuntu-vault-e2e-backend-20261004.md)에서 관리합니다. 해당 대상의 초기화 영수증 누락 진단 시, 중앙에서는 정확한 환경 ID로 비밀 없는 메타데이터만 조회하여 저장 기록이 없음을 확인했습니다. 중앙 시험 성공은 대상의 초기화 복구 성공을 뜻하지 않습니다.

감시는 실제 systemd 타이머와 로컬 journal 실패 신호까지 확인했습니다. 외부 메시지 발송이나 경보 수신자 연결은 수행하지 않았습니다. 현재 감시 코드의 인증서 점검은 중앙 CA의 7일 이상 잔여 기간이며, 모든 발급 leaf 인증서의 자동 갱신을 제공하지 않습니다. 백업 주기 실행과 원격 보관소로의 전송 자동화도 이번 시험 범위가 아닙니다.

중앙 Vault는 수동 unseal을 사용하는 구성입니다. 환경별 Vault가 중앙 Transit을 이용해 자동 unseal하는 것과 다릅니다. 하나의 시험 VM에 둘을 배치한 결과는 물리적인 장애 영역 분리나 고가용성 검증이 아닙니다.

## 증거와 정리

비밀 없는 로컬 중간 요약: `/private/tmp/railshot-vault-e2e-20261004/central-stage-summary.json`.
원격 로그: `/tmp/railshot-common-setup.log`, `/tmp/railshot-control-compose.log`, `/tmp/railshot-control-verify-resume.log`, `/tmp/railshot-control-isolated-resume.log`, `/tmp/railshot-control-monitor-test.log`, `/tmp/railshot-control-finalize.log`, `/tmp/railshot-control-post-root-issue.log`. 최종 검증과 감시, 실패 시도 로그의 비밀 없는 사본을 위 로컬 증거 디렉터리에 수집했습니다.
반복 감시 기록 `railshot-control-monitor-periodic.log`와 최종 보안 옵션·타이머·root 파일 부재 기록 `railshot-control-final-state.log`도 로컬에 수집했습니다.
PGP 실패 로그에는 성공 이전의 configure 준비 경쟁 조건이 남아 있어 최종 성공 증거로 단독 사용하지 않습니다.

VM과 임시 자원은 증거 수집 뒤 삭제·반납을 확인했습니다. 자원별 목록 근거와 재사용 자원 보존은 [자원 원장과 시험 상태](ubuntu-vault-e2e-resources-20261004.md)를 따릅니다. 합성 개인키와 원문 자격은 VM 밖으로 반출하지 않았습니다.

용어: PGP는 공개키 방식의 파일 암호화 도구 규약, 상호 TLS는 서버와 클라이언트가 서로 인증서를 검증하는 통신, UID는 운영체제 사용자 번호입니다. capability 제거는 컨테이너에 불필요한 운영체제 권한을 주지 않는 설정입니다.
