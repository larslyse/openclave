"""Streaming on the API routes (Rick's backlog, 2026-10-13): the reply text
shows up in request_phase as it arrives, tool calls still work when they
come in pieces, and Stop ends an API reply at its next chunk."""
import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import llm_client
from providers import xai_api
import request_phase

AGENT = "stream-test"


def chunk(content=None, calls=None, finish=None, usage=None):
    if usage:
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1]), choices=[])
    return SimpleNamespace(usage=None, choices=[SimpleNamespace(
        delta=SimpleNamespace(content=content, tool_calls=calls), finish_reason=finish)])


def call_piece(index, id=None, name=None, args=None):
    return SimpleNamespace(index=index, id=id, function=SimpleNamespace(name=name, arguments=args))


class Watched(list):
    """A stream that records request_phase's text after each chunk is read."""
    def __init__(self, items, seen):
        super().__init__(items)
        self.seen = seen

    def __iter__(self):
        for item in list.__iter__(self):
            yield item
            self.seen.append(request_phase.get_text(AGENT))


class XaiStreamingTests(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("XAI_API_KEY", "test-key")
        for p in (patch.object(xai_api, "log_usage"), patch.object(xai_api, "tools_for", return_value=[])):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(request_phase.set_phase, AGENT, None)

    def run_xai(self, rounds, tool_result="42"):
        sent, seen = [], []
        client = MagicMock()
        streams = iter(rounds)
        def create(**kw):
            sent.append({**kw, "messages": list(kw["messages"])})
            return Watched(next(streams), seen)
        client.chat.completions.create.side_effect = create
        fake_openai = MagicMock()
        fake_openai.OpenAI.return_value = client
        with patch.dict("sys.modules", {"openai": fake_openai}), \
             patch.object(xai_api, "_run_tool", return_value=tool_result) as run_tool:
            reply = llm_client._chat_xai(AGENT, "grok-4.3", "sys", [{"role": "user", "content": "hi"}])
        return reply, sent, seen, run_tool

    def test_text_shows_while_it_streams(self):
        reply, sent, seen, _ = self.run_xai([[chunk("Hel"), chunk("lo"), chunk(" there", finish="stop"), chunk(usage=(5, 3))]])
        self.assertEqual(reply, "Hello there")
        self.assertEqual(seen[:3], ["Hel", "Hello", "Hello there"])
        self.assertTrue(sent[0]["stream"])
        self.assertEqual(sent[0]["stream_options"], {"include_usage": True})

    def test_tool_calls_in_pieces_still_run(self):
        first = [
            chunk("Let me check."),
            chunk(calls=[call_piece(0, id="c1", name="read_note", args='{"pa')]),
            chunk(calls=[call_piece(0, args='th": "a.md"}')]),
            chunk(calls=[call_piece(1, id="c2", name="list_notes", args="{}")], finish="tool_calls"),
        ]
        second = [chunk("Done", finish="stop")]
        reply, sent, seen, run_tool = self.run_xai([first, second])
        self.assertEqual(reply, "Done")
        self.assertEqual([c.args for c in run_tool.call_args_list],
                         [(AGENT, "read_note", {"path": "a.md"}), (AGENT, "list_notes", {})])
        convo = sent[1]["messages"]
        self.assertEqual(convo[-3]["tool_calls"][0]["id"], "c1")
        self.assertEqual(convo[-2], {"role": "tool", "tool_call_id": "c1", "content": "42"})
        # The second round starts fresh: "Let me check." isn't part of the reply.
        self.assertEqual(seen[-1], "Done")

    def test_stop_ends_the_reply_at_the_next_chunk(self):
        def stopping():
            yield chunk("partial ")
            llm_client.cancel_running(AGENT)
            yield chunk("never shown", finish="stop")
        client = MagicMock()
        client.chat.completions.create.side_effect = lambda **kw: stopping()
        fake_openai = MagicMock()
        fake_openai.OpenAI.return_value = client
        with patch.dict("sys.modules", {"openai": fake_openai}):
            with self.assertRaises(llm_client.StoppedByOwner):
                llm_client._chat_xai(AGENT, "grok-4.3", "sys", [{"role": "user", "content": "hi"}])
        self.assertEqual(request_phase.get_text(AGENT), "partial ")
        self.assertFalse(llm_client.cancel_running(AGENT), "nothing left running")


class FakeAnthropicStream:
    def __init__(self, texts, final, on_event=None):
        self.texts, self.final, self.on_event = texts, final, on_event

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for t in self.texts:
            if self.on_event:
                self.on_event(t)
            yield SimpleNamespace(type="text", text=t)

    def get_final_message(self):
        return self.final


def final_message(text, stop_reason="end_turn"):
    usage = SimpleNamespace(input_tokens=1, output_tokens=1, cache_creation_input_tokens=0, cache_read_input_tokens=0)
    return SimpleNamespace(stop_reason=stop_reason, usage=usage, content=[SimpleNamespace(type="text", text=text)])


class AnthropicStreamingTests(unittest.TestCase):
    def setUp(self):
        for p in (patch.object(llm_client, "log_usage"), patch.object(llm_client, "tools_for", return_value=[])):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(request_phase.set_phase, AGENT, None)

    def run_anthropic(self, stream):
        client = MagicMock()
        client.messages.stream.return_value = stream
        fake = MagicMock()
        fake.Anthropic.return_value = client
        with patch.dict("sys.modules", {"anthropic": fake}):
            reply = llm_client._chat_anthropic(AGENT, "claude-sonnet-5", "sys", [{"role": "user", "content": "hi"}],
                                               max_tokens=100, effort="low")
        return reply, client.messages.stream.call_args.kwargs

    def test_text_shows_while_it_streams(self):
        seen = []
        stream = FakeAnthropicStream(["Hi ", "Sam"], final_message("Hi Sam"),
                                     on_event=lambda t: seen.append(request_phase.get_text(AGENT)))
        reply, kwargs = self.run_anthropic(stream)
        self.assertEqual(reply, "Hi Sam")
        self.assertEqual(seen, ["", "Hi "])  # read before each chunk is added
        self.assertEqual(request_phase.get_text(AGENT), "Hi Sam")
        self.assertEqual(kwargs["max_tokens"], 100)
        self.assertEqual(kwargs["output_config"], {"effort": "low"})

    def test_tool_round_sends_back_what_the_api_accepts(self):
        # The SDK's streamed text blocks carry parsed_output, which the API
        # rejects when the assistant turn is sent back (400 seen live).
        from anthropic.types import ParsedTextBlock, ToolUseBlock
        usage = SimpleNamespace(input_tokens=1, output_tokens=1, cache_creation_input_tokens=0, cache_read_input_tokens=0)
        first = SimpleNamespace(stop_reason="tool_use", usage=usage, content=[
            ParsedTextBlock(type="text", text="Checking.", parsed_output=None),
            ToolUseBlock(type="tool_use", id="t1", name="list_notes", input={})])
        streams = iter([FakeAnthropicStream(["Checking."], first), FakeAnthropicStream(["Done"], final_message("Done"))])
        client = MagicMock()
        sent = []
        def stream(**kw):
            sent.append([dict(m) for m in kw["messages"]])
            return next(streams)
        client.messages.stream.side_effect = stream
        fake = MagicMock()
        fake.Anthropic.return_value = client
        with patch.dict("sys.modules", {"anthropic": fake}), patch.object(llm_client, "_run_tool", return_value="none"):
            reply = llm_client._chat_anthropic(AGENT, "claude-sonnet-5", "sys", [{"role": "user", "content": "hi"}])
        self.assertEqual(reply, "Done")
        echoed = sent[1][1]["content"]
        self.assertEqual([b["type"] for b in echoed], ["text", "tool_use"])
        self.assertNotIn("parsed_output", echoed[0])

    def test_stop(self):
        stream = FakeAnthropicStream(["a", "b"], final_message("ab"),
                                     on_event=lambda t: t == "b" and llm_client.cancel_running(AGENT))
        with self.assertRaises(llm_client.StoppedByOwner):
            self.run_anthropic(stream)
        self.assertNotIn(AGENT, llm_client._API_STOPS)


class PhaseTextTests(unittest.TestCase):
    def test_cleared_with_the_phase(self):
        request_phase.set_phase(AGENT, "thinking")
        request_phase.add_text(AGENT, "abc")
        self.assertEqual(request_phase.get_text(AGENT), "abc")
        request_phase.set_phase(AGENT, None)
        self.assertEqual(request_phase.get_text(AGENT), "")


class StoppedJobTests(unittest.TestCase):
    """A stopped API reply ends the request as cancelled, like a CLI one."""

    def test_api_stop_cancels_the_request(self):
        import chat_jobs
        started, release = threading.Event(), threading.Event()
        record = {"request_id": "r1", "status": "queued", "conversation_id": "c1", "message": "hi"}
        saved = []

        def fake_run(agent_id, conversation_id, message, attachments):
            with llm_client._api_run(agent_id) as stop:
                started.set()
                release.wait(5)
                llm_client._check_stop(stop)
            return "never"

        with patch.object(chat_jobs, "_load_full", return_value=dict(record)), \
             patch.object(chat_jobs, "_save", side_effect=lambda a, r: saved.append(dict(r))), \
             patch.object(chat_jobs, "_request_file") as request_file, \
             patch.object(chat_jobs.chat_service, "agent_execution", MagicMock()), \
             patch.object(chat_jobs.chat_service, "run_chat_locked", side_effect=fake_run), \
             patch.object(chat_jobs.vault, "append_turn") as append_turn, \
             patch.object(chat_jobs, "_push_reply"):
            request_file.return_value.exists.return_value = True
            worker = threading.Thread(target=chat_jobs._run, args=(AGENT, "r1"))
            worker.start()
            self.assertTrue(started.wait(5))
            with patch.object(chat_jobs, "_load_full", return_value={**record, "status": "running"}):
                result = chat_jobs.cancel(AGENT, "r1")
            release.set()
            worker.join(5)
        self.assertTrue(result["stoppable"])
        self.assertEqual(saved[-1]["status"], "cancelled")
        append_turn.assert_called_once()


if __name__ == "__main__":
    unittest.main()
