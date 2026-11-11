"""The chat routes llm_client.chat() dispatches to, by llm_models provider id."""
from providers.anthropic_api import AnthropicApiProvider
from providers.base import Provider
from providers.claude_cli import ClaudeCliProvider
from providers.codex_cli import CodexCliProvider
from providers.xai_api import XaiApiProvider

PROVIDERS: dict[str, Provider] = {p.id: p for p in (
    AnthropicApiProvider(), XaiApiProvider(), ClaudeCliProvider(), CodexCliProvider(),
)}
# chat() has always sent any provider id it doesn't know to the xAI route.
FALLBACK = "xai"


def get(provider_id: str) -> Provider:
    return PROVIDERS.get(provider_id, PROVIDERS[FALLBACK])
