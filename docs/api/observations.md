# 제품 배포 관측

`GET /api/v1/deployments/{id}`는 영속 배포 기록과 `observation`을 함께 반환한다.
`observation.deployment_id/target_id/app`은 조회한 기록과 동일하다. 새로운 실행이나
배포 재시도는 하지 않는다. `ci.images`는 해당 run의 검증된 게시 receipt에서 읽은
이미지 digest 참조이고, `source_digest`는 접수한 입력 snapshot의 SHA-256이다.
원문 소스, Prometheus 주소, 쿼리, 자격증명은 응답하지 않는다.

대시보드는 15초마다 조회하며 완료된 배포도 관측을 계속한다. 재접속 시 브라우저에
저장한 deployment ID를 다시 조회한다. 영속 실행 결과의 성공과 현재 운영 상태는 별개다.
현재 HTTP probe 실패로 과거 배포 완료 기록을 실패로 바꾸지 않는다. 새로고침은 POST를 하지 않는다.

## 설정

운영자가 [product.example.json](../../observability/product.example.json)의 형식으로
소유자 전용 0600 파일을 만들고 API에 `RAILSHOT_OBSERVER_CONFIG` 절대 경로를 지정한다.
파일은 요청마다 다시 읽으므로 새 target/app 등록은 원자적 파일 교체로 반영할 수 있다.
각 행은 정확한 target/app, 앱 전용 namespace, node/cluster scrape instance, Blackbox의
정확한 probe URL을 묶는다. 미등록 앱의 요청은 외부 질의를 하지 않는다.

Prometheus 접근은 관리망에서 API 노드에만 허용한다. 기본 Compose의 loopback 바인딩을
그대로 사용하면 별도의 인증된 터널이 필요하다. 직접 사설 bind를 선택하면 SG/방화벽을
API 노드 송신 주소에만 제한한다. 공개 Prometheus/Grafana/Blackbox 포트를 만들지 않는다.
API 컨테이너에 파일을 읽기 전용 마운트하는 배포 설정은 플랫폼 담당이 적용한다.

기존 관측 렌더러는 30초마다 node/cluster/http를 수집한다. Pod 수에는 새로 허용한
`kube_pod_status_phase`가 필요하다. 기존 exporter에는 렌더된 allowlist 갱신이 필요하다.
CPU/메모리는 노드 전체의 사용률이며 특정 앱의 소비량이 아니다. Pod는 앱 namespace의
Running phase 개수이며 readiness를 의미하지 않는다. HTTP 1은 관측 위치의 2xx 응답이고,
0은 probe 실패다. 앱 본문/배포 버전 확인은 CD의 `public_http` receipt가 담당한다.

## 현재성 및 실패

각 metric에는 `state`, `value`, `observed_at`, `scope`가 있다. `checked_at`은 API가
조회한 시각이고, `observed_at`은 Prometheus `timestamp()`로 읽은 실제 샘플 시각이다.
instant query 평가 시각을 수집 시각으로 사용하지 않는다.

| state | 의미 |
| --- | --- |
| ready | 수집 UP, 필요한 샘플 존재, 90초 이내. HTTP value=0일 수 있음 |
| not_configured | API 관측 파일 연결 전 |
| unsupported | 정확한 target/app의 관측 등록 없음 |
| unavailable | 파일 변경 오류, timeout, 응답 오류 또는 모호한 시계열 |
| collection_failed | scrape의 up=0 |
| no_data | 필요한 시계열/값/시각 없음 |
| stale | 샘플이 90초 초과 또는 미래 시각 |

ready 이외에는 value=null이다. 실패 응답으로 이전 정상값을 재사용하지 않는다.
브라우저도 90초 후 표시값을 만료시키며, API 조회 실패 시 마지막 기록임을 표시한다.
쿼리는 고정된 세 묶음만 사용하며 각각 5초 timeout/64KiB 응답 한도를 가진다.
CPU rate에는 두 번 이상의 scrape가 필요하다. namespace 삭제 후 무자료를 0 Pod로 추정하지 않는다.

`작업 단계`, `환경 상태`, `HTTP 검증 기록`은 구조화된 기록이며 raw 앱 로그가 아니다.
실제 CI 로그는 해당 run의 GitHub Actions 링크로 이동한다. Loki/SSE/추적 프레임워크는 추가하지 않는다.

## 확인

`npm test --prefix apps/api --workspaces=false`와 `npm test --prefix ci/browser`에서
binding, 오래된 샘플, 수집 실패, HTTP 실패, reload, URL 노출 조건을 검사한다.
이 검사는 외부 서비스를 모의한 계약 검사이며 실제 클라우드 수집/배포 증거와 구분한다.

샘플 시각 근거: [Prometheus timestamp 함수](https://prometheus.io/docs/prometheus/latest/querying/functions/#timestamp),
[HTTP query API](https://prometheus.io/docs/prometheus/latest/querying/api/#instant-queries).
