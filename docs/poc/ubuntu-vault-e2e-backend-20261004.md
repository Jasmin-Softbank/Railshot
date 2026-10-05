# 임시 Ubuntu Vault 통합 시험 — 대상 runtime 준비 기록

이 문서는 대상 Kubernetes/Vault, 비밀값 주입과 전달 자격 교체 시험 담당자의 기록입니다. 신규 임시 VM의 K3s/Cilium·Secret 암호화 저장, 마지막 독립 환경의 Vault/ESO 구성, 실제 샘플 앱 값 주입·변경·삭제, 자격 교체와 재시작 후 기존 값 조회가 통과했습니다. 첫 두 환경은 초기화 후 복구자료 보관에 실패해 `unknown`으로 시험 기록을 보존했습니다. 마지막 성공은 앞선 실패를 지우거나 제품 API→GitOps 전체 배포 성공을 뜻하지 않습니다. 임시 VM·루트볼륨·포트·Floating IP는 최종 검수 뒤 삭제·반납을 확인했으며, 정확한 자원 근거는 [자원 원장과 시험 상태](ubuntu-vault-e2e-resources-20261004.md)를 따릅니다.

## 승인 범위와 역할

실제 작업은 사용자가 승인한 이번 임시 Ubuntu VM 한 대와 연결 주소로 한정했습니다. SSH 호스트 키는 콘솔에서 독립 확인한 지문에 고정했고 기존 개인키를 VM에 복사하지 않았습니다. 실제 운영 비밀은 사용하지 않고 모든 입력은 시험용이었습니다. 최종 검수 뒤 VM과 이번 실행이 만든 자원의 삭제·반납을 확인했으며, 근거는 [자원 원장과 시험 상태](ubuntu-vault-e2e-resources-20261004.md)를 따릅니다.

중앙 서비스 담당자가 공통 패키지·Docker·중앙 Vault·복구자료 보관 서비스를 설치하고, 이 시험은 K3s/Cilium·정적 저장소·대상 Vault·External Secrets Operator(외부 비밀값 동기화 구성요소)를 맡습니다. 중앙 포트는 VM 사설 주소의 18200/19443이며 외부 연결 주소에 추가 포트를 열지 않습니다. VM 재부팅과 중앙 재시작은 시점을 합의한 뒤 수행합니다.

## 고정 시험 자료

- 기존 저장소 runtime 정책: K3s `v1.34.11+k3s1`, Cilium `1.20.2`, CLI `v0.20.1`; 이미지 digest는 `deployment/airgap/versions.json`을 사용합니다.
- Vault: `hashicorp/vault:2.1.1@sha256:47f14a6acb98f48d798a07df7c83f23a6e636e1cf724c5f8ff165cb32667a1e2`.
- ESO: `ghcr.io/external-secrets/external-secrets:v2.11.0@sha256:66fb710878cbf3eba4a927e35c5d75e29a202c9bab238aad64bfb23b786b962b`.
- 샘플 앱: `nginx:1.28.0-alpine@sha256:30f1c0d78e0ad60901648be663a710bdadf19e4c10ac6782c235200619158284`.
- ESO 공식 chart `2.11.0` SHA256: `8199b42fe80b871c6a86233a80bb14f599fd6e1e6462c1216d836577f845e161`.
- Helm `v3.19.0` 공식 체크섬을 확인한 후 chart를 렌더링했고, manifest에 namespace와 이미지 digest를 고정했습니다. 최종 manifest SHA256: `19080f50b3a7fc1e390f32405589a7d5d95cad84740fdf7bbc67bb2e98ca33e8`.

이 버전 선택은 이번 시험 입력에 한정하며 제품 기본 정책을 변경하지 않습니다. 이미지 digest는 공개 registry manifest를 읽어 확인했습니다. 다운로드 출처는 [공식 chart 색인](https://charts.external-secrets.io/index.yaml), [공식 ESO chart 릴리스](https://github.com/external-secrets/external-secrets/releases/tag/helm-chart-2.11.0), [Helm 공식 배포](https://get.helm.sh/)입니다.

로컬 자료 위치: `/private/tmp/railshot-ubuntu-vault-e2e-20261004/backend/`. `images.json`, `external-secrets.yaml`, upstream chart/manifest digest와 fixture 생성기를 보관합니다. 비밀 자격은 보고서에 기록하지 않습니다.

## 재현 입력과 예정 실행

`deployment/scripts/tests/prepare_ubuntu_vault_e2e.py`는 관측된 VM metadata, 고정 이미지 목록, ESO manifest를 받아 비공개 시험 파일을 생성합니다. 이 도구 자체는 네트워크나 Kubernetes를 호출하지 않습니다.

```bash
python3 deployment/scripts/tests/prepare_ubuntu_vault_e2e.py \
  --metadata /PRIVATE/vm-metadata.json \
  --output /PRIVATE/test-inputs \
  --eso /PRIVATE/external-secrets.yaml \
  --images /PRIVATE/images.json
```

metadata에는 `environment_id`, `resource_id`, `project_id`, `private_ip`, `hostname`, `network`, `ssh`가 필요합니다. `ssh`는 기존 Ansible 계약의 사용자·포트·개인키 파일·고정 known_hosts 파일을 지정합니다. OpenStack 서버 JSON은 실제 콘솔에서 관측한 식별자와 주소를 등록 형식으로 변환한 것이며, 별도 cloud API 소유권 조회 결과로 주장하지 않습니다.

예정 순서는 다음과 같습니다.

1. 콘솔 지문 고정 → guest 검사 → 기존 `infrastructure/ansible/run.py`의 `runtime.install` → K3s/Cilium/노드 준비·Secret 저장 암호화 관측.
2. 중앙 담당자가 제한된 `railshot-api` 계정으로 고정 sudo helper의 허용/거부를 검증합니다. 이후 대상 Ansible 실행을 위한 시험 SSH 자격을 추가하면 이 계정의 실질적 대상 관리 권한이 커지므로, helper 단독 권한 검증과 분리해 기록합니다.
3. VM의 `/var/lib/railshot-test-vault`에 시험용 local PV를 준비합니다. `kubernetes.io/no-provisioner`, `Retain`, `WaitForFirstConsumer`, 정확한 hostname nodeAffinity를 사용하며 기본 K3s local-storage provisioner를 켜지 않습니다. 단일 VM 디스크 검증이며 cloud CSI 내구성 검증이 아닙니다.
4. 실제 `secrets.configure` → 중앙 helper 발급 → Ansible private staging → Vault 초기화/복구자료 보관/동기화/재시작/root 폐기 → `secrets.verify`를 실행합니다.
5. 샘플 namespace·Deployment·Service를 만든 뒤 실제 `secrets_delivery.py`의 `prepare`, `apply`, `verify`를 실행합니다. 합성 일반값과 비밀값을 Pod에서 비교하되 값은 출력하지 않습니다. 기존 동명 명시 env가 제거돼 새 revision 값이 적용되는지 확인합니다. 이 경로는 내부 전달 실행기 시험이며 제품 UI 또는 GitOps 전체 배포 성공을 대신하지 않습니다.
6. 전달 AppRole 교체, 새 자격 로그인, 이전 자격 명시 폐기, 이전 자격 거부와 교체 후 재전달을 확인합니다. 대상 Vault 재시작 후 자동 잠금 해제·보존값 재관측은 중앙 담당자와 조율해 수행합니다.
7. 상태·영수증·안전한 검사 결과를 수집해 최종 검수에 전달합니다. 임시 자원 삭제는 부모 검수 이후 진행합니다.

## 실제 대상과 초기 실행

- 이름/ID: `railshot-vault-e2e-20261004-01` / `36e3f96e-ee0c-45e7-bb0b-6694ab192cec`.
- Ubuntu 24.04.5, 4 vCPU/8 GiB, 80 GiB; 프로젝트 `nate2402`.
- 사설/접속 주소: `172.28.102.8` / `10.26.3.90`. 이전 시험 VM에는 접근하지 않았습니다.
- 콘솔에서 확인한 ED25519 지문: `SHA256:iy8OZMRppzJBlmC2fcTmLJL3ANKN+8R+DD1EwFTUE1U`. 전용 known_hosts에 동일 공개키를 고정하고 개인키는 로컬 파일 참조만 사용했습니다.
- `guest.check`: native 결과 `succeeded`, `guest_ready=true`, 일치하는 요청/대상/nonce 영수증을 받았습니다. 증거: 로컬 시험 디렉터리의 `guest-result.json`.
- `runtime.install`: 00:36:53–00:44:40 UTC, guest/runtime 두 단계 exit 0, `runtime_ready=true`. 00:45:26 UTC 관측에서 Node Ready, Cilium agent/operator/Envoy 및 CoreDNS 모두 1/1 Running·재시작 0이었습니다.
- K3s Secret 저장 암호화: 합성 canary API readback 일치, SQLite 최신 row 존재, 시험 평문 부재와 `k8s:enc:aescbc:v1:` 표식 존재를 직접 확인했습니다. canary namespace는 삭제했습니다. 증거: `secret-storage-encryption.txt`.
- 중앙 담당자의 helper 제한 검증 완료 통지 후 VM에서 별도 시험 SSH 키를 생성했습니다. 이 키를 가진 `railshot-api`는 대상 Ubuntu 관리 계정으로 연결할 수 있어, 이 시점 이후는 helper-only 권한 모델이 아닙니다.
- `/opt/railshot-src`에는 중앙용 소스만 있어 첫 native 사전검사는 `infrastructure/ansible/run.py` 미존재로 시작하지 못했습니다. 대상 Vault 변경은 없었습니다. 누락 코드 전송과 뒤이은 읽기 접속이 각각 SSH 15초 시간 초과로 실패하여 네트워크 설정 변경 없이 중단했습니다.

## 첫 대상 환경의 실제 실패와 안전 차단

접속 복구 후 실제 `run.py secrets.configure`를 실행했습니다. ESO 본체·cert-controller·webhook은 모두 1/1 Running이 됐지만, Vault 이미지의 기본 entrypoint가 `-config=/vault/config`를 자동 추가하여 명시한 파일과 동일 seal 설정을 두 번 읽었습니다. 고정 이미지의 entrypoint 내용과 `duplicate seal` 시작 오류를 확인했습니다. 제품 manifest에 `command: [/bin/vault]`를 명시하여 중복을 제거했고, UID 100·TLS·읽기 전용 루트 파일시스템·`disable_mlock`은 유지했습니다.

이미 준비 실패한 StatefulSet Pod는 template 수정만으로 교체되지 않았습니다. 첫 수동 Pod 교체 때 native 실행이 아직 끝나지 않아 실패 복원이 이전 template를 다시 적용하는 경합이 있었습니다. 이를 잘못 판단해 작업한 점을 인정하며, 이후 기존 실행 종료를 확인한 다음 정상 Pod 삭제와 재개를 직렬로 수행했습니다. 제품은 소유권·Ready·현재/목표 revision·관측 generation이 모두 맞는 이전 workload만 복원하도록 수정했습니다. 준비 실패한 이전 template로 돌아가지 않도록 검사합니다.

새 Pod 기동 후 실제 Vault 상태는 `initialized=true`, `sealed=false`였습니다. 초기화 전이라는 앞선 판단을 철회했습니다. 그러나 환경에 바인딩된 custody 조회와 중앙 DB의 읽기 전용 metadata 조회 모두 초기화 영수증이 없었습니다. 최초 응답이 어디서 유실됐는지 확정할 증거는 없으며 시간 초과를 원인으로 단정하지 않습니다. root token·복구키를 로그에서 추출하거나 같은 저장소를 재초기화하지 않았습니다.

또한 실패 복원 명령의 오류가 최초 오류를 덮어쓰는 결함을 수정했습니다. `unknown`이면 추가 복원 변이를 생략하며, 알려진 실패의 복원이 실패할 때도 최초 오류 code와 불확실성을 보존합니다. 민감 명령의 손상된 JSON 응답도 `unknown`입니다. 초기화의 제한 시간은 서버 요청 420초, CLI 480초, 외부 프로세스 510초이며 `VAULT_MAX_RETRIES=0`으로 자동 초기화 재시도를 금지합니다. 관련 단위 회귀 12개가 통과했습니다.

동일 원장의 실제 `--resume-secrets` 결과는 exit 4, `status=unknown`, `VAULT_INITIALIZATION_UNKNOWN`, `outcome_unknown=true`, `secrets_ready=false`였습니다. 증거: `original-environment-unknown-result.json`. 원래 `secrets-resume-result.json`의 `blocked/SECRETS_COMMAND_FAILED` 결과도 그대로 남겼으며, 이를 초기화가 없었다는 증거로 사용하지 않습니다.

## 별도 빈 저장소로 이어가는 시험

추가 VM을 만들지 않고 승인된 임시 VM 안에서 `railshot-vault-e2e-20261004-02` 환경을 발급합니다. 고정 namespace를 사용하는 제품 제약 때문에 첫 시험 namespace를 정상 중지·교체하지만, 첫 환경의 Retain PV `railshot-test-vault`, `/var/lib/railshot-test-vault`의 암호화 데이터, 중앙 자격과 native 원장은 보존합니다. 새 환경은 별도 Transit 키·인증서와 `railshot-test-vault-02` / `/var/lib/railshot-test-vault-02` 빈 저장소를 사용합니다. 기존 불명 초기화를 우회하여 재시도한 것이 아니라 독립된 새 환경 시험입니다.

두 번째 native 구성은 02:24:26–02:26:35 UTC에 실행됐고 `SECRETS_COMMAND_UNKNOWN`으로 끝났습니다. 서버의 고정 단계명과 시간만 수집한 기록에서 barrier 초기화는 02:26:16, root 생성은 02:26:29였으므로 이번 실패를 초기화 시간 초과로 설명하지 않습니다. 중앙 감사 기록의 `store/422`와 영수증 부재를 확인했습니다. [Vault 2.1.1 공식 CLI 소스](https://github.com/hashicorp/vault/blob/v2.1.1/command/operator_init.go#L524-L528)는 자동 잠금 해제 방식에서 빈 unseal 배열과 함께 shares/threshold `1/1`을 출력하지만, 보관 서비스는 `0/0`만 허용하고 있었습니다. 이전 Docker 시험은 HTTP 응답을 CLI 모양으로 직접 변환하여 이 차이를 놓쳤습니다. 보관 서비스 담당자가 정확한 CLI 형태에 대한 검증·회귀와 실제 CLI 기반 시험을 보완합니다.

남은 명령 계약 점검에서는 `kubectl delete`의 정상 문장을 JSON으로 파싱하는 결함도 수정했습니다. 삭제의 정상 종료 후 출력 파싱을 생략하고 후속 조회로 검증합니다. 제품 Runner로 실제 시험용 ConfigMap을 생성·삭제·부재 조회한 결과가 `kubectl-delete-adapter-probe.txt`에 있으며, 모의 단위시험과 구분합니다. Vault Pod 삭제 후 자동 잠금 해제와 값 재동기화는 최종 독립 환경에서 별도로 확인합니다.

첫 두 환경의 불명 결과와 저장소를 보존하며, 계약 수정 확인 후 `railshot-vault-e2e-20261004-03` 및 별도 빈 저장소로 마지막 기능 시험을 진행합니다. 원인 확인 없이 환경을 반복 생성하는 방식으로 시험하지 않습니다.

## 마지막 환경의 실제 구성 결과

수정된 보관 서비스 이미지와 실제 CLI 원본 JSON 보관 시험 통과를 확인한 후 세 번째 환경을 시작했습니다. native `secrets.configure`는 02:37:10–02:41:42 UTC(4분 32초)에 완료됐고, 구성·별도 검증 두 단계 모두 exit 0, `status=succeeded`, `secrets_ready=true`였습니다. 서버의 안전한 단계 관측은 02:38:57 barrier 초기화, 02:39:07 root 생성이며 구성 단계에는 중앙 자료 보관·ESO 동기화·Vault Pod 교체 후 자동 잠금 해제·동일 값 재동기화·초기 root token 폐기가 포함됩니다.

- native 영수증: `environment03-configure-result.json` 및 `environment03-configure-time.txt`.
- 초기화 보관 영수증: `c54846a0-5fb7-4151-8541-0e5337cfaf18`.
- 최초 전달 자격 보관 영수증: `50fd7afa-008c-48fb-b524-0b4787383bd1`.
- 실제 ConfigMap은 환경 `railshot-vault-e2e-20261004-03`, `phase=ready`, `readiness_verified=true`와 위 두 영수증을 가리켰습니다. `environment03-readiness-observed.txt`에 기록했습니다.
- 이전 PV 두 개는 Released/Retain, 새 PV `railshot-test-vault-03`는 Bound였습니다. 첫 두 저장소를 초기화하거나 재사용하지 않았습니다.

샘플 전달 실행의 첫 두 시도는 옮긴 공개 소스에서 `ci/scripts/gate`와 `runner` 하위 디렉터리가 빠져 import 단계에서 종료됐습니다. 전달 변이 전 실패였으며 전체 공개 CI 소스를 보완한 후 실제 import 준비를 확인하고 실행했습니다. 시험 패키징 실수와 제품 실행 결과를 구분합니다.

## 실제 앱 전달·교체·재시작 결과

`live_ubuntu_delivery.py`는 02:47:04–02:51:24 UTC에 실제 `secrets_delivery.py`와 등록된 SSH 전송을 호출했고 exit 0으로 끝났습니다. 앱은 고정 nginx 이미지 한 개이며 서비스 연결 확인은 Kubernetes 내부 서비스 경로로 수행했습니다. 외부 HTTP 포트는 열지 않았습니다.

| 확인한 동작 | 실제 관측 |
| --- | --- |
| 최초 전달 | `prepare → apply → verify`, ESO Secret 실제 동기화와 새 Pod의 일반값·비밀값 비교 통과. 기존 동명 명시 env보다 새 버전의 값이 적용됨 |
| 불변 버전 | 동일 값 재전달 허용, 같은 revision에 다른 비밀값 쓰기는 `REVISION_CONFLICT`로 거부 |
| 전달 자격 교체 | 새 AppRole 로그인·custody 보관·관리 호스트 파일 전환 후 기존 자격을 명시 폐기하고 기존 로그인 거부 확인 |
| 값 변경 | 교체한 자격으로 revision 02의 일반값·비밀값을 실제 전달하고 새 Pod 값 비교 통과 |
| 값 삭제 | revision 03의 새 Pod에서 비밀 변수 자체가 존재하지 않음을 확인. 이전 불변 버전의 Vault/Kubernetes 자료는 보존함 |
| 재시작과 보존 | 대상 Vault Pod를 정상 교체한 뒤 기존 revision 02를 인증 조회하여 동일 비밀값 확인. 이 구간에는 put 또는 재전달이 없었으며 삭제된 변수의 Pod 부재도 유지됨 |

드라이버의 `checks: 7`에는 역사 버전 자료를 다음 관측에 남기는 준비 기록이 하나 포함됩니다. 이를 독립된 기능 7개의 증명으로 해석하지 않습니다. 값과 토큰은 비교에만 사용하고 출력하지 않았습니다.

마지막 02:52:00 UTC 읽기 관측에서 Vault는 `initialized=true`, `sealed=false`, 버전 2.1.1, raft 저장소였으며 대상 Vault·샘플 앱·Cilium/CoreDNS·ESO 세 구성요소가 모두 Ready/Running이었습니다. Vault 최종 Pod UID는 `fc4d746f-fc53-4bb2-a52f-dfc49bd783ee`이며 재시작 횟수 0은 **현재 새 Pod** 기준입니다. 두 이전 볼륨은 Retain/Released로 남아 있었습니다. 세 환경의 Transit 키·runtime 인증서·seal 토큰이 모두 별개임을 값 비공개 비교로 확인했습니다.

핵심 증거는 로컬 시험 디렉터리의 다음 파일입니다.

- `environment03-delivery-attempt3-result.txt`, `environment03-delivery-attempt3-time.txt`: 최종 실제 실행 결과와 시간.
- `environment03-final-review.json`: 최종 상태·변수명·준비 조건·보존 볼륨·드라이버 결과. 비밀값 없음.
- `environment-credential-separation.json`: 세 환경의 자격 분리 비교 결과만 기록.
- `environment02-configure-result.json`, `environment02-init-events.json`, `original-environment-unknown-result.json`: 실패 및 재초기화 거부 기록.

부모의 독립 읽기 검수는 이미 고정한 SSH 연결로 `sudo python3 /home/ubuntu/railshot-backend-assets/final-review.py`를 실행할 수 있습니다. 비밀값·root token·복구키를 출력하지 않습니다. 모든 대상 변이 실행은 종료했으며 자원 삭제는 이 문서 작성 시점에 수행하지 않았습니다.

## 변경과 회귀 검사

이번 실기 중 제품 수정은 `secrets_runtime.py`의 명시적 Vault 실행 명령, 준비된 이전 workload만 복원하는 조건, 최초 오류/unknown 보존, 유한 초기화 시간과 재시도 금지, 삭제 명령의 정상 텍스트 출력 처리입니다. 보관 서비스의 CLI `1/1` 계약 수정은 중앙 서비스 담당자의 소유 변경이며 별도 보고서에 검증을 기록합니다.

```bash
/private/tmp/railshot-infra-venv/bin/python -m unittest discover \
  -s deployment/scripts/tests -p 'test_secrets_*.py' -v
# 24개 통과: runtime 13개 + delivery 11개, 격리 단위시험

/private/tmp/railshot-infra-venv/bin/python -m unittest discover \
  -s infrastructure/ansible -p test_run.py -v
# 33개 통과. localhost 임시 포트 바인딩을 허용한 격리 실행이며 실제 cloud 호출 없음.

git diff --check
```

단위시험 로그는 `secrets-all-regression.txt`, `native-regression.txt`입니다. 이 시험 결과와 실제 VM의 명령 실행·readback 결과를 구분합니다.

## 현재 확인한 범위

로컬 fixture의 native 요청 계약, ESO CRD의 `external-secrets.io/v1` 제공, 세 ESO Deployment 이미지 digest 고정과 실제 준비 상태를 확인했습니다. 실제 guest/runtime·Kubernetes 준비와 SQLite Secret 암호화 저장, 마지막 환경의 구성 및 앱 전달은 위 범위에서 통과했습니다. 첫 두 대상의 초기화 후 보관 실패는 그대로 실패 기록입니다. 실제 전달 시험 드라이버는 `deployment/scripts/tests/live_ubuntu_delivery.py`입니다. 제품 API→GitOps 전체 배포, 운영 CSI, 멀티클라우드, VM 전체 재부팅, 24시간 이상의 만료·장기 부하, 외부 공개 HTTP 서비스는 이 시험 결과에 포함하지 않습니다.
