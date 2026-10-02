# 고객 OpenStack 설치 프로그램

## 범위와 실행 준비

2026-10-03 결정에 따라 WireGuard 등록·키 생성·터널 설정은 제거했습니다. 이 프로그램은 현장 노드의 로컬 OpenStack 준비와 조회만 수행합니다. 운영자가 Keystone 및 VM 관리 주소에 도달할 경로를 미리 확보해야 합니다. Cloudflare 앱 Named Tunnel은 SSH/Kubernetes API 관리 경로를 제공하지 않습니다.

Ubuntu 24.04, root, Python 3.12를 초기 지원 대상으로 합니다. OpenStack 명령 실행은 고객 노드에서만 수행하며 상시 작업 수신 에이전트와 서버 구현은 포함하지 않습니다. 기존 PostgreSQL 구축 기능은 별개입니다. 실제 고객 환경 연결은 아직 검증하지 않았습니다.

배포 패키지에는 저장소의 `deployment/bootstrap/`와 `infrastructure/providers/openstack/`를 같은 상대 위치로 포함해야 합니다. 단일 install.sh만 다운로드하면 실행되지 않습니다. 운영 배포 URL과 서명 배포 체계는 제공되지 않았으므로 `curl | bash` 형태의 공개 설치 명령은 아직 제공하지 않습니다. 관리자는 검증한 배포 패키지를 고객 노드에 풀어 다음처럼 한 줄로 실행할 수 있습니다.

```sh
sudo bash deployment/bootstrap/install.sh --install-dependencies init --config /etc/jasmin-install/config.json
```

명령과 초기화 설정 검증은 패키지·배포본·가상환경 설치보다 먼저 수행합니다. 폐기한 등록 설정이나 CLI 옵션은 이 단계에서 거절합니다. 의존성 설치 옵션은 패키지 관리자와 Python 패키지 저장소에 접근하며 운영체제 패키지를 설치합니다. 이미 준비된 노드는 해당 옵션을 생략합니다. 외부 패키지 저장소 접근과 신뢰 검증은 고객 정책에 맞게 구성하십시오.

설정 디렉터리는 root 소유 0700, JSON 파일은 root 소유 0600으로 준비하십시오. 비밀번호를 JSON에 넣지 마십시오. 입력은 터미널에서 숨겨 받습니다. 아래는 형식 예제이며 주소와 식별자를 실제 환경 값으로 바꿔야 합니다.

```json
{
  "openstack": {
    "auth_url": "https://keystone.example.org/v3",
    "user_domain_name": "Default",
    "project_id": "PROJECT_UUID",
    "role_id": "ROLE_UUID",
    "interface": "internal"
  },
  "vm_access": {
    "network_id": "NETWORK_UUID",
    "ssh_source_cidr": "192.0.2.10/32",
    "ssh_username": "ubuntu",
    "resource_prefix": "jasmin"
  }
}
```

기본 설치는 필요한 소스와 전용 Python 환경을 `/opt/jasmin/bootstrap/`에 배치합니다. 이후 진입점은 `/opt/jasmin/bootstrap/deployment/bootstrap/install.sh`입니다. 기존 설치의 파일 해시가 다르면 자동 덮어쓰지 않습니다. 개발용 `--source-run`은 소스 디렉터리에서 직접 실행합니다.

관리자 ID와 전용 계정 이름은 설정에서 생략하면 입력받습니다. 관리자 암호는 별도 입력받습니다. 서비스 등록 키와 `--enrollment-token-file`은 지원하지 않으며, 기존 `RAILSHOT_ENROLLMENT_TOKEN`·`JASMIN_ENROLLMENT_TOKEN` 환경변수는 하위 프로세스 실행 전에 버립니다. `service_url` 또는 `wireguard*` 설정이 있으면 실행을 거절합니다. 기존 설치의 상태·자격증명은 보존하고 `diagnose` 또는 해제 절차로 관리하십시오. 새로운 로컬 설치는 별도 설정·상태 경로를 사용합니다.

## 실행 흐름

`main.initialize` → 사전 점검 → OpenStack 인증/자격증명 저장 → 실제 조회 → 서비스 분석 → 가상 머신 접근 설정 → 보고서입니다. 각 모듈은 import만으로 시스템을 변경하지 않습니다.

프로젝트 등 전체 설정의 해시를 상태에 고정합니다. 재실행 시 다른 설정으로 기존 자격증명을 재사용하지 않습니다. 변경이 필요하면 기존 등록과 자원을 확인하고 별도 설치/이관 절차를 수행하십시오. 이 프로그램은 관리망 경로를 만들거나 실제 VM 접속을 자동으로 증명하지 않습니다.

## 고객 노드 파일

- `/etc/jasmin/`: 로컬 암호화 키·자격증명. root 0700 디렉터리와 0600 파일.
- `/var/lib/jasmin/bootstrap/`: 진행 상태, 이번 설치의 자원 식별자, 원격접속 프로필, 결과 보고서.

파일 기록은 임시 파일 동기화 후 원자적 교체하며, 개인 파일의 심볼릭 링크·부적절한 소유권·권한을 거부합니다. 같은 설치 상태 경로의 동시 실행은 잠금으로 차단합니다. 암호·토큰·개인키를 상태에 기록하지 않습니다. 오류 원문도 출력하지 않습니다.

## 진단과 복구

`install.sh diagnose`는 저장된 고객 인증정보로 조회만 수행합니다. 상태의 running/failed 단계를 확인하십시오.

| 실패 단계 | 확인 및 복구 |
|---|---|
| configuration | 설정 소유권·0700/0600 권한·JSON 형식과 기존 설정 일치 여부 확인 |
| preflight | Ubuntu 버전, root 권한, 필요한 명령과 외부 통신 확인 |
| identity | 관리자 권한, 도메인/프로젝트/역할 확인. 부분 생성 자원 식별자를 점검하고 기존 계정 자동 인수 금지 |
| discovery | OpenStack 서비스 주소·접근 권한·할당량 확인 |
| vm_access_preparation | 관리망 경로, 접속 키, 보안그룹 생성 권한 확인 |

임의로 상태를 삭제하면 기존 자원 소유권을 잃을 수 있습니다. 인증 실패 후 관리자 암호를 재입력해야 할 수 있으며, 자격증명이 안전하게 저장되기 전 발생한 중단을 완전 자동 복구한다고 보장하지 않습니다.

가상 머신은 자동 생성하지 않습니다. 생성 시 `vm-access.json`의 설정을 적용한 후, 접근 프로필과 신뢰할 수 있는 경로로 확보한 SSH 호스트 키 파일을 준비해 `verify-vm --profile PATH --known-hosts PATH`로 확인합니다. 프로필 규격은 OpenStack access 모듈을 참조하십시오. 기반 준비는 실제 접속 성공을 의미하지 않습니다.

## 제거

`uninstall.sh`는 과거 설치의 `installation.json`이 가리키는 설치 소유 WireGuard 연결만 해제합니다. 새 로컬 설치에는 터널이 없으므로 이 단계는 건너뜁니다. 소유권 표식·파일 권한·경로를 검사하며 해제 실패 시 설정 파일을 보존합니다. 새 WireGuard 연결을 만들거나 재활성화하는 기능은 없습니다. OpenStack 계정·자격증명·키·보안그룹과 서비스 등록은 자동 삭제하지 않습니다. 로컬 자격증명과 자원 기록도 원격 폐기에 필요하므로 보존합니다. `/opt/jasmin/bootstrap`의 프로그램과 전용 Python 환경도 보존됩니다. 출력에 표시된 경로를 확인하고 서버 등록 및 OpenStack 자격증명을 폐기한 뒤 로컬 `/etc/jasmin`과 `/var/lib/jasmin/bootstrap` 및 배포 패키지를 관리자가 제거하십시오. 이것은 완전 자동 제거 기능이 아니며, 디스크의 안전 소거를 보장하지 않습니다.

약어: HTTPS는 암호화 웹 통신, SSH는 암호화 원격접속입니다.

## 가상 머신 접속 검증 예제

생성된 `vm-access.json`의 키 경로와 사용자 이름에 가상 머신의 실제 관리 주소를 추가하여 root 전용 경로 `/etc/jasmin/vm-test.json`에 다음 형식으로 저장합니다.

```json
{
  "host": "192.0.2.20",
  "private_key_path": "/var/lib/jasmin/bootstrap/ssh/id_ed25519",
  "ssh_username": "ubuntu"
}
```

SSH 호스트 공개키는 OpenStack 콘솔 등 별도로 신뢰할 수 있는 경로로 확인하고 `/etc/jasmin/known_hosts`에 `192.0.2.20 ssh-ed25519 ...` 형식으로 넣습니다. 네트워크에서 수집한 키를 검증 없이 신뢰하지 마십시오. 두 파일은 root 소유 0600이어야 합니다.

```sh
sudo chmod 600 /etc/jasmin/vm-test.json /etc/jasmin/known_hosts
sudo bash deployment/bootstrap/install.sh --source-run verify-vm --profile /etc/jasmin/vm-test.json --known-hosts /etc/jasmin/known_hosts
```

부분 인증 실패 시 상태에 기록된 계정 ID·이름·도메인을 대조한 뒤 관리자가 이번 설치의 자격증명을 폐기하고 필요하면 신규 전용 계정 및 역할 부여를 정리해야 합니다. 기록 없이 기존 계정의 암호를 재설정하거나 자동 인수하지 않습니다.

## 작업 요청 에이전트 확장

사용자가 승인한 후속 범위로 `apps/agent/`에 SSH 작업 요청 처리를 추가합니다. 두 번째 시험 노드가 정해진 JSON 요청을 전송하고 첫 번째 시험 노드가 로컬 자격증명으로 `openstack server list`를 실행하여 제한된 결과를 반환하는 구조입니다. 별도로 확보한 관리 경로와 검증된 SSH 호스트 키가 필요하며, 기존 설치에 자동으로 작업 접근 권한을 부여했다고 간주하지 않습니다.

현재 작업은 `instance.list` 조회 하나로 제한합니다. 키별 강제 실행 명령과 `restrict`, 원격 명령 이름 검사, 입력 크기·시간 제한, 결과 필드 허용 목록을 적용합니다. 구체 규격과 재전송 한계는 [작업 요청 규격](../api/job-contract.md)을 확인하십시오. 임의 명령 실행이나 인프라 변경 작업은 지원하지 않습니다.

두 노드에 작업 전용 키를 안전하게 배치하고 기존 관리 접속을 보존하는 실행 절차는 [실환경 작업 요청 실행 절차](../poc/agent-live-runbook.md)에 있습니다. 이 문서는 실행 전 검토용이며 실제 수행 결과는 실환경 기록과 구분합니다.
