# OpenClave development guidance

This repository is the new OpenClave codebase. `BACKLOG.md` tracks the work needed before a public release. The existing `openclave-service` installation is a separate production service and must remain untouched until the cutover described in the backlog.

## Work safely

- Develop and test in this repository only. Do not restart, reconfigure, or deploy to the existing `openclave.service` systemd unit or use its port 8767.
- Do not point this code or its tests at the live vault, live agent folders, or the old service's runtime state. Use synthetic fixtures and temporary directories. Tests must not make real provider or API calls.
- Do not start a development service until `OPENCLAVE_HOME` is implemented and configured to a scratch data directory. Follow `dev/README.md` when that is ready.
- Keep runtime state, secrets, and personal agent data out of Git. Check `git status` before committing.
- The pre-commit hook (`.githooks/pre-commit`, enabled with `git config core.hooksPath .githooks`) refuses personal details listed in the owner's private list. Never bypass it with `--no-verify`; move the detail into config or agent data instead.

## Code conventions

- Run `python3 -m unittest discover -s tests` before committing. Tests should pass with `HOME` set to an empty temporary directory.
- `static/chat_core.js` is the shared chat implementation for desktop and mobile. Keep both surfaces in sync when changing chat behavior.
- Add new tools consistently to the tool registry, `agent_tools.py`, and `mcp_vault_server.py`.
- Move personal configuration and agent-specific behavior out of core code as described in `BACKLOG.md`; do not add new hardcoded agents or local paths.
