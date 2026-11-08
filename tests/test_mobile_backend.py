import json
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import chat_jobs
import chat_service
import vault


class ImmediateExecutor:
    def submit(self, fn, *args):
        fn(*args)


class MobileBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_agents_root = vault.AGENTS_ROOT
        self.old_vault_root = vault.VAULT_ROOT
        vault.VAULT_ROOT = Path(self.temp.name).resolve()
        vault.AGENTS_ROOT = (vault.VAULT_ROOT / "OpenClave" / "Agents").resolve()
        root = vault.agent_root("reba")
        root.mkdir(parents=True)
        for name in ("mission.md", "persona.md", "backlog.md", "plan.md"):
            (root / name).write_text("", encoding="utf-8")

    def tearDown(self):
        vault.AGENTS_ROOT = self.old_agents_root
        vault.VAULT_ROOT = self.old_vault_root
        self.temp.cleanup()

    def test_conversation_traversal_is_rejected(self):
        with self.assertRaises(ValueError):
            vault.append_turn("reba", "../mission", "user", "bad")

    def test_new_conversation_ids_do_not_collide(self):
        self.assertNotEqual(vault.new_conversation_id(), vault.new_conversation_id())

    def test_conversations_are_sorted_by_latest_activity(self):
        older = vault.conversation_file("reba", "20260917-100000-aaaaaaaa")
        newer = vault.conversation_file("reba", "20260917-110000-bbbbbbbb")
        older.parent.mkdir(parents=True, exist_ok=True)
        older.write_text("---\nTitle: Older thread\nDate: 2026-09-17\n---\n")
        newer.write_text("---\nTitle: Newer thread\nDate: 2026-09-17\n---\n")

        # The older-created thread was used more recently.
        os.utime(newer, ns=(1_000_000_000, 1_000_000_000))
        os.utime(older, ns=(2_000_000_000, 2_000_000_000))

        conversations = vault.list_conversations("reba")
        self.assertEqual(
            [item["id"] for item in conversations],
            ["20260917-100000-aaaaaaaa", "20260917-110000-bbbbbbbb"],
        )

    def test_concurrent_appends_keep_both_turns(self):
        conversation_id = vault.new_conversation_id()
        barrier = threading.Barrier(2)

        def append(text):
            barrier.wait()
            vault.append_turn("reba", conversation_id, "user", text, first_message=text)

        threads = [threading.Thread(target=append, args=(text,)) for text in ("one", "two")]
        for thread in threads: thread.start()
        for thread in threads: thread.join()

        turns = vault.get_conversation("reba", conversation_id)["turns"]
        self.assertEqual({turn["content"] for turn in turns}, {"one", "two"})

    def test_requests_queue_while_agent_thinks_and_run_in_order(self):
        started = threading.Event()
        release = threading.Event()
        calls = []
        conversation_id = vault.new_conversation_id()

        def reply(agent_id, cid, message, attachments):
            calls.append((message, attachments))
            if message == "first":
                started.set()
                self.assertTrue(release.wait(5))
            if message == "second":
                raise RuntimeError("test failure")

        with ThreadPoolExecutor(max_workers=3) as executor:
            with patch.object(chat_jobs, "_EXECUTOR", executor), \
                 patch.object(chat_service, "run_chat_locked", side_effect=reply):
                chat_jobs.submit("reba", "queue_first", conversation_id, "first")
                try:
                    self.assertTrue(started.wait(5))
                    second, _ = chat_jobs.submit("reba", "queue_second", conversation_id, "second")
                    third, _ = chat_jobs.submit("reba", "queue_third", conversation_id, "", ["photo1234"])
                    duplicate, created = chat_jobs.submit("reba", "queue_third", conversation_id, "", ["photo1234"])
                    self.assertEqual(second["status"], "queued")
                    self.assertEqual(third["status"], "queued")
                    self.assertFalse(created)
                    self.assertEqual(calls, [("first", [])])
                finally:
                    release.set()
                executor.shutdown(wait=True)
        self.assertEqual(calls, [("first", []), ("second", []), ("", ["photo1234"])])
        self.assertEqual(chat_jobs.get("reba", "queue_second")["status"], "failed")
        self.assertEqual(chat_jobs.get("reba", "queue_third")["status"], "completed")

    def test_restart_drains_saved_queue_in_order(self):
        cid = vault.new_conversation_id()
        with patch.object(chat_jobs._EXECUTOR, "submit"):
            chat_jobs.submit("reba", "recover_a", cid, "first")
            chat_jobs.submit("reba", "recover_b", cid, "second")
        chat_jobs._SCHEDULED.clear()  # Simulate new process memory.
        with patch.object(chat_jobs, "_EXECUTOR", ImmediateExecutor()), \
             patch.object(chat_service, "run_chat_locked") as run:
            chat_jobs.recover()
        self.assertEqual([call.args[2] for call in run.call_args_list], ["first", "second"])
        self.assertEqual(chat_jobs.get("reba", "recover_b")["status"], "completed")

    def test_delete_preserves_queued_conversation_and_photos(self):
        cid = vault.new_conversation_id()
        vault.append_turn("reba", cid, "user", "hello")
        meta = vault.save_attachment("reba", cid, "photo.png", "image/png", b"photo")
        with patch.object(chat_jobs._EXECUTOR, "submit"):
            chat_jobs.submit("reba", "delete_pending", cid, "", [meta["id"]])
        try:
            with self.assertRaises(chat_jobs.AgentBusyError):
                chat_jobs.delete_conversation("reba", cid)
            self.assertIsNotNone(vault.get_conversation("reba", cid))
            self.assertIsNotNone(vault.read_attachment("reba", cid, meta["id"]))
            with patch.object(chat_service, "run_chat_locked"):
                chat_jobs._drain("reba")
            self.assertTrue(chat_jobs.delete_conversation("reba", cid))
            self.assertIsNone(vault.get_conversation("reba", cid))
            self.assertFalse(vault.attachments_dir("reba", cid).exists())
        finally:
            chat_jobs._SCHEDULED.discard("reba")

    def test_all_agents_have_voice_routes_and_default_voice_works(self):
        import main
        routes = {r.path for r in main.app.routes}
        for agent_id in main.AGENTS:
            self.assertIn(f"/{agent_id}/voice/transcribe", routes)
            self.assertIn(f"/{agent_id}/voice/reply", routes)
        with patch.object(chat_service, "_voice_system_prompt", return_value="voice"), \
             patch.object(chat_service.llm_client, "chat", return_value="Hello"), \
             patch.object(chat_service.voice_engine, "synthesize") as synth:
            result = chat_service.run_voice_reply_locked("reba", vault.new_conversation_id(), "Hi")
        try:
            synth.assert_called_once_with("Hello", result["reply_audio_path"])
            self.assertEqual(result["audio_mime"], "audio/wav")
        finally:
            Path(result["reply_audio_path"]).unlink(missing_ok=True)

    def test_reply_records_which_model_answered(self):
        import llm_models
        cid = vault.new_conversation_id()
        vault.append_turn("reba", cid, "user", "Hi", first_message="Hi")
        vault.append_turn("reba", cid, "assistant", "Hello", timings={"elapsed_ms": 1500, "model_id": "claude-sonnet-5"})
        turn = vault.get_conversation("reba", cid)["turns"][-1]
        self.assertEqual((turn["content"], turn["elapsed_ms"], turn["model_id"]), ("Hello", 1500, "claude-sonnet-5"))
        self.assertEqual(llm_models.display_name("claude-sonnet-5"), llm_models.get("claude-sonnet-5").display_name)
        self.assertIsNone(llm_models.display_name("no-such-model"))
        self.assertIsNone(llm_models.display_name(None))
        self.assertFalse(turn["voice"])

    def test_voice_replies_are_labelled_as_voice(self):
        cid = vault.new_conversation_id()
        vault.append_turn("reba", cid, "user", "Hi", first_message="Hi")
        vault.append_turn("reba", cid, "assistant", "Hei", timings={"provider_ms": 1, "synthesize_ms": 2, "elapsed_ms": 3,
                                                                  "model_id": chat_service.VOICE_MODEL_ID})
        turn = vault.get_conversation("reba", cid)["turns"][-1]
        self.assertTrue(turn["voice"])
        label = chat_service.reply_model_label(turn["model_id"], voice=turn["voice"])
        self.assertTrue(label.startswith("Voice · "))
        self.assertEqual(chat_service.reply_model_label("claude-sonnet-5"), "Claude Sonnet 5")

    def test_norwegian_voice_turn(self):
        import dataclasses
        cfg = patch.object(chat_service, "CONFIG", dataclasses.replace(chat_service.CONFIG, voice_second_language="no", voice_second_language_name="Norwegian (bokmål)", voice_second_voice={"provider": "fish_audio", "voice_id": "ffffffffffffffffffffffffffffffff"}))
        cfg.start()
        self.addCleanup(cfg.stop)
        self.assertEqual(chat_service.voice_languages(), ["en", "no"])
        with patch.object(chat_service.voice_engine, "transcribe", return_value="Hei") as stt:
            self.assertEqual(chat_service.transcribe_voice_audio("reba", "/tmp/x.webm", "no"), "Hei")
        stt.assert_called_once_with("/tmp/x.webm", "no")
        self.assertIn("Reply in natural Norwegian", chat_service._voice_system_prompt("reba", None, "no"))
        self.assertNotIn("Norwegian", chat_service._voice_system_prompt("reba", None, "en"))
        with patch.object(chat_service, "_voice_system_prompt", return_value="voice") as prompt, \
             patch.object(chat_service.llm_client, "chat", return_value="Hei der"), \
             patch.object(chat_service.fish_audio_tts, "synthesize") as fish, \
             patch.object(chat_service.voice_engine, "synthesize") as kokoro:
            result = chat_service.run_voice_reply_locked("reba", vault.new_conversation_id(), "Hei", language="no")
        try:
            self.assertEqual(prompt.call_args.args[2], "no")
            kokoro.assert_not_called()
            fish.assert_called_once_with("Hei der", "f" * 32, result["reply_audio_path"])
            self.assertEqual(result["audio_mime"], "audio/mpeg")
        finally:
            Path(result["reply_audio_path"]).unlink(missing_ok=True)

    def test_english_only_without_a_second_language(self):
        import dataclasses
        with patch.object(chat_service, "CONFIG", dataclasses.replace(chat_service.CONFIG, voice_second_language="")):
            self.assertEqual(chat_service.voice_languages(), ["en"])
            self.assertNotIn("# Language", chat_service._voice_system_prompt("reba", None, "no"))

    def test_durable_request_is_idempotent(self):
        with patch.object(chat_jobs, "_EXECUTOR", ImmediateExecutor()), \
             patch.object(chat_service.retrieval, "build_context", return_value="none"), \
             patch.object(chat_service.llm_client, "maybe_compact_conversation"), \
             patch.object(chat_service.llm_client, "chat", return_value="reply"):
            first, created = chat_jobs.submit("reba", "req_123", None, "hello")
            second, created_again = chat_jobs.submit("reba", "req_123", None, "hello")

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["conversation_id"], second["conversation_id"])
        self.assertEqual(second["status"], "completed")
        conversation = vault.get_conversation("reba", first["conversation_id"])
        self.assertEqual([t["content"] for t in conversation["turns"]], ["hello", "reply"])
        self.assertIsNotNone(second["created_at"])
        self.assertIsInstance(conversation["turns"][-1]["elapsed_ms"], int)

    def test_image_only_request_reaches_model_and_persists_attachment(self):
        conversation_id = vault.new_conversation_id()
        meta = vault.save_attachment("reba", conversation_id, "label.png", "image/png", b"image bytes")
        with patch.object(chat_jobs, "_EXECUTOR", ImmediateExecutor()), \
             patch.object(chat_service.retrieval, "build_context", return_value="none"), \
             patch.object(chat_service.llm_models, "selected_model_id", return_value="gpt-5.6-sol"), \
             patch.object(chat_service.llm_client, "maybe_compact_conversation"), \
             patch.object(chat_service.llm_client, "chat", return_value="label read") as chat:
            chat_jobs.submit("reba", "image_only", conversation_id, "", [meta["id"]])
        self.assertEqual(chat_jobs.get("reba", "image_only")["status"], "completed")
        blocks = chat.call_args.args[2][-1]["content"]
        self.assertEqual(blocks[-1]["type"], "image")
        conversation = vault.get_conversation("reba", conversation_id)
        self.assertEqual(conversation["turns"][0]["attachments"][0]["id"], meta["id"])
        self.assertEqual(conversation["turns"][-1]["content"], "label read")

    def _pdf(self, text):
        # Smallest valid one-page PDF with one line of text.
        stream = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode()
        objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
                b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
                b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
        out, offsets = b"%PDF-1.4\n", []
        for i, o in enumerate(objs, 1):
            offsets.append(len(out)); out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
        return out + f"trailer << /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()

    def test_documents_reach_any_model_as_text(self):
        conversation_id = vault.new_conversation_id()
        pdf = vault.save_attachment("reba", conversation_id, "manual.pdf", "application/pdf", self._pdf("Torque 42 Nm"))
        notes = vault.save_attachment("reba", conversation_id, "notes.md", "text/markdown", "# Notes\nchange oil".encode())
        self.assertTrue(vault.attachment_markdown_ref(conversation_id, pdf).startswith("[manual.pdf]("), "a link, not an image")
        with patch.object(chat_jobs, "_EXECUTOR", ImmediateExecutor()), \
             patch.object(chat_service.retrieval, "build_context", return_value="none"), \
             patch.object(chat_service.llm_models, "selected_model_id", return_value="grok-4.3"), \
             patch.object(chat_service.llm_models, "get", return_value=chat_service.llm_models.ModelOption(id="x", provider="xai", display_name="Text only", tier="t")), \
             patch.object(chat_service.llm_client, "maybe_compact_conversation"), \
             patch.object(chat_service.llm_client, "chat", return_value="read it") as chat:
            chat_jobs.submit("reba", "docs", conversation_id, "what's in these?", [pdf["id"], notes["id"]])
        self.assertEqual(chat_jobs.get("reba", "docs")["status"], "completed", "documents work even on a model without images")
        sent = chat.call_args.args[2][-1]["content"]
        self.assertIsInstance(sent, str, "documents go in as text")
        self.assertIn("# Attached file: manual.pdf", sent)
        self.assertIn("Torque 42 Nm", sent)
        self.assertIn("change oil", sent)
        turn = vault.get_conversation("reba", conversation_id)["turns"][0]
        self.assertEqual({a["filename"] for a in turn["attachments"]}, {"manual.pdf", "notes.md"})

    def test_unreadable_pdf_says_so(self):
        conversation_id = vault.new_conversation_id()
        bad = vault.save_attachment("reba", conversation_id, "broken.pdf", "application/pdf", b"%PDF-1.4 not really")
        self.assertIn("couldn't read this file", vault.attachment_text("reba", conversation_id, bad["id"]))

    def test_empty_request_without_attachment_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "message cannot be empty"):
            chat_jobs.submit("reba", "empty", None, "  ", [])

    def test_reply_timing_survives_reload_without_entering_model_history(self):
        conversation_id = vault.new_conversation_id()
        vault.append_turn("reba", conversation_id, "user", "older question")
        vault.append_turn("reba", conversation_id, "assistant", "older reply",
                          timings={"elapsed_ms": 7203, "provider_ms": 6000})
        saved = vault.get_conversation("reba", conversation_id)
        self.assertIsNone(saved["turns"][0]["elapsed_ms"])
        self.assertEqual(saved["turns"][1]["elapsed_ms"], 7203)
        self.assertEqual(saved["turns"][1]["content"], "older reply")
        timings = {}
        with patch.object(chat_service.retrieval, "build_context", return_value="none"), \
             patch.object(chat_service.llm_client, "maybe_compact_conversation"), \
             patch.object(chat_service.llm_client, "chat", return_value="new reply") as model:
            chat_service.run_chat("reba", conversation_id, "next question", timings=timings)
        history = model.call_args.args[2]
        self.assertEqual(history[1]["content"], "older reply")
        self.assertNotIn("openclave-timing", str(history))
        self.assertEqual(set(timings), {"elapsed_ms", "provider_ms", "retrieval_ms", "compaction_ms", "model_id"})
        saved = vault.get_conversation("reba", conversation_id)
        self.assertEqual(saved["turns"][-1]["elapsed_ms"], timings["elapsed_ms"])

    def test_timing_metadata_is_preserved_by_compaction(self):
        conversation_id = vault.new_conversation_id()
        vault.append_turn("reba", conversation_id, "user", "question")
        vault.append_turn("reba", conversation_id, "assistant", "reply", timings={"elapsed_ms": 1234})
        raw = vault.raw_turn_blocks("reba", conversation_id)
        vault.compact_conversation("reba", conversation_id, "earlier summary", [t["raw"] for t in raw])
        saved = vault.get_conversation("reba", conversation_id)
        self.assertEqual(saved["turns"][-1]["elapsed_ms"], 1234)
        self.assertEqual(saved["turns"][-1]["content"], "reply")

    def test_reused_request_id_with_new_input_conflicts(self):
        with patch.object(chat_jobs, "_EXECUTOR", ImmediateExecutor()), \
             patch.object(chat_service.retrieval, "build_context", return_value="none"), \
             patch.object(chat_service.llm_client, "maybe_compact_conversation"), \
             patch.object(chat_service.llm_client, "chat", return_value="reply"):
            chat_jobs.submit("reba", "req_456", None, "hello")
            with self.assertRaises(chat_jobs.RequestConflictError):
                chat_jobs.submit("reba", "req_456", None, "different")

    def test_provider_failure_preserves_user_turn(self):
        with patch.object(chat_jobs, "_EXECUTOR", ImmediateExecutor()), \
             patch.object(chat_service.retrieval, "build_context", return_value="none"), \
             patch.object(chat_service.llm_client, "maybe_compact_conversation"), \
             patch.object(chat_service.llm_client, "chat", return_value="(claude-cli error: unavailable)"):
            record, _ = chat_jobs.submit("reba", "req_789", None, "hello")

        status = chat_jobs.get("reba", "req_789")
        self.assertEqual(status["status"], "failed")
        conversation = vault.get_conversation("reba", record["conversation_id"])
        self.assertEqual([t["content"] for t in conversation["turns"]], ["hello"])

    def test_provider_timeout_is_recorded_not_raised_bare(self):
        # A raw exception from llm_client.chat (e.g. subprocess.TimeoutExpired
        # from the claude-cli/codex-cli paths) used to propagate past both
        # callers' error handling instead of the "(...error: ...)" string
        # convention they check for — an unhandled 500 on the desktop /chat
        # route, and here, an untyped failure reason in the job record.
        with patch.object(chat_jobs, "_EXECUTOR", ImmediateExecutor()), \
             patch.object(chat_service.retrieval, "build_context", return_value="none"), \
             patch.object(chat_service.llm_client, "maybe_compact_conversation"), \
             patch.object(
                 chat_service.llm_client, "chat",
                 side_effect=subprocess.TimeoutExpired(cmd=["claude", "-p"], timeout=600),
             ):
            record, _ = chat_jobs.submit("reba", "req_timeout", None, "hello")

        status = chat_jobs.get("reba", "req_timeout")
        self.assertEqual(status["status"], "failed")
        self.assertIn("timed out", status["error"])
        conversation = vault.get_conversation("reba", record["conversation_id"])
        self.assertEqual([t["content"] for t in conversation["turns"]], ["hello"])




class CancelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = (vault.AGENTS_ROOT, vault.VAULT_ROOT)
        vault.VAULT_ROOT = Path(self.temp.name).resolve()
        vault.AGENTS_ROOT = (vault.VAULT_ROOT / "OpenClave" / "Agents").resolve()
        root = vault.agent_root("reba")
        root.mkdir(parents=True)
        for name in ("mission.md", "persona.md", "backlog.md", "plan.md"):
            (root / name).write_text("", encoding="utf-8")

    def tearDown(self):
        vault.AGENTS_ROOT, vault.VAULT_ROOT = self.old
        chat_jobs._SCHEDULED.discard("reba")
        self.temp.cleanup()

    def test_cancel_queued_request_never_runs(self):
        class NoRun:
            def submit(self, fn, *args):
                pass
        with patch.object(chat_jobs, "_EXECUTOR", NoRun()):
            chat_jobs.submit("reba", "req_q", None, "hello")
        result = chat_jobs.cancel("reba", "req_q")
        self.assertEqual((result["status"], result["stoppable"]), ("cancelled", True))
        with patch.object(chat_service, "run_chat_locked") as ran:
            chat_jobs._run("reba", "req_q")
        ran.assert_not_called()
        self.assertEqual(chat_jobs.get("reba", "req_q")["status"], "cancelled")

    def test_stopped_running_request_ends_cancelled_with_clean_transcript(self):
        with patch.object(chat_jobs, "_EXECUTOR", ImmediateExecutor()), \
             patch.object(chat_service.retrieval, "build_context", return_value="none"), \
             patch.object(chat_service.llm_client, "maybe_compact_conversation"), \
             patch.object(chat_service.llm_client, "chat", side_effect=lambda *a, **k: chat_jobs._CANCELLED.add("req_r") or "(codex-cli error: killed)"):
            record, _ = chat_jobs.submit("reba", "req_r", None, "long job")
        self.assertEqual(chat_jobs.get("reba", "req_r")["status"], "cancelled")
        turns = vault.get_conversation("reba", record["conversation_id"])["turns"]
        self.assertEqual([t["role"] for t in turns], ["user", "assistant"])
        self.assertIn("Stopped", turns[-1]["content"])

    def _run_with_push(self, request_id, reply="**Done.** Here's   the plan", watched=False, fail=False):
        class NoRun:
            def submit(self, fn, *args):
                pass
        with patch.object(chat_jobs, "_EXECUTOR", NoRun()):
            record, _ = chat_jobs.submit("reba", request_id, None, "hello")
        if watched:
            chat_jobs.get("reba", request_id, watching=True)
        sent = []
        effect = RuntimeError("provider down") if fail else None
        with patch.object(chat_service, "run_chat_locked", return_value=reply, side_effect=effect), \
             patch.object(chat_jobs.threading, "Thread", side_effect=lambda target, **k: type("T", (), {"start": staticmethod(target)})()), \
             patch.object(chat_jobs.web_push, "send_all", side_effect=lambda *a, **k: sent.append((a, k)) or 1):
            chat_jobs._run("reba", request_id)
        return record, sent

    def test_finished_reply_is_pushed_without_the_chat_open(self):
        record, sent = self._run_with_push("req_push")
        self.assertEqual(len(sent), 1)
        (title, body), kwargs = sent[0]
        self.assertEqual(title, "Reba replied")
        self.assertEqual(body, "Done. Here's the plan")
        self.assertEqual(kwargs["tag"], "openclave-reply-reba")
        self.assertEqual(kwargs["url"], f"/mobile/#/agents/reba/conversations/{record['conversation_id']}")

    def test_no_push_while_a_visible_page_watches_the_request(self):
        _, sent = self._run_with_push("req_watched", watched=True)
        self.assertEqual(sent, [])

    def test_failed_reply_is_pushed_too(self):
        _, sent = self._run_with_push("req_fail", fail=True)
        self.assertEqual(sent[0][0][0], "Reba couldn't reply")
        self.assertIn("provider down", sent[0][0][1])

    def test_finished_replies_are_listed_for_autojump(self):
        chat_jobs._RECENT_REPLIES.clear()
        before = chat_jobs.recent_replies(0)["now_ms"]
        self._run_with_push("req_aj1")
        self._run_with_push("req_aj2", fail=True)  # failed: not a reply to open
        listed = chat_jobs.recent_replies(before - 1)["replies"]
        self.assertEqual([r["agent_id"] for r in listed], ["reba"])
        self.assertEqual(chat_jobs.recent_replies(listed[0]["finished_ms"])["replies"], [])
        for _ in range(chat_jobs._RECENT_MAX + 5):
            chat_jobs._remember_reply("reba", "c")
        times = [r["finished_ms"] for r in chat_jobs._RECENT_REPLIES]
        self.assertEqual(len(times), chat_jobs._RECENT_MAX)
        self.assertEqual(times, sorted(set(times)), "strictly increasing, so after=<ms> never skips one")
        chat_jobs._RECENT_REPLIES.clear()

    def test_cancel_running_kills_the_marked_process_group(self):
        import llm_client
        env = dict(os.environ)
        marker = llm_client._mark_run("reba", env)
        proc = subprocess.Popen(["sleep", "30"], env=env, start_new_session=True)
        try:
            self.assertTrue(llm_client.cancel_running("reba"))
            self.assertIsNotNone(proc.wait(timeout=5), "process stopped")
        finally:
            proc.kill()
            llm_client._unmark_run("reba", marker)
        self.assertFalse(llm_client.cancel_running("reba"), "nothing running any more")


class ConversationSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = (vault.AGENTS_ROOT, vault.VAULT_ROOT)
        vault.VAULT_ROOT = Path(self.temp.name).resolve()
        vault.AGENTS_ROOT = (vault.VAULT_ROOT / "OpenClave" / "Agents").resolve()
        vault.agent_root("reba").mkdir(parents=True)

    def tearDown(self):
        vault.AGENTS_ROOT, vault.VAULT_ROOT = self.old
        self.temp.cleanup()

    def test_search_matches_all_words_with_snippet(self):
        a, b = vault.new_conversation_id(), vault.new_conversation_id()
        vault.append_turn("reba", a, "user", "My ankle hurts after the hike", first_message="Ankle")
        vault.append_turn("reba", a, "assistant", "Ice it and rest.", timings={"elapsed_ms": 5, "model_id": "x"})
        vault.append_turn("reba", b, "user", "Knee plan for October", first_message="Knee plan")
        hits = vault.search_conversations("reba", "ANKLE hike")
        self.assertEqual([h["id"] for h in hits], [a])
        self.assertIn("ankle hurts", hits[0]["snippet"])
        self.assertEqual(vault.search_conversations("reba", "ankle october"), [])
        self.assertEqual(vault.search_conversations("reba", "openclave-timing"), [], "timing comments aren't searched")
        self.assertEqual(vault.search_conversations("reba", "   "), [])
        self.assertNotIn("###", hits[0]["snippet"], "turn headings stripped")


# Finished chat jobs send a reply push; the real subscriptions are the owner's phones.
_no_push = patch("web_push.send_all", return_value=0)


def setUpModule():
    _no_push.start()


def tearDownModule():
    _no_push.stop()

if __name__ == "__main__":
    unittest.main()
