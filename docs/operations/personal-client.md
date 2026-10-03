# 개인 OpenStack 제어 연결

목표는 **설치 완료 시 RailShot 서버가 고객 호스트에 보관된 자격증명으로 OpenStack CLI를 실행할 수 있는 상태**입니다. CLI는 명령줄 실행 도구입니다. K3s 설치, 가상 머신 생성, 기존 실행환경 등록은 이 설치 흐름에서 하지 않습니다. 관리 호스트가 OpenStack 가상 머신일 필요도 없습니다. 앱 배포 실행환경은 별도 담당 경로에서 연결합니다.

## 선행조건과 설치

고객 관리 호스트는 Ubuntu 24.04이며 sudo/root 설치 권한, OpenStack 프로젝트의 HTTPS 인증 주소와 Application Credential(프로젝트 권한을 위임받은 인증정보), 서버 HTTPS·WireGuard UDP 연결이 필요합니다. 운영자는 RailShot API 서버와 게이트웨이 설정 및 고정 배포본을 먼저 준비합니다. Application Credential의 실제 프로젝트가 설치 입력의 프로젝트 ID와 다르면 등록을 거절합니다. 자격증명은 고객 호스트의 암호화 저장소에만 보관하고 서버로 전송하지 않습니다.

운영자는 검토한 리비전에서 다음 명령으로 재현 가능한 설치 배포본을 만듭니다. 출력 파일은 자동 게시하거나 덮어쓰지 않습니다.

```bash
python3 deployment/scripts/package-personal-client.py --output /tmp/railshot-personal.tgz
```

출력의 SHA256 검증값, 배포본 HTTPS 주소 및 `deployment/bootstrap/personal-install.sh`의 변경 불가능한 HTTPS 주소를 서버 설정에 넣습니다. 설치 압축파일에는 검토한 코드만 들어가며 심볼릭 링크·특수파일·상위 경로를 허용하지 않습니다. 화면의 설치 명령은 스크립트 다운로드와 다음 실행을 한 번에 제공합니다.

```bash
sudo bash personal-install.sh \
  --api-url https://railshot.example.com \
  --enrollment-id ENROLLMENT_ID \
  --artifact-url https://releases.example.com/REVISION/personal.tar.gz \
  --artifact-sha256 VERIFIED_SHA256
```

일회용 토큰은 숨김 입력 또는 `RAILSHOT_ENROLLMENT_TOKEN` 환경변수로 전달합니다. OpenStack 비밀값과 등록 토큰을 명령 인자·로그에 넣지 않습니다. 설치기는 프로젝트 ID와 인증정보를 현장에서 입력받습니다. `--profile-id`는 서버가 발급한 경우에만 전달하는 선택 항목이며 K3s나 VM ID는 요구하지 않습니다.

설치기는 로컬 WireGuard 키와 전용 SSH 호스트 키를 만들고 공개키만 등록합니다. 서버 접속 자격을 원자적으로 저장한 뒤 운영체제 설정을 적용합니다. 같은 명령을 재실행하면 저장된 등록을 재사용합니다. 서버 등록 응답이 저장 전에 유실되면 자동으로 토큰을 다시 소비하지 않으며 운영자의 상태 대조가 필요합니다.

스크립트는 로컬 서비스 시작만으로 성공을 출력하지 않습니다. 최대 180초 동안 상태 보고를 보내며 **서버가 터널을 통해 실제 `openstack server list`를 실행하고 등록 상태를 `ready`로 확인한 뒤** 종료합니다. 시간 초과는 실패로 반환하고 현장 상태를 보존합니다. 등록의 `ready`와 앱 배포 실행환경의 `deployable`은 별개입니다.

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

서버·볼륨 삭제에는 요청의 `delete_data:true`가 추가로 필요합니다. 키페어 생성에는 `public_key`로 검증된 Ed25519 공개키를 전달해야 하며 개인키 생성은 지원하지 않습니다. 같은 키 이름으로 외부에서 다시 만든 키는 생성 당시 공개키·지문과 다르므로 삭제하지 않습니다.

`--os-*`, `--project`, `--all-projects`, `--debug`, `--insecure`, 외부 파일·사용자 데이터 입력, 인증·서버 주소·출력 형식 변경, 임의 플러그인은 거절합니다. 네트워크 계열 목록에는 처리기가 등록 프로젝트 필터를 직접 붙입니다. 관리자 역할이 있는 프로젝트 자격증명도 다른 프로젝트의 포트·보안 그룹을 변경하지 못하도록 생성 시 참조 자원을 조회해 프로젝트를 대조합니다. 공개/공유 이미지·네트워크 조회 및 외부 유동 IP 풀 사용은 필요한 예외이며, 다른 프로젝트의 자원 삭제 예외는 없습니다.

명령 실행은 실제 `OpenStackCLI`를 재사용합니다. 인증정보는 로컬 0600 임시 설정 파일에 쓰고 셸 없이 인자 배열로 실행합니다. 모든 명령 전에 내부적으로 토큰의 프로젝트를 확인하되 그 토큰은 반환하지 않습니다. 변경 의도를 디스크에 먼저 기록하므로 시간 초과·프로세스 중단 뒤 같은 변경을 자동 재실행하지 않습니다.

이 규격은 OpenStack 전체 CLI를 대체하지 않습니다. 예를 들어 역할/계정 관리, 라우터 인터페이스 연결, 임의 서버 재설정, 파일 업로드, 외부에서 만든 자원 삭제는 현재 허용하지 않습니다. 별도 기능 담당자가 필요한 명령을 확장하려면 동일한 프로젝트·소유권·불확실한 변경 결과 검사를 함께 추가해야 합니다.

## 상태와 제거

30초마다 연결·OpenStack 조회·전용 CLI 서비스 상태와 관리 호스트 사용률을 보고합니다. CPU(중앙처리장치)·네트워크 첫 표본과 읽기 실패는 null(미수집)입니다. 지표는 관리 호스트 범위이며 전체 OpenStack 프로젝트 수치가 아닙니다. 네트워크 값에는 loopback을 제외한 호스트 인터페이스 합계와 터널 통신이 포함됩니다.

환경 삭제는 서버의 기존 앱 실행기가 RailShot 서비스와 확인된 앱 전용 데이터를 먼저 제거한 후 진행합니다. 클라이언트는 `environment.delete` 명령의 `applications=[]`를 확인해야 제거합니다. 독립 systemd 임시 서비스가 관리 클라이언트·전용 SSH·전용 계정과 로컬 자격증명·WireGuard·설치 파일의 부재를 확인한 뒤 HTTPS 완료 증거를 보냅니다. 임시 실행 파일과 비밀 파일도 전송 전에 제거합니다. 마지막 응답 유실은 `unknown`(확인 필요)이며 접속이 끊겼다는 사실을 성공 근거로 쓰지 않습니다.

OpenStack 자체·기반 가상 머신·기존 클러스터·고객 별도 서비스·공유 패키지·공유 권한은 클라이언트 제거에서 삭제하지 않습니다. CLI를 통해 만들었던 기반 인프라도 클라이언트 제거만으로 삭제하지 않습니다. 정리 중 소유권이 다른 파일이나 계정을 발견하면 제거를 차단합니다.

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

`apps/agent/tests/test_openstack_control.py`, `test_personal.py`, `test_agent.py`는 명령 제한, 프로젝트 격리, 소유권과 데이터 확인, 실제 CLI 어댑터 인자·비밀 파일 처리, 변경 중복 방지, 연결 상태, 안전 제거를 검사합니다. OpenStack 클라우드 응답과 운영체제 외부 명령은 검사에서 대체합니다. 실제 Ubuntu 호스트·OpenStack 자격·WireGuard 연결로 설치→서버 조회→제거를 수행하는 검증은 아직 완료하지 않았습니다.
