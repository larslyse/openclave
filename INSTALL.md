# Installing OpenClave

The full install guide is in [README.md](README.md): what you need, the
ten-minute install, models, optional extras (subscriptions, note search,
phone, systemd, voice, Docker), updating and troubleshooting.

`.venv/bin/python run.py --check` (or Control Panel → Setup) shows what is set
up on a given machine.

## Moving to a new machine

Copy `~/.openclave` (settings, keys, agents, state) and, if you keep agents in
a separate notes folder, that folder too. Then follow the README install on the
new machine, skipping step 3 and step 4 (your settings and keys came along).
Web Push keys live in `~/.openclave/state/`, so phones that already get
notifications keep getting them.

## Rolling back a bad update

Everything is in git: `git log`, then `git checkout <good-commit>` (or
`git revert <commit>`), and restart OpenClave. Static files change
immediately, Python code after the restart. `git checkout main && git pull`
returns to the latest version.

## Developing

See [dev/README.md](dev/README.md) for a throwaway instance with test data and
[AGENTS.md](AGENTS.md) for the rules when changing the code. Tests:

```sh
python3 -m unittest discover -s tests       # also runs the Node chat checks
```
