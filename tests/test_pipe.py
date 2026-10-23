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
