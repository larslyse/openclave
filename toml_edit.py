"""Change one key in openclave.toml without touching the rest of the file.

The installer, `openclave` and the Control Panel (Phone, Update) each need to
set a single value such as `[network] port` or `[network] public_url`. A TOML
library would rewrite the whole file and drop the user's comments, so this
edits the text: it replaces the key's line inside its section, or adds the
line at the end of that section, or adds the section at the end of the file.
The result is parsed again before it is written, so a mistake never leaves a
broken settings file.

    python3 toml_edit.py <file> <section> <key> <value> [--int | --bool]
"""
import os
import re
import sys
import tempfile
import tomllib
from pathlib import Path

_KEY = re.compile(r"[A-Za-z0-9_-]+")


def _literal(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    if "\n" in text or "\r" in text:
        raise ValueError("value must be one line")
    return f'"{text}"'


def set_key_text(text: str, section: str, key: str, value) -> str:
    """Return `text` with `[section] key = value` set."""
    if not _KEY.fullmatch(section) or not _KEY.fullmatch(key):
        raise ValueError("section and key must be plain names")
    line = f"{key} = {_literal(value)}"
    lines = text.splitlines()
    header = re.compile(rf"^\s*\[\s*{re.escape(section)}\s*\]\s*(#.*)?$")
    any_header = re.compile(r"^\s*\[")
    key_line = re.compile(rf"^\s*{re.escape(key)}\s*=")
    start = next((i for i, l in enumerate(lines) if header.match(l)), None)
    if start is None:
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ([""] if lines else []) + [f"[{section}]", line]
    else:
        end = next((i for i in range(start + 1, len(lines)) if any_header.match(lines[i])), len(lines))
        found = next((i for i in range(start + 1, end) if key_line.match(lines[i])), None)
        if found is not None:
            lines[found] = line
        else:
            last = end - 1
            while last > start and not lines[last].strip():
                last -= 1
            lines.insert(last + 1, line)
    result = "\n".join(lines) + "\n"
    parsed = tomllib.loads(result)  # raises on anything broken; nothing is written then
    if parsed.get(section, {}).get(key) != value:
        raise ValueError(f"couldn't set [{section}] {key} in this file; edit it by hand")
    return result


def set_key(path: Path | str, section: str, key: str, value) -> None:
    """Set one key in the TOML file at `path` (created if missing), keeping
    its other lines, comments and file mode."""
    path = Path(path)
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    new = set_key_text(old, section, key, value)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".openclave.toml.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(new)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 4:
        print(__doc__.strip().splitlines()[-1].strip(), file=sys.stderr)
        return 2
    path, section, key, raw = args
    value = raw
    if "--int" in argv:
        value = int(raw)
    elif "--bool" in argv:
        value = raw.strip().lower() in {"1", "true", "yes", "y"}
    try:
        set_key(path, section, key, value)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"Couldn't change {path}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
