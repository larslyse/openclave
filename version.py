"""The release number from the VERSION file at the repo root.

Shown by /api/version, `python3 run.py --check` and the startup line, so a
bug report says which release it is about. Releases are git tags `vX.Y.Z`
matching this file (see CONTRIBUTING.md, "Releases").
"""
from pathlib import Path

VERSION_FILE = Path(__file__).parent / "VERSION"


def read_version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


VERSION = read_version()
