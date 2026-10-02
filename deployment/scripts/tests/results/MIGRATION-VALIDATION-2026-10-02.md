# Railshot 이관 검증 — 2026-10-02

Jasmin의 검증된 `deployment/` 영역을 Railshot의 별도 feature branch로 가져왔습니다. **최종 Target 자동 검사: PASS 65 / FAIL 0 / SKIP 0**입니다. Unit/contract(입출력 규약) 49개와 실제 Linux 시나리오 16개의 합계입니다. 처음 실패한 실행도 삭제하지 않았습니다. 추가 실제 offline deploy/verify/cleanup 결과는 아래 원본으로 보존합니다.

## 저장소·브랜치·범위

| 구분 | 실제 값 |
| --- | --- |
| Source | Jasmin-Softbank/Jasmin |
| Source branch | feature/deployment-runtime-seungmin |
| Source commit | ec6a9df0258eee9843aa457bc10703aa7026db40 |
| Target | Jasmin-Softbank/Railshot |
| Target base | integration/team-assembly-20261002 |
| Base commit | adda5c7532c22da95e938a3cf1a7ba4c05e4ca47 |
| Target head branch | feature/deployment-runtime-seungmin |
| PR 방향 | feature/deployment-runtime-seungmin → integration/team-assembly-20261002 |

Source에서 테스트·README·검증 기록·대용량 파일 제외·깨끗한 commit/push를 확인한 뒤 별도 clone에서 작업했습니다. `old-jasmin` remote를 연결하고 **deployment/만** 가져왔습니다. Root README·Agents/AGENTS·infrastructure·ci·apps·gitops·observability·contracts·다른 담당 docs는 변경하지 않았습니다. 외부 docs에서 추가로 복사해야 하는 필수 문서는 발견하지 않았습니다.

[파일 비교](migration/comparison.json)에서 target 기존 파일 203개가 source 이전 기반과 동일했습니다. 초기 이관은 **기존 12개 의도된 수정 + 신규 262개**, 삭제 0개·충돌 0개였습니다. 추가 target 변경은 시험 CLI 대기 옵션과 이 README/검증 기록입니다. 충돌을 강제 해결하거나 integration에 직접 push하지 않습니다.

## 변경 파일 목록

- `deployment/.gitignore`
- `deployment/README.md`
- `deployment/airgap/README.md`
- `deployment/airgap/check-updates.sh`
- `deployment/airgap/install-offline.sh`
- `deployment/airgap/preload-images.sh`
- `deployment/airgap/prepare-bundle.sh`
- `deployment/airgap/release-download.sh`
- `deployment/airgap/release-pack.sh`
- `deployment/airgap/release-upload.sh`
- `deployment/airgap/scripts/bundle.py`
- `deployment/airgap/scripts/check_updates.py`
- `deployment/airgap/scripts/prepare.py`
- `deployment/airgap/scripts/release.py`
- `deployment/airgap/verify-bundle.sh`
- `deployment/airgap/versions.json`
- `deployment/bootstrap/cleanup.sh`
- `deployment/bootstrap/install-k3s.sh`
- `deployment/cilium/install.sh`
- `deployment/scripts/engine.py`
- `deployment/scripts/exposure.py`
- `deployment/scripts/input_adapter.py`
- `deployment/scripts/models.py`
- `deployment/scripts/network_preflight.py`
- `deployment/scripts/render.py`
- `deployment/scripts/runtime.py`
- `deployment/scripts/schemas/input-v0.2.schema.json`
- `deployment/scripts/schemas/output-v0.2.schema.json`
- `deployment/scripts/tests/test_airgap.py`
- `deployment/scripts/tests/test_airgap_vm.py`
- `deployment/scripts/tests/test_engine.py`
- `deployment/scripts/tests/test_release.py`

`deployment/airgap/metadata/v0.1.0/`의 실제 manifest/checksum/metadata, `deployment/scripts/tests/results/airgap/`, `release/`, `migration/`의 원본 JSON·로그·보고서도 추가했습니다. 전체 path 목록은 feature branch와 base 사이의 `git diff --name-only`로 확인할 수 있습니다. Runtime 책임 밖의 파일과 큰 archive는 포함하지 않았습니다.

## Architecture(구성)와 호환성

JSON → Input Adapter(입력 변환) → DeploymentSpec(공통 설정) → Deployment Engine(실행 엔진) → DeploymentResult(결과)를 유지했습니다. Node prerequisites(노드 전제조건)·잠금 이후 Network preflight(통신 사전 검사)·승인 버전·bundle 검증을 거쳐 동일 K3s/Cilium/workload/Service/HTTP 흐름을 사용합니다.

- auto: 필수 source/registry에 접근하면 online, 불가하면 유효한 로컬 bundle을 사용합니다. 둘 다 없으면 JSON 오류입니다.
- online: 필수 외부 경로를 요구하며 승인 버전을 유지합니다. 지정한 bundle의 로컬 artifact는 재사용할 수 있습니다.
- offline: 외부 probe/최신 조회/공개 URL 검사를 생략하고 로컬 binary·chart·이미지만 사용합니다. Cilium/앱/검사 이미지는 digest(불변 식별값)와 Never pull policy(다운로드 금지)를 사용합니다.

DNS·HTTPS 443·고정 K3s/GitHub/Cilium source·quay·registry.k8s.io·Docker Hub·workload registry를 검사합니다. GHCR은 workload가 사용할 때 필수입니다. Cloudflare TCP/TLS 7844는 선택이고 authenticated(인증된) WireGuard peer/key 계약이 없어 UDP 51820은 null(미확인)입니다. Firewall·SG·VPC·Terraform·Provider provisioning은 구현하지 않았습니다.

Cloudflare는 optional exposure(선택 공개 경로)의 이미 구성된 URL 검사 hook입니다. 실패 시 앱 ready와 내부 NodePort endpoint를 유지하고 exposure만 degraded로 반환합니다. 실제 connector 생성·인증 성공을 검증했다고 표현하지 않습니다.

0.1 출력 필드와 states enum(상태 목록)은 유지합니다. 새 옵션은 0.2에 추가하며 NETWORK_CHECKING/BUNDLE_VERIFYING/AIRGAP_PRELOADING은 network_states로 기록합니다. 기본 auto preflight·승인 profile 제약은 이전보다 엄격합니다. Bundle 배포 후 verify에도 같은 bundle 입력을 전달해야 합니다.

## Airgap bundle·Release 파일·Git 제외

K3s binary/installer/system images·Cilium CLI/chart/3개 이미지·CoreDNS/pause·curl·nginx 샘플 및 업데이트 이미지를 포함합니다. 실제 arm64 bundle은 11개 파일, alias(별칭) 포함 이미지 reference 17개, 디렉터리 약 803 MiB입니다. Image digest·파일 SHA256·manifest pin·OCI blob을 검사하며 같은 digest의 preload는 재사용합니다.

| 실제 arm64 Release용 asset | bytes |
| --- | --- |
| railshot-airgap-arm64-v0.1.0.tar.zst | 747700166 (713.062 MiB) |
| bundle-manifest-arm64.json | 7194 |
| artifact-metadata-arm64.json | 795 |
| checksums-arm64.txt | 291 |

실제 파일은 새 clone의 `deployment/airgap/dist/v0.1.0/arm64/`에도 복사하고 SHA256·동일 bundle 재사용을 검증했습니다. Git에는 [metadata](../../../airgap/metadata/v0.1.0/artifact-metadata-arm64.json) 등 작은 파일만 넣었습니다. 큰 tar.zst·image archive·generated bundle·dist/downloads·.oci는 제외합니다. CPU 구조별 파일명을 분리해 Release 이름 충돌을 피합니다. **amd64 asset/설치는 실제 생성·검증하지 않았습니다.**

GitHub Release는 offline 파일의 기본 배포 정책이며 앱 온라인 이미지 정책은 GHCR입니다. 실제 앱 Linux E2E는 Docker Hub nginx입니다. GHCR workload pull·private credentials는 미검증입니다. 향후 S3/GCS object storage로 전달 계층을 바꿀 수 있습니다.

Pack 도구는 실제 파일을 만들었으며 실제 Linux에서 Release archive 압축 해제·bundle 검증을 통과했습니다. 업로드·다운로드 helper는 인증/권한·정확한 버전·신뢰 pin·draft/덮어쓰기 방지를 구현하고 unit mock으로 검사했습니다. **GitHub Release 실제 생성·업로드·원격 다운로드는 수행하지 않았습니다.** 업로드 helper는 publish하지 않습니다. 최신 조회는 실행 버전 선택과 분리하며 승인 profile 밖의 버전을 자동 설치하지 않습니다.

## 새 위치에서 실제 검사

새 Railshot 위치에서 Python unit/contract/airgap/Release 테스트 49개, 모든 Bash syntax(구문), Python AST(구문 구조), schema·README 상대 링크·기존 source 절대경로 제거를 확인했습니다. Source의 실제 JSON 36쌍과 target 실행의 실제 JSON을 해당 schema로 검사했습니다.

Linux VM에는 새 checkout의 code를 전달하고 [44개 파일 해시](migration/code-hashes.json)를 비교했습니다. 새 target code와 실제 guest 파일은 동일합니다. Linux bundle은 **실제 Release archive에서 압축 해제한 `/var/tmp/railshot-release-package-test`**를 사용했습니다. Source 경로가 있어야 동작하는 구조가 아닙니다.

| 시나리오 | 최종 결과 | 소요 초 |
| --- | --- | --- |
| 01-online-clean | PASS | 302.554 |
| 02-online-with-bundle | PASS | 21.005 |
| 03-registry-fallback | PASS | 28.645 |
| 04-no-bundle | PASS | 0.452 |
| 05-offline-clean | PASS | 77.532 |
| 06-corrupt-bundle | PASS | 0.295 |
| 07-wrong-checksum | PASS | 0.060 |
| 08-missing-image | PASS | 0.687 |
| 09-offline-repeat | PASS | 23.112 |
| 10-offline-cleanup-redeploy | PASS | 26.029 |
| 11-offline-update | PASS | 27.824 |
| 12-network-restored | PASS | 33.170 |
| 13-cloudflare-unavailable | PASS | 12.804 |
| 14-preflight-timeout | PASS | 2.585 |
| 15-architecture-mismatch | PASS | 0.060 |
| 16-no-duplicate-import | PASS | 31.608 |

[Target 최종 summary](migration/final/summary.json), [unit 로그](migration/target-unit.log), [추가 실제 CLI 결과](migration/supplement/summary.json), [schema 검사](migration/schema-validation.json)를 보존했습니다. 완전 offline은 해당 VM에서 외부 DNS/TCP/UDP를 차단하고 기존 K3s cache/config/binary를 지운 뒤 실제 설치했습니다. Mac/CSP/행사장 네트워크는 변경하지 않았으며 자체 시험 table을 제거했습니다. 기존 3개 데모 endpoint도 [HTTP 200 복원 확인](migration/restored-demo-endpoints.json)을 마쳤습니다. 테스트 전용 VM은 K3s/시험 규칙 제거와 sync 후 정상 종료가 지연되어 강제 정지했으며 VM 자체는 삭제하지 않았습니다.

### 시간 측정

- 정상 preflight 평균: **0.803초**, 정상 필수 경로 4회 표본입니다.
- Online warm deploy: **21.005초**입니다.
- Offline repeated deploy: **23.112초**입니다.
- Online clean bootstrap: **302.554초**입니다.
- Offline clean bootstrap: **77.532초**입니다.

실행 환경·registry bandwidth에 따라 달라지는 관측값이며 성능 보장값은 아닙니다. Target retry의 readiness 입력은 600초였습니다. Runtime 기본 180초와 별도 preflight 제한은 유지했습니다.

### 첫 실패와 원인

[최초 target summary](migration/attempt1/summary.json)는 **PASS 0 / FAIL 1 / SKIP 15**입니다. Online clean에서 Cilium agent/operator 이미지를 내려받는 동안 readiness 제한 180초를 넘었습니다. [실제 image pull 시간 로그](migration/cold-pull-latency.log)에서 operator 약 3분 34초, agent 약 3분 45초를 확인했습니다. 다운로드가 느려진 네트워크 원인까지는 규명하지 않았습니다.

Endpoint/TLS/auth challenge가 응답한다고 대용량 layer 다운로드 시간이나 권한까지 보장되지 않습니다. Runtime/승인 버전/네트워크 설정을 바꾸지 않고 시험 도구에 --runtime-timeout 옵션을 추가하여 기존 입력 허용 범위 내에서 600초로 다시 실행했습니다. 그 후 16개 전부 통과했습니다. 기본 180초 cold bootstrap의 이 실패를 숨기거나 항상 통과한다고 주장하지 않습니다. 운영에서는 검증 bundle 전달·명시적인 timeout·외부 layer 접근 검사를 합의해야 합니다.

## 실행과 PR

```bash
sudo ./deployment/scripts/deploy.sh --mode auto \
  --bundle /var/lib/railshot-deployment/bundle --input input.json
sudo ./deployment/scripts/verify.sh --mode offline \
  --bundle /var/lib/railshot-deployment/bundle --input input.json
sudo ./deployment/scripts/cleanup.sh --input input.json --all --disposable-node
```

최종 target commit/push는 테스트와 source scope·대용량 파일 제외 확인 뒤 수행합니다. 이 보고서를 포함한 commit SHA는 PR head와 최종 응답에 기록합니다. PR base/head는 위 표와 같고 integration에 직접 push하지 않습니다.

## 미검증 항목과 합의할 contract(연결 규약)

실제 AWS/GCP/OpenStack IAM/SG/VPC/Floating/External IP는 **requires real cloud smoke test**입니다. amd64·다른 OS·WireGuard/QUIC/Tunnel 인증·GHCR 실제 workload/private auth·GitHub 원격 Release roundtrip·장기운영/전원차단·Runtime version upgrade는 미검증입니다. Unit mock 성공을 실제 외부 서비스 검증으로 표현하지 않습니다.

팀에서는 JSON 0.2 채택·알 수 없는 error.stage 처리, bundle 저장/전달/신뢰 SHA256 또는 서명, approved version 승격·검사 담당, registry credentials/CA, Tunnel token/domain/auth, WireGuard peer/key, cold bootstrap timeout과 외부 endpoint 검사 책임을 합의해야 합니다. 첫 Touch는 node/runtime/bundle/접속 권한 준비이며 이후 deploy는 auto로 선택합니다. Internet-independent(외부 인터넷 없이 내부망/로컬 artifact로 배포 가능)를 뜻하며 Network-independent(중앙과 노드 간 통신조차 없는 원격 배포)를 주장하지 않습니다.
