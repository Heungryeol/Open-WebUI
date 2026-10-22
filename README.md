# Open-WebUI

Open WebUI 에 올려 쓰는 Function 코드 보관소.

## functions/pais_multi_agent_pipe.py

PAIS(VMware Private AI Services) 의 Agent 들을 Open WebUI 모델로 노출하는 Pipe.
Authentik OAuth2 password grant 로 토큰을 받는다.

### 적용

1. Open WebUI 관리자 → Functions → PAIS Multi Agent Pipe → 코드를 이 파일로 바꾸고 저장.
2. **Valves 에서 `Oidc Client Id` 와 `Oidc Password` 를 넣는다.** 이 저장소는 공개라 두 값을 코드에 두지 않는다.
   Valves 에 넣은 값은 Open WebUI DB 에 저장되어 코드를 다시 바꿔도 유지된다.

### 대화 이력 다듬기 (2026-10-22 추가)

에이전트가 앞 답의 KB 글을 베껴 도구 없이 답하던 문제(BRCM-MCP 쇼케이스 리허설)를 막는다.

| Valve | 기본 | 뜻 |
|---|---|---|
| `HISTORY_MODE` | `strip_kb` | `full` 이력 그대로 / `strip_kb` KB 가 든 이전 답은 짧은 표시로 / `user_only` 이전 답 모두 짧은 표시로 / `last_only` 마지막 질문만 |
| `MAX_HISTORY_TURNS` | 6 | 마지막 질문 앞에 남길 이전 대화 쌍 수 |
| `KEEP_ANSWER_CHARS` | 600 | KB 가 아닌 이전 답을 남길 글자 수 |
| `STRIP_TABLES` | 켬 | (10-22 추가) `strip_kb` 에서 이전 답의 마크다운 표는 머리 줄만 남기고 데이터 행을 "(표 N행 생략 — 첫 열: a, b, c …; 지금 값은 도구로 다시 조회)" 한 줄로(첫 열 값 최대 8개). 표 밖 문장은 남는다 |
| `SAME_QUESTION_HINT` | 켬 | (10-22 추가) 같은 채팅에서 이전과 같은 질문이면 "(같은 질문: 값이 바뀌었을 수 있으니 도구를 다시 호출)" 한 줄을 붙임. 공백·문장부호·대소문자·Q 번호·끝의 "요/줘/주세요" 만 무시하고 완전히 같을 때만, 그 질문의 답이 도구 답(표·KB·도구 이름)이었을 때만. 인사·감사·자기소개와 "나머지 보여줘" 류는 제외 |
| `FOLLOWUP_HINT` | 켬 | 주제 없는 짧은 후속 질문("잠금 해제 방법")에 앞 질문을 붙여 넘김 |
| `FOLLOWUP_MAX_CHARS` | 25 | 후속 질문으로 볼 최대 글자 수 |
| `FORCE_TOOL_CHOICE` | 끔 | (실험) `tool_choice=required`. PAIS 가 받는지 쇼 뒤에 시험 |

"나머지 보여줘" 류("나머지도 보여줘", "나머지 알려줘", "다음 페이지", "계속", "더 보여주세요")는 바로 앞 답의 도구로 이어 보기를 부르라는 한 줄을 붙여 넘긴다. 화면의 질문은 바뀌지 않는다.

같은 채팅에서 앞 표를 그대로 반복하거나 없는 행을 지어 붙이던 문제(BRCM-MCP #57 3절, F2 Q4)는 위 두 Valve 로 막는다.
앞 표의 값(이름 첫 열 몇 개 말고)이 모델에게 보이지 않으므로 표 값을 다시 물으면 도구를 불러야 한다. 두 Valve 를 끄면 10-22 이전 동작과 같다.
