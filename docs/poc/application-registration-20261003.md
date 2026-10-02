# 기존 환경에 앱 자동 등록

2026-10-03 07:50 KST 기준 구현 및 검증 구분. 운영 E2E는 아직 진행 중이다.

- 입력: 빈 앱 목록에서 실제 소스를 업로드하고 기존 AWS/GCP 환경을 선택한다.
- 등록: 환경 접속·클러스터 권한을 재사용하고 앱 전용 namespace, NodePort, Argo project, GitOps 경로, CI target binding을 만든다.
- 전달: 업로드 source commit, app, environment, target, 게시 image digest를 검증한다. 이름 불일치 검사를 유지한다.
- 공개: 기존 native LB에 앱 경로를 추가하고 Cloudflare DNS를 등록한다. Argo revision·실행 image digest·앱 health 및 서비스 경로 HTTPS 200으로 완료를 판정한다.

| 검증 | 확인한 결과 | 증명하지 않는 범위 |
| --- | --- | --- |
| 로컬 GitOps 계약 | 74개 통과: DNS 소유권·불확실 결과·TLS/HTTP·native saved plan 변경 범위 | 실제 신규 앱 배포 |
| 앱 등록·게시·경로 계약 | 34개 통과: 같은 환경의 여러 앱, 동일 앱 재사용, source/image binding | 실제 provider 권한·네트워크 |
| Cloudflare 실호출 | AWS 계산기 예정 hostname CNAME 생성·API 재조회 일치, 2026-10-02T22:33:32Z | ALB 앱 rule·Pod·HTTPS 성공 |
| API 회귀 | 152개 통과: 업로드, 세션별 앱 목록·상세, CI identity, publication 재시도 | 운영 컨테이너의 외부 실행 |
| 운영 자격 | 제한된 DNS token을 Kubernetes Secret에 저장하고 readback 일치 | 새 API 이미지 배포 및 자동 호출 |

실호출 상세는 운영자 비공개 `~/.local/share/railshot/app-registration-20261003/`의 DNS·Secret receipts에 보존한다. 비밀값은 저장소·공개 API·CI 로그에 넣지 않는다.

남은 완료 조건:

1. AWS/GCP Terraform state와 환경 접속 참조는 API PVC로 이관했다. 이전 writer 중지·state SHA readback을 확인했으며 API credential/native 실행 점검은 계속 진행 중이다.
2. 새 API 이미지에서 최초 실제 업로드를 접수한다. 수동 앱 preregistration으로 이 조건을 대신하지 않는다.
3. CI 게시부터 각 provider의 실제 앱 URL까지 검증하고 deployment ID·run ID·source/image/Git revision을 기록한다.
4. 같은 앱 재배포와 같은 환경의 다른 앱이 기존 앱을 덮어쓰지 않는지 확인한다.
5. OpenStack 담당의 native 공개 경로 writer를 연결한다. 현재는 미지원으로 표시한다.

알려진 제한: 앱별 라우팅 계약 변경은 자동 갱신하지 않는다. 불확실한 외부 변경은 자동 재시도하지 않고 `unknown`으로 남긴다. 재시작 시 미완료 작업의 자동 resume는 이 변경에서 완성하지 않았다.
