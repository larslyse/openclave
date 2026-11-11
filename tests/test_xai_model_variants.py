import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import llm_client
from providers import xai_api
import llm_models


def fake_stream():
    """One streamed completion: a text chunk, then usage (stream_options)."""
    delta = SimpleNamespace(content="ok", tool_calls=None)
    return [
        SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=delta, finish_reason="stop")]),
        SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1), choices=[]),
    ]


class XaiModelVariantTests(unittest.TestCase):
    """grok-4.6 and grok-4.6-fast share one real xAI model (api_model_id) but
    are distinct catalog/selection entries distinguished by reasoning_effort —
    verifies the dispatch sends the right model string and effort to the
    provider for each, without a real network call."""

    def setUp(self):
        os.environ.setdefault("XAI_API_KEY", "test-key")
        # _chat_xai logs usage on success — keep fake calls out of the real usage_log.jsonl.
        patcher = patch.object(xai_api, "log_usage")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _capture_create_kwargs(self, fake_openai):
        client = MagicMock()
        client.chat.completions.create.side_effect = lambda **kw: fake_stream()
        fake_openai.OpenAI.return_value = client
        return client.chat.completions.create

    def test_fast_variant_sends_low_effort_and_shared_api_model(self):
        model = llm_models.get("grok-4.6-fast")
        self.assertEqual(model.api_model_id, "grok-4.6")
        self.assertEqual(model.reasoning_effort, "low")

        with patch.dict("sys.modules", {"openai": MagicMock()}):
            import sys
            create = self._capture_create_kwargs(sys.modules["openai"])
            llm_client._chat_xai(
                "rocky", model.id, "sys", [{"role": "user", "content": "hi"}],
                api_model_id=model.api_model_id, reasoning_effort=model.reasoning_effort,
            )
        _, kwargs = create.call_args
        self.assertEqual(kwargs["model"], "grok-4.6")
        self.assertEqual(kwargs["reasoning_effort"], "low")

    def test_default_variant_sends_high_effort(self):
        model = llm_models.get("grok-4.6")
        self.assertIsNone(model.api_model_id)
        self.assertEqual(model.reasoning_effort, "high")

        with patch.dict("sys.modules", {"openai": MagicMock()}):
            import sys
            create = self._capture_create_kwargs(sys.modules["openai"])
            llm_client._chat_xai(
                "rocky", model.id, "sys", [{"role": "user", "content": "hi"}],
                api_model_id=model.api_model_id, reasoning_effort=model.reasoning_effort,
            )
        _, kwargs = create.call_args
        self.assertEqual(kwargs["model"], "grok-4.6")
        self.assertEqual(kwargs["reasoning_effort"], "high")

    def test_entry_without_reasoning_effort_omits_the_kwarg(self):
        model = llm_models.get("grok-4.3")
        self.assertIsNone(model.reasoning_effort)

        with patch.dict("sys.modules", {"openai": MagicMock()}):
            import sys
            create = self._capture_create_kwargs(sys.modules["openai"])
            llm_client._chat_xai(
                "rocky", model.id, "sys", [{"role": "user", "content": "hi"}],
                api_model_id=model.api_model_id, reasoning_effort=model.reasoning_effort,
            )
        _, kwargs = create.call_args
        self.assertNotIn("reasoning_effort", kwargs)


if __name__ == "__main__":
    unittest.main()
