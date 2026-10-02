# CI → Argo CD 인계

`handoff.py`는 검증된 CI 게시 결과로 검토용 선언을 만들고, `argo.py`는 운영자가 등록한 Argo CD에서 그 선언의 sync와 결과를 확인합니다. `bridge.py`는 이 두 구현을 제품 백엔드에서 호출하는 비공개 실행 진입점입니다. 팀원 runtime bootstrap으로 준비한 AWS/GCP single-node K3s를 재사용하며, 클러스터 설치나 CI runner 등록을 수행하지 않습니다. 사용자 소스 저장소와 Argo가 읽는 배포 선언 저장소는 별개입니다. Argo Application은 config 저장소의 앱별 경로를 읽으므로 고객마다 원본 소스 저장소를 새로 등록할 필요는 없습니다.

```sh
python gitops/handoff.py /private/published /private/target.json /private/handoff-review
```

입력은 신뢰된 GitHub CI artifact 채널에서 받은 `images.json`, `jasmin.yaml`, `verdict.json`, `manifest.json`, `handoff.json`입니다. `handoff.json`은 **version 2**여야 하며 run/producer attempt/bundle artifact ID와 `registry` 접근 검증 결과를 출력 receipt에 유지합니다. registry 결과의 `images_sha256`은 `images.json` 해시와 같아야 합니다. v1은 private pull 계약을 표현하지 못하므로 거부합니다. 게시 artifact 자체의 ID는 receipt 본문에 없으며 API가 GitHub 응답에서 별도로 검증합니다. 이 CLI는 GitHub 조회를 수행하지 않으므로 신뢰된 게시 채널에서 확인한 파일만 전달해야 합니다. 파일 해시는 무결성 검사이며 서명이 아닙니다. 임의 업로드 artifact를 신뢰하지 않습니다. 원래 이미지 tar는 이 선언 생성 단계에서 재빌드하거나 실행하지 않습니다.

target JSON은 운영자가 제공합니다. 필수 필드는 `id`, `namespace`, `argocd_namespace`, 제한된 AppProject `project`, `architecture: amd64`, `repo_url`, Kubernetes API `cluster_server`, config repo `path`, 그 경로를 검토한 Git commit SHA `revision`, 할당한 `node_port`, 실제 라우팅 원본 `ingress_cidrs`, CPU(m)/memory(Mi) requests·limits `resources`입니다. 예시는 `test_handoff.py`의 target을 참고하세요. CI S/M/L을 운영 리소스 값으로 임의 변환하지 않습니다.

Private 이미지는 target에 `"image_pull_secret": {"namespace": "tenant-demo", "name": "ghcr-pull"}`을 추가합니다. namespace/name은 게시 receipt의 참조와 정확히 같고 namespace는 `target.namespace`와 같아야 합니다. 그러면 Deployment에 `imagePullSecrets: [{name: ghcr-pull}]`을 생성합니다. 이 참조는 설치할 Secret의 위치와 이름이며 Secret 존재나 pull 성공의 증거가 아닙니다. 실제 `kubernetes.io/dockerconfigjson` Secret 설치와 자격 갱신은 CD 담당자가 수행합니다. Secret bytes, registry token, Docker 인증 설정, kubeconfig는 입력 artifact나 생성 파일에 넣지 않습니다.

생성 파일은 Deployment/Service/NetworkPolicy를 묶은 `workload.json`, 별도 `application.json`, `receipt.json`입니다. receipt의 `documents`는 두 선언의 canonical JSON hash를 보존합니다. **workload.json만** 지정한 config repo path에 넣고 commit한 뒤 그 SHA로 target.revision을 갱신해 Application을 다시 생성합니다. Application과 receipt는 workload 디렉터리 밖에 둡니다. Application 이름은 target·namespace·app을 결합하므로 AWS/GCP에 같은 앱을 배포해도 충돌하지 않습니다. 자동 sync·prune·force·namespace 생성은 켜지 않습니다.

앱 하나의 지원 범위는 단일 HTTP 서비스와 선택적 PostgreSQL 연결, 명시한 route·health endpoint, immutable image, linux/amd64입니다. `/health` 같은 절대 경로는 허용하며 prefix를 제거하거나 다시 쓰지 않습니다. `http` receipt의 `route`, `health_path`, `container_port`, `node_port`를 edge 연결에 사용합니다. ALB health check는 `health_path`를 그대로 사용하고 앱 요청 경로도 그대로 전달합니다. query·fragment·percent escaping·상위 경로 이동은 이 초기 계약에서 차단합니다. 임의 앱 secret·외부 egress·다중 서비스·arm64는 지원하지 않습니다. 여러 앱은 각각의 review 디렉터리와 Application으로 처리합니다. 같은 클러스터의 앱들은 서로 다른 NodePort를 할당해야 합니다.

Public registry는 `anonymous_manifest_read`와 null Secret 참조, private registry는 `authenticated_manifest_read`와 위 Secret 참조를 요구합니다. 이 결과는 CI에서 digest manifest에 접근한 증거이며 대상 노드에서 image layers를 pull한 증거가 아닙니다. renderer는 registry나 클러스터에 접속하지 않습니다. NetworkPolicy는 ingress CIDR/서비스 포트만 허용하고 egress를 차단합니다. NodePort는 `externalTrafficPolicy: Local`이며 **ALB target node에 실제 Pod가 있어야 합니다**. Cilium의 source IP 관측/정책 적용과 SG/WireGuard 라우팅은 실제 환경에서 함께 검증합니다.

## PostgreSQL 연결과 마이그레이션

게시된 workload가 `resources.postgres`를 요청하면 target에 다음 참조를 함께 등록해야 합니다. DB 생성·사용자 권한·TLS 인증서·아래 Secret 설치와 재조회는 환경 생성 실행자가 먼저 완료합니다. renderer는 이름과 사설 IP만 저장하며 Secret 값이나 연결 URL을 Git에 쓰지 않습니다.

```json
"database": {
  "host": "10.20.0.10",
  "port": 5432,
  "runtime_secret": "demo-runtime",
  "migration_secret": "demo-migration",
  "ca_secret": "demo-ca"
}
```

세 Secret은 workload namespace 안의 서로 다른 이름이어야 합니다. runtime Secret의 `DATABASE_URL`은 DML 전용 사용자, migration Secret의 `MIGRATION_DATABASE_URL`은 스키마 소유자 URL을 담습니다. 두 URL 모두 `sslmode=verify-full&sslrootcert=/etc/railshot/db/ca.crt`를 사용하고 인증서는 DB proxy IP SAN을 포함해야 합니다. CA Secret의 `ca.crt`만 해당 경로에 읽기 전용으로 마운트합니다. Deployment에는 runtime URL만 주입하고, 같은 이미지의 migration Job에는 migration URL을 `MIGRATION_DATABASE_URL`과 호환용 `DATABASE_URL`에 주입합니다. CI 임시 PostgreSQL과 실제 Patroni PostgreSQL의 major version은 16으로 맞춥니다.

배포 순서는 일반 Argo Sync wave의 NetworkPolicy `-2` → migration Job `-1` → Deployment `0`입니다. 사전 namespace 기본 차단 정책과 함께 사용하며 DB IP `/32`의 TCP 5432만 나갈 수 있습니다. 연결은 숫자 IP를 사용하므로 DNS 허용이 필요하지 않습니다. migration은 동일한 검증 이미지·보안 제한·CA를 사용하고, `backoffLimit: 0`, `restartPolicy: Never`, 300초 제한으로 실행합니다. Job이 실패하면 다음 wave로 진행하지 않습니다. Argo의 현재 revision 완료와 정확한 Job sync 결과가 있어야 `deployed: true`, `migration.state: succeeded`가 됩니다. 실제 SQL 응답·권한·TLS 연결 성공은 환경 E2E에서 별도로 확인합니다.

Job 이름은 이미지·명령·DB 참조의 해시에 고정되며 완료된 Job을 보존합니다. 같은 배포 ID 재호출은 기존 bridge의 읽기 전용 관측만 수행하므로 migration을 다시 시작하지 않습니다. **다른 migration으로 갱신할 때는 이전 소유 Job과 Git 선언을 운영자가 정리해야 하며, bridge가 자동 prune 없이 진행을 차단합니다.** 일반 앱 갱신·롤백에서 DB migration을 임의 재실행하거나 DB를 삭제하지 않습니다. AppProject와 namespace Role에는 DB migration이 있는 경우에만 `batch/Job`을 추가합니다.

## Argo 연결과 실행

기존 workload namespace, 해당 namespace에 제한된 runtime ServiceAccount/RBAC, private pull Secret, Argo 설치와 config repository 접근 권한을 운영자가 먼저 준비합니다. 운영 kubeconfig는 비공개 파일로 관리하고 `KUBECONFIG`로 선택합니다. 이 도구는 토큰이나 kubeconfig bytes를 명령 인수로 받지 않습니다.

```sh
# 지정 repo/server/namespace와 세 workload 종류만 허용하는 Project 선언 생성
python gitops/argo.py project /private/review-aws /private/review-gcp > /private/projects.json
kubectl --context railshot-control apply --server-side -f /private/projects.json

# 한 target의 scoped bearerToken + tlsClientConfig JSON만 stdin으로 전달
# config에는 insecure:false와 caData가 필요하며 exec/plugin/admin 자격 생성은 지원하지 않음
python gitops/argo.py register-cluster /private/review-aws --context railshot-control < /private/aws-argocd-auth.json
python gitops/argo.py register-cluster /private/review-gcp --context railshot-control < /private/gcp-argocd-auth.json

python gitops/argo.py sync /private/review-aws /private/review-gcp \
  --context railshot-control --repo /private/config-checkout --timeout 600
python gitops/argo.py verify /private/review-aws /private/review-gcp \
  --context railshot-control --repo /private/config-checkout --timeout 0
```

Project 생성은 선언 출력만 수행합니다. 이미 운영 중인 Project를 갱신할 때는 현재 앱 전체를 포함해 검토합니다. `register-cluster`는 project·namespace를 제한한 네이티브 Argo cluster Secret을 생성하고 다시 읽어 일치를 확인합니다. Secret의 namespace 목록을 축소해 기존 앱을 분리하지 않으며, 갱신 시 그 target의 모든 기존 namespace를 포함해야 합니다. 자격은 subprocess stdin으로만 전달하고 native stderr/stdout을 로그로 출력하지 않습니다. 인증 JSON은 repo·artifact에 넣지 않으며 scoped token 만료와 갱신은 운영자가 관리합니다. Secret 등록 성공은 cluster 연결 성공을 뜻하지 않습니다.

`sync`/`verify`는 live AppProject가 제한된 repo·runtime server·namespace와 Deployment/Service/NetworkPolicy 및 필요한 migration Job만 허용하는지 먼저 확인합니다. config checkout의 origin 및 `SHA:path/workload.json`을 receipt와 비교하고, 해당 Git 디렉터리에 다른 파일이 있으면 차단합니다. `sync`는 검토한 Application을 server-side apply한 뒤 같은 SHA의 네이티브 Argo operation을 요청합니다. 이미 같은 revision의 operation이 있으면 새 요청을 보내지 않고 관측합니다. `verify`는 읽기 전용입니다. 여러 앱 중 일부만 완료되면 앱별 결과와 `incomplete`를 반환합니다.

`rendered_for_review`와 `deployed: false`는 검토용 선언 생성 상태입니다. Argo CLI의 `deployed: true`는 live Application의 소유자·source·target이 일치하고 관측 revision이 고정 SHA이며, operation `Succeeded`, `Synced`, `Healthy`, 예상 리소스 및 이미지 목록이 모두 확인된 상태입니다. Argo 3에서 개별 resource health가 생략되는 경우 aggregate Application health를 사용합니다. 이는 외부 접속 완료와 구분해 `public_verified: false`, `url: null`로 반환합니다. 대상 노드의 실제 Pod imageID·Ready는 별도 E2E에서 확인해야 합니다. `imagePullPolicy: Always`도 캐시된 layers는 재사용할 수 있습니다. 제품 bridge는 이 Argo 관측 뒤 아래 HTTPS 검사를 수행합니다. Patroni 생성과 SQL·TLS 검증은 환경 생성 실행자가 수행합니다.

## 제품 백엔드 실행 연결

`apps/api/src/cd.js`의 `createCdAdapter({configPath, loadPublished})`는 비동기 `deployPublished` 함수를 반환합니다. `loadPublished`는 GitHub의 run/attempt/artifact ID와 source/target/tenant를 재검증해 원본 게시 파일 5개를 가져오는 서버 함수입니다. 클라이언트가 올린 파일이나 URL을 이 입력으로 사용하지 않습니다. Python 실행 환경에는 기존 CI와 같은 PyYAML/jsonschema가 필요하며, 서버의 `RAILSHOT_CD_CONFIG`는 아래 비공개 설정 파일을 가리킵니다.

```json
{
  "version": 1,
  "state_dir": "/private/railshot/cd-jobs",
  "repository": "/private/railshot/config-checkout",
  "branch": "deployments",
  "context": "railshot-control",
  "targets": {
    "k3s-aws": {
      "app": "demo",
      "tenant": "team",
      "target": {
        "id": "k3s-aws",
        "namespace": "tenant-demo",
        "argocd_namespace": "argocd",
        "project": "railshot",
        "architecture": "amd64",
        "repo_url": "https://github.com/example/config.git",
        "cluster_server": "https://192.0.2.1:6443",
        "path": "targets/k3s-aws/demo",
        "node_port": 30080,
        "ingress_cidrs": ["10.20.0.0/24"],
        "resources": {
          "requests": {"cpu": "100m", "memory": "128Mi"},
          "limits": {"cpu": "500m", "memory": "256Mi"}
        }
      },
      "public_http": {
        "url": "https://demo.example.com/health",
        "expected_json": {"status": "ready"}
      }
    }
  }
}
```

설정 파일은 실행 사용자 소유 0600, 상태 디렉터리는 0700이어야 합니다. `target`은 위 기존 handoff 계약을 그대로 사용하며 `revision`은 bridge가 생성한 Git commit으로 채웁니다. Private registry는 기존 `image_pull_secret` 참조도 target에 등록합니다. 전용 config checkout은 지정 branch에서 깨끗하고 원격과 일치해야 합니다. Git push 자격, 커밋 작성자, kubeconfig, 제한된 AppProject, namespace/pull Secret, Argo cluster 등록 및 공개 edge 경로는 운영자가 미리 준비합니다. 이 경로는 등록된 app/tenant 한 쌍의 namespace·NodePort·공개 URL을 갱신합니다. 다른 앱의 자동 namespace/NodePort/edge 할당은 지원하지 않습니다.

고정 명령 `python3 gitops/bridge.py --config /private/railshot/cd.json`에 다음 필드만 stdin JSON으로 전달합니다: `action: apply|observe`, `deployment_id`, `target_id`, 서버 시작 때 읽은 설정의 `config_sha256`, 재검증한 `publication`, 그리고 `files` 객체의 파일명별 base64 원본 5개. 설정이 바뀌면 실행 전에 차단하므로 진행 중인 배포가 새 환경으로 향하지 않습니다. 설정 변경 적용은 서버를 다시 시작해 새 요청에서 수행합니다. 앱·대상·원본 해시를 대조한 뒤 기존 `handoff.render`로 선언을 만들고, Git commit/push 및 원격 SHA 재조회, 기존 Argo 소유권·revision·image 검증, 마지막으로 등록된 HTTPS health 경로를 검사합니다. HTTP 200과 기대 JSON의 정확한 일치를 모두 요구하고 리다이렉트·환경 프록시를 사용하지 않습니다. 응답 본문과 native stderr는 제품 결과에 포함하지 않습니다. 이 HTTP 검사는 등록된 경로의 응답 증거이며 실제 Pod imageID 관측을 대신하지 않습니다.

stdout은 `{cd: {state, revision, deployed}, public_http: {state, verified_at, url}}`이며 오류 시 안전한 `error: {code, retryable, outcome_unknown}`를 추가합니다. 공개 검증 완료는 `public_http.state: succeeded`로 나타냅니다. bridge는 push 전에 의도를 영속 저장하고 같은 deployment ID의 재호출에서는 읽기 전용 관측만 수행합니다. 결과가 불명확하면 `unknown`을 반환하며 push/sync를 자동 재전송하지 않습니다. Node adapter는 최초 `apply` 뒤 `observe`만 제한 시간 내 polling합니다. 취소·시간 초과 시 Python과 native Git/kubectl을 포함한 프로세스 그룹을 종료하고 `unknown`을 반환합니다. 이미 원격에 접수된 작업이 취소됐다는 뜻은 아닙니다. 같은 ID에 다른 publication/설정을 보내면 충돌로 거부합니다.

```sh
python -m unittest discover -s gitops -p 'test_*.py'
```

테스트는 native kubectl 경계를 모의 실행하고 임시 로컬 bare Git에서 commit/push·고정 SHA·불명확 결과의 재실행 차단을 검사합니다. 별도 localhost HTTP 서버에서 기대 본문과 redirect 거부를 확인합니다. 실제 Argo 설치·cluster 등록·sync·공개 HTTPS 배포를 증명하지 않습니다.

공식 형식: [Argo Sync phases and waves](https://argo-cd.readthedocs.io/en/stable/user-guide/sync-waves/), [Argo declarative setup](https://argo-cd.readthedocs.io/en/stable/operator-manual/declarative-setup/), [kubectl sync operation](https://argo-cd.readthedocs.io/en/stable/user-guide/sync-kubectl/), [Argo 3 resource health 변경](https://argo-cd.readthedocs.io/en/stable/operator-manual/upgrading/2.14-3.0/#health-status-in-the-application-cr), [Kubernetes private registry Secret](https://kubernetes.io/docs/tasks/configure-pod-container/pull-image-private-registry/).
