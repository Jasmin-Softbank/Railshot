# CI → Argo CD 인계

10/1 허들 2:56:27의 Argo CD 진행 합의를 위한 **로컬 검토용 연결 코드**입니다. 팀 CD 구현을 대체하지 않으며 Git push·cluster 등록·sync·성공 관측을 수행하지 않습니다. 사용자 소스 저장소와 Argo가 읽는 배포 선언 저장소는 별개입니다. Argo Application은 config 저장소의 앱별 경로를 읽으므로 고객마다 원본 소스 저장소를 새로 등록할 필요는 없습니다.

```sh
python gitops/handoff.py /private/published /private/target.json /private/handoff-review
```

입력은 신뢰된 GitHub CI artifact 채널에서 받은 `images.json`, `jasmin.yaml`, `verdict.json`, `manifest.json`, `handoff.json`입니다. `handoff.json`의 run/producer attempt/bundle artifact ID를 결과 receipt에 유지합니다. 게시 artifact 자체의 ID는 receipt 본문에 없으며 API가 GitHub 응답에서 별도로 검증합니다. 이 CLI는 GitHub 조회를 수행하지 않으므로 신뢰된 게시 채널에서 확인한 파일만 전달해야 합니다. 파일 해시는 무결성 검사이며 서명이 아닙니다. 임의 업로드 artifact를 신뢰하지 않습니다. 원래 이미지 tar는 이 선언 생성 단계에서 재빌드하거나 실행하지 않습니다.

target JSON은 운영자가 제공합니다. 필수 필드는 `id`, `namespace`, `argocd_namespace`, 제한된 AppProject `project`, `architecture: amd64`, `repo_url`, Kubernetes API `cluster_server`, config repo `path`, 그 경로를 검토한 Git commit SHA `revision`, 할당한 `node_port`, 실제 라우팅 원본 `ingress_cidrs`, CPU(m)/memory(Mi) requests·limits `resources`입니다. 예시는 `test_handoff.py`의 target을 참고하세요. CI S/M/L을 운영 리소스 값으로 임의 변환하지 않습니다.

생성 파일은 Deployment/Service/NetworkPolicy를 묶은 `workload.json`, 별도 `application.json`, `receipt.json`입니다. **workload.json만** 지정한 config repo path에 넣고 commit한 뒤 그 SHA로 target.revision을 갱신해 Application을 다시 생성합니다. Application과 receipt를 workload 디렉터리에 함께 넣으면 Argo directory 소스로 잘못 소비될 수 있으므로 분리합니다. 기존 namespace, 제한된 AppProject, cluster/repository credential 등록은 CD 담당자가 관리합니다. 자동 sync·prune·namespace 생성은 켜지 않습니다.

첫 지원 범위는 단일 stateless HTTP 서비스, `/` 경로, 명시한 health endpoint, 공개 immutable image, linux/amd64입니다. DB·migration·secret·외부 egress·prefix route·다중 서비스·arm64는 조용히 누락하지 않고 차단합니다. registry private 이미지는 이 함수가 익명 pull을 재검증하지 않으므로 trusted publisher의 public 판정과 대상 pull 인수가 먼저 필요합니다. NetworkPolicy는 ingress CIDR/서비스 포트만 허용하고 egress를 차단합니다. NodePort는 `externalTrafficPolicy: Local`이며 **ALB target node에 실제 Pod가 있어야 합니다**. Cilium의 source IP 관측/정책 적용과 SG/온프레 방화벽은 실제 환경에서 함께 검증합니다.

`rendered_for_review`는 배포 완료가 아닙니다. Git commit → Argo sync/health → rollout → Pod digest → 외부 위치의 DNS/TLS/HTTP 응답이 연결되어야 배포 완료로 판단합니다. 현재 통합본의 API는 이 CD 결과를 자동 소비하지 않으며 CI 게시까지만 표시합니다. 일반 앱의 CD 인수와 다중 노드·Patroni는 담당자 계약을 존중해 후속 계약으로 정합니다.

공식 형식: [Argo Application declarative setup](https://argo-cd.readthedocs.io/en/stable/operator-manual/declarative-setup/).
