# 중앙 Vault·복구 보관 로컬 실기 기록

2026-10-04, macOS의 로컬 Docker에서 합성 자료만 사용했습니다. 운영 VM·클라우드·실제 자격에 접속하지 않았으며 변경을 커밋하거나 푸시하지 않았습니다.

## 실행한 코드와 이미지

- 시험: `deployment/scripts/tests/live_control_vault.py`
- 중앙·환경·복원 Vault: `hashicorp/vault:2.1.1@sha256:47f14a6acb98f48d798a07df7c83f23a6e636e1cf724c5f8ff165cb32667a1e2`
- 복구 서비스: `deployment/control-recovery.Dockerfile`을 로컬 빌드한 `railshot-custody-local-test:20261004`
- 최종 복구 서비스 이미지 ID: `sha256:8183c9364f499637330a061994cc09ba5d07379211b99afc4fb2a667757da9aa`
- Python 기반 이미지: `python:3.13.3-slim@sha256:56a11364ffe0fee3bd60af6d6d5209eba8a99c2c16dc4c7c5861dc06261503cc`
- 최종 실제 출력 로그: `/private/tmp/railshot-vault-e2e-20261004/recovery-cli-live.log` (로컬 임시 파일이며 저장소 산출물은 아닙니다)

최종 시험은 서비스 소스 마운트를 실행하는 대신 **빌드 이미지 안의 `/opt/railshot/recovery_service.py`**를 실행했습니다. 등록·관리·백업 도구는 같은 Python 이미지에서 저장소의 읽기 전용 소스 마운트로 실행했습니다. 합성 Vault는 개발 모드가 아닌 TLS·Raft 영구 저장 모드입니다.

## 결과

| 실제 검사 | 결과 |
|---|---|
| env-a/env-b에 독립 runtime·manager 인증서 및 개인키, Transit token 발급 | 통과 |
| env-a token으로 env-b Transit encrypt 호출 | 403 차단 |
| 환경 Vault의 `token="env://VAULT_TOKEN"`, 별도 중앙 CA 사용, 초기 자동 unseal 및 컨테이너 재시작 뒤 저장 값 조회 | 통과 |
| 실제 상호 TLS로 초기화 자료 custody 저장, 다른 환경 차단, custody 재시작 후 영수증 조회, 암호문 DB 백업·복원 검증 | 통과 |
| 실제 Raft snapshot과 custody DB로 `control_backup.py` 묶음 생성·검증·신규 경로 복원 | 통과 |
| 별도 신규 Vault 초기화 후 snapshot-force 복원, 원래 중앙 share로 unseal, 이전 Transit 암호문 복호화 | 통과 |
| 새 환경 token 발급·유효성 확인, 이전 token 유지 확인 후 명시적 accessor 폐기 | 통과 |
| 중앙 초기 root token 폐기 후 별도 operator 및 새 환경 token 조회 | 통과 |
| operator periodic token의 renew-self 호출 | 통과 |

기계 판독 가능한 최종 결과는 다음과 같습니다.

```json
{
  "independent_environment_keys_and_certificates": true,
  "transit_cross_environment_denied": true,
  "child_auto_unseal_restart_and_data_retention": true,
  "custody_mtls_restart_and_restore": true,
  "backup_bundle_create_verify_restore": true,
  "central_snapshot_restore_decrypts_previous_ciphertext": true,
  "explicit_rotation_preserves_then_revokes_previous_token": true,
  "operator_and_child_survive_parent_root_revocation": true,
  "operator_periodic_token_renewal": true
}
```

별도 `test_control*.py` 회귀는 **14/14** 통과했습니다. 백업의 모든 key ID 보존과 과거 키 누락 거부, 암호문·snapshot 변조 및 경로 탈출 거부, 다른 환경 token accessor 폐기 거부, 관리 자격 누락 시 재사용 거부, root와 독립인 operator token 생성, 상태 검사, 제한된 실행 도우미의 권한 하향 검사를 포함합니다. 복구 서비스의 동시 재시도·저장 실패·잘못된 키·상호 TLS 검사는 `test_recovery_service.py`, 등록 동시성은 `test_recovery_enroll.py`에 별도로 있습니다. 마지막 shell 구문 검사와 `git diff --check`도 통과했습니다.

## 시험 중 확인하고 반영한 사항

- 초기 시험은 HTTP 초기화 응답을 수작업으로 CLI 모양으로 변환하며 unseal 숫자를 0/0으로 설정했습니다. 이후 Ubuntu 실기에서 실제 Vault 2.1.1 CLI가 빈 unseal 배열과 1/1을 출력함을 확인하여 보관 스키마의 호환성 결함을 수정했습니다. 현재 하네스는 자식 컨테이너에서 실제 `vault operator init -format=json`을 실행하고 그 JSON 원본을 그대로 보관합니다. 이 경로로 9개 실기 항목을 다시 통과했으며, 위 이미지 ID와 로그는 그 재실행 결과입니다. 단위 검사에서는 1/1과 명시적 빈 배열·복구 5/3 조건, 기존 0/0 호환, 잘못된 숫자 쌍과 누락 자료 거부를 확인했습니다.

- Python 3.13의 엄격한 TLS 검증에 맞추어 발급 인증서에 키 식별 확장을 추가했습니다.
- 중앙 감사 로그 경로 `/vault/data/audit.log`가 실제 영구 볼륨에 있도록 시험 구성을 수정했습니다.
- 직접 operator token 생성에는 `no_parent=true`를 사용했습니다. 부모 root 폐기 후 token이 유지되는 것을 확인했습니다.
- Docker의 임의 호스트 포트가 재시작 뒤 바뀔 수 있어 시험이 실제 포트를 다시 조회하도록 수정했습니다. 이 문제를 Vault 자동 unseal 실패로 결론 내리지 않았습니다.
- 초기화 재개용 HTTP 암호화 전달은 root token만 포함합니다. 복구 조각 5개는 중앙 운영자의 명시적 로컬 내보내기 대상입니다.

## 자원 정리

각 시험은 무작위 접미사를 붙인 `railshot-drill-*` 컨테이너와 네트워크만 생성했습니다. `finally`에서 생성한 컨테이너·네트워크·합성 개인키가 있는 임시 디렉터리를 제거했습니다. 최종 실행 뒤 해당 접두사의 컨테이너와 네트워크가 **0개**임을 별도 조회로 확인했습니다. 기존 사용자 컨테이너·네트워크는 변경하지 않았습니다. 재검증을 위한 로컬 빌드 이미지와 공식 다운로드 이미지는 남겨 두었습니다.

## 검증하지 않은 범위와 운영상 남는 절차

**실행하지 않은 검증:** 전체 Compose 설정을 그대로 실행한 시험(특히 Vault UID 100/GID 1000과 읽기 전용 root filesystem 조합), 실제 Linux sudo 권한 하향, systemd timer 설치·시간 경과 실행, PGP 초기화 CLI와 실제 운영자 수신키, 중앙 호스트 전체 재부팅, Kubernetes·External Secrets·클라우드 전체 배포, 외부 방화벽·DNS·운영 인증서 경로는 시험하지 않았습니다. 실기 컨테이너는 UID 0에서 capability 제거와 no-new-privileges를 적용했고 서비스 연결은 localhost 임시 포트로 제한했습니다. 권한 하향은 별도 모의 회귀 범위입니다.

**구현에 포함되지 않은 자동화:** 인증서 자동 갱신, 새 seal token을 실제 대상 Secret으로 전환하는 작업, 백업의 서버 밖 전송, 독립 관리자 다인 승인, 외부 메시지 알림은 구현하지 않았습니다. 중앙 Vault는 수동 unseal 절차를 유지합니다. 제공 timer는 중앙 token 갱신과 제한된 상태 검사 및 journal 실패 신호를 수행하도록 작성했지만 이 macOS 시험에서 설치·실행하지 않았습니다.

백업 wrapper의 복원은 새 경로에 검증된 입력을 준비합니다. 실제 Vault restore·unseal은 별도 명시적 절차이며 이번 실기 시험에서 그 절차를 별도 컨테이너로 수행했습니다. manifest 해시와 snapshot 구조 검사만으로 실제 복원이 보장된다고 주장하지 않습니다. 자세한 설치·교체·복원 절차는 [중앙 운영 문서](../architecture/shared-vault-control-plane.md), 보관 권한과 키 경계는 [복구 API 계약](../api/recovery.md)을 따릅니다.
