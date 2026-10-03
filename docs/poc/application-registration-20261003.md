# 기존 환경에 앱 자동 등록

2026-10-03 운영 readback 기준 구현 및 검증 구분. 신규 앱 등록은 실제 업로드로 확인했으며, 이미지 게시부터 사용자 URL까지의 E2E는 아직 진행 중이다.

- 입력: 빈 앱 목록에서 실제 소스를 업로드하고 기존 AWS/GCP/OpenStack 환경을 선택한다.
- 등록: 환경 접속·클러스터 권한을 재사용하고 앱 전용 namespace, NodePort, Argo project, GitOps 경로, CI target binding을 만든다.
- 전달: 업로드 source commit, app, environment, target, 게시 image digest를 검증한다. 이름 불일치 검사를 유지한다.
- 공개: 기존 native LB에 앱 경로를 추가하고 Cloudflare DNS를 등록한다. Argo revision·실행 image digest·앱 health 및 서비스 경로 HTTPS 200으로 완료를 판정한다.

| 검증 | 확인한 결과 | 증명하지 않는 범위 |
| --- | --- | --- |
| 로컬 GitOps 계약 | 74개 통과: DNS 소유권·불확실 결과·TLS/HTTP·native saved plan 변경 범위 | 실제 신규 앱 배포 |
| 앱 등록·게시·경로 계약 | 34개 통과: 같은 환경의 여러 앱, 동일 앱 재사용, source/image binding | 실제 provider 권한·네트워크 |
| Cloudflare 실호출 | AWS 계산기 예정 hostname CNAME 생성·API 재조회 일치, 2026-10-02T22:33:32Z | ALB 앱 rule·Pod·HTTPS 성공 |
| API 회귀 | 152개 통과: 업로드, 세션별 앱 목록·상세, CI identity, publication 재시도 | 운영 컨테이너의 외부 실행 |
| 운영 API | source `e30feb7fca4ee65a78fa469b29421870542ea5bd`, API digest `sha256:c316d3ca50de2be396306c7c08cfd11d879a22a57b8b2f1f9ea71a1ca375dcda`, Argo revision `f989478267582ff0f395215251fae6081710c5dd` 일치·Ready 확인 | 신규 앱 E2E 전체 성공 |
| 운영 설정 | `applications_file` 및 GCP 자격 파일 참조 연결 후 AWS/GCP/OpenStack options `available=true` 확인 | 각 공급자의 신규 앱 실배포 성공 |
| AWS 최초 등록 | 브라우저의 실제 `calculator.zip` 업로드에서 namespace·권한·Argo·자격 갱신·CI binding 자동 생성 성공 | 이미지 게시·LB 경로·Pod·HTTPS 성공 |

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
| 2026-10-03T00:18:55Z readback | registration `succeeded`, operation `running / ci`, CD `not_started`, HTTP `not_run` |

이 hostname의 DNS는 앞선 Cloudflare 실호출 검증으로 이미 생성돼 있다. 이번 업로드에서 레코드를 새로 만들었다고 주장하지 않는다. 신규 hostname 생성 경로와 동일 앱 재배포·다른 앱 격리는 별도 검증 대상이다.

플랫폼 이미지의 자동 게시·Argo 검증은 통과했지만, [플랫폼 릴리스 37079070175](https://github.com/Jasmin-Softbank/Railshot/actions/runs/37079070175)의 후속 공통 multicloud 작업은 실패했다. 플랫폼 배포 성공, 공통 노드 릴리스 성공, 사용자 앱 배포 성공을 구분한다.

실호출 상세는 운영자 비공개 `~/.local/share/railshot/app-registration-20261003/`의 DNS·Secret receipts에 보존한다. 비밀값은 저장소·공개 API·CI 로그에 넣지 않는다.

남은 완료 조건:

1. AWS/GCP Terraform state와 환경 접속 참조를 API PVC로 이관했고 이전 writer 중지·state SHA readback·실제 API 자격·변경 없는 native plan을 확인했다. 첫 앱 경로의 실제 apply 결과는 아직 별도 확인이 필요하다.
2. AWS 첫 실제 ZIP 업로드와 자동 앱 등록은 완료했다. GCP/OpenStack 첫 업로드는 아직 미실행이다.
3. CI 게시부터 각 provider의 실제 앱 URL까지 검증하고 deployment ID·run ID·source/image/Git revision을 기록한다.
4. 같은 앱 재배포와 같은 환경의 다른 앱이 기존 앱을 덮어쓰지 않는지 확인한다.
5. OpenStack native route → Named Tunnel → proxied DNS writer는 운영 이미지에 연결했다. 실제 신규 앱의 NodePort와 HTTPS 응답 검증은 미실행이다.

실제 실행에서 GitHub가 Check `details_url`을 `/runs/<check-id>`로 정규화해 agent 이벤트가 갱신되지 않는 관측 결함을 발견했다. CI 본체 실행과 구분하며, 수정 전 이벤트 스트림을 완전한 진행 증거로 사용하지 않는다.

알려진 제한: 앱별 라우팅 계약 변경은 자동 갱신하지 않는다. 불확실한 외부 변경은 자동 재시도하지 않고 `unknown`으로 남긴다. 재시작 시 미완료 작업의 자동 resume는 이 변경에서 완성하지 않았다.
