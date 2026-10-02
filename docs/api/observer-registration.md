# 공유 관측 서버 초기 설치와 자동 대상 등록

이 경로는 [runtime 등록](runtime-registration.md)의 native 실행기를 재사용한다.
앱마다 관측 VM을 생성하지 않는다. 최초 설치에서 기존 provider 실행기가 관측 VM 한 대의
descriptor와 SSH 등록을 만들고, 이후에는 같은 공유 서버에 scrape 대상만 추가한다.

## 최초 설치 파이프라인

플랫폼의 단일 executor가 기존 provider/billing 경로로 관측 VM을 준비한 다음 다음 명령을 실행한다.
새 VM 생성·비용 예약·SG/IAM 소유권은 기존 provider/profile 단계에 남는다. 이 명령은
이미 등록된 VM의 Docker/Compose와 관측 구성을 설치하며 VM을 추가로 생성하지 않는다.

```sh
python3 observability/bootstrap.py --config /private/observer-config.json --out /private/bootstrap.json
```

입력은 [registration.example.json](../../observability/registration.example.json)의 운영자 전용 0600 파일이다.
`observer_registry_file`은 기존 환경 worker가 만드는 v1 `targets` 레지스트리와 같은 형식이고,
`observer_target_id`가 실제 provider descriptor/SSH host key를 가리킨다. SSM/IAP forwarding과
엄격한 known-host 검사를 재사용한다. 공개 제품 입력에서 이 파일·주소·SSH 인자를 받지 않는다.

초기 provider/profile이 함께 준비할 네트워크는 다음과 같다.

- API/control의 사설 송신 주소만 observer의 TCP 9090 접근 허용.
- observer의 사설 송신 `/32`만 runtime의 두 exporter NodePort 접근 허용.
- AWS 신규 runtime에는 위 규칙의 등록된 SG를 기존 `additional_security_group_ids`로 연결.
- GCP는 같은 송신 범위의 등록된 방화벽/관리망 경로가 필요하다. 이 코드가 AWS SG를 GCP에 적용하지 않는다.
- Grafana 3000은 loopback, Blackbox는 Docker 내부이며 공개 관리 포트를 만들지 않는다.

이 네트워크/IAM 선언은 플랫폼 bootstrap의 책임이다. 소스에 entrypoint가 존재한다는 사실은
그 파이프라인이 원격에서 실행됐거나 새 SG가 준비됐다는 증거가 아니다.

`bootstrap.py`는 수집 대상이 비어 있는 구성으로 시작한다. 등록 전 앱에 대한 성공값을 만들지 않는다.
원격 출력 디렉터리의 `owner:lifecycle` marker가 일치해야 재실행할 수 있으며 기존 Grafana 비밀번호·
볼륨·수집 대상을 보존한다. `acceptance` 디렉터리를 `shared`로 채택하거나 종료 timer를 제거하지 않는다.
수명 만료값은 실제 provider 만료 정책과 일치해야 한다. bootstrap은 timer/리소스 만료를 연장하지 않는다.

## 새 runtime 등록

운영자 profile의 `registration.observability_config_file`에 위 파일을 설정하면 runtime registrar가
credential 등록 뒤, CI binding 전에 다음 native command를 호출한다.

```sh
python3 observability/register.py --config /private/observer-config.json \
  --request /private/environment/observability-request.json --out /private/environment/observability.json
```

request에는 `version,target_id,environment_id,app,namespace,node_ip,probe_url,registry_file,context`만 둔다.
registrar가 검증한 descriptor와 edge allocation에서 생성하며, 관측 helper가 target/private IP를 다시 대조한다.
노드만 등록할 때는 `app,namespace,probe_url`을 생략한다. 같은 target의 앱 행은 함께 보관하되 물리 자원 변경은 거부한다. 기존 NodePort나 관측 소유권이 다른
동명 Kubernetes 자원을 덮어쓰지 않는다.

앱을 포함한 등록도 독립적인 노드 행을 남기며, 앱 제거 후에도 런타임 연결 관측을 유지한다.
관리 소유권과 버전을 확인한 K3s에 경로 제한 `AuthenticationConfiguration`을 적용해 `/healthz`만
인증 없이 확인한다. 기존 노드 배포 잠금 아래 설정을 백업하고 필요한 경우 K3s를 재시작한다.
`/healthz=200`, 일반 API `401`, 기존 workload 식별자·설정 보존을 검증하고 실패 시 원복한다.
control/API 노드는 이 변경 대상이 아니다. 관측기에는 공개 CA 인증서와 정확한 TLS 서버 이름만
전달하며 앱 토큰·관리자 인증서는 복사하지 않는다. 기존 Blackbox가 `runtime_healthz` job으로
30초마다 검사하고, API의 `healthz_url` 바인딩으로 실제 샘플을 읽는다. 관리 API의 기존 네트워크
접근 제한을 유지하며 사용자 입력 URL이나 앱 URL로 이 검사를 대신하지 않는다.

native worker는 기존 Cilium의 NetworkPolicy를 사용하는 exporter manifest를 적용하고, 공유 Prometheus
설정에 기존 모든 대상을 보존하며 새 대상을 추가한 다음 SIGHUP으로 다시 읽게 한다. Promtool 검증이
실패하면 이전 파일을 복원한다. API의 `RAILSHOT_OBSERVER_CONFIG`는 `state_dir/product.json`을 가리킨다.
이 파일은 원자적으로 교체되며 API가 요청마다 읽기 때문에 API 재시작 없이 새 target/app을 관측한다.

플랫폼에서는 `state_dir`를 `/var/lib/railshot/state/` 하위에 둔다. 검증된 private import의
profiles → deployment → `observability_config_file`을 따라 bootstrap이 이 `product.json` 경로를
`railshot-environments` ConfigMap의 `observer_file`에 한 번 등록한다. API와 초기화 컨테이너는
optional `RAILSHOT_OBSERVER_PRODUCT_FILE`로 같은 경로를 받는다. 이후 초기화는 오래된 Secret의
`observer.json`을 다시 복사하지 않으며, 동적 파일이 없거나 잘못되면 이전 정상값으로 대체하지 않는다.
관측 등록 설정이 없는 기존 설치는 `RAILSHOT_OBSERVER_CONFIG`의 정적 파일을 계속 사용한다.
기존 ConfigMap에는 새 key만 추가할 수 있고, 이미 지정된 경로를 바꾸려면 운영자가 별도로 이행해야 한다.

하나의 파일 lock이 공유 대상 목록을 보호한다. 외부 변경 전에 `desired.json`과 unknown receipt를 저장한다.
collector 전송 실패 이후 다른 등록이 들어와도 이전 의도를 목록에서 지우지 않는다. unknown 작업의
자동 재실행은 제품 규약에 따라 금지하며 운영자가 동일 입력으로 인수·복구한다.

결과 `status=succeeded, registered=true, collection_state=pending`은 설정 연결 완료다. exporter Pod Ready,
실제 scrape/HTTP 성공, 대시보드 값 갱신은 [관측 API](observations.md)에서 별도로 확인한다.
운영자 파일이 만료되면 등록을 거부하고 관측 API도 현재 정상값을 반환하지 않는다.

## 수명과 제거 책임

`collector.id/role/lifecycle/expires_at`을 제품 observation에 포함하고 UI에서 공유 서버의 운영용/임시 인수용
구분과 만료 시각을 표시한다. 설정에 수명 정보가 없는 기존 서버는 미제공으로 표시한다.

2026-10-02의 `observer-ui-20261002`는 **임시 인수용**이며 22:43:38 KST 자동 terminate 예정이다.
이것은 상시 관측 서버 설치가 아니다. 소유 장부는 controller의
`/Users/mango/.local/share/railshot/observer-ui-20261002/resources.json`이다. 플랫폼 담당은 인수 종료 시
자신이 적용한 runtime/control SG 규칙과 exporter 리소스를 회수한다. 관측 담당은 자신이 만든 VM/SG/EBS
종료·잔존을 확인한다. 비용 lease 해제는 이관된 단일 billing 권위에서 플랫폼 담당과 조율한다.

상시 공유 observer의 재생성/제거는 최초 provider 작업의 owner/state로만 수행한다. 애플리케이션 하나를
제거한다고 공유 observer VM이나 다른 앱의 수집 정보를 삭제하지 않는다.

## 검사와 미검증 범위

로컬 검사는 초기 렌더 재실행과 비밀번호 보존, 임시→운영 전환 거부, target/물리 자원 충돌,
공유 목록 보존, collector 실패 이후 의도 보존, 패키징과 CI 경로 선택을 확인한다.
최초 VM 준비·bootstrap·신규 runtime 등록을 원격 단일 executor가 연속 수행한 결과는 별도 live receipt가 필요하다.
현재 임시 AWS VM에서 수집한 기존 fixture 메트릭은 이 신규 자동 등록 경로의 실행 증거가 아니다.
