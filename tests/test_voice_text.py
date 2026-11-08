"""What voice mode reads aloud (2026-10-12/13: agents read asterisks
and other markdown out loud). The shown reply keeps its markdown."""
import unittest

from chat_service import _speakable_text as speak


class SpeakableTextTests(unittest.TestCase):
    def test_no_markdown_symbols_left(self):
        reply = ("## Status\n**Done.** Added to the *backlog*, see [the note](https://x.y/z).\n"
                 "- First\n- [ ] Second `snake_case`\n> quoted\n---\n```\nprint(1)\n```\n| A | B |\n|---|---|\n| 1 | 2 |")
        spoken = speak(reply)
        for symbol in ("*", "#", "`", "[", "](", "|", "http", "---", ">"):
            self.assertNotIn(symbol, spoken)
        self.assertEqual(spoken, "Status. Done. Added to the backlog, see the note. First. Second snake_case. "
                                 "quoted. print(1). A, B. 1, 2")

    def test_words_and_numbers_survive(self):
        self.assertEqual(speak("Use snake_case_name: 2 * 3 = 6."), "Use snake_case_name: 2 times 3 = 6.")
        self.assertEqual(speak("Read https://example.com/a?b=1 later"), "Read a link later")
        self.assertEqual(speak("1. One\n2. Two"), "1. One. 2. Two")

    def test_stage_cues_still_dropped(self):
        self.assertEqual(speak("Fine *burp* whatever."), "Fine whatever.")
        self.assertEqual(speak("Oh *sighs* fine."), "Oh sighs fine.")

    def test_plain_text_unchanged(self):
        self.assertEqual(speak("Sure, I'll look at that tomorrow."), "Sure, I'll look at that tomorrow.")


if __name__ == "__main__":
    unittest.main()


class UnbackedClaimTests(unittest.TestCase):
    """A reply saying "Added" with no writing tool behind it gets one more
    round (voice-mode Rick, 2026-10-12/13: four lost backlog items)."""

    def run_checked(self, replies, tools_per_call, model_id="claude-sonnet-5"):
        from unittest.mock import patch
        import chat_service, llm_client
        calls = []

        def fake_chat(agent_id, prompt, history, model_id=None, **kw):
            n = len(calls)
            calls.append(history)
            for tool in tools_per_call[n]:
                llm_client._TOOLS_RUN.setdefault(agent_id, []).append(tool)
            return replies[n]

        with patch.object(llm_client, "chat", fake_chat):
            reply = chat_service._chat_checked("rick", "sys", [{"role": "user", "content": "add X"}], model_id)
        return reply, calls

    def test_claim_without_a_tool_gets_another_round(self):
        import chat_service
        reply, calls = self.run_checked(["Added: X.", "Added X to the backlog."], [[], ["update_backlog"]])
        self.assertEqual(reply, "Added X to the backlog.")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][-2], {"role": "assistant", "content": "Added: X."})
        self.assertEqual(calls[1][-1]["content"], chat_service.UNBACKED_CLAIM_NOTE)

    def test_backed_claim_or_plain_answer_is_left_alone(self):
        self.assertEqual(self.run_checked(["Added: X."], [["update_backlog"]])[0], "Added: X.")
        self.assertEqual(len(self.run_checked(["It's sunny."], [[]])[1]), 1)
        self.assertEqual(len(self.run_checked(["Lagt til i backloggen.", "Lagt til."], [[], ["update_backlog"]])[1]), 2, "Norwegian too")

    def test_reading_tools_dont_count(self):
        self.assertEqual(len(self.run_checked(["Added.", "Done, added."], [["read_note"], ["update_backlog"]])[1]), 2)

    def test_cli_routes_arent_checked(self):
        # Their tools run out of sight, so there's nothing to compare.
        self.assertEqual(len(self.run_checked(["Added."], [[]], model_id="claude-opus-5-5-cli")[1]), 1)
