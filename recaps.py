"""Recaps: what an archived conversation was about, at a glance (2026-11-03).

Archiving a conversation (by hand, or from the manager's weekly cleanup) moves
it to conversations/archived/ and, in the background, a fast model writes a
recap into the archived file's front matter:

    Recap:
      about: one or two sentences
      status: concluded | open | dropped
      open_ends: one line, or empty
      made: YYYY-MM-DD

Each agent's Recaps tab lists these cards instead of the chats. The transcript
stays in the file underneath, one tap away (read it, or restore the chat), so
nothing is lost. The recap only touches the front matter and keeps the file's
mtime, which is the conversation's "last active" date.

Off without an Anthropic API key (tests run without one); a card then says
"No recap yet" and offers to write one.
"""
import json
import os
import re
import threading
from datetime import datetime

import llm_client
import vault

MODEL_ID = "claude-haiku-5-5"
STATUSES = ("concluded", "open", "dropped")
MAX_ABOUT = 320
MAX_OPEN = 200
MAX_INPUT = 24_000  # characters of transcript sent to the model
PROMPT = (
    "You write a recap card for an archived chat between a person and their AI assistant, so they can "
    "grasp it in five seconds months later. Reply with JSON only, no code fence:\n"
    '{"about": "...", "status": "concluded|open|dropped", "open_ends": "..."}\n'
    "about: one or two short sentences: what it was about and what came out of it (the decision, answer "
    "or result, with the concrete names and numbers that matter). No 'The user asked'; write it like a "
    "note: 'Picked the Nibe S735 over Panasonic; quote due in March.'\n"
    "status: concluded = it reached an answer or decision; open = it stopped with something still to do "
    "or decide; dropped = it fizzled out or was abandoned without a result.\n"
    "open_ends: one line naming what was left undone, or an empty string if nothing.\n"
    "Write in the language of the chat."
)

_LOCK = threading.Lock()
_RUNNING: set[tuple[str, str]] = set()


def _archived_file(agent_id: str, conversation_id: str):
    return vault.archived_conversation_file(agent_id, conversation_id)


def _transcript(agent_id: str, conversation_id: str) -> tuple[dict, str] | None:
    f = _archived_file(agent_id, conversation_id)
    if not f.exists():
        return None
    fm, body = vault.split_frontmatter(f.read_text(encoding="utf-8"))
    turns = vault._raw_turns(body, vault.AGENTS[agent_id].name, vault._speaker_names(agent_id, fm))
    summary = vault._SUMMARY_RE.search(body)
    parts = [f"(Earlier part, summarised: {summary.group(2).strip()})"] if summary else []
    parts += [f"{'Person' if t['role'] == 'user' else 'Assistant'}: {t['content'].strip()}" for t in turns]
    text = "\n\n".join(parts)
    if len(text) > MAX_INPUT:
        # Beginning (the question) and end (where it landed) matter most.
        half = MAX_INPUT // 2
        text = text[:half] + "\n\n[…]\n\n" + text[-half:]
    return fm, text


def parse(text: str) -> dict | None:
    """The model's JSON reply as a clean recap, or None."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    about = " ".join(str(data.get("about") or "").split())[:MAX_ABOUT]
    status = str(data.get("status") or "").strip().lower()
    if not about or status not in STATUSES:
        return None
    open_ends = " ".join(str(data.get("open_ends") or "").split())[:MAX_OPEN]
    if open_ends.lower() in ("none", "nothing", "-", "n/a"):
        open_ends = ""
    return {"about": about, "status": status, "open_ends": open_ends}


def _generate(title: str, transcript: str) -> tuple[str, dict]:
    import anthropic
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=MODEL_ID,
        max_tokens=400,
        thinking={"type": "disabled"},  # see conversation_titles._generate
        system=PROMPT,
        messages=[{"role": "user", "content": f"Chat title: {title}\n\n{transcript}"}],
    )
    usage = {"input_tokens": response.usage.input_tokens or 0, "output_tokens": response.usage.output_tokens or 0}
    return "".join(b.text for b in response.content if b.type == "text"), usage


def save(agent_id: str, conversation_id: str, recap: dict) -> bool:
    """Write a recap into the archived file's front matter, keeping its mtime."""
    f = _archived_file(agent_id, conversation_id)
    with vault.file_lock(agent_id, f) as scoped:
        if not scoped.exists():
            return False
        fm, body = vault.split_frontmatter(scoped.read_text(encoding="utf-8"))
        fm["Recap"] = {**recap, "made": datetime.now().strftime("%Y-%m-%d")}
        vault._write_keeping_mtime(scoped, vault.render_frontmatter(fm, body))
    return True


def make_now(agent_id: str, conversation_id: str, generate=None) -> dict | None:
    """Write the recap for one archived conversation. Returns it, or None."""
    got = _transcript(agent_id, conversation_id)
    if not got:
        return None
    fm, transcript = got
    if not transcript.strip():
        recap = {"about": "Empty chat: nothing was said.", "status": "dropped", "open_ends": ""}
    else:
        text, usage = (generate or _generate)(str(fm.get("Title") or conversation_id), transcript)
        if usage:
            llm_client.log_usage(agent_id, "anthropic", MODEL_ID, usage, None)
        recap = parse(text)
        if not recap:
            return None
    return recap if save(agent_id, conversation_id, recap) else None


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def schedule(agent_id: str, conversation_id: str) -> None:
    """After an archive: write its recap in the background. Never raises."""
    if not available():
        return
    key = (agent_id, conversation_id)
    with _LOCK:
        if key in _RUNNING:
            return
        _RUNNING.add(key)

    def run():
        try:
            make_now(agent_id, conversation_id)
        except Exception:  # noqa: BLE001 -- a recap is a nicety, never an error
            pass
        finally:
            with _LOCK:
                _RUNNING.discard(key)

    threading.Thread(target=run, name="openclave-recap", daemon=True).start()


def _card(agent_id: str, f) -> dict:
    fm, _ = vault.split_frontmatter(f.read_text(encoding="utf-8"))
    recap = fm.get("Recap") if isinstance(fm.get("Recap"), dict) else None
    status = recap.get("status") if recap else None
    return {
        "id": f.stem,
        "title": str(fm.get("Title", f.stem)),
        "started": str(fm.get("Date", "")),
        "last_active": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d"),
        "about": str(recap.get("about") or "") if recap else "",
        "status": status if status in STATUSES else None,
        "open_ends": str(recap.get("open_ends") or "") if recap else "",
        "pending": (agent_id, f.stem) in _RUNNING,
    }


def card(agent_id: str, conversation_id: str) -> dict | None:
    """One archived conversation's card, or None."""
    f = _archived_file(agent_id, conversation_id)
    return _card(agent_id, f) if f.exists() else None


def list_for(agent_id: str) -> list[dict]:
    """Recap cards for an agent's archived conversations, last active first."""
    d = vault.conversations_dir(agent_id) / vault.ARCHIVED_DIR
    files = sorted(d.glob("*.md") if d.exists() else (), key=lambda f: f.stat().st_mtime, reverse=True)
    return [_card(agent_id, f) for f in files]


def concluded_since(agent_id: str, since_ts: float) -> list[dict]:
    """Concluded recaps archived or recapped after a time: material the
    manager's audit looks at for things worth keeping in the main vault."""
    d = vault.conversations_dir(agent_id) / vault.ARCHIVED_DIR
    out = []
    for f in (d.glob("*.md") if d.exists() else ()):
        card = _card(agent_id, f)
        made = None
        try:
            fm, _ = vault.split_frontmatter(f.read_text(encoding="utf-8"))
            made = datetime.strptime(str(fm["Recap"]["made"]), "%Y-%m-%d").timestamp()
        except (KeyError, TypeError, ValueError):
            pass
        if card["status"] == "concluded" and made is not None and made >= since_ts - 86_400:
            out.append(card)
    return out


def backfill(agent_ids) -> None:
    """Once at startup: recaps for archived conversations that have none yet
    (archived before recaps existed, or the call failed). One at a time."""
    import scheduler
    agent_ids = list(agent_ids)

    def run():
        if not available():
            return
        for agent_id in agent_ids:
            try:
                cards = list_for(agent_id)
            except Exception:  # noqa: BLE001
                continue
            for card in cards:
                if card["status"] is None:
                    try:
                        make_now(agent_id, card["id"])
                    except Exception:  # noqa: BLE001
                        pass

    # Once, right after startup; one model call per conversation, so in the worker pool.
    scheduler.register("recap-backfill", "Recaps for archived chats", run, worker=True)
