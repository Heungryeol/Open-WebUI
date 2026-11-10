"""Offline checks for the PAIS pipe's local work: follow-up chips, the hidden
next-question list, and Open WebUI tasks. No PAIS, no network.

    cd tests && python -m unittest test_pipe
"""
import json
import os
import sys
import types
import unittest

# loguru is only a log sink here; the pipe imports it at load time.
sys.modules.setdefault("loguru", types.SimpleNamespace(logger=types.SimpleNamespace(
    error=lambda *a, **k: None, info=lambda *a, **k: None, warning=lambda *a, **k: None, debug=lambda *a, **k: None)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "functions"))
import pais_multi_agent_pipe as pipe_mod  # noqa: E402

P = pipe_mod.Pipe
KB = ("KB 376473 · Resolution\n\n**근거 일치도: ★★★ 높음**\n\n본문\n\n요약:\nxx\n\n"
      "이어서 물어볼 만한 것:\n- KB 376473 vmkping 으로 vMotion 네트워크 MTU 확인 방법\n"
      "- KB 341568 Packet capture on ESXi using the pktcap-uw tool\n\n이어서 보려면 '나머지 보여줘'")


class Stream:
    """An SSE response cut into small chunks, as PAIS streams it."""

    def __init__(self, text, size=5, fail=False):
        self.parts = [text[i:i + size] for i in range(0, len(text), size)]
        self.fail = fail

    def iter_lines(self):
        for x in self.parts:
            yield "data: " + json.dumps({"choices": [{"delta": {"content": x}}]})
        if self.fail:
            raise RuntimeError("stream cut")
        yield "data: [DONE]"

    def close(self):
        pass


class TestForwardedParams(unittest.TestCase):
    """Open WebUI 의 Advanced Params 중 밸브가 고른 것만 PAIS payload 로 넘어간다.

    2026-11-04: 모델이 머리줄 한 문장을 587회 반복해 78초 동안 21,800자를 쏟고 연결이 끊겼는데
    (BRCM-MCP #271), Controls 에서 max_tokens=2048 을 걸어도 아무 일도 없었다 -- pipe 가 messages 와
    stream 만 담아 보내 그 값이 버려지고 있었다. PAIS 는 모르는 키에 400(UNEXPECTED_MODEL) 을 내므로
    밸브로 하나씩 늘린다.
    """

    def _pipe(self, forward="max_tokens"):
        p = P()
        p.valves.FORWARD_PARAMS = forward
        return p

    def test_the_default_valve_forwards_the_common_openai_params(self):
        # 2026-11-04 159: PAIS 가 max_tokens 를 받아 vLLM 까지 넘기는 것이 확인됐다(400 없음, 답이
        # 21,800자에서 3,970자로 잘림). 나머지도 같은 꼴이라 기본값에 넣고, 400 이 나면 FORWARD_RETRY
        # 가 그 호출만 파라미터 없이 다시 보낸다.
        self.assertEqual(
            P()._forwarded_param_keys(),
            ["max_tokens", "temperature", "top_p", "frequency_penalty", "presence_penalty", "seed", "stop"],
        )

    def test_one_named_key_still_works(self):
        self.assertEqual(self._pipe("max_tokens")._forwarded_param_keys(), ["max_tokens"])

    def test_several_keys_with_blanks_and_duplicates(self):
        p = self._pipe(" max_tokens , temperature ,, max_tokens , top_p ")
        self.assertEqual(p._forwarded_param_keys(), ["max_tokens", "temperature", "top_p"])

    def test_an_empty_valve_forwards_nothing(self):
        for blank in ("", "   ", ",,"):
            with self.subTest(valve=blank):
                self.assertEqual(self._pipe(blank)._forwarded_param_keys(), [])

    def test_the_payloads_own_keys_can_never_be_overwritten(self):
        # PAIS 는 payload 에 model 이 있으면 400 UNEXPECTED_MODEL 을 낸다; messages·stream 은 pipe 가 짓는다.
        p = self._pipe("model, messages, stream, max_tokens")
        self.assertEqual(p._forwarded_param_keys(), ["max_tokens"])


class TestFixedParams(unittest.TestCase):
    """FORWARD_DEFAULTS 가 고정한 값이 Controls 보다 우선한다.

    Controls 는 채팅마다다. 새 채팅은 비어 있고(API, ask --webui) 사용자가 지울 수도 있으므로,
    안전장치(max_tokens)를 Controls 에만 두면 걸리지 않는 경로가 남는다. 2026-11-04 159 에서
    모델이 한 문장을 587회 반복해 연결이 끊겼고(BRCM-MCP #271), 상한이 모든 경로에 걸려야 한다.
    """

    def _pipe(self, fixed="", forward="max_tokens, temperature", fallback=""):
        p = P()
        p.valves.FORWARD_PARAMS = forward
        p.valves.FORWARD_DEFAULTS = fixed
        p.valves.FORWARD_FALLBACK = fallback      # 이 묶음은 고정값·Controls 만 본다
        return p

    def test_no_fixed_value_means_controls_decides(self):
        p = self._pipe()
        self.assertEqual(p.forwarded_values({"max_tokens": 2048}), {"max_tokens": 2048})
        self.assertEqual(p.forwarded_values({}), {})          # 폴백도 비었으면 아무것도 안 보낸다

    def test_the_three_tiers_are_fixed_then_controls_then_fallback(self):
        """FORWARD_DEFAULTS > Controls > FORWARD_FALLBACK.

        폴백이 Controls 아래인 것이 핵심이다: max_tokens 8192 는 안전장치가 아니라 "정상 답이
        안 잘리는 값"(KB 전문 VC_KB_FULL_CHARS=8000자)이라, Controls 에서 일부러 낮춰 보는 것을
        막으면 안 된다. 반대로 고정값은 Controls 를 이겨야 안전장치 노릇을 한다.
        """
        p = self._pipe(fallback="max_tokens=8192")
        self.assertEqual(p.forwarded_values({}), {"max_tokens": 8192})                 # 아무도 안 줌
        self.assertEqual(p.forwarded_values({"max_tokens": 2048}), {"max_tokens": 2048})  # Controls 가 이김
        p = self._pipe(fixed="max_tokens=4096", fallback="max_tokens=8192")
        self.assertEqual(p.forwarded_values({"max_tokens": 2048}), {"max_tokens": 4096})  # 고정값이 이김
        self.assertEqual(p.forwarded_values({}), {"max_tokens": 4096})

    def test_the_shipped_fallback_covers_the_longest_answer_the_servers_can_make(self):
        """기본값은 8192 여야 한다. 4096(약 7,900자)은 KB 전문 8,000자를 아슬아슬하게 자른다.

        환산은 159 실측이다: max_tokens=2048 이 21,800자를 3,970자로 잘랐다 -> 토큰당 약 1.94자.
        """
        self.assertEqual(P().valves.FORWARD_FALLBACK, "max_tokens=8192")
        self.assertGreater(8192 * 1.94, 8000)                 # KB 전문이 들어간다

    def test_a_fallback_key_outside_the_forward_list_is_still_sent(self):
        """폴백에 적은 키는 FORWARD_PARAMS 목록에 없어도 나간다 -- 적은 사람이 뜻한 것이다."""
        p = self._pipe(forward="temperature", fallback="max_tokens=8192")
        self.assertEqual(p.forwarded_values({}), {"max_tokens": 8192})

    def test_an_empty_fallback_sends_nothing(self):
        """비우면 수정 전과 같다 -- 폴백이 강제가 되면 끌 방법이 없어진다."""
        self.assertEqual(self._pipe(fallback="").forwarded_values({}), {})
        self.assertEqual(self._pipe(fallback="   ").forwarded_values({}), {})

    def test_a_fixed_value_wins_over_controls_and_over_an_empty_one(self):
        p = self._pipe("max_tokens=4096")
        self.assertEqual(p.forwarded_values({"max_tokens": 2048}), {"max_tokens": 4096})
        self.assertEqual(p.forwarded_values({}), {"max_tokens": 4096})          # 새 채팅에도 걸린다

    def test_fixed_and_controls_live_side_by_side(self):
        p = self._pipe("max_tokens=4096")
        self.assertEqual(p.forwarded_values({"max_tokens": 1, "temperature": 0.2}),
                         {"max_tokens": 4096, "temperature": 0.2})

    def test_values_keep_their_type(self):
        p = self._pipe("max_tokens=4096, temperature=0.2, seed=7", forward="")
        self.assertEqual(p.forwarded_values({}), {"max_tokens": 4096, "temperature": 0.2, "seed": 7})

    def test_a_fixed_key_is_sent_even_when_the_list_leaves_it_out(self):
        # 적은 사람이 뜻한 것이다; FORWARD_PARAMS 는 Controls 를 거르는 목록이다.
        p = self._pipe("top_k=20", forward="max_tokens")
        self.assertEqual(p.forwarded_values({"top_k": 99}), {"top_k": 20})

    def test_a_broken_piece_is_dropped_not_raised(self):
        p = self._pipe("max_tokens=4096, 이건=, 저건, =3", forward="")
        self.assertEqual(p.forwarded_values({}), {"max_tokens": 4096, "이건": ""})

    def test_the_payloads_own_keys_can_never_be_fixed(self):
        p = self._pipe("model=x, messages=y, stream=false, max_tokens=4096", forward="")
        self.assertEqual(p.forwarded_values({}), {"max_tokens": 4096})


class TestChips(unittest.TestCase):
    def test_the_answer_list_is_copied_with_more_first(self):
        self.assertEqual(P.follow_up_chips(KB), ["나머지 보여줘",
                         "KB 376473 vmkping 으로 vMotion 네트워크 MTU 확인 방법",
                         "KB 341568 Packet capture on ESXi using the pktcap-uw tool"])

    def test_a_finished_document_has_no_more_chip_even_before_the_list(self):
        a = "본문\n\n문서의 끝입니다.\n\n이어서 물어볼 만한 것:\n- 나머지 보여줘\n- KB 320280 요약해줘"
        self.assertEqual(P.follow_up_chips(a), ["KB 320280 요약해줘"])

    def test_no_list_no_chips_and_links_are_not_chips(self):
        self.assertEqual(P.follow_up_chips("| a |\n|---|\n| 1 |"), [])
        self.assertEqual(P.follow_up_chips("관련:\n- X (https://knowledge.broadcom.com/x)\n- KB 1 요약해줘"),
                         ["KB 1 요약해줘"])

    def test_the_models_own_quoted_offers_become_chips_when_there_is_no_list(self):
        # 159 10-30: the kr-ops 3-month onset answer ended with the model's own heading and
        # quoted questions with a gloss -- no "이어서" list, so no chips at all.
        a = ("kr-ops 의 다음 변화 시점은 ...\n\n| 지표 | 시점 |\n|---|---|\n| CPU | 09-29 |\n\n"
             "**분석 요약:**\n9월 29일 ...\n\n추가로 확인이 필요한 사항이 있다면 말씀해 주세요:\n"
             "* \"다음 변화점 보여줘\" (남은 변화점 확인)\n"
             "* \"kr-ops 느려진 원인 분석\" (로그 및 알람 기반 원인 분석)\n"
             "* \"kr-ops 09-29 14:59 원인 분석\" (가장 큰 변화가 있었던 CPU 급증 시점 집중 분석)\n")
        # The gloss stays on the chip (admin, 11-02); the pipe strips it again when the chip comes back.
        self.assertEqual(P.follow_up_chips(a), ["다음 변화점 보여줘 (남은 변화점 확인)", "kr-ops 느려진 원인 분석 (로그 및 알람 기반 원인 분석)",
                                                "kr-ops 09-29 14:59 원인 분석 (가장 큰 변화가 있었던 CPU 급증 시점 집중 분석)"])
        # The server's list still wins when both are there, and a quoted item inside a table is not read.
        both = a + "\n이어서 물어볼 만한 것:\n- 나머지 보여줘\n"
        self.assertEqual(P.follow_up_chips(both), ["나머지 보여줘"])
        self.assertEqual(P.follow_up_chips("표\n- \"a\" (x)\n\n끝 문장입니다."), [])
        # 11-02: backticked items under the model's own heading, with and without a gloss.
        b = ("...\n\n**다음 단계로 무엇을 도와드릴까요?**\n* `sc-nfs01-ds 왜 문제야?` (스토리지 이슈 원인 분석)\n"
             "* `kr-mgmt-cl01 왜 문제야?` (네트워크 이슈 원인 분석)\n* `나머지 보여줘`\n")
        self.assertEqual(P.follow_up_chips(b), ["sc-nfs01-ds 왜 문제야? (스토리지 이슈 원인 분석)", "kr-mgmt-cl01 왜 문제야? (네트워크 이슈 원인 분석)",
                                                "나머지 보여줘"])

    def test_the_models_own_offer_list_is_hidden_from_the_body_and_the_chip_comes_back_bare(self):
        # 11-02 12:57: the list under "다음 단계로 무엇을 도와드릴까요?" showed in the answer AND as chips,
        # and a clicked chip arrived as '"sc-nfs01-ds 왜 문제야?" (스토리지 이슈 원인 분석)'.
        p = P()
        body = ("요약입니다.\n\n---\n**다음 단계로 무엇을 도와드릴까요?**\n* `sc-nfs01-ds 왜 문제야?` (스토리지 이슈 원인 분석)\n"
                "* \"나머지 보여줘\" (진행 중인 WARNING 알람 확인)\n")
        shown = p.strip_next_list(body, "c9")
        self.assertNotIn("다음 단계로", shown)
        self.assertNotIn("sc-nfs01-ds 왜 문제야", shown)
        self.assertTrue(shown.rstrip().endswith("---"))
        chat = [{"role": "user", "content": "q"}, {"role": "assistant", "content": shown}]
        out = p.pipe({"model": "x", "messages": [{"role": "user", "content": "t"}]},
                     __metadata__={"task": "follow_up_generation", "chat_id": "c9", "task_body": {"messages": chat, "chat_id": "c9"}})
        self.assertEqual(json.loads(out)["follow_ups"], ["sc-nfs01-ds 왜 문제야? (스토리지 이슈 원인 분석)", "나머지 보여줘 (진행 중인 WARNING 알람 확인)"])
        # The chip comes back: its gloss goes, so the server sees the bare sentence; so does the quoted form.
        self.assertEqual(p._unwrap_chip("sc-nfs01-ds 왜 문제야? (스토리지 이슈 원인 분석)"), "sc-nfs01-ds 왜 문제야?")
        self.assertEqual(p._unwrap_chip('"sc-nfs01-ds 왜 문제야?" (스토리지 이슈 원인 분석)'), "sc-nfs01-ds 왜 문제야?")
        # A typed question with its own parenthesis is not a chip and stays whole.
        self.assertEqual(p._unwrap_chip("CPU 사용률 (지난 7일) 보여줘"), "CPU 사용률 (지난 7일) 보여줘")
        # A heading followed by ordinary text is not an offer list: nothing hidden.
        kept = p.strip_next_list("답.\n\n**확인이 필요한 사항:**\n네트워크를 점검하세요.\n", "c10")
        self.assertIn("확인이 필요한 사항", kept)
        self.assertIn("네트워크를 점검하세요", kept)

    def test_the_models_paging_sentence_and_reworded_heading_still_give_chips(self):
        # 159 11-02 13:31: "나머지 1건을 보려면 "나머지 보여줘"라고 말씀해 주세요." and no list -> no chips.
        self.assertEqual(P.follow_up_chips("표\n\n나머지 1건을 보려면 \"나머지 보여줘\"라고 말씀해 주세요."), ["나머지 보여줘"])
        # 03:55: the model reworded the heading and wrote plain items.
        a = "답.\n\n**이어서 다음과 같이 질문하실 수 있습니다:**\n*   진행 중 알림 간트 차트로 그려줘\n*   진행 중 알림 보여줘\n*   나머지 보여줘 (INFORMATION 등 낮은 단계 알림 확인)\n"
        self.assertEqual(P.follow_up_chips(a), ["진행 중 알림 간트 차트로 그려줘", "진행 중 알림 보여줘", "나머지 보여줘 (INFORMATION 등 낮은 단계 알림 확인)"])
        p = P()
        shown = p.strip_next_list("답.\n\n**다음 단계로 무엇을 도와드릴까요?**\n* 진행 중 알림 간트 차트로 그려줘\n* 나머지 보여줘\n", "c12")
        self.assertEqual(shown.strip(), "답.")
        self.assertEqual(p._next_lists["c12"], ["- 진행 중 알림 간트 차트로 그려줘", "- 나머지 보여줘"])

    def test_the_offer_list_is_hidden_from_a_stream_too(self):
        p = P()
        text = "답.\n\n**추천 질문:**\n* `esx09 알림 보여줘` (경고 확인)\n* `esx09 변화 시점 그래프 보여줘`"
        out = "".join(p.stream_generator(Stream(text), None, "c11"))
        self.assertEqual(out.strip(), "답.")
        self.assertEqual(p.follow_up_chips("x\n\n이어서 물어볼 만한 것:\n" + "\n".join(p._next_lists["c11"])),
                         ["esx09 알림 보여줘 (경고 확인)", "esx09 변화 시점 그래프 보여줘"])

    def test_zero_left_is_not_more(self):
        self.assertEqual(P.follow_up_chips("나머지 0개는 '나머지 보여줘' 로.\n\n이어서 물어볼 만한 것:\n- a"), ["a"])

    def test_an_answer_kept_under_output_is_read(self):
        chat = [{"role": "user", "content": "q"},
                {"role": "assistant", "content": "", "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": "x\n\n이어서 물어볼 만한 것:\n- a"}]}]}]
        out = P().pipe({"model": "x", "messages": [{"role": "user", "content": "t"}]},
                       __metadata__={"task": "follow_up_generation", "task_body": {"messages": chat}})
        self.assertEqual(json.loads(out)["follow_ups"], ["a"])


class TestHiddenList(unittest.TestCase):
    def test_the_list_is_hidden_from_the_stream_and_becomes_the_chips(self):
        p = P()
        shown = "".join(p.stream_generator(Stream(KB), None, "c1"))
        self.assertNotIn("이어서 물어볼 만한 것", shown)
        self.assertIn("이어서 보려면 '나머지 보여줘'", shown)
        chat = [{"role": "user", "content": "q"}, {"role": "assistant", "content": shown}]
        out = p.pipe({"model": "x", "messages": [{"role": "user", "content": "t"}]},
                     __metadata__={"task": "follow_up_generation", "chat_id": "c1",
                                   "task_body": {"messages": chat, "chat_id": "c1"}})
        self.assertEqual(json.loads(out)["follow_ups"][0], "나머지 보여줘")
        self.assertEqual(len(json.loads(out)["follow_ups"]), 3)

    def test_a_broken_stream_forgets_the_earlier_list(self):
        p = P()
        p._next_lists["c2"] = ["- old chip"]
        list(p.stream_generator(Stream("부분 답\n", fail=True), None, "c2"))
        self.assertNotIn("c2", p._next_lists)

    def test_lists_stay_when_hiding_is_off(self):
        p = P()
        p.valves.HIDE_NEXT_LIST = False
        self.assertIsNone(p._hiding({"chat_id": "c"}))


class TestTasks(unittest.TestCase):
    def test_tasks_never_reach_pais(self):
        p = P()
        meta = {"task": "tags_generation"}
        self.assertEqual(json.loads(p.pipe({"model": "x", "messages": []}, __metadata__=meta)), {"tags": []})
        t = p.pipe({"model": "x", "messages": []}, __task__="title_generation",
                   __task_body__={"messages": [{"role": "user", "content": "VM 목록 보여줘"}]})
        self.assertEqual(json.loads(t)["title"], "VM 목록 보여줘")

    def test_a_normal_question_is_not_a_task(self):
        self.assertIsNone(P().local_task("", {"metadata": None}, None))


SPACE = """esx09-datastore-bay8: 전체 3577 GB 중 3431 GB 사용(95.9%), 남은 146 GB.

| VM | 사용량(GB) | 할당 용량(GB) | 전원 |
|---|---|---|---|
| 261018-esx09-vcf911-esx-01a | 932 GB | 1060 GB | poweredOn |
| 261018-esx09-vcf911-esx-02a | 916 GB | 1060 GB | poweredOn |

```mermaid
pie showData
  title 『 esx09-datastore-bay8 공간 구성(GB) 』
  "261018-esx09-vcf911-esx-01a" : 932
  "여유" : 146
```
"""
DFW = """'sc-fill01' 와 'sc-mig01' 에 걸리는 분산 방화벽 규칙 5개 (L3 의 DROP/REJECT 1개)

| category | policy | seq | rule | source | destination | services | action | hits |
|---|---|---|---|---|---|---|---|---|
| Application | sc-showcase | 10 | sc-deny-http | sc-mig01 | sc-fill01 | HTTP | REJECT | - |
"""


def chat(*turns):
    """user, assistant, user, ... -> the messages Open WebUI sends."""
    return [{"role": "assistant" if i % 2 else "user", "content": t} for i, t in enumerate(turns)]


class TestEarlierDataIsNotLeftToCopy(unittest.TestCase):
    """159, 2026-11-10: an eleven-turn chat replayed through the agent. Turn 5 ("… 용량을 가장 많이
    차지하고 있는 개체는?") and turn 9 ("DFW 룰 확인해줘") made no tool call -- the gateway journal has
    no CALL line for either -- and answered with tables: 1,250/1,100/500/579 GB where vCenter had
    just said 932/916/898/684, and four rules named sc-allow-all that NSX does not have. Both
    tables used the header row of the answer before them."""

    def setUp(self):
        self.pipe = P()

    def shaped(self, *turns):
        return self.pipe._shape_history(chat(*turns))

    def test_the_question_after_a_table_is_told_the_values_are_gone(self):
        for question in ("esx09-datastore-bay8 데이터스토어 용량을 가장 많이 차지하고 있는 개체는?",
                         "DFW 룰 확인해줘", "sc-fill01 VM 통신 문제가 있어"):
            out = self.shaped("esx09-datastore-bay8 데이터스토어 용량이 부족한 이유는?", SPACE, question)
            self.assertEqual(out[-1]["content"], f"{question}\n{P._STALE_HINT}", question)

    def test_an_earlier_chart_block_goes_with_its_numbers(self):
        sent = self.shaped("용량이 부족한 이유는?", SPACE, "DFW 룰 확인해줘")[1]["content"]
        self.assertNotIn("mermaid", sent)
        self.assertNotIn(": 932", sent)
        self.assertIn(P._CHART_STUB, sent)
        # A chart-only answer ("… 원형 그래프로 그려줘") has no table row to trigger on.
        only = "```mermaid\npie showData\n  \"a\" : 1\n```"
        out = self.shaped("원형 그래프로 그려줘", only, "esx09 상태는?")
        self.assertEqual(out[1]["content"], P._CHART_STUB)
        self.assertTrue(out[-1]["content"].endswith(P._STALE_HINT))

    def test_a_chart_cut_before_its_closing_fence_goes_too(self):
        sent = self.shaped("q", "앞 문장\n```mermaid\npie showData\n  \"a\" : 932", "다른 질문이야")[1]["content"]
        self.assertEqual(sent, "앞 문장\n" + P._CHART_STUB)

    def test_the_header_row_is_not_left_as_a_template(self):
        sent = self.shaped("두 VM 통신 확인", DFW, "DFW 룰 확인해줘")[1]["content"]
        self.assertNotIn("| category | policy |", sent)
        self.assertIn("(표 1행 생략 — 첫 열: Application; 지금 값은 도구로 다시 조회)", sent)
        self.assertIn("분산 방화벽 규칙 5개", sent)            # the sentence around the table stays
        self.pipe.valves.KEEP_TABLE_HEADER = True
        self.assertIn("| category | policy |", self.shaped("두 VM 통신 확인", DFW, "DFW 룰 확인해줘")[1]["content"])

    def test_a_kb_answer_counts_as_data_left_out(self):
        out = self.shaped("vMotion 실패 KB", KB, "kr-mgmt-cl01 vMotion VMkernel MTU 보여줘")
        self.assertTrue(out[-1]["content"].endswith(P._STALE_HINT))

    def test_no_line_when_nothing_was_left_out(self):
        self.assertEqual(self.shaped("esx09 상태는?")[-1]["content"], "esx09 상태는?")
        out = self.shaped("안녕", "안녕하세요. 무엇을 도와드릴까요?", "esx09 상태는?")
        self.assertEqual(out[-1]["content"], "esx09 상태는?")

    def test_thanks_and_greetings_get_no_line(self):
        # A hint on a greeting made the model call a tool for it (2nd QA M-B).
        for word in ("고마워", "감사합니다", "네 알겠습니다"):
            self.assertEqual(self.shaped("q", SPACE, word)[-1]["content"], word, word)

    def test_one_hint_per_message(self):
        paging = self.shaped("q", SPACE, "나머지 보여줘")[-1]["content"]
        self.assertNotIn(P._STALE_HINT, paging)
        same = self.shaped("esx09 데이터스토어 용량 보여줘", SPACE, "esx09 데이터스토어 용량 보여줘")[-1]["content"]
        self.assertIn(P._SAME_QUESTION, same)
        self.assertNotIn(P._STALE_HINT, same)
        how = self.shaped("q", SPACE, "esx09 데이터스토어 용량 늘리는 방법 알려줘")[-1]["content"]
        self.assertIn(P._TOOL_HINT, how)
        self.assertNotIn(P._STALE_HINT, how)
        short = self.shaped("계정 잠김", SPACE, "잠금 해제")[-1]["content"]
        self.assertIn("앞 질문", short)
        self.assertNotIn(P._STALE_HINT, short)

    def test_the_valve_turns_it_off(self):
        self.pipe.valves.STALE_DATA_HINT = False
        self.assertEqual(self.shaped("q", SPACE, "DFW 룰 확인해줘")[-1]["content"], "DFW 룰 확인해줘")

    def test_full_history_is_untouched(self):
        self.pipe.valves.HISTORY_MODE = "full"
        messages = chat("q", SPACE, "DFW 룰 확인해줘")
        self.assertIs(self.pipe._shape_history(messages), messages)


if __name__ == "__main__":
    unittest.main()
