"""Short, clear names for new conversations.

A conversation starts out named after the first line of its first message
(vault.append_turn). After its first exchange, a fast model names it in the
background, so the reply isn't slowed. The name only replaces that starting
title: a conversation the user has renamed (or any other title, like
"Onboarding" or "Reminders") is never touched, and it only happens once.
"""
import os
import re
import threading

import llm_client
import vault

MODEL_ID = "claude-haiku-5-5"
MAX_LEN = 48
PROMPT = (
    "Name this chat for the user's list of conversations. Reply with the name only: 1 to 5 words, "
    f"at most {MAX_LEN} characters, no quotes, no ending punctuation, no emoji. Name the topic, "
    "like a folder: 'Lower back', 'Heat pump integration', 'Hair products', 'Tailscale in HA'. "
    "If their message opens with a short topic label (\"Heatpumps. Lets integrate...\", "
    "\"IKEA devices. So i just got...\"), use that label, tidied. Write in the language of their "
    "message. Sentence case: capitalize only the first word and names."
)


def initial_title(first_message: str) -> str:
    """The starting title append_turn gives a new conversation."""
    lines = first_message.strip().splitlines()
    return lines[0][:60] if lines else ""


# The owner often opens a chat with its topic: "Heatpumps. Lets integrate...",
# "Planting av tre. Denne samtalen...", "Aliexpress list: in this...". That
# label is the name he'd give it, so it's used as is, without a model.
_LABEL_RE = re.compile(r"^\s*([^.:!?\n]{2,32}?)\s*[.:](?:\s|$)")


def label_title(first_message: str) -> str | None:
    m = _LABEL_RE.match(first_message or "")
    if not m:
        return None
    label = m.group(1).strip()
    words = label.split()
    if not 1 <= len(words) <= 4 or words[0].lower() in ("from", "hi", "hey", "yo", "ok", "so"):
        return None
    return label[0].upper() + label[1:]


def suggest(user_text: str, reply_text: str) -> str:
    """The name for a conversation from its first exchange: his own label if
    the message starts with one, else a model's."""
    label = label_title(user_text)
    if label:
        return label
    text, usage = _generate(user_text, reply_text)
    llm_client.log_usage("rick", "anthropic", MODEL_ID, usage, None)
    return clean(text)


def clean(title: str) -> str:
    title = title.strip().splitlines()[0] if title.strip() else ""
    title = re.sub(r"^(title|name)\s*:\s*", "", title, flags=re.I)
    title = re.sub(r"[*_`#]+", "", title)
    title = title.strip(" \"'“”‘’").rstrip(".!?:;,").strip(" \"'“”‘’")
    if len(title) > MAX_LEN:
        title = title[:MAX_LEN].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return title


def _first_exchange(agent_id: str, conversation_id: str):
    conversation = vault.get_conversation(agent_id, conversation_id)
    if not conversation:
        return None
    turns = conversation["turns"]
    if len(turns) != 2 or turns[0]["role"] != "user" or turns[1]["role"] == "user":
        return None
    first = turns[0]["content"]
    if conversation["title"] != initial_title(first):
        return None  # renamed, or a named conversation from the start
    return conversation["title"], first, turns[1]["content"]


def _generate(user_text: str, reply_text: str) -> tuple[str, dict]:
    import anthropic
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=MODEL_ID,
        max_tokens=60,
        # Haiku 5.5 otherwise sometimes thinks through the whole budget and
        # returns no text (5 of 9 calls on some chats).
        thinking={"type": "disabled"},
        system=PROMPT,
        messages=[{"role": "user", "content": (
            f"User:\n{user_text[:1500]}\n\nAssistant:\n{reply_text[:1500]}")}],
    )
    usage = {"input_tokens": response.usage.input_tokens or 0, "output_tokens": response.usage.output_tokens or 0}
    return "".join(b.text for b in response.content if b.type == "text"), usage


def name_now(agent_id: str, conversation_id: str) -> str | None:
    """Name the conversation if it has just had its first exchange and still
    has its starting title. Returns the new title, or None."""
    exchange = _first_exchange(agent_id, conversation_id)
    if not exchange:
        return None
    old_title, user_text, reply_text = exchange
    title = label_title(user_text)
    if not title:
        text, usage = _generate(user_text, reply_text)
        llm_client.log_usage(agent_id, "anthropic", MODEL_ID, usage, None)
        title = clean(text)
    if not title or title == old_title:
        return None
    return title if vault.rename_conversation_if(agent_id, conversation_id, old_title, title) else None


def rename_now(agent_id: str, conversation_id: str) -> str | None:
    """One-click Auto-rename from the conversation options: a fresh name
    from its first exchange, whatever it's called now. Keeps the file's
    mtime, so it doesn't show as new. None if there's nothing to name it from."""
    conversation = vault.get_conversation(agent_id, conversation_id)
    if not conversation:
        return None
    turns = conversation["turns"]
    user = next((t["content"] for t in turns if t["role"] == "user"), "")
    reply = next((t["content"] for t in turns if t["role"] != "user"), "")
    if not user.strip():
        return None
    if not os.environ.get("ANTHROPIC_API_KEY") and not label_title(user):
        raise RuntimeError("Auto-rename needs an Anthropic API key")
    title = suggest(user, reply)
    if not title:
        return None
    return title if vault.rename_conversation(agent_id, conversation_id, title) else None


def schedule(agent_id: str, conversation_id: str) -> None:
    """After a reply is saved: name it in the background. Never raises.
    Off without an Anthropic key (tests run without one)."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return

    def run():
        try:
            name_now(agent_id, conversation_id)
        except Exception:  # noqa: BLE001 -- a name is a nicety, never an error
            pass

    threading.Thread(target=run, name="openclave-title", daemon=True).start()
