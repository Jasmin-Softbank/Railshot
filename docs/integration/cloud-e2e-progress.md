# AWS/GCP 클라우드 E2E 검증

2026-10-02 12:27 KST 실측. 이 문서는 관리자 실행 경로의 배포 검증이며 제품 UI에서 시작하는 전체 자동화 완료를 뜻하지 않는다.

## 실제 접속 결과

| 대상 | 공개 HTTPS | 결과 |
|---|---|---|
| AWS | https://fixture-npm-js-8461c33ac524.railshot.io/health | TLS 검증, HTTP 200, `{"status":"ready"}` |
| GCP | https://fixture-npm-js-3feba5a1b1cf.railshot.io/health | TLS 검증, HTTP 200, `{"status":"ready"}` |

Python 기본 CA 검증과 curl로 각각 확인했다. GCP HTTP 요청의 HTTPS 301 redirect도 확인했다. 자동화 Chrome은 새 도메인에서 `ERR_BLOCKED_BY_CLIENT`를 반환했으므로 브라우저 통과라고 기록하지 않는다. 루트 경로는 이 fixture의 서비스 경로가 아니며 `/health`가 실제 계약이다.

## 분리된 실행 환경

- 운영: 기존 AWS `railshot-control-poc`, t3.medium, K3s v1.34.11+k3s1 및 Argo CD v3.5.3. 운영 Pod CIDR 10.52.0.0/16, Service CIDR 10.53.0.0/16.
- CI 실행: 기존 `railshot-ci-k3s-aws`의 별도 격리된 host Actions runner/BuildKit. 이 VM에는 K3s를 설치하지 않았다. 운영 K3s와 신뢰하지 않는 빌드를 분리했다.
- AWS 앱: t3.large 한 노드, private 172.31.13.147, K3s+Cilium 1.20.2 Ready. 기존 VPC 172.31.0.0/16의 2a subnet.
- GCP 앱: e2-standard-2 한 노드, private 10.66.0.2, K3s+Cilium 1.20.2 Ready. Seoul asia-northeast3-a, 별도 10.66.0.0/24 subnet.
- 공유 ALB: 기존 AWS 2a·2b subnet, AWS/GCP 모두 사설 IP target. 대상별 NodePort 30080, `/health`, 두 target 모두 Healthy.
- GCP 경로: ALB subnet route table의 10.66.0.2/32 → 운영 ENI → WireGuard → GCP. 운영 ENI source/destination check 해제, AWS/GCP 고정 endpoint 사이 UDP 51820만 허용했다. 별도 NAT·CDN·게이트웨이 VM은 생성하지 않았다.

WireGuard는 각 host에서 개인키를 생성하고 공개키만 교환했다. 개인키와 설정은 원격 root 소유 0600이며 Git에 없다. 실제 handshake·private route·Argo 연결·ALB health·공개 HTTPS를 별도로 확인했다. 이번 전달은 기존 K3s/Cilium forwarding과 앱 NetworkPolicy를 재사용했고 추가 광역 NAT 규칙은 넣지 않았다.

## CI → CD 증거 연결

| 항목 | AWS | GCP |
|---|---|---|
| CI run | [36958562111](https://github.com/Jasmin-Softbank/railshot-apps/actions/runs/36958562111) | [36959591722](https://github.com/Jasmin-Softbank/railshot-apps/actions/runs/36959591722) |
| CI source | `41d2255b1a253952bbf3b04d621264e079feac3c` | 동일 |
| GitOps revision | `6795df667ccaf5b964ced350267012c041ba81e8` | `1bc51ec97bb1b5c1e2078ac93c43c00dcf620734` |
| Argo 결과 | Synced / Healthy / Succeeded | Synced / Healthy / Succeeded |
| 실제 Pod | Running / Ready | Running / Ready |

두 실행의 trusted platform ref는 `32168f3`이다. 게시한 이미지와 실제 두 Pod의 imageID는 모두 `ghcr.io/jasmin-softbank/demo-fixture-npm-js-web@sha256:48cf1c0ab9c4251cf60c41256f96a45bf6a9430a1e7e42bdbbf396a26fde5b86`로 일치한다. 대상별 published receipt와 immutable workload Git 경로를 대조한 뒤 Argo가 앱을 적용했다. 팀 `deploy.sh`로 앱을 별도 적용하지 않았다.

Private registry는 유지했다. 전용 workflow [36958492793](https://github.com/Jasmin-Softbank/railshot-apps/actions/runs/36958492793)의 두 번째 실행이 OIDC → 제한된 SSM SecureString 전달에 성공했다. 첫 실행의 신뢰 조건 오류는 GitHub immutable subject의 실제 조직·저장소 ID를 반영해 수정했다. Namespace의 pull Secret은 신뢰한 SSH stdin으로 전달했고 토큰을 명령 인자·SSM 명령 로그·Git·Terraform state에 넣지 않았다.

Argo는 앱 namespace에 한정한 ServiceAccount와 Deployment/Service/NetworkPolicy 권한을 사용한다. 토큰은 6시간으로 제한했다. Argo 3.5.3의 status.summary 생략은 동일 sync revision의 정확한 Deployment images를 확인하는 분기로 처리하고 회귀 검사를 추가했다.

## Ansible API와 실행 계약

`infrastructure/ansible/api.py`는 localhost 전용 `POST /v1/ansible/jobs`, `GET /v1/ansible/jobs/{request_id}`를 제공한다. 등록된 target, 0600 bearer 파일, 요청 digest, 영속 상태, 대상 잠금과 기존 CLI를 재사용한다. 같은 요청은 재실행하지 않고 저장 결과를 반환하며 충돌·중단 상태를 구분한다.

실제 AWS `guest.check`는 HTTP 202 → succeeded/guest_ready=true로 완료했다. 같은 request_id 재전송은 HTTP 200과 기존 생성 시각을 반환했다. AWS runtime 설치는 SSM→strict SSH, GCP는 IAP→strict SSH로 실행해 각각 성공했다. 재실행 가능한 arbitrary playbook·shell·임의 target 등록은 API에 없다.

HTTP API의 실제 검증 위치는 관리자 로컬 controller다. 운영 K3s의 API 서비스 설치, 제품 API/작업 큐 연결, provider 자격의 운영 서버 위임은 아직 수행하지 않았다. Terraform plan/apply, Ansible, CI dispatch, GitOps commit/sync는 이번에 관리자가 각 경계를 호출해 연결했다. 이를 유저 클릭 한 번의 자동 배포 완료로 확대하지 않는다.

## 도메인 정책과 운영 범위

`railshot.io`를 Porkbun에서 1년 구매했고 Route53으로 NS 위임 및 ACM wildcard DNS 검증을 완료했다. `gitops/service_name.py`는 입력 이름을 DNS slug로 정규화하고 `[owner, name, environment]`의 SHA-256 앞 12자를 붙인다. 같은 서비스 identity는 재배포 후에도 주소를 유지하며 AWS/GCP PoC 환경은 서로 다른 suffix를 사용한다. 이 이름 함수의 제품 API 호출 연결은 별도 통합 범위다.

테스트 비용 제한: CI VM은 17:12 KST, AWS 앱 노드는 19:30 KST에 정지하도록 설정됐다. GCP는 각 start 후 8시간에 STOP한다. 정지 후 디스크·공인 IP·ALB·Route53 비용은 남는다. AWS 앱 정지 timer는 해당 boot에 한정하며 restart 후 재설정이 필요하다. Argo 등록 토큰은 6시간, GHCR pull PAT는 2026-10-09 만료다. 장기 운영 전 토큰 자동 갱신·재기동 검증·state 원격 잠금/백업을 연결해야 한다.

## 저장소와 명세

전체 팀 runtime은 `beed099`에서 별도로 반입했다. 개인 담당 공통 CI/CSP/network/Ansible API/GitOps 연결 변경은 `32168f3`, `0ab08b2`로 분리했다. 실제 fixture workload는 통합 브랜치에만 보존하며 개인 브랜치에는 팀 deployment 구현을 복사하지 않는다. 원본 checkout의 다른 문서·Terraform 미커밋 변경은 보존했다.

완성된 Notion [01 네트워크](https://app.notion.com/p/3ed8bee9ada481659fe2c48c520c5c0f), [07 Ansible](https://app.notion.com/p/3ed8bee9ada4812dada2db68a9d0fec2), [08 Argo](https://app.notion.com/p/3ed8bee9ada4818eb86efaf609889d4c)를 읽고 최신 구현과 대조했다. 이전 966741d 기준 미구현 문구와 현재 IP target 차이를 문서 담당 채팅에 전달했다. CDN은 선택 사항이며 이번 검증 경로에 추가하지 않았다.

후속 변경 검사: Ansible 25개, GitOps 9개, Terraform provider tools 40개 통과. Terraform AWS/GCP/edge validation과 개별 saved plan 검토 후 실제 apply를 수행했다. native 결과와 원본 artifacts, private state/plan·credentials·비용 자료는 Git 밖 `/Users/mango/.local/share/railshot/cloud-e2e-20261002`에 보존한다. 주요 결과 파일: `aws-public-http.json`, `gcp-public-http.json`, `alb-target-health.json`, `aws-argo-verified.json`, `gcp-argo-result.json`, 두 `*-pod-observation.json`, `aws-http-api-result.json`.
