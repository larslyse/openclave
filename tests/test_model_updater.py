import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import llm_models
import model_updater as mu


def offer(route, api_id, rank, name=None, older=False, vision=True, price=(None, None)):
    return {"route": route, "api_id": api_id, "name": name or api_id, "vision": vision, "price_in": price[0],
            "price_out": price[1], "rank": rank, "older": older, "tier": "t"}


class ModelUpdaterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "auto_models.json"
        self.patches = [patch.object(mu, "STATE_FILE", path), patch.object(llm_models, "AUTO_FILE", path),
                        patch.object(llm_models, "in_use", return_value={"gpt-6-sol"})]
        for p in self.patches:
            p.start()
        llm_models.apply_auto({})

    def tearDown(self):
        for p in self.patches:
            p.stop()
        llm_models.apply_auto()  # back to the real state
        self.tmp.cleanup()

    def run_with(self, by_route, probe=lambda route, api_id: 2.0, errors=None):
        with patch.object(mu, "offers", return_value=(by_route, errors or {})):
            return mu.run(probe=probe)

    def codex_offers(self, *extra):
        known = ["gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]
        listed = list(extra) + [offer("codex-cli", k, 0) for k in known]
        for i, o in enumerate(listed):
            o["rank"] = float(len(listed) - i)
        return listed

    def test_only_models_newer_than_what_we_have_are_tested_and_kept_as_suggestions(self):
        tested = []
        by_route = {
            "codex-cli": self.codex_offers(offer("codex-cli", "gpt-7-sol", 0, "GPT-7 Sol")),
            "anthropic": [offer("anthropic", "claude-opus-5-5", 100), offer("anthropic", "claude-opus-4-8", 50),
                          offer("anthropic", "claude-sonnet-6", 1790000000, "Claude Sonnet 6", price=(2, 10))],
            "claude-cli": [offer("claude-cli", "claude-opus-5-5", 100), offer("claude-cli", "claude-sonnet-6", 200, "Claude Sonnet 6 (subscription)")],
        }
        state = self.run_with(by_route, probe=lambda r, a: tested.append((r, a)) or (2.0 if r == "anthropic" else 9.0))
        self.assertEqual(sorted(tested), [("anthropic", "claude-sonnet-6"), ("claude-cli", "claude-sonnet-6"), ("codex-cli", "gpt-7-sol")],
                         "older models the catalog lacks (Opus 4.8) are not added")
        self.assertFalse([m for m in llm_models.CATALOG if m.auto_added], "nothing added without a yes (2026-11-04)")
        self.assertEqual(sorted(state["available"]), ["claude-sonnet-6", "claude-sonnet-6-cli", "gpt-7-sol"])
        for model_id in list(state["available"]):
            mu.add_available(model_id)
        added = {m.id: m for m in llm_models.CATALOG if m.auto_added}
        self.assertEqual(added["claude-sonnet-6"].speed, "fast")
        self.assertEqual(added["claude-sonnet-6"].released, "2026-09", "release month from the provider's date")
        self.assertEqual(added["claude-sonnet-6"].api_input_usd, 2)
        self.assertEqual(added["claude-sonnet-6-cli"].api_model_id, "claude-sonnet-6")
        self.assertEqual(added["claude-sonnet-6-cli"].provider, "claude-cli")
        self.assertEqual(added["gpt-7-sol"].speed, "fast")
        self.assertIn("gpt-7-sol", [m.id for m in llm_models.picker_models()])
        self.assertEqual([e["action"] for e in mu.load()["log"]].count("added"), 3)

    def test_older_labelled_codex_models_are_never_added(self):
        tested = []
        self.run_with({"codex-cli": self.codex_offers(offer("codex-cli", "gpt-5.5", 0, older=True))},
                      probe=lambda r, a: tested.append(a) or 5.0)
        self.assertEqual(tested, [])

    def test_failed_test_reply_is_logged_and_retried_up_to_three_times(self):
        by_route = {"codex-cli": self.codex_offers(offer("codex-cli", "gpt-7-sol", 0))}
        def boom(route, api_id):
            raise RuntimeError("ERROR: model not available")
        for _ in range(4):
            state = self.run_with(by_route, probe=boom)
        self.assertEqual(state["failures"]["gpt-7-sol"], 3, "not retried after three failures")
        self.assertNotIn("gpt-7-sol", llm_models._BY_ID)
        self.assertIn("model not available", state["log"][-1]["detail"])

    def test_models_the_provider_dropped_are_noted_not_hidden(self):
        offered = [o for o in self.codex_offers() if o["api_id"] not in ("gpt-5.6-luna", "gpt-6-sol")]
        state = self.run_with({"codex-cli": offered})
        self.assertEqual(state["hidden"], [], "nothing hidden without a yes (2026-11-04)")
        self.assertIn("gpt-5.6-luna", state["gone"])
        mu.hide("gpt-5.6-luna", "no longer offered")
        self.assertNotIn("gpt-5.6-luna", [m.id for m in llm_models.picker_models()])
        self.assertIn("gpt-5.6-luna", llm_models._BY_ID, "hidden, not deleted")
        with self.assertRaises(ValueError):
            mu.hide("gpt-6-sol", "old")  # in use

    def test_a_route_that_could_not_be_read_hides_nothing(self):
        state = self.run_with({}, errors={"codex-cli": "TimeoutExpired"})
        self.assertEqual(state["hidden"], [])
        self.assertEqual(state["log"][0]["action"], "error")

    def test_provider_older_labels_are_kept_for_the_old_marker(self):
        listed = self.codex_offers()
        for o in listed:
            o["older"] = o["api_id"].startswith("gpt-5.6")
        state = self.run_with({"codex-cli": listed})
        self.assertEqual(set(state["provider_older"]), {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"})
        self.assertEqual(llm_models.old_reason(llm_models.get("gpt-5.6-terra")), "the provider lists it as an older model")

    def test_remove_and_restore(self):
        self.run_with({"codex-cli": self.codex_offers(offer("codex-cli", "gpt-7-sol", 0))}, probe=lambda r, a: 5.0)
        mu.add_available("gpt-7-sol")
        mu.remove("gpt-7-sol")
        self.assertNotIn("gpt-7-sol", [m.id for m in llm_models.picker_models()])
        tested = []
        self.run_with({"codex-cli": self.codex_offers(offer("codex-cli", "gpt-7-sol", 0))}, probe=lambda r, a: tested.append(a) or 5.0)
        self.assertEqual(tested, [], "a removed model isn't re-added")
        mu.restore("gpt-7-sol")
        self.assertIn("gpt-7-sol", [m.id for m in llm_models.picker_models()])
        with self.assertRaises(ValueError):
            mu.remove("gpt-6-sol")  # in use

    def test_hidden_model_still_shows_while_an_agent_uses_it(self):
        llm_models.apply_auto({"hidden": [{"id": "gpt-6-sol", "reason": "x"}]})
        self.assertIn("gpt-6-sol", [m.id for m in llm_models.picker_models()])


if __name__ == "__main__":
    unittest.main()
