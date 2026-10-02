# 공통 릴리스 배포

`railshot-ci.yml`의 성공한 `Railshot CI gate`가 같은 커밋의 플랫폼 릴리스를 호출한다. `RAILSHOT_AUTO_RELEASE=true`이고 현재 브랜치가 `RAILSHOT_PLATFORM_VERIFY_REF`와 일치하면 문서 전용 변경을 제외한 플랫폼·런타임·공통 정책 변경을 자동 게시하고 플랫폼 배포를 검증한다. 기존 플랫폼 자동 릴리스는 이 변수로 계속 운영한다. 중앙 워커와 AWS/GCP/OpenStack까지 같은 릴리스를 적용하려면 별도로 `RAILSHOT_MULTICLOUD_RELEASE=true`를 설정한다. 이 두 번째 변수는 미설정·`false`가 기본이며, 초기 운영 바인딩과 세 환경 검증을 준비한 뒤 활성화한다. 수동 실행도 저장소 변수가 `true`일 때만 `multicloud` 입력을 허용한다. `false`·미설정 상태에서 입력만 켜면 admission에서 차단한다.

1. dashboard/API/ci-runner를 시험하고 같은 실행에서 얻은 GHCR digest를 고정한다.
2. 기존 `deployment/platform` 브랜치에 플랫폼 선언을 반영하고 실제 Argo 상태·파드 digest·공개 HTTPS를 검증한다.
3. 고정된 SSM 문서가 제어 서버에서 현재 승인 브랜치와 CI gate를 다시 확인한다. 기존 build-controller와 credential-renewer를 UID/CAS로 갱신하고 새 digest로 실행된 Job을 검증한다. 진행 중인 고객 runner Job은 보존한다.
4. AWS/GCP/OpenStack을 병렬 실행한다. 앱이 등록된 환경은 기존 Terraform state의 소유권·drift·saved plan을 확인한 뒤 허용된 기존 리소스와 공통 RBAC·pull Secret·관측 구성을 갱신하고 앱·공개 HTTPS까지 검증한다. 명시적인 `node-only` 환경은 edge와 앱 단계를 건너뛰고 K3s·Cilium·관리 TLS·노드 UID·실제 CPU/메모리 수집을 검증한다. 두 경로 모두 공통 런타임 정책에 묶인다.
5. **세 환경 모두 같은 source SHA로 검증된 경우에만** 앱 저장소의 실행 workflow와 `PLATFORM_REF`를 승격한다. 하나라도 실패하거나 상태가 불확실하면 전체 결과는 `incomplete`이고 앱 버전을 승격하지 않는다.

서로 독립적인 세 클라우드의 반영은 병렬로 시작하며 완료 시각은 다를 수 있다. 공통 버전 완료 여부는 세 환경의 receipt를 묶어서 판단한다.

## 처음 연결할 때

기존 제어 VM에서 `bootstrap-release-tools.py`로 `apps/api/runtime-tools.json`에 고정된 도구를 `/opt/railshot-release`에 설치한다. Ubuntu의 `unzip`, `dpkg-deb`, `tar`와 기존 Python·Ansible·Git이 필요하다. 내려받은 모든 도구의 SHA256을 검사한 뒤 설치한다.

`infrastructure/terraform/platform-release`는 운영자 bootstrap 소유이다. 기존 제어 VM만 지정하는 고정 SSM 문서/OIDC 역할과 GCP 인증 바인딩을 먼저 검토·적용한다. 출력의 문서 버전·SHA256을 각각 `RAILSHOT_RELEASE_DOCUMENT_VERSION`, `RAILSHOT_RELEASE_DOCUMENT_SHA256` 저장소 변수로 설정한다. 해당 bootstrap의 IAM·인증·방화벽·SSM 문서 변경은 일반 런타임 릴리스로 자동 적용하지 않는다.

`/etc/railshot/release.json`은 root 소유 `0600` 파일이다. 다음 필드가 필요하다.

| 필드 | 내용 |
|---|---|
| `version` | `1` |
| `state_dir` | root 전용 전체 릴리스 기록 디렉터리 |
| `operator_kubeconfig` | 기존 `railshot-operator`의 명시적 제어 클러스터 kubeconfig |
| `gcp_credentials_file` | 제어 EC2 identity에 바인딩된 GCP external-account 설정 |
| `workers` | `platform_workers.py discover`가 읽은 runner URL·build 노드·기존 객체 UID |
| `apps` | `{"repository":"Jasmin-Softbank/railshot-apps","branch":"main"}` |
| `targets` | `provider`, `target_id`, `registry_file`, `config_file`, `registration_state`, `from_policy_file`, `edge_config_file`가 있는 세 항목. 앱이 없는 대상은 `scope="node-only"`를 명시하고 `edge_config_file`·`binding_file`을 생략한다 |

Target 파일 경로는 `/home/railshot-operator/.local/share/railshot/` 아래에 두며 operator 소유 `0600`을 유지한다. 기존 OpenStack 등록 파일·개인키·관측 collector를 재사용한다. AWS/GCP의 과거 수동 등록은 `environment-adopt.py plan`으로 현재 노드 UID·리소스 UID/RV·관리 CA·HTTPS·기존 라벨을 고정하고, 계획 digest를 검토한 후 `apply --expected-plan-sha256`로 명시적으로 인수한다. 이 작업은 기존 객체의 소유 라벨만 변경한다. 아직 없는 관측 수집기를 성공한 것으로 기록하지 않는다.

앱이 없는 기존 노드는 config와 registration 모두 명시적인 v2 `node-only` 계약을 사용한다. v1에서 앱 필드가 빠졌다고 이 경로로 전환하지 않는다. config 예시는 다음과 같다.

```json
{
  "version": 2,
  "scope": "node-only",
  "registration": {
    "state_dir": "/home/railshot-operator/.local/share/railshot/registration-claims",
    "observability_config_file": "/home/railshot-operator/.local/share/railshot/observer-config.json"
  },
  "management": {"server": "https://REGISTERED_PRIVATE_IP:6443"}
}
```

관리 endpoint override가 등록된 대상만 registry의 private IP와 일치하는 `tls_server_name`을 추가한다. `environment-adopt.py plan`은 현재 버전·노드 UID/IP·CA·SSH 설정을 고정하고 관리 TLS를 검증한다. 검토된 digest의 `apply`는 private claim과 registration receipt만 저장한다. 이 경로는 앱·namespace·Argo Application을 생성하지 않는다. 첫 릴리스의 from-policy는 인수 시 검증한 baseline과 같아야 하며, 이후에는 직전 검증된 to-policy와 이어져야 한다. `application`·`public_http`·`edge`는 성공 대신 `not_applicable`로 기록한다. 관측 등록과 90초 이내 실제 수집 검증은 생략하지 않는다.

각 provider의 `edge_config_file`은 `edge_update.py`의 v1 입력이다. 원본 local Terraform writer를 중지하고 최신 lineage·serial·파일 hash·fresh no-op plan을 확인한 뒤 단 하나의 실행 권위를 옮긴다. state 복사만으로 권위 인수가 완료되지는 않는다. GCP native LB와 OpenStack AWS relay의 source 모듈은 다르다. OpenStack의 `edge_kind="aws-relay"`는 기존 TG·listener·Route53 alias 세 리소스만 소유하며, 제어 SG ingress와 ALB SG egress는 기존 소유자에게 남긴다. 기존 target tuple·ingress rule·healthy 상태도 매번 검사한다. Octavia로 전환할 때는 별도 인수 절차가 필요하다.

GCP의 WireGuard를 사용하지 않는 관리 경로는 provisioned public IPv4의 TLS endpoint에 고정한다. 관측도 같은 주소를 사용하고 `observer_source_cidr`를 기존 collector의 외부 IPv4 `/32`에 고정한다. 기본 내부 환경은 기존 observer private IP `/32`를 유지한다. SSH는 기존 SSM/IAP 연결을 재사용한다.

## 공용 관측 등록 원본

공통 런타임 릴리스는 API PVC의 registrar 상태만 사용한다. 운영자의 `observability_config_file`에는 다음 바인딩을 추가한다. UID는 기존 객체를 조회해 고정하며 아래 예시를 그대로 사용하지 않는다.

```json
{
  "api_registrar": {
    "context": "railshot-control",
    "deployment_uid": "EXISTING_RAILSHOT_API_DEPLOYMENT_UID",
    "pvc_uid": "EXISTING_RAILSHOT_API_PVC_UID",
    "config_file": "/var/lib/railshot/config/app-db/observer.json"
  }
}
```

`config_file`은 **registrar settings** 파일이다. API의 legacy metrics용 `/var/lib/railshot/config/observer.json`과 구분한다. 이 파일의 `state_dir/product.json`과 API의 `RAILSHOT_OBSERVER_PRODUCT_FILE`이 같아야 한다. API 설정에는 `api_registrar`를 넣지 않는다. 운영자와 API의 collector 식별자·수명·주소·포트·collector 디렉터리가 다르면 쓰기 전에 거부한다.

제어 서버는 바인딩된 Deployment·PVC 및 그 ReplicaSet이 소유한 Ready API Pod를 확인하고, 그 Pod의 기존 `register.py --api-registrar`에 대상 한 건만 전달한다. API의 일반 앱 등록과 이 요청은 같은 `registration.lock` 안에서 현재 desired/product를 병합하고 Prometheus를 갱신한 후 product를 원자적으로 저장한다. 운영자 디렉터리로 product를 복제하지 않으며, 릴리스 검증도 API 원본을 다시 읽는다. 실행 중 Pod/Deployment/PVC가 바뀌거나 응답이 불확실하면 실패로 남기고 자동 재시도하지 않는다.

API 바인딩이 준비되지 않으면 노드 변경 전에 `observer_preflight`에서 차단한다. 기존 앱의 관측 파일을 이동해야 한다면 먼저 API·운영자 등록을 정지하고, 기존 desired와 product의 대상이 모두 보존된 한 원본을 API PVC에 준비한 뒤 경로를 연결한다. 단순 파일 덮어쓰기나 두 collector writer의 병행 운용은 허용하지 않는다.

## 검증과 복구

전체 receipt는 `state_dir/<source_sha>/receipt.json`, 현재 완료 판본은 `current.json`, 가장 최근 시도는 `last-attempt.json`이다. 대상별 runtime/edge 기록은 operator private 디렉터리에 남는다. SSM command ID와 Actions receipt도 보존한다. 성공했던 릴리스를 다시 확인할 때는 새 health/plan 조회를 수행하며, 이전 성공 결과만 재사용하지 않는다.

실패 후 같은 source를 자동 재실행하지 않는다. 현재 state·진행 중인 SSM command·runtime recovery 기록을 먼저 확인한다. Worker와 앱 workflow는 `platform_workers.py rollback --state ...`, `rollback-apps --state ...`의 UID/CAS 복구를 사용한다. 플랫폼은 기존 `publish-platform.py`에서 검증된 이전 digest로 새 배포 커밋을 만들고 `verify-platform.py`로 확인한다. Git history를 강제로 되돌리지 않는다.

K3s/Cilium 버전이 그대로면 설치나 재시작 없이 정책을 갱신한다. 버전 변경은 같은 minor 안의 전진 patch만 지원하고 `upgrade.recovery_ack=true`와 변경되는 바이너리의 SHA256을 요구한다. 런타임 identity, SQLite·token·config·기존 binary 및 Cilium Helm 설정을 보관한 뒤 갱신한다. 실패한 업그레이드의 자동 rollback이나 minor/major 이동은 하지 않는다. 백업과 실패 지점을 확인한 운영자 복구가 필요하다.

원래 정지된 CronJob은 계속 정지 상태로 유지하며 전체 실행 검증을 통과시키지 않는다. Worker receipt의 `scope="worker_execution"`은 컨트롤러/갱신기 실행 검증이며 고객 앱 빌드 성공을 뜻하지 않는다. 앱 빌드·배포 성공은 별도 CI/CD operation으로 확인한다.
