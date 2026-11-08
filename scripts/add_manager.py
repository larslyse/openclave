"""Create the manager agent from the ready-made manager template, under the
name the user chose (install.sh asks for it; there is no default name).

    python3 scripts/add_manager.py --status       existing manager's name, if any
    python3 scripts/add_manager.py --check "Name" can this name be used?
    python3 scripts/add_manager.py "Name"         create it

Exit codes: 0 done (prints the name), 2 the name can't be used (prints why),
3 a manager already exists (prints its name), 4 --status: no manager yet.
The model is the default model when its route is set up, else the first
model that is; the user can change it on the agent's page.
"""
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
    current = existing_manager()
    if argv[:1] == ["--status"]:
        if current:
            print(current)
            return 0
        return 4
    if current:
        print(current)
        return 3
    check = argv[:1] == ["--check"]
    name = " ".join(argv[1:] if check else argv).strip()
    try:
        agent_factory._check_new_name(name)
    except ValueError as e:
        print(e)
        return 2
    if check:
        return 0
    catalog.install(TEMPLATE, pick_model(), name)
    print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
