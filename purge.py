"""Purge a conversation (2026-11-05): keep the chat and its name, drop the
transcript, keep only a short summary when there's durable context in it.

  preview  -> a suggested summary ("" when nothing is worth keeping) plus
              the agent files that mention the chat, so the owner can check
              before anything changes
  apply    -> the full transcript is archived first (conversations/archive/,
              the same as compaction), then replaced by the summary block;
              front matter Purged: <archive file> remembers where it went
  undo     -> puts the archived transcript back exactly as it was

Attachments are never touched. Nothing is purged without the owner's
confirmation in the UI.
"""
import re

import llm_client
import recaps
import vault

MODEL_ID = recaps.MODEL_ID
SHORT_CHARS = 600   # a chat this small has nothing worth summarising
PROMPT = (
    "You compress a finished chat between a person and their assistant into the little that is worth keeping. "
    "Keep only durable context: facts about the person or their things, decisions made, numbers, and open items. "
    "Drop greetings, back-and-forth, explanations and anything easy to look up again. At most 80 words, plain "
    "bullets, in the chat's language. If nothing is worth keeping, reply with exactly NOTHING."
)


def _transcript(agent_id: str, conversation_id: str) -> tuple[dict, str]:
    convo = vault.get_conversation(agent_id, conversation_id)
    if convo is None:
        raise FileNotFoundError("conversation not found")
    parts = [f"(Earlier, summarised: {convo['summary']})"] if convo.get("summary") else []
    parts += [f"{'Person' if t['role'] == 'user' else 'Assistant'}: {t['content'].strip()}" for t in convo["turns"]]
    text = "\n\n".join(parts)
    if len(text) > recaps.MAX_INPUT:
        half = recaps.MAX_INPUT // 2
        text = text[:half] + "\n\n[…]\n\n" + text[-half:]
    return convo, text


def _generate(transcript: str) -> tuple[str, dict]:
    import anthropic
    response = anthropic.Anthropic().messages.create(
        model=MODEL_ID, max_tokens=300, thinking={"type": "disabled"}, system=PROMPT,
        messages=[{"role": "user", "content": transcript}],
    )
    usage = {"input_tokens": response.usage.input_tokens or 0, "output_tokens": response.usage.output_tokens or 0}
    return "".join(b.text for b in response.content if b.type == "text"), usage


def _mentions(agent_id: str, conversation_id: str, title: str) -> list[str]:
    """Agent files (not chats) that mention this chat by id or title."""
    root = vault.agent_root(agent_id)
    needles = [conversation_id] + ([title] if len(title) >= 12 else [])
    found = []
    for f in sorted(root.rglob("*.md")):
        rel = f.relative_to(root)
        if rel.parts[0] in ("conversations", ".page-history"):
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if any(n in text for n in needles):
            found.append(str(rel))
    return found[:20]


def preview(agent_id: str, conversation_id: str, generate=None) -> dict:
    convo, transcript = _transcript(agent_id, conversation_id)
    turns = len(convo["turns"])
    summary = ""
    if len(transcript) > SHORT_CHARS and (generate or recaps.available()):
        text, usage = (generate or _generate)(transcript)
        if usage:
            llm_client.log_usage(agent_id, "anthropic", MODEL_ID, usage, None)
        summary = "" if text.strip().upper().startswith("NOTHING") else text.strip()
    return {"title": convo.get("title") or conversation_id, "turns": turns, "chars": len(transcript),
            "summary": summary, "mentioned_in": _mentions(agent_id, conversation_id, convo.get("title") or "")}


def apply(agent_id: str, conversation_id: str, summary: str) -> dict:
    if vault.get_conversation(agent_id, conversation_id) is None:
        raise FileNotFoundError("conversation not found")
    summary = (summary or "").strip()[:4000] or "(Purged: nothing in it was worth keeping.)"
    archive = vault.compact_conversation(agent_id, conversation_id, summary, [])
    f = vault.conversation_file(agent_id, conversation_id)
    with vault.file_lock(agent_id, f) as scoped:
        fm, body = vault.split_frontmatter(scoped.read_text(encoding="utf-8"))
        fm["Purged"] = archive
        vault._write_text_unlocked(scoped, vault.render_frontmatter(fm, body))
    return {"purged": True, "archive": archive}


def undo(agent_id: str, conversation_id: str) -> dict:
    f = vault.conversation_file(agent_id, conversation_id)
    with vault.file_lock(agent_id, f) as scoped:
        if not scoped.exists():
            raise FileNotFoundError("conversation not found")
        fm, _ = vault.split_frontmatter(scoped.read_text(encoding="utf-8"))
        archive = str(fm.get("Purged") or "")
        if not re.fullmatch(r"[\w.-]+\.md", archive):
            raise ValueError("this chat wasn't purged")
        source = vault.conversations_dir(agent_id) / "archive" / archive
        if not source.exists():
            raise ValueError("the purged transcript is gone")
        vault._write_text_unlocked(scoped, source.read_text(encoding="utf-8"))
    return {"restored": True}
