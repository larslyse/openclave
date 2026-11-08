#!/usr/bin/env bash
# OpenClave installer: asks a few questions, sets everything up, starts it.
#
#   ./install.sh                     from a cloned openclave folder
#   curl -fsSL <url>/install.sh | bash   downloads OpenClave to ~/openclave first
#
# Safe to run again: it keeps your settings and agents, updates the Python
# packages, and lets you add a model. Ubuntu/Debian (and WSL) for now; on
# anything else it points to the step-by-step guide in README.md.
# OPENCLAVE_SKIP_LOGIN=1 skips the Claude/Codex sign-in (automated tests).
set -euo pipefail

REPO_URL="https://github.com/larslyse/openclave.git"
DATA="${OPENCLAVE_HOME:-$HOME/.openclave}"
export OPENCLAVE_HOME="$DATA"
PORT=8777
USER="${USER:-$(id -un)}"

# ── Output and questions ─────────────────────────────────────────────────
if [ -t 1 ]; then B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; D=$'\e[2m'; N=$'\e[0m'; else B= G= Y= R= D= N=; fi
say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==> %s%s\n' "$B" "$*" "$N"; }
ok()   { printf '%s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '%s!%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '\n%s✗ %s%s\n' "$R" "$*" "$N" >&2; exit 1; }

# Questions read from the terminal, so they also work with `curl ... | bash`.
# With no terminal (automated runs) every question takes its default.
TTY=/dev/tty
if ! { : <"$TTY"; } 2>/dev/null; then TTY=""; fi
ask() {  # ask VAR "Question" "default"
  local __var=$1 __q=$2 __def=${3:-} __ans=""
  if [ -n "$TTY" ]; then
    if [ -n "$__def" ]; then printf '%s %s[%s]%s ' "$__q" "$D" "$__def" "$N"; else printf '%s ' "$__q"; fi
    IFS= read -r __ans <"$TTY" || true
  fi
  printf -v "$__var" '%s' "${__ans:-$__def}"
}
ask_secret() {  # ask_secret VAR "Question"
  local __var=$1 __ans=""
  if [ -n "$TTY" ]; then
    printf '%s ' "$2"; IFS= read -rs __ans <"$TTY" || true; printf '\n'
  fi
  printf -v "$__var" '%s' "$__ans"
}
yes_no() {  # yes_no "Question" Y|N  → exit status
  local a; ask a "$1 (y/n)" "$2"
  case "$a" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

# ── Checks ───────────────────────────────────────────────────────────────
say "${B}OpenClave installer${N}"
say "Sets up OpenClave on this computer in a few minutes. Press Enter to take"
say "the suggested answer ${D}[in brackets]${N}. Your settings go in $DATA."

[ "$(id -u)" -ne 0 ] || die "Run this as your normal user, not as root (it asks for your password when it needs it)."

OS="$(uname -s)"
if [ "$OS" = "Darwin" ]; then
  die "macOS isn't supported by this installer yet. Follow \"Detailed setup\" in README.md."
fi
command -v apt-get >/dev/null || die "This installer needs Ubuntu or Debian (or WSL with Ubuntu). On other systems, follow \"Detailed setup\" in README.md."
WSL=""; grep -qi microsoft /proc/version 2>/dev/null && WSL=1
SYSTEMD=""; [ -d /run/systemd/system ] && SYSTEMD=1

need_pkgs=()
command -v git >/dev/null || need_pkgs+=(git)
command -v curl >/dev/null || need_pkgs+=(curl)
command -v python3 >/dev/null || need_pkgs+=(python3)
if command -v python3 >/dev/null && ! python3 -c 'import ensurepip, venv' 2>/dev/null; then need_pkgs+=(python3-venv); fi
command -v python3 >/dev/null || need_pkgs+=(python3-venv)

as_root() {  # runs a command with sudo, saying why first
  if [ -z "${SUDO_OK:-}" ]; then
    say "Some steps install system software, so it may ask for your password (sudo)."
    SUDO_OK=1
  fi
  sudo "$@"
}
apt_install() {
  as_root apt-get update -qq
  as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" >/dev/null
}

if [ ${#need_pkgs[@]} -gt 0 ]; then
  step "Installing the basics: ${need_pkgs[*]}"
  apt_install "${need_pkgs[@]}"
  ok "Installed ${need_pkgs[*]}"
fi

python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' || die "OpenClave needs Python 3.12 or newer; this computer has $(python3 --version 2>&1). Ubuntu 24.04 or newer has it."

# ── Get OpenClave ────────────────────────────────────────────────────────
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/run.py" ] && [ -f "$SELF_DIR/requirements.txt" ]; then
  REPO="$SELF_DIR"
else
  REPO="$HOME/openclave"
  if [ -f "$REPO/run.py" ]; then
    step "Updating OpenClave in $REPO"
    git -C "$REPO" pull --ff-only -q || warn "Couldn't update $REPO (local changes?); using it as it is."
  else
    step "Downloading OpenClave to $REPO"
    git clone -q "$REPO_URL" "$REPO"
  fi
fi
cd "$REPO"

step "Installing OpenClave's Python packages (into $REPO/.venv, nothing system-wide)"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q --disable-pip-version-check -r requirements.txt
ok "Packages installed"

# ── About you ────────────────────────────────────────────────────────────
mkdir -p "$DATA"; chmod 700 "$DATA"
TOML="$DATA/openclave.toml"
ENV_FILE="$DATA/.env"
touch "$ENV_FILE"; chmod 600 "$ENV_FILE"

toml_str() { local s=${1//\\/\\\\}; s=${s//\"/\\\"}; printf '"%s"' "$s"; }

if [ -f "$TOML" ]; then
  step "Settings"
  ok "Keeping your settings in $TOML"
else
  step "About you"
  guess_name="$(getent passwd "$USER" 2>/dev/null | cut -d: -f5 | cut -d, -f1 | cut -d' ' -f1)"
  [ -n "$guess_name" ] || guess_name="${USER^}"
  ask NAME "What should your agents call you?" "$guess_name"

  guess_tz="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
  [ -n "$guess_tz" ] || guess_tz="$(cat /etc/timezone 2>/dev/null || true)"
  [ -n "$guess_tz" ] || guess_tz="$(readlink /etc/localtime 2>/dev/null | sed 's|.*/zoneinfo/||' || true)"
  guess_tz="${guess_tz#"${guess_tz%%[!/]*}"}"  # some systems write "/UTC"
  .venv/bin/python -c 'import sys, zoneinfo; zoneinfo.ZoneInfo(sys.argv[1])' "$guess_tz" 2>/dev/null || guess_tz="UTC"
  while :; do
    ask TZ_ANS "Your timezone (e.g. Europe/Oslo, America/New_York)?" "$guess_tz"
    if .venv/bin/python -c 'import sys, zoneinfo; zoneinfo.ZoneInfo(sys.argv[1])' "$TZ_ANS" 2>/dev/null; then break; fi
    warn "\"$TZ_ANS\" isn't a timezone name. Examples: Europe/London, Asia/Tokyo, America/Chicago."
    if [ -z "$TTY" ]; then TZ_ANS=UTC; break; fi
  done
  {
    printf '# OpenClave settings. All options: openclave.example.toml in %s\n\n' "$REPO"
    printf '[owner]\nname = %s\n\n[time]\ntimezone = %s\n' "$(toml_str "$NAME")" "$(toml_str "$TZ_ANS")"
  } >"$TOML"
  ok "Saved to $TOML"
fi

# ── Your manager ─────────────────────────────────────────────────────────
# Every install has one manager agent (the ready-made manager template). The
# user names it, picks or describes its personality and can change its job;
# none of it has a default. Created after the model step.
step "Your manager"
MANAGER="" PERSONA="" MISSION_EXTRA="" EXTRAS=()
if MGR_NAME="$(.venv/bin/python scripts/add_manager.py --status 2>/dev/null)"; then
  ok "Your manager is $MGR_NAME"
else
  MGR_NAME=""
  say "Every OpenClave has one manager agent. It keeps OpenClave tidy, runs a weekly"
  say "review, and turns the other agents' bugs and ideas into a to-do list."
  say "Give it any name you like; it will introduce itself by it. Some ideas:"
  names=(); while IFS= read -r n; do [ -n "$n" ] && names+=("$n"); done < <(.venv/bin/python scripts/add_manager.py --names)
  line=""; for i in "${!names[@]}"; do line+="${B}$((i + 1))${N} ${names[$i]}   "; done
  [ -n "$line" ] && say "  $line"
  while :; do
    ask MANAGER "Type a name, or a number from the ideas:" ""
    MANAGER="$(printf '%s' "$MANAGER" | sed 's/^ *//; s/ *$//')"
    if [[ "$MANAGER" =~ ^[0-9]+$ ]] && [ "$MANAGER" -ge 1 ] && [ "$MANAGER" -le "${#names[@]}" ]; then
      MANAGER="${names[$((MANAGER - 1))]}"
    fi
    if [ -z "$MANAGER" ]; then
      if [ -z "$TTY" ]; then MANAGER=Manager; break; fi
      warn "Your manager needs a name: letters, digits, spaces or dashes."
      continue
    fi
    if why="$(.venv/bin/python scripts/add_manager.py --check "$MANAGER" 2>&1)"; then break; fi
    warn "$why"
  done

  say ""
  say "How should $MANAGER come across? Pick one, or describe it in your own words:"
  ids=(); i=0
  while IFS=$'\t' read -r pid plabel psum; do
    i=$((i + 1)); ids+=("$pid"); say "  ${B}$i${N}  $plabel: $psum"
  done < <(.venv/bin/python scripts/add_manager.py --personas)
  own=$((i + 1)); say "  ${B}$own${N}  In my own words"
  PERSONA=""
  while :; do
    ask PCHOICE "Personality:" ""
    if [[ "$PCHOICE" =~ ^[0-9]+$ ]] && [ "$PCHOICE" -ge 1 ] && [ "$PCHOICE" -le "$i" ]; then
      PERSONA="${ids[$((PCHOICE - 1))]}"; break
    elif [ "$PCHOICE" = "$own" ] || [ ${#PCHOICE} -ge 10 ]; then
      while [ ${#PCHOICE} -lt 10 ]; do
        [ -n "$TTY" ] || { PCHOICE="${ids[0]}"; break; }
        ask PCHOICE "Describe $MANAGER's personality in a sentence or two:" ""
        [ ${#PCHOICE} -ge 10 ] || warn "A few more words, please, e.g. \"dry humour, patient, never uses jargon\"."
        if [ ${#PCHOICE} -gt 1000 ]; then warn "Shorter, please: up to 1000 characters."; PCHOICE=""; fi
      done
      PERSONA="$PCHOICE"; break
    elif [ -z "$TTY" ]; then
      PERSONA="${ids[0]}"; break
    else
      warn "Type a number from 1 to $own."
    fi
  done

  say ""
  say "$MANAGER's job:"
  while IFS= read -r line; do say "  - $line"; done < <(.venv/bin/python scripts/add_manager.py --job)

  EXTRAS=(); xids=(); i=0
  while IFS=$'\t' read -r xid xlabel xsum; do
    [ $i -eq 0 ] && { say ""; say "Extra jobs $MANAGER can take on (none unless you pick them):"; }
    i=$((i + 1)); xids+=("$xid"); say "  ${B}$i${N}  $xlabel: $xsum"
  done < <(.venv/bin/python scripts/add_manager.py --extras)
  while [ $i -gt 0 ]; do
    ask XCHOICE "Numbers of the ones you want, e.g. \"1 2\" (Enter for none):" ""
    EXTRAS=(); bad=""
    for n in ${XCHOICE//,/ }; do
      if [[ "$n" =~ ^[0-9]+$ ]] && [ "$n" -ge 1 ] && [ "$n" -le $i ]; then EXTRAS+=(--extra "${xids[$((n - 1))]}"); else bad=1; fi
    done
    [ -z "$bad" ] && break
    [ -n "$TTY" ] || { EXTRAS=(); break; }
    warn "Type numbers from 1 to $i, separated by spaces, or just press Enter."
  done
  say ""
  while :; do
    ask MISSION_EXTRA "Anything to change or add to $MANAGER's job, in your own words? (Enter: nothing)" ""
    [ ${#MISSION_EXTRA} -le 300 ] && break
    warn "Shorter, please: up to 300 characters. You can tell $MANAGER more in its first chat."
  done
fi

# ── A model ──────────────────────────────────────────────────────────────
have_key() { grep -q "^$1=." "$ENV_FILE" 2>/dev/null || [ -n "${!1:-}" ]; }
set_key() {  # set_key NAME value: replace or add the line in .env
  local tmp; tmp="$(mktemp)"
  grep -v "^$1=" "$ENV_FILE" >"$tmp" 2>/dev/null || true
  printf '%s=%s\n' "$1" "$2" >>"$tmp"
  cat "$tmp" >"$ENV_FILE"; rm -f "$tmp"; chmod 600 "$ENV_FILE"
}
check_key() {  # check_key anthropic|xai KEY → 0 accepted, 1 refused, 2 couldn't tell
  local code
  if [ "$1" = anthropic ]; then
    code="$(curl -s -o /dev/null -w '%{http_code}' -m 15 https://api.anthropic.com/v1/models -H "x-api-key: $2" -H "anthropic-version: 2023-06-01" || true)"
  else
    code="$(curl -s -o /dev/null -w '%{http_code}' -m 15 https://api.x.ai/v1/models -H "Authorization: Bearer $2" || true)"
  fi
  case "$code" in 200) return 0 ;; 401|403) return 1 ;; *) return 2 ;; esac
}
add_api_key() {  # add_api_key anthropic|xai
  local var url label key
  if [ "$1" = anthropic ]; then var=ANTHROPIC_API_KEY url="https://console.anthropic.com/settings/keys" label=Anthropic
  else var=XAI_API_KEY url="https://console.x.ai/" label=xAI; fi
  say "Create a key at ${B}$url${N} (sign up, add a little credit, then \"Create key\")."
  say "Tip: set a monthly spending limit there too."
  while :; do
    ask_secret key "Paste your $label key (it won't show), or press Enter to skip:"
    key="$(printf '%s' "$key" | tr -d '[:space:]')"
    [ -n "$key" ] || { warn "Skipped. You can run ./install.sh again later."; return 1; }
    local rc=0; check_key "$1" "$key" || rc=$?
    case $rc in
      0) ok "$label accepted the key"; set_key "$var" "$key"; return 0 ;;
      1) warn "$label refused that key. Check you copied all of it." ;;
      *) warn "Couldn't reach $label to check the key; saving it anyway."; set_key "$var" "$key"; return 0 ;;
    esac
  done
}
need_node22() {
  if command -v node >/dev/null && node -e 'process.exit(parseInt(process.versions.node) < 22 ? 1 : 0)' 2>/dev/null; then return 0; fi
  say "Installing Node.js 22 (from NodeSource; Ubuntu's own version is too old)..."
  curl -fsSL https://deb.nodesource.com/setup_22.x | as_root -E bash - >/dev/null 2>&1 || die "Couldn't add the Node.js package source (NodeSource)."
  apt_install nodejs
}
setup_claude_cli() {
  if ! command -v claude >/dev/null && [ ! -x "$HOME/.local/bin/claude" ]; then
    say "Installing Claude Code (the official Claude command-line tool)..."
    curl -fsSL https://claude.ai/install.sh | bash >/dev/null
  fi
  local claude; claude="$(command -v claude || echo "$HOME/.local/bin/claude")"
  [ -x "$claude" ] || { warn "Claude Code didn't install. See README, \"Use your Claude or ChatGPT subscription\"."; return 1; }
  ok "Claude Code installed"
  if [ -n "$TTY" ] && [ -z "${OPENCLAVE_SKIP_LOGIN:-}" ]; then
    say ""
    say "Now sign in with your Claude account. Claude opens in this terminal:"
    say "  1. pick ${B}Claude account with subscription${N} and follow the link,"
    say "  2. when it says you're signed in, type ${B}/exit${N} and press Enter."
    local go; ask go "Press Enter to start" ""
    "$claude" <"$TTY" >"$TTY" 2>&1 || true
  fi
}
setup_codex_cli() {
  need_node22
  command -v bwrap >/dev/null || apt_install bubblewrap
  command -v codex >/dev/null || { say "Installing the Codex CLI..."; as_root npm install -g --silent @openai/codex >/dev/null; }
  # Ubuntu 24.04+ blocks the user namespaces Codex's sandbox uses, unless bwrap is allowed.
  if [ "$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns 2>/dev/null || echo 0)" = 1 ] && [ ! -f /etc/apparmor.d/bwrap ]; then
    printf '%s\n' 'abi <abi/4.0>,' 'include <tunables/global>' '' 'profile bwrap /usr/bin/bwrap flags=(unconfined) {' '  userns,' '  include if exists <local/bwrap>' '}' \
      | as_root tee /etc/apparmor.d/bwrap >/dev/null
    as_root apparmor_parser -r /etc/apparmor.d/bwrap
  fi
  ok "Codex installed"
  if [ -n "$TTY" ] && [ -z "${OPENCLAVE_SKIP_LOGIN:-}" ]; then
    say "Now sign in with your ChatGPT account:"
    if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then codex login <"$TTY" || true
    else say "(No screen here: you get a code to enter on any device.)"; codex login --device-auth <"$TTY" || true; fi
  fi
}

step "Choose how your agents think"
ready=()
have_key ANTHROPIC_API_KEY && ready+=("Anthropic API key")
have_key XAI_API_KEY && ready+=("xAI API key")
{ command -v claude >/dev/null || [ -x "$HOME/.local/bin/claude" ]; } && ready+=("Claude subscription (Claude Code)")
command -v codex >/dev/null && ready+=("ChatGPT subscription (Codex)")
pick=1
if [ ${#ready[@]} -gt 0 ]; then
  ok "Already set up: $(IFS=,; echo "${ready[*]}" | sed 's/,/, /g')"
  yes_no "Add another way?" N || pick=0
fi
if [ "$pick" = 1 ]; then
  say "Agents need an AI model. Pick one (you can add more later):"
  say "  ${B}1${N}  Anthropic API key   pay per use, a few dollars a month for light use. Easiest."
  say "  ${B}2${N}  Claude Pro or Max   use the subscription you already pay for"
  say "  ${B}3${N}  ChatGPT Plus or Pro use the subscription you already pay for"
  say "  ${B}4${N}  xAI API key         Grok, pay per use"
  say "  ${B}5${N}  Skip for now"
  ask CHOICE "Your choice:" 1
  case "$CHOICE" in
    1) add_api_key anthropic || true ;;
    2) setup_claude_cli || true ;;
    3) setup_codex_cli || true ;;
    4) add_api_key xai || true ;;
    *) warn "No model yet. Agents can't answer until you add one: run ./install.sh again." ;;
  esac
fi

if [ -n "$MANAGER" ]; then
  if out="$(.venv/bin/python scripts/add_manager.py "$MANAGER" --persona "$PERSONA" "${EXTRAS[@]}" --mission-extra "$MISSION_EXTRA" 2>&1)"; then
    MGR_NAME="$MANAGER"; ok "Created $MGR_NAME, your manager. It interviews you briefly the first time you open it."
  else
    warn "Couldn't create the manager ($out). Add it later: Control Panel → New agent → ready-made agents."
  fi

  # ── Agents to start with (first run only; later: Control Panel → New agent) ──
  step "Agents to start with"
  tids=(); i=0
  say "Ready-made agents you can add now, or any time later (none unless you pick them):"
  while IFS=$'\t' read -r tid tname ttag; do
    i=$((i + 1)); tids+=("$tid"); say "  ${B}$i${N}  $tname: $ttag"
  done < <(.venv/bin/python scripts/add_agents.py --list)
  say "You can also make your own from scratch in the app (New agent)."
  while [ $i -gt 0 ]; do
    ask ACHOICE "Numbers of the ones you want, e.g. \"2 5\" (Enter for none):" ""
    picked=(); bad=""
    for n in ${ACHOICE//,/ }; do
      if [[ "$n" =~ ^[0-9]+$ ]] && [ "$n" -ge 1 ] && [ "$n" -le $i ]; then picked+=("${tids[$((n - 1))]}"); else bad=1; fi
    done
    if [ -z "$bad" ]; then
      if [ ${#picked[@]} -gt 0 ]; then
        while IFS= read -r line; do case "$line" in "Couldn't "*) warn "$line" ;; *) ok "Added $line" ;; esac; done < <(.venv/bin/python scripts/add_agents.py "${picked[@]}" 2>&1 || true)
        say "Each one asks you a few questions in its first chat."
      fi
      break
    fi
    [ -n "$TTY" ] || break
    warn "Type numbers from 1 to $i, separated by spaces, or just press Enter."
  done
fi

# ── Extras ───────────────────────────────────────────────────────────────
step "Extras"
if command -v qmd >/dev/null; then ok "Note search (qmd) is installed"
elif yes_no "Install note search, so agents can search all your notes? (needs Node.js, about 100 MB)" Y; then
  need_node22
  as_root npm install -g --silent @tobilu/qmd >/dev/null && ok "Note search installed" || warn "Note search didn't install; everything else works without it."
fi

BOOT=""
if [ -n "$SYSTEMD" ]; then
  if [ -f /etc/systemd/system/openclave.service ]; then BOOT=1; ok "OpenClave already starts with the computer"
  elif yes_no "Start OpenClave automatically when this computer starts?" Y; then
    sed -e "s|__USER__|$USER|g" -e "s|__REPO__|$REPO|g" -e "s|__DATA__|$DATA|g" -e "s|__HOME__|$HOME|g" \
      deploy/openclave.service.example | as_root tee /etc/systemd/system/openclave.service >/dev/null
    as_root systemctl daemon-reload
    as_root systemctl enable -q openclave
    BOOT=1; ok "Installed as a service (openclave.service)"
  fi
fi

# ── Check and start ──────────────────────────────────────────────────────
step "Checking the setup"
.venv/bin/python run.py --check || true

URL="http://localhost:$PORT/"
open_browser() {
  if [ -n "$WSL" ] && command -v explorer.exe >/dev/null; then explorer.exe "$URL" >/dev/null 2>&1 || true
  elif [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v xdg-open >/dev/null; then xdg-open "$URL" >/dev/null 2>&1 || true
  fi
}
wait_up() { for _ in $(seq 40); do curl -s -o /dev/null "$URL" && return 0; sleep 0.5; done; return 1; }
far_away() {
  if [ -z "$WSL" ] && [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
    say "No screen on this computer? From your own computer run"
    say "  ${B}ssh -L $PORT:localhost:$PORT $USER@${HOSTNAME:-this-computer}${N}"
    say "and open $URL there."
  fi
}

step "Starting OpenClave"
if [ -n "$BOOT" ]; then
  as_root systemctl restart openclave
  if wait_up; then ok "OpenClave is running: ${B}$URL${N}"; else warn "It didn't answer yet: journalctl -u openclave -n 50 shows why."; fi
  open_browser; far_away
  say ""
  say "It keeps running in the background and starts with the computer."
  say "Log: journalctl -u openclave -f   ·   Restart: sudo systemctl restart openclave"
  say "Next: open $URL, say hi to ${B}${MGR_NAME:-your manager}${N}, then add two or three agents for what you care about."
elif curl -s -o /dev/null -m 3 "$URL"; then
  ok "OpenClave is already running at ${B}$URL${N}."
  say "Stop it (Ctrl+C where it runs) and start it again so it picks up the changes:"
  say "  cd $REPO && .venv/bin/python run.py"
else
  ok "Open ${B}$URL${N} in your browser (stop OpenClave with Ctrl+C)."
  far_away
  say "Next: say hi to ${B}${MGR_NAME:-your manager}${N}, then add two or three agents for what you care about."
  say "To start it again later: cd $REPO && .venv/bin/python run.py"
  ( wait_up && open_browser ) &
  exec .venv/bin/python run.py
fi
