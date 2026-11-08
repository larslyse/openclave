# Development instance isolation

The production installation is separate. Never use its port, vault, agent folders, or runtime state from this repository.

R1.1 and R1.4 provide isolated settings and runtime storage. Start a disposable local instance with:

```sh
bash dev/run-scratch.sh
```

The script creates a new temporary `OPENCLAVE_HOME`, copies only the synthetic agents from `tests/fixtures/Agents`, writes a local config and shared concept file, and starts on `127.0.0.1:8777`. It sets `OPENCLAVE_DISABLE_BACKGROUND_JOBS=1`, so reminders, cleanup, digest, and model updater stay off. The printed path contains the scratch data; stop with Ctrl+C. This instance does not use personal agent data or credentials.

For other manual launches, follow the same isolation rules: configure a fresh scratch `OPENCLAVE_HOME`, verify its vault and agent paths, keep background jobs off, and use a port other than the production port. Do not install a second systemd unit before cutover.

## Working on OpenClave while you also use it

Keep the checkout your service runs from clean: it only ever runs `main`, and updates with `git pull` plus a restart. Develop in a second clone (e.g. `git clone <repo> ~/openclave-dev`), test there with `bash dev/run-scratch.sh`, then push and pull into the live checkout. A half-finished change then never reaches your running agents.

## Keeping personal details out

Your settings, agents and state live in `OPENCLAVE_HOME` and your agents folder, never in the repo. As a safety net, enable the pre-commit check in each clone:

```sh
git config core.hooksPath .githooks
```

It refuses a commit whose added lines, file names or author match a pattern in your private list (`~/.openclave/private-strings.txt`, or `$OPENCLAVE_PRIVATE_STRINGS`). The list stays outside the repo; see `scripts/check_private.py` for the format. `python3 scripts/check_private.py --all` sweeps every tracked file.

## Trying the install as a new user

`bash dev/fresh-install-test.sh` follows the README install in a brand-new
Ubuntu 24.04 container (Docker) with a fresh user and no keys, using your
working tree, then serves it on http://localhost:8777/ (`PORT=` to change).
Use it after changing the README, requirements or first-run screens.
