"""Model picker cost tiers: by API output price per 1M
tokens, low <= $4, mid > $4 and <= $20, high > $20; CLI models use the same
model's API list price."""
import unittest

import llm_models
import main
from routes import models as model_routes
from llm_models import ModelOption, cost_tier


def model(out=None, **kw):
    return ModelOption(id=kw.pop("id", "x-test"), provider=kw.pop("provider", "xai"), display_name="X", tier="",
                       api_input_usd=None if out is None else 1, api_output_usd=out, **kw)


class CostTierTests(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(cost_tier(model(0.5)), "low")
        self.assertEqual(cost_tier(model(4)), "low")
        self.assertEqual(cost_tier(model(4.01)), "mid")
        self.assertEqual(cost_tier(model(20)), "mid")
        self.assertEqual(cost_tier(model(20.01)), "high")
        self.assertEqual(cost_tier(model(50)), "high")

    def test_unknown_price_has_no_tier(self):
        self.assertIsNone(cost_tier(model(None, id="no-such-model")))

    def test_cli_and_auto_models_use_the_api_list_price(self):
        cli = model(None, id="claude-opus-5-5-cli", provider="claude-cli", api_model_id="claude-opus-5-5")
        self.assertEqual(llm_models.api_prices(cli), (4, 20))
        self.assertEqual(cost_tier(cli), "mid")
        self.assertEqual(cost_tier(model(None, id="gpt-6-astra", provider="codex-cli")), "high")
        self.assertEqual(cost_tier(model(None, id="gpt-6-luna", provider="codex-cli")), "low")

    def test_own_price_wins_over_the_list(self):
        self.assertEqual(cost_tier(model(30, id="gpt-6-luna")), "high")

    def test_no_red(self):
        colours = {c.upper() for _, c in llm_models.COST_TIERS.values()}
        self.assertEqual(colours, {"#22C55E", "#F59E0B", "#A855F7"})

    def test_every_builtin_model_has_a_tier(self):
        self.assertEqual([m.id for m in llm_models.BUILTIN if cost_tier(m) is None], [])

    def test_models_api_carries_the_tier(self):
        rows = [option.model_dump() for option in model_routes.api_models()]
        self.assertTrue(rows)
        for row in rows:
            m = llm_models.get(row["id"])
            tier = cost_tier(m)
            label, colour = llm_models.COST_TIERS.get(tier, ("", ""))
            self.assertEqual((row["cost_tier"], row["cost_label"], row["cost_color"]), (tier, label, colour), row["id"])
            self.assertEqual(row["api_output_usd"], llm_models.api_prices(m)[1], row["id"])

if __name__ == "__main__":
    unittest.main()
