"""Main-vault writes (outside agent folders) for accepted manager proposals.
Part of vault.py's facade: import vault, not this module."""

import hashlib
import re
import tempfile
import fcntl
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import yaml

import vault
from vault_paths import _write_text_unlocked
from vault_guard import read_refusal


# ── The main vault (outside agent folders): manager proposals only ────────
# The manager's weekly audit proposes notes worth keeping in the owner's main
# vault (proposals.py); this writes one only after the owner said yes. It
# never touches agent folders, configured confidential folders or anything
# but Markdown; never creates folders and never deletes: it appends to an
# existing note or creates a new note in an existing folder.
class MainVaultError(ValueError):
    pass


_CHANGELOG_RE = re.compile(r"^## Changelog[ \t]*$", re.M)


def main_vault_target(rel_path: str) -> Path:
    """Resolve and check a vault-relative note path. Raises MainVaultError."""
    rel_path = (rel_path or "").strip().strip("/")
    if rel_path.startswith("qmd://"):
        rel_path = rel_path.split("/", 3)[-1]
    p = Path(rel_path)
    if not rel_path or p.is_absolute() or ".." in p.parts or rel_path.startswith("~"):
        raise MainVaultError("give the note's path inside the vault, like 'Area/Projects/Note.md'")
    if p.suffix.lower() != ".md":
        raise MainVaultError("only Markdown notes (.md)")
    f = (vault.VAULT_ROOT / p).resolve()
    if vault.VAULT_ROOT not in f.parents:
        raise MainVaultError("that path is outside the vault")
    agents_root = vault.AGENTS_ROOT.resolve()
    if f == agents_root or agents_root in f.parents:
        raise MainVaultError("that's inside an agent's folder; propose a note in the main vault")
    for name in vault.CONFIG.confidential_folders:
        folder = (vault.VAULT_ROOT / name).resolve()
        if folder in f.parents:
            raise MainVaultError("that folder is confidential; nothing is written there")
    if any(part.startswith(".") for part in f.relative_to(vault.VAULT_ROOT).parts):
        raise MainVaultError("hidden folders are off limits")
    if read_refusal(f):
        raise MainVaultError("that's OpenClave's own settings or state; nothing is written there")
    return f


@contextmanager
def _main_vault_lock(path: Path):
    lock_dir = Path(tempfile.gettempdir()) / "openclave-file-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(str(path).encode()).hexdigest()
    with open(lock_dir / f"{digest}.lock", "a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield path
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def write_main_vault_note(rel_path: str, content: str, mode: str, change_summary: str) -> str:
    """mode 'append': add content to an existing note (before its
    '## Changelog', if it has one); 'create': a new note in an existing folder.
    A note with a '## Changelog' section gets a dated [BOT] line there.
    Returns the vault-relative path written."""
    f = main_vault_target(rel_path)
    content = (content or "").strip()
    if not content:
        raise MainVaultError("nothing to write")
    if mode not in ("append", "create"):
        raise MainVaultError("mode must be append or create")
    if not f.parent.is_dir():
        raise MainVaultError(f"the folder {f.parent.relative_to(vault.VAULT_ROOT)} doesn't exist; new notes go in existing folders")
    stamp = f"- {datetime.now().strftime('%Y-%m-%d')} [BOT] {' '.join((change_summary or 'Added from OpenClave').split())[:200]}"
    with _main_vault_lock(f):
        if mode == "create":
            if f.exists():
                raise MainVaultError("that note already exists; propose an append instead")
            if content.startswith("---\n"):
                end = content.find("\n---", 4)
                try:
                    if end == -1 or not isinstance(yaml.safe_load(content[4:end]) or {}, dict):
                        raise MainVaultError("the new note's front matter isn't valid")
                except yaml.YAMLError as exc:
                    raise MainVaultError("the new note's front matter isn't valid YAML") from exc
            text = content + "\n"
            if _CHANGELOG_RE.search(text):
                text = text.rstrip("\n") + "\n" + stamp + "\n"
        else:
            if not f.exists():
                raise MainVaultError("that note doesn't exist; propose creating it instead")
            text = f.read_text(encoding="utf-8")
            m = _CHANGELOG_RE.search(text)
            if m:
                before, after = text[:m.start()].rstrip("\n"), text[m.start():].rstrip("\n")
                text = f"{before}\n\n{content}\n\n{after}\n{stamp}\n"
            else:
                text = text.rstrip("\n") + f"\n\n{content}\n"
        _write_text_unlocked(f, text)
    return str(f.relative_to(vault.VAULT_ROOT))
