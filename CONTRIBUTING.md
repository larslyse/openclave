# Contributing to OpenClave

Thanks for wanting to make OpenClave better. Bug reports, ideas, new agents
and code are all welcome. This page explains how to get a change in with as
little back-and-forth as possible.

## Start with an issue

- **Bug:** open a [bug report](../../issues/new?template=bug_report.yml).
  Include what you did, what you expected, what happened, and the output of
  `.venv/bin/python run.py --check`.
- **Small fix** (typo, unclear README step, obvious bug with a one-line
  fix): send a pull request straight away.
- **Anything bigger** (a feature, a new setting, a change to how agents
  work): open an [idea](../../issues/new?template=idea.yml) first and wait
  for a yes before writing code. A paragraph is quick to agree on; 500 lines
  built in a direction the project won't take are a lost weekend for you.

## Two kinds of contribution

**Ready-made agents** (`catalog/agents/<name>/`) are mostly text: a mission,
a persona, an onboarding interview, an avatar and an `agent.json`. A weak one
only affects people who choose to install it, so these are reviewed lightly.
Copy an existing agent folder as a starting point and read
[catalog/README.md](catalog/README.md) for what makes a good one: one clear
domain, a personality that fits it, onboarding questions a non-technical
person can answer.

**Core code** (everything else) runs for every user, so it gets a careful
review. Keep pull requests small and about one thing.

## Working on the code

```sh
git clone https://github.com/<you>/openclave.git && cd openclave
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
git config core.hooksPath .githooks
HOME=$(mktemp -d) .venv/bin/python -m unittest discover -s tests
```

- [dev/README.md](dev/README.md) runs a throwaway instance with test data, so
  you never touch your own agents while developing.
- [AGENTS.md](AGENTS.md) holds the code rules (they apply to people too).
  The important ones:
  - Tests must pass with an empty `HOME` and never call a real provider.
  - Desktop and phone chat share `static/chat_core.js`; change both together.
  - No personal details, hostnames, local paths or hardcoded agents in core
    code. They belong in config or agent data.
- Write for the user who isn't a developer: plain words in the UI and in
  messages, no jargon.
- If your change affects installation or setup, update README.md in the
  same pull request.

## Pull requests

- Fill in the template: what it does, why, and how you tested it.
- Tests run automatically and must pass.
- An AI review comments on the first version. Address what's right, say so
  when it's wrong. A human (the maintainer) makes the final call and merges.
- The first time you contribute, a bot asks you to sign the
  [Contributor License Agreement](CLA.md) by posting one comment. It lets
  the project keep its licensing options open (for example, to offer
  OpenClave under other terms later) while you keep the copyright to your
  work.

## Releases

- `main` is the development branch: it should always pass its tests, but it
  can change from one day to the next.
- A release is a git tag `vX.Y.Z` on `main`. The number follows
  `MAJOR.MINOR.PATCH`: a patch fixes bugs, a minor release adds features, a
  major release needs you to do something when you update.
- Every change that a user would notice gets a line under "Unreleased" in
  [CHANGELOG.md](CHANGELOG.md), in the same pull request.

To cut a release (maintainer):

1. Put the new number in `VERSION`.
2. In `CHANGELOG.md`, rename "Unreleased" to the new version and add an
   empty "Unreleased" above it; update the compare links at the bottom.
3. Commit both ("Release X.Y.Z"), then tag and push:
   `git tag -a vX.Y.Z -m "OpenClave X.Y.Z" && git push origin main vX.Y.Z`.

## Licence

OpenClave is licensed under the [AGPL-3.0](LICENSE). By contributing you
agree that your contribution is licensed under it and under the terms of the
[CLA](CLA.md).
