# Railshot · 지환 담당 feature

이 브랜치는 류지환 담당 CI·CSP·네트워크와 공통 인터페이스 연결부를 관리합니다. 테스트용 전체 조립본은 같은 저장소의 `integration/team-assembly-20261002` 브랜치에 있습니다. 다른 팀원의 코드는 통합 브랜치에서 확인합니다.

- `ci/`: 기본 검사, 제한된 AI 수정과 재검사, 검증 이미지의 동일성 보존 및 게시
- `infrastructure/terraform/`: CSP host·CI worker·선택형 AWS edge 구성
- `infrastructure/providers/terraform_tools/`: Terraform 운영 지원 도구
- `infrastructure/ansible/ci.yml`: CI worker 설치 계약
- `infrastructure/ansible/{run.py,api.py,transport.py}`: JSON·HTTP 실행 계약, 대상 검증, 영속 요청·잠금과 SSM/IAP 관리 연결
- `infrastructure/ansible/{guest.yml,runtime.yml}`: 팀 guest 검사·runtime bootstrap 호출과 결과 확인을 위한 공통 wrapper
- `infrastructure/ansible/control.sh`: 고객 target과 분리된 운영 K3s·Argo 설치 보조
- `gitops/`: 검증된 CI 게시 결과의 선언 생성, 기존 Argo 등록·sync와 증거 확인을 연결하는 공통 adapter
- `contracts/`, `examples/ansible/`, `docs/api/ansible.md`: 공통 요청 계약·자격 없는 예제·호출 규약
- `docs/api/ci-publication.md`: 게시 산출물과 API/CD 소비 계약
- [실가동 진행과 남은 일](docs/integration/ci-activation.md)

현재 기본 경로는 GitHub Actions와 GHCR입니다. CodeBuild는 기존 선택 구현으로 보존하며 이번 활성화에는 사용하지 않습니다. workflow는 private apps 저장소에 설치하는 템플릿이며 이 저장소에서 자동 실행되지 않습니다.

`Agents.md`의 합의 구조와 담당 범위를 따릅니다. 고객 UI/API·OpenStack Provider·고객 K3s/Cilium runtime·DB 구현과 팀 CD 구현은 각 담당자가 관리합니다. 이 브랜치의 HTTP API는 운영자용 Ansible 실행 경계이며 제품 `apps/api`와 구분합니다. Argo 연결부는 기존 Argo의 선언과 상태를 사용하며 별도 CD 엔진을 구현하지 않습니다. 팀원 `apps/`, `deployment/`, Ansible guest 검사 본문은 개인 feature에 복사하지 않습니다.

## 실행에 필요한 통합 코드

이 브랜치만으로 CI·Terraform 도구·공통 계약의 오프라인 검사를 실행할 수 있습니다. Ansible의 실제 `guest.check`·`runtime.install`은 팀 코드를 포함한 통합 checkout에서 실행합니다. 현재 연결 기준은 통합 commit [`0ab08b2971ea437d90e65a0349db0b39683411e1`](https://github.com/Jasmin-Softbank/Railshot/tree/0ab08b2971ea437d90e65a0349db0b39683411e1)이며, 그 checkout 전체를 고정해 사용합니다. 개인 clone에 팀 파일을 덧복사하거나 bootstrap 이름만 바꾸어 설치하지 않습니다.

- `infrastructure/ansible/tasks/guest-checks.yml`: 김정빈 담당 공통 guest 검사
- `deployment/scripts/common.sh`, `deployment/bootstrap/{preflight,install-k3s,health}.sh`, `deployment/cilium/{install,health}.sh`: 이승민 담당 runtime bootstrap·health 검사

개인 feature의 Ansible·GitOps 테스트는 실행기와 native 호출을 모의 실행합니다. 테스트 통과와 `--validate-only`는 위 팀 파일의 설치·원격 실행·대상 Ready를 증명하지 않습니다. 실제 CI 게시·cloud 배포·외부 HTTP 상태는 별도의 통합 진행 기록에서 구분합니다. 실제 PoC workload 선언은 통합 브랜치에만 둡니다.
