# CI → Argo CD 인계

`handoff.py`는 검증된 CI 게시 결과로 검토용 선언을 만들고, `argo.py`는 운영자가 등록한 Argo CD에서 그 선언의 sync와 결과를 확인합니다. 팀원 runtime bootstrap으로 준비한 AWS/GCP single-node K3s를 재사용하며, 클러스터 설치나 CI runner 등록을 수행하지 않습니다. 사용자 소스 저장소와 Argo가 읽는 배포 선언 저장소는 별개입니다. Argo Application은 config 저장소의 앱별 경로를 읽으므로 고객마다 원본 소스 저장소를 새로 등록할 필요는 없습니다.

```sh
python gitops/handoff.py /private/published /private/target.json /private/handoff-review
```

입력은 신뢰된 GitHub CI artifact 채널에서 받은 `images.json`, `jasmin.yaml`, `verdict.json`, `manifest.json`, `handoff.json`입니다. `handoff.json`은 **version 2**여야 하며 run/producer attempt/bundle artifact ID와 `registry` 접근 검증 결과를 출력 receipt에 유지합니다. registry 결과의 `images_sha256`은 `images.json` 해시와 같아야 합니다. v1은 private pull 계약을 표현하지 못하므로 거부합니다. 게시 artifact 자체의 ID는 receipt 본문에 없으며 API가 GitHub 응답에서 별도로 검증합니다. 이 CLI는 GitHub 조회를 수행하지 않으므로 신뢰된 게시 채널에서 확인한 파일만 전달해야 합니다. 파일 해시는 무결성 검사이며 서명이 아닙니다. 임의 업로드 artifact를 신뢰하지 않습니다. 원래 이미지 tar는 이 선언 생성 단계에서 재빌드하거나 실행하지 않습니다.

target JSON은 운영자가 제공합니다. 필수 필드는 `id`, `namespace`, `argocd_namespace`, 제한된 AppProject `project`, `architecture: amd64`, `repo_url`, Kubernetes API `cluster_server`, config repo `path`, 그 경로를 검토한 Git commit SHA `revision`, 할당한 `node_port`, 실제 라우팅 원본 `ingress_cidrs`, CPU(m)/memory(Mi) requests·limits `resources`입니다. 예시는 `test_handoff.py`의 target을 참고하세요. CI S/M/L을 운영 리소스 값으로 임의 변환하지 않습니다.

Private 이미지는 target에 `"image_pull_secret": {"namespace": "tenant-demo", "name": "ghcr-pull"}`을 추가합니다. namespace/name은 게시 receipt의 참조와 정확히 같고 namespace는 `target.namespace`와 같아야 합니다. 그러면 Deployment에 `imagePullSecrets: [{name: ghcr-pull}]`을 생성합니다. 이 참조는 설치할 Secret의 위치와 이름이며 Secret 존재나 pull 성공의 증거가 아닙니다. 실제 `kubernetes.io/dockerconfigjson` Secret 설치와 자격 갱신은 CD 담당자가 수행합니다. Secret bytes, registry token, Docker 인증 설정, kubeconfig는 입력 artifact나 생성 파일에 넣지 않습니다.

생성 파일은 Deployment/Service/NetworkPolicy를 묶은 `workload.json`, 별도 `application.json`, `receipt.json`입니다. receipt의 `documents`는 두 선언의 canonical JSON hash를 보존합니다. **workload.json만** 지정한 config repo path에 넣고 commit한 뒤 그 SHA로 target.revision을 갱신해 Application을 다시 생성합니다. Application과 receipt는 workload 디렉터리 밖에 둡니다. Application 이름은 target·namespace·app을 결합하므로 AWS/GCP에 같은 앱을 배포해도 충돌하지 않습니다. 자동 sync·prune·force·namespace 생성은 켜지 않습니다.

앱 하나의 첫 지원 범위는 단일 stateless HTTP 서비스, 명시한 route·health endpoint, immutable image, linux/amd64입니다. `/health` 같은 절대 경로는 허용하며 prefix를 제거하거나 다시 쓰지 않습니다. `http` receipt의 `route`, `health_path`, `container_port`, `node_port`를 edge 연결에 사용합니다. ALB health check는 `health_path`를 그대로 사용하고 앱 요청 경로도 그대로 전달합니다. query·fragment·percent escaping·상위 경로 이동은 이 초기 계약에서 차단합니다. DB·migration·앱 secret 주입·외부 egress·다중 서비스·arm64는 담당 계약이 필요합니다. 여러 앱은 각각의 review 디렉터리와 Application으로 처리합니다. 같은 클러스터의 앱들은 서로 다른 NodePort를 할당해야 합니다.

Public registry는 `anonymous_manifest_read`와 null Secret 참조, private registry는 `authenticated_manifest_read`와 위 Secret 참조를 요구합니다. 이 결과는 CI에서 digest manifest에 접근한 증거이며 대상 노드에서 image layers를 pull한 증거가 아닙니다. renderer는 registry나 클러스터에 접속하지 않습니다. NetworkPolicy는 ingress CIDR/서비스 포트만 허용하고 egress를 차단합니다. NodePort는 `externalTrafficPolicy: Local`이며 **ALB target node에 실제 Pod가 있어야 합니다**. Cilium의 source IP 관측/정책 적용과 SG/WireGuard 라우팅은 실제 환경에서 함께 검증합니다.

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

`sync`/`verify`는 live AppProject가 제한된 repo·runtime server·namespace와 Deployment/Service/NetworkPolicy만 허용하는지 먼저 확인합니다. config checkout의 origin 및 `SHA:path/workload.json`을 receipt와 비교하고, 해당 Git 디렉터리에 다른 파일이 있으면 차단합니다. `sync`는 검토한 Application을 server-side apply한 뒤 같은 SHA의 네이티브 Argo operation을 요청합니다. 이미 같은 revision의 operation이 있으면 새 요청을 보내지 않고 관측합니다. `verify`는 읽기 전용입니다. 여러 앱 중 일부만 완료되면 앱별 결과와 `incomplete`를 반환합니다.

`rendered_for_review`와 `deployed: false`는 검토용 선언 생성 상태입니다. Argo CLI의 `deployed: true`는 live Application의 소유자·source·target이 일치하고 관측 revision이 고정 SHA이며, operation `Succeeded`, `Synced`, `Healthy`, 예상 리소스 및 이미지 목록이 모두 확인된 상태입니다. Argo 3에서 개별 resource health가 생략되는 경우 aggregate Application health를 사용합니다. 이는 외부 접속 완료와 구분해 `public_verified: false`, `url: null`로 반환합니다. 대상 노드의 실제 Pod imageID·Ready와 외부 DNS/TLS/HTTP는 별도 E2E에서 확인해야 합니다. `imagePullPolicy: Always`도 캐시된 layers는 재사용할 수 있습니다. API의 CD 결과 소비와 다중 노드·Patroni는 담당자 계약에 맞춰 연결합니다.

```sh
python -m unittest discover -s gitops -p 'test_*.py'
```

테스트는 native kubectl 경계를 모의 실행하고 임시 로컬 Git에서 고정 SHA 검증을 수행합니다. 실제 Argo 설치·cluster 등록·sync·외부 배포를 증명하지 않습니다.

공식 형식: [Argo declarative setup](https://argo-cd.readthedocs.io/en/stable/operator-manual/declarative-setup/), [kubectl sync operation](https://argo-cd.readthedocs.io/en/stable/user-guide/sync-kubectl/), [Argo 3 resource health 변경](https://argo-cd.readthedocs.io/en/stable/operator-manual/upgrading/2.14-3.0/#health-status-in-the-application-cr), [Kubernetes private registry Secret](https://kubernetes.io/docs/tasks/configure-pod-container/pull-image-private-registry/).
