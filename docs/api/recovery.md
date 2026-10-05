# 중앙 복구 보관 서비스 계약

구현: `deployment/scripts/recovery_service.py`, 로컬 등록: `recovery_enroll.py`, 클라이언트: `recovery_escrow.py`. 이 문서는 로컬 구현 계약이며 실제 운영 배포 완료를 뜻하지 않습니다. OpenAPI 명세는 [recovery.openapi.json](recovery.openapi.json)입니다.

## 신뢰 경계와 키 소유

중앙 서비스 한 대가 여러 환경의 복구 자료를 보관합니다. 환경마다 Vault는 독립적이며, 클라이언트 인증서는 중앙의 root 전용 registry가 환경 및 역할에 결합합니다. 인증서의 CN이나 요청 본문 `environment_id`만으로 권한을 부여하지 않습니다. 서버는 신뢰 CA가 서명한 인증서를 TLS 계층에서 검증한 다음 DER 인증서의 SHA-256 지문을 registry에서 찾습니다. 지문은 비밀이 아닙니다.

| 역할 | 저장 | 암호화 전달 |
|---|---|---|
| `runtime` | 자신 환경의 초기화 및 배포 AppRole | 자신 환경의 초기화 재개 자료 |
| `manager` | 자신 환경의 배포 AppRole만 | 자신 환경의 배포 AppRole 인계 |

환경 와일드카드는 없습니다. manager 인증서도 환경별로 따로 발급합니다. registry는 매 요청마다 최대 64 KiB를 안전하게 읽으며, 파일 접근·권한·구문 오류면 이전 내용을 재사용하지 않고 503으로 차단합니다. 등록 해제는 이후 요청에 즉시 적용됩니다. TLS 인증서 체인은 연결 시점에 검사합니다.

서로 다른 키를 구분해야 합니다.

- **중앙 보관 암호화 키**: 무작위 256비트 AES 키입니다. 서비스에 root 전용 파일로 별도 마운트합니다. 환경 클러스터로 전송하지 않습니다. 암호화 DB와 다른 관리 경계에 백업해야 합니다.
- **중앙 클라이언트 CA 개인키**: 등록 도구만 사용합니다. HTTP 서비스에 필요하지 않으며 환경으로 보내지 않습니다.
- **환경별 runtime/manager 개인키**: 등록 도구가 서로 다른 RSA 3072비트 키를 생성합니다. 해당 역할이 실행되는 호스트에만 최소 범위로 전달합니다.
- **Vault 자동 봉인 해제·프로젝트 암호화 키**: 보관 암호화 키와 별개입니다. 이 서비스의 영수증은 Vault 저장소나 자동 봉인 해제 서비스의 백업을 대신하지 않습니다.

초기화 JSON에는 5개 복구 조각 전체가 함께 들어갑니다. 이는 **독립적인 다인 승인 보관을 구현한 것이 아닙니다.** 중앙 root와 보관 키 소유자는 5개 조각을 모두 복호화할 수 있습니다. 독립 승인자가 요구되면 별도 관리 경계와 승인 절차가 필요합니다. 초기 root 토큰도 보관되지만 Vault가 폐기한 토큰은 복구해도 다시 유효해지지 않습니다.

## HTTP 계약

모든 경로는 인증서 상호 검증 TLS(mTLS)를 요구하며 TLS 1.2 이상입니다. 평문 비밀을 반환하는 HTTP 경로는 없습니다. 본문 최대 64 KiB, 작업 스레드 최대 16개, 소켓 무응답 제한 10초이며 대기열은 16개입니다. 공개 인터넷에 직접 노출하지 않고 관리 네트워크와 방화벽으로 접속 범위를 제한합니다. 과부하 연결은 응답 없이 닫힐 수 있습니다.

응답은 `Cache-Control: no-store`, 서버가 매번 생성한 `X-Request-ID`, `Content-Type: application/json`을 포함합니다. 실패는 공통 `error` 객체의 `code/message/request_id/retryable/outcome_unknown` 다섯 필드입니다. 원문 오류·토큰·개인키·요청 본문은 응답·로그에 출력하지 않습니다.

### 저장

`POST /api/v1/escrows`

```json
{"version":1,"environment_id":"env001","kind":"vault-initialization","operation_id":"bootstrap-stable-id","material":{"root_token":"<sensitive>","recovery_keys_b64":["<share1>","<share2>","<share3>","<share4>","<share5>"]}}
```

`kind`는 `vault-initialization`, `vault-delivery-approle` 중 하나입니다. `operation_id`는 같은 작업의 재시도에서 반드시 재사용합니다. 생략하면 이전 클라이언트와의 호환을 위해 `initial`이 되므로 새 클라이언트는 명시적으로 지정해야 합니다. ID는 영숫자로 시작하고 영숫자·`.`·`_`·`-`로 구성된 1–128자입니다.

초기화 material은 Vault CLI JSON 형식입니다. 필수 `root_token`, `recovery_keys_b64`에 더해 `recovery_keys_hex`, `recovery_keys_shares`, `recovery_keys_threshold`, `unseal_keys_b64`, `unseal_keys_hex`, `unseal_shares`, `unseal_threshold`만 허용합니다. base64 복구 조각은 서로 다른 33바이트 값 5개이며 hex가 있으면 동일 값이어야 합니다. shares/threshold가 있으면 각각 5/3이어야 하고 unseal 배열은 비어 있어야 합니다. unseal 숫자는 기존 입력의 0/0 또는 실제 Vault CLI의 1/1 쌍을 허용합니다. 1/1일 때는 두 빈 unseal 배열과 recovery shares/threshold 5/3이 모두 명시되어야 합니다. 이는 자동 unseal의 저장 키를 1/1로 표시하는 [Vault 2.1.1 CLI 형식](https://github.com/hashicorp/vault/blob/v2.1.1/command/operator_init.go#L524-L528)이며, 복구 조각 검증을 완화하지 않습니다. root 토큰은 영숫자·`.`·`_`·`-` 8–4096자입니다. 배포 AppRole material은 `role_id`, `secret_id` 두 필드이며 같은 문자 범위 8–1024자입니다. 임의 필드·중복 JSON 키·잘못된 자료형은 거부합니다.

암호문과 영수증을 함께 SQLite 트랜잭션으로 저장한 후에만 201을 반환합니다. `Location`은 `/api/v1/escrows/{receipt_id}`이며 본문은 정확히 다음 두 필드입니다.

```json
{"stored":true,"receipt_id":"opaque-receipt-id"}
```

같은 `(환경, kind, operation_id)`와 정규화된 동일 내용은 200과 기존 영수증을 반환합니다. 내용이 다르면 `409 IDEMPOTENCY_CONFLICT`입니다. 비교는 기존 암호문을 복호화한 뒤 수행하며 비밀의 평문 해시를 저장하거나 노출하지 않습니다. 영수증 자동 만료·삭제 API는 없습니다. SQLite `BEGIN IMMEDIATE`, unique 제약 및 프로세스 내부 잠금으로 동시 요청도 한 건으로 수렴합니다. rollback journal과 `synchronous=EXTRA`로 파일 및 디렉터리 동기화를 수행합니다. 저장·commit 오류에는 성공 영수증을 반환하지 않습니다.

### 응답 유실 조회

`GET /api/v1/escrows?kind=vault-initialization&operation_id=bootstrap-stable-id`

`GET /api/v1/escrows/{receipt_id}`

자신의 환경에서만 영수증 두 필드를 반환합니다. 없는 자원과 다른 환경의 자원은 모두 404입니다. 조회 전에 해당 암호문 인증·복호화 가능 여부도 확인합니다. 목록 열람과 다른 환경 지정 query는 지원하지 않습니다. 조회는 저장 내용 일치의 증거가 아닙니다. 클라이언트에 원본 자료가 남아 있으면 동일 operation POST로 확인하고, 재개 자료가 필요하면 아래 권한 제한 전달을 사용합니다.

### 수신 인증서에 묶인 암호화 전달

`POST /api/v1/escrows/{receipt_id}/exports`

본문은 `{"purpose":"initialization-resume"}` 또는 `{"purpose":"delivery-handoff"}`입니다. 전자는 runtime/초기화 root 토큰만, 후자는 manager/AppRole 자료에만 허용됩니다. 복구 조각은 HTTP 암호화 전달에도 포함하지 않으며 중앙 운영자의 명시적 로컬 내보내기만 허용합니다. 다른 환경 영수증, 잘못된 역할, 목적과 자료 종류의 불일치를 거부합니다.

응답 메타데이터는 `version:1`, `environment_id`, `kind`, `operation_id`, `receipt_id`, `purpose`, `recipient_fingerprint`입니다. 추가 세 필드 `wrapped_key`, `nonce`, `ciphertext`는 표준 base64입니다. 매 전달마다 새 AES-256-GCM 키와 12바이트 nonce를 생성하고, AppRole material 또는 초기화 `{"root_token":"..."}` 객체를 정렬된 JSON으로 암호화합니다. 메타데이터 전체를 `sort_keys=True, separators=(",", ":")`로 정규화한 바이트를 인증 부가 데이터(AAD)로 사용합니다. AES 키는 **현재 TLS 클라이언트 인증서**의 RSA 공개키에 OAEP-SHA256/MGF1-SHA256, label=None으로 감쌉니다. RSA 2048비트 미만 또는 다른 종류의 키는 전달을 거부합니다. 저장용 중앙 키는 이 응답에 포함되지 않습니다.

클라이언트는 복호화 전에 환경·종류·작업·영수증·목적·자기 인증서 지문 전부를 예상 값과 비교해야 합니다. 복호화 결과는 내부 제한 파이프 또는 root 전용 파일 인계에만 사용하고 제품 응답·대시보드·로그에 내보내지 않습니다. 이 전달 방식은 수신 환경 root의 자료 접근을 허용하는 명시적 신뢰 경계입니다.

### 상태와 감사

`GET /api/v1/healths`는 등록 인증서에만 `{"status":"ready"}`를 반환합니다. DB 읽기와 최신 기록 한 건의 인증 복호화를 검사하며, 전체 무결성 검사를 뜻하지 않습니다. 서버 시작과 `verify` 명령은 전체 기록을 검사합니다. `ready` 로컬 명령은 동일한 제한된 DB 검사이며 HTTP 프로세스가 응답 중인지까지 증명하지 않습니다. 최초 환경 등록 전에는 이 로컬 검사를 사용할 수 있습니다.

감사 기록은 구조화된 stderr에 동작·상태·서버 요청 ID·인증서 지문·환경·영수증만 기록합니다. raw URL, 본문, 비밀, 원문 예외는 기록하지 않습니다. 운영자는 이 로그를 별도의 접근 제한된 감사 저장소로 전달해야 합니다. 로그 전달 자체의 영속성은 이 프로그램이 보장하지 않습니다.

## 실행·등록·인계

Python 및 [requirements-recovery.txt](../../deployment/scripts/requirements-recovery.txt)의 `cryptography`가 필요합니다. 자체 암호 알고리즘을 구현하지 않고 [PyCA AESGCM](https://cryptography.io/en/stable/hazmat/primitives/aead/)을 사용합니다. 데이터 디렉터리는 root 0700, 키·registry·자격 파일은 root 0600 또는 0400입니다. CLI는 root 실행만 허용합니다.

keyring 파일:

```json
{"version":1,"active_key_id":"custody-1","keys":{"custody-1":"<base64-encoded-random-32-byte-key>"}}
```

registry 파일:

```json
{"version":1,"clients":{"<lowercase-sha256-of-der-cert>":{"environment_id":"env001","role":"runtime"}}}
```

```sh
python recovery_service.py --data-dir /var/lib/railshot-recovery --keyring /run/secrets/custody-keyring.json serve --listen 0.0.0.0 --port 9443 --registry /run/secrets/recovery-clients.json --cert /run/secrets/server.crt --key /run/secrets/server.key --ca /run/secrets/client-ca.crt
python recovery_enroll.py issue --environment-id env001 --role both --ca-file /root/custody-ca.crt --ca-key-file /root/custody-ca.key --registry /etc/railshot/recovery-clients.json --output-dir /root/enroll/env001
```

출력 디렉터리는 미리 root 0700으로 만듭니다. 등록 도구는 runtime과 manager의 독립 RSA 키·인증서, 공개 `ca.crt`를 root 0600 파일로 배타 생성합니다. 기존 파일은 덮어쓰지 않습니다. registry는 별도 파일 잠금과 임시 파일 fsync→원자적 교체→디렉터리 fsync로 갱신하므로 다른 환경의 동시 등록을 잃지 않습니다. 출력은 경로·지문 메타데이터만 포함합니다. CA private key는 중앙에 유지하고 runtime 자격만 환경에, manager 자격은 관리 호스트에 인계합니다.

인증서 갱신은 새로운 출력 디렉터리에 발급하고 인계·접속 성공을 확인한 후 이전 지문을 명시하여 폐기합니다. 이전 자격은 자동 삭제되지 않습니다. registry 쓰기 결과가 불확실하면 새 개인키 파일도 삭제하지 않으므로 지문을 확인하여 조정할 수 있습니다. 재발급을 반복하기 전에 남은 파일과 registry를 점검합니다.

```sh
python recovery_enroll.py revoke --environment-id env001 --role runtime --fingerprint <old-cert-fingerprint> --registry /etc/railshot/recovery-clients.json
```

로컬 평문 내보내기는 환경·영수증·종류를 모두 명시해야 합니다.

```sh
python recovery_service.py --data-dir /var/lib/railshot-recovery --keyring /run/secrets/custody-keyring.json export --environment-id env001 --receipt-id <receipt> --material delivery-approle --output /root/private/approle.json
```

`root-token`, `recovery-share --share-index 1`도 선택할 수 있습니다. 조각은 1–5 중 하나만 내보내며 전체 자료 일괄 출력 기능은 없습니다. 대상 디렉터리는 root 0700, 출력은 `O_EXCL`·0600·fsync를 사용하여 기존 파일·심볼릭 링크를 거부합니다. 명령행 인자로 비밀 값을 받지 않고 stdout에도 출력하지 않습니다. 배포 AppRole JSON은 관리 측 도구가 검증한 다음 정해진 `token_file`에 안전하게 인계해야 합니다.

## 백업·복원·키 교체

`backup --output /root/backup/custody.sqlite3`는 SQLite backup API로 일관된 **암호문 DB만** 배타 생성합니다. 실행 중 서버와 병행할 수 있습니다. 다른 키·CA·registry·인증서는 해당 소유 경계에서 별도로 백업합니다. 복구 키를 암호문 DB와 같은 백업에 넣으면 관리 경계가 합쳐집니다. 저장 DB에는 환경·종류·작업·영수증 메타데이터가 평문으로 있으므로 백업 접근도 제한합니다.

복원은 서비스를 멈추고 새 root 0700 디렉터리에 백업을 `custody.sqlite3`/0600으로 복사한 뒤, 별도로 복구한 keyring을 사용하여 `verify`를 실행합니다. 전체 기록 인증 복호화와 SQLite 무결성 검사가 통과해야 서비스를 시작합니다. 원본 디렉터리를 덮어쓰지 않아 복원 실패 시 원본을 보존합니다. keyring이 없거나 잘못된 키, 변조 자료는 실패합니다. 빈 DB 신규 생성은 serve만 허용하고 verify/backup/rotate는 기존 DB가 필요합니다.

키 교체 절차는 서비스 중지 → 기존 키와 새 키를 함께 담은 keyring의 active_key_id 변경 → `rotate` → `verify` → 새 키만 담은 keyring으로 다시 `verify` → 서비스 시작입니다. `rotate`는 전체 자료를 한 트랜잭션으로 다시 암호화합니다. 서버와 rotate는 동일 파일의 프로세스 잠금을 잡아 동시에 실행되지 않습니다. 이전 백업을 해독할 수 있도록 이전 키는 별도의 보관 정책에 따라 유지해야 합니다. 자동 키 생성·외부 키 보관소 연동·키 보관자 승인 절차는 이 서비스에 포함되지 않습니다.

암호화는 실행 중 root 침해, 메모리·swap·core dump 탈취 또는 삭제·오래된 DB로의 전체 롤백을 방지하지 않습니다. 중앙 호스트와 별도 키 보관, 감사 로그의 변경 방지, 독립 백업 복원 훈련이 필요합니다. 단일 호스트 장애 시 서비스는 중단될 수 있습니다. 로컬 합성 인증서 시험은 실제 클라우드 장애 복구 시험을 대신하지 않습니다.
