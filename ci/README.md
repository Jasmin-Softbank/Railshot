# CI와 이미지 게시

`workflows/railshot-deploy.yml`은 private apps 저장소에서 사용할 workflow 템플릿이다. `loop`와 `release` 두 job만 포함한다. 업로드 원본 → baseline gate → 필요한 adapter/fixer 수정 → 전체 gate → 검증한 이미지 bundle → GHCR 게시를 담당한다. UI·Provider API·클러스터 설치·CD·DB·공개 ingress는 각 담당 영역에서 연결한다. 공통 설계와 역할은 루트 README를 따른다.

## 실행 경계

- `loop`: 임대한 전용 CI worker에서 실행한다. 관리자가 고정한 `PLATFORM_REF`, 입력 경로, 영속 `RAILSHOT_RUN_ROOT`, 설치 네트워크와 SDK 인증 경로를 검사한다. 부분 gate 성공이나 SDK의 자체 보고로 게시를 허용하지 않는다.
- `release`: 보호된 `railshot-release` environment의 hosted worker에서 실행한다. producer가 반환한 bundle artifact ID로 다운로드하고 `bundle.py publish`로 검증한 이미지만 게시한다. 사용자 source를 다시 빌드하거나 실행하지 않는다.
- GHCR prefix·visibility·게시 자격은 trusted release 설정이다. 업로드와 모델 출력에서 받지 않는다. 기본 private 모드는 대상의 pull 자격 설치·검증 연결이 없으므로 게시 전에 차단한다. 명시적 public 모드는 이미 public인 package만 지원하며 실제 digest를 별도 빈 인증 설정으로 조회한다.
- CI worker 등록, 인증 준비와 target runtime 준비가 완료됐다는 뜻은 아니다. 현재 템플릿의 로컬 테스트는 실제 GitHub workflow 실행·registry 게시·배포 성공의 증거가 아니다.

## CD에 전달하는 산출물

| 산출물 | 식별과 내용 |
|---|---|
| `release-bundle-<run_attempt>` | `loop.outputs.bundle_id`; 검증한 `images.tar`, spec, verdict와 SHA-256 manifest |
| `published-<run_attempt>` | `release.outputs.published_id`; `images.json`의 원격 digest, 원본 `jasmin.yaml`, `verdict.json`, `manifest.json` 및 출처 영수증 `handoff.json` |
| bundle 연결 | `release.outputs.bundle_id`는 소비한 원본 artifact ID. manifest는 source digest와 spec/verdict/image archive 해시를 보존 |

소비자는 workflow run 및 producer artifact ID를 기준으로 읽어야 한다. 고정 `rendered` alias나 가장 최근 이름으로 대체하지 않는다. 이 산출물은 게시된 이미지와 CI 검사 근거이며 URL·클러스터 상태·배포 성공을 포함하지 않는다. CD 구현과 완료 조건은 CD 담당이 결정한다.

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

source_commit/target_id를 workflow 입력으로 받아 checkout 및 운영자 target과 일치하는지 검사한다. 게시 ZIP은 `images.json`, `jasmin.yaml`, `verdict.json`, `manifest.json`, `handoff.json`의 flat 구조다. 기존 네 evidence 파일은 byte 그대로 유지하며 handoff.json이 source/run/attempt/target과 파일 해시를 연결한다. API는 실제 producer artifact ID로 읽고 published 상태만 표시한다. 자세한 계약은 [CI publication](../docs/api/ci-publication.md)을 따른다.
