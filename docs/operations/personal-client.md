# 개인 OpenStack 제어 연결

목표는 **등록 스크립트 한 번으로 OpenStack 제어 연결과 별도 가상 머신의 앱 실행환경을 준비하고, 서버가 클러스터 접속·전용 인증·배포 권한까지 검증한 상태**입니다. CLI는 명령줄 실행 도구입니다. 관리 호스트가 OpenStack 가상 머신일 필요는 없으며, 관리 호스트에는 K3s를 설치하지 않습니다. 테스트 앱도 배포하지 않습니다.

## 선행조건과 설치

고객 관리 호스트는 Ubuntu 24.04 또는 26.04이며 sudo/root 설치 권한, OpenStack 관리자 인증정보, 서버 HTTPS·WireGuard UDP 연결이 필요합니다. 운영자는 RailShot API 서버와 게이트웨이 설정 및 고정 배포본을 먼저 준비합니다. 설치기는 가장 먼저 `GET /api/v1/readiness?scope=personal`을 확인하며, 중앙 배포·CI(지속적 통합)·운영 선행조건이 준비되지 않았으면 운영체제 패키지 설치나 OpenStack 변경 전에 중단합니다.

설치기는 실행 사용자의 안전한 로컬 설정과 환경변수에서 Keystone v3 인증 주소를 찾습니다. 후보가 없거나 여러 개면 주소를 확인받고, 관리자 ID·도메인·인증 프로젝트와 암호를 입력받습니다. 관리자 암호는 저장하지 않습니다. 이름이 `railshot`인 프로젝트를 조회해 재사용하거나 없으면 만들고, 전용 서비스 사용자·제한된 프로젝트 역할·응용 자격 증명을 생성합니다. 응용 자격 증명 ID나 비밀값을 사용자에게 입력받지 않습니다. 프로젝트와 생성 자원의 소유 기록은 로컬에 남으며, 서버에는 인증정보를 보내지 않습니다. 기존 암호화 저장소가 있으면 실제 토큰 프로젝트와 `server list` 권한을 다시 검증하고, 소유 기록이 없는 기존 자격 증명을 새로 만든 자원으로 간주하지 않습니다.

운영자는 검토한 리비전에서 다음 명령으로 재현 가능한 설치 배포본을 만듭니다. 첫 명령은 단일 압축파일 점검용이고, 두 번째 명령은 운영 대시보드 이미지가 게시하는 고정 해시 디렉터리와 manifest를 만듭니다. 출력은 기존 경로를 덮어쓰지 않습니다.

```bash
python3 deployment/scripts/package-personal-client.py --output /tmp/railshot-personal.tgz
python3 deployment/scripts/package-personal-release.py --output /tmp/railshot-personal-release
```

운영 대시보드 이미지는 `/personal/manifest.json`과 `/personal/<artifact-sha256>/{install.sh,personal-client.tgz}`를 함께 제공합니다. 서버 설정의 두 URL과 SHA256은 이 manifest와 일치해야 하며 배포 사전검사가 실제 HTTPS 응답과 바이트 해시를 다시 확인합니다. 설치 압축파일에는 기존 설치·진단·VM 검증·제거 명령과 개인 등록 모듈을 함께 넣으며, 심볼릭 링크·특수파일·상위 경로를 허용하지 않습니다. 화면의 설치 명령은 스크립트 다운로드와 다음 실행을 한 번에 제공합니다.

```bash
sudo bash install.sh --personal-registration \
  --api-url https://railshot.example.com \
  --enrollment-id ENROLLMENT_ID \
  --artifact-url https://releases.example.com/REVISION/personal.tar.gz \
  --artifact-sha256 VERIFIED_SHA256
```

일회용 토큰은 숨김 입력 또는 `RAILSHOT_ENROLLMENT_TOKEN` 환경변수로 전달합니다. 이 환경변수는 명시적인 `--personal-registration` 모드에서만 내부 등록 프로세스로 전달되며 기존 `init`, `diagnose`, `verify-vm`, `uninstall` 흐름에서는 즉시 제거됩니다. OpenStack 비밀값과 등록 토큰을 명령 인자·로그에 넣지 않습니다. `--project-id`는 발견한 `railshot` 프로젝트와 일치하는지 추가로 고정할 때만 사용하고, `--profile-id`는 서버가 발급한 경우에만 전달합니다. 기존 `personal-install.sh`는 같은 canonical 설치기로 위임하는 호환 진입점입니다.

이전 설치 시도가 `/opt/railshot/personal`에 검증 가능한 소스 표식만 남기고 등록 설정을 만들지 못한 경우, 새 설치기는 이를 임의로 덮어쓰거나 인수하지 않습니다. 운영자는 당시 배포본과 표식, 실행 중인 프로세스, `/etc/railshot-personal/client.json` 부재를 대조한 뒤 별도의 수동 복구 절차로 정리해야 합니다. 실제 서버 선행조건이 준비되지 않은 상태에서 재설치를 강행하지 않습니다.

OpenStack 연결 뒤에는 현재 프로젝트의 기존 VM 선택 또는 새 VM 생성을 스크립트 안에서 선택합니다. 기존 VM은 관리 네트워크 주소, sudo 가능한 비 root SSH 사용자, 개인키, 독립적으로 검증한 known_hosts 파일이 필요합니다. SSH 파일은 root 또는 sudo를 실행한 사용자 소유의 안전한 일반 파일만 받으며, root 전용 저장소에 복사하므로 사용자가 원본 소유권을 바꿀 필요가 없습니다. 개인키는 외부 서버로 전송하지 않습니다. 관리 호스트의 주소·머신 식별자 또는 DevStack 관리 경로가 확인되면 실행 대상에서 거절합니다.

새 VM은 Ubuntu 22.04/24.04 amd64 이미지, 2 CPU·2 GB 이상 메모리·15 GB 이상 디스크의 크기, 관리 네트워크, VM 이름, 접속 사용자, 관리 호스트의 `/32` 접속 주소를 선택합니다. 새 SSH 키와 해당 관리 주소에서 22·6443번 포트로만 들어오는 전용 보안 그룹을 만들고, cloud-init에는 SSH 계정·키 설정과 호스트 공개키 확인 표식만 넣습니다. 서버 ID와 프로젝트를 전후 대조한 OpenStack 콘솔의 표식으로 호스트 키를 고정합니다. 콘솔을 읽을 권한이 없거나 표식을 확인하지 못하면 진행을 중단하며 검증 없는 접속으로 우회하지 않습니다. 생성 결과가 불확실하면 같은 자원을 자동으로 다시 만들지 않습니다. 이 흐름에서 만든 VM·키페어·보안 그룹은 클라이언트 제거만으로 삭제하지 않습니다.

정상 K3s가 이미 있으면 읽기 전용 상태 검사 후 재사용합니다. K3s 흔적이 있으나 비정상이면 재설치하지 않고 확인 필요 상태로 남깁니다. K3s가 없는 별도 VM만 기존 `infrastructure/ansible/run.py`의 `runtime.install` → `runtime.yml` 경로로 준비합니다. 기존 게스트 검사, K3s·Cilium 설치, 상태 검증과 중복 실행 방지를 그대로 사용합니다. Ansible 의존성 설치는 일회용 등록이 완료된 뒤에 실행합니다. 초기 운영체제 패키지 설치가 오래 걸려 토큰이 만료되면 등록 상태를 확인하고 새 토큰으로 재실행하며, 유효기간을 자동으로 늘리지 않습니다.

반복 가능한 비대화형 입력이 필요하면 `--runtime-config /root/runtime-plan.json`을 사용할 수 있습니다. 이 파일은 root 소유 0600이며 부모 디렉터리는 0700이어야 합니다. 기존 VM 입력은 `mode: "existing"`, `resource_id`, `ssh_user`, `identity_file`, `known_hosts_file`이며, 주소가 여러 개면 `management_network`를 지정합니다. 새 VM 입력은 `mode: "create"`, `image_id`, `flavor_id`, `network_id`, `name`, `ssh_user`, `ssh_source_cidr`입니다. VM 접속 정보와 선택은 고객 호스트의 비공개 파일로 고정하며 재실행 시 다른 VM으로 임의 변경하지 않습니다.

Ubuntu 26.04의 Python 3.14 환경도 지원합니다. 설치기는 배포판의 `python3-openstackclient`를 설치하고, RailShot의 암호화 라이브러리는 `/opt/railshot/personal/.venv`에 따로 설치합니다. 시스템 Python이나 기존 DevStack 가상환경에 `pip`로 패키지를 넣지 않습니다. 패키지 설치에는 `--no-upgrade`를 적용해 이미 설치된 패키지의 갱신을 피합니다. 전용 OpenStack 처리기는 `/usr/bin:/bin` 경로만 사용합니다.

승인된 시험에서 HTTP 인증 주소나 HTTP RailShot 서버를 사용해야 할 때만 설치 명령에 `--test-allow-http`를 추가합니다. 사설 IP·localhost 주소도 이 옵션 없이는 HTTP를 허용하지 않습니다. 이 옵션은 배포본 다운로드, RailShot 등록·상태 보고·제거 완료 보고, 입력하는 OpenStack 인증 주소에 적용되며 `/etc/railshot-personal/client.json`의 `test_allow_http: true`로 보관해 서비스 재시작 뒤에도 유지합니다. 재설치에도 같은 옵션을 전달해야 합니다. HTTPS 인증서 검증과 리디렉션·주소 내 인증정보·질의문자열 금지는 그대로 유지합니다.

서버가 HTTP 설치 명령을 발급하려면 **API 서버 프로세스**에 `RAILSHOT_PERSONAL_TEST_ALLOW_HTTP=1`을 설정해야 합니다. 이 서버 환경변수는 클라이언트 설정과 다르며, 설치 명령의 `--test-allow-http`가 고객 호스트에 시험 설정을 전달합니다. 기본값은 양쪽 모두 HTTPS입니다.

전용 계정을 만드는 `useradd`와 암호 로그인을 비활성화하는 `usermod`는 느린 계정 데이터베이스 기록을 고려해 각각 최대 180초를 기다립니다. 시간 초과 뒤 계정만 있고 `/etc/railshot-personal/cli-account.json` 소유 기록이 없으면 재설치는 그 계정을 인수하지 않고 거절합니다. 운영자는 설치 전 상태·시스템 계정 생성 기록·잔여 프로세스·공유 사용자 여부를 대조해야 하며, 이름·홈 경로가 같다는 이유만으로 기존 계정을 삭제하거나 소유 기록을 만들어서는 안 됩니다. 이번 설치에서 생성한 전용 계정임이 입증된 경우에만 승인된 복구 절차에 따라 계정을 정리하고 재시도합니다. 저장된 `client.json`, 자격증명과 터널은 이 계정 복구를 위해 지울 필요가 없습니다.

설치기는 로컬 WireGuard 키와 전용 SSH 호스트 키를 만들고 공개키만 등록합니다. 서버 접속 자격을 원자적으로 저장한 뒤 운영체제 설정을 적용합니다. 같은 명령을 재실행하면 저장된 등록을 재사용합니다. 서버 등록 응답이 저장 전에 유실되면 자동으로 토큰을 다시 소비하지 않으며 운영자의 상태 대조가 필요합니다.

스크립트는 로컬 서비스 시작만으로 성공을 출력하지 않습니다. 먼저 최대 180초 동안 **서버가 터널을 통해 실제 `openstack server list`를 실행하여 `connection_status: ready`를 확인**합니다. 이어 별도 VM과 전용 관리 채널을 준비하고, 서버의 실행환경 등록·실제 권한 검증이 끝나 `status: ready`, `deployable: true`가 모두 확인되어야 완료합니다. 중앙 배포 설정이나 레지스트리 인증 등 선행조건이 없으면 차단 이유를 남기며 완료로 표시하지 않습니다. 시간 초과·불확실한 결과는 현장 상태를 보존하고 자동 재실행하지 않습니다.

## 권한과 통신 경계

고객 WireGuard 인터페이스는 `railshot0`입니다. 고객은 서버의 `/32` 주소만 경로로 등록하고 전체 고객 사설망이나 기본 경로를 광고하지 않습니다. 서버는 고객마다 별도 `/32`를 배정하고 게이트웨이 인터페이스를 통과하는 전달 트래픽을 양방향 차단합니다.

`railshot-personal-runtime`라는 기존 내부 이름의 전용 SSH 서비스는 WireGuard 고객 주소의 2222번 포트에서만 실행됩니다. 이름과 달리 Kubernetes 실행환경을 준비하지 않습니다. root 로그인·암호 인증·임의 셸·대화형 터미널·포트 전달을 금지하고, 서버가 가진 키와 고객이 등록한 SSH 호스트 키를 서로 검증합니다.

OpenStack 명령은 sudo 권한이 없는 `railshot-openstack` 시스템 계정으로 실행합니다. 이 계정은 `/var/lib/railshot-personal-cli`의 별도 암호화 저장소와 작업 기록만 사용합니다. 루트 관리 클라이언트의 `/etc/railshot-personal` 접속 토큰·WireGuard 개인키를 읽을 수 없습니다. 패키지 설치·터널 관리·클라이언트 제거는 root 서비스가 담당하고, 원격 CLI 처리기는 root 실행 자체를 거절합니다. 기존 SSH 설정·사용자 키·공유 계정은 변경하지 않습니다.

## 다른 서버 기능에서 호출하는 규격

서버 내부 `personalAdapter.execute(target, {job_id, argv, delete_data?, public_key?})`가 다음 요청을 전용 SSH 명령 `railshot-openstack-v1`의 표준입력으로 보냅니다. 브라우저에 임의 CLI 실행 경로를 제공하지 않습니다.

```json
{
  "version": 1,
  "job_id": "provision-server-001",
  "action": "openstack.execute",
  "params": {
    "argv": ["server", "create", "--image", "IMAGE_ID", "--flavor", "small", "web"]
  }
}
```

응답 형식은 `{version, job_id, action, ok, result, error}`입니다. `error.code=execution_unknown`은 변경 요청이 적용되었을 가능성이 있으므로 재실행하지 말고 자원을 대조해야 한다는 뜻입니다. 다른 요청에 같은 작업 ID를 재사용하면 거절합니다. 성공한 변경을 같은 ID로 재요청하면 저장한 결과를 반환합니다. CLI 인증정보·토큰·개인키를 결과나 작업 기록에 저장하지 않습니다.

허용 명령은 다음 범위입니다. 선택 옵션은 `apps/agent/openstack_control.py`의 고정 표가 최종 규격이며, 생성 옵션은 리소스 이름 또는 ID 앞에 둡니다.

| 종류 | 명령 |
| --- | --- |
| 조회 | `server`, `volume`, `network`, `subnet`, `router`, `port`, `floating ip`, `security group`, `security group rule`, `image`, `flavor`, `keypair`의 `list`·`show` |
| 추가 조회 | `availability zone list`, `limits show`, `quota show` |
| 생성 | `server`, `volume`, `network`, `subnet`, `router`, `port`, `floating ip`, `security group`, `security group rule`, `keypair`의 `create` |
| 삭제 | 위 생성 지원 종류의 `delete`; 이 제어 경로가 생성하고 로컬 소유 기록에 남긴 ID 또는 키 이름만 허용 |

`limits show` 요청은 처리기 내부에서 `limits show --absolute`로 실행합니다. 외부 요청에 임의 조회 옵션을 허용하지 않습니다.

서버·볼륨 삭제에는 요청의 `delete_data:true`가 추가로 필요합니다. 키페어 생성에는 `public_key`로 검증된 Ed25519 공개키를 전달해야 하며 개인키 생성은 지원하지 않습니다. 같은 키 이름으로 외부에서 다시 만든 키는 생성 당시 공개키·지문과 다르므로 삭제하지 않습니다.

`--os-*`, `--project`, `--all-projects`, `--debug`, `--insecure`, 외부 파일·사용자 데이터 입력, 인증·서버 주소·출력 형식 변경, 임의 플러그인은 거절합니다. 네트워크 계열 목록에는 처리기가 등록 프로젝트 필터를 직접 붙입니다. 관리자 역할이 있는 프로젝트 자격증명도 다른 프로젝트의 포트·보안 그룹을 변경하지 못하도록 생성 시 참조 자원을 조회해 프로젝트를 대조합니다. 공개/공유 이미지·네트워크 조회 및 외부 유동 IP 풀 사용은 필요한 예외이며, 다른 프로젝트의 자원 삭제 예외는 없습니다.

명령 실행은 실제 `OpenStackCLI`를 재사용합니다. 인증정보는 로컬 0600 임시 설정 파일에 쓰고 셸 없이 인자 배열로 실행합니다. 모든 명령 전에 내부적으로 토큰의 프로젝트를 확인하되 그 토큰은 반환하지 않습니다. 변경 의도를 디스크에 먼저 기록하므로 시간 초과·프로세스 중단 뒤 같은 변경을 자동 재실행하지 않습니다.

이 규격은 OpenStack 전체 CLI를 대체하지 않습니다. 예를 들어 역할/계정 관리, 라우터 인터페이스 연결, 임의 서버 재설정, 파일 업로드, 외부에서 만든 자원 삭제는 현재 허용하지 않습니다. 별도 기능 담당자가 필요한 명령을 확장하려면 동일한 프로젝트·소유권·불확실한 변경 결과 검사를 함께 추가해야 합니다.

## 상태와 제거

30초마다 연결·OpenStack 조회·전용 CLI 서비스 상태와 관리 호스트 사용률을 보고합니다. CPU(중앙처리장치)·네트워크 첫 표본과 읽기 실패는 null(미수집)입니다. 지표는 관리 호스트 범위이며 전체 OpenStack 프로젝트 수치가 아닙니다. 네트워크 값에는 loopback을 제외한 호스트 인터페이스 합계와 터널 통신이 포함됩니다.

환경 삭제는 서버의 기존 앱 실행기가 RailShot 서비스와 확인된 앱 전용 데이터를 먼저 제거한 후 진행합니다. 클라이언트는 `environment.delete` 명령의 `applications=[]`를 확인해야 제거합니다. 설치가 새로 만든 응용 자격 증명은 로컬 소유 기록과 별도 root 전용 소유자 저장소의 결합을 확인한 뒤, 전용 서비스 사용자 인증으로 그 자격 증명만 폐기하고 조회 결과가 없음을 확인합니다. 관리자 암호는 사용하거나 보관하지 않습니다. 기존 외부 자격 증명은 소유 기록이 없으므로 폐기하지 않습니다. 폐기 실패나 결과 불확실 상태에서는 로컬 자격 증명 저장소를 보존하고 기존 재확인·재개 절차를 따릅니다.

폐기가 확인되면 독립 systemd 임시 서비스가 관리 클라이언트·전용 SSH·전용 계정과 로컬 자격증명·WireGuard·설치 파일의 부재를 확인한 뒤 완료 증거를 보냅니다. 기본 통신은 HTTPS이며 명시적으로 저장한 시험 설정에 한해 HTTP를 허용합니다. 임시 실행 파일과 비밀 파일도 전송 전에 제거합니다. 마지막 응답 유실은 `unknown`(확인 필요)이며 접속이 끊겼다는 사실을 성공 근거로 쓰지 않습니다.

OpenStack 자체·기반 가상 머신·기존 클러스터·고객 별도 서비스·공유 패키지·공유 권한은 클라이언트 제거에서 삭제하지 않습니다. CLI를 통해 만들었던 기반 인프라도 클라이언트 제거만으로 삭제하지 않습니다. 설치기가 만든 전용 서비스 사용자와 역할 할당 메타데이터, 공유 `railshot` 프로젝트도 보존합니다. 정리 중 소유권이 다른 파일이나 계정을 발견하면 제거를 차단합니다.

## 게이트웨이 운영 배치

`deployment/scripts/personal_wireguard.py`는 실제 `wg`, `wg-quick`, `ip`, `iptables`, `systemctl`을 실행하는 root 전용 도구입니다. 모의 성공 기본값은 없습니다. 다음 예시는 API와 게이트웨이 권한 도구가 같은 호스트의 파일 경로를 사용하는 배치입니다.

- `deployment/manifests/personal/railshot-personal-gateway`를 `/usr/local/sbin/railshot-personal-gateway`에 root 소유 0755로 설치합니다.
- Python 도구를 `/opt/railshot/deployment/scripts/personal_wireguard.py`에 root 소유로 설치합니다. 상위 경로는 API 계정이 쓸 수 없어야 합니다.
- `gateway.example.json`을 `/etc/railshot-personal-gateway/config.json`에 root 소유 0600으로 설치하고 실제 공개 주소와 API 계정 UID를 지정합니다.
- 게이트웨이 개인키는 0600으로 준비합니다. 실제 네트워크에서 고객의 outbound UDP가 게이트웨이 포트에 도달해야 합니다.
- `gateway.sudoers`의 API 계정명을 확인하고 `visudo -cf`로 검증하여 설치합니다. 고정 wrapper 외 일반 명령에는 sudo 권한을 부여하지 않습니다.

API 설정에는 `gateway.command=/usr/local/sbin/railshot-personal-gateway`와 고정 설정 경로를 넣습니다. 요청 파일은 `request_root` 바로 아래에서 `request_owner_uid` 소유의 0600 일반 파일만 허용합니다. 상대 경로·심볼릭 링크·하드 링크는 거절합니다. 게이트웨이는 등록 주소와 제거 기록을 영구 저장하고, 연결 갱신 시 고객 `/32` 경로를 실제로 추가·제거합니다.

기존 API 컨테이너에서 호스트 sudo를 직접 사용할 수 있다고 가정하지 않습니다. 컨테이너 배치를 유지하려면 별도의 권한 중계 연결이 필요하며, API를 root로 실행하는 방식으로 우회하지 않습니다.

## 검증 범위

`apps/agent/tests/test_openstack_control.py`, `test_personal.py`, `test_agent.py`는 명령 제한, 프로젝트 격리, 소유권과 데이터 확인, 실제 CLI 어댑터 인자·비밀 파일 처리, 변경 중복 방지, 연결 상태, 안전 제거를 검사합니다. OpenStack 클라우드 응답과 운영체제 외부 명령은 검사에서 대체합니다. 이 자동 시험의 통과는 실제 설치·삭제 결과와 구분합니다. 실제 환경에서 확인한 범위는 아래 기록을 따릅니다.


### 2026-10-04 실제 삭제·복구 검증

기존 Ubuntu 고객 호스트에서 결과가 불확실했던 삭제 작업을 **제품 API의 재확인 → 재개 경로**로 처리했습니다. 설치가 온전하고 제거 프로세스가 없다는 고객 측 확인 후 같은 작업 ID를 재개했으며, 최종 상태 `succeeded/complete`, 잔여 항목 없음, 대상 `deleted`와 소유 환경 목록 제외를 확인했습니다. 데이터베이스 상태를 직접 수정하지 않았습니다.

고객 호스트에서는 RailShot 클라이언트 파일·전용 계정과 그룹·임시 제거 실행 파일·서비스 파일·`railshot0` 터널의 부재를 확인했습니다. 기존 `wg0` 터널과 SSH 접속은 유지됐고, 원래 OpenStack 가상머신 3개도 같은 ID로 `ACTIVE` 상태를 유지했습니다. 서버에서는 해당 WireGuard 연결 상대가 제거되고 클라이언트 인증 해시와 명령이 폐기됐음을 확인했습니다. 비밀값이나 접속 자격은 이 기록에 포함하지 않습니다.

**해당 실제 개인 환경에는 RailShot 앱이 0개였으므로 이 결과만으로 앱 삭제까지 검증했다고 보지 않습니다.** 앱 삭제는 별도의 일회용 실제 K3s에서 제품의 기존 `lifecycle_runtime.inventory/execute`를 직접 호출하여 확인했습니다. 실행 중 Deployment·Pod와 앱 전용 Secret·ConfigMap·네임스페이스가 제거됐고, 다른 네임스페이스의 ConfigMap은 고유번호와 데이터가 보존됐습니다. 이 시험은 등록 환경의 전체 앱 삭제 API 흐름을 실제 앱으로 끝까지 실행한 검증과는 구분합니다. PVC(영구 저장 공간 요청)와 그 실제 데이터의 삭제는 이번 실제 시험에 포함하지 않았으며 관련 검증은 모의 시험에 한정됩니다.

## 별도 가상머신의 Kubernetes 관리 통로

등록 설치기가 선택한 가상머신의 건강 상태를 확인한 뒤 `apps/agent/runtime_access.py`가 고객 관리 호스트에 두 통로를 구성합니다. 관리 호스트 자체에 K3s를 설치하지 않습니다. 일반 가상머신 SSH 개인키는 고객 호스트의 root 전용 저장소에 남습니다.

- WireGuard 고객 주소의 **2223번**은 `railshot-runtime` 전용 계정으로만 접속합니다. 서버 주소 하나와 등록 공개키만 허용하고, 고정 명령 처리기가 검증한 Kubernetes 조회·전용 등록 권한 생성·정확한 고유번호를 지정한 권한 제거만 전달합니다. 임의 셸, 대화형 접속, SSH 포트 전달, 다른 고객 네임스페이스의 비밀 조회는 허용하지 않습니다. 제한 없는 sudo 권한도 부여하지 않습니다.
- 같은 주소의 **16443번**은 선택한 가상머신의 6443번 Kubernetes API로 암호화된 바이트를 전달합니다. TLS(전송 계층 보안)를 중간에서 해제하지 않으며, 서버 WireGuard 주소에서 온 연결만 받습니다. 목적지나 포트를 요청별로 바꾸는 기능은 없습니다. 서버는 실제 가상머신 주소를 인증서 이름으로 검증합니다.

새 네임스페이스는 원자적인 생성 요청으로 만들고 고유번호와 개인 환경 ID·등록 세대·가상머신 ID를 고객 호스트의 root 전용 기록에 보관합니다. 개인 환경 네임스페이스와 `app-<24자리 해시>` 형식의 앱 전용 네임스페이스를 지원합니다. 이미 있는 고객 네임스페이스는 이름이나 라벨이 같아도 인수하지 않습니다. 이후 고유번호나 환경 귀속이 달라지면 해당 통로의 변경 요청을 차단합니다. 인증서 조회는 CA(인증기관) 인증서만 반환하며 관리자의 인증서·개인키가 포함된 kubeconfig를 서버에 전달하지 않습니다.

앱 중지·재시작·삭제는 기존 `lifecycle_runtime.py`의 소유권·저장소 검사를 고객 측에서도 다시 실행합니다. 다른 고객 자원이 섞이거나 공유 저장소가 발견되면 변경을 거절합니다. 저장소 보존 판단에 필요한 전체 볼륨·연결 정보는 조회만 허용하고, 변경은 기록된 앱 네임스페이스의 고유번호와 버전에 묶습니다. 검사 시간이 필요한 이 요청만 서버에서 최대 300초를 기다리며, 일반 조회·등록의 제한시간은 그대로입니다. 시간 초과는 삭제 성공으로 간주하지 않습니다.

전용 서비스는 `railshot-personal-kube`와 `railshot-personal-kube-proxy`입니다. 제거 사전검사는 두 서비스, 고정 sudo 처리기, 전용 계정, 로컬 설치 기록의 소유권을 함께 확인합니다. 검증된 클라이언트 제거는 이 로컬 접속 기능을 정리하며, 기존 가상머신·고객 SSH 키 원본·클러스터를 삭제하지 않습니다. 실제 고객 연결 검증 전에는 모의 명령 검사 통과를 실배포 성공으로 표시하지 않습니다.
