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
| `FOLLOWUP_HINT` | 켬 | 주제 없는 짧은 후속 질문("잠금 해제 방법")에 앞 질문을 붙여 넘김 |
| `FOLLOWUP_MAX_CHARS` | 25 | 후속 질문으로 볼 최대 글자 수 |
| `FORCE_TOOL_CHOICE` | 끔 | (실험) `tool_choice=required`. PAIS 가 받는지 쇼 뒤에 시험 |

"나머지 보여줘" 류는 바로 앞 답의 도구로 이어 보기를 부르라는 한 줄을 붙여 넘긴다. 화면의 질문은 바뀌지 않는다.
