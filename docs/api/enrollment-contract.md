# 서비스 등록 규격 초안

이 문서는 클라이언트 구현에 필요한 **서버 규격 초안**입니다. 실제 서버 구현 또는 호환성 검증을 의미하지 않습니다. 서버 주소는 설치 설정의 service_url로 입력합니다.

클라이언트는 일회용 등록 키와 로컬에서 생성한 WireGuard 공개키, 영속화된 요청 식별자를 HTTPS 등록 요청에 전달합니다. 정확한 경로·본문 키·응답 검증은 `deployment/bootstrap/client_setup/enrollment.py`와 일치시켜 서버에서 구현해야 합니다. OpenStack 암호·토큰·개인키는 포함하지 않습니다.

응답 필드: `node_id`, `address`, `server_public_key`, `endpoint`, `allowed_ips`, `probe_url`. 허용 경로는 터널 내부 개별 호스트 주소(/32 또는 /128)이며 probe_url은 해당 경로에 속한 IP의 HTTPS 주소여야 합니다. 서버 인증서는 그 IP를 인증해야 합니다.

서버는 등록 키를 고객/등록 권한/만료 시점에 묶고, 한 설치 요청에서만 소비해야 합니다. 같은 request_id와 공개키로 응답을 재요청하면 기존 성공 결과를 반환해야 합니다. 다른 공개키나 요청 식별자로 소비된 키를 재사용하면 거부해야 합니다. 등록 결과 유실 복구 정책 없이는 안전한 자동 재시도가 완성되지 않습니다.

등록 취소, 고객별 WireGuard 격리, 서버의 키 보관과 감사 기록은 서버 측 책임이며 이번 구현에 포함하지 않습니다.

요청: `POST {service_url}/v1/enrollments`, `Authorization: Bearer <일회용 키>`, `Idempotency-Key: <request_id>`, JSON 본문 `{ "public_key": "...", "request_id": "..." }`. 성공 응답은 HTTP 200과 위 여섯 필드의 JSON이며, 리다이렉트와 추가 필드는 거부합니다.
