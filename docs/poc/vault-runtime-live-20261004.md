# Vault runtime 기반 Ubuntu 실기 검증 — 2026-10-04

이 기록은 승인된 단일 시험 가상머신에서 K3s, Cilium, Kubernetes Secret 저장 암호화와 secrets 설정 누락 차단을 확인한 결과입니다. Vault, External Secrets Operator, Provider 영구 볼륨, 외부 잠금 해제 및 복구자료 보관 서비스는 준비되지 않아 실행하지 않았습니다. 부분 설치 상태를 runtime 준비 완료로 표시하지 않습니다.

## 대상과 접근 경계

| 항목 | 값 |
|---|---|
| OpenStack 프로젝트 | `nate2402` |
| 서버 이름 / ID | `railshot-vault-test-20261004` / `205a0517-45cf-4b8c-a5c4-8d18e5680345` |
| 이미지 / 자원 | Ubuntu 24.04, amd64, 2 vCPU, 4 GB RAM, 40 GiB |
| 이미지 ID | `608319cc-d48c-4c85-902f-601f5047e601` |
| 사설 / 접속 주소 | `172.28.102.150` / `10.26.4.245` |
| 네트워크 | `railshot-pgtest-1002-net` |
| 포트 / 루트 디스크 ID | `51f8d846-1865-4afd-be85-08935b07e826` / `4ef0dd31-9c94-45c1-96bf-d817b65b9d44` |
| 시험 전용 보안그룹 | `ddfc5c62-14af-44e3-b6a6-35ef3905943f` |
| SSH 허용 | TCP 22, 실제 관측 출발지 `210.107.196.187/32` |
| ED25519 지문 | `SHA256:r4S+R1UId9xr1zdUK3WsjvK4CLxPvuhxOSLQw68mDO4` |

로컬 VPN 주소 `10.113.0.5/32`를 SSH 출발지로 허용했을 때 TCP 연결은 시간 초과됐습니다. 이전 실증 기록과 같이 VM의 `SSH_CONNECTION`에서 실제 출발지 `210.107.196.187`을 확인했고, 해당 주소의 `/32` 규칙으로 제한한 뒤 연결했습니다. `0.0.0.0/0`에서 접속을 허용하는 인바운드 규칙은 추가하지 않았고 TCP 6443, 8200 등 다른 외부 포트도 열지 않았습니다. 최종 보안그룹은 이 TCP 22 인바운드 한 개와 기본 IPv4·IPv6 이그레스 두 개입니다.

콘솔에서 독립 확인한 지문과 `ssh-keyscan -t ed25519` 결과가 일치한 뒤 `/private/tmp/railshot-vault-test-20261004/known_hosts`에 사설 IP `172.28.102.150`의 `HostKeyAlias`로 고정했습니다. 개인키는 기존 `/Users/waffle/.ssh/id_aolda`를 참조했으며 키 내용은 출력하거나 보고서에 기록하지 않았습니다. SSH는 별도 설정 파일, 에이전트, 프록시, 전달 기능을 모두 끄고 `StrictHostKeyChecking=yes`로 실행했습니다.

## 설치 전 관측과 guest 검사

- 호스트 이름, Ubuntu `24.04`, 커널 `6.8.0-142-generic`, amd64, 2 vCPU, 약 4 GB RAM이 요청과 일치했습니다.
- 루트 파일시스템 가용 공간은 약 37.8 GiB, swap은 0이었고 passwordless sudo가 가능했습니다.
- cloud-init은 약 462초에 `done`, SSH 서비스는 `active`였습니다.
- 기존 `/usr/local/bin/k3s`, `/etc/rancher/k3s/config.yaml`, Railshot Cilium 자산은 없었습니다.
- `systemd-machine-id-commit.service`는 최초 부팅 때 30초 제한을 넘겨 `Result=timeout`, `ExecMainStatus=15`로 실패했습니다. `/etc/machine-id`는 33바이트의 읽기 전용 tmpfs 마운트였고 다른 실패 unit은 없었습니다. 이 시험에서는 값을 재생성하거나 서비스를 재실행하지 않았습니다.
- `railshot-vault-test-20261004-guest-01` 실제 실행은 종료 코드 0, `status=succeeded`, `guest_ready=true`와 일치하는 영수증을 반환했습니다.

실행 결과는 다음 비공개 임시 파일에 남겼습니다.

- `/private/tmp/railshot-vault-test-20261004/guest-result.json`
- `/private/tmp/railshot-vault-test-20261004/runtime-result.json`
- `/private/tmp/railshot-vault-test-20261004/secrets-missing-result.json`
- `/private/tmp/railshot-vault-test-20261004/secret-encryption-canary-result.txt`
- `/private/tmp/railshot-vault-test-20261004/post-runtime-state.txt`
- `/private/tmp/railshot-vault-test-20261004/final-readonly-state.txt`

핵심 실행은 저장소 루트에서 다음 형태로 수행했습니다. 요청 JSON과 SSH 참조는 모두 0600 파일이었고 출력은 준비 상태 메타데이터만 기록했습니다.

```bash
PATH=/private/tmp/railshot-infra-venv/bin:$PATH \
  /private/tmp/railshot-infra-venv/bin/python infrastructure/ansible/run.py \
  --request /private/tmp/railshot-vault-test-20261004/guest-request.json \
  --state-dir /private/tmp/railshot-vault-test-20261004/state

PATH=/private/tmp/railshot-infra-venv/bin:$PATH \
  /private/tmp/railshot-infra-venv/bin/python infrastructure/ansible/run.py \
  --request /private/tmp/railshot-vault-test-20261004/runtime-request.json \
  --state-dir /private/tmp/railshot-vault-test-20261004/state

PATH=/private/tmp/railshot-infra-venv/bin:$PATH \
  /private/tmp/railshot-infra-venv/bin/python infrastructure/ansible/run.py \
  --request /private/tmp/railshot-vault-test-20261004/secrets-missing-request.json \
  --state-dir /private/tmp/railshot-vault-test-20261004/state
```

원격 읽기 검사는 위에서 고정한 key와 known_hosts만 사용해 `sudo -n k3s kubectl get nodes,pods -A`, `sudo -n k3s secrets-encrypt status`, `vmstat`, `journalctl -u k3s`를 실행했습니다. canary 검사는 `/private/tmp/railshot-vault-test-20261004/secret-encryption-canary.sh`를 표준입력으로 전달했으며 시험값을 출력하지 않도록 비교 결과만 기록했습니다.

## runtime 설치 결과

`railshot-vault-test-20261004-runtime-01`은 2026-10-03 16:11:56 UTC에 시작해 16:26:50 UTC에 종료했습니다. 고정 버전 K3s `v1.34.11+k3s1`, Cilium `1.20.2`와 `secrets-encryption: true` 설정을 사용했습니다.

최종 실행기 결과는 다음과 같습니다.

| 항목 | 결과 |
|---|---|
| guest 단계 | 종료 코드 0, 영수증 일치, `guest_ready=true` |
| runtime 단계 | 종료 코드 2 |
| 전체 상태 | `failed` |
| 오류 | `RUNTIME_INSTALL_FAILED` |
| 결과 불명확 표시 | `outcome_unknown=true`, `retryable=false` |
| 준비 상태 | `runtime_ready=false`, `secrets_ready=false` |

K3s 서비스와 Kubernetes API는 기동됐고 노드 버전과 내부 주소는 요청에 일치했습니다. Cilium Helm chart도 `1.20.2`로 적용됐지만 내부 600초 준비 제한 동안 Cilium agent, operator, Envoy와 CoreDNS가 준비되지 않아 Node는 `NotReady`로 남았습니다. 따라서 설치 성공 영수증은 발급되지 않았습니다.

실행 중 다음 현상을 관측했습니다.

- `vmstat`의 CPU I/O wait가 반복해서 약 44~48%였고 blocked process가 1~3개 있었습니다.
- K3s 로그에서 SQLite/kine 쓰기가 수초에서 최대 약 25초 걸린 `Slow SQL`이 반복됐습니다.
- containerd 초기화와 Pod sandbox 생성이 지연됐고 일부 sandbox 요청은 `DeadlineExceeded` 또는 예약 이름 충돌로 재시도됐습니다.
- Cilium 이미지 세 개의 pull이 시작됐지만 준비 제한 종료 시점에도 컨테이너가 `ContainerCreating` 또는 `Init:0/6`이었습니다.
- 커널에서 블록 장치 오류는 관측되지 않았습니다.

높은 I/O 대기와 이미지 pull 지연을 확인했지만 하나의 근본 원인으로 확정하지 않습니다. 기존 또는 공용 인프라의 볼륨 정책, flavor, 네트워크는 변경하지 않았고 실패한 요청을 새 요청 ID로 중복 실행하지 않았습니다.

종료 약 2분 30초 뒤인 16:29:20 UTC에 엄격한 호스트키 검증으로 다시 접속했습니다. K3s는 `active`였고 Secret 암호화 상태도 유지됐지만, 노드는 여전히 `NotReady`, 세 Cilium workload는 `ContainerCreating` 또는 `Init:0/6`, CoreDNS는 `Pending`이었습니다. 이미지 수는 3개로 유지됐고 짧은 `vmstat` 관측에서 I/O wait가 48%, 57%, 96%였습니다. 자연 수렴을 성공으로 관측하지 못했으며 재부팅이나 중복 설치는 수행하지 않았습니다.

## Kubernetes Secret 저장 암호화

Kubernetes API가 준비된 시점에 `railshot-encryption-canary-20261004` 임시 namespace와 실제 비밀이 아닌 고정된 시험용 Secret을 만들었습니다. 값 자체는 로그나 문서에 기록하지 않았습니다.

1. API로 생성한 값을 다시 읽어 입력과 일치함을 확인했습니다.
2. `k3s secrets-encrypt status`에서 `Encryption Status: Enabled`, `Current Rotation Stage: start`, 서버 해시 일치와 활성 `AES-CBC` 키를 확인했습니다.
3. K3s SQLite의 해당 Secret 최신 row에서 시험용 평문이 없고 `k8s:enc:aescbc:v1:` 표식이 있음을 확인했습니다.
4. 임시 namespace를 삭제하고 삭제 완료를 확인했습니다.

이 검사는 K3s Secret 저장 암호화가 실제 API 쓰기와 SQLite 저장에 적용됐다는 근거입니다. Cilium과 Node 준비, 재부팅 후 복구, Vault 저장 암호화나 Vault 잠금 해제의 근거로 확대하지 않습니다.

## secrets 설정 누락 차단

외부 seal, escrow, TLS 및 영구 저장 프로필이 없는 상태에서 `railshot-vault-test-20261004-secrets-missing-01`의 실제 `secrets.configure` 실행을 호출했습니다. 실행은 종료 코드 3, `status=blocked`, `SECRETS_CONFIGURATION_REQUIRED`, `steps=[]`로 SSH나 Vault/Kubernetes 변이 전에 차단됐습니다. `secrets_ready=false`를 유지했으며 누락된 외부 서비스를 성공으로 모의하지 않았습니다.

## 수행하지 않은 검증

- Vault persistent volume bind와 데이터 보존
- Provider KMS 또는 외부 Transit을 이용한 자동 잠금 해제
- mTLS escrow 서버의 복구자료 내구 저장과 인수
- Vault 초기화, root 토큰 폐기, 감사 로그, Kubernetes auth와 앱별 정책
- External Secrets Operator 설치, webhook 준비와 실제 Secret 동기화
- Vault 및 K3s 재시작 후 복구
- Node Ready, Cilium/CoreDNS 최종 준비와 앱 HTTP readback

Vault 전체 및 Provider별 준비 완료 판정에는 위 운영 입력과 별도 승인된 재검증이 필요합니다. 이번 결과는 Ubuntu 단일 VM에서 guest 계약, K3s API, 실제 Secret 저장 암호화, 누락 설정 차단까지의 부분 검증입니다.
