"""Weekly review: the Control Panel checklist decides what the
cleanup and the manager's audit suggest; model changes are proposals the
owner says yes to, never automatic."""
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import audit
import llm_models
import model_updater as mu
import proposals
import service_settings
from llm_models import ModelOption


class WeeklyReviewSettingsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = patch.object(service_settings, "SETTINGS_FILE", Path(tmp.name) / "service_settings.json")
        p.start()
        self.addCleanup(p.stop)

    def test_all_ticked_by_default_and_saved_per_box(self):
        self.assertTrue(all(service_settings.load()["weekly_review"].values()))
        service_settings.update({"weekly_review": {"remove_old_models": False}})
        review = service_settings.load()["weekly_review"]
        self.assertFalse(review["remove_old_models"])
        self.assertTrue(review["add_new_models"], "other boxes untouched")
        self.assertFalse(service_settings.review_on("remove_old_models"))
        for bad in ({"nope": True}, {"tasks": "yes"}, "all"):
            with self.assertRaises(ValueError):
                service_settings.update({"weekly_review": bad})


class OldModelTests(unittest.TestCase):
    def m(self, id, name, provider="anthropic", released="2026-09"):
        return ModelOption(id=id, provider=provider, display_name=name, tier="t", released=released)

    def test_old_means_superseded_provider_older_or_aged(self):
        a, b = self.m("o5", "Claude Opus 5"), self.m("o55", "Claude Opus 5.5")
        cli = self.m("o5cli", "Claude Opus 5 (subscription)", provider="claude-cli")
        models = [a, b, cli]
        self.assertEqual(llm_models.old_reason(a, models), "Claude Opus 5.5 is newer")
        self.assertIsNone(llm_models.old_reason(b, models))
        self.assertIsNone(llm_models.old_reason(cli, models), "only a newer one on the same route counts")
        aged = self.m("s", "Claude Sonnet 4", released="2026-01")
        self.assertEqual(llm_models.old_reason(aged, [aged], date(2026, 11, 4)), "released 10 months ago")

    def test_short_names(self):
        self.assertEqual(llm_models.short_name(self.m("x", "Claude Opus 5.5 (subscription)")), "Opus 5.5")
        long = llm_models.short_name(self.m("y", "Grok Extremely Long Model Name 9"))
        self.assertLessEqual(len(long), llm_models.NAME_MAX)
        self.assertTrue(long.endswith("…"))


class ModelProposalTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for target, name, value in ((service_settings, "SETTINGS_FILE", root / "s.json"),
                                    (proposals, "STATE_FILE", root / "p.json"),
                                    (mu, "STATE_FILE", root / "auto.json"),
                                    (llm_models, "AUTO_FILE", root / "auto.json")):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(llm_models, "in_use", return_value={"gpt-6-sol"})
        p.start()
        self.addCleanup(p.stop)
        llm_models.apply_auto({})
        self.addCleanup(llm_models.apply_auto)
        new = ModelOption(id="claude-sonnet-6", provider="anthropic", display_name="Claude Sonnet 6", tier="t", released="2026-11")
        from dataclasses import asdict
        state = mu.load()
        state["available"] = {"claude-sonnet-6": {"model": asdict(new), "detail": "test reply in 2.0s"}}
        mu.save(state)

    def test_proposals_follow_the_checklist_and_yes_applies_them(self):
        filed = audit.model_proposals()
        titles = [p["title"] for p in filed]
        self.assertIn("Add new model: Claude Sonnet 6 (API)", titles)
        self.assertIn("Remove old model: Claude Opus 5 (API)", titles, "Opus 5.5 is newer")
        self.assertNotIn("Remove old model: GPT-6 Sol (CLI)", titles, "in use: never suggested")
        self.assertEqual(audit.model_proposals(), [], "not filed twice")
        add = next(p for p in filed if p["model_action"] == "add")
        proposals.approve(add["id"])
        self.assertIn("claude-sonnet-6", [m.id for m in llm_models.picker_models()])
        hide = next(p for p in filed if p["model_id"] == "claude-opus-5")
        proposals.approve(hide["id"])
        self.assertNotIn("claude-opus-5", [m.id for m in llm_models.picker_models()])
        no = next(p for p in filed if p["model_action"] == "hide" and p["id"] != hide["id"])
        proposals.decline(no["id"])
        self.assertNotIn(no["title"], [p["title"] for p in audit.model_proposals()], "a No isn't asked again soon")

    def test_unticked_boxes_file_nothing(self):
        service_settings.update({"weekly_review": {"remove_old_models": False, "add_new_models": False}})
        self.assertEqual(audit.model_proposals(), [])

    def test_the_manager_cannot_file_model_proposals_itself(self):
        with patch("agents.has_role", return_value=True):
            reply = proposals.tool_reply("rick", {"kind": "model", "title": "x", "content": "y"})
        self.assertTrue(reply.startswith("refused"))

    def test_audit_brief_lists_only_ticked_items(self):
        text = audit.brief({"files": {}, "concluded": {}, "declined": []}, "2026-11-01",
                           review={"vault_notes": False, "nudge_agents": True, "tasks": False})
        self.assertIn("Nudges", text)
        self.assertNotIn("main vault** (kind=vault_note)", text)
        self.assertIn("Agents to merge", text)  # not in the dict: on by default
        off = audit.brief({"files": {}, "concluded": {}, "declined": []}, "2026-11-01", review={"merge_agents": False})
        self.assertNotIn("Agents to merge", off)


if __name__ == "__main__":
    unittest.main()
