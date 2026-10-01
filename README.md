# Railshot · 지환 담당 feature

이 브랜치는 류지환 담당 CI·CSP·네트워크 구현을 관리합니다. 테스트용 전체 조립본은 같은 저장소의 `integration/team-assembly-20261002` 브랜치에 있습니다. 다른 팀원의 코드는 통합 브랜치에서 확인합니다.

- `ci/`: 기본 검사, 제한된 AI 수정과 재검사, 검증 이미지의 동일성 보존 및 게시
- `infrastructure/terraform/`: CSP host·CI worker·선택형 AWS edge 구성
- `infrastructure/providers/terraform_tools/`: Terraform 운영 지원 도구
- `infrastructure/ansible/ci.yml`: CI worker 설치 계약
- `docs/api/ci-publication.md`: 게시 산출물과 API/CD 소비 계약
- [실가동 진행과 남은 일](docs/integration/ci-activation.md)

현재 기본 경로는 GitHub Actions와 GHCR입니다. CodeBuild는 기존 선택 구현으로 보존하며 이번 활성화에는 사용하지 않습니다. workflow는 private apps 저장소에 설치하는 템플릿이며 이 저장소에서 자동 실행되지 않습니다.

`Agents.md`의 합의 구조와 담당 범위를 따릅니다. 고객 UI/API·OpenStack Provider·K3s runtime·Argo·DB 구현은 각 담당자가 관리합니다. 개인 feature에는 해당 코드를 복사하지 않습니다.

실제 CI와 registry 게시, cloud 배포 상태는 진행 기록에서 구분합니다. 네트워크 확장안은 다른 채팅의 검토가 끝난 뒤 연결합니다.
