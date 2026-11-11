"""Data format version and the migrations that bring old data up to date.

OPENCLAVE_HOME/state/version.json holds {"data_version": N}: the format of
everything OpenClave keeps on disk (agent folders, agent.json, chat front
matter, state/*.json). MIGRATIONS lists the steps in order; step N turns
data of version N-1 into version N. At startup run() applies every step above
the stored version, in order, each exactly once, and stores the new version
after each success.

A fresh install (no state files yet) is stamped with the current version
without running anything. An existing install without version.json counts as
version 0.

A failing step stops the run and leaves the stored version at the last step
that worked, so the next start tries again from there. The app keeps
starting either way: a half-migrated install that still opens is easier to
fix than one that doesn't.

Before the first step that changes data, the state/*.json files are copied
to state/backups/<timestamp>/. Agent folders are not copied (they can be
large); a step that changes them should be careful and idempotent.

    python3 -m migrations --status   show the stored and current version
    python3 -m migrations --run      apply pending steps now
"""
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from state_io import load_json, save_json

VERSION_FILE = "version.json"
BACKUP_DIR = "backups"

# (version, description, function or None). The function gets the CONFIG
# object (paths: home, state_dir, agents_root ...). None means the step only
# stamps the version: nothing on disk changes, so no backup is taken.
Migration = tuple[int, str, Callable | None]

MIGRATIONS: list[Migration] = [
    (1, "baseline: the data format of release 0.1.0", None),
]

CURRENT_VERSION = MIGRATIONS[-1][0]


def stored_version(state_dir: Path) -> int | None:
    """The stored data version, or None when version.json is missing or unreadable."""
    data = load_json(Path(state_dir) / VERSION_FILE, None)
    if isinstance(data, dict) and isinstance(data.get("data_version"), int):
        return data["data_version"]
    return None


def _stamp(state_dir: Path, version: int) -> None:
    save_json(Path(state_dir) / VERSION_FILE, {"data_version": version}, mode=0o644)


def _is_fresh(state_dir: Path) -> bool:
    """No state files yet: nothing to migrate."""
    state_dir = Path(state_dir)
    return not state_dir.is_dir() or not any(p.name != VERSION_FILE for p in state_dir.glob("*.json"))


def backup_state(state_dir: Path) -> Path:
    """Copy state/*.json to state/backups/<timestamp>/ and return that folder."""
    state_dir = Path(state_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = state_dir / BACKUP_DIR / stamp
    target.mkdir(parents=True, exist_ok=False)
    for path in sorted(state_dir.glob("*.json")):
        shutil.copy2(path, target / path.name)
    return target


def pending(state_dir: Path, migrations: list[Migration] | None = None) -> list[Migration]:
    migrations = MIGRATIONS if migrations is None else migrations
    version = stored_version(state_dir)
    start = 0 if version is None else version
    return [m for m in migrations if m[0] > start]


def run(config=None, migrations: list[Migration] | None = None, log=print) -> dict:
    """Apply pending migrations. Never raises; the result says what happened:
    {"from": N, "to": N, "ran": [versions], "error": str | None}."""
    if config is None:
        from config import CONFIG as config
    migrations = MIGRATIONS if migrations is None else migrations
    current = migrations[-1][0]
    state_dir = Path(config.state_dir)
    result = {"from": stored_version(state_dir), "to": None, "ran": [], "error": None}
    try:
        if result["from"] is None and _is_fresh(state_dir):
            state_dir.mkdir(parents=True, exist_ok=True)
            _stamp(state_dir, current)
            result["to"] = current
            return result
        version = result["from"] or 0
        backed_up = False
        for number, description, fn in migrations:
            if number <= version:
                continue
            if fn is not None and not backed_up:
                where = backup_state(state_dir)
                log(f"data migration: state files backed up to {where}")
                backed_up = True
            try:
                if fn is not None:
                    fn(config)
            except Exception as exc:
                result["error"] = f"migration {number} ({description}) failed: {exc}"
                log(f"data migration {number} FAILED: {exc}. Data stays at version {version}; "
                    "OpenClave starts anyway and tries again at the next start. "
                    "Please report this with the message above.")
                break
            _stamp(state_dir, number)
            version = number
            result["ran"].append(number)
            log(f"data migration {number}: {description}")
        result["to"] = version
    except Exception as exc:  # e.g. the state folder is not writable
        result["error"] = f"migrations could not run: {exc}"
        log(f"data migrations could not run: {exc}. OpenClave starts anyway.")
    return result


def main(argv: list[str]) -> int:
    from config import CONFIG
    if "--run" in argv:
        result = run(CONFIG)
        print(f"data version: {result['to']}" + (f" (error: {result['error']})" if result["error"] else ""))
        return 1 if result["error"] else 0
    if "--status" in argv or not argv:
        version = stored_version(CONFIG.state_dir)
        todo = pending(CONFIG.state_dir)
        print(f"data version: {'none stored' if version is None else version}; current: {CURRENT_VERSION}")
        for number, description, _ in todo:
            print(f"  pending {number}: {description}")
        return 0
    print("usage: python3 -m migrations --status | --run")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
