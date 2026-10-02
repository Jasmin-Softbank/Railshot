# OpenStack 고객 설치기 통합 검증

## 원본 보존

대상은 `integration/team-assembly-20261002@88d501b0659d7bfe2e3907fc0d701efc2feba655`, 원본은 `feat/openstack-client-bootstrap@4608f9512c6e3b10b7c42dded2882e72d4d60b70`이다. 원본 commit을 merge 이력으로 포함한다. feature가 추가한 38개 파일은 바이트와 Git mode를 그대로 유지하며 원본 브랜치에 push하지 않는다. README 충돌은 integration 내용을 유지하고 feature의 고객 설치기 안내를 추가해 해결했다.

## integration 조정

- 기존 OpenStack CI job에서 feature의 `ci/scripts/check-bootstrap.sh`를 Ubuntu 24.04 / Python 3.12로 실행한다. `requirements.lock`을 그대로 읽는다.
- 고객 설치기·agent 변경을 OpenStack 검사로 연결한다. 이 코드는 고객 Ubuntu 호스트에서 실행하므로 기존 dashboard/API/CI runner 이미지에 추가하지 않는다.
- 기존 Controller의 Ruff 범위는 `src tests`로 명시한다. 새 feature의 형식을 일괄 변경하지 않는다.
- feature의 `openstack/__init__.py` 추가로 pytest가 저장소 패키지를 외부 `openstacksdk`의 `openstack`으로 읽는 충돌을 확인했다. integration의 `pyproject.toml`에서 namespace와 importlib 수집을 설정하고, mypy 소스 기준을 `src`에 고정했다. Controller 구현과 feature 구현을 변경하지 않는다.

## 로컬 검증

- 원본 신규 파일 38개 바이트 동일성 확인.
- Controller: 243개 pytest 통과. SDK/Starlette deprecation warning은 기존 의존성에서 발생한다.
- 고객 설치기·제한 SSH 작업: 93개 테스트와 6개 하위 사례 통과.
- CI 경로 선택 9개, workflow 정책 10개 검사 통과.
- Controller Ruff/format 및 mypy는 기존 엄격한 규칙을 유지한다.

재현 명령:

```sh
uv sync --frozen --project infrastructure/providers/openstack --group dev
uv run --frozen --project infrastructure/providers/openstack pytest infrastructure/providers/openstack/tests -q
uv run --frozen --project infrastructure/providers/openstack --with-requirements deployment/bootstrap/requirements.lock bash ci/scripts/check-bootstrap.sh
python3 -m unittest discover -s ci/scripts -p test_ci_scope.py -v
```

## AWS control 클러스터의 현재 조건

2026-10-02 SSM 읽기 전용 점검: 계정 `721622471953`, 서울 리전, 기존 control `i-033ae2db907fde68e`. SSM 명령 ID `077ac911-1d32-4cd0-8a42-abedb8561aa5`.

- Ubuntu 24.04.5, K3s v1.34.11+k3s1, control·build worker 두 노드 Ready.
- dashboard/API Deployment 각각 1/1 Available. `https://railshot.io/healthz`는 TLS 검증과 함께 HTTP 200.
- 기존 WireGuard는 `wg-railshot`, UDP 51820, 터널 `10.254.0.0/30`이며 GCP `10.66.0.2/32` 경로를 사용한다. 보안그룹은 기존 GCP endpoint `34.47.68.21/32`에서 들어오는 UDP만 허용한다.
- 현재 제품 API 소스와 클러스터 Deployment/Service에는 feature가 요구하는 `/v1/enrollments` 등록 서비스가 없다. control 호스트에 터널 IP HTTPS probe 리스너도 확인되지 않았다.

이는 기존 서비스 상태 점검이다. 이번 merge를 운영 환경에 배포하거나 고객 OpenStack 설치에 성공했다는 증거가 아니다.

## 고객 연결에 필요한 서버 조건

feature의 [등록 규격](../api/enrollment-contract.md)과 [작업 규격](../api/job-contract.md)을 그대로 따른다.

1. 등록 서버는 정확히 `POST /v1/enrollments`, HTTP 200, 6개 응답 필드를 제공해야 한다. 일회용 키의 고객·만료·소비 상태와 같은 request ID/공개키의 응답 복구를 영속 관리해야 한다.
2. 고객 `jasmin0`는 `/32` 또는 `/128` 호스트 경로만 받는다. 기존 AWS VPC, Pod/Service CIDR, GCP 연결 경로와 겹치지 않는 터널 주소를 선택해야 한다. 기존 `wg-railshot` 연결을 고객 설치기로 덮어쓰지 않는다.
3. `probe_url`은 터널 IP의 HTTPS 주소여야 한다. 해당 IP SAN과 고객이 신뢰하는 CA가 필요하다. 인증서 검증을 끄거나 public hostname probe로 대체할 수 없다.
4. 고객 endpoint의 실제 출발지와 서버 UDP 포트를 정한 뒤 해당 출발지만 보안그룹에 추가해야 한다. 현재 GCP 전용 규칙으로 고객 연결이 된다고 간주하지 않는다.
5. 조회 송신기는 터널 인터페이스·출발지 바인딩, 고정 SSH 명령, 별도로 확인한 host key, 제한된 작업 키를 사용한다. OpenStack 인증정보는 고객 노드에만 둔다.

등록 서버 구현·배치와 고객별 peer/인증서/작업 키가 준비되기 전에는 초기 설치 E2E를 완료로 표시할 수 없다. 기존 제품 API Pod에 root, hostNetwork, NET_ADMIN 또는 고객 OpenStack 비밀정보를 추가해 이 호스트 계약을 우회하지 않는다.
