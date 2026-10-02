# 고객 노드 작업 요청 규격 v1

## 범위

이 규격은 두 번째 시험 가상 머신이 SSH로 작업을 지시하고, 첫 번째 시험 가상 머신의 에이전트가 고객 노드에 보관된 인증정보로 OpenStack 명령을 실행한 뒤 결과를 반환하는 기능입니다. 현재 허용 작업은 가상 머신 목록 조회 `instance.list` 하나뿐입니다. 임의 셸 명령, 자원 생성·삭제, 인증정보 원격 전달은 제공하지 않습니다.

SSH 연결은 기존에 확인한 호스트 키를 강제로 검증합니다. 대상 키의 authorized_keys 항목은 `restrict`와 강제 실행 명령을 사용하며, 요청자가 보낸 원격 명령은 정확히 `jasmin-job-v1`이어야 합니다. 강제 실행 명령에 지정한 실행 파일·설정 경로는 고객 노드의 관리자가 정하며, 요청 본문으로 변경할 수 없습니다.

## 요청

UTF-8 JSON 한 건을 표준 입력으로 전달하고 입력을 종료합니다. 최대 크기는 16,384바이트입니다.

```json
{"version":1,"job_id":"job-20261002-01","action":"instance.list","params":{}}
```

- 네 필드 외 추가 필드는 허용하지 않습니다.
- `version`은 정수 1이며, 불리언 true는 허용하지 않습니다.
- `job_id`는 영문자·숫자·밑줄·하이픈으로 구성된 1~64자입니다. 공백·개행은 허용하지 않습니다.
- `action`은 정확히 `instance.list`입니다.
- `params`는 빈 JSON 객체여야 합니다. 배열, null, 추가 파라미터는 허용하지 않습니다.
- JSON 중복 필드, 잘못된 인코딩, 여러 JSON 문서, 크기 초과 요청은 거부합니다.

## 응답

표준 출력에는 JSON 응답 한 건만 반환합니다.

```json
{
  "version": 1,
  "job_id": "job-20261002-01",
  "action": "instance.list",
  "ok": true,
  "result": [{"id":"SERVER_UUID","name":"example","status":"ACTIVE"}],
  "error": null
}
```

실패 시 `ok`는 false, `result`는 null, `error`는 `{ "code": "고정 오류 코드" }`입니다. 요청을 안전하게 해석하지 못하면 `job_id`와 `action`은 null로 반환할 수 있습니다. 고정 오류 코드는 `invalid_request`, `request_too_large`, `command_rejected`, `credentials_unavailable`, `execution_failed`, `invalid_upstream_result`, `response_too_large`, `transport_failed`입니다. 외부 오류 문자열로 코드를 생성하지 않습니다.

OpenStack 목록의 각 항목은 `id`, `name`, `status`만 반환합니다. 프로젝트 인증정보, 토큰, 네트워크 주소, 메타데이터, CLI 원본 출력·오류는 응답에 포함하지 않습니다. 조회 결과 자체는 고객 프로젝트의 정보이므로 이 작업 키는 해당 프로젝트를 조회할 권한이 있는 호출자에게만 부여해야 합니다.

## 인증정보와 제한 시간

인증정보는 첫 번째 가상 머신의 `CredentialStore`에서만 로딩합니다. 두 번째 가상 머신은 작업 전용 SSH 개인키와 검증된 호스트 키만 필요합니다. 암호문이나 복호화 키를 두 번째 노드에 복사하지 않습니다.

표준 입력 읽기는 최대 10초이며, 실제 OpenStack CLI 실행은 30초로 제한합니다. 응답은 최대 1 MiB, 최대 10,000개 항목이며 각 문자열은 id 128자, name 255자, status 64자 이내입니다. 제어문자와 로컬 인증 비밀값을 포함한 필드는 거부합니다. 제한을 넘는 결과는 일부 성공처럼 자르지 않고 오류로 반환합니다. OpenStack CLI 실패 시 표준 오류의 비밀정보를 그대로 전달하지 않습니다. 구체 제한과 검증 결과는 구현 및 테스트를 확인합니다.

## 재전송 정책

`job_id`는 호출자와 응답의 연결을 위한 값이며 v1에서는 영속 중복 제거 또는 정확히 한 번 실행을 보장하지 않습니다. 같은 요청을 다시 보내면 목록을 다시 조회할 수 있으므로 그 사이 목록 내용이 바뀔 수 있습니다. 현재 작업이 조회 전용이므로 자원 중복 생성은 없지만 API 부하가 발생할 수 있습니다. 자원 변경 작업을 추가하기 전에는 영속 작업 상태·중복 방지·권한·승인 규격을 별도로 설계해야 합니다.

## 검증 상태

자동 테스트는 잘못된 요청과 모의 OpenStack 응답을 사용한 코드 검증입니다. 실제 고객 인증정보로 두 번째 노드 → 첫 번째 노드 → OpenStack 조회 → 결과 반환을 확인하기 전에는 전체 실제 연동 성공으로 보고하지 않습니다.

용어: SSH는 암호화 원격접속, CLI는 명령줄 도구, JSON은 구조화된 데이터 형식, API는 프로그램 간 호출 인터페이스입니다.

독립 공격 사례 검증은 `python -m pytest -q ci/tests/test_agent_jobs.py`로 실행합니다. 구현 담당자의 별도 테스트는 `apps/agent/tests/`에 있습니다.

## 두 노드의 설치·실행 방법

이 기능은 기존 `/etc/jasmin` 자격증명의 root 소유권을 유지하기 위해 고객 노드의 root 계정에 **작업 전용으로 제한된 공개키 한 개**를 등록하는 방식입니다. 일반 root 원격 셸 키를 호출자에게 제공하는 방식이 아닙니다. 키 생성과 설치는 관리자가 수행하며 기존 authorized_keys를 덮어쓰지 않습니다.

첫 번째 노드에서 아래 도구는 등록할 공개키 한 줄을 출력만 합니다. SSH 설정을 자동 수정하지 않습니다.

```sh
python apps/agent/install_forced_command.py \
  --public-key /root/jasmin-job/server-job.pub \
  --source-ip 10.200.0.2 \
  --python /opt/jasmin/bootstrap/deployment/bootstrap/.venv/bin/python \
  --repo /opt/jasmin/bootstrap \
  --config-dir /etc/jasmin
```

주소는 형식 예시이며 실제 두 번째 노드의 WireGuard 주소로 바꿔야 합니다. 출력에는 `restrict`, 단일 출발지 제한, 고정된 실행 명령이 포함됩니다. 출력한 한 줄을 검토한 후 첫 번째 노드의 root authorized_keys에 기존 내용과 함께 추가합니다. 이 파일과 부모 경로·프로그램·Python 환경은 root 소유로 보호하고, 개인 파일은 0600으로 설정해야 합니다. Python 경로는 일반 사용자가 교체할 수 없어야 합니다.

두 번째 노드에서는 작업 전용 개인키와 첫 번째 노드의 별도 확인된 호스트 키 파일을 준비하고 다음처럼 요청합니다.

```sh
python apps/agent/sender.py \
  --host 10.200.0.1 --user root \
  --key /root/jasmin-job/server-job \
  --known-hosts /root/jasmin-job/known_hosts \
  --interface jasmin0 --job-id trial-1
```

송신 도구는 대상 주소의 실제 경로가 지정한 WireGuard 인터페이스인지 확인하고 그 경로의 출발지에 연결을 묶습니다. 기본 인터페이스는 `jasmin0`이며 시험 터널의 이름이 다르면 명시해야 합니다. 경로 확인은 5초, SSH 전체 요청은 45초로 제한합니다. SSH 표준 오류는 호출자에게 그대로 출력하지 않고 응답의 작업 식별자·버전·필드와 종료 코드까지 확인합니다.

기존 고정 설치 묶음과 새 소스가 다르면 설치 프로그램은 자동 덮어쓰지 않습니다. 작업 에이전트를 포함한 새 배포는 기존 설치 파일을 임의 교체하는 대신 검증된 별도 배포 경로 또는 명시적인 버전 전환 절차로 수행해야 합니다. 실제 전환 여부와 검증 결과는 실행기록에 별도로 남깁니다.
