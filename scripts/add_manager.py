"""Create the manager agent from the ready-made manager template, with the
name and personality the user chose and any changes to its job (install.sh
asks for all three; there are no defaults).

    python3 scripts/add_manager.py --status        existing manager's name, if any
    python3 scripts/add_manager.py --check NAME    can this name be used?
    python3 scripts/add_manager.py --personas      starting personalities: id<TAB>label<TAB>summary
    python3 scripts/add_manager.py --job           the job, one line each
    python3 scripts/add_manager.py NAME --persona ID_OR_OWN_WORDS [--mission-extra TEXT]

Exit codes: 0 done (prints the name), 2 can't be used (prints why),
3 a manager already exists (prints its name), 4 --status: no manager yet.
The model is the default model when its route is set up, else the first
model that is; the user can change it on the agent's page.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_factory  # noqa: E402
import agents  # noqa: E402
import catalog  # noqa: E402
import llm_models  # noqa: E402
import setup_check  # noqa: E402

TEMPLATE = "rick"  # the manager template's folder in catalog/agents


def existing_manager() -> str | None:
    ids = agents.with_role(agents.MANAGER)
    return agents.AGENTS[ids[0]].name if ids else None


def pick_model() -> str:
    ready = setup_check.ready_providers()
    default = llm_models.get(llm_models.DEFAULT_MODEL_ID)
    if default.provider in ready:
        return default.id
    for m in llm_models.picker_models():
        if m.provider in ready:
            return m.id
    return default.id


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Create the manager agent.")
    ap.add_argument("name", nargs="*")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--personas", action="store_true")
    ap.add_argument("--job", action="store_true")
    ap.add_argument("--persona", default="")
    ap.add_argument("--mission-extra", default="")
    a = ap.parse_args(argv)
    t = catalog.get(TEMPLATE)
    if a.personas:
        for p in t["personas"]:
            print(f"{p['id']}\t{p['label']}\t{p['summary']}")
        return 0
    if a.job:
        print("\n".join(t["job"]))
        return 0
    current = existing_manager()
    if a.status:
        if current:
            print(current)
            return 0
        return 4
    if current:
        print(current)
        return 3
    name = " ".join(a.name).strip()
    try:
        agent_factory._check_new_name(name)
        if a.check:
            return 0
        catalog.install(TEMPLATE, pick_model(), name, persona=a.persona, mission_extra=a.mission_extra)
    except ValueError as e:
        print(e)
        return 2
    print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
