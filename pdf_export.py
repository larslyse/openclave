"""Create a print-ready PDF of an allowed note in the configured vault."""
import os
import re
import subprocess
import sys
from pathlib import Path

import vault

VAULT_PDF = vault.CONFIG.pdf_tool
CONFIDENTIAL_FOLDERS = tuple(vault.VAULT_ROOT / name for name in vault.CONFIG.confidential_folders)
# Keep the old single-folder seam for callers/tests that patch it directly.
CONFIDENTIAL = CONFIDENTIAL_FOLDERS[0] if CONFIDENTIAL_FOLDERS else None
_SKIP_PARTS = {".obsidian", ".trash", ".git"}
TIMEOUT_S = 180


class PdfError(Exception):
    pass


def _norm(text: str) -> str:
    """qmd lowercases paths and turns spaces into dashes; compare that way."""
    return re.sub(r"[\s_\-]+", "-", text.strip().lower())


def _allowed(path: Path) -> bool:
    path = path.resolve()
    confidential = tuple(p.resolve() for p in CONFIDENTIAL_FOLDERS)
    if CONFIDENTIAL is not None and CONFIDENTIAL not in confidential:
        confidential += (CONFIDENTIAL.resolve(),)
    return (vault.VAULT_ROOT in path.parents and not any(p == path or p in path.parents for p in confidential)
            and not _SKIP_PARTS & set(path.relative_to(vault.VAULT_ROOT).parts))


def resolve_note(note: str) -> Path:
    """An absolute vault path, a vault-relative path, a qmd:// URI from
    search_vault, or just the note's name."""
    raw = (note or "").strip()
    if not raw:
        raise PdfError("say which note")
    raw = re.sub(r"^qmd://[^/]+/", "", raw)
    candidates = [Path(raw)] if Path(raw).is_absolute() else [vault.VAULT_ROOT / raw]
    for c in list(candidates):
        if c.suffix != ".md":
            candidates.append(c.with_name(c.name + ".md"))
    for c in candidates:
        c = c.resolve()
        if c.is_file():
            if not _allowed(c) and any(p == c or p in c.parents for p in _confidential_paths()):
                names = ", ".join(p.name for p in _confidential_paths())
                raise PdfError(f"notes in confidential folder(s) {names} are never exported")
            if not _allowed(c):
                raise PdfError("that file is outside the notes the user lets agents export")
            return c
    # By name (or a qmd path, whose casing/dashes differ from the real file)
    want = _norm(raw[:-3] if raw.lower().endswith(".md") else raw)
    want_name = want.rsplit("/", 1)[-1]
    by_path, by_name = [], []
    for f in vault.VAULT_ROOT.rglob("*.md"):
        if not _allowed(f):
            continue
        rel = _norm(str(f.relative_to(vault.VAULT_ROOT))[:-3])
        if rel == want:
            by_path.append(f)
        elif _norm(f.stem) == want_name:
            by_name.append(f)
    hits = by_path or by_name
    if not hits:
        raise PdfError(f"no note found for '{note}'")
    if len(hits) > 1:
        listed = "\n".join(f"- {h.relative_to(vault.VAULT_ROOT)}" for h in sorted(hits))
        raise PdfError(f"several notes match; call again with one of these paths:\n{listed}")
    return hits[0]


def make_pdf(note: str) -> str:
    """Make the PDF and say where it went (plus any warning, e.g. a missing image)."""
    src = resolve_note(note)
    if VAULT_PDF is None:
        raise PdfError("PDF export is disabled; configure paths.pdf_tool to enable it")
    if not VAULT_PDF.is_file():
        raise PdfError(f"the PDF tool isn't installed ({VAULT_PDF})")
    env = {**os.environ, "PATH": os.environ.get("PATH", "/usr/bin:/bin") + ":/snap/bin"}
    try:
        result = subprocess.run([sys.executable, str(VAULT_PDF), str(src)],
                                capture_output=True, text=True, timeout=TIMEOUT_S, env=env)
    except subprocess.TimeoutExpired:
        raise PdfError(f"making the PDF took over {TIMEOUT_S}s and was stopped")
    out = src.with_suffix(".pdf")
    if result.returncode != 0 or not out.is_file():
        raise PdfError((result.stderr or result.stdout).strip()[-600:] or "the PDF tool failed")
    warnings = "\n".join(l for l in result.stderr.splitlines() if l.startswith("warning:"))
    msg = (f"PDF saved: {out.relative_to(vault.VAULT_ROOT)} ({out.stat().st_size // 1024} KB). "
           "It syncs wherever the notes sync.")
    return msg + (f"\n{warnings}" if warnings else "")


def _confidential_paths() -> tuple[Path, ...]:
    paths = tuple(p.resolve() for p in CONFIDENTIAL_FOLDERS)
    if CONFIDENTIAL is not None and CONFIDENTIAL.resolve() not in paths:
        paths += (CONFIDENTIAL.resolve(),)
    return paths
