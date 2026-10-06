# 플랫폼 에이전트 인증 교체

에이전트는 플랫폼 운영 계정으로 실행한다. 사용자 앱의 환경변수·업로드 소스·GitHub 변수로 계정이나 키를 선택하지 않는다. Codex 구독 인증은 기존 전용 build 호스트의 `/var/lib/railshot-runner/codex/auth.json`에만 설치한다. 이 경로는 사용자 앱 컨테이너에 마운트하지 않는다.

운영자 PC에서 AWS 계정 `721622471953`의 기존 SSM 관리 권한과 Codex CLI, OpenSSL을 사용한다. 플랫폼 계정으로 발급한 전용 `auth.json`을 저장소 밖에 `0600`으로 준비한 뒤 실행한다. 비밀값을 명령 인자나 환경변수에 넣지 않는다.

```sh
python3 deployment/scripts/rotate-platform-codex.py \
  --auth-file /private/railshot-platform-account/auth.json \
  --model gpt-5.5
```

모델 이름은 해당 계정이 실제로 지원하는 값으로 지정한다. 위 모델은 2026-10-06에 확인한 예시다. 이 명령은 격리된 인증 디렉터리에서 실제 짧은 모델 요청으로 호환성을 검사하므로 SDK 호출 비용/구독 한도를 사용한다. 검사 실패 시 운영 인증을 변경하지 않으며, 원본 provider 오류나 토큰은 출력하지 않는다. 계정 로그인 자체는 이 명령이 대신 수행하지 않는다. 플랫폼 전용 계정의 갱신 토큰을 개인 PC와 여러 런너에서 동시에 사용하지 않는다.

사전 확인 후 고정된 build 호스트 `i-09955d23ad1d8dbe2`의 일회용 공개 인증서로 인증 묶음을 암호화한다. 기존 SSM TLS 통신에는 공개 코드·공개 인증서·암호문만 전달된다. 호스트의 개인키와 해독 파일은 교체 시도 종료 시 제거한다. GitHub Actions에서 이 운영자 명령을 실행하는 것은 거부한다. GitHub Secrets/Variables, Git, Terraform state, workflow 입력, 로그 또는 업로드 아티팩트로 인증값을 전달하지 않는다. build 인스턴스의 기존 SSM/Secrets Manager 읽기 거부 IAM 정책도 완화하지 않는다.

호스트는 기존 에이전트 capacity 슬롯을 모두 확보한 뒤 인증과 `railshot-account.json`을 교체한다. 실행 중인 에이전트가 있으면 `AGENT_BUSY`로 중단하며 자동으로 강제 교체하지 않는다. 두 파일은 `root:root`, `0600`이고, 비밀값 없는 binding에는 플랫폼 소유권·계정 지문·지원 모델·교체 ID가 들어간다. 파일 교체 도중 프로세스가 중단되면 계정 binding 검사가 일치하지 않는 조합을 거부한다. 현재 SDK가 같은 계정의 토큰을 갱신하는 동작은 허용한다. 교체 ID와 모델은 loop의 인증 경로 binding에도 포함되어 이전 인증 상태로 만들어진 checkpoint를 묵시적으로 재사용하지 않는다.

이전 두 파일은 호스트의 `/var/lib/railshot-runner/credential-rotation/previous-<교체 ID>/`에 비공개로 보존한다. 교체나 readback 실패 시 기존 파일을 되돌린다. 수동 복구가 필요하면 새 CI 접수를 멈추고 실행 중 에이전트가 없음을 확인한 뒤 이 백업을 같은 고정 경로로 복원하고 권한·계정 binding을 검증한다. 백업도 비밀값이므로 GitHub에 업로드하지 않는다. 운영 종료 시 인증 디렉터리와 이전 인증 백업의 폐기도 함께 수행한다.

출력 receipt의 `installed`는 파일 배포와 binding readback 성공을 뜻한다. 실제 CI SDK 실행·이미지 게시·AWS/GCP 앱 배포 완료는 각각 별도로 확인한다. `DELIVERY_OUTCOME_UNKNOWN:<command-id>`이면 해당 SSM command와 호스트의 `rotation_id`를 먼저 조회하며, 새 교체를 자동 반복하지 않는다. 최초 SSM prepare 응답이 유실된 경우에는 비공개 staging 디렉터리의 생성 시각과 실행 상태를 확인한 뒤 오래된 일회용 키만 제거한다.

`DELIVERY_SUBMISSION_UNKNOWN:<교체 ID>`는 SSM 접수 응답 자체가 유실된 경우다. `railshot-credential-<교체 ID>-apply` comment의 SSM 명령 이력과 호스트 binding을 확인한 뒤 후속 조치를 정한다. 진행 중인 loop도 최초 인증 경로 지문을 각 SDK 호출 직전에 대조하므로, 시도 사이에 계정이 교체되면 새 계정으로 이어가지 않고 SDK 호출 전에 중단한다.
