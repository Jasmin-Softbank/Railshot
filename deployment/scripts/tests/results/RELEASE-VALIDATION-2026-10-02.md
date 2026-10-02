# Release 분리와 Linux 추가 검증 — 2026-10-02

기존 network/airgap 기능을 유지하고 Release 배포 도구와 실제 Linux 검사를 보완했습니다. **Source 최종 자동 검사: PASS 65 / FAIL 0 / SKIP 0**입니다. 단위·계약 49개와 Linux 시나리오 16개의 합계이며, 미실행 환경이나 mock(모의 응답)의 실제 원격 성공을 이 숫자에 포함하지 않습니다.

## 변경 파일과 구성

- `deployment/airgap/scripts/release.py`, `release-pack.sh`, `release-upload.sh`, `release-download.sh`: 로컬 압축·무결성 검사·명시적 draft 업로드·지정 버전 다운로드 도구입니다.
- `deployment/airgap/metadata/v0.1.0/`: 실제 arm64 manifest·checksum·metadata입니다. archive는 포함하지 않습니다.
- `deployment/.gitignore`: dist·downloads·생성 이미지 `.oci`를 제외했습니다.
- `deployment/scripts/network_preflight.py`: 이번 필수 목록에 맞춰 registry.k8s.io를 필수 검사로 추가했습니다.
- `deployment/scripts/tests/test_release.py`: 실제 작은 archive 압축/해제·손상·경로 탈출·재사용·원격 권한·draft/덮어쓰기 방지·신뢰 pin을 검사합니다. GitHub 호출은 mock입니다.
- `deployment/scripts/tests/test_airgap_vm.py`: CPU 구조 불일치와 동일 digest 재import 방지를 추가해 16개로 확장했습니다.
- `deployment/README.md`, `deployment/airgap/README.md`와 검증 기록을 최신화했습니다. 이전 host harness 로그의 source 절대경로는 `<SOURCE_REPOSITORY>`로 정규화했습니다.

기존 JSON → Adapter(입력 변환) → DeploymentSpec(공통 설정) → Engine(실행 엔진) → Result 구조를 유지합니다. Release 전달은 최초 Onboarding(노드 준비) 도구이며 core deploy 중 GitHub Release API를 호출하지 않습니다.

## 모드·Capability·호환성

`auto`는 실제 endpoint를 검사해 online 또는 검증 bundle 기반 airgap을 고릅니다. `online`은 필수 경로 접근을 요구하며 승인 버전을 유지합니다. `offline`은 외부 probe를 생략하고 로컬 artifact와 digest 고정·Never pull policy(외부 이미지 다운로드 금지)를 사용합니다.

DNS·HTTPS 443·승인 K3s installer/GitHub binary/Cilium CLI/chart·quay·registry.k8s.io·Docker Hub·workload registry를 검사합니다. GHCR은 workload가 사용하면 필수입니다. Cloudflare TCP/TLS 7844는 선택이며 WireGuard 인증 peer/key(상대 노드·키) 계약이 없어 UDP 51820은 null(미확인)입니다. 방화벽을 여는 기능은 구현하지 않습니다.

Cloudflare는 이미 구성된 공개 URL 검사 hook(선택 연결 지점)입니다. unavailable이면 앱은 ready를 유지하고 내부 endpoint와 degraded(공개 기능 불가) 상태를 반환합니다. 실제 connector provisioning(터널 생성)은 구현하지 않습니다.

기존 JSON `0.1` 필드·states enum(상태 목록)을 유지합니다. 새 옵션은 `0.2`를 명시하거나 CLI 옵션으로 선택합니다. 새 단계는 `network_states`에 별도로 기록합니다. 필수 preflight와 승인 버전 제약이 이전보다 엄격하므로 완전히 동일한 실행 동작이라고 주장하지 않습니다. 최신 탐지와 실제 설치 버전 선택은 분리합니다.

## Bundle와 Release 파일

Bundle은 K3s binary·installer·공식 system archive·Cilium CLI/chart/agent/operator/envoy·CoreDNS·pause·curl·nginx 샘플과 업데이트용 nginx를 포함합니다. 파일 11개, 별칭 포함 이미지 reference 17개, arm64 bundle 디렉터리 약 803 MiB입니다. 추가 앱은 prepare-bundle의 --image로 넣습니다. Runtime 버전은 K3s v1.34.11+k3s1 / Cilium 1.20.2 / CLI v0.20.1입니다.

| Release용 실제 파일 | 크기 bytes |
| --- | --- |
| railshot-airgap-arm64-v0.1.0.tar.zst | 747700166 (713.062 MiB) |
| bundle-manifest-arm64.json | 7194 |
| artifact-metadata-arm64.json | 795 |
| checksums-arm64.txt | 291 |

실제 파일은 로컬 `deployment/airgap/dist/v0.1.0/arm64/`에 있습니다. 큰 archive·이미지 tar·생성 bundle·downloads·dist는 Git에서 제외합니다. [Git에 보존한 metadata](../../../airgap/metadata/v0.1.0/artifact-metadata-arm64.json)와 checksum에는 실제 SHA256을 기록합니다.

amd64도 CPU별 4개 파일 이름/선택 경로가 있으나 **실제 amd64 bundle/archive 생성과 설치는 미검증**입니다. CPU 구조를 속여 arm64 파일을 amd64 asset으로 만들지 않았습니다.

기본 앱 이미지 저장소 정책은 GHCR, offline 배포 파일은 GitHub Release입니다. 공식 시스템 이미지는 공식 registry를 사용합니다. 앱 CI/GHCR push는 담당 범위 밖이며 실제 앱 E2E는 Docker Hub nginx입니다. 큰 파일/잦은 갱신은 향후 S3/GCS 같은 object storage(파일 저장 서비스)로 전달 계층을 바꿀 수 있습니다.

## 실제 실행 결과

Ubuntu 24.04 arm64 전용 VM에서 source repository 코드를 전달해 실행했습니다. 외부 차단은 시험 VM의 단독 nft table에서만 수행하며 Mac/CSP 방화벽을 바꾸지 않습니다. 전체 K3s 데이터 제거 후 offline 최초 설치를 확인했습니다.

| 시나리오 | 결과 | 소요 초 |
| --- | --- | --- |
| 01-online-clean | PASS | 134.073 |
| 02-online-with-bundle | PASS | 25.001 |
| 03-registry-fallback | PASS | 37.001 |
| 04-no-bundle | PASS | 0.373 |
| 05-offline-clean | PASS | 76.027 |
| 06-corrupt-bundle | PASS | 0.290 |
| 07-wrong-checksum | PASS | 0.074 |
| 08-missing-image | PASS | 0.788 |
| 09-offline-repeat | PASS | 22.766 |
| 10-offline-cleanup-redeploy | PASS | 27.927 |
| 11-offline-update | PASS | 28.000 |
| 12-network-restored | PASS | 32.991 |
| 13-cloudflare-unavailable | PASS | 11.995 |
| 14-preflight-timeout | PASS | 2.588 |
| 15-architecture-mismatch | PASS | 0.067 |
| 16-no-duplicate-import | PASS | 28.220 |

[Source Linux summary](release/source-linux/summary.json), [unit 로그](release/source-unit.log), [실제 Release archive의 Linux 압축 해제·bundle 검증](release/linux-release-extraction.json)을 보존했습니다. Metadata/checksum과 전체 bundle 파일·OCI blob digest를 검사했습니다. 재실행의 `imported_archives=[]`와 containerd 이미지 reference 목록 동일성을 실제로 확인했습니다.

- 정상 preflight 평균: **0.862초**, 인터넷 정상 4회 표본입니다. 보편적인 보장값은 아닙니다.
- Online warm deploy(기존 클러스터 재배포): **25.001초**입니다.
- Offline warm repeat: **22.766초**입니다.
- Online clean bootstrap: **134.073초**입니다.
- 외부 차단 후 Offline clean bootstrap: **76.027초**입니다.

## Release 인증·공개 상태와 미검증 범위

GitHub 인증과 대상 저장소 push 권한은 read-only(조회 전용)로 확인했습니다. **실제 Release 생성·업로드·원격 다운로드는 수행하지 않았습니다.** 업로드 도구는 정확한 tag·repository·파일을 요구하고, draft에만 추가하며, 덮어쓰기나 publish를 수행하지 않습니다. 새 초안은 --create-draft를 명시하고 정확한 commit SHA/notes를 전달해야 합니다.

실제 pack·재사용·압축 해제·Linux bundle 검증은 수행했습니다. 원격 upload/download는 단위 모의 검사만 수행했습니다. GitHub 원격 roundtrip, amd64 실행, 실제 AWS/GCP/OpenStack 자원·네트워크, GHCR workload pull, private registry 인증, WireGuard/QUIC/Tunnel 인증은 별도 실제 검사 대상입니다. 자동 테스트의 SKIP 0은 이 외부 미검증 목록까지 실행했다는 뜻이 아닙니다.

## Source 완료 조건·이관 규칙

Source: Jasmin-Softbank/Jasmin / feature/deployment-runtime-seungmin입니다. Target: Jasmin-Softbank/Railshot / feature/deployment-runtime-seungmin, base는 integration/team-assembly-20261002입니다. 현재 source 검사를 완료하고 깨끗한 commit을 만든 뒤, 별도 clone에 deployment/만 가져옵니다. Root README/Agents·infrastructure·CI·apps·gitops·observability·다른 담당 docs는 복사하지 않습니다.

이관 전 target 기존 deployment 203개 blob(파일 내용 식별값)은 source 이전 기반 ab13440과 모두 같았습니다. 실제 clone에서 다시 비교해 target 변경·삭제·덮어쓰기 충돌이 있으면 강제 처리하지 않습니다. 새 repository 위치에서 unit/shell/schema/contract/airgap 검사와 가능한 Linux 실행을 다시 수행합니다. 그 결과와 source/target commit·push·PR 정보는 별도 MIGRATION-VALIDATION 기록에 남깁니다. 이 source 기록은 target 검사 완료를 미리 주장하지 않습니다.

## 팀 합의 사항

JSON 0.2 채택·error.stage의 새 문자열 처리, bundle 저장·전달·신뢰 SHA256/서명·버전 승격 담당, GHCR credentials/CA, Cloudflare token/domain/auth와 WireGuard peer/key 계약이 필요합니다. 최초 Touch는 Runtime/bundle/권한/연결 준비이며 수동·반자동이어도 됩니다. 이후 반복 deploy는 저장한 bundle 입력으로 auto 선택합니다. 외부 인터넷 없이 배포하는 Internet-independent를 뜻하며, 중앙과 노드 통신까지 없는 Network-independent 원격 배포를 주장하지 않습니다.
