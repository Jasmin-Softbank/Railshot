# 기존 환경에 앱 자동 등록

2026-10-03T01:07:49Z까지 확보한 운영 증거 기준. 신규 앱 등록과 이미지 게시를 실제 업로드로 확인했다. AWS 공개 경로 적용 전 설정 오류를 보완했으며, 같은 게시 산출물로 CD를 재개하는 수정의 운영 반영을 기다린다. 사용자 URL까지의 E2E는 아직 완료되지 않았다.

- 입력: 빈 앱 목록에서 실제 소스를 업로드하고 기존 AWS/GCP/OpenStack 환경을 선택한다.
- 등록: 환경 접속·클러스터 권한을 재사용하고 앱 전용 namespace, NodePort, Argo project, GitOps 경로, CI target binding을 만든다.
- 전달: 업로드 source commit, app, environment, target, 게시 image digest를 검증한다. 이름 불일치 검사를 유지한다.
- 공개: 기존 native LB에 앱 경로를 추가하고 Cloudflare DNS를 등록한다. Argo revision·실행 image digest·앱 health 및 서비스 경로 HTTPS 200으로 완료를 판정한다.

| 검증 | 확인한 결과 | 증명하지 않는 범위 |
| --- | --- | --- |
| 로컬 GitOps 계약 | 74개 통과: DNS 소유권·불확실 결과·TLS/HTTP·native saved plan 변경 범위 | 실제 신규 앱 배포 |
| 앱 등록·게시·경로 계약 | 34개 통과: 같은 환경의 여러 앱, 동일 앱 재사용, source/image binding | 실제 provider 권한·네트워크 |
| Cloudflare 실호출 | AWS 계산기 예정 hostname CNAME 생성·API 재조회 일치, 2026-10-02T22:33:32Z | ALB 앱 rule·Pod·HTTPS 성공 |
| 재개 API 회귀 | 전체 API 184개 및 마지막 사전 검사 재시도 보완 회귀 5개 통과. 세션·source·artifact·attempt·image 고정, 동일 배포 재개, CI/등록 반복 방지 | 운영 컨테이너의 외부 실행 |
| 재개 UI 및 등록 사전 검사 | 브라우저 13개, Python 등록·경로 29개 통과 | 클라우드 E2E |
| 최초 업로드 당시 API | source `e30feb7fca4ee65a78fa469b29421870542ea5bd`, API digest `sha256:c316d3ca50de2be396306c7c08cfd11d879a22a57b8b2f1f9ea71a1ca375dcda`, Argo revision `f989478267582ff0f395215251fae6081710c5dd` 일치·Ready 확인 | 현재 최신 이미지 또는 신규 앱 E2E 전체 성공 |
| 운영 설정 | `applications_file` 및 GCP 자격 파일 참조 연결 후 AWS/GCP/OpenStack options `available=true` 확인 | 각 공급자의 신규 앱 실배포 성공 |
| AWS 최초 등록·CI | 브라우저의 실제 `calculator.zip` 업로드에서 namespace·권한·Argo·자격 갱신·CI binding 자동 생성, CI 및 GHCR 게시 성공 | LB 경로·Pod·HTTPS 성공 |

## 최초 업로드 증거

업로드 직전 운영 DB의 앱 수 0, 진행 중/결과 불명 작업 0, 앱 binding 파일 0을 확인했다. 수동 앱 등록 없이 ZIP 선택 → 선택 내용 확인 → 배포 시작을 실행했다.

| 항목 | 값 |
| --- | --- |
| 환경 | `k3s-aws` |
| 앱 | `calculator` |
| deployment | `3c276c54-36cb-444e-b353-35b078fdcca8` |
| application / target / namespace | `app-6a1a2d16ee77fce182a31f60` |
| NodePort | `32576` |
| 업로드 파일 내용 digest | `509f9859ff10c232a1dc5ca4aa6abbaf6f886a66837a683c1a3c30f7e0336cd0` |
| 게시용 소스 commit | `35afdaeac23cc03d5a3cfd13b8351c0b8214cf14` |
| CI | [37081493857](https://github.com/Jasmin-Softbank/railshot-apps/actions/runs/37081493857) |
| 예약 hostname | `calculator-fb2427c7fd33.railshot.io` |
| 게시 artifact / attempt | `11259187657` / `1` |
| 게시 이미지 | `ghcr.io/jasmin-softbank/demo-calculator-web@sha256:786d577e78009a47834f999997c6115795e21127c9558decc629ec56200f0afc` |
| 2026-10-03T00:28:03Z readback | registration `succeeded`, CI `published`, operation `unknown / cd`, HTTP `not_run`, URL 없음 |

이 hostname의 DNS는 앞선 Cloudflare 실호출 검증으로 이미 생성돼 있다. 이번 업로드에서 레코드를 새로 만들었다고 주장하지 않는다. 신규 hostname 생성 경로와 동일 앱 재배포·다른 앱 격리는 별도 검증 대상이다.

AWS runtime에 보안 그룹 두 개가 연결되어 있는데 환경 descriptor에 선택값이 없어 `aws_security_group()`에서 중단됐다. `edge.prepare` 이전 실패이며 native allocation은 없고 Terraform state serial 49가 유지됐다. 실제 instance·private IP·연결된 보안 그룹과 기존 경로를 대조하여 지원 필드 `security_group_id`를 보완했고 앱 등록 fingerprint가 그대로임을 확인했다. 원래 배포 기록·CI run·publication 파일을 초기화하지 않았다.

[PR #81](https://github.com/Jasmin-Softbank/Railshot/pull/81)은 01:07:49Z에 `a6e2beecf208c892aeb3fe465ff3045007b0ed0f`로 병합됐다. 같은 배포의 `actions`에 `resume`를 요청하는 API와 버튼, 등록·CI 전 AWS 경로 사전 검사를 추가했다. 실제 재개는 [자동 릴리즈 37084898946](https://github.com/Jasmin-Softbank/Railshot/actions/runs/37084898946)의 새 코드 반영 확인 후 수행한다. 불확실한 native intent는 지우거나 무조건 재실행하지 않는다.

[선행 릴리즈 37083829119](https://github.com/Jasmin-Softbank/Railshot/actions/runs/37083829119)는 플랫폼 이미지·Argo 검증과 CI-runtime 자동 업데이트가 성공했지만 후속 공통 multicloud 관측 등록은 실패했다. 플랫폼 배포, 공통 노드 릴리즈, 사용자 앱 배포의 결과를 구분한다.

실호출 상세는 운영자 비공개 `~/.local/share/railshot/app-registration-20261003/`의 DNS·Secret receipts에 보존한다. 비밀값은 저장소·공개 API·CI 로그에 넣지 않는다.

남은 완료 조건:

1. AWS/GCP Terraform state와 환경 접속 참조를 API PVC로 이관했고 이전 writer 중지·state SHA readback·실제 API 자격·변경 없는 native plan을 확인했다. 첫 앱 경로의 실제 apply 결과는 아직 별도 확인이 필요하다.
2. AWS 첫 실제 ZIP 업로드와 자동 앱 등록은 완료했다. GCP/OpenStack 첫 업로드는 아직 미실행이다.
3. CI 게시부터 각 provider의 실제 앱 URL까지 검증하고 deployment ID·run ID·source/image/Git revision을 기록한다.
4. 같은 앱 재배포와 같은 환경의 다른 앱이 기존 앱을 덮어쓰지 않는지 확인한다.
5. OpenStack native route → Named Tunnel → proxied DNS writer는 운영 이미지에 연결했다. 실제 신규 앱의 NodePort와 HTTPS 응답 검증은 미실행이다.

실제 실행에서 GitHub가 Check `details_url`을 `/runs/<check-id>`로 정규화해 agent 이벤트가 갱신되지 않는 관측 결함을 발견했다. [PR #77](https://github.com/Jasmin-Softbank/Railshot/pull/77)에서 정확한 check ID와 producer binding을 유지하며 해당 URL 형식을 수용했다. CI 본체 성공과 이벤트 관측의 완전성은 별도로 검증한다.

관측 GAP(2026-10-03T01:07:49Z): 기존 실패 기록의 최상위 상태는 `unknown`이지만 중단 직전 `cd.state=running`은 남아 있다. 실행 중이라는 증거로 사용하지 않으며, 단계 상태와 작업 종료의 정합성은 추가 정리 대상이다.

알려진 제한: 앱별 라우팅 계약 변경은 자동 갱신하지 않는다. 불확실한 외부 변경은 자동 재시도하지 않고 `unknown`으로 남긴다. 게시 이후 같은 산출물의 명시적 CD 재개만 제공하며, 모든 미완료 단계의 자동 resume를 주장하지 않는다.
