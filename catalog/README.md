# Ready-made agents

Templates people install from the New agent screen (phone) or the Control
Panel instead of starting from a blank agent. Each one has a job and a
personality that worked well for someone. After install, it runs a short
onboarding interview that fits it to the new user: its own questions, a
check that the personality fits, tools, voice and phone page.

## The agents

Thirteen to start from. Pick the few that match your life, not all of them.

| Agent | Area | What it does | Needs |
|---|---|---|---|
| **Manager** | OpenClave | You choose its name and personality (suggestions offered, no defaults). Every install needs one: keeps OpenClave tidy, runs the weekly audit, proposes what's worth moving from agent workspaces into your main vault, turns the other agents' bugs and ideas into a buildable backlog. Optional extra jobs: business ideas, agent scout. | |
| **Reba** | Health | A conscientious, Feynman-spirited trainer: rehabs injuries, plans training to prevent new ones, logs progress. | Optional Garmin connection ([garmin-mcp](https://github.com/Sinfjell/garmin-mcp), MIT, pinned) |
| **Fresh** | Health | A blunt long-horizon steward of how you look and feel: longevity, sleep, diet, hair, skin, grooming. | |
| **Bolt** | Home | A household droid: Home Assistant, home security, networking and IT support. Acts directly instead of just advising. | Home Assistant token |
| **Stash** | Home | The gruff warehouse: what you own, so nothing is bought twice; procurement, rental and resale. | |
| **Bob** | Home | A calm master carpenter for repairing an older house, step by step. | |
| **Greg** | Home | A gentle plant nerd: plant register, diagnosis from photos, short care advice. | |
| **Rocky** | Making | An eager bench engineer who talks in clipped fragments: motor control, power electronics, PCBs, embedded, vehicle builds. | |
| **Marco** | Vehicles | A cocky paddock mate: track and street riding coach, exact sourced specs for repairs. | |
| **Ray** | Outdoors | A calm outdoorsman: bushcraft, hunting and fishing, taught by explaining the why. | |
| **Penny** | Money | A future-focused analyst: transparent financial models, business-idea economics. No buy/sell advice. | |
| **Charles** | Writing | A plainspoken writing partner: drafts, polishes your own prose, keeps your notes vault tidy. Can write straight into vault folders you give him. | |
| **Spice** | Personal | The quiet listener: an honest record of your taste and humour that advises the other agents. | |

## How many, and how they fit together

Each agent is a domain, so "who do I ask?" and "where did that end up?" have the same answer. That works best with **about 5–10 agents**, and no more than about 12: few enough to name from memory, each one a domain you'd think of without trying ("the body", "the house", "the bikes").

Inside an agent, chats can go in plain **folders** (drag a chat onto one): one per running thread, such as an injury, a project or a bike. Agent → folder → chat, and no deeper.

So templates combine well. Start with the manager plus the two to four areas you care about most, and fold related ones together instead of adding agents:

- Reba + Fresh → one health agent; longevity and appearance get a folder
- Rocky + Marco → one maker; the motorcycles get a folder
- Ray + Greg → one outdoors agent; garden and plants get a folder
- Penny + Charles → one desk agent for money and writing
- Manager + Spice → the manager gets a taste folder

Add an agent when a domain has its own vocabulary and no agent obviously owns it. Merge two when you keep hesitating which one to ask. Split one when it has ten or more busy folders that don't share context. To fold an agent you already run into another, run:

```sh
python3 scripts/merge_agent.py <source-id> <target-id> --folder "Folder name" --dry-run
```

Drop `--dry-run` to do it, then restart the service. Chats move into the target, in that folder; memory, tracking, foundations and files move too, and a note named after the source keeps what it was for. The source folder goes to `<agents folder>/_Retired/`, so nothing is deleted.

## A template

One folder per agent in `catalog/agents/<id>/`:

| File | What |
|---|---|
| `agent.json` | `id`, `name`, `color`, `tagline`, plus any `roles`, `connections`, `mcp_servers`, `cli_tools`; and a `catalog` block (below) |
| `mission.md` | Goals and a `## Domains` list, about "the user". Max 1,850 characters |
| `persona.md` | Who the agent is, tone, how it works, ending with `## Feedback`. Max 2,200 characters |
| `onboarding.md` | A bullet list of questions that fit the template to a new user |
| `avatar.svg` | Optional, 64×64 viewBox, plain shapes (sanitized when served) |
| `skills/` | Optional skills it comes with (`<name>/SKILL.md`) |
| `personas/`, `extras/` | Optional suggestions to pick from at install (the manager has them): starting personalities, and extra jobs added to the mission when picked. One `.md` each, front matter `Label`, `Summary`, `Order` |

The **avatar library** (`catalog/avatars/*.svg`) holds pictures any agent can
pick in Control Panel → Edit → Pick from library. Same rules as `avatar.svg`;
put the label in a `<title>`.

```json
"catalog": {
  "version": 1,
  "category": "Home",
  "summary": "One or two sentences shown in the list.",
  "suggested_tools": ["write_note", "web_search"],
  "needs": "Anything to set up first, e.g. a Home Assistant token.",
  "authors": ["your-github-name"],
  "job": ["Optional: the job in plain bullets, shown before install"],
  "name_ideas": ["Optional: names to suggest, when the user names it"]
}
```

`suggested_tools: null` means every optional tool. Use `${HA_BASE_URL}`-style
placeholders for anything install-specific in `mcp_servers`; `${OPENCLAVE_DIR}`
and `${OPENCLAVE_PYTHON}` point at the install itself.

## Rules for a good template

- **Nothing personal.** Write about "the user". Facts about one person (age,
  injuries, the bikes in their garage, where they live) become onboarding
  questions instead.
- **Keep the character.** The persona is the point: keep its voice, quirks
  and the feedback that shaped it, written as rules.
- **No real people or famous characters as the agent.** "Calm, old-school
  bushcraft teacher" rather than a named TV presenter. Naming a style or a
  historical writer as an influence is fine.
- **Lanes, not names.** Other agents may not exist on someone else's
  install: "leave inventory to an inventory agent if there is one".

## Contributing one

1. Export a draft from an agent you use:
   `python3 scripts/export_agent.py <agents folder>/<Agent> catalog/agents/<id>`
   It copies the mission, persona, avatar and skills, and lists lines that
   look personal.
2. Rewrite those parts by hand (or ask your maintainer agent to), and write
   `onboarding.md`.
3. Install it on a scratch instance (`bash dev/run-scratch.sh`) and run the
   onboarding once.
4. Open a pull request. The pre-commit check refuses personal details from
   your private list.

Other catalogs work the same way: clone one and add its folder to
`[catalog] sources` in `openclave.toml`.
