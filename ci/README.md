# CI와 이미지 게시

## Railshot 저장소의 PR 검사

사용자 앱을 게시하는 아래 템플릿과 별도로, [Railshot CI](../.github/workflows/railshot-ci.yml)는 PR과 `integration/**` push에서 플랫폼 통합본을 검사한다. GitHub가 workflow를 인식하는 `.github/workflows/`에는 실행 연결만 두고 검사 구현은 `ci/`와 기존 담당 경로에서 재사용한다.

| 검사 | 실행 범위 |
|---|---|
| Python / OpenStack contracts | 기존 CI·Ansible·runtime·GitOps·provider 검사와 OpenAPI. observability는 실제 promtool 설정 검사와 loopback HTTP 200/503/redirect probe를 포함. 모델·외부 API는 mock |
| HTTP publication and browser E2E | 실제 HTTP ZIP 업로드 → Python 게시 산출물 → API 읽기. GitHub·registry는 mock. Chromium은 실제 dashboard 선택·검토·탐색을 실행 |
| Terraform validation | 모든 현재 모듈의 fmt/init/validate 및 기존 계약 검사. credentials/backend/apply 없음 |
| Database Ansible contracts | 팀 DB playbook 9개의 native syntax/lint, 실제 localhost 입력·템플릿 생성 15개, 기존 guest/runtime syntax. DB 서비스·복제·장애 전환 실행은 아님 |
| Linux amd64 runtime E2E and cleanup | 일회성 GitHub runner에 실제 K3s/Cilium 설치 → Pod·서비스·HTTP 검증 → 소유 자원과 클러스터 정리 |
| Railshot CI gate | 위 검사가 모두 성공해야 통과하는 고정 이름의 합산 검사 |

PR 검사에는 cloud·모델·private registry 자격을 제공하지 않는다. 브라우저 검사는 현재 선택·검토 UI가 요청을 아직 전송하지 않는다는 사실도 확인한다. 통과를 제품 UI에서 AWS/GCP로 자동 배포한 결과로 해석하지 않는다. Vercel preview는 별도 연동이다.

실패 브라우저 trace·스크린샷과 runtime 요청·단계 결과·정리 영수증은 run/attempt별 artifact로 7일 보존한다. runtime은 `finally`와 workflow `always()`에서 정리하며, 정리 실패도 gate를 실패시킨다. [E2E 배포 해제 경로](../docs/integration/e2e-teardown.md)에 재시도와 실제 클라우드 자원 정리 범위를 기록했다.

로컬 Python 의존은 `python -m pip install -r ci/requirements-test.txt`, 브라우저 검사는 [ci/browser](browser/README.md)를 따른다. runtime wrapper는 기존 개발·운영 노드에서 실행하지 못하도록 hosted runner·소유권을 검사한다.

`workflows/railshot-deploy.yml`은 private apps 저장소에서 사용할 workflow 템플릿이다. `loop`와 `release` 두 job만 포함한다. 업로드 원본 → baseline gate → 필요한 adapter/fixer 수정 → 전체 gate → 검증한 이미지 bundle → GHCR 게시를 담당한다. UI·Provider API·클러스터 설치·CD·DB·공개 ingress는 각 담당 영역에서 연결한다. 공통 설계와 역할은 루트 README를 따른다.

## 실행 경계

- `loop`: 임대한 전용 CI worker에서 실행한다. 관리자가 고정한 `PLATFORM_REF`, 입력 경로, 영속 `RAILSHOT_RUN_ROOT`, 설치 네트워크와 SDK 인증 경로를 검사한다. 부분 gate 성공이나 SDK의 자체 보고로 게시를 허용하지 않는다.
- `release`: 보호된 `railshot-release` environment의 hosted worker에서 실행한다. producer가 반환한 bundle artifact ID로 다운로드하고 `bundle.py publish`로 검증한 이미지만 게시한다. 사용자 source를 다시 빌드하거나 실행하지 않는다.
- GHCR prefix·visibility·게시 자격은 trusted release 설정이다. 업로드와 모델 출력에서 받지 않는다. 기본 private 모드는 전용 pull 자격과 운영자가 정한 namespace·Secret 이름이 있어야 게시하며, 별도 임시 인증 설정으로 각 digest의 manifest를 조회한다. 명시적 public 모드는 이미 public인 package를 빈 인증 설정으로 조회한다. 어느 모드도 package 공개 설정을 변경하지 않는다.
- CI worker 등록, 인증 준비와 target runtime 준비가 완료됐다는 뜻은 아니다. 현재 템플릿의 로컬 테스트는 실제 GitHub workflow 실행·registry 게시·배포 성공의 증거가 아니다.

## CD에 전달하는 산출물

| 산출물 | 식별과 내용 |
|---|---|
| `release-bundle-<run_attempt>` | `loop.outputs.bundle_id`; 검증한 `images.tar`, spec, verdict와 SHA-256 manifest |
| `published-<run_attempt>` | `release.outputs.published_id`; `images.json`의 원격 digest, 원본 `railshot.yaml`, `verdict.json`, `manifest.json` 및 출처 영수증 `handoff.json` |
| `publish-journal-<run_attempt>` | 같은 run의 게시 복구용 `publish.json`만 보존. 자격·Docker 설정·진단 로그는 포함하지 않음 |
| bundle 연결 | `release.outputs.bundle_id`는 소비한 원본 artifact ID. manifest는 source digest와 spec/verdict/image archive 해시를 보존 |

소비자는 workflow run 및 producer artifact ID를 기준으로 읽어야 한다. 고정 `rendered` alias나 가장 최근 이름으로 대체하지 않는다. 이 산출물은 게시된 이미지와 CI 검사 근거이며 URL·클러스터 상태·배포 성공을 포함하지 않는다. CD 구현과 완료 조건은 CD 담당이 결정한다.

게시 재실행은 같은 run의 journal과 GitHub job 이력을 읽는다. journal은 repository·run·source commit·target·PLATFORM_REF·원본 bundle artifact ID·manifest hash에 묶이며 다른 입력으로 복원할 수 없다. 이전 게시 단계가 명시적으로 건너뛰어진 경우에만 새 push를 허용한다. 게시가 시작됐거나 이력이 불완전하면 확인하지 못한 이미지는 registry에서 읽어 검증하고 다시 push하지 않는다. worker 소실로 journal이 업로드되지 않아도 이 규칙은 유지한다. 원격 이미지를 확인할 수 없으면 불확실 상태로 멈춘다.

bundle과 게시 journal의 보존 기간은 1일이다. bundle이 만료되면 release만 재실행할 수 없으므로 새로운 CI 실행에서 전체 검사를 다시 해야 한다. private manifest 조회 성공은 고객 노드의 pull·Secret 설치·Pod Ready를 뜻하지 않으며, 이 검증은 CD에서 이어간다.

## 보존한 AWS 선택 구현

`scripts/codebuild.py`, `scripts/codebuild_release.py`, `workflows/codebuild-release.yml`은 별도의 관리자용 CodeBuild/ECR publisher다. 승인한 bundle을 private S3로 전달하고 고정 trusted source로 게시한다. 제품의 기본 registry/driver로 강제하지 않는다. 공통 `ci/scripts/gate/bundle.py`를 재사용하며 작업 dispatch 불확실성을 journal로 보존한다. 테스트는 AWS CLI 경계를 mock하며 실제 클라우드 호출을 하지 않는다.

## 로컬 검증

필요한 Python 환경에는 PyYAML, jsonschema, 기존 고정 SDK가 있어야 한다. SDK contract 테스트는 SDK 객체를 mock하며 모델을 호출하지 않는다.

```sh
python -m unittest discover -s ci/scripts -p 'test_*.py'
python -m unittest discover -s ci/scripts/gate -p 'test_*.py'
python -m unittest discover -s ci/scripts/loop -p 'test_*.py'
python -m unittest discover -s ci/scripts/runner -p 'test_*.py'
```

[현재 CI 게시 계약](../docs/api/ci-publication.md)과 [과거 snapshot 검증 기록](https://github.com/Jasmin-Softbank/Jasmin/blob/9e13c7c2e50b003c146852ccf22c9e9399271cf7/platform/scenarios/ci-validation.md)을 구분한다. 개인 Argo/CNPG/KEDA renderer·CD observer·reachability job은 제거했다. 과거의 해당 테스트 결과는 현재 실행 범위를 증명하지 않는다.

## 통합 입력과 게시 계약

source_commit/target_id를 workflow 입력으로 받아 checkout 및 운영자 target과 일치하는지 검사한다. 게시 ZIP은 `images.json`, `railshot.yaml`, `verdict.json`, `manifest.json`, `handoff.json`의 flat 구조다. 기존 네 evidence 파일은 byte 그대로 유지하며 handoff.json이 source/run/attempt/target과 파일 해시를 연결한다. API는 실제 producer artifact ID로 읽고 published 상태만 표시한다. 자세한 계약은 [CI publication](../docs/api/ci-publication.md)을 따른다.

운영자 변수 `RAILSHOT_TARGET_IDS`에 `["k3s-aws","k3s-gcp"]`처럼 허용할 target을 JSON 배열로 등록할 수 있다. 이 변수가 없을 때만 기존 `RAILSHOT_TARGET_ID` 한 개를 사용한다. 빈 배열·중복·잘못된 ID는 차단하며, loop와 release가 동일한 검증 함수를 호출한다. target 선택으로 registry·게시 자격·pull Secret 정책을 바꿀 수는 없다.

## Private pull 자격 배송

`workflows/railshot-pull-credential.yml`은 `Jasmin-Softbank/railshot-apps`의 보호된 `railshot-release` 환경에서 수동 실행하는 별도 workflow다. 같은 환경의 `GHCR_PULL_TOKEN`을 GitHub OIDC로 AWS 계정 `721622471953`, 서울 리전의 `/railshot/registry/ghcr/pull_token`에 `SecureString`으로 전달한다. 운영자 변수는 `GHCR_PULL_SYNC_ROLE_ARN`, `GHCR_PULL_USERNAME`이며 사용자가 role·region·parameter 경로를 입력할 수 없다. OIDC role은 해당 저장소·environment subject만 신뢰하고, 이 parameter의 `ssm:PutParameter`만 허용해야 한다. workflow도 동일한 session policy를 적용하며 SSM 읽기 권한은 갖지 않는다.

토큰은 environment secret → 0600 임시 JSON → AWS CLI 파일 입력으로만 전달하고, 성공·실패·시간 초과 모두 파일을 삭제한다. 토큰을 CLI 인수·로그·artifact에 넣지 않는다. 운영 노드는 별도 IAM으로 이 parameter 하나의 `GetParameter`만 허용받아 대상 namespace의 pull Secret을 설치한다. 이 workflow의 성공은 SSM 저장 접수이며 실제 노드의 private image pull은 별도로 검증한다.

현재 관리 중인 GHCR 자격은 classic PAT의 `read:packages` 권한만 가지며 기록된 만료일은 **2026-10-09**다. 만료 전에 보호된 GitHub secret을 교체하고 이 workflow를 다시 실행한 뒤 대상 Secret과 실제 pull을 확인한다. SSM parameter의 설명은 만료 메타데이터이며 토큰을 자동 갱신하지 않는다.

## 고객 앱 CI의 기본 실행량

기본 경로는 L0(변경·비밀 경계) → L1(배포 명세) → L2(이미지 빌드) → L4(보안 검사) → L3(실제 기동·HTTP)다. 별도 lint/type/unit Q는 기본 배포에서 실행하지 않는다. `--layers L0,L1,Q,L2,L4,L3`로 명시하면 기존 진단을 실행하며, 이전 6단계 bundle도 계속 검증한다.

표준 단일 Vite 앱은 lockfile과 기존 build 명령을 보존하는 템플릿으로 패키징한다. 계산기처럼 이 조건에 맞으면 AI 호출 없이 baseline을 실행한다. 사용자 Dockerfile·명세, Go 서버, SSR·사용자 출력 디렉터리는 자동으로 덮어쓰지 않는다. 나머지는 실패할 때만 기본 1회, Codex medium 추론으로 packaging 수정을 시도한다. 운영 변수 `REPAIR_SCOPE=source`나 `RAILSHOT_MAX_REPAIR_ATTEMPTS`를 설정한 저장소는 명시한 값이 우선한다.

Memos의 Go 서버와 영구 `/var/opt/memos` 저장소는 정적 Vite 앱 조건에 해당하지 않는다. CI 축소는 영구 볼륨 지원을 추가하지 않으며, 임시 디스크로 대체하여 배포 성공으로 처리해서는 안 된다.
