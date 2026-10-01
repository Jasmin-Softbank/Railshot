# RAILSHOT 제출 안내

작성일 2026-10-02 · 공식 안내 확인 · 제출 전

이 문서는 제출물, 기한, 발표 형식을 설명합니다. 기준 자료는 운영진의 [예선 1 킥오프 안내](https://docs.google.com/presentation/d/1739GqEJuv_Enqu_m3CEkej75kcj6TsalBIP3E2v5eJU/edit)입니다. 전체 75쪽의 텍스트와 한국어·일본어 대응 페이지를 확인했습니다. 원본 이미지의 모든 문구를 별도로 판독한 것은 아닙니다.

## 1. 제출물과 기한

10/3에 제출하는 항목은 개발 과정과 자료를 볼 수 있는 **세 종류의 링크**입니다. 그 시점에 코드나 발표 문서가 완성되어 있을 필요는 없습니다. 팀 Slack 채널에서 `@mentors`를 붙여 제출합니다. [공식 p25](https://docs.google.com/presentation/d/1739GqEJuv_Enqu_m3CEkej75kcj6TsalBIP3E2v5eJU/edit#slide=id.g3fb988a1216_15_29), [p27](https://docs.google.com/presentation/d/1739GqEJuv_Enqu_m3CEkej75kcj6TsalBIP3E2v5eJU/edit#slide=id.g3fa6c960a6a_0_946).

| 제출 항목 | 내용 | 기한 |
|---|---|---|
| 코드 링크 | GitHub 저장소 등 소스코드의 위치 | 10/3 10:00 |
| 전체 자료 링크 | 설계 문서, 논의 메모, 회의록, 대안 검토 기록 | 10/3 10:00 |
| 발표 자료 링크 | 중간 보고와 최종 발표에서 사용할 문서 | 10/3 10:00 |
| 발표 언어와 통역 원고 | 한국어 또는 일본어 선택, 통역용 스크립트 | 10/4 13:00 |

언어와 통역 원고의 제출 기한은 [p29](https://docs.google.com/presentation/d/1739GqEJuv_Enqu_m3CEkej75kcj6TsalBIP3E2v5eJU/edit#slide=id.g3fa6c960a6a_0_1258)에서도 확인할 수 있습니다. 시각은 서울 현장 일정에 맞춰 KST로 관리합니다. 기한 문자열 자체에는 별도의 시간대 표기가 없습니다.

회의록과 발표 자료를 포함한 모든 자료는 팀 Notion 페이지에 작성합니다. 하위 페이지와 데이터베이스를 추가할 수 있습니다. 현재 HTML/PDF는 로컬 검토용 출력본이며, Notion 게시와 Slack 링크 제출은 아직 수행하지 않았습니다. [공식 p60](https://docs.google.com/presentation/d/1739GqEJuv_Enqu_m3CEkej75kcj6TsalBIP3E2v5eJU/edit#slide=id.g3fb988a1216_15_89).

## 2. 발표 형식

| 구분 | 시간 | 사용하는 자료와 내용 |
|---|---|---|
| 중간 보고 | 보고 3분, Q&A 최대 4분 | 시스템 소개 약 30초, 설계 개요와 아키텍처. 작업 중인 문서를 사용합니다. |
| 최종 발표 | 5분, 통역 시간 제외 | 설계 설명 약 2분, 실제 데모 약 3분. 슬라이드는 사용하지 않습니다. |

순차 통역을 위해 문단 단위로 멈춥니다. 최종 설명에는 제품의 목적, 선택 이유, 기술적 특징, 구현 범위를 포함합니다. 목업 화면을 사용하면 목업임을 밝힙니다. [중간 보고 p31](https://docs.google.com/presentation/d/1739GqEJuv_Enqu_m3CEkej75kcj6TsalBIP3E2v5eJU/edit#slide=id.g3fa6c960a6a_0_1419), [최종 발표 p33](https://docs.google.com/presentation/d/1739GqEJuv_Enqu_m3CEkej75kcj6TsalBIP3E2v5eJU/edit#slide=id.g3fbe3b9b5c4_0_0), [설명 내용 p35](https://docs.google.com/presentation/d/1739GqEJuv_Enqu_m3CEkej75kcj6TsalBIP3E2v5eJU/edit#slide=id.g3fbe3b9b5c4_0_5).

시연에서는 서비스가 실제로 배포되는 모습을 보여줍니다. 간단한 샘플 웹앱으로 충분하며, 배포하는 앱 자체의 기능 수는 평가 대상이 아닙니다. 녹화 영상으로 실제 배포를 대체할 수 있는지는 확인하지 못했습니다. [공식 p39](https://docs.google.com/presentation/d/1739GqEJuv_Enqu_m3CEkej75kcj6TsalBIP3E2v5eJU/edit#slide=id.g3fa355e0b43_0_988).

전체 일정은 Day 1 중간 보고 14:00부터 17:00까지, Day 2 최종 발표 15:00부터 16:30까지입니다. 상황에 따라 변경될 수 있으며 팀별 발표 순서는 별도로 확인해야 합니다. [공식 p55](https://docs.google.com/presentation/d/1739GqEJuv_Enqu_m3CEkej75kcj6TsalBIP3E2v5eJU/edit#slide=id.g3f851d930e8_0_1970).

## 3. 평가 기준

| 평가 항목 | 배점 | RAILSHOT에서 제시할 근거 |
|---|---:|---|
| 완성도·데모 | 30 | Cloud와 On-prem의 실제 배포, 두 환경의 공개 URL과 앱 응답 |
| 클라우드 활용 | 30 | 공통 앱 계약과 환경별 Provider·runtime·네트워크의 연결 |
| 흥미·독창성 | 10 | 사용자가 수행하는 작업과 제품이 대신 처리하는 배포 준비 과정 |
| AI 활용 | 10 | 입력 소스, 검사 실패, 허용된 AI 수정, 재검사 결과 |
| 팀 개발 | 20 | 담당자별 구현, 인터페이스 합의, 대안 검토, 통합 기록 |

이 배점은 [올해 공식 평가표 p41](https://docs.google.com/presentation/d/1739GqEJuv_Enqu_m3CEkej75kcj6TsalBIP3E2v5eJU/edit#slide=id.g3fb988a1216_15_52)을 따릅니다. 개발 과정과 중간 보고도 평가에 포함됩니다. 개인 선발에서는 기술력, 팀워크, 소통, 적극성을 별도로 봅니다. [공식 p42](https://docs.google.com/presentation/d/1739GqEJuv_Enqu_m3CEkej75kcj6TsalBIP3E2v5eJU/edit#slide=id.g3fa4c268ac0_3_78).

운영진은 앱이 실제 노트북 VM에서 실행되면 외부 EC2나 터널을 공개 입구로 사용해도 온프레미스로 평가한다고 답했습니다. 이 답변은 공개 입구와 앱 실행 위치를 구분할 근거입니다. 특정 터널 제품의 채택을 의미하지는 않습니다. [운영진 답변](https://softbankhackathon2026.slack.com/archives/C0C1V129P7G/p1790850490196929?thread_ts=1790848374.451949&cid=C0C1V129P7G).

## 4. 개발 범위와 문서 구성

공식 테마는 **One Action, Infinite Clouds.**입니다. 로컬 웹앱을 AI의 도움으로 Cloud 또는 On-prem에 쉽게 배포하는 시스템을 만듭니다. MCP는 이 목표를 위한 팀의 진입점 선택입니다. [공식 p15](https://docs.google.com/presentation/d/1739GqEJuv_Enqu_m3CEkej75kcj6TsalBIP3E2v5eJU/edit#slide=id.g409f4b789e5_1_0).

모니터링, DB 변환, rollback, blue-green, canary, 비용 관리 등은 아이디어 예시입니다. 전체 목록을 필수 구현 범위로 사용하지 않습니다. CSP와 AI 도구는 자유롭게 선택할 수 있습니다. [예시 p19](https://docs.google.com/presentation/d/1739GqEJuv_Enqu_m3CEkej75kcj6TsalBIP3E2v5eJU/edit#slide=id.g3fb988a1216_15_13), [기술 선택 p21](https://docs.google.com/presentation/d/1739GqEJuv_Enqu_m3CEkej75kcj6TsalBIP3E2v5eJU/edit#slide=id.g3fb988a1216_15_22).

SoftBank의 공식 안내는 클라우드 기술과 설계·구축·시험·운용, 고객과 동료에게 기술을 설명하는 능력을 강조합니다. 기획서에서는 이를 환경별 설계, 실행 결과, 팀 간 연결 과정으로 설명합니다. 측정하지 않은 시간 절감률이나 비용 효과는 넣지 않습니다. [공식 p9](https://docs.google.com/presentation/d/1739GqEJuv_Enqu_m3CEkej75kcj6TsalBIP3E2v5eJU/edit#slide=id.g3fb988a1216_15_0).

| 읽을 문서 | 내용 |
|---|---|
| [기획서](proposal.md) | 사용자 문제, 핵심 시나리오, 개발 범위와 완료 조건 |
| [아키텍처](architecture/README.md) | 시스템·클러스터·네트워크·사용자·데이터·시퀀스 그림과 구현 계약 |
| [인터페이스](api/ansible.md) | Ansible 호출 방법, 입력, 결과, 지원 범위 |
| [회의와 R&R](meetings/2026-10-01.md) | 담당 역할과 선택의 근거 |
| [검증 보고](integration/validation.md) | 실행한 검사와 남은 통합 작업 |
| [발표 원고](presentation-script.md) | 2분 설명과 3분 데모, 통역용 용어 |

자료를 발표용 완성본으로 정리하더라도 기존 회의와 대안 검토 기록은 함께 보존합니다. 여섯 그림은 팀 정렬을 위한 구성으로, 대회가 정한 필수 그림 개수는 아닙니다.

## 5. 남은 확인 사항

팀은 발표 언어와 발표자, 시연할 앱과 두 target, 데모 순서와 소요 시간을 확정해야 합니다. 팀별 발표 순서, 최종 코드 동결 시각, 녹화 대체 허용, Notion 대신 외부 문서만 사용할 수 있는지는 별도 확인이 필요합니다. 고정 페이지 수, PDF 규격, 글자 수, 별도 제출 템플릿은 확인한 안내에 없습니다.

원본 조사는 `#term1_all_announce`, `#all-softbankhackathon2026`, `#term1_all_question`의 공지 본문과 관련 질문 답글을 포함합니다. 확인한 범위에서는 기한과 발표 형식을 바꾸는 공지가 없었습니다. 이후 안내가 나오면 이 문서에 변경 시점과 출처를 기록합니다.
