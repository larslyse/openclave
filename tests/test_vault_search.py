import dataclasses
import unittest
from unittest.mock import patch

import vault

INDEX = {
    "reiseforsikring": "qmd://v/00-pe/12-resources/forsikringer/forsikringsoversikt.md:25 #a\nReiseforsikring: ...",
    "forsikring": "qmd://v/00-pe/12-resources/forsikringer/forsikringsoversikt.md:4 #a\nForsikring\n\n"
                  "qmd://v/agents/rick/conversations/1.md:3 #b\nforsikring chat",
    "insurance": "qmd://v/agents/rick/conversations/1.md:9 #b\ninsurance chat",
}


def fake_query(query, timeout=10, max_results=None):
    return INDEX.get(query.lower(), "")


class SearchVaultTests(unittest.TestCase):
    def test_alternatives_find_the_norwegian_note(self):
        with patch.object(vault, "qmd_query", side_effect=fake_query):
            out = vault.search_vault("travel insurance | reiseforsikring")
        self.assertIn("forsikringsoversikt.md", out)

    def test_english_alone_misses_it(self):  # the bug the alternatives fix
        with patch.object(vault, "qmd_query", side_effect=fake_query):
            self.assertNotIn("forsikringsoversikt.md", vault.search_vault("insurance"))

    def test_no_hits_retries_word_by_word_and_notes_come_before_chats(self):
        with patch.object(vault, "qmd_query", side_effect=fake_query):
            out = vault.search_vault("insurance forsikring")
        self.assertIn("forsikringsoversikt.md", out)
        self.assertLess(out.index("forsikringsoversikt.md"), out.index("conversations/1.md"))
        self.assertEqual(out.count("conversations/1.md"), 1)  # one block per file

    def test_language_hint_names_the_configured_languages(self):
        with patch.object(vault, "CONFIG", dataclasses.replace(vault.CONFIG, note_languages=("English", "Norwegian"))):
            self.assertIn("English and Norwegian", vault.search_language_hint())
        with patch.object(vault, "CONFIG", dataclasses.replace(vault.CONFIG, note_languages=())):
            self.assertNotIn("Norwegian", vault.search_language_hint())


if __name__ == "__main__":
    unittest.main()
