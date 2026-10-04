# 배포와 운영 인사이트 데모

상태: integration에서 분기한 구현. 운영 배포·관측 등록은 별도이며, 이 변경은 기존 CI/CD workflow,
빌드/복구 루프, 이미지 게시, Terraform 실행, GitOps/Argo 로직을 수정하지 않는다.

## 데모 경험과 범위

1. `examples/insights-demo`의 내용을 앱 저장소 루트로 배포한다. 기존 배포 흐름을 사용한다.
2. 배포 내역의 **운영 인사이트**에서 배포 결과, AI 실제 적용/검증 이력, 현재 HTTP, 요청 추이를 본다.
3. 앱 주소를 QR로 공유하거나 직접 열어 정상 요청을 만든다. QR 생성기는 이 기능에 포함하지 않는다.
4. 앱의 **시험용 오류/지연** 버튼은 해당 요청에만 503/800ms를 만든다. 시험이라는 사실을 설명한다.
5. 연결한 AI에 “이 배포의 최근 15분 요청과 현재 상태, AI가 수정한 내용을 보여줘”라고 요청한다.
6. MCP Apps 지원 호스트에서는 같은 카드/차트가 대화 안에 열린다. 미지원 호스트는 요약과 대시보드 링크를 사용한다.
7. **보고서 저장**은 현재 조회 결과를 Markdown으로 내려받는다. 별도 문서화 SaaS는 필요 없다.

앱 계측은 이 데모 앱에만 들어간다. 모든 업로드 소스에 자동 삽입하지 않는다. 데이터가 없는 과거
배포에 가짜 이력이나 AI 수정 기록을 만들지 않는다. 앱 요청은 방문자/PV가 아니며 실제 앱까지
도달한 요청이다. 네트워크·LB에서 거절된 요청은 이 지표에 없으므로 외부 Blackbox 검사를 함께 본다.
AI 활동은 CI가 실제로 발생시킨 기록이 있을 때만 표시한다. 이 데모 앱은 유효한 Dockerfile이 있어
그대로 배포하면 AI가 호출되지 않을 수 있다. 자동 복구 데모에는 별도의, 사전 검증된 실패 소스를
사용하고 모델이 호출되지 않았을 때 호출한 것처럼 시연하지 않는다.

## 구성과 책임

- `apps/api/src/traffic.js`: 등록된 앱/대상으로만 Prometheus 조회. 고정 지표와 15/60분 구간, 최대 121점.
- `apps/api/src/metrics.js`: 기존 관측 설정 검증과 크기·시간 제한이 있는 Prometheus 전송 재사용.
- `apps/api/src/insights.js`: 기존 세션 권한을 가진 배포/이벤트/진단/로그 조회 결과 투영. 실행 권한 없음.
- `apps/dashboard/src/insights-view.js`: 대시보드와 MCP App이 공유하는 화면/보고서 생성. 외부 문자열은 텍스트로 표시.
- `apps/agent/src/mcp-tools.js`: 기존 OAuth/세션 MCP를 확장. 차트 좌표는 `_meta.overview`, 모델에는 요약 제공.
- `apps/agent/ui/insights-app.js`: 공식 MCP Apps SDK로 호스트 연결. 화면의 30초 갱신은 모델을 재호출하지 않는다.
- `observability/register.py`: 기존 등록에 선택적인 `traffic_port` 추가. 기존 앱 등록은 그대로 동작한다.

GET `/api/v1/deployments/{id}/insights?minutes=15`는 배포 기록, AI 활동, 현재 관측,
트래픽 요약/시계열을 반환한다. `minutes`는 15 또는 60이다.
GET `/api/v1/deployments/{id}/evidence?area=build`는 선택 영역의 근거를 반환한다.
`area`는 build/deploy/runtime이다. build 로그는 최대 2개/각 2000 bytes이며 runtime은 기존
최대 3개 컨테이너/전체 32KiB 조회 계약을 유지한다. POST나 임의 PromQL/URL은 받지 않는다.

MCP 도구 `get_app_overview(deployment_id, minutes)`와
`get_deployment_evidence(deployment_id, area)`는 이 API를 사용한다.
기존 배포 도구와 인증 동작은 변경하지 않는다. 외부 도구에도 웹과 동일한 소유권 검사가 적용된다.
배포 ID가 필요하면 기존 배포 접수 결과에서 사용한다. 임의 앱 이름으로 다른 세션을 탐색하지 않는다.

## 운영 연결 순서

1. 이 브랜치의 API·대시보드·기존 MCP 이미지를 기존 플랫폼 릴리스 방법으로 반영한다.
   이번 변경은 릴리스 파이프라인을 수정하거나 자동 운영 배포하지 않는다.
2. 데모 앱을 기존 파이프라인으로 배포한다. 앱 8080 포트와 `/health`를 사용한다.
   별도 9400 포트는 메트릭 전용이며 공개 앱 HTTP에는 `/metrics`가 없다.
3. `examples/insights-demo/metrics-access.example.yaml`의 namespace/target/CIDR을 실제 등록값으로
   치환한다. 미사용 NodePort를 선택하고, 클라우드/호스트 방화벽을 관측 VM 송신 IP `/32`로 제한한다.
   기존 Cilium/네트워크 정책과 함께 접근이 되는지 실제 확인한다. 이 샘플 자체를 바로 적용하지 않는다.
4. 기존 `observability/register.py` 운영자 등록 요청의 app/namespace/probe_url에
   `"traffic_port": 30940`과 제품 배포의 `application_id`를 추가한다.
   request의 `target_id`는 descriptor의 환경 ID(예: `k3s-aws`)를 유지한다.
   `application_id`는 배포 응답의 앱 대상 ID이며 namespace와 함께 운영자가 확인한다. 노드 전용 등록에서는 사용할 수 없다.
   `--config`, `--request`, `--out`은 기존 등록 절차의 실제 운영자 파일을 사용한다.
   등록기는 descriptor의 주소와 포트로 `traffic_instance`를 만들고 다음 job을 렌더링한다.

```yaml
job_name: app_traffic
metrics_path: /metrics
static_configs:
  - targets: ["<registered-node>:30940"]
    labels:
      app: insights-demo
      target_id: <exact-app-target-id>
```

5. 기존 API의 `RAILSHOT_OBSERVER_PRODUCT_FILE`(또는 `RAILSHOT_OBSERVER_CONFIG`)에 등록 결과가
   반영되는지 확인한다. 다른 파일/메트릭 서버 주소를 MCP 입력으로 받지 않는다.
6. Prometheus에서 `up{job="app_traffic"}=1`과 HTTP probe의 최신 샘플을 확인한다.
   최소 두 번의 scrape 이후 요청을 발생시킨다. 수집은 운영 30초, 로컬 리허설 5초다.
7. 기존 `/mcp` 연결에 새 도구가 보이는지 확인한다. 외부 클라이언트가 도구 목록을 캐시하면 재연결한다.
   브라우저 대시보드 링크는 같은 권한의 웹 세션이 필요하다. 링크 자체에는 토큰이 없다.

등록은 `traffic_instance`를 기존 앱에 추가할 수 있고, 동일 요청은 같은 설정을 유지한다.
이미 지정된 주소를 다른 주소로 바꾸는 동작은 차단한다. 삭제/주소 이전은 운영자 등록 정리 절차로 처리한다.
자동 수집 대상 탐색이나 인프라 권한 확대는 포함하지 않는다.

### 기존 서비스 보존과 적용 순서

API의 새 조회 코드를 먼저 릴리스한 뒤 새 앱의 관측 등록을 추가한다. `application_id`가 없는
기존 등록은 기존 방식으로 조회한다. 환경 ID·앱 이름·application ID가 모두 일치해야 새 앱의
Pod/HTTP/트래픽을 읽으며, 미등록 앱에는 기존처럼 환경의 노드 관측만 제공한다.
기존 행을 다른 application ID나 수집 주소로 덮어쓰는 요청은 거부한다.

운영자 요청의 추가 필드는 다음과 같다. 나머지 필드는 기존 운영자 등록 파일을 사용한다.
임의로 환경 descriptor의 target ID를 앱 ID로 교체하지 않는다.

```json
{
  "target_id": "k3s-aws",
  "app": "insights-demo",
  "application_id": "app-fea3bad6f677e1eeb0282ad3",
  "namespace": "app-fea3bad6f677e1eeb0282ad3",
  "traffic_port": 30940
}
```

- 앱 Pod가 Ready이고 기존 공개 주소가 응답하는지 먼저 확인한다. 배포 자체가 대기 중이면
  관측 설정으로 해결하거나 성공으로 표시하지 않는다.
- 메트릭 Service/NetworkPolicy는 기존 앱 리소스와 이름을 달리하여 추가한다. 샘플 정책은
  기존 앱의 8080 ingress 허용 정책과 함께 사용한다. 기존 정책이 없는 Pod에 그대로 적용하면
  일반 요청까지 차단할 수 있으므로 적용 전에 selector와 정책을 확인한다.
- 미사용 NodePort와 옵저버 송신 주소를 확인하고 SG/호스트 방화벽은 해당 포트의 송신자를
  옵저버로 한정한다. 기존 앱 포트/정책을 교체하지 않는다.
- 기존 Prometheus 설정·등록 파일을 보존하고 기존 등록 경로로 병합한다. Promtool 검증 후
  재시작 대신 설정 reload를 사용한다. 전후 기존 수집 대상의 `up`을 비교한다.
- 최소 두 번 수집한 뒤 정상·시험 오류·지연 요청을 보내 MCP의 실제 값으로 검증한다.
  롤백은 이번 앱의 수집 항목·관측 Service/정책·SG 규칙만 제거한다. 공유 옵저버는 삭제하지 않는다.

이 변경 자체는 운영 수집 항목이나 네트워크를 자동 수정하지 않으며 CI/CD 흐름에도 손대지 않는다.

## 해석 기준

- 트래픽 scope는 `app_target`이다. 같은 앱의 여러 배포가 조회 구간에 포함될 수 있다.
  그래프의 배포 선은 HTTP 검증 완료 시각이며 트래픽 전체가 그 리비전에서 발생했다는 뜻은 아니다.
- `requests`는 Prometheus `increase`의 추정 구간 합으로 소수가 나올 수 있다. 방문자 수가 아니다.
- 5xx는 전체 앱 요청 대비 비율. 트래픽 0이면 오류율·p95는 null이다.
- p95는 histogram 기반 추정이며 적은 표본으로 성능을 단정하지 않는다.
- 90초 초과 샘플, 수집 실패, 미등록, 표본 없음은 각각 구분한다. 없는 자료를 0으로 채우지 않는다.
- API 요청 시각과 실제 표본 시각을 구분한다. 배포 성공 이력을 현재 HTTP 실패 때문에 변경하지 않는다.
- CPU/메모리는 기존과 같이 노드 전체 값이다. 앱별 사용량이라고 설명하지 않는다.

## 로컬 검증

```sh
npm ci --ignore-scripts
npm ci --prefix examples/insights-demo --ignore-scripts
npm ci --prefix ci/browser --ignore-scripts
npm run check:insights --workspace @railshot/agent
node --test apps/api/test/metrics.test.js apps/api/test/insights.test.js
npm test --workspace @railshot/agent
npm test --prefix examples/insights-demo
node --test ci/browser/insights.test.mjs
# ci/requirements-test.txt가 설치된 Python 환경에서 실행
python3 -m unittest discover -s observability -p 'test_register.py'
npm run build
```

실제 앱/Prometheus 수집 리허설(운영 연결 불필요):

```sh
docker compose -p railshot-insights-check -f examples/insights-demo/compose.yml up -d --build
RAILSHOT_LIVE_TRAFFIC_TEST=1 node --test apps/api/test/traffic-live.test.js
docker compose -p railshot-insights-check -f examples/insights-demo/compose.yml down
```

앱 `http://127.0.0.1:18080`, Prometheus `http://127.0.0.1:19090`은 로컬에만 바인딩한다.
실제 수집 결과는 `outputs/insights-live-traffic.json`, 화면 테스트 캡처는 `outputs/insights-*.png`에 기록한다.
화면 테스트는 명시적인 가상 데이터이며 실제 수집 리허설과 구분한다.

MCP App은 self-contained HTML을 `apps/agent/src/insights-app.html`로 커밋한다.
기존 MCP 이미지가 src 디렉터리를 복사하므로 릴리스 빌드 단계를 추가할 필요가 없다.
UI/SDK 변경 후 `npm run build:insights --workspace @railshot/agent`로 재생성하고
`check:insights`로 재현성을 검사한다. 외부 CDN이나 임의 HTML 생성은 사용하지 않는다.

외부 AI 제품별 MCP Apps 지원 및 운영 계정 로그인은 최종 데모 호스트에서 확인해야 한다.
이 저장소의 자동 검증은 SDK 도구·리소스 프로토콜, 공식 AppBridge와 iframe의 초기화·갱신, 브라우저 화면 동작을 검증한다.

## 이번 작업에서 확인한 결과

- 핵심 API 조회/관측 테스트 6개, MCP 테스트 11개, 새 화면/공식 AppBridge 테스트 2개 통과.
- 대시보드 프로덕션 빌드와 커밋된 MCP HTML 재현성 검사 통과.
- 실제 로컬 앱과 Prometheus에서 정상 12회·503 3회·지연 1회 요청을 수집했다.
  5xx 비율 18.75%, 요청 추정 합계 약 16.21회와 응답 시간/차트 표본을 확인했다.
- 전체 API 검사에서 진단 테스트 5개가 실패했다. 수정 전 integration에서도 동일한
  `diagnostic-source.json` 누락으로 재현했다. 전체 브라우저 검사 역시 기존 환경/소스 접수
  테스트가 실패했고, 수정 전 integration에서도 재현을 확인했다.
  전체 테스트가 모두 통과한 상태는 아니며, 요청에 따라 추가 전체 회귀 검사는 중단했다.
- 이번 UI 추가로 필요한 정적 파일 제공 목록은 테스트 서버에도 반영했고 배포 내역 화면
  테스트 4개를 다시 통과했다.

위 항목은 최초 구현 시점의 검증 기록이다. 이후 운영 확인은 아래에 별도로 기록한다.

## 2026-10-04 운영 데모 연결 기록

- MCP로 접수한 배포 `a9d5dc75-0c43-416e-91d4-08a3ced86724`는 성공했다.
  공개 주소는 `https://insights-demo-f7edb701418c.railshot.io/`이다.
- `k3s-aws`의 namespace `app-fea3bad6f677e1eeb0282ad3`에 샘플의
  `insights-demo-metrics` Service(30940 → 9400)와 `insights-demo-observer` 정책을 추가했다.
  기존 8080 Service/NetworkPolicy/Deployment는 변경하지 않았다.
- 현재 수집기는 control 노드 `172.31.0.172`의 기존 Prometheus다. 별도 관측 VM/Grafana는 만들지 않았다.
  AWS ingress 규칙 `sgr-0d0ad769600813416`은 control SG만 허용하며,
  egress 규칙 `sgr-0819eab713dd7d73a`는 `172.31.13.147/32:30940`만 허용한다.
- Prometheus의 기존 9개 수집 대상을 보존하고 `app_traffic`과 앱 HTTP probe를 추가했다.
  Promtool 검증 후 SIGHUP으로 reload했으며 컨테이너 시작 시각이 유지됐다.
  기존 9개와 추가 2개 수집 대상 모두 `up`이었다. HTTP probe 성공값도 1이었다.
- 정상 요청 8회·시험 오류 2회·지연 요청 1회를 보냈다. 수집된 누적 카운터는 기존 정상 1회를
  포함해 2xx=10, 5xx=2였다. 이는 방문자 수나 15분 increase 값이 아닌 원시 누적 카운터다.
- API의 기존 `product.json`/`desired.json` 3개 환경 행을 유지하고, `application_id`를 가진
  새 앱 행 1개를 추가했다. API는 파일을 매번 읽는다. 운영 API에는 본 브랜치의 조회 수정이
  아직 릴리스되지 않았으므로 수집 성공을 MCP/대시보드 표시 완료로 설명하면 안 된다.
- 핵심 회귀 검사: API 관측/인사이트 6개, 등록기 15개 통과. 전체 테스트는 반복하지 않았다.

운영 원본 백업은 control의
`/home/railshot-operator/observer-shared/prometheus.before-insights-demo-20261004.json`,
API PVC의 `/var/lib/railshot/state/observer/product.before-insights-demo-20261004.json` 및
`desired.before-insights-demo-20261004.json`에 있다. 이후 다른 등록이 생겼다면 백업 전체를
덮어쓰지 말고 이번 앱 항목만 제거한다. SG 회수는 위 rule ID로 한정한다.
