# 실제 클라우드 Smoke Test 점검 — 2026-10-02

Smoke test(실제 환경의 짧은 배포 확인)를 준비했지만, 이 실행 환경에서는 AWS/GCP Runtime 배포를 실행하지 못했습니다. **준비 노드가 없다고 확인한 결과가 아닙니다.** 팀 문서에는 두 노드가 존재한다고 기록되어 있으며, 현재 controller(명령을 실행하는 Mac)의 접근 가능 여부와 분리합니다.

## 현재 실행 결과

| 항목 | AWS | GCP |
| --- | --- | --- |
| 인증 확인 | `aws sts get-caller-identity` 성공 | gcloud 실행 파일과 기본 설치 위치·설정 디렉터리를 찾지 못함 |
| 현재 인증 상태 | ready; `for-study` 사용자 | not-ready / 현재 환경에서 확인 불가 |
| 문서상 기존 노드 | private 172.31.13.147, K3s+Cilium Ready로 보고됨 | private 10.66.0.2, asia-northeast3-a, K3s+Cilium Ready로 보고됨 |
| 이번 노드 실시간 조회 | `ec2:DescribeInstances` 권한 없음 | project/node API 조회 미실행 |
| Runtime deploy | NOT_RUN | NOT_RUN |
| K3s / Cilium / workload | NOT_RUN | NOT_RUN |
| Internal HTTP(노드 내부 응답) | NOT_RUN | NOT_RUN |
| verify / cleanup / redeploy | NOT_RUN | NOT_RUN |
| 실행 상태 | `BLOCKED_AWS_READ_PERMISSION` | `BLOCKED_GCP_AUTH_NOT_READY` |
| 신규 Runtime cloud 시나리오 | SKIP | SKIP |

AWS에서는 `ap-northeast-2`에서 이름이 `railshot*`인 기존 노드의 최소 정보만 조회하려고 했습니다. `UnauthorizedOperation`으로 거부되어 노드의 부재·실시간 Ready 여부를 판단하지 못했습니다. 인증 성공은 노드 조회·SSM(원격 관리 연결)·SSH(원격 명령 실행) 권한을 뜻하지 않습니다. 다른 인증 profile로 전환하거나 권한을 변경하지 않았습니다.

GCP의 known project는 `railshot-poc-20261001`, region은 `asia-northeast3`, zone은 `asia-northeast3-a`입니다. 현재 환경에 gcloud 또는 검증된 원격 연결 계약이 없어 인증·project·VM 상태를 확인하지 못했습니다. 팀 문서의 `/Users/mango/...` 관리자 자료는 이 Mac의 자료가 아니므로 읽은 것으로 간주하지 않습니다.

## 기존 공개 서비스 관측

아래는 이번에 실제 HTTPS GET(보안 연결을 통한 읽기 요청)으로 확인했습니다. TLS(연결 암호화와 인증서) 검증을 유지했고 timeout은 8초였습니다.

| 대상 | URL | 이번 결과 |
| --- | --- | --- |
| AWS 기존 앱 | https://fixture-npm-js-8461c33ac524.railshot.io/health | HTTP 200, `{"status":"ready"}` |
| GCP 기존 앱 | https://fixture-npm-js-3feba5a1b1cf.railshot.io/health | HTTP 200, `{"status":"ready"}` |

[Machine-readable 결과](review/current-summary.json)의 `existing_public_http`에 응답과 측정 범위를 보존했습니다. 이 관측은 기존 팀 앱의 external exposure(외부 접근) 확인입니다. 이번 Runtime의 deploy/verify/cleanup PASS, 노드 내부 HTTP, IAM(클라우드 권한) 검증으로 확대하지 않습니다.

기존 노드와 앱 배포 방식은 [팀 클라우드 기록](../../../../docs/integration/cloud-e2e-progress.md)에 있습니다. 해당 기록에서는 앱을 Argo(선언된 배포 상태를 맞추는 도구)로 적용했으며, 이번 `deploy.sh`의 재실행 증거가 아닙니다.

## 팀에서 전달할 준비 정보

1. 기존 노드의 resource ID, provider, region/zone, Linux/CPU 구조, 현재 접속 가능한 상태를 확인합니다. AWS SSM 또는 GCP IAP(인증된 접속 경로), SSH 사용자·identity 파일·신뢰한 host key를 관리자 실행 계층에서 준비합니다. Secret/token/private key는 Git이나 결과 로그에 넣지 않습니다.
2. 테스트를 허용한 대상과 namespace(앱 리소스의 구역)·NodePort(노드 공개 포트)를 지정합니다. 현재 팀 서비스는 NodePort 30080을 사용하므로 예제를 그대로 실행하면 충돌할 수 있습니다. 기존 Argo 관리 앱과 `railshot-workload`의 소유권을 분리해야 합니다.
3. Runtime code와 필요 시 architecture에 맞는 bundle·신뢰한 manifest SHA256(무결성 값)을 전달합니다. 현재 arm64 bundle을 amd64 노드에 사용하지 않습니다.
4. 상위 실행 계층이 JSON 입력을 만들고 대상 Linux에서 `deploy.sh` → `verify.sh`를 실행하여 stdout JSON·exit code·stderr를 수집합니다. Cold install(최초 설치)에는 기존 허용 입력인 `runtime.timeout_seconds: 600`을 명시하는 방안을 검토합니다. 기본값을 바꾸지는 않았습니다.
5. Internal HTTP와 상위 controller의 external HTTP를 따로 기록합니다. 외부 접근 실패 시 Provider 담당이 라우팅·방화벽을 점검합니다. Runtime에서 SG/VPC/Firewall을 수정하지 않습니다.
6. 테스트 소유 workload에 한해 cleanup → redeploy를 검증합니다. 공유 클러스터에서 `--all --disposable-node`를 실행하지 않습니다. 전체 삭제는 팀이 명시적으로 지정한 폐기 가능한 전용 노드에서만 사용합니다.

EC2/GCE 생성·시작, Terraform, IP 생성, SG/VPC/Firewall 변경, 권한 부여, credentials 전달은 이번에 수행하지 않았습니다. cloud Runtime 테스트는 접근 계약이 준비된 뒤 실제 실행해야 합니다.
