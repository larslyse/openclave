"""Attachments: images and documents saved alongside a conversation. Part of
vault.py's facade: import vault, not this module."""

import json
import hashlib
import re
from pathlib import Path

import vault
from vault_paths import _write_bytes_unlocked, _write_text_unlocked, conversations_dir, file_lock


# ── Attachments (images pasted/uploaded into a conversation) ─────────────
#
# One conversation-scoped subfolder per attachment id, not a flat pool —
# keeps them alongside the conversation they belong to (delete_conversation
# above cleans this up too) and keeps the id-collision surface small (ids are
# a content hash, scoped per conversation, not globally unique).
#
# Bytes and metadata are separate files (<id>.bin / <id>.json) rather than
# guessing a file extension back from media_type on read — simpler and keeps
# the original filename/size available without re-deriving them.

_ATTACHMENT_ID_RE = re.compile(r"^[A-Za-z0-9]{8,64}$")
IMAGE_MEDIA_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
# Documents: every agent takes attachments, whatever its model. Their text
# is extracted and added to the message (attachment_text), so they work on
# every model; images go to the model as images.
DOCUMENT_MEDIA_TYPES = {"application/pdf", "text/plain", "text/markdown", "text/csv", "application/json"}
ATTACHMENT_MEDIA_TYPES = IMAGE_MEDIA_TYPES | DOCUMENT_MEDIA_TYPES
MAX_DOCUMENT_TEXT_CHARS = 60_000
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # raw upload cap; clients are expected to downscale before this


def attachments_dir(agent_id: str, conversation_id: str) -> Path:
    # Reuses conversation_file's id validation, without requiring the
    # conversation to already exist — an attachment can be uploaded before
    # the first message that references it.
    vault.conversation_file(agent_id, conversation_id)
    return conversations_dir(agent_id) / "attachments" / conversation_id


def _attachment_base(agent_id: str, conversation_id: str, attachment_id: str) -> Path:
    if not _ATTACHMENT_ID_RE.fullmatch(attachment_id):
        raise ValueError("invalid attachment id")
    d = attachments_dir(agent_id, conversation_id).resolve()
    base = (d / attachment_id).resolve()
    if base.parent != d:
        raise ValueError("invalid attachment id")
    return base


def save_attachment(agent_id: str, conversation_id: str, filename: str, media_type: str, data: bytes) -> dict:
    if media_type not in ATTACHMENT_MEDIA_TYPES:
        raise ValueError(f"unsupported media type: {media_type}")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError(f"attachment too large: {len(data)} bytes (max {MAX_ATTACHMENT_BYTES})")
    attachment_id = hashlib.sha256(data).hexdigest()[:16]
    base = _attachment_base(agent_id, conversation_id, attachment_id)
    meta = {"id": attachment_id, "filename": filename, "media_type": media_type, "size": len(data)}
    data_file, meta_file = base.with_suffix(".bin"), base.with_suffix(".json")
    with file_lock(agent_id, data_file):
        if not data_file.exists():
            _write_bytes_unlocked(data_file, data)
            _write_text_unlocked(meta_file, json.dumps(meta))
    return meta


def read_attachment(agent_id: str, conversation_id: str, attachment_id: str) -> tuple[bytes, str] | None:
    meta = get_attachment_meta(agent_id, conversation_id, attachment_id)
    if meta is None:
        return None
    base = _attachment_base(agent_id, conversation_id, attachment_id)
    return base.with_suffix(".bin").read_bytes(), meta["media_type"]


def get_attachment_meta(agent_id: str, conversation_id: str, attachment_id: str) -> dict | None:
    try:
        base = _attachment_base(agent_id, conversation_id, attachment_id)
    except ValueError:
        return None
    meta_file = base.with_suffix(".json")
    if not meta_file.exists():
        return None
    return json.loads(meta_file.read_text(encoding="utf-8"))


def attachment_markdown_ref(conversation_id: str, meta: dict) -> str:
    bang = "!" if meta.get("media_type") in IMAGE_MEDIA_TYPES else ""
    return f"{bang}[{meta['filename']}](attachments/{conversation_id}/{meta['id']})"


def attachment_text(agent_id: str, conversation_id: str, attachment_id: str) -> str:
    """A document attachment's text for the model (PDF pages, or the file as
    UTF-8), capped at MAX_DOCUMENT_TEXT_CHARS. Never raises: says what went
    wrong instead, so the agent can tell the user."""
    found = read_attachment(agent_id, conversation_id, attachment_id)
    if not found:
        return "(file not found)"
    data, media_type = found
    try:
        if media_type == "application/pdf":
            import io
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(data))
            text = "\n\n".join(f"[page {i + 1}]\n{(page.extract_text() or '').strip()}" for i, page in enumerate(reader.pages))
            if not re.search(r"\w", re.sub(r"\[page \d+\]", "", text)):
                return f"(a {len(reader.pages)}-page PDF with no extractable text: probably scanned images)"
        else:
            text = data.decode("utf-8", errors="replace")
    except Exception as exc:
        return f"(couldn't read this file: {type(exc).__name__})"
    if len(text) > MAX_DOCUMENT_TEXT_CHARS:
        text = text[:MAX_DOCUMENT_TEXT_CHARS] + f"\n… (cut here: the file has {len(text):,} characters)"
    return text


_ATTACHMENT_REF_RE = re.compile(r"!?\[[^\]]*\]\(attachments/[^/]+/([A-Za-z0-9]+)\)")


def turn_attachments(agent_id: str, conversation_id: str, content: str) -> list[dict]:
    out = []
    for attachment_id in _ATTACHMENT_REF_RE.findall(content):
        meta = get_attachment_meta(agent_id, conversation_id, attachment_id)
        if meta:
            out.append(meta)
    return out
