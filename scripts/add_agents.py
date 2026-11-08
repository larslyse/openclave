"""Add ready-made agents from the catalog (install.sh's "agents to start
with" step). Nothing is added unless picked.

    python3 scripts/add_agents.py --list      id<TAB>name<TAB>tagline, not yet added, no manager
    python3 scripts/add_agents.py ID [ID ...]  add them (prints each name, or why it failed)

Each starts in onboarding with the same model the manager got
(add_manager.pick_model) and asks its questions in its first chat.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import add_manager  # noqa: E402
import catalog  # noqa: E402


def choices() -> list[dict]:
    return [t for t in catalog.listing() if not t["manager"] and not t["installed"]]


def main(argv: list[str]) -> int:
    if argv == ["--list"]:
        for t in choices():
            print(f"{t['id']}\t{t['name']}\t{t['tagline']}")
        return 0
    if not argv:
        print(__doc__)
        return 2
    rc, model = 0, add_manager.pick_model()
    for template_id in argv:
        try:
            created = catalog.install(template_id, model)
        except ValueError as e:
            print(f"Couldn't add {template_id}: {e}")
            rc = 2
            continue
        print(catalog.agents.AGENTS[created["id"]].name if created["id"] in catalog.agents.AGENTS else created["id"])
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
