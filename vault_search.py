"""qmd retrieval: index refresh, keeping denied files out of the index, search
and get. Part of vault.py's facade: import vault, not this module."""

import os
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import yaml

import vault
from vault_paths import _write_text_unlocked
from vault_guard import VaultReadRefused, _HOME_SECRET_FILES, _SECRET_NAME_PREFIXES, _SECRET_SUFFIXES, _confidential_dirs, _secret_dirs, _secret_refusal, _within, confidential_refusal, read_text_anywhere_in_vault


# ── QMD retrieval ───────────────────────────────────────────────────────
#
# Uses BM25 keyword search ("qmd search"), not the hybrid "qmd query" with
# LLM-driven query expansion + reranking. This box has no GPU, and reranking
# alone measured ~370s for 40 candidates on CPU — "qmd query" was silently
# timing out on every chat message and falling back to empty context. BM25
# measured ~0.8s with genuinely relevant results, so it's the default here.
# Revisit if/when reranking runs somewhere with GPU acceleration.

_QMD_REFRESH_MIN_INTERVAL_S = 30


def _qmd_refresh_index(timeout: int = 15) -> None:
    """Best-effort file-index refresh (not embeddings — those are slow on this
    CPU-only box and only matter for the unused hybrid 'qmd query' path).
    Without it the index goes stale for days, and search_vault is blind to
    everything written since. Measured cost for this
    vault's size: ~0.4s for 265 files — cheap enough to run before every
    search rather than needing a separate cron/scheduling mechanism, so
    retrieval is self-healing instead of depending on remembering to run
    `qmd update` manually. Never raises: a failed/slow refresh should not
    block the chat response, just mean this one search runs on a
    slightly-stale index.

    Throttled to once per _QMD_REFRESH_MIN_INTERVAL_S: the staleness this
    guards against is hours/days of inactivity, not the seconds between
    consecutive messages in one conversation — profiled as a real,
    unconditional per-message cost with no benefit at that frequency. Still
    refreshes on the first message after any real idle gap."""
    if not vault.qmd_available():
        return
    now = time.monotonic()
    if now - vault._qmd_last_refresh < _QMD_REFRESH_MIN_INTERVAL_S:
        return
    try:
        _qmd_ensure_collection(timeout)
        _qmd_apply_ignores()
        subprocess.run(
            [vault.QMD_BIN, "--index", vault.QMD_INDEX, "update"],
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, **vault.QMD_ENV},
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    vault._qmd_last_refresh = now


def _qmd_ensure_collection(timeout: int) -> None:
    """A new install has an empty qmd index: index the notes folder the first
    time, so search works without a manual `qmd collection add`."""
    env = {**os.environ, **vault.QMD_ENV}
    listed = subprocess.run([vault.QMD_BIN, "--index", vault.QMD_INDEX, "collection", "list"],
                            capture_output=True, text=True, timeout=timeout, env=env)
    if "No collections" in (listed.stdout + listed.stderr):
        subprocess.run([vault.QMD_BIN, "--index", vault.QMD_INDEX, "collection", "add", str(vault.VAULT_ROOT), "--name", "notes"],
                       capture_output=True, text=True, timeout=max(timeout, 120), env=env)


# ── Keeping denied files out of qmd ───────────────────────────────────────
# qmd indexes **/*.md and already skips dotfiles and dot-folders. The state
# folder and confidential folders hold Markdown too, so they go into each
# collection's `ignore` list in qmd's YAML config; the next `qmd update` then
# drops them from the index. Because `qmd get` also resolves docids and
# path-suffix guesses, an empty index is what keeps them unreachable; the
# checks on search hits and get results below cover an index that has not
# been refreshed yet or a qmd that ignores the setting.

def _qmd_config_file() -> Path:
    return Path(vault.QMD_ENV["XDG_CONFIG_HOME"]) / "qmd" / f"{vault.QMD_INDEX}.yml"


def _qmd_db_file() -> Path:
    return Path(vault.QMD_ENV["XDG_CACHE_HOME"]) / "qmd" / f"{vault.QMD_INDEX}.sqlite"


def _denied_index_dirs() -> tuple[Path, ...]:
    return _secret_dirs() + tuple(folder for _, folder in _confidential_dirs())


def _glob_escape(text: str) -> str:
    return re.sub(r"([*?\[\]{}()!\\])", r"\\\1", text)


def _qmd_ignore_patterns(root: Path) -> list[str]:
    """fast-glob patterns, relative to a collection root, for the denied
    folders inside it ("**" when the root itself is inside one)."""
    patterns = []
    denied = _denied_index_dirs()
    for folder in denied:
        if _within(root, folder):
            return ["**"]
        if any(other in folder.parents for other in denied):
            continue  # already covered by the folder around it
        if root in folder.parents:
            patterns.append(_glob_escape(folder.relative_to(root).as_posix()) + "/**")
    return patterns


def _qmd_apply_ignores() -> None:
    """Add the denied folders to the ignore list of every collection in this
    index's qmd config, keeping any patterns already there. Never raises."""
    f = _qmd_config_file()
    try:
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return
    collections = data.get("collections") if isinstance(data, dict) else None
    if not isinstance(collections, dict):
        return
    changed = False
    for spec in collections.values():
        if not isinstance(spec, dict) or not spec.get("path"):
            continue
        current = spec.get("ignore") if isinstance(spec.get("ignore"), list) else []
        wanted = [p for p in _qmd_ignore_patterns(Path(str(spec["path"])).resolve()) if p not in current]
        if wanted:
            spec["ignore"] = [*current, *wanted]
            changed = True
    if changed:
        try:
            _write_text_unlocked(f, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        except OSError:
            pass


def _qmd_collection_roots() -> dict[str, Path]:
    try:
        data = yaml.safe_load(_qmd_config_file().read_text(encoding="utf-8")) or {}
        collections = data.get("collections") or {}
        return {str(name): Path(str(spec["path"])).resolve() for name, spec in collections.items()
                if isinstance(spec, dict) and spec.get("path")}
    except (OSError, yaml.YAMLError, AttributeError, TypeError):
        return {}


def _qmd_segment(text: str) -> str:
    """One path segment the way qmd stores it: lowercase, runs of anything
    but letters, digits and $ turned into a dash, no dashes at the ends."""
    return re.sub(r"(?:[^\w$]|_)+", "-", text.lower()).strip("-")


def _qmd_parts(path: Path | str) -> list[str]:
    return [s for s in (_qmd_segment(p) for p in Path(path).parts) if s]


def qmd_uri_refusal(uri: str, roots: dict[str, Path] | None = None) -> str | None:
    """read_refusal for a qmd document: "qmd://collection/path" (a ":line"
    suffix and anything after it are ignored) or "collection/path". The path
    is in qmd's lowercased, dashed form, so it is compared segment by segment
    in that form against the denied folders inside the collection's root."""
    text = (uri or "").strip().split()[0] if (uri or "").strip() else ""
    text = re.sub(r":\d+$", "", text.removeprefix("qmd://"))
    collection, _, rel = text.partition("/")
    if not rel:
        return None
    roots = _qmd_collection_roots() if roots is None else roots
    root = roots.get(collection, vault.VAULT_ROOT.resolve())
    segments = [s for s in rel.split("/") if s]
    if any(s.startswith(".") for s in segments):
        return _secret_refusal(Path(rel))
    lowered = [_qmd_segment(s) for s in segments]
    confidential = dict((folder, name) for name, folder in _confidential_dirs())
    for folder in _denied_index_dirs():
        name = confidential.get(folder)
        if _within(root, folder):
            return confidential_refusal(name) if name else _secret_refusal(Path(rel))
        if root in folder.parents:
            prefix = _qmd_parts(folder.relative_to(root))
            if lowered[:len(prefix)] == prefix:
                return confidential_refusal(name) if name else _secret_refusal(Path(rel))
    home = vault.CONFIG.home.resolve()
    if root in home.parents or root == home:
        for secret in _HOME_SECRET_FILES:
            if lowered == _qmd_parts((home / secret).relative_to(root)):
                return _secret_refusal(Path(rel))
    if segments and (Path(segments[-1]).suffix.lower() in _SECRET_SUFFIXES
                     or segments[-1].lower().startswith(_SECRET_NAME_PREFIXES)):
        return _secret_refusal(Path(rel))
    return None


def allowed_qmd_blocks(blocks: list[str]) -> list[str]:
    """Search-result blocks whose document agents may see."""
    roots = _qmd_collection_roots()
    return [b for b in blocks if qmd_uri_refusal(b.split("\n", 1)[0], roots) is None]


def _qmd_get_refusal(output: str) -> str | None:
    """For a `qmd get` result: the refusal when every indexed document with
    exactly this text is denied. None when one is allowed (no new text leaks)
    or the index can't be read (the ignore list is then what keeps denied
    files out)."""
    try:
        with sqlite3.connect(f"file:{_qmd_db_file()}?mode=ro", uri=True) as db:
            rows = db.execute(
                "SELECT d.collection, d.path FROM documents d JOIN content c ON c.hash = d.hash "
                "WHERE d.active = 1 AND trim(c.doc, char(32, 9, 10, 13)) = ?", (output,)).fetchall()
    except sqlite3.Error:
        return None
    roots = _qmd_collection_roots()
    refusals = [qmd_uri_refusal(f"{collection}/{path}", roots) for collection, path in rows]
    if refusals and all(refusals):
        return refusals[0]
    return None


def qmd_query(query: str, timeout: int = 10, max_results: int | None = None) -> str:
    if not vault.qmd_available():
        return f"(vault search is off — qmd executable '{vault.QMD_BIN}' was not found)"
    _qmd_refresh_index()
    try:
        result = subprocess.run(
            [vault.QMD_BIN, "--index", vault.QMD_INDEX, "search", query, *(["-n", str(max_results)] if max_results else [])],
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, **vault.QMD_ENV},
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


SEARCH_MAX_HITS = 12


def _qmd_blocks(output: str) -> list[str]:
    return [b.strip() for b in re.split(r"(?m)^(?=qmd://)", output or "") if b.strip().startswith("qmd://")]


def _hit_file(block: str) -> str:
    return re.split(r"[:\s]", block.split("\n", 1)[0][len("qmd://"):], maxsplit=1)[0]


def search_vault(query: str, max_hits: int = SEARCH_MAX_HITS) -> str:
    """The search_vault tool. qmd's keyword search needs every word to match
    and does not translate, so "travel insurance" missed a note called
    Forsikringsoversikt. Alternatives split on "|" (e.g. the
    same words in another language) run as separate searches; a multi-word
    alternative with no hits is retried word by word. Results are merged
    round-robin, one block per file, conversation logs last. Hits that
    read_refusal/qmd_uri_refusal keep from agents are dropped."""
    alternatives = [a.strip() for a in query.split("|") if a.strip()] or [query.strip()]
    per_alt = []
    for alt in alternatives:
        blocks = allowed_qmd_blocks(_qmd_blocks(vault.qmd_query(alt)))
        words = alt.split()
        if not blocks and len(words) > 1:
            for word in words:
                if len(word) >= 3:
                    blocks += allowed_qmd_blocks(_qmd_blocks(vault.qmd_query(word, max_results=3)))
        per_alt.append(blocks)
    merged, seen = [], set()
    for rank in range(max(map(len, per_alt), default=0)):
        for blocks in per_alt:
            if rank < len(blocks) and _hit_file(blocks[rank]) not in seen:
                seen.add(_hit_file(blocks[rank]))
                merged.append(blocks[rank])
    merged.sort(key=lambda b: "/conversations/" in b.split("\n", 1)[0])  # stable: notes first
    return "\n\n".join(merged[:max_hits])


def search_language_hint() -> str:
    langs = vault.CONFIG.note_languages
    if len(langs) < 2:
        return "Separate alternative searches with | (e.g. synonyms)."
    example = ("'travel insurance | reiseforsikring'" if "Norwegian" in langs
               else f"'<words> | <the same words in {langs[1]}>'")
    return (f"Notes are written in {', '.join(langs[:-1])} and {langs[-1]}, and the search does not translate: "
            f"always give the keywords in each language as alternatives separated by |, e.g. {example}.")


def qmd_get(path_or_uri: str, timeout: int = 10) -> str:
    """Full contents of one file, resolved by qmd itself (handles qmd:// URIs
    and its lowercased/dashed path convention) so callers don't have to guess
    the real on-disk path/casing. Denied files (read_refusal) come back as a
    "refused: ..." message the model can relay."""
    # Context-directory paths must work even before QMD indexes a note, and
    # must reflect current edits. Keep URI/search-result resolution with QMD.
    if Path(path_or_uri).is_absolute():
        try:
            return read_text_anywhere_in_vault(Path(path_or_uri))
        except VaultReadRefused as exc:
            return str(exc)
        except (OSError, ValueError) as exc:
            return f"(could not read '{path_or_uri}': {exc})"
    refusal = qmd_uri_refusal(path_or_uri)
    if refusal:
        return refusal
    if not vault.qmd_available():
        return f"(vault search is off — qmd executable '{vault.QMD_BIN}' was not found; use an absolute vault path to read a file)"
    try:
        result = subprocess.run(
            [vault.QMD_BIN, "--index", vault.QMD_INDEX, "get", path_or_uri],
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, **vault.QMD_ENV},
        )
        if result.returncode != 0:
            return f"(could not read '{path_or_uri}': {result.stderr.strip()[:200]})"
        text = result.stdout.strip()
        return _qmd_get_refusal(text) or text
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return f"(timed out reading '{path_or_uri}')"


def qmd_available() -> bool:
    """Whether the configured QMD executable is available on this host."""
    return shutil.which(vault.QMD_BIN) is not None
