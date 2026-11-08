#!/usr/bin/env bash
# Try the README install on a brand-new Ubuntu 24.04 machine (a Docker
# container with a fresh non-root user), using this working tree instead of
# `git clone`. Then open http://localhost:${PORT}/ to click through as a new
# user. No keys are passed in: the first-run screens are what you see.
#   bash dev/fresh-install-test.sh          build, install, start (port 8777)
#   docker logs -f openclave-fresh          follow it;  docker rm -f openclave-fresh  to stop
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
port="${PORT:-8777}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
(cd "$repo" && git ls-files -co --exclude-standard | grep -v __pycache__ | tar czf "$work/tree.tgz" -T -)
cat > "$work/Dockerfile" <<'EOF'
FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl sudo && rm -rf /var/lib/apt/lists/*
RUN useradd -m -s /bin/bash friend && echo 'friend ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/friend
USER friend
WORKDIR /home/friend
EOF
cat > "$work/steps.sh" <<'EOF'
#!/bin/bash
# README "Install", step by step (clone replaced by the copied tree).
set -ex
sudo apt update -qq && sudo apt install -y -qq git python3-venv >/dev/null
mkdir openclave && tar xzf /src/tree.tgz -C openclave && cd openclave
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
mkdir -p ~/.openclave
printf '[owner]\nname = "Alex"\n\n[time]\ntimezone = "Europe/Oslo"\n' > ~/.openclave/openclave.toml
.venv/bin/python run.py --check || true
# Only so the host's browser can reach the container:
printf '\n[network]\nbind = "0.0.0.0"\n' >> ~/.openclave/openclave.toml
exec .venv/bin/python run.py
EOF
chmod +x "$work/steps.sh"
docker build -q -t openclave-fresh "$work" >/dev/null
docker rm -f openclave-fresh >/dev/null 2>&1 || true
mkdir -p "$work/src" && cp "$work/tree.tgz" "$work/steps.sh" "$work/src/"
# The container needs /src only while installing; copy it in instead of mounting.
docker create --name openclave-fresh -p "127.0.0.1:$port:8777" openclave-fresh /src/steps.sh >/dev/null
docker cp "$work/src" openclave-fresh:/src
docker start openclave-fresh >/dev/null
echo "Installing… (docker logs -f openclave-fresh)"
for _ in $(seq 120); do curl -s -o /dev/null "http://localhost:$port/" && break; sleep 3; done
docker logs openclave-fresh 2>&1 | sed -n '/^OpenClave data folder/,/^OpenClave: /p'
echo "Open http://localhost:$port/  ·  stop: docker rm -f openclave-fresh"
