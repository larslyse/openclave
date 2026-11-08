import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import llm_client
import retrieval
import vault


class FoundationsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for name, value in [('VAULT_ROOT', root), ('AGENTS_ROOT', root / 'Agents')]:
            patcher = patch.object(vault, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_missing_is_optional_and_reads_do_not_create_file(self):
        self.assertEqual(vault.read_foundations('rick'), '')
        self.assertIn('missing file is normal', retrieval.context_directory('rick'))
        self.assertFalse(vault.foundations_file('rick').exists())

    def test_claim_and_rule_tool_roundtrip(self):
        llm_client._run_tool('stash', 'add_foundation', {
            'topic': 'inventory', 'foundation': 'Record location | including shelf',
            'kind': 'operating_rule', 'reference': 'Agreed to avoid duplicate purchases',
        })
        llm_client._run_tool('stash', 'add_foundation', {
            'topic': 'materials', 'foundation': 'Example claim', 'kind': 'claim',
            'reference': 'Example source; limited evidence', 'confidence': 6,
        })
        text = vault.read_foundations('stash')
        self.assertIn('Operating rule', text)
        self.assertIn('&#124;', text)
        self.assertIn('| — |', text)
        self.assertIn('6/10', text)
        self.assertNotIn('Example claim', vault.read_foundations('stash', 'inventory'))
        self.assertFalse((vault.agent_root('stash') / 'principles.md').exists())

    def test_validation_prevents_misleading_scores_and_empty_entries(self):
        for kind, text, basis, score in [('operating_rule', 'Rule', 'Reason', 8),
                                         ('claim', 'Claim', '', 5),
                                         ('claim', '', 'Source', None),
                                         ('claim', 'Claim', 'Source', 11)]:
            with self.assertRaises(ValueError):
                vault.add_foundation('stash', 'General', text, kind, basis, score)
        self.assertFalse(vault.foundations_file('stash').exists())

    def test_append_preserves_existing_prose(self):
        path = vault.foundations_file('reba')
        path.parent.mkdir(parents=True)
        path.write_text('# Reba — Foundations\n\n## General\n\nImportant explanation.\n')
        vault.add_foundation('reba', 'general', 'Claim', 'claim', 'Source; uncertainty stated')
        text = path.read_text()
        self.assertIn('Important explanation.\n\n| Kind |', text)
        self.assertIn('Not scored', text)
