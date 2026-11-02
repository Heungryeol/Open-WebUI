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


if __name__ == "__main__":
    unittest.main()
