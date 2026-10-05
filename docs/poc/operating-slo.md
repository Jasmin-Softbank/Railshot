# 운영 SLO와 배포 검증

이 문서는 운영 목표다. 로컬 테스트 통과는 회귀 방지 근거이며 운영 SLO 달성의 증거가 아니다. 측정할 때 운영 이미지 digest, GitOps revision, operation ID, 관측 구간과 표본 수를 함께 기록한다. 해커톤 환경의 예약 종료 시간은 별도로 공개하며 24시간 서비스로 계산하지 않는다.

| 지표 | 운영 목표 | 측정 경계 |
| --- | --- | --- |
| 조회 API | p95 500ms, p99 1초 이하 | 공개 HTTPS 요청 시작부터 응답 본문 수신까지 |
| 조회 가용성 | 합의한 운영 시간의 rolling 28일 99.9% 이상 | 유효 요청의 성공 비율. 5xx, timeout, 플랫폼 용량 부족을 숨기지 않음 |
| 진행 상태 표시 | p95 15초 이하 | backend 상태 확정부터 브라우저의 동일 상태 표시까지 |
| 짧은 데모 CI | runner 대기 p95 60초, loop 실행 180초, release 60초 이하 | GitHub job의 created/start/complete 시각. AI 수정 시간도 별도 기록 |
| 짧은 데모 전체 배포 | p95 5분 이하 | 접수부터 동일 digest의 공개 URL 검증까지. 신규 인증서·DNS 준비 시간도 포함 |
| Argo 적용 | p95 120초 이하 | config commit 게시부터 동일 revision의 Synced/Healthy까지 |
| Argo 캐시 | 정상 앱 등록으로 공유 클러스터 캐시 무효화 0회 | shared Secret scope와 controller invalidation 로그 |
| 클러스터 자격 갱신 | AWS/GCP 선택 대상 전체를 300초 내 완료, 갱신 후 만료까지 2시간 이상 | 항목별 시작·종료·소요 시간·오류 코드와 전체 성공 수. 일부 성공을 전체 성공으로 계산하지 않음 |
| 앱 실행 상태 관측 | 상세 화면의 현재 workload와 공개 HTTP 관측 시각을 표시 | 등록 완료·과거 배포 성공과 현재 실행 상태를 구분. 수집 실패를 정상이나 0으로 대체하지 않음 |
| Pod 재시작 복구 | 120초 이하, 저장 상태 손실 0건 | API/MCP readiness, 기존 세션·동일 bearer·배포 조회 검증 |
| 데이터 복구 | RPO 1시간, RTO 60분 이내 | 마지막 성공한 외부 복구점 / 장애 선언부터 데이터·세션·공개 조회 복구까지 |

최초 목표의 적용 대상은 준비된 AWS/GCP 환경의 작은 HTTP 데모다. 큰 저장소와 임의 앱의 빌드 시간을 같은 숫자로 보장하지 않는다. 첫 인증서 발급, AI 수정, registry 게시, cache 초기화, provider health check는 총시간에서 제거하지 않고 각각 구간으로 기록한다. 짧은 기준선 두 건이나 수십 회 GET를 장기 p95·가용성 달성으로 표현하지 않는다.

## 노드와 용량

운영 제어면/API 데이터, Argo·UI·MCP, 고객 빌드를 역할에 따라 배치한다. build taint를 유지하고, 고객 앱은 운영 클러스터 밖의 AWS/GCP runtime에서 실행한다. Pod 수만으로 과부하를 판단하지 않고 실행 중인 Pod와 완료 이력을 나누어 CPU·메모리·재시작·노드 pressure를 함께 읽는다. 예상하지 않은 OOM/restart는 0회를 목표로 한다. 변경 후 30분 동안 5분 평균 CPU 70%와 메모리 working set 80%를 넘는 노드가 있으면 신규 동시 실행을 늘리지 않고 해당 작업과 자원을 먼저 확인한다.

현재 API는 단일 writer, 보관 operation 최대 500개, 동시 배포 3개다. 500개 operation은 500개 동시 앱 부하 검증을 뜻하지 않는다. 고객 단일 노드·replica1은 HA가 아니다. 노드를 늘릴 때 provider target/NEG, NodePort의 Local/Cluster 정책, 실제 Ready EndpointSlice, Pod 배치, 볼륨 이동을 함께 검증한다. 새 노드를 만들었다는 사실만으로 분산 배치 완료로 보고하지 않는다.

## 검증과 복구

1. 동일 공개 조회 경로를 변경 전후 측정하고 표본·오류·p50/p95/최대값을 남긴다. 단일 요청 시간과 부하 시험을 구분한다.
2. source commit, CI run/attempt, image digest, GitOps commit, Argo revision, Pod imageID/Ready, 공개 HTTPS 응답을 한 배포에 묶는다. Argo의 과거 Healthy 문자열과 현재 클러스터 접근성을 구분한다.
3. `kubectl`의 Node/Pod/Service/EndpointSlice와 provider LB/NEG를 대조한다. controller 로그의 전체 비교 시간을 Git fetch 시간으로 단정하지 않는다.
4. 회복 불가한 작업은 blocked 상태로 드러내고 자동 재시도하지 않는다. 기존 operation/run 조회 없이 새 배포를 중복 제출하지 않는다.
5. API SQLite는 PVC의 파일이며 휘발성 메모리 DB가 아니다. 다만 local-path는 노드에 묶이므로 PV Retain과 외부 복구본을 함께 유지한다. 30분 간격 백업으로 RPO 여유를 두되 실패 시 마지막 성공 객체의 나이를 기준으로 목표 미달을 기록한다.
6. 복원은 DB·key·source·config를 함께 검증한다. 온라인 백업은 DB별 일관성이며 전체 클라우드 작업의 원자적 snapshot은 아니다. 계획된 이전은 writer를 멈추고 최종 복구본을 만든다. 실제 노드 복구와 서비스 재개를 측정하기 전에는 RTO 달성을 주장하지 않는다.

추가 모니터링 스택 없이 API telemetry, GitHub job timestamp, Argo 로그, CRI stats, cloud metrics, S3 업로드 영수증을 사용한다. 운영 구간이 충분히 쌓이면 목표를 실측 분포와 사용자 영향에 맞춰 조정한다.
