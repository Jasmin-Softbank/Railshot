# 전용 CI VM의 GitHub Actions runner 컨테이너

`ci/runner-compose.yml`은 **전용 Ubuntu 24.04 amd64 CI VM**에서 앱 저장소의 `railshot-deploy.yml` loop job을 한 번 실행하는 runner입니다. 운영 K3s와 고객 runtime에는 설치하지 않습니다. GitHub 공식 `actions/actions-runner:2.337.0` 이미지를 digest로 고정하고 Python·호스트 방화벽 확인 도구만 추가합니다. 공식 이미지에 Git, Docker CLI, Buildx, Actions용 Node가 포함됩니다. workflow의 `setup-python`이 Python 3.13을 설치하고 SDK 버전은 기존 workflow에서 고정합니다. 사용자 Python·Node·Java 빌드/테스트는 기존 Q/L2의 격리 컨테이너에서 수행하므로 runner에 Java나 앱용 Node 도구를 추가하지 않습니다. 이미지 게시 release job은 GitHub-hosted runner를 계속 사용하며 이 runner에는 AWS CLI·운영 kubeconfig·cloud 자격을 설치하거나 마운트하지 않습니다.

이 컨테이너는 신뢰된 CI 실행기를 포장합니다. **전용 VM이 기존 root 신뢰 경계**입니다. 같은 VM의 Docker socket과 host network, Docker 기본 capability에 추가한 `NET_ADMIN`이 필요합니다. socket은 CI VM의 root 권한에 해당하며 운영 호스트 socket과 공유할 수 없습니다. `NET_ADMIN`은 기존 root-owned helper의 실제 iptables 정책 검증에, host network는 격리된 L3 컨테이너 IP의 HTTP 검사에 필요합니다. runner에는 `privileged: true`나 host PID, `SYS_ADMIN`, seccomp 해제를 추가하지 않습니다. 사용자 코드에는 socket·runner 인증·운영 자격을 전달하지 않고 기존의 비root/읽기전용/자원제한 Q 컨테이너와 제한된 BuildKit/L3 네트워크를 유지합니다.

BuildKit은 새로 만들지 않습니다. 먼저 `infrastructure/ansible/ci.yml`을 실행한 기존 bootstrap이 `railshot-buildkit` 컨테이너와 `railshot-quality` bridge/firewall을 준비하고 실제 네트워크 검증 receipt를 남겨야 합니다. runner는 자신만의 Docker config에 remote Buildx 연결 정보만 만들고 같은 gate 코드로 기존 BuildKit의 이미지·network·자원제한·실행 flags를 확인합니다. bootstrap의 기존 BuildKit 권한 설정은 이 패키징에서 변경하지 않습니다.

## 준비와 실행

다음은 bootstrap을 마친 전용 CI VM의 저장소 루트에서 수행합니다. 호스트 준비 명령은 root가 필요하며 Kubernetes 설치 흔적을 발견하면 중단합니다. Docker Compose v2는 호스트에 별도로 설치되어 있어야 합니다.

```sh
sudo bash ci/scripts/runner/prepare-host.sh
docker build -f ci/scripts/runner/Dockerfile -t railshot-ci-runner:local .
docker run --rm railshot-ci-runner:local --check-image
```

운영자는 GitHub의 해당 **비공개 앱 저장소** runner 설정에서 짧은 유효기간의 registration token을 발급받아 root 소유·0600 일반 파일에 저장합니다. PAT·GitHub App private key를 대신 넣지 않습니다. 토큰은 저장소나 `.env`에 넣지 않고 shell 인수·로그에 출력하지 않습니다. 다음 환경 변수에는 파일 경로만 넣습니다. Compose 명령은 해당 파일과 Docker에 접근할 수 있는 운영자/root shell에서 실행합니다.

```sh
export RAILSHOT_RUNNER_TOKEN_FILE=/private/github-runner-registration-token
export RAILSHOT_RUNNER_URL=https://github.com/YOUR_ORG/YOUR_PRIVATE_APPS_REPO
export RAILSHOT_RUNNER_NAME=railshot-ci-vm01-attempt01
export RAILSHOT_RUNNER_LABELS=railshot-ci
export RAILSHOT_CI_RUNNER_IMAGE=railshot-ci-runner:local
docker compose -f ci/runner-compose.yml config --quiet
docker compose -f ci/runner-compose.yml up --no-build --force-recreate
```

게시된 이미지를 사용할 때는 `RAILSHOT_CI_RUNNER_IMAGE`에 검토한 digest를 지정하고 `docker compose -f ci/runner-compose.yml pull`을 먼저 실행합니다. 위의 로컬 build 명령은 생략할 수 있습니다. 이미지 빌드 성공과 `--check-image`는 runner 의존성 검사이며 GitHub 등록·실제 CI 실행·격리 검증 완료를 뜻하지 않습니다.

GitHub 앱 저장소에는 기존 workflow의 변수도 설정합니다.

| 저장소 변수 | 값/역할 |
| --- | --- |
| `RAILSHOT_CI_RUNNER_LABELS` | `["self-hosted","Linux","X64","railshot-ci"]`; runner의 추가 label과 일치 |
| `RAILSHOT_RUN_ROOT` | `/var/lib/railshot-runner/runs`; `_work`·`RUNNER_TEMP`와 분리된 재시도 상태 |
| `QUALITY_NETWORK` | `railshot-quality` |
| `PLATFORM_REF` | 설치한 executor 계약과 같은 검토된 platform commit 40자리 SHA |
| `AGENT_PROVIDER`, `AGENT_AUTH_MODE` | 기존 workflow 계약의 provider와 `subscription` 또는 `api-key` |
| `RAILSHOT_CODEX_HOME` | subscription일 때 `/var/lib/railshot-runner/codex`; 운영자가 해당 전용 디렉터리에 `auth.json` 준비 |

API key 모드는 기존 workflow의 GitHub Actions secret을 사용합니다. Codex subscription 디렉터리는 토큰 갱신을 위해 runner에만 쓰기 가능하게 연결되며 작업 workspace와 분리됩니다. API key 모드에서는 해당 디렉터리를 비워둡니다. AWS/GCP/Azure/OpenStack 자격, Docker registry login 설정이나 운영 KUBECONFIG는 이 VM/컨테이너에 전달하지 않습니다. CI VM instance role은 기존 bootstrap/관리용으로 제한하고 운영 cloud 권한을 부여하지 않습니다.

## 호스트 경로와 실패 조건

workspace(`/var/lib/railshot-runner/work`), run root(`/var/lib/railshot-runner/runs`), `TMPDIR`(`/var/lib/railshot-runner/work/_temp`)는 **컨테이너와 호스트의 절대경로가 같아야** 합니다. gate는 source와 L4의 임시 image archive를 호스트 Docker에 bind mount하므로 경로가 다르면 실행할 수 없습니다. Compose는 경로를 고정하고 시작 전 실제 `docker inspect`로 mount source/destination과 읽기·쓰기 모드를 검사합니다. `prepare-host.sh`가 만든 root-owned 전용 CI 마커, 호스트의 K3s 디렉터리, helper·executor 계약·실제 네트워크 검증 receipt도 확인합니다. 허용 목록 밖의 mount, 변경된 권한·네트워크·추가 capability는 등록 전에 거부합니다.

helper·executor 계약·receipt는 원래 절대경로에 읽기전용으로 연결합니다. helper의 lock 파일만 같은 경로에 쓰기 가능하게 공유하여 호스트 정책 변경과 검사가 동일한 lock을 사용합니다. Docker 재시작 후 receipt가 무효화됐으면 호스트의 기존 `railshot-ci-verify.service`/`test-ci-network`가 성공해야 합니다. helper나 이미지 계약을 갱신했으면 runner 컨테이너도 다시 생성합니다. 정책 실패를 우회해 runner를 등록하지 않습니다.

runner는 `--ephemeral`로 **한 job 뒤 등록을 해제하고 종료**하며 Compose의 restart는 `no`입니다. 다음 job은 새 registration token과 고유 runner name을 준비한 뒤 `up --force-recreate`로 새 컨테이너를 실행합니다. 자동 재등록용 PAT나 무한 재시작은 넣지 않습니다. 한 VM에서 이 Compose runner는 하나만 실행합니다. job 전에 중단된 runner는 GitHub 설정에서 잔여 등록을 확인해 제거한 뒤 다시 등록합니다. `work`와 `runs`는 호스트에 남아 재시도 증거를 보존하므로 필요 없는 실행 자료 삭제는 운영자가 별도로 관리합니다. `--disableupdate`를 사용하므로 GitHub가 요구하는 runner 업데이트 기한에 맞춰 base digest를 갱신하고 이미지를 다시 배포해야 합니다.

로컬 최소 검사:

```sh
python3 -m unittest discover -s ci/scripts/runner -p test_container.py
bash -n ci/scripts/runner/entrypoint.sh ci/scripts/runner/prepare-host.sh
```

실제 완료 판정에는 전용 Linux VM에서 bootstrap 네트워크 검증, ephemeral GitHub job, SDK의 native 읽기 제한, Q/L2/L4/L3를 통과한 evidence가 필요합니다. 컨테이너 기본 seccomp 때문에 SDK sandbox가 실행되지 않으면 실패를 보존하고 VM에서 원인을 확인하며 sandbox를 비활성화하지 않습니다. Argo CD와 K3s 고객 runtime은 기존 `gitops/` 및 `deployment/` 구성으로 관리합니다.

공식 근거: [Actions runner 2.337.0 Dockerfile](https://github.com/actions/runner/blob/v2.337.0/images/Dockerfile), [ephemeral runner와 업데이트 운영](https://docs.github.com/en/actions/reference/runners/self-hosted-runners).
