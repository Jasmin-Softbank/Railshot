# 회의 원문과 설계 판단

이 폴더는 회의 원문과 통합 결정의 근거를 관리합니다. 2026-09-28, 09-29, 10-01의 자동 전사 전문과 허들 스레드를 날짜별 Markdown으로 보존했습니다. 전사는 발언자와 상대 시각을 유지하며, 원음과 별도로 대조한 자료는 아닙니다. 이번 조사에서는 설계·R&R·인증·Ansible·Argo·네트워크 관련 구간을 앞뒤 발언과 함께 읽었습니다.

| 날짜 | 원문 | 로컬 보존 | 이번에 적용한 판단 |
|---|---|---|---|
| 9/28 | [허들](https://softbankhackathon2026.slack.com/archives/C0C1FHVQES3/p1790599194977399) · [전사](https://softbankhackathon2026.slack.com/files/USLACKBOT/F0C531KM57B/___________________) | `raw/2026-09-28-huddle.md`, `raw/2026-09-28-thread.md` | AI 진입과 배포 실행 분리, 도구·스펙을 팀이 정하고 AI의 무제한 선택을 막는 방향. 초기 역할·전략 후보는 이후 회의로 갱신 |
| 9/29 | [허들](https://softbankhackathon2026.slack.com/archives/C0C1FHVQES3/p1790683611218859) · [전사](https://softbankhackathon2026.slack.com/files/USLACKBOT/F0C5FRND65A/___________________) | `raw/2026-09-29-huddle.md`, `raw/2026-09-29-thread.md` | Provider별 차이를 공통 API/검증 경계 아래에 둠. DNS와 LB 역할 분리. 담당별 PoC를 가져와 통합 |
| 10/1 | [허들](https://softbankhackathon2026.slack.com/archives/C0C1FHVQES3/p1790856034100609) · [전사](https://softbankhackathon2026.slack.com/files/USLACKBOT/F0C63236BL1/___________________) | `raw/2026-10-01-huddle.md`, `raw/2026-10-01-thread.md` | [최신 결정 원장](2026-10-01.md) |

원문에는 내부 계정과 개인 대화가 포함되어 있어 `raw/`는 로컬에만 보관하고 Git과 공유 ZIP에서 제외합니다. 원문 6개 파일의 크기와 SHA256은 [source-index.json](source-index.json)에 기록합니다. 팀 공유 문서에는 결정, 담당, 근거 링크를 사용합니다.

9/28 자동 요약에는 회의 날짜와 맞지 않는 일정이 있습니다. 제출 기한은 [공식 제출 안내](../submission-requirements.md)를 따릅니다. 이전 연구에서 전사를 확보하지 못했다고 기록한 내용은 당시 상태로 보존합니다.
