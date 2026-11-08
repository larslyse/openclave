"""Refuse commits that contain your personal details.

The patterns come from a list kept outside the repo, so the list itself
never becomes public: ``$OPENCLAVE_PRIVATE_STRINGS``, else
``~/.openclave/private-strings.txt``. One pattern per line, matched without
regard to case; ``re:`` starts a regular expression (case-sensitive), and
``#`` starts a comment. For example::

    # my details
    re:\\bAlex\\b
    alex@example.com
    192.168.1.
    myhost

Modes:
    --staged   lines added in the staged diff, plus the commit author
               (the pre-commit hook in .githooks runs this)
    --all      every tracked file, e.g. for a sweep before a release

Exits 1 when something matches. Without a list it says so and passes, so
contributors without one aren't blocked.
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


def list_path() -> Path:
    return Path(os.environ.get("OPENCLAVE_PRIVATE_STRINGS") or Path.home() / ".openclave" / "private-strings.txt")


def load_patterns(path: Path) -> list[tuple[str, re.Pattern]]:
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("re:"):
            out.append((line, re.compile(line[3:])))
        else:
            out.append((line, re.compile(re.escape(line), re.IGNORECASE)))
    return out


def find(text: str, patterns) -> list[str]:
    return [label for label, rx in patterns if rx.search(text)]


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def staged_lines():
    """(file, line number, text) for every line the staged diff adds."""
    path, number = None, 0
    for line in _git("diff", "--cached", "-U0", "--no-color", "--no-ext-diff").splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            number = int(m.group(1)) if m else 0
        elif line.startswith("+") and path:
            yield path, number, line[1:]
            number += 1
    for name in _git("diff", "--cached", "--name-only", "--diff-filter=AR").splitlines():
        yield name, 0, name  # the file name itself


def tracked_lines():
    for name in _git("ls-files").splitlines():
        yield name, 0, name
        try:
            text = Path(name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            yield name, number, line


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Refuse commits that contain personal details.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="check the staged changes (default)")
    mode.add_argument("--all", action="store_true", help="check every tracked file")
    args = parser.parse_args(argv)
    path = list_path()
    if not path.is_file():
        print(f"check_private: no list at {path}; nothing checked", file=sys.stderr)
        return 0
    patterns = load_patterns(path)
    hits = []
    for name, number, text in (tracked_lines() if args.all else staged_lines()):
        for label in find(text, patterns):
            hits.append(f"{name}:{number}: {label}" if number else f"{name} (file name): {label}")
    if not args.all:
        author = _git("var", "GIT_AUTHOR_IDENT").rsplit(" ", 2)[0]
        hits.extend(f"commit author '{author}': {label}" for label in find(author, patterns))
    if hits:
        print(f"check_private: {len(hits)} personal detail{'s' if len(hits) != 1 else ''} found (list: {path}):",
              file=sys.stderr)
        for hit in hits[:200]:
            print("  " + hit, file=sys.stderr)
        if len(hits) > 200:
            print(f"  ... and {len(hits) - 200} more", file=sys.stderr)
        if not args.all:
            print("Remove them, or move them into your own config or agent data. "
                  "(git commit --no-verify skips this check.)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
