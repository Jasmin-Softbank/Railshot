# 폐기한 WireGuard 서비스 등록 규격

2026-10-03 결정에 따라 WireGuard 등록 경로를 제거했습니다. `POST /v1/enrollments`, 일회용 등록 키, WireGuard 공개키·터널 응답은 현재 클라이언트 계약과 선택 항목이 아닙니다. 해당 서버 기능을 새로 구현하거나 이전 문서의 등록 명령을 실행하지 않습니다.

현재 [로컬 OpenStack 준비](../architecture/client-bootstrap.md)는 현장 관리자가 확보한 네트워크 경로에서 인증·조회·VM 접근 준비를 수행합니다. 기존 등록·자격증명·자원 기록은 자동 삭제하지 않으며, 설치 소유 WireGuard 연결의 해제만 지원합니다. Cloudflare 앱 Named Tunnel은 SSH 관리 경로를 대신하지 않습니다.
