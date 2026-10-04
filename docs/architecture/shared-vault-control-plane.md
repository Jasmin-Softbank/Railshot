# 중앙 Transit Vault와 복구자료 보관 운영

2026-10-04 기준입니다. 이 문서는 구현된 명령과 운영 준비 절차를 설명합니다. **설치·초기화·발급·교체·폐기·복원 등 실제 운영 변경은 사용자의 명시적 승인 후에만 수행합니다.** 문서의 명령을 작성한 것은 실행 승인을 뜻하지 않습니다.

중앙 Vault는 환경별 자동 잠금 해제에 사용하는 Transit 키를 보관합니다. 별도 custody 서비스는 환경 Vault의 초기화·복구 자료와 전달 자격을 암호화해 보관합니다. 환경마다 앱 Vault, Transit 키·토큰, runtime/manager 인증서, Vault 서버 인증서를 분리합니다. 중앙 Vault를 모든 앱 비밀값의 직접 공급자로 바꾸지 않습니다. 복구 HTTP 계약은 [복구 API](../api/recovery.md), 대상 환경 설치·재개는 [Ansible 계약](../api/ansible.md)을 따릅니다.

## 검증된 버전과 범위

실제 로컬 Docker 시험에서 사용한 이미지는 다음과 같으며 [시험 코드](../../deployment/scripts/tests/live_control_vault.py)의 `VAULT_IMAGE`와 일치합니다.

```text
hashicorp/vault:2.1.1@sha256:47f14a6acb98f48d798a07df7c83f23a6e636e1cf724c5f8ff165cb32667a1e2
```

Vault Community 2.1.1의 호스트 실행 파일은 [공식 설치 안내](https://developer.hashicorp.com/vault/install)와 [공식 배포·체크섬·서명](https://releases.hashicorp.com/vault/2.1.1/)으로 확인합니다. 다른 버전·플랫폼으로 바꾸면 이미지와 실행 파일을 별도로 검증합니다.

최종 로컬 시험은 중앙 초기화·환경별 정책 분리, child Vault의 Transit 자동 잠금 해제와 재시작 후 데이터 유지, 실제 mTLS custody 저장·재시작·복원, 중앙 Raft snapshot 복원 후 기존 암호문 복호화, 이전 토큰을 유지하는 교체와 명시적 폐기, 초기 root 폐기 뒤 운영자·child 토큰 유지 및 갱신을 비롯하여 백업 묶음 생성·검증·복원 입력 준비까지 9개 결과를 통과했습니다. [실행 기록](../poc/control-vault-live-20261004.md)에 이미지와 검증 범위를 기록했습니다. **전체 Kubernetes·ESO·Provider·클라우드 배포와 실제 운영 복구 훈련은 검증하지 않았습니다.** 시험 코드에 검사가 있다는 사실과 그 검사의 실행 성공은 구분합니다.

## Ubuntu 호스트 설치와 실행 파일

관리 호스트에는 Python 3.11 이상, 가상환경 모듈, GnuPG, 공식 Vault CLI, Docker와 Compose 플러그인이 필요합니다. Ubuntu 24.04의 배포판 Python을 사용하는 예입니다. 다른 Ubuntu 버전에서는 Python이 3.11 이상인지 먼저 확인합니다.

```sh
sudo apt-get update
sudo apt-get install -y python3 python3-venv gnupg ca-certificates curl unzip
python3 -c 'import sys; assert sys.version_info >= (3, 11)'
sudo python3 -m venv /opt/railshot-control
sudo /opt/railshot-control/bin/python3 -m pip install -r deployment/scripts/requirements-recovery.txt
sudo /opt/railshot-control/bin/python3 -c 'import cryptography; assert cryptography.__version__ == "50.0.2"'
```

Vault CLI는 공식 2.1.1의 해당 CPU 아키텍처용 ZIP을 내려받아 공식 서명과 SHA-256 체크섬을 검증한 뒤 `/usr/local/bin/vault`에 root 소유 0755로 설치합니다. `vault version`으로 버전을 확인합니다. Docker/Compose는 호스트의 승인된 설치 절차로 준비합니다. 위 Python 설치는 Vault CLI나 Docker를 설치하지 않습니다.

다음 파일을 root 소유, 다른 사용자가 수정할 수 없는 `/usr/local/libexec/railshot/`에 설치합니다.

```text
control_vault.py                 control_backup.py
recovery_service.py              recovery_enroll.py
control-environment-provision.sh  control-vault-bootstrap.sh
control-backup.sh                 control-restore-harness.sh
control-monitor.sh
```

Python 파일은 root 0644, 실행용 shell 파일은 root 0755로 둘 수 있습니다. 가상환경과 모든 상위 디렉터리는 root가 관리하고 다른 사용자에게 쓰기 권한을 주지 않습니다. 중앙 shell 실행 래퍼는 **`/opt/railshot-control/bin/python3`**를 사용합니다. 시스템 Python만 설치하거나 `PATH`에 가상환경을 잠시 추가하는 것으로 대체하지 않습니다. `control-provision-helper.py`는 표준 라이브러리만 쓰며 별도 root 0755 `/usr/local/libexec/railshot-provision-environment`에 설치합니다. 설치 시 첫 줄을 `#!/usr/bin/python3`로 고정하여 sudo 실행이 사용자 `PATH`의 Python을 선택하지 않게 합니다.

## 중앙 서버 디렉터리·TLS·Compose

[Compose 구성](../../deployment/control-compose.yaml), [환경 파일 예시](../../deployment/control-vault.env.example), [Vault HCL 예시](../../deployment/control-vault.hcl.example)를 사용합니다. 비밀값과 실제 환경 파일은 Git 밖에 둡니다.

| 경로 역할 | 호스트 소유자·권한 | 비고 |
|---|---|---|
| 중앙 운영 설정·CA 개인키·토큰·custody keyring/registry | root 디렉터리 0700, 파일 0600 | CA 개인키는 대상 환경/API에 제공하지 않음 |
| 중앙 Vault 설정·서버 TLS bind 디렉터리 | UID 100, GID 1000, 디렉터리 0500, 파일 0400 | 컨테이너 `user: 100:1000`이 읽을 수 있어야 함 |
| 중앙 Vault 데이터 bind 디렉터리 | UID 100, GID 1000, 0700 | Raft·audit 쓰기 필요 |
| custody 데이터·비밀 bind 디렉터리 | root 0700, 파일 0600 | custody 프로세스는 컨테이너 root로 실행 |
| 환경 발급 원장 | root 0700, 파일 0600 | 제한 helper의 중앙 원본 |
| API용 환경 자격 출력 | 등록된 API UID/GID, 0700/0600 | helper가 권한 하향 후 기록 |

root 0600 파일을 그대로 UID 100 Vault 컨테이너에 bind하면 읽지 못합니다. **Vault 전용 서버 인증서·키·설정 복사본만** 위 UID/GID에 맞추고, 중앙 CA 개인키·custody keyring까지 소유권을 바꾸지 않습니다. host의 운영 도구가 읽는 CA 복사본은 별도 root 0600을 유지합니다.

중앙 Vault 설정의 핵심은 다음과 같습니다. 실제 승인된 사설 DNS와 인증서 SAN을 맞춥니다.

```hcl
ui = false
disable_mlock = true
api_addr = "https://control-vault.example.internal:8200"
cluster_addr = "https://control-vault.example.internal:8201"
listener "tcp" {
  address = "0.0.0.0:8200"
  cluster_address = "0.0.0.0:8201"
  tls_cert_file = "/vault/tls/server.crt"
  tls_key_file = "/vault/tls/server.key"
  tls_min_version = "tls12"
}
storage "raft" {
  path = "/vault/data"
  node_id = "railshot-control-1"
}
```

현재 Compose는 모든 Linux capability를 제거하고 읽기 전용 root filesystem을 사용하므로 `disable_mlock=true`를 지정합니다. 호스트 swap 사용 정책은 별도로 점검해야 합니다. Raft의 `cluster_addr`를 생략하지 않습니다. Compose 상태 검사는 `https://127.0.0.1:8200`을 사용하므로 서버 인증서에는 사설 DNS 외에 `127.0.0.1` IP SAN도 필요합니다.

기본 포트는 호스트 loopback의 Vault 18200, custody 19443입니다. 외부 target VM은 이 loopback 주소에 직접 접근할 수 없습니다. 실제 사설 DNS·방화벽·TLS 통과 경로를 별도 승인·설정한 후 `address`와 `recovery.endpoint`를 등록해야 합니다. 기본 Compose가 원격 접속 경로까지 제공한다고 간주하지 않습니다. `restart: unless-stopped`는 컨테이너 재시작 정책이며 중앙 Vault를 자동 unseal하지 않습니다.

## 중앙 초기화·수동 unseal·운영자 구성

중앙 Vault에는 외부 seal을 자동 구성하지 않으며 Shamir 5개 share 중 3개로 수동 unseal합니다. 서로 다른 5명의 PGP 공개키 파일 경로를 줄마다 적은 0600 파일과 root-token 수신용 PGP 공개키 파일을 준비합니다. [초기화 래퍼](../../deployment/scripts/control-vault-bootstrap.sh)는 공개키 소유·권한과 5개 fingerprint의 서로 다름을 검사합니다.

```sh
sudo /usr/local/libexec/railshot/control-vault-bootstrap.sh \
  https://control-vault.example.internal:8200 \
  /etc/railshot/control/central-ca.crt \
  /etc/railshot/control/pgp-recipients.txt \
  /etc/railshot/control/root-recipient.asc
```

출력은 Vault가 PGP로 암호화한 share와 root-token JSON입니다. 승인된 서버 밖 저장소에 보관하고, 운영 로그·Git에 남기지 않습니다. 각 운영자는 자기 개인키로 share를 복호화해 Vault CLI의 대화형 unseal 입력에 직접 넣습니다. 평문 share를 명령 인자나 한 서버의 영구 파일로 모으지 않습니다. 상태 조회 실패를 미초기화로 해석하지 않으며, 이미 초기화된 Vault에 `init`을 다시 호출하지 않습니다. 부팅·재기동 후 sealed이면 운영자가 다시 unseal해야 합니다.

중앙 도구의 root 0600 설정은 다음 형태입니다. CA의 인증서·개인키와 registry는 미리 준비하며, 초기화 단계에서는 `operator_token_file`만 임시 root token을 가리킵니다. 평문 root는 승인된 tmpfs 파일에서만 잠시 사용하고 보관하지 않습니다.

```json
{
  "version": 1,
  "address": "https://control-vault.example.internal:8200",
  "ca_file": "/etc/railshot/control/central-ca.crt",
  "operator_token_file": "/run/railshot-bootstrap/root-token",
  "recovery": {
    "endpoint": "https://custody.example.internal:9443/api/v1/escrows",
    "ca_file": "/etc/railshot/authorities/custody-ca.crt",
    "ca_key_file": "/etc/railshot/authorities/custody-ca.key",
    "registry_file": "/srv/railshot-control/custody-secrets/recovery-clients.json"
  },
  "vault_tls": {
    "ca_file": "/etc/railshot/authorities/child-ca.crt",
    "ca_key_file": "/etc/railshot/authorities/child-ca.key"
  }
}
```

```sh
sudo /opt/railshot-control/bin/python3 /usr/local/libexec/railshot/control_vault.py configure \
  --config /etc/railshot/control/provision.json \
  --operator-output /etc/railshot/control/operator-token
```

`configure`는 Transit mount, 원문 비밀값을 쓰지 않는 audit, 환경 seal 토큰 역할과 운영자 정책·토큰을 설정합니다. 출력 토큰 파일은 새 경로여야 합니다. 설정의 `operator_token_file`을 새 운영자 토큰으로 바꾼 뒤 실제 인증·갱신을 확인하고, 별도 승인된 Vault `token revoke -self` 절차로 임시 root를 폐기하고 tmpfs 파일을 제거합니다. 이 도구가 중앙 root를 자동 폐기하지는 않습니다.

**중앙 provisioner는 신뢰된 관리 권한입니다.** `sys/policies/acl/railshot-*` 정책 작성 권한을 가지므로 이름 prefix만 보고 작은 권한 범위의 비신뢰 실행기로 분류하면 안 됩니다. 반면 각 환경 seal 토큰은 자기 환경의 Transit encrypt/decrypt와 자기 token 조회·갱신만 허용합니다. 중앙 운영자·환경 seal 토큰은 부모 root 폐기와 별개로 유지되는 orphan periodic token입니다.

## 환경 발급과 제한 sudo helper

직접 운영자 호출은 다음과 같습니다. 출력 디렉터리의 부모는 root 0700이어야 합니다.

```sh
sudo /opt/railshot-control/bin/python3 /usr/local/libexec/railshot/control_vault.py provision \
  --config /etc/railshot/control/provision.json \
  --environment-id env-a \
  --output-dir /var/lib/railshot-control/environments/env-a
```

완료된 기존 환경 디렉터리는 identity와 파일을 확인해 재사용합니다. 완료 profile이 없는 부분 발급은 자동 덮어쓰기·재발급하지 않습니다. 출력은 환경별 `transit-profile.json`, `seal-env.json`, token accessor, runtime/manager mTLS 인증서·개인키, child Vault TLS, manager용 `delivery-recovery.json`입니다. 중앙 CA 개인키는 출력에 포함하지 않습니다.

제품의 trusted profile은 다음 항목으로 고정 helper를 호출합니다. 공개 요청에서 helper·경로·중앙 주소를 입력받지 않습니다.

```json
{
  "central_provisioning": {
    "helper": "/usr/local/libexec/railshot-provision-environment",
    "state_dir": "/var/lib/railshot-api/credentials"
  }
}
```

고정 root 0600 `/etc/railshot/control-provisioner.json` 예시는 다음과 같습니다. UID/GID 1000은 실제 서비스 사용자 값으로 바꿉니다.

```json
{
  "version": 1,
  "caller_uid": 1000,
  "caller_gid": 1000,
  "root_state_dir": "/var/lib/railshot-control/environments",
  "output_root": "/var/lib/railshot-api/credentials",
  "provision_config": "/etc/railshot/control/provision.json"
}
```

sudoers에는 이 helper만 허용하고 `visudo -cf`로 검사합니다.

```sudoers
railshot ALL=(root) NOPASSWD: /usr/local/libexec/railshot-provision-environment *
```

helper는 환경 ID 한 개 외의 인자를 거부하고 고정 설정·root 관리 경로를 검사합니다. 환경별 잠금 아래 원본 발급 자료를 읽은 다음 `setgroups([])`, `setgid`, `setuid`로 영구 권한 하향한 후 API 소유 디렉터리에 씁니다. 변경된 결과를 교체할 때 이전 디렉터리는 `retained` 이름으로 보존합니다. API 전체 root 실행이나 API 사용자에게 중앙 CA 개인키 읽기 권한을 주는 방식은 사용하지 않습니다. 기본 dashboard/API Compose에는 이 관리 호스트 설치가 자동 포함되지 않으므로 실제 실행 호스트에서 sudo와 고정 경로를 준비해야 합니다.

## 토큰·인증서 교체와 만료 복구

환경 seal 토큰은 24시간 periodic token이며 실행 중인 child Vault가 갱신합니다. child가 24시간 이상 꺼져 토큰이 만료되면 기존 파일 재사용만으로 재기동할 수 없습니다. 중앙 운영자가 새 디렉터리에 새 토큰을 발급합니다.

```sh
sudo /opt/railshot-control/bin/python3 /usr/local/libexec/railshot/control_vault.py rotate \
  --config /etc/railshot/control/provision.json --environment-id env-a \
  --output-dir /var/lib/railshot-control/rotations/env-a-20261004
```

이 명령은 새 `seal-env.json`과 accessor를 만들며 기존 token·Transit 키·profile을 자동 변경하지 않습니다. 운영자는 새 파일을 신뢰된 환경 profile/원장에 전환하고 helper 출력 갱신, 대상의 버전별 Secret 교체, Vault rollout·unseal·readback을 확인합니다. 중앙 `rotate` 성공만으로 대상 전환까지 완료됐다고 판단하지 않습니다. 실패하면 이전 참조를 보존합니다. 이전 토큰 폐기는 다음 별도 명령으로 수행합니다.

```sh
sudo /opt/railshot-control/bin/python3 /usr/local/libexec/railshot/control_vault.py revoke \
  --config /etc/railshot/control/provision.json --environment-id env-a \
  --accessor-file /var/lib/railshot-control/environments/env-a/seal-token-accessor.json
```

폐기는 accessor의 환경 ID와 실제 정책을 대조합니다. 이미 만료된 토큰의 조회·폐기가 실패할 수 있으며, 이를 새 토큰 준비 완료의 증거로 사용하지 않습니다. Transit 키와 과거 key version은 자동 삭제하지 않습니다.

runtime/manager 인증서는 기본 30일, child Vault 서버 인증서는 90일입니다. 자동 인증서 갱신·배포 스케줄러는 구현하지 않았습니다. `recovery_enroll.py issue --environment-id … --role both --ca-file … --ca-key-file … --registry … --output-dir <새 경로>`로 새 인증서를 발급한 뒤 trusted profile과 target/manager 참조를 전환·검증해야 합니다. 이전 fingerprint는 `recovery_enroll.py revoke --environment-id … --role … --fingerprint … --registry …`로 명시 폐기합니다. child TLS는 운영자 CA 절차 또는 새 발급 디렉터리에서 준비하여 버전별 Secret으로 전환합니다. 같은 완료 디렉터리에 `provision`을 다시 호출하는 것은 인증서 갱신이 아닙니다.

앱 설정 전달 AppRole은 24시간·1,000회 제한입니다. 만료·교체는 [관리자 전용 전달 자격 교체](../api/ansible.md)의 `--rotate-credentials` 경로를 사용합니다. 기존 AppRole이 만료되어도 등록된 관리 접속과 전용 Kubernetes issuer 역할로 재발급할 수 있습니다. 공개 제품 사용자가 이 관리 권한을 행사할 수는 없습니다.

## 운영자 토큰 갱신과 감시

중앙 운영자 토큰도 24시간 periodic token입니다. [service](../../deployment/control-monitor.service)와 [timer](../../deployment/control-monitor.timer)는 root로 약 5분마다 실행합니다. `/etc/railshot/control-monitor.env`는 root 0600이며 다음 절대 경로를 지정합니다.

```text
CONTROL_PROVISION_CONFIG=/etc/railshot/control/provision.json
CONTROL_CUSTODY_DATA_DIR=/srv/railshot-control/custody-data
CONTROL_CUSTODY_KEYRING=/srv/railshot-control/custody-secrets/recovery-keyring.json
CONTROL_LATEST_BACKUP_BUNDLE=/srv/railshot-control/backups/latest-verified
```

단위 파일을 `/etc/systemd/system/`에 설치한 뒤 승인 후 `systemctl daemon-reload`, `systemctl enable --now control-monitor.timer`를 실행하고 journal과 첫 실행 결과를 확인합니다. unit의 `ReadWritePaths` 기본값은 예시와 같은 `/srv/railshot-control/custody-data`이므로 실제 custody 데이터 경로를 다른 곳에 두면 root 소유 override로 맞춰야 합니다. 경로를 변경할 때 unit의 쓰기 허용 경로도 함께 바꿉니다.

실제 감시는 중앙 Vault initialized/unsealed 상태, 운영자 token 갱신, 지정 CA의 7일 이상 잔여 유효기간, custody 암호문 데이터 준비 검사, custody 디스크 사용률 85% 미만, 최신 백업 manifest의 24시간 이내 시각을 확인합니다. **모든 환경 token·leaf 인증서 만료, 현재 audit 설정, 원격 네트워크, 백업의 서버 밖 도착, 실제 복원 가능성을 전수 감시하지 않습니다.** 백업 manifest 시각 확인도 전체 복원 훈련을 대신하지 않습니다.

실패 신호는 비정상 종료와 systemd journal입니다. 외부 메신저·이메일·호출 경보는 연결하지 않았으므로 운영자가 별도로 연결해야 합니다. 중앙이 24시간 이상 내려가 운영자 token까지 만료되면 monitor는 만료 token을 되살리지 못합니다. 승인된 중앙 관리자 인증 또는 보관 중인 Shamir quorum을 사용하는 Vault 운영자 root 재생성 절차로 새 운영자 token을 발급하고, 설정 전환·갱신 확인 뒤 임시 root를 폐기해야 합니다. 이 root 재생성·운영자 복구는 자동화하지 않았습니다.

## 백업·키 보존·복원 훈련

세 종류의 키를 구분합니다: 중앙 Vault를 여는 Shamir share, 중앙 Transit의 환경별 키, custody 저장 암호문을 여는 keyring입니다. 하나를 보관했다고 나머지 손실이 복구되지는 않습니다. custody HTTP 재개 envelope에는 초기 root token만 포함되고 복구 share는 포함하지 않습니다. 중앙 로컬 export는 별도 승인된 운영자 작업입니다.

백업 입력은 실제 Vault Raft snapshot과 custody `backup` 명령이 만든 SQLite 백업입니다. 실행 중 SQLite 파일을 단순 복사하지 않습니다. config-reference JSON은 `{"version":1,"vault_version":"2.1.1","transit_key_ids":["railshot-env-a"]}` 형태의 메타데이터만 담습니다.

```sh
sudo /opt/railshot-control/bin/python3 /usr/local/libexec/railshot/recovery_service.py \
  --data-dir /srv/railshot-control/custody-data \
  --keyring /srv/railshot-control/custody-secrets/recovery-keyring.json \
  backup --output /srv/railshot-control/backups/custody-20261004.sqlite3

sudo /usr/local/libexec/railshot/control-backup.sh \
  /srv/railshot-control/backups/vault-20261004.snapshot \
  /srv/railshot-control/backups/custody-20261004.sqlite3 \
  /srv/railshot-control/custody-secrets/recovery-keyring.json \
  /srv/railshot-control/backups/config-reference.json \
  /srv/railshot-control/backups/bundle-20261004
```

snapshot은 승인된 Vault 운영자 인증으로 `vault operator raft snapshot save`를 실행해 얻습니다. `control-backup.sh`가 snapshot을 자동 획득하지는 않습니다. 묶음은 snapshot 내부 항목·체크섬, custody schema·각 암호문 복호화, 파일 해시와 사용 중인 모든 custody `key_id`를 검사합니다. keyring 자체와 CA 개인키·Shamir share·평문 token은 묶음에 넣지 않습니다.

완성된 묶음은 별도 서버·장애 영역으로 복사하고 도착·보존을 확인합니다. 이 서버 밖 전송은 구현하지 않았습니다. 별도 보안 경로에 **모든 과거 백업이 참조하는 custody key ID의 키**, 중앙 Shamir PGP 개인키·암호화 share, CA 개인키·인증서, 인증서 registry·운영 설정을 함께 보존합니다. keyring 회전 후 현재 DB가 새 키만 사용해도 과거 백업용 키를 임의 삭제하지 않습니다. 제품 프로젝트 원본의 `RAILSHOT_PROJECT_KEY_FILE`은 또 다른 키링이며 별도 백업 대상입니다.

```sh
sudo /usr/local/libexec/railshot/control-restore-harness.sh \
  /srv/railshot-control/backups/bundle-20261004 \
  /etc/railshot/offline-restore/recovery-keyring.json \
  /srv/railshot-control/restore-drill/new-inputs
```

복원 harness는 **새 디렉터리**에 입력을 검증·준비할 뿐 운영 Vault를 덮어쓰거나 unseal하지 않습니다. 격리된 새 Vault에 snapshot restore, 원래 Shamir quorum으로 unseal, 기존 Transit 암호문 복호화, custody 복호화·인증서 정책 확인까지 별도로 수행해야 복원 훈련입니다. 중앙 Vault·custody가 중단돼도 이미 unsealed인 child가 반드시 즉시 멈추는 것은 아니지만, 재시작·자동 unseal·키 관련 작업과 custody 의존 전달은 영향을 받을 수 있습니다. 중앙 가용성과 서버 밖 복구자료는 별도의 운영 책임입니다.
