#!/usr/bin/env python3
"""Build a throwaway OPENCLAVE_HOME with demo agents and made-up chats, for
the getopenclave.com screenshots. No model is called; nothing personal.

    python3 site/demo/make_demo_home.py /tmp/oc-demo
    OPENCLAVE_HOME=/tmp/oc-demo OPENCLAVE_DISABLE_BACKGROUND_JOBS=1 \
        python3 -m uvicorn main:app --port 8795
"""
import json, shutil, sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOME = Path(sys.argv[1]).resolve()

# (catalog template, display name, folder)
AGENTS = [("rick", "Juno"), ("reba", "Reba"), ("bolt", "Bolt"),
          ("bob", "Bob"), ("penny", "Penny"), ("charles", "Charles")]

CHATS = {
    "Reba": [
        ("Knee after Sunday's run", [
            ("user", "Knee ached on the last 3 km of Sunday's 12 km run. Fine walking, a bit stiff on stairs."),
            ("Reba", "Outside or under the kneecap? Stairs-down pain under the kneecap usually means patellar load, not a structural injury.\n\nThis week:\n- **Runs:** keep them under 6 km, flat, easy pace\n- **Daily:** 3×15 slow step-downs, 2×30 s wall sit\n- **Stop rule:** pain above 3/10 during, or worse the next morning\n\nI've moved Saturday's long run to next week in your plan."),
            ("user", "Under the kneecap. Did the step-downs, no pain."),
            ("Reba", "Good sign. Logged. If Thursday's 6 km is also pain-free we go back to 10 km on Sunday."),
        ]),
    ],
    "Bolt": [
        ("Garage door left open", [
            ("user", "Can you warn me if the garage door is open after 22:00?"),
            ("Bolt", "Done. New automation 'Garage open late': at 22:00, and on any opening after that, if `cover.garage_door` is open for 5 min you get a phone notification with a Close button.\n\nTested it with the door sensor: notification arrived in 2 s."),
            ("user", "Perfect. And the hallway sensor battery?"),
            ("Bolt", "Hallway motion sensor is at 14 %. CR2032. Put it on your shopping list."),
        ]),
    ],
    "Bob": [
        ("Soft spot in the bathroom floor", [
            ("user", "Floor feels soft by the shower. House from 1962."),
            ("Bob", "Let's find out before guessing. Two checks, ten minutes:\n\n1. Press a screwdriver into the floor edge at the shower threshold. Does it sink in?\n2. From the room below or the crawl space: look at the joist under the shower for dark stains.\n\nSend me a photo of each. If the wood is soft, we stop the leak first, then replace the subfloor. That's a weekend job you can do."),
        ]),
    ],
    "Penny": [
        ("Solar panels: worth it?", [
            ("user", "Quote for 8 kW solar: 110 000. Worth it?"),
            ("Penny", "Depends on two numbers I don't have yet: your yearly use and what you pay per kWh.\n\nWith typical values (12 000 kWh/year, 1.2 per kWh, 6 500 kWh produced): payback about 14 years, panels last ~25. Model with all assumptions is in your files as solar-8kw.md, change any number and ask me again."),
        ]),
    ],
    "Charles": [
        ("Post about the shed build", [
            ("user", "Turn my notes on the shed build into a short blog post. Keep my words."),
            ("Charles", "Draft saved as Shed build.md. I only reordered your notes, joined sentences and cut repeats. Three places marked [?] where your notes disagree with each other (roof angle, cost, number of weekends)."),
        ]),
    ],
    "Juno": [
        ("Weekly review", [
            ("Juno", "Weekly review done.\n\n- Reba and Fresh overlap on sleep: suggest one owner (Reba)\n- 3 chats older than 60 days archived, recaps kept\n- Bolt asked for a calendar connection: added to the backlog\n\nAnything you want changed?"),
        ]),
    ],
}


def main():
    if HOME.exists():
        shutil.rmtree(HOME)
    agents_dir = HOME / "agents"
    for sub in ("state", "docs"):
        (HOME / sub).mkdir(parents=True)
    for template, name in AGENTS:
        dst = agents_dir / name
        shutil.copytree(REPO / "catalog" / "agents" / template, dst)
        meta = json.loads((dst / "agent.json").read_text())
        meta["name"] = name
        meta["id"] = name.lower()
        (dst / "agent.json").write_text(json.dumps(meta, indent=2))

    t = datetime.now() - timedelta(hours=3)
    for name, chats in CHATS.items():
        conv = agents_dir / name / "conversations"
        conv.mkdir(exist_ok=True)
        for i, (title, turns) in enumerate(chats):
            body = ""
            for role, text in turns:
                t += timedelta(minutes=2)
                label = "User" if role == "user" else name
                body += f"\n### {label} ({t:%H:%M})\n{text}\n"
            cid = f"{t:%Y%m%d-%H%M%S}-demo{i:04d}"
            fm = f"---\nAgent: {name}\nDate: '{t:%Y-%m-%d}'\nTitle: {title}\n---\n\n"
            (conv / f"{cid}.md").write_text(fm + body.strip() + "\n")

    (HOME / "openclave.toml").write_text(
        '[paths]\nvault_root = "."\nagents_root = "agents"\nshared_concept_file = "openclave.md"\n'
        'tools_doc = "docs/tools.md"\n\n[privacy]\nconfidential_folders = []\n\n'
        '[network]\nbind = "127.0.0.1"\nport = 8795\n\n[time]\ntimezone = "UTC"\n\n[owner]\nname = "Sam"\n')
    (HOME / "openclave.md").write_text("# OpenClave\n\nDemo instance for screenshots.\n")
    print(HOME)


if __name__ == "__main__":
    main()
