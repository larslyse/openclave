#!/usr/bin/env bash
# Run ./install.sh on a brand-new Ubuntu 24.04 machine (a Docker container with
# a fresh non-root user) as a new user would, typing ANSWERS into its questions
# through a real terminal. Then open http://localhost:${PORT}/ to look around.
#   bash dev/install-script-test.sh
#   ANSWERS=my-answers.txt bash dev/install-script-test.sh   (format: dev/install_driver.py)
#   ANSWERS=dev/install-answers-ask-all.txt INSTALL_ARGS=--ask-all bash dev/install-script-test.sh
#   docker logs -f openclave-install;  docker rm -f openclave-install  to stop
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
port=8777  # host network: the container uses this computer's port 8777
answers="${ANSWERS:-"$(dirname "${BASH_SOURCE[0]}")/install-answers.txt"}"
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
(cd "$repo" && git ls-files -co --exclude-standard | grep -v __pycache__ | tar czf "$work/tree.tgz" -T -)
cp "$answers" "$work/answers"; cp "$repo/dev/install_driver.py" "$work/"
cat > "$work/Dockerfile" <<'EOD'
FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates sudo python3 && rm -rf /var/lib/apt/lists/*
RUN useradd -m -s /bin/bash -c "Alex Example" friend && echo 'friend ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/friend
USER friend
WORKDIR /home/friend
EOD
cat > "$work/run.sh" <<'EOD'
#!/bin/bash
mkdir openclave && tar xzf /src/tree.tgz -C openclave
cd openclave && exec python3 /src/install_driver.py /src/answers -- ./install.sh $INSTALL_ARGS
EOD
chmod +x "$work/run.sh"
docker build -q -t openclave-install "$work" >/dev/null
docker rm -f openclave-install >/dev/null 2>&1 || true
mkdir -p "$work/src" && cp "$work/tree.tgz" "$work/run.sh" "$work/answers" "$work/install_driver.py" "$work/src/"
docker create --name openclave-install --network host -e OPENCLAVE_SKIP_LOGIN=1 -e INSTALL_ARGS="${INSTALL_ARGS:-}" openclave-install /src/run.sh >/dev/null
docker cp "$work/src" openclave-install:/src
docker start openclave-install >/dev/null
echo "Installing… (docker logs -f openclave-install)"
for _ in $(seq 200); do curl -s -o /dev/null "http://localhost:$port/" && break; sleep 3; done
docker logs openclave-install 2>&1
echo "Open http://localhost:$port/  ·  stop: docker rm -f openclave-install"
