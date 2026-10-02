# Deployment 통합 준비 점검 — 2026-10-02

이번 작업은 Runtime code 변경 없이 과거 변경 감사, PR/Vercel 상태, 원본 evidence(검증 근거), 클라우드 접근 전제조건, timeout과 팀 연결 규약을 점검합니다. 기존 검증 **65 PASS / 0 FAIL / 0 SKIP**과 이번 실행을 분리합니다.

## 과거 변경 감사와 브랜치 범위

시작 저장소는 `Jasmin-Softbank/Railshot`, branch는 `feature/deployment-runtime-seungmin`, HEAD는 `c732b3bc83ad1b9cab416f1d763cfee9be26ea05`이며 working tree(미커밋 파일 상태)는 깨끗했습니다. `origin`은 Railshot, `old-jasmin`은 Jasmin입니다. 모든 origin branch를 fetch(원격 이력 조회)하고 author commit·포함 branch·local reflog(로컬 작업 이력)·GitHub PR merge 기록을 대조했습니다.

- Railshot의 해당 author commit은 `c732b3b`이며 변경 368개는 모두 `deployment/`입니다. 미푸시 commit, main 직접 변경, 다른 담당 파일 변경은 발견하지 못했습니다.
- Local integration은 clone 시점 `adda5c7` 그대로입니다. 최초 원격 조회는 `021b8b2`, fetch 중 팀 통합이 진행되어 `362e235`를 확인했습니다. 다른 branch를 checkout하거나 merge/rebase/reset하지 않았습니다.
- `c732b3b`는 원격 `codex/integrate-runtime-20261002`와 integration에도 포함됩니다. 이는 팀원 `mangowhoiscloud`의 merge commit `4f97f82103476ced4c84407d33f167774eafd824` 및 [PR #3](https://github.com/Jasmin-Softbank/Railshot/pull/3)의 결과입니다. commit의 포함 여부만으로 본 작업의 직접 push라고 판단하지 않습니다.
- [PR #1](https://github.com/Jasmin-Softbank/Railshot/pull/1)은 `mangowhoiscloud`가 **2026-10-02 16:21:56 KST**에 MERGED(통합됨) 처리했습니다. `isDraft` 값은 true로 남아 있으나 열려 있는 Draft PR은 아닙니다. 본 작업에서 merge·되돌리기를 수행하지 않았고, 이미 통합된 PR을 Draft로 유지하거나 reopen하지 않았습니다.
- 최신 integration과 feature의 `deployment/`는 동일했습니다. integration 연결부는 `infrastructure/ansible/runtime.yml` 등에만 추가됐습니다. 자동 역병합 없이 읽기만 했습니다.

| Case | 감사 결과 |
| --- | --- |
| A: 다른 branch local checkout만 | Railshot clone 시 integration에서 own commit 없이 feature를 만들었습니다. 현재 feature이며 stash/patch가 필요한 기존 변경은 없습니다. |
| B: 다른 branch 미푸시 own commit | 발견하지 못했습니다. |
| C: 다른 branch에 과거 push | 기존 Jasmin의 승인된 `feature/poc-cloud-seungmin`, `chore/railshot-structure` 작업은 아래에 기록합니다. Railshot의 다른 branch는 팀의 merge 이력이며 본 작업의 직접 push가 아닙니다. |
| D: own feature에서 다른 담당 파일 변경 | Railshot `c732b3b`는 deployment 외 변경이 없습니다. |
| E: main/integration 직접 commit/push | 로컬 이력과 확인한 원격/PR 기록에서 발견하지 못했습니다. 자동 revert(되돌리는 커밋)는 수행하지 않습니다. |

### 기존 Jasmin의 deployment 외 이력

현재 작업 이전의 명시적 사용자 요청에 따른 변경입니다. 이관 때 해당 파일은 가져오지 않았으며 원복하지 않았습니다.

| Commit | 원래 push branch | 파일 |
| --- | --- | --- |
| c3be75003c2187061197c641cea4707aa3fb862c | feature/poc-cloud-seungmin | README.md, deployment-poc/의 초기 PoC·시험·문서 |
| fb503fd609161dc94cd167f75da0eef456d148e2 | feature/poc-cloud-seungmin | README.md, deployment-poc/README.md |
| b93d3a1d7a7ede54d1ac2c07e0148df8cd138146 | chore/railshot-structure | AGENTS.md |
| 4b5e22cb575c6b7ab8fb69f0b0dbe101064bc034 | chore/railshot-structure | README.md |

Jasmin `9ba8046`, `ab13440`, `f40a0a7`, `ec6a9df`는 deployment/만 변경했고 기존 Runtime branch에 push됐습니다. Main 직접 commit은 확인되지 않았습니다. Git history(커밋 이력) 자체는 조직의 전체 push audit log가 아니므로 author/contains 결과와 로컬 reflog·확인한 PR 주체를 구분했습니다.

## Vercel 실패 분류

**VERCEL_FAILURE_SCOPE = pre_existing**입니다. 기존 base 측 실패가 존재한다는 분류이며 build의 상세 원인까지 규명한 것은 아닙니다.

| 비교 commit | 상태 | 해석 |
| --- | --- | --- |
| adda5c7: 원래 base | status 없음 | 성공/실패를 추정하지 않음 |
| 1b1ebfe, 021b8b2: c732b3b 미포함 integration 이력 | Vercel FAILURE | 최신 Runtime을 포함하기 전에도 실패 현상이 존재함 |
| c732b3b: Runtime PR head | Vercel FAILURE | Deployment 65 PASS와 별개 |
| 362e235: 팀 통합 head | Vercel FAILURE | 통합 후에도 별도 웹 배포 연동 실패 |
| cc1f084: main | Vercel SUCCESS | main 결과를 integration/feature의 성공으로 확대하지 않음 |

이번 Runtime PR은 apps·root/Vercel config를 변경하지 않았고, c732b3b와 021b8b2의 apps 및 root Vercel config도 같았습니다. Vercel CLI는 현재 없고 직접 배포 상세 페이지는 로그인 화면으로 이동하여 상세 로그를 읽지 못했습니다. 따라서 동일한 상세 build 원인, 프로젝트 root 설정, 영향 경로는 미확인입니다. Vercel 설정과 다른 담당 앱/CI는 수정하지 않았습니다.

## 변경 파일 368개 감사

전체 변경 파일 크기는 **1,036,036 bytes(약 0.988 MiB)**입니다. 파일 수와 큰 airgap archive의 용량을 혼동하지 않습니다.

| 분류 | 파일 수 | 판단 |
| --- | --- | --- |
| A: source/test/schema/README/artifact metadata | 35 | 유지 필수 |
| A: summary 및 최소 machine-readable evidence | 17 | 유지 필수 |
| B/C: 원본 JSON·Linux log·실패/재실행 자료 | 316 | 대표 자료와 생성물을 포함하며 이번에는 유지 |

Markdown 외 result 파일 330개는 780,840 bytes입니다. 정확히 동일한 내용의 그룹은 22개, 추가 중복 파일은 69개였습니다. 이들은 source/target·실패/재시도·offline의 실행 문맥을 각각 보존하며 여러 보고서가 링크합니다. 이미 팀 integration에도 들어간 상태에서 작은 증거를 삭제하는 이득보다 이력·링크 검증 부담이 큽니다. 따라서 **삭제 0개**로 유지하고 이번 기록은 요약 JSON·unit 로그만 추가합니다. Fixture(시험 입력)와 기존 evidence를 삭제하거나 이동하지 않습니다.

## 이번 실행과 기존 검증 분리

| 항목 | 이번 결과 |
| --- | --- |
| Unit/contract/airgap/network/Release 검사 | 49 PASS / 0 FAIL / 0 SKIP |
| Bash syntax(구문) | 19개 파일 통과 |
| Python AST(구문 구조) | 20개 파일 통과 |
| 기존 실제 input/output의 JSON schema 재검사 | 21쌍 및 supplement input/3 outputs 통과 |
| 실제 macOS endpoint preflight | 0.816초, 필수 unavailable 없음 |
| arm64 archive/manifest SHA256와 size | 일치 |
| 실제 로컬 bundle 검증 | verified |
| AWS/GCP 신규 Runtime cloud smoke | 2 SKIP: 접근 권한/인증 도구 미준비 |
| 기존 AWS/GCP 앱 공개 health 관측 | 각각 HTTP 200; Runtime 새 배포 검사와 별도 |
| Linux 16개 전체 재설치 | NOT_RUN: Runtime 경로 변경 없음 |

테스트 수 합산은 **이번 49 PASS / 0 FAIL / 2 SKIP**(unit 49 + cloud smoke 2)입니다. 구문·무결성·schema·관측 결과는 별도 검사로 보존하며 PASS 수를 부풀리지 않습니다. Vercel FAILURE도 이 Runtime 검사 합계에 넣지 않습니다.

[이번 JSON 요약](review/current-summary.json), [unit 원본 로그](review/current-unit.log), [Cloud smoke 상세](CLOUD-SMOKE-2026-10-02.md)를 확인하시면 됩니다. 기존 **65 PASS**는 [이관 검증](MIGRATION-VALIDATION-2026-10-02.md)의 unit 49 + 실제 Linux 16 결과이며 이번에 Linux 16개를 새로 통과했다고 표현하지 않습니다.

## Timeout(대기 제한) 정책 분석

Runtime JSON 기본은 180초, 허용 범위는 5~900초이며 바꾸지 않았습니다. Shell 독립 실행 common.sh 기본은 300초, 팀 Ansible runtime도 WAIT_TIMEOUT 300초를 지정합니다. 이 값들은 동일하지 않으며 전체 deploy의 총 제한을 뜻하지 않습니다.

| 현재 구간 | 실제 제한과 의미 | 분리 검토 |
| --- | --- | --- |
| Network preflight | probe 기본 3초, subprocess 4초, endpoint별 병렬 | 이미 분리되어 있음; 긴 bootstrap timeout과 연동하지 않음 |
| Runtime helper 다운로드 | connect 10초/max-time 180초, retry 3회 | 다운로드 전체 retry budget과 설치 readiness를 분리할 가치가 있음 |
| K3s installer/설치 | JSON engine shell 제한 timeout+600초 | 공식 installer 내부 다운로드와 구간별 시간을 따로 측정해야 함 |
| K3s API readiness | WAIT_TIMEOUT, API 요청 30초, poll 3초 | 전체 shell 제한과 개별 readiness를 구분해야 함 |
| Cilium image pull/readiness | Cilium status WAIT_TIMEOUT, 이어 Node/CoreDNS wait | image pull은 containerd가 수행하며 현재 별도 timeout 필드 없음 |
| Workload rollout | JSON timeout, 명령 wrapper timeout+30초 | 현재 사용자 지정 값 유지 가능 |
| Service/health | 진단 Pod 대기 JSON timeout, curl 3초/10초, endpoint 요청 10초 | curl 제한과 Pod pull/readiness 대기는 다름; loop는 요청 1회만큼 초과 가능 |

Helper 다운로드의 max-time 180초는 각 시도에 적용되므로 retry 전체가 180초 안에 끝난다는 의미가 아닙니다. Cilium 상태 확인·Node/CoreDNS 확인은 순차여서 구간별 대기의 합이 늘어날 수 있습니다. 기본값을 전체 작업 SLA(완료 시간 보장)로 해석하지 않아야 합니다.

| 방안 | 이점 | 비용/제약 | 이번 판단 |
| --- | --- | --- | --- |
| A: 기본 180초 유지 | 빠른 오류 감지, 기존 계약 유지 | 관측된 cold image pull 약 225초는 초과 | 유지 |
| B: 최초 설치만 연장 | clean 설치 성공 가능성 개선 | cache/이미지 상태를 판정하는 규칙 필요 | 제안만; 미구현 |
| C: 단계별 timeout | 실패 위치와 시간 budget이 명확 | schema·adapter·engine·Ansible 계약 변경 필요 | 후속 우선 검토 |
| D: adaptive timeout(진행에 따른 자동 조정) | 느린 환경에 대응 | 진행률 판정·상한·멈춤 감지 복잡성 | 현재 PoC에는 보류 |
| E: 사용자 지정 timeout | 현재 코드로 cold 설치 대응 | 상위 도구가 명시적인 profile을 선택해야 함 | 현 단계 권장 |

당장은 A+E를 유지하고 clean smoke input에 600초를 명시하는 방안을 팀과 합의하는 것이 적절합니다. 다음 단계는 C의 download/K3s/Cilium/rollout/health budget을 분리하는 설계입니다. Network 401 응답은 registry 접근 가능 신호이며 image layer 다운로드 권한·속도를 보장하지 않습니다. 검증 bundle을 미리 전달하면 이 외부 다운로드 의존성을 줄일 수 있습니다. 이번에 timeout 정책과 Runtime code를 변경하지 않았습니다.

## Airgap·Release·Exposure 상태

실제 arm64 archive는 **747,700,166 bytes(713.062 MiB)**이며 SHA256은 `9951caad0711cba40608eb2dabc27122723d028a4233c0d09c8a03d378b806ac`입니다. Manifest SHA256은 `0772caa341314645645a42e0f41c39936d1ee535679bd38761fde263d6a7a0c3`입니다. 이번에 실제 파일을 다시 읽어 checksum과 size를 검사했고, bundle의 모든 declared file/image 검증도 통과했습니다.

Git에는 script와 작은 manifest/checksum/metadata가 있고 tar/OCI(이미지 archive)·bundle·dist/downloads는 없습니다. `.gitignore` 실제 적용을 확인했습니다. Metadata의 `architecture=arm64`, `publication_status=local_prepared`도 일치합니다. **amd64 artifact/설치는 미검증**입니다.

Railshot의 `airgap-bundle-v0.1.0` Release 조회는 404였으며 실제 Release 생성/업로드/원격 다운로드는 이번에 수행하지 않았습니다. 현재 상태는 **release helper implemented; remote roundtrip unverified**입니다. 업로드 전 별도 승인이 필요합니다. Online 앱 정책 GHCR은 실제 새 GHCR workload pull 성공과 다릅니다.

Cloudflare는 기존 public_url(이미 구성된 공개 주소)을 확인하는 선택 hook입니다. 미구성/접근 실패/explicit offline이면 exposure만 degraded(공개 경로 제한)이고 내부 endpoint를 유지합니다. 이번 default nodeport preflight에서는 Cloudflare를 검사하지 않았으므로 null이며 인증 연결을 확인했다고 표현하지 않습니다. Account/token/tunnel/DNS를 만들거나 바꾸지 않았습니다. WireGuard도 peer/key(상대 노드·인증키) 계약이 없어 unknown입니다.

## 팀 통합에 필요한 contract(연결 규약)

- 최신 Ansible runtime은 bootstrap shell 6개와 `airgap/versions.json`을 전달하고 실행합니다. JSON adapter/engine을 호출하거나 bundle/network module을 전달하지 않습니다. 따라서 **Ansible bootstrap 성공 = JSON auto/airgap 통합 성공**이 아닙니다. 전체 deployment/ 전달과 JSON CLI 호출로 바꿀지, 기존 bootstrap+Argo 역할을 유지할지 담당자와 합의해야 합니다.
- Ansible 요청 schema 1.0과 Deployment JSON 0.2를 동일 schema로 취급하지 않습니다. Provider/target/resource/SSH transport는 controller 책임이고 node-local 공통 spec에는 provider별 provisioning 분기를 넣지 않습니다.
- JSON 0.2의 mode/bundle path/신뢰 manifest pin·network_states·error.stage·stderr/stdout 수집 계약을 합의해야 합니다. 기존 0.1 필드와 상태 목록은 유지하지만 승인 버전/preflight 요구는 더 엄격합니다.
- Namespace·NodePort·기존 앱/Argo 소유권, 기존 K3s config와 cluster identity, 외부 health 검사 위치를 명시해야 합니다. 한 환경의 신규 시험을 위해 기존 30080 앱을 교체하지 않습니다.
- Cold timeout, bundle 배포 위치/CPU 구조/신뢰값/보관, version 승격, private registry credentials/CA, Tunnel 인증/URL, 원격 접속 권한과 작업 중단 복구는 추가 합의가 필요합니다.

AWS/GCP Runtime 재배포, amd64, authenticated Tunnel/WireGuard, GitHub Release 원격 전달, 장기 운영은 여전히 미검증입니다. 이번 commit/push는 현재 feature만 대상으로 하며 main/integration/타인 branch 직접 변경·PR merge는 수행하지 않습니다. 종료 commit과 push 결과는 최종 응답에서 실제 값으로 보고합니다.
