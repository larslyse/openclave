#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
scratch_home="$(mktemp -d "${TMPDIR:-/tmp}/openclave-dev.XXXXXX")"
dev_port="${OPENCLAVE_DEV_PORT:-8777}"
if [[ ! "$dev_port" =~ ^[0-9]+$ ]] || (( dev_port < 1 || dev_port > 65535 )); then
  echo "OPENCLAVE_DEV_PORT must be an integer from 1 to 65535" >&2
  exit 2
fi
mkdir -p "$scratch_home/agents" "$scratch_home/state" "$scratch_home/docs"
cp -a "$repo_root/tests/fixtures/Agents/." "$scratch_home/agents/"

cat > "$scratch_home/openclave.toml" <<TOML
[paths]
vault_root = "."
agents_root = "agents"
shared_concept_file = "openclave.md"
tools_doc = "docs/tools.md"
garmin_token_dir = "state/garmin"

[privacy]
confidential_folders = []

[network]
bind = "127.0.0.1"
port = $dev_port

[time]
timezone = "UTC"

[owner]
name = ""
TOML
cat > "$scratch_home/openclave.md" <<'DOC'
# OpenClave

Scratch development instance using synthetic test agents.
DOC

cd "$repo_root"
export OPENCLAVE_HOME="$scratch_home"
export OPENCLAVE_DISABLE_BACKGROUND_JOBS=1
printf 'Scratch data: %s\n' "$OPENCLAVE_HOME"
printf 'Development UI: http://127.0.0.1:%s/\n' "$dev_port"
exec python3 -m uvicorn main:app --host 127.0.0.1 --port "$dev_port"
