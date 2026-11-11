# Changelog

What changed in each release of OpenClave. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases are
numbered `MAJOR.MINOR.PATCH` (see "Releases" in [CONTRIBUTING.md](CONTRIBUTING.md)).

## [Unreleased]

### Changed

- Installs and updates follow releases: the installer, `openclave update` and
  Control Panel → Update move to the newest release (a `vX.Y.Z` tag) instead
  of the newest change on `main`, and never go back to an older version. Set
  `[updates] channel = "main"` in `openclave.toml` to follow `main` instead.
- The installer asks less: your name, timezone, a model, note search and
  start at boot. You name your manager and pick your first agents on the
  welcome card in the browser. `./install.sh --ask-all` (or
  `OPENCLAVE_ASK_ALL=1`) asks everything in the terminal as before.

## [0.1.0]

The first public release.

### Added

- Agents: one per area of your life, each a folder of plain Markdown
  (mission, plan, backlog, notes, chats) on your own computer.
- A manager agent that looks after the others: a weekly review that archives
  finished chats, writes chat recaps and brings yes/no proposals.
- Ready-made agents in `catalog/`, each with an onboarding interview, and a
  form to create your own.
- The dashboard (every agent in its own box with a message box), the agent
  page (topics and chats, plan, backlog, kanban board), and the Control Panel
  (models per agent, tools, scheduled tasks, reminders, usage, Setup check).
- Topics: chats grouped in folders you name, by drag and drop.
- Claves: teams of agents with a lead, a group chat and a shared backlog.
- Agents that follow up: messages between agents, scheduled tasks and
  reminders, with push notifications on the phone.
- Model routes: Anthropic and xAI API keys, or the Claude Pro/Max and
  ChatGPT Plus/Pro subscriptions through the Claude and Codex CLIs; the model
  is picked per agent.
- A phone app at `/mobile/`, for use over Tailscale, with an optional access
  token.
- Optional extras: note search with qmd, local voice (speech to text and
  speech), Home Assistant tools whose changes wait for your OK.
- `install.sh`, a guided installer, plus install by hand, a systemd unit
  example and a Docker image for the API routes.
- A release number (`VERSION`), shown by `/api/version`, `run.py --check`
  and the startup line, and a data format version (`state/version.json`)
  with migrations that run once at startup.

[Unreleased]: https://github.com/larslyse/openclave/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/larslyse/openclave/releases/tag/v0.1.0
