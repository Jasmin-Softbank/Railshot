# Standalone Cilium와 AWS 관측 구성 실자원 인수 검증

2026-10-02 18:07:55 KST부터 2026-10-02 18:22:28 KST까지 실행했다. 검증 대상은 PR #12의 코드 커밋
`02f59c55b4412fcb4d2605b43c3298311f9f80a5`이다. 기존 담당 feature 브랜치는 변경하지 않았다.
검증 후 아래 전용 자원은 모두 제거했다. 운영 배포 또는 제품 UI/API에서 시작하는 자동 배포 완료를 뜻하지 않는다.

## 환경과 접근 경계

- AWS `ap-northeast-2`, Ubuntu 24.04 amd64, K3s `v1.34.11+k3s1`, Cilium `1.20.2`, CLI `v0.20.1`.
- 신규 `t3.medium` 두 대: standalone runtime 한 대, Prometheus/Grafana/Blackbox 관측 VM 한 대.
  각각 암호화 gp3 20 GiB, `DeleteOnTermination=true`, shutdown 동작 `terminate`, 110분 자동 종료 타이머를 사용했다.
- `RailshotOwner=remaining-o11s-20261002` 태그와 비공개 자원 장부를 사용했다. 기존 VM·VPC·서브넷·IAM profile은 변경하지 않았다.
- 공개 SSH/관리 포트는 허용하지 않았다. SSM의 인증된 명령으로 SSH 공개 호스트키를 받아 `StrictHostKeyChecking=yes` SSH 터널을 사용했다.
- SG ingress는 관측 VM의 사설 IPv4 `/32`에서 runtime의 `30080/30081/30910`만 허용했다.
  Grafana `3000`과 Prometheus `9090`의 실제 listen 주소는 `127.0.0.1`이었고 Blackbox는 Docker 내부 포트만 사용했다.
- Grafana 비밀번호는 관측 VM에서 생성해 사용했으며 로그/Git/controller로 출력하지 않았다. 관리자 kubeconfig는 controller의 비공개 디렉터리에만 보관했다.

## 실행 결과

| 검사 | 관측 결과 |
| --- | --- |
| `site.yml` 신규 설치 | 144.13초, `ok=40 changed=21 unreachable=0 failed=0`; Cilium, Node Ready, CoreDNS, 노드 HTTP와 Pod DNS/HTTP 통과 |
| 같은 inventory·설정으로 재실행 | 44.96초, `ok=39 changed=3 unreachable=0 failed=0`; K3s restart handler 미실행, Cilium 재조정과 임시 probe 생성/제거 수행 |
| 다른 node identity로 재실행 | 기존 identity 비교에서 exit 2, `changed=0`; K3s 설정과 ownership 파일 SHA-256이 전후 동일 |
| 관측 리소스 설치 | 실제 서버 dry-run과 apply, kube-state-metrics Deployment 및 node-exporter DaemonSet rollout 통과 |
| 수집 | node/cluster/정상 HTTP/잘못된 HTTP 경로의 scrape 4개 모두 `up=1` |
| 실제 메트릭 | CPU rate, 가용 메모리, root filesystem, Node Ready=1, 샘플 Deployment 목표=1/가용=1 확인 |
| HTTP 성공/실패 구분 | `/`의 `probe_success=1`, `/railshot-missing-health`는 0. 잘못된 URL의 scrape 자체는 `up=1` |
| Cilium ingress 차단 | 허용된 관측 VM은 수집 성공. 다른 Namespace의 probe Pod는 exporter DNS 해석에 성공한 뒤 HTTP 5초 timeout으로 차단 확인 |
| 외부 메트릭 포트 차단 | controller에서 runtime 공인 IP의 `30081/30910` TCP 연결 모두 timeout |
| 수집기 중단 | cluster-metrics replica=0 이후 `up{job="cluster"}=0`; `up == 1`로 제한한 Node Ready 쿼리가 빈 결과를 반환 |
| 앱 장애 표시 | 전용 샘플에 존재하지 않는 이미지 적용; 대기 사유 값=1, 목표 replica=1/가용=0 확인 |
| 복구 | exporter와 샘플 이미지를 복원한 뒤 4개 scrape 모두 UP, 정상 probe=1/잘못된 경로=0, 목표=1/가용=1 재확인 |
| Grafana API | DB health `ok`, Prometheus datasource health `OK`, 지정 UID dashboard의 8개 패널과 패널 PromQL 데이터 확인 |

Grafana **브라우저 화면 렌더링과 스크린샷 검증은 수행하지 않았다**. 위 결과는 실제 서버의 Grafana API와
Prometheus 질의 결과에 근거한다. 정상 상태에서 장애 대기 사유 패널의 빈 결과는 장애가 없음을 뜻하며,
CPU·메모리·디스크·Node Ready·replica·HTTP 패널은 실제 시계열 존재를 확인했다.

## 발견한 설치 절차 수정

처음에는 `cluster.json` 전체에 `apply --dry-run=server`를 실행했다. 같은 List에 Namespace가 있어도
서버 dry-run은 이를 실제 생성하지 않으므로 나머지 namespaced 리소스가 `NotFound`로 실패했다.
[설치 안내](../../observability/README.md)에 전용 Namespace를 먼저 준비하는 명령을 추가했다.
이 순서로 실제 dry-run과 apply를 다시 실행해 통과했다. 생성 리소스, 권한, Cilium 설정 및 기존 runtime 코드는 변경하지 않았다.

관측 unit 검사 14개 중 12개 통과, 로컬 `PROMTOOL`/`BLACKBOX` 환경변수가 필요한 2개는 명시적으로 skip했다.
이와 별도로 실제 VM의 Prometheus·Blackbox 프로세스에서 정상/실패 HTTP와 수집 결과를 검사했다.

## 제거와 잔존 확인

1. 생성한 `cluster.json`의 리소스를 삭제하고 Namespace, ClusterRole, ClusterRoleBinding이 없는지 확인했다.
2. 일반 `docker compose down`이 named volume 두 개를 보존함을 확인했다. 이번 전용 검증 데이터에 한해
   `down --volumes --remove-orphans`를 실행한 뒤 해당 프로젝트의 container/volume/network가 모두 0임을 확인했다.
3. 장부에 기록한 VM 두 대만 terminate했다. AWS에서 각 상태가 `terminated`인지 확인하고 전용 SG를 삭제했다.
4. 생성한 EBS 두 개와 ENI 두 개를 **정확한 ID로 다시 조회**해 모두 없음, 해당 owner 태그의 EBS/SG 및 해당 SG의 ENI가 0임을 확인했다.
   Elastic IP, LB, NAT gateway, DNS 레코드는 생성하지 않았다.

이번 확인은 이 실행에서 만든 자원의 정리 결과이며 계정 전체의 비용이나 기존 자원 상태를 판정하지 않는다.
온프레미스, VPN, ARM64, 장시간 재부팅 복구, Grafana 브라우저 렌더와 제품 API/CD 연결은 이 검증 범위에 포함하지 않았다.

## 비공개 원본 증거

원본 로그·메트릭 응답·정확한 자원 ID·SSH known_hosts는 controller의
`/Users/mango/.local/share/railshot/remaining-integration-20261002/o11s/`에 보관한다(디렉터리 0700).
자격증명이나 kubeconfig 내용은 이 문서에 포함하지 않는다. 아래 SHA-256은 완료 시점의 원본 파일을 식별한다.

| 파일 | SHA-256 |
| --- | --- |
| `runtime-install.json` | `d7b804777ed98fb731043885c6863dedc1d8ec4b6bfd3d1ae479dae9b7714e0d` |
| `runtime-repeat.json` | `92a62a094c83878c9a66393c932c7b203e79194ec2776deb5091f7d90930b0da` |
| `identity-guard.json` | `718ec9dc2853422953321a63eccba48e2bd8a093ea994873840d48fdcb6115f6` |
| `metrics-healthy.json` | `619cfdd7b8690838f9130cff3418fa641fff3205a94cfee343d83d88bc2fe3c1` |
| `metrics-exporter-stopped.json` | `cd1a7648ae71b5200ea6a663b9b4970de8be74a990c83347cbf6b73cd22d6ead` |
| `metrics-image-failure.json` | `9127a3144f4ea0532f759be745c99bb1f9898886e6f2b3ac0fdcbbf2cb4fcbb0` |
| `metrics-recovered.json` | `40e8f81611a9864f7277c0c2422ff151a136723765845c959c994744161ba002` |
| `forbidden-probe.log` | `2d4d30a310eafa196fc4dc3617f10753c0fc7a61a25faba26841caec01a088c4` |
| `public-metrics-denied.json` | `3820b506b38140428f554938bb7e65532b4d62a62cf16f358cc31009d74a2b01` |
| `module-cleanup.json` | `32ca5ca8109952315739b401e8a8452f1bd87e4394081d2420827f1df31f872c` |
| `cleanup.json` | `ca7338dc7c6fe434673891806faed109e80a1a7d32fa0bf14a6bbebd2baca097` |
| `cleanup-readback.json` | `c6e4717db2aed25c1f157833770eaa747b24b9d48de58422d8ebdfb0d5952224` |
