"""Route table safety net: the app registers the same (methods, path) routes as
the fixture, and for every route's path the first matching handler is the same.

The table is built in a fresh interpreter (the synthetic agents from
tests/fixtures/Agents, plus one agent registered at runtime) so routes other
tests add while the suite runs do not leak in. Regenerate the fixture only for
an intended route change:  python3 tests/test_route_table.py --write
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).parent / "fixtures" / "route_table.txt"
RUNTIME_AGENT = "zz-runtime"

_DUMP = r"""
import json, re, sys
sys.path.insert(0, ".")
sys.path.insert(0, "tests")
import test_00_agents_data  # synthetic agents, as in the unit suite
import main
main._register_agent_routes(%(agent)r)
from starlette.routing import Mount

def entry(route):
    if isinstance(route, Mount):
        return {"methods": "MOUNT", "path": route.path, "name": route.name}
    return {"methods": ",".join(sorted(route.methods or [])), "path": route.path,
            "name": getattr(route.endpoint, "__name__", "?")}

routes = list(main.app.routes)
table = [entry(r) for r in routes]

def first_match(path, method):
    for route, row in zip(routes, table):
        if isinstance(route, Mount):
            if path == route.path or path.startswith(route.path + "/"):
                return row["name"]
            continue
        if route.path_regex.match(path) and method in (route.methods or ()):
            return row["name"]
    return None

winners = []
for route, row in zip(routes, table):
    if isinstance(route, Mount):
        continue
    sample = re.sub(r"\{[^}]+\}", "x", route.path)
    for method in sorted(route.methods or []):
        winners.append(f"{method} {sample} -> {first_match(sample, method)}")
print(json.dumps({"table": table, "winners": winners}))
"""


def current() -> dict:
    env = dict(os.environ, OPENCLAVE_DISABLE_BACKGROUND_JOBS="1")
    out = subprocess.run([sys.executable, "-c", _DUMP % {"agent": RUNTIME_AGENT}], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return json.loads(out.stdout.strip().splitlines()[-1])


def render(data: dict) -> str:
    lines = ["# routes: methods path handler"]
    lines += [f"{r['methods']} {r['path']} {r['name']}" for r in data["table"]]
    lines += ["# first match per route path: method sample -> handler"]
    lines += data["winners"]
    return "\n".join(lines) + "\n"


def parse(text: str) -> tuple[list[str], list[str]]:
    routes, winners, section = [], [], None
    for line in text.splitlines():
        if line.startswith("# routes"):
            section = routes
        elif line.startswith("# first match"):
            section = winners
        elif line.strip():
            section.append(line)
    return routes, winners


class RouteTableTests(unittest.TestCase):
    def test_routes_match_fixture(self):
        expected_routes, expected_winners = parse(FIXTURE.read_text())
        routes, winners = parse(render(current()))
        # Same set of (methods, path); the handler module may move, the name may not.
        self.assertEqual(sorted(r.rsplit(" ", 1)[0] for r in routes),
                         sorted(r.rsplit(" ", 1)[0] for r in expected_routes))
        self.assertEqual(sorted(routes), sorted(expected_routes))
        self.assertEqual(sorted(set(winners)), sorted(set(expected_winners)))


if __name__ == "__main__":
    if "--write" in sys.argv:
        FIXTURE.write_text(render(current()))
        print(f"wrote {FIXTURE}")
    else:
        unittest.main()
