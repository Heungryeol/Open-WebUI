"""
title: PAIS Multi-Agent Pipe (Authentik OAuth2 Password Grant)
author: open-webui / merged by AI Assistant
author_url: https://github.com/open-webui
version: 0.1.0
description: >
    VMware Private AI Services(PAIS)의 여러 Agent를 Open WebUI 모델 목록에
    동적으로 노출하고(1번 코드 방식), Authentik의 OAuth2 password grant로
    발급받은 토큰을 캐싱/자동 갱신하여 인증한다(2번 코드 방식).
    client_id 와 password 는 저장소에 두지 않는다 -- Open WebUI 의 Valves 에서
    한 번 넣으면 DB 에 저장되어 코드를 바꿔도 유지된다.
requirements: httpx
"""

import re
import time
import threading
from typing import Optional, Union, Generator, Iterator

import httpx
from pydantic import BaseModel, Field
from loguru import logger


class Pipe:
    class Valves(BaseModel):
        # --- PAIS API 설정 ---
        PAIS_API_BASE_URL: str = Field(
            default="https://kr-pais.rangers.lab/api/v1/compatibility/openai/v1",
            description="PAIS OpenAI 호환 API Base URL (.../compatibility/openai/v1 까지).",
        )
        VERIFY_SSL: bool = Field(
            default=False,
            description="PAIS 및 Authentik 토큰 엔드포인트 SSL 검증 여부. 사내 CA 신뢰 전까지 False 권장.",
        )

        # --- Authentik OAuth2 Password Grant 설정 ---
        # 3번 첨부에서 실제로 토큰 발급 및 /agents 조회까지 confirmed된 값을
        # 기본값으로 사용한다. 현재 환경은 "pais-backend"/"pais" 이원화 없이
        # 단일 "pais-provider"(Public 클라이언트, Application: pais) 구성이다.
        AUTHENTIK_TOKEN_URL: str = Field(
            default="https://kr-auth.rangers.lab:9443/application/o/token/",
            description="Authentik OAuth2/OIDC 토큰 엔드포인트.",
        )
        OIDC_CLIENT_ID: str = Field(
            default="",
            description=(
                "PAIS가 신뢰하는 Authentik provider(pais-provider, Public 클라이언트)의 Client ID."
            ),
        )
        OIDC_CLIENT_SECRET: str = Field(
            default="",
            description="Public 클라이언트이므로 비워둔다. 값이 있을 때만 요청에 포함한다.",
        )
        OIDC_USERNAME: str = Field(
            default="pais-sv",
            description="Application의 필요 그룹(pais-users 등)에 소속된 Authentik 서비스 계정 사용자명.",
        )
        OIDC_PASSWORD: str = Field(
            default="",
            description="위 서비스 계정의 App Password/Token.",
        )
        OIDC_SCOPE: str = Field(
            default="openid groups profile email offline_access",
            description="3번에서 confirmed된 scope 조합.",
        )
        TOKEN_REFRESH_MARGIN_SECONDS: int = Field(
            default=60,
            description="토큰 실제 만료 시각보다 이 초만큼 앞서서 미리 갱신한다.",
        )

        # --- 동작 옵션 ---
        ENABLE_STREAMING: bool = Field(
            default=True, description="스트리밍 응답 사용 여부."
        )
        CONNECTION_TIMEOUT: float = Field(
            default=10.0, description="연결 타임아웃(초)."
        )
        READ_TIMEOUT: float = Field(
            default=300.0, description="응답 읽기 타임아웃(초)."
        )
        AGENTS_LIST_LIMIT: int = Field(
            default=400, description="/agents 조회 시 가져올 최대 개수."
        )

        # --- 대화 이력 처리 (KB 지어내기 방지) ---
        # The agent copied an earlier KB answer instead of calling a tool when
        # the full chat history (with that answer's text) was sent. Earlier
        # answers that carry KB text are therefore replaced by a stub, so the
        # only way to answer a new question is a new tool call.
        HISTORY_MODE: str = Field(
            default="strip_kb",
            description=(
                "full = 이력 그대로 / strip_kb = KB 가 든 이전 답은 짧은 표시로 바꿈(기본) / "
                "user_only = 이전 답을 모두 짧은 표시로 바꿈 / last_only = 마지막 질문만 보냄"
            ),
        )
        MAX_HISTORY_TURNS: int = Field(
            default=6,
            description="마지막 질문 앞에 남길 이전 대화 쌍의 최대 수 (0 = 이전 대화 없음).",
        )
        KEEP_ANSWER_CHARS: int = Field(
            default=600,
            description="strip_kb 에서 KB 가 아닌 이전 답을 남길 최대 글자 수.",
        )
        # An earlier TABLE was repeated as the answer to the same question, and once
        # rewritten into a table with datastores that do not exist (BRCM-MCP #57
        # section 3, 10-22 F2 Q4): no tool was called. strip_kb now also replaces
        # an earlier answer that holds a markdown table, so there is no table to copy.
        STRIP_TABLES: bool = Field(
            default=True,
            description="strip_kb 에서 마크다운 표(|---| 줄)가 든 이전 답도 짧은 표시로 바꿈.",
        )
        SAME_QUESTION_HINT: bool = Field(
            default=True,
            description="이전과 같은(거의 같은) 질문이면 '값이 바뀌었을 수 있으니 도구를 다시 호출' 한 줄을 붙임.",
        )
        # A fix question sent wrapped in backticks got no tool call and a made-up KB
        # answer ("근거 일치도: ★★★ 높음", no KB number, no link; 159 F1 10-23 01:00).
        TOOL_HINT: bool = Field(
            default=True,
            description="해결·방법 질문이나 영어 오류 문구가 든 질문에 '도구(KB 검색)로 답함' 한 줄을 붙임.",
        )
        FOLLOWUP_HINT: bool = Field(
            default=True,
            description="짧은 후속 질문('잠금 해제 방법' 등)에 앞 질문을 붙여 도구 호출을 유도.",
        )
        FOLLOWUP_MAX_CHARS: int = Field(
            default=25,
            description="이 글자 수 이하의 질문을 후속 질문으로 본다.",
        )
        # Open WebUI sends its own background tasks (chat title, follow-up chips, tags)
        # to the chat's model when no task model is set -- here that is the PAIS agent,
        # which then called tools for prompts nobody typed ("현재 발생 중인 주요 장애 …",
        # 159 10-23 00:51) and wrote chips unlike the answer's own list (BRCM-MCP #64).
        # The pipe answers those tasks itself, without PAIS.
        LOCAL_TASKS: bool = Field(
            default=True,
            description="Open WebUI 작업(제목·후속 질문 칩·태그 등)은 PAIS 를 부르지 않고 파이프가 직접 답함. 칩 = 마지막 답의 '이어서 물어볼 만한 것:' 줄 그대로.",
        )
        FOLLOWUP_MAX_CHIPS: int = Field(
            default=5, description="후속 질문 칩 최대 개수."
        )
        FORCE_TOOL_CHOICE: bool = Field(
            default=False,
            description="(실험) payload 에 tool_choice=required 를 넣음. PAIS 가 받는지 쇼 뒤에 시험.",
        )

    def __init__(self):
        self.type = "manifold"
        self.id = "pais_multi_agent"
        self.name = "PAIS: "
        self.valves = self.Valves()
        self.assistants: list = []
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._token_lock = threading.Lock()
        # 요청마다 새 TCP/TLS 커넥션을 맺지 않도록, Pipe 인스턴스 생명주기 동안
        # 재사용하는 단일 httpx.Client. (Open WebUI <-> PAIS 구간 지연의 상당수는
        # 매 메시지마다 새 클라이언트를 생성/종료하며 발생하는 handshake 비용이었음)
        self._client_lock = threading.Lock()
        self._http_client: Optional[httpx.Client] = None

    def _get_http_client(self) -> httpx.Client:
        """토큰 발급/Agent 목록 조회/채팅 완료 호출에 공용으로 쓰는 persistent client.
        커넥션 풀을 유지해 매 요청마다 TLS 핸드셰이크가 반복되지 않도록 한다."""
        with self._client_lock:
            if self._http_client is None or self._http_client.is_closed:
                self._http_client = httpx.Client(
                    verify=self.valves.VERIFY_SSL,
                    timeout=httpx.Timeout(
                        connect=self.valves.CONNECTION_TIMEOUT,
                        read=self.valves.READ_TIMEOUT,
                        write=30.0,
                        pool=10.0,
                    ),
                    limits=httpx.Limits(
                        max_keepalive_connections=10,
                        max_connections=20,
                        keepalive_expiry=60.0,
                    ),
                )
            return self._http_client

    # ------------------------------------------------------------------
    # Authentik OAuth2 Password Grant (2번 코드 방식)
    # ------------------------------------------------------------------
    def _fetch_token(self) -> str:
        """Authentik에서 password grant로 새 access token을 발급받는다.
        client_id + username + password(+scope) 조합만 사용하며,
        Public 클라이언트이므로 client_secret은 값이 있을 때만 포함한다."""
        data = {
            "grant_type": "password",
            "client_id": self.valves.OIDC_CLIENT_ID,
            "username": self.valves.OIDC_USERNAME,
            "password": self.valves.OIDC_PASSWORD,
            "scope": self.valves.OIDC_SCOPE,
        }
        if self.valves.OIDC_CLIENT_SECRET:
            data["client_secret"] = self.valves.OIDC_CLIENT_SECRET

        client = self._get_http_client()
        resp = client.post(self.valves.AUTHENTIK_TOKEN_URL, data=data)

        if resp.status_code != 200:
            logger.error(
                f"Authentik 토큰 발급 실패. Status: {resp.status_code}, Body: {resp.text[:500]}"
            )
            raise RuntimeError(
                f"Authentik token request failed (HTTP {resp.status_code}): {resp.text[:500]}"
            )

        payload = resp.json()
        access_token = payload.get("access_token")
        expires_in = payload.get("expires_in", 300)
        if not access_token:
            raise RuntimeError("Authentik 응답에 access_token이 없습니다.")

        self._access_token = access_token
        self._token_expires_at = time.time() + int(expires_in)
        logger.info(f"새 PAIS access token 발급 완료. 유효시간 {expires_in}s")
        return access_token

    def _get_access_token(self, force_refresh: bool = False) -> str:
        """캐시된 토큰을 반환하고, 없거나 만료 임박/강제 갱신 시 새로 발급한다."""
        with self._token_lock:
            now = time.time()
            margin = self.valves.TOKEN_REFRESH_MARGIN_SECONDS
            if (
                force_refresh
                or self._access_token is None
                or now >= (self._token_expires_at - margin)
            ):
                return self._fetch_token()
            return self._access_token

    def _auth_headers(self, token: Optional[str] = None) -> dict:
        token = token or self._get_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # 다중 Agent 목록 조회 (1번 코드 방식)
    # ------------------------------------------------------------------
    def get_base_url(self) -> str:
        return self.valves.PAIS_API_BASE_URL

    def get_all_assistants(self) -> list:
        """PAIS의 /agents 엔드포인트에서 사용 가능한 모든 Agent 목록을 가져온다."""
        base_url = self.get_base_url()
        agents_url = f"{base_url}/agents?limit={self.valves.AGENTS_LIST_LIMIT}"

        try:
            headers = self._auth_headers()
        except Exception as e:
            logger.error(f"토큰 발급 중 오류: {e}")
            return [{"id": "error", "name": f"Authentik 토큰 발급 실패: {str(e)}"}]

        try:
            client = self._get_http_client()
            response = client.get(agents_url, headers=headers)

            if response.status_code == 200:
                return response.json().get("data", [])

            logger.error(
                f"Agent 목록 조회 실패. Status: {response.status_code}, "
                f"Body: {response.text[:500]}"
            )
            return [
                {
                    "id": "error",
                    "name": "PAIS에서 모델 목록을 가져오지 못했습니다. OAuth 클라이언트/API Base URL을 확인하세요.",
                }
            ]
        except httpx.TimeoutException as e:
            logger.error(f"Agent 목록 조회 타임아웃: {e}")
            return [{"id": "error", "name": "PAIS 모델 목록 조회 타임아웃"}]
        except Exception as e:
            logger.error(f"Agent 목록 조회 오류: {e}")
            return [{"id": "error", "name": f"모델 목록 조회 오류: {str(e)}"}]

    def get_assistant(self, assistant_id: str) -> Optional[dict]:
        for assistant in self.assistants:
            if assistant.get("id") == assistant_id:
                return assistant
        return None

    def pipes(self) -> list:
        """Open WebUI가 모델 선택 목록으로 사용할 Agent 목록을 반환한다."""
        self.assistants = self.get_all_assistants()
        return self.assistants

    # ------------------------------------------------------------------
    # 대화 이력 다듬기
    # ------------------------------------------------------------------
    # Short, declarative stubs: an imperative stub in brackets was echoed back
    # by the model as its own reply (10-22, Q4). The tool name tells the model
    # where "나머지 보여줘" goes without showing it any KB text to copy.
    _KB_STUB = "(KB 문서 답, {tool} 결과 — 본문 생략)"
    _ANSWER_STUB = "(이전 답 — 본문 생략)"
    _TABLE_ROWS = "(표 {n}행 생략 — 첫 열: {names}; 지금 값은 도구로 다시 조회)"
    _SAME_QUESTION = "(같은 질문: 값이 바뀌었을 수 있으니 도구를 다시 호출)"
    # Names no tool: an Operations alert name ("Datastore is running out of disk space 조치
    # 방법은?") is Operations' to answer, a log phrase the log tool's (3rd QA M4).
    _TOOL_HINT = (
        "(이 질문은 도구로 답함: 오류·해결은 KB 검색, 로그 줄은 로그 도구, 알림은 Operations. "
        "앞 답이나 기억으로 KB 를 쓰지 않음)"
    )
    # A fix or how-to question (close to the gateway's is_how_to/asks_kb_resolution). Not
    # "방법론" (methodology) and not a bare "어떻게" ("어떻게 지내", 3rd QA M4).
    _HOW_TO = (
        r"해결|조치|고치|방법(?!론)|절차|어떻게\s*(?:해|하|고치|고쳐|해결|조치|복구|설정|확인|바꾸|바꿔|풀)"
        r"|(?<![A-Za-z])how\s+(?:to|do\s+i|can\s+i|should\s+i)(?![A-Za-z])"
        r"|(?<![A-Za-z])(?:fix|resolve|resolution|workaround|troubleshoot)"
    )
    # An English error text: four or more Latin words in a row WITH an error word -- "how are you
    # doing today", "Nice to meet you all" are four Latin words too (3rd QA M4).
    _ENGLISH_RUN = r"[A-Za-z][A-Za-z'’-]*(?:\s+[A-Za-z][A-Za-z'’-]*){3,}"
    _ERROR_WORD = (
        r"(?<![A-Za-z])(?:error|errors|fail|fails|failed|failure|unable|cannot|can't|no\s+more|denied"
        r"|timeout|timed\s+out|not\s+responding|invalid|refused|not\s+found|unreachable|corrupt\w*"
        r"|mismatch|did\s+not|has\s+been\s+modified|out\s+of\s+\w+)(?![A-Za-z])"
    )
    # Looking for log lines is the log tool's question; a fix word still wins ("감사 로그에서
    # 로그인 실패 해결 방법"). "로그인" is a login, not a log.
    _LOG_SEARCH = r"줄|로그(?!인)|찾아|검색|들어간|(?<![A-Za-z])logs?(?![A-Za-z])"
    # Small talk, matched on the WHOLE message (3rd QA M5: "감사 로그" is an audit log, and a
    # question after "안녕하세요," is still a question).
    _GREETING = r"^\s*(?:안녕(?:하세요|하십니까|하신가요)?|hi|hello|hey|좋은\s*(?:아침|오후|저녁)(?:입니다|이에요)?)[\s,.!~]*"
    _CHIT_CHAT = (
        r"(?:감사(?:합니다|해요|해)?|고마워(?:요)?|고맙습니다|수고(?:하셨습니다|했어요|했어|하세요)?|잘했어(?:요)?"
        r"|넌\s*누구(?:야|니)?|너는\s*누구(?:야|니)?|누구(?:세요|야)|뭐\s*할\s*수\s*있어|무엇을\s*할\s*수\s*있나요"
        r"|어떻게\s*지내(?:요|세요)?|반가워(?:요)?|반갑습니다|좋아(?:요)?|네|응|알겠(?:어|습니다|어요)"
        r"|thanks?(?:\s+(?:a\s+lot|you))?|thank\s+you|ok(?:ay)?|good|great|how\s+are\s+you(?:\s+doing)?(?:\s+today)?"
        r"|nice\s+to\s+meet\s+you(?:\s+all)?|what\s+can\s+you\s+do(?:\s+for\s+me)?|who\s+are\s+you"
        r"|can\s+you\s+speak\s+\w+|도움말|help)[\s.!?~]*"
    )

    @classmethod
    def _needs_tool(cls, text: str) -> bool:
        """A fix/how-to question, or an English error text that is not a log-line search."""
        import re

        if re.search(cls._HOW_TO, text, re.I):
            return True
        return bool(re.search(cls._ENGLISH_RUN, text) and re.search(cls._ERROR_WORD, text, re.I)
                    and not re.search(cls._LOG_SEARCH, text, re.I))

    @classmethod
    def _drop_greeting(cls, text: str) -> str:
        import re

        return re.sub(cls._GREETING, "", text or "", flags=re.I).strip()

    @classmethod
    def _is_chit_chat(cls, text: str) -> bool:
        """A short greeting, thanks or "who are you", and nothing else."""
        import re

        t = (text or "").strip()
        if len(t) > 40:
            return False
        rest = cls._drop_greeting(t)
        # One or more small-talk pieces ("네 알겠습니다", "감사합니다, 수고하세요").
        return not rest or bool(re.fullmatch(rf"(?:{cls._CHIT_CHAT}[\s,]*)+", rest, re.I))
    # The whole message in one wrapper: ```…```, `…`, "…", '…', “…”, ‘…’, 「…」, 『…』.
    _WRAPPERS = (("```", "```"), ("`", "`"), ('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"),
                 ("「", "」"), ("『", "』"))
    # Only a wrapped QUESTION is unwrapped: a quoted phrase alone ('"Update virtual NIC"') is a log
    # phrase to look for, and the quotes say so (3rd QA L1).
    _ASKS = r"방법|해결|조치|알려|보여|찾아|뭐야|있어|\?|(?<![A-Za-z])(?:how|what|why|fix|show)(?![A-Za-z])"
    _FENCE_LANGS = ("bash", "sh", "shell", "zsh", "python", "py", "json", "yaml", "yml", "text", "txt",
                    "console", "powershell", "ps1", "log", "ini", "xml", "sql")

    @classmethod
    def _unwrap(cls, text: str) -> str:
        """The message without a wrapper around ALL of it. A quote inside a sentence ("… 'vmk1' 가
        들어간 줄") stays: only a message that starts and ends with one wrapper, holding no other
        copy of it, is unwrapped -- the model read the backticked sentence as a block to answer."""
        import re

        t = (text or "").strip()
        for left, right in cls._WRAPPERS:
            if len(t) > len(left) + len(right) and t.startswith(left) and t.endswith(right):
                inner = t[len(left):len(t) - len(right)]
                if left == "```" and "\n" in inner:
                    # A fence may name a language on its first line ("```text").
                    head, rest = inner.split("\n", 1)
                    if head.strip().lower() in cls._FENCE_LANGS + ("",) and rest.strip():
                        inner = rest
                if (left not in inner and right not in inner and inner.strip()
                        and re.search(cls._ASKS, inner, re.I)):
                    return inner.strip()
        return t
    # A typed question number, either case: "Q4 ...", "q1. ...".
    _QLABEL = r"^\s*[Qq]\d+[.):]?\s*"
    # "나머지 보여줘" and its variants; a sentence with more in it is a new question.
    _PAGING = (
        r"(?:나머지|다음|더|계속)\s*(?:도|를|은)?\s*(?:페이지|거|것)?\s*(?:도|를)?\s*"
        r"(?:보여\s*줘|보여\s*주세요|보여\s*줄래|알려\s*줘|알려\s*주세요|줘|주세요|보기|해\s*줘)?[.!?]?"
        r"|more|next(?:\s*page)?|continue"
    )

    @classmethod
    def _same_text(cls, text: str) -> str:
        # Only form is ignored -- spacing, punctuation, case, a "Q4" label and a
        # polite ending ("보여줘" / "보여 주세요" / "보여요"). QA on 522e1cc: a 0.9
        # similarity marked "전원이 켜진 VM" after "전원이 꺼진 VM" as the same question.
        import re

        bare = re.sub(cls._QLABEL, "", text or "")
        bare = re.sub(r"[\s.,!?~·]+", "", bare).lower()
        return re.sub(r"(?:주세요|줘요|줘|요)$", "", bare)

    @classmethod
    def _compact_tables(cls, text: str, table_mark) -> str:
        """Each markdown table keeps its header row; its data rows become one line naming up to
        eight first-column values. QA on 81b561b: replacing the whole answer lost what "그 호스트
        로그 보여줘" points at; the values themselves stay out so there is nothing to copy."""
        lines = text.split("\n")
        out, i = [], 0
        while i < len(lines):
            # Leading pipes are optional in markdown ("a | b" over "---|---").
            if i + 1 < len(lines) and "|" in lines[i] and table_mark.match(lines[i + 1]):
                out.append(lines[i])
                i += 2
                firsts = []
                while i < len(lines) and "|" in lines[i] and lines[i].strip():
                    cells = lines[i].strip().strip("|").split("|")
                    firsts.append(cells[0].strip() if cells else "")
                    i += 1
                shown = ", ".join(f for f in firsts[:8] if f) + (" …" if len(firsts) > 8 else "")
                out.append(cls._TABLE_ROWS.format(n=len(firsts), names=shown or "-"))
                continue
            out.append(lines[i])
            i += 1
        return "\n".join(out)

    @staticmethod
    def _text_of(content) -> str:
        # Open WebUI sends either a string or a list of parts (text/image).
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
            )
        return ""

    def _shape_history(self, messages: list) -> list:
        """Trim what earlier turns the agent sees, so it calls a tool for every
        new question instead of rewriting an earlier KB answer from memory."""
        import re

        mode = (self.valves.HISTORY_MODE or "strip_kb").strip().lower()
        if mode == "full" or not messages:
            return messages

        system = [m for m in messages if m.get("role") == "system"]
        turns = [m for m in messages if m.get("role") in ("user", "assistant")]
        last_user = None
        for i in range(len(turns) - 1, -1, -1):
            if turns[i].get("role") == "user":
                last_user = i
                break
        if last_user is None:
            return messages
        if mode == "last_only":
            return system + [turns[last_user]]

        earlier = turns[:last_user]
        # Keep only the last N user/assistant pairs before the current question.
        keep = max(0, int(self.valves.MAX_HISTORY_TURNS)) * 2
        earlier = earlier[-keep:] if keep else []

        kb_mark = re.compile(r"\bKB\s*\d{5,}|knowledge\.broadcom\.com|근거\s*일치도")
        # A markdown table's separator row: "|---|---|", "| :--- | ---: |".
        table_mark = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*\|", re.M)
        shaped = []
        for m in earlier:
            if m.get("role") != "assistant":
                shaped.append(m)
                continue
            text = self._text_of(m.get("content"))
            if mode == "user_only" or kb_mark.search(text):
                if kb_mark.search(text):
                    tool = "nsx_query" if re.search(r"\bNSX\b", text) else "vcenter_query"
                    stub = self._KB_STUB.format(tool=tool)
                else:
                    stub = self._ANSWER_STUB
                shaped.append({"role": "assistant", "content": stub})
            else:
                if self.valves.STRIP_TABLES and table_mark.search(text):
                    # Declarative, like the KB stub: values in an old table are stale
                    # by the next question, and a table in view was copied or extended.
                    text = self._compact_tables(text, table_mark)
                limit = max(0, int(self.valves.KEEP_ANSWER_CHARS))
                cut = text if len(text) <= limit else text[:limit] + " …(생략)"
                shaped.append({"role": "assistant", "content": cut})
        current = turns[last_user]
        # "나머지 보여줘" with no earlier text to copy made the model write a
        # paragraph about `more` values instead of calling the tool (10-22).
        # Spell out the call it should make; the server keeps the page.
        cur_text = self._text_of(current.get("content")).strip()
        bare = re.sub(self._QLABEL, "", cur_text)
        unwrapped = self._unwrap(bare)
        if unwrapped != bare:
            cur_text = cur_text[: len(cur_text) - len(bare)] + unwrapped
            bare = unwrapped
            current = {"role": "user", "content": cur_text}
        plain = current
        is_paging = bool(re.fullmatch(self._PAGING, bare, re.I))
        if is_paging:
            prev_tool = None
            for m in reversed(shaped):
                if m.get("role") == "assistant":
                    hit = re.search(r"(vcenter_query|nsx_query|ops_query|logs_query)", m.get("content", ""))
                    prev_tool = hit.group(1) if hit else None
                    break
            hint = f"{prev_tool or '바로 앞 답의 도구'} 를 request=\"나머지 보여줘\" 로 호출"
            current = {"role": "user", "content": f"{cur_text}\n({hint})"}
        elif (
            self.valves.FOLLOWUP_HINT
            and len(bare) <= int(self.valves.FOLLOWUP_MAX_CHARS)
            # A greeting is not a follow-up: the line asked for a tool call (159, 10-23).
            and not self._is_chit_chat(bare)
            # A sentence that names its own subject (a product, an object, a
            # Latin term) is a new question, not a follow-up.
            and not re.search(
                r"[A-Za-z]{2,}|호스트|데이터스토어|클러스터|스냅샷|라이선스|인증서|네트워크|알림|알람|로그|스위치|세그먼트",
                bare,
            )
        ):
            # A short follow-up ("잠금 해제 방법") was answered from the model's
            # own knowledge with no tool call (10-22, F3). Naming the earlier
            # question lets it build a full request, and the line asks for a
            # call; the earlier question carries the subject the tool needs.
            prev_q = None
            for m in reversed(shaped):
                if m.get("role") == "user":
                    prev_q = re.sub(self._QLABEL, "", self._text_of(m.get("content"))).strip()
                    break
            if prev_q:
                prev_q = prev_q[:120]
                current = {
                    "role": "user",
                    # The first wording made the model resend the earlier
                    # question verbatim as the tool request (10-22, F3 Q2:
                    # "해결방법" became a second log search). Ask for the new
                    # words plus the earlier subject instead.
                    "content": (
                        f"{cur_text}\n(앞 질문 '{prev_q}' 의 주제에 대한 '{bare}'. "
                        f"도구가 필요하면 request 에 '{bare}' 를 꼭 넣고 앞 질문의 주제 낱말을 붙임 -- "
                        f"앞 질문 문장을 그대로 다시 보내지 않음)"
                    ),
                }
        if self.valves.SAME_QUESTION_HINT and not is_paging:
            # The same sentence again in one chat was answered by repeating the
            # earlier answer with no tool call (BRCM-MCP #57 section 3, 10-22
            # 14:18). Every earlier question counts, also the ones MAX_HISTORY_TURNS
            # dropped: the model may still have answered it from what it kept.
            # Only when the earlier answer came from a tool: a repeated greeting needs no
            # tool call, and the hint made the model call one (2nd QA M-B).
            tool_answer = re.compile(
                r"vcenter_query|nsx_query|ops_query|ops_top_n|ops_get_metric|logs_query|logs_incident"
                r"|logs_correlate|본문 생략|행 생략"
            )
            now = self._same_text(bare)
            for j, m in enumerate(turns[:last_user]):
                if m.get("role") != "user":
                    continue
                reply = turns[j + 1] if j + 1 < last_user else {}
                reply_text = self._text_of(reply.get("content")) if reply.get("role") == "assistant" else ""
                from_tool = bool(
                    table_mark.search(reply_text) or kb_mark.search(reply_text) or tool_answer.search(reply_text)
                )
                if (
                    len(now) >= 4
                    and from_tool
                    and not self._is_chit_chat(bare)
                    and self._same_text(self._text_of(m.get("content"))) == now
                ):
                    current = {
                        "role": "user",
                        "content": f"{self._text_of(current.get('content'))}\n{self._SAME_QUESTION}",
                    }
                    break
        if (
            self.valves.TOOL_HINT
            and not is_paging
            # One hint per message: the follow-up and same-question lines already ask for a call.
            and current is plain
            and not self._is_chit_chat(bare)
            and self._needs_tool(self._drop_greeting(bare))
        ):
            current = {"role": "user", "content": f"{cur_text}\n{self._TOOL_HINT}"}
        return system + shaped + [current]

    # ------------------------------------------------------------------
    # Chat Completion 호출
    # ------------------------------------------------------------------
    # ---- Open WebUI background tasks (BRCM-MCP #64) ----
    NEXT_TITLE = "이어서 물어볼 만한 것:"
    MORE = "나머지 보여줘"
    # "이어서 보려면 '나머지 보여줘'" / "나머지 44건은 '나머지 보여줘' 로" in any quote style,
    # but not "나머지 0개는 …" (a real misprint, vcenter tools.py 2026-10-16).
    _Q = r"['\"‘’“”`]?"
    _MORE_LINE = re.compile(r"이어서\s*보려면\s*" + _Q + r"나머지\s*보여\s*줘" + _Q
                            + r"|나머지(?!\s*0\s*[개건대줄명곳])\s*[^\n]{0,30}?" + _Q + r"나머지\s*보여\s*줘" + _Q + r"\s*로")
    _END_LINE = re.compile(r"문서의\s*끝입니다")
    # Heading variants seen from models: bold, "###", no colon, full-width colon.
    _HEAD = re.compile(r"^\s*(?:#+\s*)?\**\s*(이어서\s*물어볼\s*만한\s*것|관련)\s*\**\s*[:：]?\s*\**\s*$")
    _ITEM = re.compile(r"^(?:[-*•]|\d+[.)])\s+(.*)$")

    @staticmethod
    def _task_of(__task__, __metadata__) -> str:
        task = __task__ or (__metadata__ or {}).get("task") or ""
        return str(task).split(".")[-1].lower()

    @classmethod
    def _output_text(cls, item) -> str:
        """Text of Open WebUI 0.10+ `output` items: an answer can be saved there with
        `content` left empty (lab 2026-10-15; 159 chats read back empty 10-24)."""
        if isinstance(item, str):
            return item
        if isinstance(item, list):
            return "\n".join(t for t in (cls._output_text(x) for x in item) if t and t.strip())
        if not isinstance(item, dict):
            return ""
        kind = str(item.get("type") or "")
        if kind in ("function_call_output", "tool_result", "tool_call_output", "reasoning", "web_search_call") \
                or "call" in kind:
            return ""
        if isinstance(item.get("text"), str):
            return item["text"]
        for key in ("content", "output", "message"):
            if key in item:
                return cls._output_text(item[key])
        return ""

    @classmethod
    def _last_answer(cls, messages: list) -> str:
        for m in reversed(messages or []):
            if isinstance(m, dict) and m.get("role") == "assistant":
                text = cls._text_of(m.get("content"))
                if not text.strip():
                    text = cls._output_text(m.get("output"))
                if text.strip():
                    return text
        return ""

    @classmethod
    def _history_answer(cls, prompt: str) -> str:
        """The last ASSISTANT turn inside the task prompt's <chat_history>, for an
        Open WebUI that does not pass the chat itself in the task body."""
        m = re.search(r"<chat_history>(.*?)</chat_history>", prompt or "", re.S)
        hist = m.group(1) if m else (prompt or "")
        parts = re.split(r"(?im)^\s*(USER|ASSISTANT)\s*:", hist)
        last = ""
        for i in range(1, len(parts) - 1, 2):
            if parts[i].upper() == "ASSISTANT":
                last = parts[i + 1]
        return last

    @classmethod
    def follow_up_chips(cls, answer: str, limit: int = 5) -> list:
        """Chips are the answer's own list, copied: a chip is sent back word for word,
        so only sentences the server offered route and page correctly (#64)."""
        lines = (answer or "").splitlines()
        lists, listed = {}, set()
        for i, line in enumerate(lines):
            h = cls._HEAD.match(line)
            if not h:
                continue
            key = "next" if h.group(1).startswith("이어서") else "related"
            got = []
            for j in range(i + 1, len(lines)):
                raw = lines[j]
                t = raw.strip()
                if not t:
                    if got:
                        break
                    continue
                it = cls._ITEM.match(t)
                if not it:
                    break
                listed.add(j)
                if raw[:1] in (" ", "\t") and len(raw) - len(raw.lstrip()) >= 2 and got:
                    continue  # a sub-item belongs to the line above, not a question of its own
                q = it.group(1).strip().strip("`").strip()
                q = re.sub(r"^\*\*(.*)\*\*$", r"\1", q).strip()
                # A KB title with its Broadcom link is a reference, not a question to send back.
                if q and "://" not in q and q not in got:
                    got.append(q)
            lists.setdefault(key, got)
        chips = lists.get("next") or []
        # The model sometimes put the offers under "관련:" instead (159 10-23, 1022-final
        # instructions), with the "이어서" list empty or gone; take them from there then.
        if not chips:
            chips = lists.get("related") or []
        # "문서의 끝입니다." anywhere outside the lists: a finished document has no "more".
        rest = "\n".join(l for k, l in enumerate(lines) if k not in listed)
        if cls._END_LINE.search(rest):
            chips = [c for c in chips if c != cls.MORE]
        elif cls._MORE_LINE.search(rest) and cls.MORE not in chips[:1]:
            chips = [cls.MORE] + [c for c in chips if c != cls.MORE]
        return chips[: max(0, limit)]

    @classmethod
    def chat_title(cls, messages: list) -> str:
        for m in messages or []:
            if isinstance(m, dict) and m.get("role") == "user":
                t = " ".join(cls._text_of(m.get("content")).split())
                t = cls._unwrap(t) if t else t
                if t:
                    return t if len(t) <= 40 else t[:39].rstrip() + "…"
        return ""

    def local_task(self, task: str, body: dict, __metadata__: dict = None) -> Optional[str]:
        """The JSON Open WebUI expects for its task, or None when the task is not ours."""
        if not task:
            return None
        import json

        meta = __metadata__ or {}
        tbody = meta.get("task_body") or body.get("metadata", {}).get("task_body") or {}
        chat = tbody.get("messages") if isinstance(tbody, dict) else None
        prompt = self._text_of((body.get("messages") or [{}])[-1].get("content")) if body.get("messages") else ""
        if task in ("follow_up_generation", "follow_ups_generation"):
            answer = self._last_answer(chat) if chat else self._history_answer(prompt)
            return json.dumps({"follow_ups": self.follow_up_chips(answer, self.valves.FOLLOWUP_MAX_CHIPS)},
                              ensure_ascii=False)
        if task == "title_generation":
            return json.dumps({"title": self.chat_title(chat or [])}, ensure_ascii=False)
        if task == "tags_generation":
            return json.dumps({"tags": []})
        if task == "query_generation":
            # Web search skips an empty list; the user's own question is the best query.
            last_q = ""
            for m in reversed(chat or []):
                if isinstance(m, dict) and m.get("role") == "user":
                    last_q = " ".join(self._text_of(m.get("content")).split())
                    break
            return json.dumps({"queries": [last_q] if last_q else []}, ensure_ascii=False)
        if task == "emoji_generation":
            return ""
        if task == "autocomplete_generation":
            return json.dumps({"text": ""})
        if task:
            # Any other Open WebUI task: still nothing for the agent to do with tools.
            return "{}"
        return None

    def pipe(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __metadata__: dict = None,
        __event_emitter__=None,
        __task__=None,
        __task_body__=None,
    ) -> Union[str, Generator, Iterator]:
        if self.valves.LOCAL_TASKS:
            meta = dict(__metadata__ or {})
            if __task_body__ and not meta.get("task_body"):
                meta["task_body"] = __task_body__
            task = self._task_of(__task__, meta)
            done = self.local_task(task, body, meta)
            if done is not None:
                logger.info(f"pais pipe: Open WebUI task {task} answered locally ({len(done)} chars)")
                return done
        target_id = body["model"][body["model"].find(".") + 1 :]
        target_assistant = self.get_assistant(target_id)

        if not target_assistant:
            # pipes()가 아직 호출되지 않았거나 캐시가 비어있는 경우 한 번 더 시도
            self.assistants = self.get_all_assistants()
            target_assistant = self.get_assistant(target_id)

        if not target_assistant:
            return "Error: 요청한 Agent를 찾을 수 없습니다."

        try:
            token = self._get_access_token()
        except Exception as e:
            logger.error(f"토큰 발급 중 오류: {e}")
            return f"Error: Authentik 토큰 발급 실패: {str(e)}"

        base_url = self.get_base_url()
        url = f"{base_url}/agents/{target_assistant['id']}/chat/completions"

        # 주의(2번 코드에서 확인된 사항): payload에 "model" 필드를 넣으면
        # PAIS가 400 UNEXPECTED_MODEL 오류를 반환하므로 의도적으로 생략한다.
        payload = {
            "messages": self._shape_history(body.get("messages", [])),
            "stream": body.get("stream", self.valves.ENABLE_STREAMING),
        }
        if self.valves.FORCE_TOOL_CHOICE:
            payload["tool_choice"] = "required"

        client = self._get_http_client()

        def do_request(bearer_token: str):
            headers = self._auth_headers(bearer_token)
            req = client.build_request("POST", url, headers=headers, json=payload)
            resp = client.send(req, stream=payload["stream"])
            return resp

        try:
            response = do_request(token)

            # 토큰이 만료/거부된 경우 한 번 강제 갱신 후 재시도 (커넥션은 그대로 재사용)
            if response.status_code == 401:
                response.close()
                token = self._get_access_token(force_refresh=True)
                response = do_request(token)

            if response.status_code != 200:
                body_text = response.read().decode("utf-8", errors="ignore")
                response.close()
                return f"Error: PAIS 응답 오류 (Status {response.status_code}): {body_text[:1000]}"

            if payload["stream"]:
                return self.stream_generator(response, __event_emitter__)
            else:
                data = response.json()
                response.close()
                return data["choices"][0]["message"]["content"]

        except httpx.TimeoutException as e:
            return (
                f"Error: {self.valves.READ_TIMEOUT}초 후 요청이 시간 초과되었습니다. "
                f"READ_TIMEOUT을 늘리거나 요청을 단순화해보세요. ({e})"
            )
        except httpx.ConnectError as e:
            return f"Error: PAIS에 연결할 수 없습니다. PAIS_API_BASE_URL과 네트워크 상태를 확인하세요. ({e})"
        except Exception as e:
            logger.error(f"예상치 못한 오류: {e}")
            return f"Error: 예상치 못한 오류가 발생했습니다: {str(e)}"

    def stream_generator(self, response, __event_emitter__=None):
        """SSE(data: {...}) 스트림을 파싱해서 delta content만 순차적으로 yield한다.
        공용 persistent client는 여기서 닫지 않고 response만 닫아 커넥션(keep-alive)을
        다음 요청에서도 재사용할 수 있게 한다."""
        import json

        try:
            for line in response.iter_lines():
                if not line:
                    continue
                if line.startswith("data: "):
                    chunk_str = line[len("data: ") :].strip()
                else:
                    chunk_str = line.strip()

                if chunk_str == "[DONE]":
                    break

                try:
                    chunk = json.loads(chunk_str)
                except ValueError:
                    continue

                choices = chunk.get("choices") or []
                if not choices:
                    continue

                delta = choices[0].get("delta", {})
                content = delta.get("content")
                if content:
                    yield content

        except Exception as e:
            logger.error(f"스트리밍 처리 중 오류: {e}")
            yield f"\n\nError during streaming: {str(e)}"
        finally:
            response.close()
