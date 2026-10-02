# Provider 모의 통합 검증 — 2026-10-02

## 결론과 검증 범위

**별도의 새 Linux VM 3개에서 동일한 Deployment Runtime의 전체 흐름을 실제 실행하여 모두 통과했습니다.** 노드별 8개 검사, 총 24개가 통과했으며 Mac에서의 HTTP 200·샘플 본문 확인도 3건 성공했습니다. 로컬 계약·엔진·하네스 검사 21개는 실패·SKIP 없이 통과했습니다.

이 결과는 **AWS·GCP·OpenStack이 Linux 노드를 준비한 이후의 공통 Runtime 검증**입니다. 실제 AWS/GCP/OpenStack API나 클라우드 자원은 사용하지 않았습니다. 테스트 대상은 서로 다른 디스크와 boot ID(부팅 고유 식별값)를 가진 독립 VM입니다. 같은 VM에 provider 이름만 바꿔 실행한 결과와 구분합니다.

```json
{
  "aws": {"status": "passed"},
  "gcp": {"status": "passed"},
  "openstack": {"status": "passed"}
}
```

요약 JSON은 [provider-status.json](provider-simulation/20261002080234/provider-status.json), 전체 결과는 [summary.json](provider-simulation/20261002080234/summary.json)입니다.

## 실행 환경

- 작업 브랜치(독립 작업 공간)는 `feature/deployment-runtime-seungmin`입니다. 변경 전 Runtime 기준 커밋은 `9ba804600518b8b26c79bc328aa77c09c9aa4e89`입니다.
- 실행 ID는 `20261002080234`이며 2026-10-02 08:02:34~08:10:54 KST에 실행했습니다. 원본 결과의 UTC 시각은 2026-10-01 23시입니다.
- Mac Apple Silicon / RAM 16 GiB / Lima 2.2.0 / 호스트 Python 3.14.7입니다.
- 각 VM은 Ubuntu 24.04.4 / Linux 6.8.0-134-generic / **aarch64(arm64)** / CPU 2개 / RAM 4 GiB / 디스크 20 GiB입니다.
- K3s는 `v1.34.11+k3s1`, Cilium은 `1.20.2`, 검사 이미지는 `curlimages/curl:8.12.1`입니다.
- 세 VM은 메모리 확보를 위해 순서대로 실행했습니다. 기존 데모 VM의 클러스터는 제거하지 않았습니다.
- 추가 NIC(네트워크 장치)는 dummy(소프트웨어 가상 장치)입니다. 실제로 서로 다른 IP를 할당하고 K3s Node InternalIP(노드 내부 주소) 및 출력 endpoint에 반영했습니다. 다운로드에는 Lima 기본 NIC를 사용했습니다.

| 모의 대상 | hostname(노드 이름) | 시험용 NIC / IP | 노드 내 검사 | VM 생성부터 정지까지 | Mac 외부 확인 |
| --- | --- | --- | --- | --- | --- |
| aws | `ip-10-250-10-11` | `awsnic0` / `10.250.10.11` | 8/8 PASS | 161.930초 | HTTP 200 / `30084` |
| gcp | `gce-railshot-01` | `gcpnic0` / `10.250.20.12` | 8/8 PASS | 171.871초 | HTTP 200 / `30085` |
| openstack | `openstack-railshot-01` | `osnic0` / `10.250.30.13` | 8/8 PASS | 166.310초 | HTTP 200 / `30086` |

**amd64는 미검증입니다.** 호스트의 네이티브 arm64 가상화를 사용했습니다. 실제 EC2·GCE의 NIC 드라이버, MTU(패킷 크기 제한), VPC(클라우드 가상 네트워크) 경로는 재현하지 않았습니다.

## 실제 수행한 검사

| 단계 | 확인 사항 | AWS-like | GCP-like | OpenStack-like |
| --- | --- | --- | --- | --- |
| `clean-install` | 새 노드 설치: K3s → Cilium → nginx → Service | [PASS](provider-simulation/20261002080234/aws/guest/clean-install.json) | [PASS](provider-simulation/20261002080234/gcp/guest/clean-install.json) | [PASS](provider-simulation/20261002080234/openstack/guest/clean-install.json) |
| `bootstrap-repeat` | 동일 입력 재실행: Deployment·Pod UID 유지 | [PASS](provider-simulation/20261002080234/aws/guest/bootstrap-repeat.json) | [PASS](provider-simulation/20261002080234/gcp/guest/bootstrap-repeat.json) | [PASS](provider-simulation/20261002080234/openstack/guest/bootstrap-repeat.json) |
| `health-endpoint` | Cilium·Node·DNS·Service·endpoint 확인 | [PASS](provider-simulation/20261002080234/aws/guest/health-endpoint.json) | [PASS](provider-simulation/20261002080234/gcp/guest/health-endpoint.json) | [PASS](provider-simulation/20261002080234/openstack/guest/health-endpoint.json) |
| `workload-update` | nginx 1.28.0-alpine → 1.28.1-alpine 업데이트 | [PASS](provider-simulation/20261002080234/aws/guest/workload-update.json) | [PASS](provider-simulation/20261002080234/gcp/guest/workload-update.json) | [PASS](provider-simulation/20261002080234/openstack/guest/workload-update.json) |
| `update-health` | 업데이트한 이미지와 health 확인 | [PASS](provider-simulation/20261002080234/aws/guest/update-health.json) | [PASS](provider-simulation/20261002080234/gcp/guest/update-health.json) | [PASS](provider-simulation/20261002080234/openstack/guest/update-health.json) |
| `workload-cleanup` | 앱 제거 및 Deployment·Service 부재 확인 | [PASS](provider-simulation/20261002080234/aws/guest/workload-cleanup.json) | [PASS](provider-simulation/20261002080234/gcp/guest/workload-cleanup.json) | [PASS](provider-simulation/20261002080234/openstack/guest/workload-cleanup.json) |
| `workload-redeploy` | 원래 입력으로 앱 재배포 | [PASS](provider-simulation/20261002080234/aws/guest/workload-redeploy.json) | [PASS](provider-simulation/20261002080234/gcp/guest/workload-redeploy.json) | [PASS](provider-simulation/20261002080234/openstack/guest/workload-redeploy.json) |
| `full-cleanup` | K3s·Cilium 전체 제거 및 K3s 파일 부재 확인 | [PASS](provider-simulation/20261002080234/aws/guest/full-cleanup.json) | [PASS](provider-simulation/20261002080234/gcp/guest/full-cleanup.json) | [PASS](provider-simulation/20261002080234/openstack/guest/full-cleanup.json) |

`clean-install` 전에 K3s 바이너리·데이터·설정이 모두 없는지 확인했습니다. K3s API 준비 후 Cilium을 설치하고 Node·CoreDNS(클러스터 DNS) 준비 상태를 확인했습니다. 일시적인 검사 Pod(앱 실행 단위)에서 DNS → Service(앱 접속 주소) → HTTP 200을 검사했고 노드에서도 지정 NIC의 NodePort(서버 포트로 앱을 공개하는 방식)로 요청했습니다.

재실행에서는 K3s와 Cilium을 다시 호출하고 Deployment·Pod UID(리소스 고유 식별값)가 유지되는지 확인했습니다. 업데이트 이후에는 입력 이미지와 실제 배포 이미지가 일치하는지 검증했습니다. 일반 cleanup(제거)은 앱 삭제를 확인했고, 전체 cleanup은 공식 제거 경로를 통해 K3s 파일 부재까지 확인했습니다.

Mac에서 세 포트로 HTTP 200 및 `Railshot Runtime OK` 본문을 확인했습니다. 이 경로는 **Mac → Lima SSH 포워딩(포트 전달) → VM NodePort**입니다. 인터넷 접속이나 클라우드 방화벽 통과를 검증한 경로는 아닙니다. 업데이트 중 무중단 요청률이나 부하를 측정한 결과도 아닙니다.

추가 확인 결과는 다음과 같습니다.

- Runtime 핵심 파일 15개의 SHA-256(파일 내용 식별값)이 세 VM과 호스트에서 동일합니다. [파일별 비교 기준](provider-simulation/20261002080234/core-reference.json)을 보존했습니다.
- 세 VM의 Linux boot ID가 모두 다릅니다. 각 `result.json`의 `guest.environment`에서 확인하실 수 있습니다.
- 실제 Runtime 응답 JSON 24개 모두 [출력 schema(데이터 규약)](../../schemas/output.schema.json)를 통과했습니다. [계약 검증 로그](provider-simulation/20261002080234/contract-validation.log)를 보존했습니다.
- 로컬 검사 21개에는 provider 입력 정규화, 잘못된 HTTP 본문 거부, 로그 압축 파일의 경로 이탈 거부, VM 정지 실패 시 다음 VM 실행 중단과 JSON 오류 반환을 포함합니다. [로컬 검사 로그](provider-simulation/20261002080234/unit-tests.log)를 보존했습니다. VM 정지 실패는 모의 주입 검사이며 실제 VM 정지 실패가 발생한 것은 아닙니다.

## 변경 내용과 발견한 문제

공통 엔진·bootstrap·Cilium 설치 코드·manifest(배포 정의)는 수정하지 않았습니다. 이번 세 환경의 실행에서는 Runtime 실패가 발견되지 않았습니다.

| 파일 | 역할 |
| --- | --- |
| `deployment/scripts/tests/provider_simulation.py` | Mac에서 새 VM 생성·설정·순차 검사·외부 HTTP 확인·증거 수집·VM 정지를 수행합니다. |
| `deployment/scripts/tests/simulated_node.py` | 새 Linux 노드에서 provider와 무관한 8단계 검증 및 전체 제거를 수행합니다. |
| `deployment/scripts/tests/fixtures/provider-simulation.json` | provider별 hostname·NIC·내부 IP·Mac 확인 포트를 제공합니다. 기존 aws/gcp/openstack fixture를 재사용합니다. |
| `deployment/scripts/tests/test_provider_simulation.py` | 테스트 하네스(자동 실행 도구)의 입력·HTTP·압축 파일·VM 정지 실패 경계를 검사합니다. |
| `deployment/README.md` | 실행 방법과 실제 검증 범위·제거 방법을 설명합니다. |
| `deployment/scripts/tests/results/` 아래 이번 보고서 및 실행 ID 폴더 | 원본 명령 로그·JSON·환경 정보·검증 결과를 보존합니다. |

호스트 Python 3.14가 초기 하네스의 `finally` 내부 `break`에 SyntaxWarning(구문 경고)을 출력했습니다. 다음 VM 실행 중단 판단을 `finally` 밖으로 옮겨 수정했습니다. 최초 실제 실행 로그에는 경고를 그대로 보존했으며, 수정 후 VM 정지 실패 경로를 포함한 로컬 검사 21개를 다시 통과했습니다. 변경은 하네스의 실패 제어 흐름에 한정되며 이번 실제 실행의 성공 경로와 Runtime 소스는 동일합니다.

메모리 확보를 위해 잠시 정지했던 기존 `railshot-runtime-test` VM도 다시 기동했습니다. 이때 K3s·Cilium·앱은 정상 복구했지만 이전 SSH master(연결 재사용 프로세스)가 Mac의 30082 포트를 계속 점유하여 외부 요청이 시간 초과했습니다. 해당 VM의 오래된 연결만 종료하고 현재 SSH 설정으로 포워딩을 재생성한 뒤 HTTP 200을 확인했습니다. Runtime 코드는 변경하지 않았습니다. 이 복구 확인은 위의 세 신규 노드 검사와 별도이며, [기존 주소 복구 결과](provider-simulation/20261002080234/existing-endpoints.json)에 30080·30081·30082의 실제 응답을 기록했습니다. 세 신규 모의 VM의 정지 후 확인 포트 30084~30086에 남은 리스너(접속 대기 프로세스)는 없었습니다.

## 재실행 및 증거 위치

Mac의 저장소 루트에서 실행합니다. Lima·Python 3.12 이상과 이미지 다운로드가 가능한 인터넷 연결이 필요합니다.

```bash
python3 deployment/scripts/tests/provider_simulation.py --disposable-vms \
  > provider-result.json 2> provider-test.log
```

새 실행 ID로 세 전용 VM을 새로 생성합니다. `--disposable-vms`는 전용 VM 내 전체 클러스터 제거를 허용하는 명시적 옵션입니다. 표준 출력은 provider별 JSON이며 표준 오류는 `[RUN]`, `[PASS]`, `[FAIL]` 진행 로그입니다. 종료 코드 0은 세 환경 모두 통과한 경우입니다.

- [호스트 실행 로그](provider-simulation/20261002080234/host-harness.log)
- [호스트 원본 표준 출력](provider-simulation/20261002080234/host-stdout.json)
- [AWS 원본 결과](provider-simulation/20261002080234/aws/result.json) / [GCP 원본 결과](provider-simulation/20261002080234/gcp/result.json) / [OpenStack 원본 결과](provider-simulation/20261002080234/openstack/result.json)
- 각 provider의 `guest/` 아래 단계별 `.json`, `.log`, `node-state.log`, `clean-state.txt`가 있습니다.

세 시험 VM은 클러스터 전체 제거 후 **정지 상태**입니다. 디스크는 증거 확인을 위해 보존했습니다. 지금은 해당 확인 포트로 접속할 수 없습니다. 결과 보존 후 VM 디스크까지 지우려면 다음 명령을 실행합니다.

```bash
limactl delete --force railshot-sim-aws-20261002080234
limactl delete --force railshot-sim-gcp-20261002080234
limactl delete --force railshot-sim-openstack-20261002080234
```

## 실제 클라우드에서 추가로 검증할 항목

| 항목 | 상태 | 실제 환경에서 확인할 내용 |
| --- | --- | --- |
| AWS IAM(접근 권한) | `requires real cloud smoke test` | 원격 실행·이미지 접근에 필요한 권한입니다. |
| Security Group(인스턴스 방화벽) | `requires real cloud smoke test` | 승인된 클라이언트의 NodePort 접근과 필요한 외부 통신입니다. |
| Elastic IP(고정 공인 주소) | `requires real cloud smoke test` | 공인 주소와 실제 도달 가능한 endpoint의 연결입니다. |
| VPC routing(가상 네트워크 경로) | `requires real cloud smoke test` | 서브넷·라우트·NAT·앱/서비스 주소 범위 충돌입니다. |
| GCP IAM(접근 권한) | `requires real cloud smoke test` | 노드 실행 및 이미지 접근 권한입니다. |
| VPC Firewall(가상 네트워크 방화벽) | `requires real cloud smoke test` | ingress/egress(들어오고 나가는 통신) 및 대상 노드 선택입니다. |
| External IP(외부 주소) | `requires real cloud smoke test` | 실제 클라우드 외부 클라이언트에서의 HTTP 응답입니다. |
| actual cloud metadata semantics(실제 노드 정보 서비스의 동작) | `requires real cloud smoke test` | IMDS/GCE metadata의 권한·헤더·응답·접속 제한입니다. |

공통 Runtime은 cloud metadata를 읽지 않으므로 IMDS/GCE metadata mock(모의 응답 서버)은 추가하지 않았습니다. metadata 동작이 검증됐다고 간주하지 않습니다. OpenStack 실제 보안 그룹·라우팅·Floating IP(외부 연결 주소)도 별도 환경에서 확인해야 합니다.

다음 실제 smoke test(짧은 통합 확인)는 각 Provider에서 전용 Linux VM 생성 → 동일 JSON과 `deployment/` 전달 → Runtime 실행 → 별도 클라이언트 HTTP 200 → cleanup 순서입니다. 실제 클라우드 권한·네트워크·다운로드 조건까지 충족해야 통과로 기록합니다.

## Provider/Ansible 연결 시 합의할 계약

Provider(인프라 제공 계층)는 Linux 노드와 관리자 실행 권한, 내부 주소, 원격 전달 방법, 방화벽·외부 접속 경로를 준비합니다. Ansible(원격 설정 자동화 도구) 또는 다른 상위 실행 도구가 JSON과 `deployment/`를 전달하고 대상 노드 안에서 실행합니다.

공통 엔진은 원격 SSH나 자원 생성을 하지 않습니다. `provider`는 입력 변환 계층과 결과 문맥에만 남으며, `runtime.node_ip`에는 실제 노드 NIC에 할당된 내부 주소를 사용합니다. cloud의 공인 NAT 주소를 직접 넣지 않습니다. 외부 공개 주소·TLS(암호화 연결)·로드밸런서 정보와 노드 내부 endpoint의 구분, 접속 자격정보 전달 방식, 재시도/rollback(이전 버전 복원) 책임은 팀에서 합의해야 합니다.

이번 검증은 JSON 임시 계약 `0.1`을 그대로 사용했습니다. 최종 Provider Interface나 Ansible 변수명이 확정되면 input adapter(외부 입력 변환 계층)에서 변환하며 Runtime에 provider 분기를 추가하지 않습니다.
