# RailShot 공통 Deployment Runtime 실제 검증

2026-10-02(KST)에 새 Linux VM에서 JSON 입력 기반의 K3s·Cilium·workload·Service·HTTP 경로를 검증했습니다. **최초 설치 성공, 첫 자동 실행 23 PASS, 확장한 최종 실행 29 PASS, 최신 코드 배포·검증 성공, 로컬 검사 16 PASS**를 확인했습니다.

## 재사용·수정·신규 구현 판단

| 구분 | 적용 내용 |
| --- | --- |
| 재사용 | 기존 PoC의 Linux 사전 조건, 관리하지 않는 K3s 설치 거부, 기본 Flannel·network-policy 비활성화, 공식 Cilium CLI checksum 검증, 상태 확인, 공식 uninstall 절차를 가져왔습니다. |
| 수정 | 고정 namespace·이미지·본문을 공통 workload 설정으로 변환했습니다. 소유권 검사, 일반 앱의 HTTP 검사, 명령 시간 초과 시 자식 프로세스 종료, JSON 실패 결과를 추가했습니다. |
| 신규 | Input Adapter, 불변 DeploymentSpec, provider에 의존하지 않는 Deployment Engine, DeploymentResult, 입력·출력 schema, namespace/Deployment/Service JSON 템플릿, provider fixture와 자동 검사를 구현했습니다. |

기존 `deployment-poc` 코드를 이동하거나 수정하지 않았습니다. 새 `feature/deployment-runtime-seungmin`은 구조 합의가 기록된 `chore/railshot-structure`에서 시작했습니다. **이번 구현 변경은 모두 `deployment/` 아래입니다.** 기능 코드를 다른 책임 영역에 추가하지 않았습니다.

## 실행 환경과 증거

- 별도 Lima VM(맥에서 실행하는 Linux 가상 컴퓨터): `railshot-runtime-test`입니다.
- Ubuntu 24.04 / arm64 / Linux 6.8.0-134 / CPU 2개 / RAM 4 GiB / 디스크 20 GiB입니다.
- 설치 전 `/usr/local/bin/k3s`, `/var/lib/rancher/k3s` 부재를 확인했습니다. [환경 및 신규 상태](environment.log)
- K3s `v1.34.11+k3s1`, Cilium `1.20.2`, CLI `v0.20.1`을 사용했습니다.
- [최초 설치 JSON](clean-install.json), [최초 설치 로그](clean-install.log), [최종 클러스터 상태](final-state.log)를 보존했습니다.
- 기존 시연 VM은 수정하지 않았고, Mac 포트 30080의 정상 응답도 별도 확인했습니다. [기존 시연 응답](original-demo-http.log)

## 실제 수행한 검사

| 검사 | 실제 동작과 결과 |
| --- | --- |
| 최초 설치 | K3s 부재 확인 → 설치 → Cilium → nginx → HTTP 200·샘플 본문 확인에 성공했습니다. |
| K3s·Cilium 재실행 | AWS/GCP/OpenStack 입력으로 동일 설치 절차를 반복했습니다. Deployment·Pod 고유 번호가 유지됐습니다. |
| workload 배포·업데이트 | nginx `1.28.0-alpine → 1.28.1-alpine` 교체, rollout 완료, endpoint 정상화 후 기존 입력으로 복원했습니다. |
| 일반 앱 | 별도 namespace에서 `nginxinc/nginx-unprivileged:1.28.0-alpine`, 복제본 2개, 포트 8080, `/index.html` 경로를 배포·검증했습니다. 샘플 본문과 이미지 내부의 wget/curl에 의존하지 않았습니다. |
| 없는 이미지 | 실제 이미지 다운로드 실패를 만들고 `WORKLOAD_DEPLOYING` 오류와 원인 진단을 반환한 뒤 복원했습니다. |
| health 실패 | `/missing-healthz` HTTP 404로 새 앱 준비가 실패하는 것을 확인하고 JSON 실패와 이벤트를 검증한 뒤 복원했습니다. |
| 잘못된 Service | 실제 targetPort를 81로 변경하고 `SERVICE_DRIFT`를 감지한 뒤 정상 선언을 재적용했습니다. |
| Cilium 장애 | Cilium agent 이미지를 잘못된 값으로 교체하고 `CILIUM_READY` 단계 실패를 감지했습니다. 원래 이미지로 복원하고 상태·HTTP를 다시 확인했습니다. |
| 소유권 | 관리 표식이 없는 namespace에 대해 verify와 cleanup이 `OWNERSHIP_CONFLICT`를 반환했습니다. 외부 구역을 변경·삭제하지 않았습니다. |
| 최소 통신 정책 | 적용 전 두 클라이언트가 HTTP 200을 받는지 확인했습니다. 적용 후 승인한 클라이언트만 HTTP 200을 받고 다른 클라이언트는 시간 초과되는 것을 확인했습니다. 이후 정책과 시험 Pod를 제거했습니다. |
| 일반 cleanup | 지정 앱만 제거했고 별도 namespace의 일반 앱은 유지됐습니다. 두 번 제거해도 성공했으며 이후 재배포했습니다. |
| 전체 cleanup | 전용 노드 표식을 요구했습니다. Cilium·K3s 제거와 핵심 파일 부재를 확인했고 반복 제거도 성공했습니다. 이어서 다시 설치하고 HTTP를 확인했습니다. |
| 외부 클라이언트 | Mac에서 VM NodePort로 연결하는 포트 30082에 HTTP 200·정상 본문을 확인했습니다. 인터넷 공개 주소 검증은 아닙니다. |

첫 자동 실행은 **23 PASS / 1 SKIP / 예상 밖 FAIL 0**입니다. [첫 실행 요약](linux-suite.json), [로그](linux-suite.log)

추가 장애·소유권·정책 검사를 포함한 최종 실행은 **29 PASS / 1 SKIP / 예상 밖 FAIL 0**입니다. [최종 요약](linux-final.json), [최종 콘솔](linux-final.log), [단계별 원본 결과](final/summary.json)

각 자동 실행의 최초 설치는 이미 설치된 상태라 SKIP했습니다. 최초 설치는 새 VM에서 별도 수행했으며, 전체 제거·재설치는 자동 검사 마지막에 실제 수행했습니다. 고의 장애를 정상 감지하고 복원한 경우 검사 결과가 PASS입니다. 장애 주입 시 runtime 자체는 JSON `status: failed`와 종료 코드 1을 반환했습니다.

명령 시간 초과 처리 보완 후 최신 코드를 VM에 다시 전달해 배포·검증에 성공했습니다. [최신 deploy](latest-deploy.json), [최신 verify](latest-verify.json)

## JSON 결과와 로컬 검사

- 최초 설치·최종 단계별 결과·최신 배포/검증의 **실제 JSON 응답 31개**를 [출력 schema](../../schemas/output.schema.json)로 검증했습니다. 모든 응답이 규약을 만족했습니다.
- provider fixture 세 개가 동일한 `DeploymentSpec`과 manifest로 변환되는 것을 확인했습니다. provider와 SSH 접속 변수는 engine의 입력 모델에 포함되지 않습니다.
- 잘못된 타입·이미지 참조·예약 namespace·자격정보 포함 URL·중복 JSON 키·잘못된 CLI 인자를 검사했습니다.
- 외부 namespace와 리소스 보호, rollout 실패 단계, cleanup 범위, 실행 잠금, verify의 설치 금지, 실제 설정 차이를 검사했습니다.
- 실제 자식 프로세스가 시간 초과 뒤 파일을 쓰도록 구성한 시험에서 해당 프로세스가 종료되어 후속 변경이 발생하지 않는 것을 확인했습니다.
- `jsonschema` 개발 의존성을 설치하고 **로컬 검사 16개를 모두 통과**했습니다. SKIP은 없습니다. [로컬 로그](local-tests.log)
- Bash 문법, ShellCheck(셸 정적 검사), JSON 템플릿 파싱을 통과했습니다.

## 가벼운 외부 요청 검사

Mac에서 `http://127.0.0.1:30082/`로 요청 100개를 동시 8개씩 보냈습니다. 모두 HTTP 200과 `Railshot Runtime OK` 본문을 반환했습니다. 관찰 시간은 **0.209초**, 실패는 **0개**입니다. [HTTP 응답](mac-http.log), [요청 집계](mac-load.json)

현재 구현의 이미지 교체 **중간 구간에 연속 요청을 보내지는 않았습니다.** 교체 완료 후 내부·노드 HTTP는 확인했습니다. 이전 PoC의 부하 결과를 이 구현의 결과로 대체하지 않았습니다. 이 짧은 요청 검사는 처리량이나 운영 안정성 보장이 아닙니다.

## 새 파일과 실행 방법

- `bootstrap/`: `preflight.sh`, `install-k3s.sh`, `health.sh`, `cleanup.sh`입니다.
- `cilium/`: `install.sh`, `health.sh`, `network-policy.json.template`입니다.
- `manifests/`: `namespace.json.template`, `workload.json.template`, `service.json.template`입니다.
- `scripts/`: `models.py`, `input_adapter.py`, `render.py`, `engine.py`, `runtime.py`, `common.sh`, deploy/verify/cleanup 진입 스크립트, `test-poc.py`, `test-poc.sh`입니다.
- `scripts/schemas/`: 입력·출력 schema입니다.
- `scripts/tests/`: AWS/GCP/OpenStack fixture, 계약·엔진 테스트, 개발 의존성과 이번 실제 실행 결과입니다.
- `cloudflared/`, `sealed-secrets/`, `cnpg/`: 미확정 모듈을 자동 설치하지 않고 역할을 설명하는 README만 추가했습니다.
- `deployment/README.md`, `.gitignore`를 추가했습니다.

대상 Linux 노드의 저장소 루트에서 다음과 같이 실행합니다.

```bash
sudo ./deployment/scripts/deploy.sh --input deployment/scripts/tests/fixtures/openstack.json
sudo ./deployment/scripts/verify.sh --input deployment/scripts/tests/fixtures/openstack.json
sudo ./deployment/scripts/cleanup.sh --input deployment/scripts/tests/fixtures/openstack.json
```

시험용 전용 노드에서 전체 검사를 실행합니다.

```bash
sudo ./deployment/scripts/test-poc.sh --disposable-node --results /var/tmp/railshot-runtime-results
```

입력·출력 예시와 전체 옵션은 [Deployment 안내](../../../README.md)를 확인해 주시기 바랍니다.

## Provider 차이와 통합 계약

AWS/GCP/OpenStack의 이름은 요청 문맥입니다. 이번 실검증은 **세 요청을 같은 로컬 VM에서 실행한 결과**이며 AWS·GCP·OpenStack 자원을 직접 검증한 것이 아닙니다.

| 책임 | 연결 시 필요한 사항 |
| --- | --- |
| Provider | Linux 자원·이미지 구조·접속 권한·다운로드 경로를 준비합니다. AWS/GCP 방화벽·라우팅과 OpenStack 보안 그룹·floating IP 등은 각 담당 영역입니다. |
| Ansible/원격 실행 | 신뢰할 수 있는 대상 노드로 파일·JSON을 전달하고 그 노드에서 root로 CLI를 실행합니다. stdout JSON과 stderr를 분리하고 종료 코드를 수집합니다. |
| Input Adapter | 최종 외부 API/Ansible 변수명을 공통 `DeploymentSpec`으로 변환합니다. 현재 규약은 임시 `0.1`입니다. |
| Engine | 공급자 호출 없이 Linux·K3s·Cilium·앱·HTTP를 처리합니다. |
| 외부 확인 | 상위 실행 머신에서 공개/접근 가능한 endpoint에 요청해야 합니다. 서버 내부의 HTTP 성공을 외부 공개 성공으로 취급하지 않습니다. |

팀 합의가 필요한 것은 최종 JSON 규약과 ReadyTarget 매핑, 기존 Ansible K3s의 안전한 채택 방식, namespace·환경 소유권, 앱 이름, private registry 비밀정보, 자원·보안 정책, 외부 TLS·라우팅과 검증 주체입니다. `node.host`는 실제 원격 연결이나 대상 인증을 수행하지 않으므로 실행 노드 선택은 상위 계층이 책임져야 합니다.

## 남은 제한과 후속 작업

- 검증된 실행 환경은 Ubuntu arm64 한 종류입니다. 실제 AWS/GCP/OpenStack, Debian/x86_64, 기존 운영 클러스터, 회장 네트워크, 인터넷 URL/TLS는 검증하지 않았습니다.
- 단일 K3s 서버만 지원합니다. EKS·GKE·기존 다른 CNI 클러스터를 임의로 변경하지 않습니다.
- namespace당 관리 앱 이름은 하나이며, 일반 cleanup은 namespace와 별도 정책을 보존합니다.
- 자동 rollback, 전체 Pod 보안, private registry credential 관리, 지속 작업 기록, 설치 중 강제 전원 중단 복구는 구현하지 않았습니다.
- 임의 버전 조합의 호환성과 CIDR 변경은 보장하지 않습니다. 설치·검증·운영 업그레이드 계약은 추가 합의가 필요합니다.
- 장시간 부하, 디스크·메모리 고갈, 다중 노드·HA, 저장소·DB·cache는 이번 검증 범위가 아닙니다.
- full cleanup은 클러스터 전체 앱과 데이터를 삭제하므로 전용 노드에서만 사용합니다. 모든 커널 잔여 상태의 초기화는 새 VM이나 재부팅으로 확인해야 합니다.
- cloudflared·Sealed Secrets·CNPG·Argo CD·NFD·airgap·Gateway API는 자동 설치하지 않습니다. 향후 승인된 별도 모듈로 연결합니다.

검증 종료 시 테스트 VM은 정상 앱과 함께 실행 중입니다. Mac에서 `http://127.0.0.1:30082/`로 샘플에 접근할 수 있습니다.
