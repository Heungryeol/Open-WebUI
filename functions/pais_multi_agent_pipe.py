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
        FOLLOWUP_HINT: bool = Field(
            default=True,
            description="짧은 후속 질문('잠금 해제 방법' 등)에 앞 질문을 붙여 도구 호출을 유도.",
        )
        FOLLOWUP_MAX_CHARS: int = Field(
            default=25,
            description="이 글자 수 이하의 질문을 후속 질문으로 본다.",
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
    _TABLE_STUB = "(이전 표 답, 본문 생략 — 지금 값은 도구로 다시 조회)"
    _SAME_QUESTION = "(같은 질문: 값이 바뀌었을 수 있으니 도구를 다시 호출)"

    @staticmethod
    def _same_text(text: str) -> str:
        # Spacing, case, punctuation and a typed "Q4" label do not make a new question.
        import re

        bare = re.sub(r"^\s*Q\d+[.):]?\s*", "", text or "")
        return re.sub(r"[\s.,!?~·]+", "", bare).lower()

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
            elif self.valves.STRIP_TABLES and table_mark.search(text):
                # Declarative, like the KB stub: values in an old table are stale
                # by the next question, and a table in view was copied or extended.
                shaped.append({"role": "assistant", "content": self._TABLE_STUB})
            else:
                limit = max(0, int(self.valves.KEEP_ANSWER_CHARS))
                cut = text if len(text) <= limit else text[:limit] + " …(생략)"
                shaped.append({"role": "assistant", "content": cut})
        current = turns[last_user]
        # "나머지 보여줘" with no earlier text to copy made the model write a
        # paragraph about `more` values instead of calling the tool (10-22).
        # Spell out the call it should make; the server keeps the page.
        cur_text = self._text_of(current.get("content")).strip()
        bare = re.sub(r"^\s*Q\d+[.):]?\s*", "", cur_text)
        is_paging = bool(
            re.fullmatch(r"(나머지|다음|더)\s*(보여\s*줘|보여\s*주세요|줘|보기)?[.!?]?|more|next", bare, re.I)
        )
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
                    prev_q = re.sub(r"^\s*Q\d+[.):]?\s*", "", self._text_of(m.get("content"))).strip()
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
            import difflib

            now = self._same_text(bare)
            for m in turns[:last_user]:
                if m.get("role") != "user":
                    continue
                before = self._same_text(self._text_of(m.get("content")))
                # Near-equal only with the same numbers: "esx08 CPU 사용률" and
                # "esx09 CPU 사용률" differ by one character and are not the same question.
                if len(now) >= 4 and (
                    before == now
                    or (
                        re.findall(r"\d+", before) == re.findall(r"\d+", now)
                        and difflib.SequenceMatcher(None, before, now).ratio() >= 0.9
                    )
                ):
                    current = {
                        "role": "user",
                        "content": f"{self._text_of(current.get('content'))}\n{self._SAME_QUESTION}",
                    }
                    break
        return system + shaped + [current]

    # ------------------------------------------------------------------
    # Chat Completion 호출
    # ------------------------------------------------------------------
    def pipe(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __metadata__: dict = None,
        __event_emitter__=None,
    ) -> Union[str, Generator, Iterator]:
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
