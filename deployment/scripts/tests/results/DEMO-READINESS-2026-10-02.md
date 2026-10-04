# RailShot Runtime 데모 준비 점검 — 2026-10-02

Runtime(배포 실행 계층)의 로컬 검사와 최신 integration(팀 통합 브랜치)의 연결부 검사는 통과했습니다. 현재 공개 플랫폼과 AWS/GCP 기존 앱도 정상 응답합니다. 다만 이번 점검에서 개인 Runtime의 실제 AWS/GCP 신규 설치나 Dashboard(배포 화면)에서 시작하는 전체 배포를 실행한 것은 아닙니다.

## A. Runtime readiness

대상은 `Jasmin-Softbank/Railshot`, 작업 브랜치는 `feature/deployment-runtime-seungmin`, HEAD는 `5f893ff2cf991561698524fe0d76cabcd2c07323`입니다. 최신 integration은 `94808d26e36f84bc8e65f7b7e3d3bb085634e334`까지 확인했습니다. 점검 중 PR #28과 #35가 병합되어 처음 검사한 `18bed30` 이후의 상위 연결부 검사를 다시 실행했습니다. integration은 임시 소스 사본에서 읽고 검사했으며 작업 브랜치로 병합하지 않았습니다.

| 항목 | 결과 | 의미 |
| --- | --- | --- |
| 개인 Runtime 전체 unit/contract test(단위·입출력 계약 검사) | 58 PASS / 0 FAIL / 1 SKIP | 기존 미추적 Dashboard handoff 검사 6개를 포함합니다. |
| 개인 브랜치의 Ansible Cilium 검사 | 13 PASS | 기존 CNI(클러스터 네트워크 플러그인) 보호 및 Cilium 연결을 검사합니다. |
| 최신 integration의 Runtime 전체 검사 | 68 PASS / 0 FAIL / 1 SKIP | 배포된 플랫폼 검증 도구의 계약 검사를 포함합니다. |
| 최신 integration의 Ansible 전체 단위 검사 | 61 PASS / 0 FAIL / 4 SKIP | 실제 VM을 변경하는 playbook(서버 설정 절차)은 실행하지 않았습니다. |
| 최신 integration의 GitOps 단위 검사 | 26 PASS | 게시 결과·배포 선언·Argo 연결 경계를 검사합니다. |
| 최신 integration의 API 연결부 검사 | 66 PASS | environments/product/CD/metrics/deployment 테스트를 실행했습니다. 외부 실행기는 mock(모의 실행)입니다. |
| shell 문법 검사 | 개인 20개 / integration 19개 PASS | `bash -n`으로 검사했습니다. |

최종 대상 버전 기준 합계는 **292 PASS / 0 FAIL / 6 SKIP**입니다. 먼저 검사했던 `18bed30`의 연결부 결과를 이 숫자에 중복 합산하지 않았습니다. SKIP은 개인·integration에서 각각 생략된 Argo native CRD(실제 확장 자원 정의) 검사 1개씩과 integration의 실제 Ansible/TLS/Vault 도구 검사 4개입니다. 실제 Linux 상태 검사와 공개 HTTP 요청도 이 단위 검사 숫자에 합산하지 않았습니다.

기존 Linux VM `railshot-runtime-test`에서 변경 없이 확인한 결과는 다음과 같습니다.

- Ubuntu 24.04.4 LTS, K3s `v1.34.11+k3s1`, Node Ready(서버 준비 완료)입니다.
- Cilium `1.20.2`, Operator 및 Envoy가 정상이며 관련 실행 단위가 모두 Ready입니다.
- `railshot-demo/railshot-workload` Pod(앱 실행 단위)가 Running/Ready입니다.
- Mac → Lima 포트 전달 → NodePort(노드의 앱 접속 포트) 경로 `http://127.0.0.1:30082/`에서 HTTP 200 및 `Railshot Runtime OK`를 확인했습니다.
- Cilium CLI(명령줄 도구)의 설치 위치는 `/usr/local/lib/railshot-deployment/cilium`입니다. 일반 PATH의 `cilium` 이름 조회는 실패했지만 실제 설치 경로로 상태 검사는 통과했습니다. 새로 설치하지 않았습니다.

이번에는 기존 클러스터를 초기화하지 않았습니다. 과거 세 Lima VM의 provider 모의 검사 및 16개 airgap(외부 인터넷 없는 설치) 실험은 기존 기록이며 이번 재실행 결과로 계산하지 않습니다. 기존 [provider 모의 검증](PROVIDER-SIMULATION-2026-10-02.md)과 [airgap 원본 결과](migration/final/summary.json)를 참고하실 수 있습니다.

### Runtime 계약과 최신 연결부

`engine.py`, `input_adapter.py`, `models.py`, `runtime.py`, 입력·출력 schema(데이터 규약), `bootstrap/`, `cilium/`은 개인 HEAD와 최신 integration 사이에 차이가 없습니다. JSON 계약 `0.1/0.2` 호환성에 대한 변경은 발견하지 못했습니다.

| 경계 | 실제 구현 | 판단 |
| --- | --- | --- |
| 현재 공개 UI → API | 소스와 environment/provider를 multipart(파일 포함 요청)로 전달하고 서버가 등록 앱·대상을 결정합니다. | Runtime JSON의 직접 입력이 아닙니다. |
| 최신 `94808d2` UI → API | 기존 등록 대상 경로 외에 계획이 있으면 app/target_id/plan_id를 전달합니다. | 최신 API 연결부 66개 검사가 통과했습니다. 현재 공개 배포 버전과는 다릅니다. |
| 최신 환경 준비 | `environments.js` → Ansible `guest.check`/`runtime.install` → `deployment/scripts/environment.py register`입니다. | 이전 점검에서 미연결이던 등록 helper(대상 등록 도구)가 최신 코드에는 호출됩니다. 실제 클라우드 호출은 이번에 실행하지 않았습니다. |
| K3s/Cilium 설치 | Ansible이 K3s를 구성하고 `deployment/cilium/install.sh`, `preflight.py`, `common.sh`, 고정 버전 파일을 재사용합니다. | 개인 Deployment 영역의 Cilium 설치 모듈은 실제 통합 경로에서 사용됩니다. |
| 앱 배포 | CI(검사·이미지 생성) → 게시 결과 확인 → CD bridge(배포 연결 도구) → Argo → 공개 HTTP 확인입니다. | 앱을 개인 `DeploymentEngine`으로 별도 적용하는 경로는 아닙니다. |
| 개인 JSON CLI | JSON → Adapter(입력 변환) → DeploymentSpec(실행 명세) → Engine(실행기) → DeploymentResult(실행 결과)입니다. | provider 이름 외에도 완성된 이미지·노드·namespace(앱 격리 공간)·포트가 필요합니다. UI 입력을 그대로 전달하지 않습니다. |

코드 근거는 최신 integration의 [product.js](https://github.com/Jasmin-Softbank/Railshot/blob/94808d26e36f84bc8e65f7b7e3d3bb085634e334/apps/api/src/product.js#L253), [environments.js](https://github.com/Jasmin-Softbank/Railshot/blob/94808d26e36f84bc8e65f7b7e3d3bb085634e334/apps/api/src/environments.js#L414), [Ansible site.yml](https://github.com/Jasmin-Softbank/Railshot/blob/94808d26e36f84bc8e65f7b7e3d3bb085634e334/infrastructure/ansible/site.yml#L186)에 있습니다.

Ansible이 관리하는 K3s 설정에는 node-name/cluster-dns/tls-san과 별도 설치 소유권 정보가 있습니다. 개인 bootstrap은 기존 설정과 다른 경우 덮어쓰지 않고 거부합니다. 따라서 팀의 기존 클라우드 노드에 개인 `deploy.sh`를 그대로 실행해서 두 설치 방식을 섞는 것은 이번 데모의 재실행 경로로 삼지 않습니다.

## B. Cloud validation status

### 최신 플랫폼 배포

현재 확인한 공개 플랫폼의 소스는 `18bed3025870b315ab9d43c3dd9945ac4b56576e`, 배포 선언 revision(정확한 Git 버전)은 `d480f062cb655f4ff8180da7be9a175b980d45c3`입니다. 최신 integration `94808d2`이 이미 공개 배포됐다고 주장하지 않습니다.

[플랫폼 배포 실행 37011150579](https://github.com/Jasmin-Softbank/Railshot/actions/runs/37011150579)의 build/publish/deploy/verify가 모두 성공했습니다. 작은 검증 결과 artifact(실행 증거 파일)만 다운로드했습니다. 이 기록은 **22:18:47 KST**에 Argo의 정확한 revision, 실제 Ready Pod의 이미지 digest(내용 식별값), 공개 플랫폼 health/API를 검증했다고 반환합니다.

- Dashboard 이미지: `ghcr.io/jasmin-softbank/railshot-dashboard@sha256:5d6cb04d0ddde0ef3795c6689167b3e1281ec6241c96381cedaf0145bee3fc99`
- API 이미지: `ghcr.io/jasmin-softbank/railshot-api@sha256:e479f8ec4159058f200b5281be49d9d255830e89041c32d14d436450f8c3a394`

이번 **22:32~22:33 KST** 실제 GET(읽기 요청)에서도 플랫폼 `/`, `/healthz`, `/api/v1/targets`, `/api/v1/deployment-options`가 모두 HTTP 200입니다. 플랫폼 `/health`는 정의된 검사 경로가 아니므로 404입니다. 앱의 `/health`와 플랫폼의 `/healthz`를 구분해야 합니다.

새 소스 `94808d2`의 [플랫폼 실행 37014365786](https://github.com/Jasmin-Softbank/Railshot/actions/runs/37014365786)은 점검 중 진행 상태입니다. 이 실행은 아직 검증 성공으로 간주하지 않습니다. 최신 UI/API는 환경 선택 경로를 `/api/v1/options`로 함께 변경했으며 로컬 연결부 검사는 통과했습니다. 배포가 끝나면 이 새 경로와 `/profiles`의 실제 응답을 다시 확인해야 합니다.

### 환경별 증거

| 구분 | AWS | GCP |
| --- | --- | --- |
| 팀의 실제 설치·배포 기록 | K3s/Cilium, Argo 앱 적용, 실제 Pod/imageID 및 공개 HTTPS 성공 기록이 있습니다. | 같은 종류의 성공 기록이 있습니다. |
| 이번 기존 앱 공개 주소 GET | HTTP 200, `{"status":"ready"}` | HTTP 200, `{"status":"ready"}` |
| 이번 controller(명령 실행 Mac) 확인 | 과거 사용한 `for-study` 인증 profile을 현재 찾지 못했습니다. 다른 인증으로 전환하지 않았습니다. | SDK 587.0.0, 활성 인증, 프로젝트 접근을 확인했습니다. `railshot-gcp-poc`, `asia-northeast3-a`, 상태 RUNNING입니다. |
| 이번 원격 Node/K3s/Cilium 직접 검사 | 미실행 | 미실행; VM RUNNING은 Kubernetes 정상 상태의 증명이 아닙니다. |
| 개인 deploy.sh/verify.sh 신규 실제 cloud 시험 | NOT_RUN | NOT_RUN |
| 실제 cloud 자원·기존 앱 변경 또는 cleanup | 없음 | 없음 |

팀 기록은 [cloud-e2e-progress.md](https://github.com/Jasmin-Softbank/Railshot/blob/94808d26e36f84bc8e65f7b7e3d3bb085634e334/docs/integration/cloud-e2e-progress.md)에 있습니다. 기록상 AWS/GCP 모두 **공유 AWS ALB(외부 요청 분배 장치)** 뒤에 있으며 GCP는 WireGuard(암호화된 사설 연결)를 통해 전달됩니다. 현재 경로가 GCP 자체 LB를 구현했다는 뜻은 아닙니다. 다른 저장소의 workflow(자동 실행 절차)는 이번에 조회하지 않았습니다.

이전 GCP 보고서의 “gcloud 없음”과 AWS 보고서의 “for-study 인증 성공”은 당시 관측입니다. 이번 현재 상태는 위 표이며 과거 문서를 소급해서 고치지 않았습니다. 전용 클라우드 시험 대상의 소유권·접속·namespace/포트가 정해진 뒤에 개인 Runtime의 별도 smoke test(짧은 실제 실행 확인)를 진행해야 합니다.

## C. Demo risk

1. **공개 배포와 최신 integration의 버전 차이:** `18bed30`은 공개 플랫폼 검증 증거가 있지만 `94808d2`의 새 앱·DB 흐름은 이번에 로컬 계약 검사만 했습니다. 발표 전 사용할 버전을 고정하고, 통합 담당자가 새 배포 시 실제 검증 결과를 확보해야 합니다. 최신 UI의 `/api/v1/options`와 API가 같은 버전으로 올라가는지도 확인해야 합니다.
2. **현재 공개 UI의 대상 제한:** AWS `k3s-aws` / `fixture-npm-js`만 사용 가능하며 OpenStack/Proxmox는 unavailable(미연결), GCP 선택지는 없습니다. 현재 세 provider를 UI에서 모두 배포할 수 있다고 시연하지 않습니다.
3. **상태 표시와 실제 증거의 차이:** `/api/v1/targets`의 runtime.status는 `unknown`, observed_at은 null입니다. UI의 available은 실행 연결 상태이며 현재 Node/Cilium health를 증명하지 않습니다.
4. **클러스터 소유권 충돌:** 개인 JSON Engine과 Ansible bootstrap을 기존 팀 노드에서 섞어 재설치하지 않습니다. 앱 업데이트는 현재 Argo 소유권을 유지합니다.
5. **시연 시간:** 과거 clean install(최초 설치)은 수 분 이상 걸렸고 최신 계획 API는 최대 10분까지 기다리는 설정입니다. 2~3분 발표는 준비된 환경에서 소스 → 검사·이미지 → GitOps 반영 → URL 검증에 집중하고 긴 최초 설치는 증거 자료로 보여줍니다. 짧은 발표 안에 매번 완료된다고 측정한 것은 아닙니다.
6. **실제 화면에서의 전체 실행 미검증:** 이번에는 배포 POST(실행 요청), 실제 CI dispatch(작업 시작), 앱 업데이트, 재시작, 장애 주입을 하지 않았습니다. 담당자가 사용할 소스·대상·계정으로 리허설을 수행해야 합니다. 과거 정지 timer·인증 만료 기록은 현재 설정과 다를 수 있으므로 운영 담당자가 시연 직전에 다시 확인해야 합니다.

## D. 발표용 3문장

저희는 클라우드나 온프레미스가 준비한 Linux 노드 위에 K3s(경량 쿠버네티스)와 Cilium(클러스터 네트워크)을 구성해 앱이 실행될 공통 환경을 제공합니다.
배포 파이프라인(자동 검사·게시·적용 과정)은 검증된 이미지를 GitOps(Git에 기록한 배포 상태를 적용하는 방식)로 반영하고 실제 URL 응답까지 확인합니다.
AWS·GCP의 기존 앱에서 공개 HTTPS 정상 응답을 확인했으며, 이번 데모는 준비된 환경의 배포 과정과 검증 결과를 중심으로 보여드립니다.

## E. 예상 질문과 답변

| 질문 | 답변 |
| --- | --- |
| 왜 K3s인가요? | 노트북 VM과 단일 Linux 노드에서도 Kubernetes(컨테이너 실행·복구 시스템)를 구성할 수 있도록 설치·운영 부담을 줄였습니다. Kubernetes 자원 형식을 사용하므로 앱 배포 정의를 공유할 수 있습니다. [K3s 공식 문서](https://docs.k3s.io/) |
| 왜 Cilium인가요? | 환경별 네트워크 제품에 앱 규칙을 직접 묶기보다 공통 CNI(클러스터 네트워크 플러그인)와 NetworkPolicy(앱 사이 통신 규칙)를 사용하기 위해 선택했습니다. 성능 우위를 이번에 측정했다는 주장은 하지 않습니다. [Cilium 공식 문서](https://docs.cilium.io/en/stable/overview/intro/) |
| 왜 Argo CD/GitOps인가요? | 검증된 이미지와 배포 상태를 Git 이력으로 추적하고 Argo CD(선언된 상태를 클러스터에 적용하는 도구)가 적용·상태 확인을 담당하도록 했습니다. 자동 롤백이나 LKG(마지막 정상 버전) 복구의 실증은 별도 증거가 필요합니다. [Argo CD 공식 문서](https://argo-cd.readthedocs.io/en/stable/) |
| 왜 provider별 LB를 분리하나요? | provider(인프라 제공 환경)마다 외부 주소·인증서·라우팅·권한이 달라 외부 노출 책임을 Runtime의 Service(앱 접속 자원)와 분리합니다. 현재 실증은 AWS/GCP가 공유 AWS ALB를 사용하는 방식이며 provider마다 별도 LB를 완성한 상태는 아닙니다. |
| 왜 cross-cloud는 제외했나요? | 이번 목표는 환경별 독립 배포와 검증입니다. cross-cloud HA(여러 클라우드에 걸친 고가용성), 자동 장애 전환, DB 복제는 데이터 일관성과 장애 처리까지 검증해야 하므로 제외했습니다. AWS↔GCP 사설 통신이 이미 있다는 사실과 cross-cloud HA 구현은 구분합니다. |

## F. integration handoff message

> Deployment 점검 결과입니다. 개인 Runtime HEAD는 `5f893ff`, 최신 integration `94808d2`과 JSON 계약·bootstrap·Cilium 핵심 코드가 동일합니다. 개인 및 최신 통합 연결부 검사는 292 PASS / 0 FAIL / 6 SKIP, shell 문법은 39개 통과했습니다. 기존 로컬 Linux VM의 Node/Cilium/앱과 외부 포트 전달 HTTP 200도 확인했습니다. 팀 Ansible 경로는 Deployment의 Cilium 설치 모듈을 재사용하며 최신 코드는 runtime.install 이후 대상 등록까지 연결합니다. 검증 완료 플랫폼은 `18bed30` 기반 `d480f06`이며 배포 검증 기록과 현재 공개 HTTP가 정상입니다. `94808d2` 플랫폼 배포는 진행 중이므로 검증 결과를 확인하고 새 `/api/v1/options` 응답도 확인해 주세요. AWS/GCP 기존 앱도 HTTP 200이지만 개인 deploy.sh의 실제 cloud 신규 실행은 이번에 하지 않았습니다. 현재 화면은 AWS 등록 앱만 실행 가능하므로 사용할 버전·대상·소스를 고정하고, 새 통합 버전은 배포 검증 후 실제 화면부터 URL까지 리허설해 주세요. 기존 Ansible 클러스터에 개인 bootstrap을 덮어 실행하지 말아 주세요.

이 메시지는 전달용 초안입니다. 다른 팀원에게 실제 전송하지 않았습니다.

## 실행 증거 및 변경 범위

구조화 결과는 [summary.json](demo-readiness/summary.json), 최신 플랫폼 기록은 [platform-verification.json](demo-readiness/platform-verification.json), 실제 HTTP 관측은 [public-http.json](demo-readiness/public-http.json)입니다. 단위 검사 로그, shell 검사 JSON, 로컬 Node/Cilium 출력은 같은 `demo-readiness/`에 보존했습니다.

검사는 기존 `/tmp/railshot-runtime-validation/bin/python`과 Node 22.22.2를 사용했습니다. integration은 `git archive`로 만든 임시 사본에서 실행했습니다. 자식 Python에도 기존 검증 환경을 사용하도록 PATH를 지정했습니다. Runtime 전체 검사는 `python -m unittest discover -s deployment/scripts/tests -p 'test_*.py' -v`, Ansible/GitOps 검사는 해당 폴더의 `test*.py` 전체, API 검사는 environments/product/cd/metrics/deployment 테스트 파일을 Node의 `--test`로 실행했습니다.

이번 변경은 이 보고서와 `deployment/scripts/tests/results/demo-readiness/`의 증거 파일뿐입니다. 기능 코드·다른 담당 영역·원격 브랜치를 수정하지 않았고 commit/push/merge를 실행하지 않았습니다. 기존 미추적 Dashboard 계약 검사와 결과 파일은 그대로 보존했습니다.
