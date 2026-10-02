# 통합 브랜치와 PR 병합 규칙

2026-10-02 합의: 현재 `integration/team-assembly-20261002`를 Gitflow의 `develop` 역할로 사용한다. 별도 `develop` 브랜치를 만들거나 `main`에 바로 구현을 모으지 않는다.

| 브랜치 | 역할 |
|---|---|
| `main` | 검증된 릴리스 반영. 이번 feature 통합 대상이 아님 |
| `integration/team-assembly-20261002` | 팀 구현을 모으고 연결부를 검증하는 개발 기준 |
| `feature/*` | 담당 구현의 원본. 담당 범위의 변경을 유지 |
| `codex/integrate-*` | 충돌 해결·연결부 보완이 필요한 경우 사용하는 임시 PR 브랜치 |

## 병합 절차

1. 원격 feature의 최신 SHA와 담당 범위를 확인한다. 팀원의 구현 의도를 유지하고 충돌·입출력 계약·실행 의존성을 점검한다.
2. 충돌이 없는 feature PR은 integration으로 merge commit을 만든다. 충돌이나 연결부 수정이 필요하면 최신 integration에서 임시 브랜치를 만들고 `git merge --no-ff <feature SHA>`로 원본 이력을 포함한다.
3. 임시 브랜치에서 충돌과 필요한 공통 연결부만 해결한다. 기존의 담당 코드만 담은 개인 feature에 통합본 전체를 역병합하지 않는다.
4. 바뀐 연결부의 테스트와 빌드를 실행하고 PR 본문에 원본 SHA·해결 내용·검증 범위를 적는다. 기존 클라우드 배포 결과를 새 merge commit의 배포 결과로 취급하지 않는다.
5. PR을 integration으로 merge한다. squash·rebase·force push 없이 feature 이력을 보존하고, 원격 HEAD와 포함된 feature SHA를 다시 확인한다.

Vercel Preview는 별도 웹 배포 연동이다. 런타임·Ansible·CI 검증과 구분하며, 프로젝트 설정·로그가 확인되지 않은 Vercel 실패를 팀 런타임 테스트 실패로 기록하지 않는다. 현재 강제 필수 검사 설정은 없으며 위 절차는 이번 통합의 운영 규칙이다.

## 지환 feature 이력 연결

기준 integration은 `adda5c7532c22da95e938a3cf1a7ba4c05e4ca47`, 개인 feature는 `efa4d7c599bf9af512112a353b3d5324c036db68`이다. 개인 feature의 210개 파일 중 208개는 통합본과 내용·mode가 동일하다. 나머지는 개인 범위를 설명하는 README와 Ansible 문서 상단 안내뿐이다.

이미 반영된 CI·CSP·네트워크·Ansible·GitOps 구현을 다시 복사하지 않고 merge로 계보를 연결했다. README와 Ansible 문서는 통합 범위의 설명을 유지했다. 통합본에만 있는 팀 구현과 실제 앱 선언은 보존했다.
