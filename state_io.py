"""Reading and writing the JSON state files (OPENCLAVE_HOME/state/*.json and
the JSON files in agent folders) the same safe way everywhere.

save_json writes a temp file in the same folder, fsyncs it and renames it
over the old one, so a crash or a full disk leaves either the old file or the
new one, never half of each. load_json returns a default for a missing file;
a file that is not valid JSON is moved aside to `<name>.corrupt` (so nothing
is silently lost when the next save writes a fresh one) and the default is
returned.

Locking stays with each caller: these functions only make a single read or
write safe, not a read-modify-write.
"""
import copy
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any


def load_json(path: Path, default: Any = None) -> Any:
    """The parsed contents of `path`, or a copy of `default` when the file is
    missing, unreadable or corrupt."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return copy.deepcopy(default)
    except UnicodeDecodeError:
        _set_aside(path)
        return copy.deepcopy(default)
    except OSError as exc:
        print(f"state file {path} unreadable ({exc}); using defaults")
        return copy.deepcopy(default)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        _set_aside(path)
        return copy.deepcopy(default)


def _set_aside(path: Path) -> None:
    aside = path.with_name(path.name + ".corrupt")
    try:
        os.replace(path, aside)
        print(f"state file {path} was not valid JSON; kept it as {aside.name} and started fresh")
    except OSError as exc:
        print(f"state file {path} was not valid JSON and could not be moved aside: {exc}")


def save_json(path: Path, data: Any, *, indent: int | None = 2, ensure_ascii: bool = True,
              mode: int | None = None) -> None:
    """Replace `path` with `data` as JSON (plus a final newline), atomically.

    The new file keeps the old file's permissions; a new file is private to
    the owner (0600) unless `mode` says otherwise. `mode` always wins.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=indent, ensure_ascii=ensure_ascii) + "\n"
    if mode is None:
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            mode = None
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temp:
            temp.write(text)
            temp.flush()
            os.fsync(temp.fileno())
        if mode is not None:
            os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
