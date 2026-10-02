# 제품 배포 관측

`GET /api/v1/targets/{id}/observations`는 배포 이력 없이 등록 환경을 관측한다.
`GET /api/v1/targets`의 공용 또는 현재 세션 소유 대상만 허용하며 다른 세션·미등록 대상은
Prometheus 접속 전에 404다. `deployment_id=null`, `environment_id`는 세션 환경 ID 또는 공용 대상의 null이다.
`app`은 등록 앱이며 앱 바인딩이 없으면 null이다. 이때 노드만 조회하고 Pod·HTTP는 unsupported다.
provider는 운영자 매핑이나 저장된 환경 profile에서 읽으며 ID 문자열로 추측하지 않는다.
대상 목록의 `runtime=unknown`은 정적 메타데이터다. observation의 `runtime.status/observed_at`은
`metrics.node_up.state/observed_at`과 같으며 node exporter 수집 상태를 뜻한다. 앱 준비 상태나 배포 성공을 뜻하지 않는다.

노드 지표는 `node_up`, `cpu_percent`, `memory_percent`, `disk_percent`,
`network_receive_bytes_per_second`, `network_transmit_bytes_per_second`다.
CPU·메모리는 노드 전체 사용률, 디스크는 루트 `/` 파일시스템의 사용률이다(여러 series면 최대 사용률).
네트워크는 lo를 제외한 인터페이스의 2분 평균 bytes/s 합계다. 가상 인터페이스도 포함될 수 있어
외부 회선 트래픽으로 해석하지 않는다. 기존 exporter 기본 설정에는 netdev가 없으므로 해당
collector가 실제 활성화되어 샘플을 수집하기 전에는 `no_data/null`이다. 정상 0 또는 시계열을 합성하지 않는다.
노드·Pod·HTTP 질의의 부분 실패는 다른 질의의 현재 값을 지우지 않는다.

`GET /api/v1/deployments/{id}`는 영속 배포 기록과 `observation`을 함께 반환한다.
`observation.deployment_id/target_id/app`은 조회한 기록과 동일하다. 새로운 실행이나
배포 재시도는 하지 않는다. `ci.images`는 해당 run의 검증된 게시 receipt에서 읽은
이미지 digest 참조이고, `source_digest`는 접수한 입력 snapshot의 SHA-256이다.
원문 소스, Prometheus 주소, 쿼리, 자격증명은 응답하지 않는다.

대시보드는 15초마다 조회하며 완료된 배포도 관측을 계속한다. 재접속 시 세션에 속한
서버 배포 내역에서 최근 deployment ID를 다시 조회한다. 영속 실행 결과의 성공과 현재 운영 상태는 별개다.
현재 HTTP probe 실패로 과거 배포 완료 기록을 실패로 바꾸지 않는다. 새로고침은 POST를 하지 않는다.

## 설정

운영자가 [product.example.json](../../observability/product.example.json)의 형식으로
소유자 전용 0600 파일을 만들고 API에 `RAILSHOT_OBSERVER_CONFIG` 절대 경로를 지정한다.
파일은 요청마다 다시 읽으므로 새 target/app 등록은 원자적 파일 교체로 반영할 수 있다.
앱 관측 행은 정확한 target/app, 앱 namespace, node/cluster scrape instance, Blackbox의
정확한 probe URL을 묶는다. 노드만 관측하는 행에는 `target_id`, `prometheus_url`,
`node_instance`만 필요하다. 이 행은 app/namespace/probe_url을 생략하며 cluster_instance는
선택 사항이다. 샘플 앱을 만들거나 삭제한 앱의 probe를 되살릴 필요가 없다.
정확한 앱 행이 있으면 우선 사용하고, 없으면 명시적으로 등록된 노드 행으로 노드만 조회한다.
이때 Pod·HTTP는 unsupported/null이며 다른 앱의 행을 대신 사용하지 않는다.
노드 행도 없는 미등록 앱의 요청은 외부 질의를 하지 않는다.

Prometheus 접근은 관리망에서 API 노드에만 허용한다. 기본 Compose의 loopback 바인딩을
그대로 사용하면 별도의 인증된 터널이 필요하다. 직접 사설 bind를 선택하면 SG/방화벽을
API 노드 송신 주소에만 제한한다. 공개 Prometheus/Grafana/Blackbox 포트를 만들지 않는다.
API 컨테이너에 파일을 읽기 전용 마운트하는 배포 설정은 플랫폼 담당이 적용한다.

기존 관측 렌더러는 30초마다 node/cluster/http를 수집한다. Pod 수에는 새로 허용한
`kube_pod_status_phase`와 `kube_pod_labels`가 필요하다. 기존 manifest의
`app.kubernetes.io/name`과 `railshot.io/target` 두 라벨만 수집하며 정확한 앱/대상의 Pod로 제한한다. 기존 exporter에는 렌더된 allowlist 갱신이 필요하다.
CPU/메모리는 노드 전체의 사용률이며 특정 앱의 소비량이 아니다. Pod는 namespace와 두 앱/대상 라벨이 모두 일치하는
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
쿼리는 최대 세 묶음만 사용하며 각각 5초 timeout/64KiB 응답 한도를 가진다.
CPU rate에는 두 번 이상의 scrape가 필요하다. namespace 삭제 후 무자료를 0 Pod로 추정하지 않는다.

`작업 단계`, `환경 상태`, `HTTP 검증 기록`은 구조화된 기록이며 raw 앱 로그가 아니다.
실제 CI 로그는 해당 run의 GitHub Actions 링크로 이동한다. Loki/SSE/추적 프레임워크는 추가하지 않는다.

## 확인

`npm test --prefix apps/api --workspaces=false`와 `npm test --prefix ci/browser`에서
binding, 오래된 샘플, 수집 실패, HTTP 실패, reload, URL 노출 조건을 검사한다.
이 검사는 외부 서비스를 모의한 계약 검사이며 실제 클라우드 수집/배포 증거와 구분한다.

샘플 시각 근거: [Prometheus timestamp 함수](https://prometheus.io/docs/prometheus/latest/querying/functions/#timestamp),
[HTTP query API](https://prometheus.io/docs/prometheus/latest/querying/api/#instant-queries).

## CI 진단과 앱 로그

CI의 `steps[].tasks`는 조회한 GitHub job의 내부 단계와 결과다. 알려진 플랫폼 단계 이름만 반환하며 임의 단계 이름은 번호로 표시한다. 실패한 현재 attempt의 `loop-N` artifact는 run ID, source SHA, 개수·크기·만료를 확인한 뒤 `ci.diagnostics`로 축약한다. 원문 오류, SDK 세션, 비밀 경로는 공개하지 않는다. `repair_scope`, `agent_attempts`, `changed_file_count`는 실제 기록에서 읽는다. `changed_files`는 공개해도 되는 정해진 패키징 파일 이름만 포함한다.

`NO_TESTS` 등 차단은 `blocked`, 결과 유실은 `unknown`을 보존한다. unknown은 새 실행 접수를 계속 막으며 자동 재시도하지 않는다. 예전 실패 기록은 상세 GET에서 진단만 보강할 수 있다. 진단 자료를 읽지 못한 경우 30초 이후 재조회하며 CI·CD를 다시 제출하지 않는다. 배포 내역에서 연 모니터에도 실패 요약과 GitHub Actions 링크를 표시한다.

`GET /api/v1/deployments/{id}/logs`는 해당 세션 소유의 배포에서 최근 앱 로그를 읽는다. 서버의 CD 설정과 저장된 검증 receipt를 재사용하고, 현재 Argo revision·이미지·Deployment→ReplicaSet→Pod 소유권을 대조한다. 같은 앱에 새 CD 작업이 시작되면 이전 기록은 `superseded`로 막는다. 실제 앱 적용 전은 `not_deployed`, 설정 없음은 `not_configured`, 조회 실패는 `unavailable`, 출력 없음은 `no_data`다. HTTP 오류가 없는 빈 결과를 수집 성공 로그로 꾸미지 않는다.

최대 세 컨테이너, 각각 최근 100줄, 전체 32 KiB로 제한하며 tail만 조회한다. 요청에서 namespace, Pod, 명령, 주소를 받지 않는다. 기존 namespace Role에 `pods/log:get`만 추가하고, 제품 API에는 등록된 클러스터 Secret 한 개의 `get`만 허용한다. 인증 정보는 서버 내부에서만 사용한다. 흔한 자격 문자열을 가리고 UI는 textContent로 표시하지만, 임의 앱이 출력한 모든 비밀을 탐지한다고 보장하지 않는다. 앱 로그 탭을 열었을 때만 조회하고 기존 15초 갱신과 연결한다.
