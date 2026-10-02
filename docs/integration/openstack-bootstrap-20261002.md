# OpenStack 고객 설치기 통합 검증

## 원본 보존

최초 기준은 `integration/team-assembly-20261002@88d501b0659d7bfe2e3907fc0d701efc2feba655`, 원본은 `feat/openstack-client-bootstrap@4608f9512c6e3b10b7c42dded2882e72d4d60b70`이다. 원본 commit을 merge 이력으로 포함한다. feature가 추가한 38개 파일은 바이트와 Git mode를 그대로 유지하며 원본 브랜치에 push하지 않는다. README 충돌은 integration 내용을 유지하고 feature의 고객 설치기 안내를 추가해 해결했다.

후속 integration `18bed30`(대시보드 PR #33)도 merge로 포함했다.

## integration 조정

- 기존 OpenStack CI job에서 feature의 `ci/scripts/check-bootstrap.sh`를 Ubuntu 24.04 / Python 3.12로 실행한다. `requirements.lock`을 그대로 읽는다.
- 고객 설치기·agent 변경을 OpenStack 검사로 연결한다. 이 코드는 고객 Ubuntu 호스트에서 실행하므로 기존 dashboard/API/CI runner 이미지에 추가하지 않는다.
- 기존 Controller의 Ruff 범위는 `src tests scripts`로 명시한다. 새 feature의 형식을 일괄 변경하지 않는다.
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

## AWS 경로의 실제 확인

Route53 public zone `Z01500273BKOZ113O8L34`의 `railshot.io`와 AWS 시험 앱 A alias는 `railshot-apps-1982466775.ap-northeast-2.elb.amazonaws.com`을 가리킨다. 공개 권한 DNS도 AWS nameserver 4개로 확인했다.

| 단계 | 플랫폼 | AWS 시험 앱 |
|---|---|---|
| 호스트 | `railshot.io` | `fixture-npm-js-8461c33ac524.railshot.io` |
| ALB HTTPS 443 규칙 | priority 400 | priority 100 |
| 대상 | `172.31.0.172:31080` | `172.31.13.147:30080` |
| Target health | healthy | healthy |
| 공개 HTTPS | `/healthz`: 200, TLS 검증 성공 | `/health`: 200, `{"status":"ready"}`, TLS 검증 성공 |

플랫폼 HTTP 80은 HTTPS 443으로 301 redirect한다. Route53 alias, ALB host rule, private target health와 실제 HTTP 응답을 각각 확인했다. AWS 대상 경로에는 기존 GCP WireGuard hop이 들어가지 않는다.

control 호스트에서는 별도 0700 임시 디렉터리와 Python 3.12 venv로 원본 설치기·agent 테스트 93개 및 하위 사례 6개를 다시 실행해 통과했다. 네트워크/클라우드 동작은 모의이며 실제 등록·OpenStack 인증을 실행한 것은 아니다. SSM ID: `f73838ed-5c48-4e12-b9ff-0f0c43077fc3`. 테스트 후 venv와 소스 제거를 확인했다. 후속 SSM `8adc4493-2cc7-4ed7-a91d-b80d95c1f79b`에서 노드/Deployment 상태와 NodePort 31080 → Ready endpoint도 재확인했다.

## 네트워크 전환 방향

2026-10-02 사용자 지시: WireGuard는 폐기 수순이며, `Route53 → ALB → AWS 클러스터` 경로 확인 후 `Cloudflare → {AWS ALB | GCP L7 LB | On-prem L7 LB}`로 이관한다. 위 AWS 경로를 현재 확인했다. Cloudflare 전환 자체는 이 PR에서 수행하지 않았다.

- WireGuard 기반 등록 서버와 신규 고객 peer를 이번 PR에서 만들지 않는다. feature 원본과 테스트는 그대로 보존한다.
- 원본 `client_setup.main.initialize`는 등록 및 WireGuard 연결을 먼저 요구하고 `apps/agent/sender.py`는 WireGuard 경로로 SSH를 보낸다. 따라서 DNS/L7 진입점 변경만으로 원본 설치기·작업 채널까지 전환됐다고 볼 수 없다.
- 후속 integration 연결부에서 고객 노드의 HTTPS 작업 수신/응답, 노드 인증, 재시도·중복 처리 계약을 별도로 정해야 한다. 고객의 로컬 OpenStack 자격증명과 제한된 작업 규격은 보존한다. 현재 구현된 것으로 표시하지 않는다.
- 기존 GCP 앱의 private target `10.66.0.2`는 아직 `wg-railshot`을 사용한다. GCP 자체 L7 LB 경로와 관리 통신 대체 경로를 검증한 뒤 해당 의존성을 제거한다. 이번 AWS 경로 확인만으로 기존 GCP 터널을 즉시 제거하지 않는다.

## 보존한 원본의 현재 제약

feature의 [등록 규격](../api/enrollment-contract.md)과 [작업 규격](../api/job-contract.md)은 원본 그대로다. 그 문서의 WireGuard 등록 서버 조건은 기존 구현을 설명하며, 위 신규 전환 방향의 구축 요구가 아니다. 원본의 고객 초기 설치 E2E는 미검증으로 유지한다. 기존 제품 API Pod의 root/hostNetwork/NET_ADMIN 권한을 늘리거나 고객 OpenStack 비밀정보를 서버로 옮기지 않았다.
