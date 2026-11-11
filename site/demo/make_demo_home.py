#!/usr/bin/env python3
"""Build a throwaway OPENCLAVE_HOME with demo agents and made-up chats, for
the getopenclave.com screenshots. No model is called; nothing personal.

    python3 site/demo/make_demo_home.py /tmp/oc-demo
    OPENCLAVE_HOME=/tmp/oc-demo OPENCLAVE_DISABLE_BACKGROUND_JOBS=1 \
        python3 -m uvicorn main:app --port 8795
"""
import json, os, shutil, sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOME = Path(sys.argv[1]).resolve()

# (catalog template, display name, folder)
AGENTS = [("rick", "Juno"), ("reba", "Reba"), ("bolt", "Bolt"),
          ("bob", "Bob"), ("penny", "Penny"), ("charles", "Charles")]

CHATS = {
    "Reba": [
        ("Half marathon in October?", [
            ("user", "Is a half marathon in October realistic?"),
            ("Reba", "Yes, with 14 weeks. Three runs a week, long run up by 1 km a week. Plan is in your files."),
        ], "Running"),
        ("Shoes for wet trails", [
            ("user", "Which shoes for wet forest trails?"),
            ("Reba", "A 4 to 5 mm lug sole. Your road pair is fine on gravel, not on roots."),
        ], "Running"),
        ("Waking at 4 am", [
            ("user", "I keep waking at 4 am after hard sessions."),
            ("Reba", "Common after late intervals. Move them before 18:00 for two weeks and log your sleep."),
        ], "Sleep"),
        ("Step-down form check", [
            ("user", "Is my knee supposed to track over the toes?"),
            ("Reba", "Yes, over the second toe. Slow on the way down, three seconds."),
        ], "Knee rehab"),
        ("Knee after Sunday's run", [
            ("user", "Knee ached on the last 3 km of Sunday's 12 km run. Fine walking, a bit stiff on stairs."),
            ("Reba", "Outside or under the kneecap? Stairs-down pain under the kneecap usually means patellar load, not a structural injury.\n\nThis week:\n- **Runs:** keep them under 6 km, flat, easy pace\n- **Daily:** 3×15 slow step-downs, 2×30 s wall sit\n- **Stop rule:** pain above 3/10 during, or worse the next morning\n\nI've moved Saturday's long run to next week in your plan."),
            ("user", "Under the kneecap. Did the step-downs, no pain."),
            ("Reba", "Good sign. Logged. If Thursday's 6 km is also pain-free we go back to 10 km on Sunday."),
        ], "Knee rehab"),
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


# (id, name, lead, members) for the claves screenshot
CLAVES = [
    ("home", "Home", "bolt", ["bolt", "bob"]),
    ("health", "Health", "reba", ["reba"]),
    ("money-writing", "Money & writing", "penny", ["penny", "charles"]),
]

# Archived chats per agent: (days ago, title, status, about, open ends)
ARCHIVED = {
    "Reba": [
        (3, "Shin splints in week 2", "concluded",
         "Shin pain after adding a third run. Dropped to two runs plus cycling for ten days; pain gone.", ""),
        (9, "Half marathon pacing plan", "open",
         "Built a pacing plan for the October half: 5:20/km for the first 15 km, then by feel.",
         "Test the gel timing on the next long run."),
        (16, "Gym or home workouts?", "concluded",
         "Compared gym and home options. Chose two home sessions a week with a kettlebell and bands.", ""),
        (24, "Calf strap for night cramps", "dropped",
         "Looked at night cramps and a calf strap. Cramps stopped after more water and magnesium.", ""),
        (31, "New running shoes", "concluded",
         "Picked a cushioned daily trainer one size up; old pair kept for short runs.", ""),
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
        # The agent's own page, as agent_factory.render_dashboard makes it.
        page = (REPO / "templates" / "agent_dashboard.html").read_text()
        for key, value in {"NAME": name, "ID": name.lower(), "COLOR": meta.get("color", "#2E9E5B"),
                           "INITIALS": name[:2].upper()}.items():
            page = page.replace("{{" + key + "}}", value)
        (dst / "dashboard.html").write_text(page)

    t = datetime.now() - timedelta(hours=3)
    for name, chats in CHATS.items():
        conv = agents_dir / name / "conversations"
        conv.mkdir(exist_ok=True)
        topics = []
        for i, (title, turns, *topic) in enumerate(chats):
            body = ""
            for role, text in turns:
                t += timedelta(minutes=2)
                label = "User" if role == "user" else name
                body += f"\n### {label} ({t:%H:%M})\n{text}\n"
            cid = f"{t:%Y%m%d-%H%M%S}-demo{i:04d}"
            folder = f"Folder: {topic[0]}\n" if topic else ""
            if topic and topic[0] not in topics:
                topics.append(topic[0])
            fm = f"---\nAgent: {name}\nDate: '{t:%Y-%m-%d}'\nTitle: {title}\n{folder}---\n\n"
            (conv / f"{cid}.md").write_text(fm + body.strip() + "\n")
        if topics:
            (agents_dir / name / "folders.json").write_text(json.dumps({"folders": topics}, indent=2))

    # Reba's plan, backlog and kanban, for the agent-page screenshot.
    today = f"{datetime.now():%Y-%m-%d}"
    reba = agents_dir / "Reba"
    (reba / "plan.md").write_text(
        f"---\nStatus: on-track\nUpdated: '{today}'\nTarget-event: Half marathon, 18 October\nPhases:\n"
        "- {name: Base, status: done, percent: 100}\n"
        "- {name: Build, status: in-progress, percent: 40}\n"
        "- {name: Taper, status: not-started, percent: 0}\n"
        "---\n\n# Plan\n\nThree runs a week. Long run back to 10 km once the knee is pain-free twice in a row.\n")
    items = ["Thursday 6 km, easy and flat", "Step-downs daily, 3x15", "Test gel timing on the next long run",
             "Book a physio check if pain passes 3/10"]
    (reba / "backlog.md").write_text(
        f"---\nUpdated: '{today}'\n---\n\n# Backlog\n\n" + "".join(f"- [ ] {x}\n" for x in items)
        + "- [x] Move Saturday's long run\n")
    (reba / "kanban.json").write_text(json.dumps(
        {items[0]: "in-progress", items[1]: "in-progress", items[2]: "backlog", "Move Saturday's long run": "done"}, indent=2))

    # Three teams (claves) for the claves screenshot. The manager (Juno) is in
    # every clave by itself, so it isn't listed as a member.
    for cid, name, lead, members in CLAVES:
        d = HOME / "Claves" / name
        d.mkdir(parents=True)
        (d / "clave.md").write_text(
            f"---\nId: {cid}\nName: {name}\nLead: {lead}\nMembers: {json.dumps(members)}\n"
            f"Created: '{datetime.now():%Y-%m-%d}'\n---\n\n# {name}\n")
    (HOME / "state" / "service_settings.json").write_text(json.dumps(
        {"multiple_claves": True, "active_clave": ""}))

    # Archived chats with recaps, for the Recaps screenshot.
    for name, items in ARCHIVED.items():
        arch = agents_dir / name / "conversations" / "archived"
        arch.mkdir(parents=True, exist_ok=True)
        for i, (days, title, status, about, open_ends) in enumerate(items):
            t = datetime.now() - timedelta(days=days)
            cid = f"{t:%Y%m%d-%H%M%S}-arch{i:04d}"
            recap = (f"Recap:\n  about: {json.dumps(about)}\n  status: {status}\n"
                     f"  open_ends: {json.dumps(open_ends)}\n  made: '{datetime.now():%Y-%m-%d}'\n")
            f = arch / f"{cid}.md"
            f.write_text(f"---\nAgent: {name}\nDate: '{t:%Y-%m-%d}'\nTitle: {title}\n{recap}---\n\n"
                         f"### User ({t:%H:%M})\n{title}\n")
            ts = t.timestamp()
            os.utime(f, (ts, ts))

    (HOME / "openclave.toml").write_text(
        '[paths]\nvault_root = "."\nagents_root = "agents"\nshared_concept_file = "openclave.md"\n'
        'tools_doc = "docs/tools.md"\n\n[privacy]\nconfidential_folders = []\n\n'
        '[network]\nbind = "127.0.0.1"\nport = 8795\n\n[time]\ntimezone = "UTC"\n\n[owner]\nname = "Sam"\n')
    (HOME / "openclave.md").write_text("# OpenClave\n\nDemo instance for screenshots.\n")
    print(HOME)


if __name__ == "__main__":
    main()
