# 후속 사안별 통합과 실제 자원 검증

2026-10-02. 진행 중인 다른 채팅의 컨테이너·제품 API 설계와 범위를 나누고, 원본 feature를 보존한 채 별도 feature PR을 integration에 병합한다. 실제 자원 적용은 사용자가 승인했으며 기존 서비스와 데이터에 영향을 주지 않는 전용 AWS 자원을 사용한다.

## 사안별 PR

| 사안 | PR | 확인 결과 |
|---|---|---|
| CI 부팅·GCP/Azure egress 회귀 검사 복원 | [#11](https://github.com/Jasmin-Softbank/Railshot/pull/11) | 최신 base의 필수 CI 통과, merge `b16ee2085668062f7d178bc55cf6e2b70433b8de` |
| 정빈 standalone Cilium 후속 이력과 공통 설치기 연결 | [#12](https://github.com/Jasmin-Softbank/Railshot/pull/12) | 원본 `80dae724` 포함, 최신 base의 필수 CI 통과, merge `cfa2fac8e4d20ce65853a9cacb4784864a80d35c` |
| 대시보드 실제 업로드·CI 상태 조회 | [#13](https://github.com/Jasmin-Softbank/Railshot/pull/13) | 최신 base의 필수 CI 통과, merge `6037d3a8e22cb6951436ea7f9d59582952c5b745` |
| Standalone runtime·관측 실자원 인수와 설치 안내 수정 | [#14](https://github.com/Jasmin-Softbank/Railshot/pull/14) | 최신 base의 필수 CI 통과, merge `42af8bde819b18dd03adf14ded719c642b83c81a` |
| K3s 외부 HA DB의 승인 profile 실행 | [#18](https://github.com/Jasmin-Softbank/Railshot/pull/18) | head `974c90e9`의 CI 8개와 실자원 인수·정리 통과, merge `08dc01ec51c98001a5cfc986ae3ae6539f938203` |

필수 검사는 GitHub Actions app의 `Railshot CI gate`이며 최신 integration 반영을 요구한다. 관리자 우회·force push·integration 삭제를 허용하지 않는다. 각 PR은 merge commit을 사용하며 담당자 feature를 삭제하거나 통합본으로 덮어쓰지 않는다.

회귀 검사 복원은 원래 코드가 잘못됐을 때 실패하는지까지 확인했다. CI systemd 의존성·실행 순서·timeout, HTTPS egress 허용·차단과 실제 apt source 변환에 9개 의도적 오류를 넣었고 각 검사가 실패했다. 정상본은 Ansible 30, GCP 11, Azure 2개 검사를 통과했다.

Cilium은 삭제된 `scripts/install-cilium.sh` 참조를 현재 `cilium/install.sh`로 연결하고 `scripts/common.sh`, `airgap/versions.json`을 함께 전달한다. 다른 설치 경로와 같은 `/run/railshot-deployment.lock`을 사용한다. Python 계약 검사 외 native Ansible syntax·렌더 검사도 CI에서 수행한다.

대시보드는 ZIP·폴더·공개 GitHub 소스를 서버의 등록 대상으로 제출한다. 선택적 운영자 토큰은 메모리와 인증 헤더에서만 사용한다. 조회 종료·오류·수동 중지·페이지 종료 시 타이머와 요청을 정리한다. `published`를 앱 배포 성공으로 표시하지 않는다. 다른 채팅의 PR #10 (`957afa2`)과 PR #13 (`201d244`)을 임시 checkout에서 결합했을 때 API 23개, 브라우저 2개, Vite build가 통과했다. README 충돌 외 소스 충돌은 없었으며 원본 작업 공간은 수정하지 않았다.

## 실제 자원 인수 검사

[관측 실측](observability-acceptance-20261002.md)은 완료했다. runtime 신규 설치·동일 재실행·identity 변경 차단, Prometheus 수집과 Grafana API, HTTP 성공/실패 구분, 수집 중단·잘못된 이미지의 장애 표시와 복구, 접근 차단을 확인했다. VM 2대와 EBS 2개·ENI 2개·SG를 정리하고 정확한 ID로 부재를 확인했다. 원본 증거 12개 SHA-256을 독립 대조했다. Grafana 브라우저 렌더는 검증하지 않았다.

DB는 [화균 원본 README](https://github.com/Jasmin-Softbank/Railshot/blob/67d19efc81b01b2a55b6dd54c198088b018bdd1f/README.md)와 [10/1 Slack 전사](https://softbankhackathon2026.slack.com/files/USLACKBOT/F0C63236BL1/___________________)의 2:45–2:47, 2:49:49–2:51:07, 2:53:19를 따른다. K3s에 포함되거나 가입하는 DB가 아니라 별도 VM의 PostgreSQL 16·Patroni·etcd·HAProxy 클러스터다. 전사문의 provider별 수량은 배치 예시이며 운영 기본값으로 고정하지 않는다.

전용 AWS 3대에 DB 2·DCS 3·proxy 1을 배치하고 네 번째 VM은 복구 전용으로 격리했다. [DB 실측 기록](database-acceptance-20261002.md)의 결과는 설치, HAProxy 경유 TLS SQL, 동기 복제, API 재전송·재시작의 기존 결과 보존, full backup, 격리 파일 복원 및 별도 WAL/SQL 비교 성공이다. 첫 etcd v3 gateway 실패와 unknown, 수동 재조정 근거도 보존했다. 이는 단일 AWS 거점의 최소 구성 인수이며 다중 거점 장애 내성 검증은 아니다.

09:59 UTC DB VM 4대 종료, EBS 7개·ENI 4개·SG 2개 제거와 로컬 API/실행기/SSM 터널 0을 확인했다. 실행 원장의 정확한 소유 ID만 사용했다. 원본 증거 25개 SHA-256을 대조했고, 실제 실행과 최종 PR의 DB 실행 파일 58개가 일치한다. 관측 검증과 합쳐 이번 작업에서 만든 VM 6대·EBS 9개·ENI 6개·SG 3개를 모두 정리했다. 기존 운영·화균 소유 VM은 정리 대상에 포함하지 않았다.

## 기존 서비스와 남은 경계

09:14와 09:56 UTC 확인: 기존 AWS/GCP fixture의 HTTPS `/health`는 모두 유효한 TLS로 200과 `{"status":"ready"}`를 반환했다. 기존 AWS 운영·고객 VM은 running, 기존 두 CI VM은 stopped를 유지했다. 이 읽기 검사는 변경 전후 운영 상태를 대조하기 위한 것이며 새 코드의 배포 성공으로 기록하지 않는다.

제품 API의 Provider→대상 등록→Ansible→CI/CD 전체 자동화, 유저·tenant 인가 및 서버 측 대상 선택은 다른 채팅의 설계 범위다. [#16](https://github.com/Jasmin-Softbank/Railshot/pull/16)은 해당 REST 규약·설계를 병합한 것이며 제품 구현 완료를 뜻하지 않는다. 컨테이너·CI 배치 [#10](https://github.com/Jasmin-Softbank/Railshot/pull/10)은 다른 진행 중 채팅이 담당한다. 운영 K3s Cilium 코드 [#17](https://github.com/Jasmin-Softbank/Railshot/pull/17)은 담당 채팅에서 병합했지만 기존 운영 VM의 CNI 전환 완료를 뜻하지 않는다. 이번 UI는 기존 CI 제출·조회 계약에 연결하며 그 후속 설계를 선점하지 않는다. 기본 단일 PostgreSQL 설치와 Proxmox 구현은 기존 담당 코드에 없으므로 지원으로 표시하지 않는다.
