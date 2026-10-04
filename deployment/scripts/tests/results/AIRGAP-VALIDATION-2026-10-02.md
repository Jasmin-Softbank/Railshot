# Airgap 검증 기록 — 2026-10-02

`feature/deployment-runtime-seungmin`에서 기존 공통 Runtime을 유지하며 network capability preflight(필요한 통신 경로 사전 검사), 로컬 airgap bundle(인터넷 없는 환경용 설치 묶음), `auto/online/offline` 경로와 선택 exposure(외부 노출)를 추가했습니다. 변경은 `deployment/` 안에 한정했습니다.

**최종 자동 테스트: PASS 54 / FAIL 0 / SKIP 0입니다.** 이는 단위·계약 검사 40개와 아래 전용 Linux VM 시나리오 14개의 합계입니다. 미검증 환경을 이 수치에 포함하지 않습니다. 최초 VM 실행의 실패도 아래에 보존했습니다.

## 1. 변경 파일

- `deployment/.gitignore`
- `deployment/README.md`
- `deployment/bootstrap/cleanup.sh`
- `deployment/bootstrap/install-k3s.sh`
- `deployment/cilium/install.sh`
- `deployment/scripts/engine.py`
- `deployment/scripts/input_adapter.py`
- `deployment/scripts/models.py`
- `deployment/scripts/render.py`
- `deployment/scripts/runtime.py`
- `deployment/scripts/tests/test_engine.py`
- `deployment/airgap/README.md`
- `deployment/airgap/check-updates.sh`
- `deployment/airgap/install-offline.sh`
- `deployment/airgap/preload-images.sh`
- `deployment/airgap/prepare-bundle.sh`
- `deployment/airgap/scripts/bundle.py`
- `deployment/airgap/scripts/check_updates.py`
- `deployment/airgap/scripts/prepare.py`
- `deployment/airgap/verify-bundle.sh`
- `deployment/airgap/versions.json`
- `deployment/scripts/exposure.py`
- `deployment/scripts/network_preflight.py`
- `deployment/scripts/schemas/input-v0.2.schema.json`
- `deployment/scripts/schemas/output-v0.2.schema.json`
- `deployment/scripts/tests/test_airgap.py`
- `deployment/scripts/tests/test_airgap_vm.py`

이 보고서 `deployment/scripts/tests/results/AIRGAP-VALIDATION-2026-10-02.md`도 추가했습니다. 원본 실행 증거 파일 목록은 해당 `airgap/` 디렉터리를 확인하시면 됩니다. 실행 증거는 `deployment/scripts/tests/results/airgap/` 아래에 추가했습니다. 약 803 MiB의 실행 bundle은 **로컬 `deployment/airgap/bundles/2026-10-02.1-arm64/`에 있으며 Git에서 제외**했습니다. Git checkout만으로 offline 실행 파일이 전달되는 것은 아닙니다. 전체 bundle을 별도 전달해야 합니다.

## 2. Architecture(구성)

```text
JSON 0.1/0.2 → Input Adapter → DeploymentSpec
    → 전용 노드 전제조건·잠금
    → Network Capability Preflight
    → 승인된 버전 확인 + 명시한 bundle 검증
    → Online 또는 Airgap 선택
    → 로컬 K3s binary/system images 준비 또는 승인 artifact 다운로드
    → K3s API 준비 → 이미지 preload → Cilium·노드 Ready
    → Workload Deployment/Service → Pod DNS/Service HTTP 검사
    → NodePort HTTP 검사 → 선택 Exposure → DeploymentResult JSON
```

Provider 이름은 요청 문맥으로 보존하며 엔진에는 provider 분기문을 추가하지 않았습니다. AWS/GCP/OpenStack 자원 생성·방화벽·Terraform은 구현하지 않았습니다. NetworkCapability가 전체 인터넷 유무 boolean(참/거짓) 하나로 축약되지 않습니다.

## 3. CLI(실행 명령)

대상 **전용 Linux 노드**의 저장소 루트에서 실행합니다. root(관리자)·systemd·Python 3.10 이상·기본 OS 도구가 필요합니다. 전달과 원격 접속은 상위 계층 책임입니다.

```bash
# 연결된 같은 CPU 구조의 Linux builder에서 한 번 준비합니다.
# builder에는 실행 중인 K3s/containerd가 이미 있어야 합니다.
sudo ./deployment/airgap/prepare-bundle.sh \
  --input deployment/scripts/tests/fixtures/aws.json \
  --output /var/tmp/railshot-bundle --bundle-version 2026-10-02.1 \
  --image nginx:1.28.1-alpine

# bundle 전체를 대상에 전달한 후 검증하고 배포합니다.
sudo ./deployment/airgap/verify-bundle.sh --bundle /var/lib/railshot-deployment/bundle
sudo ./deployment/scripts/deploy.sh --mode auto \
  --bundle /var/lib/railshot-deployment/bundle --input input.json \
  > result.json 2> deploy.log

sudo ./deployment/scripts/deploy.sh --mode online --input input.json
sudo ./deployment/scripts/deploy.sh --mode offline \
  --bundle /var/lib/railshot-deployment/bundle --input input.json
sudo ./deployment/scripts/verify.sh --mode offline \
  --bundle /var/lib/railshot-deployment/bundle --input input.json
sudo ./deployment/scripts/cleanup.sh --input input.json
sudo ./deployment/scripts/cleanup.sh --input input.json --all --disposable-node

# 조회만 수행하며 설치 버전을 바꾸지 않습니다.
./deployment/airgap/check-updates.sh
```

입력·출력 예시는 [Deployment README](../../../README.md#네트워크-검사와-onlineoffline-배포)에 있으며 실제 입력은 [offline input](airgap/final/05-offline-clean.input.json), 결과는 [offline result](airgap/final/05-offline-clean.json)로 보존했습니다. 증거의 `/home/...` bundle 경로는 시험 VM 경로이므로 자신의 노드 경로로 바꿔야 합니다. 새 정보는 `schema_version: "0.2"`로 받습니다. stdout은 JSON 한 개, stderr는 단계별 로그입니다.

## 4. 모드 차이

| 모드 | 동작 |
| --- | --- |
| auto(기본) | 필수 경로가 가능하면 online입니다. 불가하고 유효한 bundle이 있으면 airgap으로 전환하며, 둘 다 없으면 `BUNDLE_REQUIRED`로 실패합니다. |
| online | 필수 외부 경로를 요구하며 실패 시 `NETWORK_UNAVAILABLE`입니다. bundle을 지정하면 검증한 로컬 artifact도 재사용하지만 자동 airgap 전환은 하지 않습니다. |
| offline | 외부 probe·최신 버전 조회·공개 URL 확인을 생략하고 bundle만 사용합니다. Cilium·앱·검사 이미지에 `imagePullPolicy: Never`를 사용합니다. |

명시한 bundle이 손상되면 online에서도 무시하지 않고 실패합니다. auto의 중간 artifact/이미지 접근 오류 재시도는 한 번으로 제한합니다. 이 중간 실패 분기는 단위 검사로 검증했고 실제 중간 다운로드 단절은 재현하지 않았습니다. 앱 readiness(준비 상태)나 잘못된 포트 오류를 fallback으로 숨기지 않습니다.

## 5. Network preflight(통신 사전 검사)

- DNS 해석 결과, 실제 HTTPS/TLS 응답, 승인된 K3s installer·GitHub binary·Cilium CLI·chart source를 확인합니다.
- quay.io·Docker Hub와 workload registry를 필수로 확인합니다. registry.k8s.io·GHCR은 기본 진단 대상이고 해당 workload를 사용하면 필수입니다.
- 요청된 Cloudflare 경로는 TCP/TLS 7844만 검사합니다. 실패해도 workload 배포를 막지 않습니다. QUIC(UDP 연결)이나 인증 성공을 의미하지 않습니다.
- 인증된 peer/key(상대 노드·키) 계약이 없는 WireGuard UDP 51820은 `null`입니다. UDP를 보냈다는 사실만으로 성공 처리하지 않습니다.
- curl timeout은 기본 3초이며 병렬입니다. 실험에서는 2초로 설정한 응답 없는 TLS endpoint를 사용해 사전 검사 자체는 **2.029초**, deploy의 JSON 오류 반환까지 **2.090초**였습니다. 시험 서버 정리까지 포함한 시나리오 소요 시간은 2.584초입니다.
- Registry HTTP 401은 접속 가능성을 뜻하지만 실제 이미지 권한·tag·CDN 다운로드를 보장하지 않습니다.
- `endpoint_overrides`는 probe 대상만 바꿉니다. 실제 다운로드 URL이나 containerd mirror를 구성하지 않습니다.

## 6. Bundle manifest(설치 목록)

[실제 전체 manifest](airgap/bundle-manifest.json)와 [manifest SHA256](airgap/manifest.sha256)을 보존했습니다. bundle `2026-10-02.1`, `linux/arm64`, 파일 11개와 이미지 reference(이름·digest 별칭 포함) 17개입니다. K3s `v1.34.11+k3s1`, Cilium `1.20.2`, CLI `v0.20.1`을 사용했습니다.

```json
{
  "schema_version": "1",
  "bundle_version": "2026-10-02.1",
  "platform": "linux/arm64",
  "runtime": {"k3s_version": "v1.34.11+k3s1", "cilium_version": "1.20.2", "cilium_cli_version": "v0.20.1"},
  "files": {"k3s": {"path": "artifacts/k3s-arm64", "sha256": "272f45b9efc69d0bbdb7042156156c6903087829a5003d4593af0ad2d08d76d4", "size": 70451362}},
  "images": [{"reference": "quay.io/cilium/cilium@sha256:2939231d0d3e3ebddcd80fffa168b7ddcc78fdf0dc864d1c8c126ff523c54f01", "digest": "sha256:2939231d0d3e3ebddcd80fffa168b7ddcc78fdf0dc864d1c8c126ff523c54f01", "archive": "image_5", "role": "cilium_agent"}]
}
```

위 JSON은 축약 설명이며 실행용 전체 manifest가 아닙니다. 바이너리·archive SHA256, OCI(표준 이미지 형식) blob digest·이름 대응·필수 이미지·CPU 구조·승인 버전을 검사합니다. 자체 checksum은 서명이 아닙니다. Onboarding(최초 노드 준비) 때 신뢰한 manifest digest를 별도 전달해 `--bundle-sha256`으로 고정해야 출처 신뢰를 추가할 수 있습니다.

## 7. Preload(이미지 사전 적재)·버전 정책

K3s 공식 system archive를 `/var/lib/rancher/k3s/agent/images/`에 배치하고 로컬 binary·installer에 `INSTALL_K3S_SKIP_DOWNLOAD=true`를 사용합니다. 추가 OCI archive는 K3s의 containerd `k8s.io` 공간에 native platform과 digest를 지정해 import합니다. 같은 reference/digest가 있으면 import하지 않으며 content(이미지 데이터)가 실제 있는지도 확인합니다.

[반복 offline 결과](airgap/final/09-offline-repeat.json)의 `imported_archives`는 빈 배열이고 테스트에서 동일 Pod UID를 확인했습니다. [Cilium 실제 설치·초기화 이미지](airgap/final/offline-cilium-images.json)와 [workload](airgap/final/offline-workload.json)에서 digest와 `Never` 설정을 확인했습니다. K3s 서비스 재시작 시 공식 system archive 재읽기는 가능하며 이 작업은 부팅별 import 생략을 주장하지 않습니다.

최신 조회와 실행 버전 선택은 분리했습니다. [실제 최신 조회](airgap/latest-detection.json)에서 K3s 신규 버전이 탐지되었지만 설치에는 승인한 `v1.34.11+k3s1`을 유지했습니다. 새로운 runtime 버전은 profile/bundle 후보 작성·compatibility 검사·팀 승인 후 승격해야 합니다. 승인 gate(검사·승인 절차)의 자동화는 구현하지 않았습니다.

## 8. 실제 Linux 검증

Ubuntu 24.04.4, kernel 6.8, arm64, CPU 2개/RAM 4 GiB의 전용 Lima VM `railshot-sim-aws-20261002080234`에서 실행했습니다. provider=`aws` fixture를 쓰지만 **실제 AWS VM이나 네트워크가 아닙니다.** 앱은 nginx이며 DB/API 외부 의존성은 없습니다.

| 순서 | 실제 시나리오 | 결과 | 소요 초 |
| --- | --- | --- | --- |
| 1 | Online 최초 설치 | PASS | 138.335 |
| 2 | Online + bundle 존재 | PASS | 23.748 |
| 3 | Registry 실패 + bundle → auto fallback | PASS | 30.510 |
| 4 | Registry 실패 + bundle 없음 → JSON 오류 | PASS | 0.342 |
| 5 | 외부 DNS/TCP/UDP 차단 + 전체 캐시 제거 후 Offline 최초 설치 | PASS | 78.758 |
| 6 | 손상 bundle 거부 | PASS | 0.256 |
| 7 | 잘못된 checksum 거부 | PASS | 0.059 |
| 8 | Workload 이미지 누락 거부 | PASS | 0.664 |
| 9 | Offline 반복 배포: 동일 Pod UID·추가 import 없음 | PASS | 20.949 |
| 10 | Offline workload cleanup/redeploy | PASS | 27.922 |
| 11 | Offline nginx 1.28.0 → 1.28.1 update | PASS | 27.001 |
| 12 | 네트워크 복구 후 Online 재실행 | PASS | 31.936 |
| 13 | Cloudflare 불가: 앱 ready·공개 경로 degraded | PASS | 13.037 |
| 14 | TLS 응답 없는 endpoint timeout 제한 | PASS | 2.584 |

표 형태 CLI 로그 일부의 줄 끝 공백만 제거했습니다. 명령 내용·결과·오류는 유지했습니다. 원본 [최종 summary](airgap/final/summary.json), [VM stderr](airgap/vm-final.log), [자동 단위·계약 검사 로그](airgap/unit-tests.log)를 확인할 수 있습니다. 최종 입력·출력 JSON 17쌍을 실제 schema 0.2로 검사했습니다.

완전 offline 검사는 **K3s binary/data/config까지 전체 제거**한 후 VM의 전용 nft table(방화벽 규칙)에서 eth0의 외부 DNS/TCP/UDP와 전달 트래픽을 차단했습니다. 관리용 SSH 응답만 허용했습니다. [외부 접속 실패 증거](airgap/final/external-block-proof.json), [차단 규칙](airgap/final/offline-firewall.nft), [새 노드 확인](airgap/final/offline-clean-node.txt)을 보존했습니다. Mac 호스트·공유기·CSP 방화벽은 변경하지 않았습니다.

추가 offline 설치·반복 배포·verify·전체 cleanup을 수행했고, 외부 차단 상태에서 **Mac → Lima SSH 전달 → VM NodePort**까지 HTTP 200 및 본문을 확인했습니다. [Mac endpoint 증거](airgap/mac-offline-endpoint.json), [추가 감사 결과](airgap/offline-audit/result.json), [추가 verify](airgap/offline-audit/verify.json)에 기록했습니다. 인터넷 공개 URL 검증은 아닙니다. 추가 상태 조회는 `KUBECONFIG` 지정 없이 실행해 한 번 실패했으며 경로를 지정해 다시 조회했습니다. Runtime 자체는 kubeconfig 경로를 지정합니다.

시험 종료 시 자체 nft table과 시험 K3s를 제거하고 VM을 중지했습니다. 기존 demo VM을 복원하고 기존 endpoint들을 확인했습니다. 최종 코드의 builder로 [신규 후보 bundle 생성](airgap/builder-final.json)과 [기존 bundle 재사용](airgap/builder-reuse.json)을 확인했습니다. 이 후보는 실행 기본 bundle로 자동 승격하지 않았습니다. [Builder 이미지 공간](airgap/builder-namespaces.txt)은 기존 `k8s.io`만 남았으며, [기존 endpoint 3개](airgap/restored-demo-endpoints.json)도 모두 HTTP 200으로 복원됐습니다.

## 9. 발견·수정한 문제와 실패 기록

- 첫 VM 시도에서 `ctr images check --platform`이 K3s 포함 containerd의 지원 옵션이 아니어서 실패했습니다. native 검사로 수정했습니다. [attempt1 summary](airgap/attempt1/summary.json)는 **PASS 1 / FAIL 1 / SKIP 12**이며 삭제하지 않았습니다. 수정 후 [attempt2](airgap/attempt2/summary.json)와 최종 실행은 각각 14개 모두 통과했습니다.
- 짧은 `nginx:tag`를 registry hostname으로 오인하는 정규화와 GHCR의 HEAD 405 응답 문제를 수정했습니다. Registry probe는 GET을 사용합니다.
- Builder의 고정 containerd namespace에 남은 이전 이미지가 system 이미지로 기록되는 문제를 없애기 위해 호출마다 분리된 namespace를 사용했습니다. 같은 digest의 여러 tag가 archive를 덮어쓰지 않게 파일 이름도 분리했습니다.
- Cilium values(설치 설정) JSON을 Helm이 인식하는 중첩 형태로 만들고 실제 digest·Never 설정을 검사했습니다.
- Cilium이 이미 깨진 전용 시험 노드의 전체 cleanup이 실패한 기록을 보존했습니다. 관리 표식·명시적 전체 삭제 플래그를 요구한 상태에서 공식 K3s uninstall까지 진행하도록 보완했습니다. 관리하지 않는 기존 클러스터를 삭제하지 않습니다.
- Containerd timeout도 JSON `IMAGE_PRELOAD_TIMEOUT` 오류로 바꾸고 단위 검사했습니다. 중간 network fallback은 일반 readiness 오류에 반응하지 않게 제한했습니다.

## 10. 미검증 항목·남은 위험

| 항목 | 상태와 필요한 후속 작업 |
| --- | --- |
| 실제 AWS/GCP/OpenStack IAM·Security Group·VPC/Floating/External IP·metadata | **requires real cloud smoke test**입니다. 현재 추가 airgap E2E는 Linux VM 한 대에서만 수행했습니다. |
| amd64, Ubuntu 22.04, Debian, SELinux 계열 | 실제 설치 미검증입니다. OS 패키지·SELinux 정책은 bundle에 포함하지 않습니다. |
| Authenticated WireGuard·QUIC·Cloudflare token/domain/auth 및 공개 URL 성공 경로 | 실제 검증하지 않았습니다. Hook과 degraded 경로 구현을 실제 Tunnel 설치 지원으로 표현하지 않습니다. |
| Private registry credentials·image authorization·mirror | 미검증이며 자격정보·신뢰한 CA·mirror 계약이 필요합니다. |
| Online 선택 후 실제 다운로드 도중 인터넷 단절 | 단위 failure injection(오류 주입)만 검사했습니다. Helm 중간 실패가 남으면 자동 재시도만으로 복구되지 않을 수 있습니다. |
| Bundle 서명·보안 패치 적합성·공급망 승인 | SHA256 무결성은 확인했으나 서명 체계·운영 보안 승인은 없습니다. |
| Runtime K3s/Cilium 버전 upgrade·전원 차단·장기 운영·이미지 GC(자동 정리) | 이 작업의 14개 시나리오에는 없습니다. Workload tag update와 전체 제거 후 offline 재설치는 실제 검사했습니다. |
| 앱 외부 DB/API 의존성 | 이미지가 있어도 앱 외부 의존성까지 offline으로 바뀌지는 않습니다. |

`Internet-independent`는 내부망/로컬 bundle로 외부 인터넷 없이 배포한다는 뜻입니다. 중앙과 대상 사이의 통신까지 전혀 없는 원격 배포인 `Network-independent`는 주장하지 않습니다. 기본 route·로컬 통신·NIC와 최초 전달 수단이 필요합니다.

## 11. 기존 Runtime과 호환성

`0.1` 입력은 기존 필드만 반환하며 `states`의 허용 목록·순서를 유지합니다. `0.2` 입력 또는 명시적 CLI 새 옵션에서 새 필드를 반환합니다. 새 phase(진행 단계)는 별도 `network_states`에 기록합니다. `error.stage`를 소비하는 도구는 새 문자열도 표시해야 합니다.

구조 호환성과 동작 동일성은 다릅니다. 기본 auto의 사전 검사는 이전보다 엄격하며 승인 profile 밖의 runtime 버전은 거부합니다. `verify`와 `cleanup`은 외부 설치 경로를 검사하지 않습니다. Bundle로 배포하면 실제 이미지를 digest로 고정하므로 verify도 동일한 bundle 입력을 사용합니다. Bundle은 전체 cleanup 대상 K3s 디렉터리 밖에 두어야 합니다.

## 12. 팀과 합의할 contract(연결 규약)

1. 상위 Provider/Ansible이 대상 Linux·권한·기본 route·내부 통신·최초 runtime/bundle 전달을 준비하고 대상 안에서 실행할 방법입니다.
2. JSON 0.2 채택 시점, 알 수 없는 error.stage 처리, stdout/exit code/stderr 수집 및 별도 클라이언트의 공개 endpoint 검사 책임입니다.
3. Bundle 배포 경로·CPU 구조·보존 기간·신뢰한 manifest digest 전달/서명·용량과 lifecycle(생성→검사→전달→승격→폐기) 책임입니다.
4. 승인된 runtime/image profile 변경과 compatibility suite·버전 승격 담당입니다. 최신 탐지는 설치 승인과 다릅니다.
5. Registry credentials·CA·mirror·Cloudflare token/domain/auth·WireGuard peer/key와 노출 방식 계약입니다.
6. 첫 Touch(최초 Onboarding)는 수동/반자동으로 준비해도 됩니다. 이후 Touch에서는 저장한 bundle 경로와 요청 JSON만 넘기고 내부 auto 선택을 사용합니다.

공식 근거: [K3s Air-Gap Install](https://docs.k3s.io/installation/airgap)의 로컬 binary/system archive/skip-download 방식과 [Cilium K3s 설치](https://docs.cilium.io/en/stable/installation/k3s/)의 Flannel·network policy 비활성화 및 K3s CIDR 구성을 사용했습니다.
