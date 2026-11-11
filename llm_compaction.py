"""Compacting long conversations: summarize older turns once a conversation
grows past a size threshold (split out of llm_client.py)."""
import os

import vault
from providers.xai_api import XAI_BASE_URL


# A long-running conversation resends its ENTIRE raw history on every turn
# (main.py's chat() builds `history` straight from vault.get_conversation()) —
# with no cap this grows every message's cost/latency forever and eventually
# breaks. Mirrors Hermes Agent's compaction design: before that gets out of
# hand, summarize everything except the most recent turns into one block, and
# archive the untouched original so nothing is silently lost (vault.py's
# compact_conversation). Threshold is on raw characters, not tokens — cheap
# to check on every chat call, conservative enough (~40k chars is roughly
# 10k tokens) to leave real headroom before any provider's context limit.
COMPACTION_CHAR_THRESHOLD = 40_000
COMPACTION_KEEP_RECENT_TURNS = 8
# Deliberately NOT whichever provider the agent itself is configured to use:
# compaction is background housekeeping, not a user-facing reply, so it
# shouldn't inherit claude-cli's per-message subprocess overhead or cost, or
# depend on the agent's own model choice at all.
COMPACTION_MODEL = "grok-4-1-fast-non-reasoning"


def _summarize_transcript(transcript: str) -> str:
    import openai
    client = openai.OpenAI(api_key=os.environ["XAI_API_KEY"], base_url=XAI_BASE_URL)
    response = client.chat.completions.create(
        model=COMPACTION_MODEL,
        max_tokens=800,
        messages=[
            {
                "role": "system",
                "content": (
                    "Summarize this conversation transcript into a compact but complete record: concrete "
                    "facts stated, decisions made, values/measurements given, and any open threads. Write "
                    "it so a different assistant could pick up the conversation cold from this summary "
                    "alone. Be dense, not chatty — this replaces the raw turns for context purposes, so "
                    "don't lose anything concretely useful."
                ),
            },
            {"role": "user", "content": transcript},
        ],
    )
    return (response.choices[0].message.content or "").strip()


def maybe_compact_conversation(agent_id: str, conversation_id: str) -> None:
    """Called before building history for a chat call. No-ops for the vast
    majority of (short) conversations — only kicks in once raw size crosses
    COMPACTION_CHAR_THRESHOLD, and only if there are enough turns that
    compacting actually helps."""
    if vault.conversation_size(agent_id, conversation_id) < COMPACTION_CHAR_THRESHOLD:
        return
    turns = vault.raw_turn_blocks(agent_id, conversation_id)
    if len(turns) <= COMPACTION_KEEP_RECENT_TURNS:
        return
    to_summarize, keep = turns[:-COMPACTION_KEEP_RECENT_TURNS], turns[-COMPACTION_KEEP_RECENT_TURNS:]
    existing = vault.get_conversation(agent_id, conversation_id)
    prior_summary = (existing or {}).get("summary")
    transcript = ("Earlier summary:\n" + prior_summary + "\n\n" if prior_summary else "") + \
        "\n".join(t["raw"] for t in to_summarize)
    summary = _summarize_transcript(transcript)
    vault.compact_conversation(agent_id, conversation_id, summary, [t["raw"] for t in keep])
