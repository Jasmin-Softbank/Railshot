# API–CI 연결 및 로컬 검사

2026-10-02 통합 worktree의 담당 소스 조립 뒤 API/CI 연결을 검사했다. 원문 선택은 source-map.json, 이동만 한 첫 변경은 relocation-map.json, API·화면·CI의 최종 변경 파일 해시는 api-ci-changes.json에 구분했다.

## 연결한 범위

- 진기 API/CLI/MCP와 정적 UI를 유지하고 asset 경로를 연결했다. localhost bind/Host/Origin 보호를 유지했다.
- source 등록 commit SHA와 운영자 target ID를 dispatch에 전달한다. 앱/tenant 이름을 기존 CI 검사와 맞췄다. workflow checkout/source SHA 또는 운영자 target이 다르면 모델이나 게시 전에 차단한다.
- 상태 조회는 loop/release 두 job과 실제 release producer attempt를 읽는다. 고유 artifact ID, run SHA, source/target, 원본 evidence 파일 해시를 검사하고 published 상태를 반환한다. 기존 rendered alias와 URL 성공 가정은 제거했다.
- 기존 네 publisher evidence 파일의 bytes를 바꾸지 않고 평탄한 디렉터리에 복사하며 handoff.json으로 run/attempt/source/target을 기록한다. 앱 배포, Git push, Argo 관측, 외부 URL 확인은 수행하지 않는다.

## 결과

| 로컬 검사 | 명령 | 결과 |
| --- | --- | --- |
| Node/API·업로드·published 조회 | `cd apps/api && npm test` | 16 PASS |
| CI policy/publisher 및 공통 Python helper | `python -m unittest discover -s ci/scripts -p 'test_*.py'` | 34 PASS |
| gate | `python -m unittest discover -s ci/scripts/gate -p 'test_*.py'` | 127 PASS |
| AI 작업 제어 | `python -m unittest discover -s ci/scripts/loop -p 'test_*.py'` | 31 PASS |
| SDK 경계 | `python -m unittest discover -s ci/scripts/runner -p 'test_*.py'` | 21 PASS |
| intake | `python -m unittest discover -s ci/scripts/poc -p 'test_*.py'` | 1 PASS |

합계 **230 PASS**다. Python interpreter는 기존 `/tmp/railshot-ci-check-venv/bin/python`을 사용했다. Node lockfile 의존성은 offline cache가 없어 `npm ci --ignore-scripts --no-audit --no-fund`로 설치했다. package 설치와 로컬 mock 검사를 실제 cloud/model/registry 실행으로 취급하지 않는다.

Node 검사에는 실제 CI Python receipt writer를 호출하고 그 파일을 Node API reader가 읽는 계약 검사가 포함된다. 소스 변경 없는 재제출, latest release 실패, 이전 producer attempt 재사용, artifact 만료·중복·누락, source/target/attempt/파일 변조를 검사했다. 실제 workflow shell의 source/target guard도 stub Git으로 실행했다. 저장된 mock fixture만 검사하고 지나간 것이 아니다.

추가로 source-map의 선택 파일 254개가 모두 존재함을 확인했고 API/CI 수정·신규 파일의 whitespace 검사를 통과했다. CLI `--target` 값 누락은 source 읽기/원격 호출 전에 오류로 거부한다. syntax 검사도 통과했다.

원격 GitHub dispatch/artifact readback, 이미지 build/push/pull, Provider VM 생성, SSH/Ansible 설치, Argo 적용, 실제 외부 HTTP는 이번 검증에서 수행하지 않았다. Provider·Ansible·CD·Terraform 담당의 별도 검사 결과는 총괄 보고에서 따로 합산한다. `published`를 `deployed`나 CD 소비 완료로 바꾸지 않는다.
