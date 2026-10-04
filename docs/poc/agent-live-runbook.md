# 두 노드 작업 요청 실환경 실행 절차

이 문서는 실행 전 검토용 절차입니다. 기록된 명령은 자동으로 수행되지 않았습니다. 시험 노드 이외에는 적용하지 않으며 기존 고정 설치 `/opt/jasmin/bootstrap`을 덮어쓰지 않습니다.

## 실행 담당자가 먼저 확정할 값

- 고객 노드: 이번 시험 node-1. WireGuard 주소와 호스트 키를 독립 확인합니다.
- 지시 노드: 이번 시험 node-2. 실제 WireGuard 주소를 공개키 출발지 제한에 사용합니다.
- 새 배포 경로 예시: `/opt/jasmin-agent-e2e-20261002-02`.
- 고객 인증정보 경로: `/etc/jasmin` 또는 명시적으로 확인한 별도 root 전용 경로.
- 기존 root authorized_keys와 기존 원격접속 세션을 보존합니다.

실제 고객 자격증명이 아직 없으면 강제 명령 거부와 인증정보 없음 오류까지 검증할 수 있습니다. 성공 목록을 만들어 대체하거나 이를 실제 OpenStack 조회로 보고하지 않습니다.

## 1. 새 배포 경로 준비

검증한 소스를 root 소유 새 디렉터리에 복사하고 `apps/agent`, `deployment/bootstrap/client_setup`, `infrastructure/providers/openstack`의 상대 구조를 보존합니다. 새 경로가 이미 있으면 내용을 먼저 비교하며 덮어쓰지 않습니다. 실행 프로그램과 모든 부모 디렉터리는 root 소유이며 일반 사용자가 수정할 수 없어야 합니다. 복사한 프로그램이 심볼릭 링크를 통해 일반 사용자 경로를 참조하면 중단합니다.

새 배포 안에 Python 가상환경을 생성하고 고정된 requirements.lock을 설치합니다. 기존 설치 가상환경을 수정하지 않습니다. 격리 실행 검증 예:

```sh
/opt/jasmin-agent-e2e-20261002-02/.venv/bin/python -I \
  /opt/jasmin-agent-e2e-20261002-02/apps/agent/runner.py \
  --repo /opt/jasmin-agent-e2e-20261002-02 --config-dir /etc/jasmin
```

원격 명령 환경변수가 없는 위 실행은 `command_rejected` JSON 한 건과 종료 코드 1이어야 하며 원본 오류 추적을 출력하면 안 됩니다. 이 검증은 실제 조회 성공이 아닙니다.

## 2. 작업 전용 키와 호스트 키

두 번째 노드에서 이번 작업 전용 Ed25519 키를 root 전용 디렉터리에 생성합니다. 개인키는 두 번째 노드에만 보관하고 공개키만 첫 번째 노드로 전달합니다. 기존 관리용 SSH 개인키를 재사용하지 않습니다. 공개키 지문을 양쪽에서 대조합니다.

첫 번째 노드의 호스트 공개키는 콘솔에서 확인한 값으로 두 번째 노드의 root 전용 known_hosts에 기록합니다. 네트워크 검색으로 받은 미검증 키를 그대로 신뢰하지 않습니다. 개인키·known_hosts는 root 소유 0600, 디렉터리는 0700입니다.

## 3. 제한된 공개키 행 생성과 검토

첫 번째 노드에서 고정된 새 배포 Python으로 다음을 실행합니다. 실제 WireGuard 출발지 주소로 바꾼 뒤 사용합니다.

```sh
/opt/jasmin-agent-e2e-20261002-02/.venv/bin/python \
  /opt/jasmin-agent-e2e-20261002-02/apps/agent/install_forced_command.py \
  --public-key /root/jasmin-job/server-job.pub \
  --source-ip 10.200.0.2 \
  --python /opt/jasmin-agent-e2e-20261002-02/.venv/bin/python \
  --repo /opt/jasmin-agent-e2e-20261002-02 \
  --config-dir /etc/jasmin
```

출력에는 `restrict`, `/32` 또는 `/128` 한 주소의 `from`, `python -I`로 시작하는 고정 `command`가 모두 있어야 합니다. 출력을 `/root/jasmin-job/restricted-key.line`이라는 root 소유 0600 파일에 저장해 한 줄임을 검토합니다. 출력만 수행하는 도구이므로 여기까지 기존 SSH 파일은 변경되지 않습니다.

## 4. 기존 authorized_keys 보존 후 추가

`/root`, `/root/.ssh`, authorized_keys의 심볼릭 링크·소유권·권한·하드링크를 확인합니다. `.ssh`는 root 소유 0700, 기존 authorized_keys는 root 소유 일반 파일 0600이어야 합니다. 기존 동일 키가 이미 제한 없이 등록되어 있으면 작업을 중단하며, 기존 행을 자동 삭제하지 않습니다.

다음 절차를 실행 전에 검토합니다.

1. 기존 내용을 그대로 읽어 백업 파일 `authorized_keys.before-jasmin-job`에 0600으로 저장합니다. 백업이 이미 있으면 덮어쓰지 않고 중단합니다.
2. 같은 공개키 본문이 기존 파일에 있는지 확인합니다. 완전히 동일한 제한 행이면 재추가하지 않습니다. 다른 제한 또는 무제한 등록이면 중단합니다.
3. 기존 내용 끝에 개행을 유지하고 검토한 제한 행 한 개만 추가합니다.
4. 같은 디렉터리의 0600 임시 파일에 쓴 뒤 내용을 동기화하고 원자적으로 authorized_keys를 교체합니다.
5. 기존 관리 접속을 유지한 상태에서 파일 차이가 제한 행 한 개뿐인지 확인합니다. SSH 서비스의 전역 설정이나 기존 키는 바꾸지 않습니다.

파일 편집을 실행 담당자가 직접 검토하며 수행해도 됩니다. 정상 동작 확인 전 기존 관리 세션을 종료하지 않습니다. 롤백 시 이번에 추가한 제한 행만 제거하고 동시 변경 여부를 확인합니다. 백업으로 무조건 덮어쓰면 다른 작업의 키 변경을 잃을 수 있습니다.

## 5. 두 번째 노드에서 실제 요청

고객 노드의 실제 자격증명은 기존 `CredentialStore`에서만 읽습니다. 두 번째 노드에 복사하지 않습니다.

```sh
python /opt/jasmin-agent-e2e-20261002-02/apps/agent/sender.py \
  --host 10.200.0.1 --user root \
  --key /root/jasmin-job/server-job \
  --known-hosts /root/jasmin-job/known_hosts \
  --interface jasmin0 --job-id live-list-01
```

주소·인터페이스는 실제 시험 터널 값으로 바꿉니다. `ip route get`이 해당 터널과 출발지를 선택해야 하며, sender가 고정 명령 `jasmin-job-v1`과 요청 본문을 SSH로 전달합니다. 정상 결과는 JSON 한 건, `ok:true`, `id/name/status` 항목 목록, 종료 코드 0입니다. 원본 CLI 출력이나 암호·토큰은 출력하지 않습니다.

같은 작업 식별자 재전송은 목록을 다시 조회합니다. 이는 정확히 한 번 실행이나 결과 캐시를 보장하지 않습니다.

## 6. 실패 조건 실제 검증

- 작업 전용 키로 `id` 등 다른 원격 명령을 요청하면 `command_rejected`로 거부되어야 합니다. 실제 셸이 열리면 즉시 중단합니다.
- 다른 호스트 키가 들어 있는 별도 시험 known_hosts를 지정하면 연결을 거부해야 합니다. 정상 파일은 변경하지 않습니다.
- 실제 경로와 다른 인터페이스 이름을 지정하면 SSH 실행 전에 거부해야 합니다.
- 알 수 없는 action 또는 추가 필드가 있는 JSON은 `invalid_request`이며 OpenStack 명령을 실행하지 않아야 합니다.
- 자격증명이 없으면 `credentials_unavailable` 오류이며 성공 목록을 반환하지 않아야 합니다.

실제 검증 결과는 `bootstrap-live-20261002.md`에 실행 시각, 대상, 제한된 결과, 종료 코드만 기록합니다. 비밀정보나 전체 자격증명을 저장하지 않습니다.

## 7. 유지·제거 범위

사용자가 유지하도록 지시한 시험 보안그룹 TCP 22번 포트 전체 출발지 허용은 이 절차에서 자동 축소·제거하지 않습니다. 작업 전용 공개키 행의 `from` 제한은 그 키에만 적용됩니다. 시험 작업 접근을 제거할 때는 그 제한 행과 전용 키만 구분해 처리하고, 기존 관리 키·운영 가상 머신·다른 프로젝트는 변경하지 않습니다.
