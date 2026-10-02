# Integration 동기화 및 Vercel 진단 — 2026-10-02

**Primary classification = G. UNKNOWN**입니다. Vercel(웹앱 호스팅)의 실제 build log와 현재 project settings에 접근하지 못해 원인을 확정할 수 없습니다. Deployment Runtime과의 관계는 **pre_existing(기존 실패)**으로 분류합니다. 로컬에서 확인한 환경 문제를 Vercel의 실패 원인으로 동일시하지 않습니다.

## 브랜치 동기화

| 항목 | 결과 |
| --- | --- |
| Repository | Jasmin-Softbank/Railshot |
| 작업 branch | feature/deployment-runtime-seungmin |
| 시작 HEAD | a50dd1a3239e2f8651ee61b4efe7a0dba55c08b4 |
| 최초 integration 조회 | 004709677627fe8f7bde7f73426a1b84a1a87686 |
| 작업 중 추가 integration 조회 | 08dc01ec51c98001a5cfc986ae3ae6539f938203 |
| 동기화 | git merge origin/integration/team-assembly-20261002, 두 번 모두 fast-forward |
| 신규 merge commit | 없음; 기존 팀 이력을 그대로 가져왔습니다. |
| 충돌 | 없음 |
| push | 먼저 0047096 동기화분을 지정 feature에 push했습니다. 최종 진단 기록과 08dc01e 포함 여부는 최종 응답에서 실제 원격 SHA로 확인합니다. |

이전 점검 commit a50dd1a는 팀이 PR #6으로 integration에 이미 통합했습니다. 따라서 이 작업에서 다른 branch를 checkout하거나 rebase·force push할 필요가 없었습니다. Team integration에는 대시보드/CI/인프라/AGENT.md 변경이 포함되어 있어 merge로 가져왔으나, 이 파일을 직접 편집하지 않았습니다. Main/integration/타인 branch 직접 commit·push, PR merge는 수행하지 않습니다.

## Vercel에서 실제 확인한 것

- GitHub PR #1의 Vercel status: FAILURE. Deployment identifier는 `dpl_5oK6ujZo2X7LnxkTvGLagd3Kfzri`입니다. [실패 상세 URL](https://vercel.com/ghdwlsrl100-gachonackrs-projects/railshot/5oK6ujZo2X7LnxkTvGLagd3Kfzri)은 현재 browser에서 Vercel 로그인 화면으로 이동합니다.
- Vercel bot comment는 프로젝트 `railshot`, 실패 상세 URL, Error 상태를 보여줍니다. Comment의 machine metadata는 `rootDirectory: null`입니다. **과거 comment snapshot이며 현재 인증된 settings 조회 결과가 아닙니다.**
- 이 실패의 첫 실제 build error는 로그 접근이 없어 확인하지 못했습니다. GitHub의 `Deployment has failed`는 일반 상태 설명이며 컴파일 오류나 output 오류를 뜻한다고 단정할 수 없습니다.
- c732b3b(Runtime 작업 commit)이 없는 integration 이력 `1b1ebfe`, `021b8b2`에서도 Vercel FAILURE를 확인했습니다. 원래 base adda5c7에는 status가 없어 성공으로 간주하지 않습니다.
- 동기화 대상 0047096과 08dc01e의 GitHub combined status는 조회 당시 `pending`, status 배열은 비어 있었습니다. 실제 Vercel 실행이 대기 중인지 또는 이 commit에 status가 없는지 구분할 수 없습니다. 최신 commit도 실패한다고 확대하지 않습니다.
- Vercel CLI·로컬 인증 파일·VERCEL_TOKEN·project link는 현재 환경에서 찾지 못했습니다. Credential 내용이나 token을 출력하거나 로그인·프로젝트 연결·설정 변경을 시도하지 않았습니다.

## Repository에서 확인한 구성

| 항목 | 실제 내용 |
| --- | --- |
| Root workspace | apps/* |
| Root build | npm run build --workspace @railshot/dashboard |
| Dashboard build | vite build |
| Framework dependency | Vite 8.2.2, Rolldown 1.2.12 |
| Dashboard engines.node | >=20.19 |
| 실제 Vite/Rolldown Node 요구 | ^20.19.0 또는 >=22.12.0 |
| API workspace Node 요구 | >=22 |
| Root Node engines/packageManager | 명시 없음 |
| 실제 build 결과 위치 | apps/dashboard/dist/ |
| Root dist/ | 없음 |
| Vercel config/link file | 저장소에서 찾지 못함 |
| Preview env(빌드 환경변수) | Dashboard에 VITE_/import.meta.env 참조 없음; VITE_ 변수가 없는 환경에서 Node 22 build 성공 |

Workspace 호출은 정상입니다. Vite build가 dashboard 작업 위치에서 실행되어 해당 directory 아래 dist를 생성합니다. 정적 dashboard build 성공은 `/healthz`와 `/api/...` backend 연결·인증·제품 동작의 성공을 뜻하지 않습니다. API/GitHub/operator 설정은 서버 운영 시의 별도 요구사항이며 이 build의 필수 env로 확인되지 않았습니다.

## 실제 로컬 재현

Source와 lockfile을 수정하지 않고 저장소 root에서 실행했습니다. Node 선택은 해당 subprocess의 PATH만 지정했으며 전역 설정·.nvmrc·package.json을 바꾸지 않았습니다.

| 실행 | 결과 | 관측 |
| --- | --- | --- |
| Node 21.7.1 / npm 10.5.0: npm ci | exit 0 | EBADENGINE 경고, 24개 package 설치 |
| Node 21.7.1: npm run build | exit 1 | Rolldown native binding을 찾지 못함 |
| Node 22.22.2 / npm 10.9.7: npm ci | exit 0 | 25개 package 설치, 해당 engine 경고 없음 |
| Node 22.22.2: npm run build | exit 0 | Vite 8.2.2, 5개 module 변환, 300ms, dist 생성 |

최초 로컬 오류의 의미 있는 부분은 다음입니다.

```text
Error: Cannot find native binding.
cause: Cannot find module '@rolldown/binding-darwin-arm64'
```

소유 package는 dashboard의 Vite 의존성인 `node_modules/rolldown`입니다. Node 21은 실제 Vite/Rolldown 지원 범위 밖이며 npm이 optional native dependency(운영체제별 선택 실행 파일)를 설치하지 않은 상태였습니다. 같은 lockfile로 지원 범위 내 Node 22/npm 10.9.7을 사용하면 binding-darwin-arm64가 설치되고 build가 통과했습니다. **Node와 npm을 함께 변경한 비교이므로 각각의 영향을 독립적으로 분리한 실험은 아닙니다.** Lock에는 darwin/linux의 native binding 항목이 모두 있어 플랫폼 항목이 lock에서 누락됐다고 판단하지 않습니다.

오류 메시지가 제안한 lockfile 삭제·npm install은 실행하지 않았습니다. 두 번 모두 npm ci를 사용했으며 package-lock.json·package.json·dashboard source의 내용이 HEAD와 동일한 것을 확인했습니다. 생성된 node_modules/dist는 Git에서 제외됩니다.

원본 install/build 로그와 [machine-readable 진단](vercel-diagnosis/summary.json)을 함께 보존합니다.

- [Node 21 install](vercel-diagnosis/npm-ci-node21.log), [Node 21 실패 build](vercel-diagnosis/build-node21.log)
- [Node 22 install](vercel-diagnosis/npm-ci-node22.log), [Node 22 성공 build](vercel-diagnosis/build-node22.log)

## 현재 project settings의 확인 수준

| Vercel 항목 | 확인 수준 |
| --- | --- |
| Root Directory | 과거 bot metadata null; 현재 settings 미확인 |
| Build Command | Repository 기대값은 npm run build; 실제 override 미확인 |
| Install Command | 로컬 npm ci 성공; 실제 Vercel 명령 미확인 |
| Framework Preset | Repository는 Vite; 실제 설정 미확인 |
| Output Directory | 로컬 결과 apps/dashboard/dist; 실제 설정 미확인 |
| Node.js runtime | 현재 Vercel 버전 미확인 |
| Preview env | 설정 목록 미접근; 저장소 build는 별도 env 없이 성공 |

지원 환경에서 repository build가 성공하므로, 현재 Vercel의 환경·project 설정 차이를 우선 조사해야 합니다. **Vercel 원인을 E(output path), D(Node), B(project config) 중 하나로 확정할 증거는 없습니다.** Root Directory가 repository root라면 이 build의 결과는 apps/dashboard/dist에 있으며, dashboard root라면 dist에 있습니다. 이 상대 경로가 실제 설정과 일치하는지 확인하는 것은 합리적인 다음 검사입니다. 설정 불일치를 관측한 것은 아닙니다.

[Vercel 공식 build 문서](https://vercel.com/docs/builds/configure-a-build)는 Root Directory와 framework·build/install·output 설정을 구분합니다. [공식 Node 버전 문서](https://vercel.com/docs/functions/runtimes/node-js/node-js-versions)에 따라 실제 project Node 버전도 별도로 확인해야 합니다. 이 문서는 프로젝트의 현재 설정을 증명하는 자료가 아닙니다.

## 담당자에게 필요한 조치와 수정 후보

담당 영역은 **dashboard/workspace 및 Vercel 프로젝트 관리**입니다. Deployment의 K3s/Cilium/airgap core를 고쳐서 해결할 근거는 없습니다. 메시지를 팀원에게 보내거나 설정을 변경하지 않았습니다.

1. Vercel 프로젝트 관리자가 위 deployment의 build log에서 첫 오류와 commit SHA를 확인하고, Node/npm 버전·install/build 명령·Root/Output Directory·Framework Preset을 함께 제공해야 합니다. Secret/env 값은 공유하지 않고 필요 시 이름과 존재 여부만 확인합니다.
2. 빌드가 끝났지만 output을 찾지 못했다면 실제 root 기준 apps/dashboard/dist 또는 dist를 비교합니다. 설정 변경은 승인 후 담당자가 수행합니다.
3. Node/native binding 오류가 실제 Vercel에도 있으면 설치 정책·Node/npm 조합·optional dependency 상태를 비교합니다. 이번 Mac의 Node 21 오류를 Linux Vercel에 그대로 적용하지 않습니다.
4. Preview build 이후 API 연결은 별도 제품 통합 검사로 진행합니다. 정적 빌드 통과를 backend 배포 통과로 취급하지 않습니다.

승인된 해결에 필요할 수 있는 후보는 Vercel project settings, root package.json의 engine/package-manager 명시, 필요 시 vercel.json, apps/dashboard/package.json입니다. **로그로 확인된 원인에 따라서만 선택해야 하며 현재 수정 필요가 확정된 파일은 없습니다.** Lockfile 변경도 필요하다고 확인한 것이 아니며 임의 재생성하지 않습니다. Vercel 설정만의 문제라면 repository 파일 수정 자체가 필요하지 않을 수 있습니다.

## 검증과 범위

동기화 후 Deployment unit/contract 49개, Ansible Cilium 연결 검사 5개, Cilium control/foreign-CNI guard(다른 네트워크 플러그인 변경 방지) 검사 3개를 통과했습니다. 추가 검사에 필요한 PyYAML 6.0.3은 기존 CI 요구 버전으로 임시 검증 venv에만 설치했습니다. 이전 yaml 미설치 오류는 테스트 환경의 의존성 문제였으며 코드 수정으로 해결하지 않았습니다. 실제 Linux 전체 재설치·클라우드 배포는 이번 목표에 포함해 수행하지 않았습니다.

본 작업의 신규 변경은 이 진단 문서와 최소 증거뿐입니다. Apps/dashboard/API/root package.json/lockfile/Vercel/CI/infrastructure/타인 docs는 직접 편집하지 않았습니다. Merge로 가져온 기존 팀 변경과 본 작업의 문서 변경은 구분합니다. Main 직접 변경 NO, integration 직접 변경 NO, 다른 branch 직접 변경 NO, PR merge NO입니다.
