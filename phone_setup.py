"""Phone access through Tailscale: one place for the steps the installer and
Control Panel → Phone both run.

A phone needs an HTTPS address for the microphone and notifications, and
OpenClave shouldn't be open to the internet. `tailscale serve` gives both: a
https://<name>.<tailnet>.ts.net address that only the user's own devices can
reach. The steps:

1. Tailscale is installed and signed in.
2. MagicDNS and HTTPS certificates are on for the tailnet (admin console).
3. `tailscale serve --bg <port>` (needs root unless this user is the
   Tailscale operator; then we print the commands instead).
4. The https address is saved as `[network] public_url` in openclave.toml.

Nothing here runs unless the user asked for it. It never replaces a
`tailscale serve` setup that already points somewhere else.

    python3 phone_setup.py status --port 8777
    python3 phone_setup.py setup --port 8777 --toml ~/.openclave/openclave.toml
"""
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

import toml_edit

ADMIN_DNS_URL = "https://login.tailscale.com/admin/dns"


def _tailscale() -> str | None:
    return shutil.which("tailscale")


def _run(args: list[str], timeout: float = 15) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _json(args: list[str]) -> dict:
    out = _run(args)
    if not out or out.returncode != 0:
        return {}
    try:
        data = json.loads(out.stdout or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _served(binary: str, port: int) -> tuple[str, int | None]:
    """(https address serving `port`, or "";  port that "/" serves instead, or None)."""
    web = _json([binary, "serve", "status", "--json"]).get("Web") or {}
    other = None
    for hostport, conf in web.items():
        handlers = (conf or {}).get("Handlers") or {}
        root = handlers.get("/") or {}
        proxy = str(root.get("Proxy") or "")
        if not proxy:
            continue
        try:
            target = urlsplit(proxy if "://" in proxy else f"http://{proxy}").port
        except ValueError:
            target = None
        host, _, hport = hostport.rpartition(":")
        host = host or hostport
        if target == port:
            return (f"https://{host}" if hport in ("", "443") else f"https://{host}:{hport}"), None
        if hport in ("", "443"):
            other = target
    return "", other


def status(port: int) -> dict:
    """What Tailscale looks like on this computer; only reads, changes nothing."""
    binary = _tailscale()
    result = {"installed": bool(binary), "logged_in": False, "dns_name": "", "magic_dns": False,
              "https_certs": False, "served_url": "", "serves_other_port": None}
    if not binary:
        return result
    st = _json([binary, "status", "--json"])
    result["logged_in"] = st.get("BackendState") == "Running"
    result["dns_name"] = str((st.get("Self") or {}).get("DNSName") or "").rstrip(".")
    result["magic_dns"] = bool((st.get("CurrentTailnet") or {}).get("MagicDNSEnabled"))
    result["https_certs"] = bool(st.get("CertDomains"))
    if result["logged_in"]:
        result["served_url"], result["serves_other_port"] = _served(binary, port)
    return result


def root_commands(port: int) -> list[str]:
    return [f"sudo tailscale serve --bg {port}", "sudo tailscale set --operator=$USER"]


def _permission_problem(text: str) -> bool:
    text = text.lower()
    return any(word in text for word in ("access denied", "permission denied", "denied", "must be root",
                                         "sudo", "operator"))


def saved_public_url(toml_path: Path | str) -> str:
    try:
        raw = tomllib.loads(Path(toml_path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    network = raw.get("network") if isinstance(raw.get("network"), dict) else {}
    return str(network.get("public_url") or "").strip().rstrip("/")


def setup(port: int, toml_path: Path | str) -> dict:
    """Run the steps. Returns {ok, reason, message, url, mobile_url, commands}.

    reason when not ok: not_installed, logged_out, https_off, busy,
    needs_root, failed, save_failed."""
    def fail(reason, message, **extra):
        return {"ok": False, "reason": reason, "message": message, "url": "", "mobile_url": "",
                "commands": [], **extra}

    st = status(port)
    if not st["installed"]:
        return fail("not_installed", "Tailscale isn't installed on this computer. Get it from "
                    "https://tailscale.com/download, sign in, then try again.")
    if not st["logged_in"]:
        return fail("logged_out", "Tailscale is installed but not signed in. Run `sudo tailscale up` "
                    "in a terminal and sign in, then try again.")
    if not st["magic_dns"] or not st["https_certs"]:
        return fail("https_off", "Your tailnet needs MagicDNS and HTTPS certificates. In the Tailscale "
                    f"admin console ({ADMIN_DNS_URL}), under DNS, turn on MagicDNS and HTTPS "
                    "Certificates, then try again.")
    url = st["served_url"]
    if not url:
        if st["serves_other_port"] is not None:
            return fail("busy", f"Tailscale already serves another program (port {st['serves_other_port']}) "
                        "at this computer's address. OpenClave won't change that. Run `tailscale serve "
                        "status` to see it; set it up by hand if you want both (README, \"Use it from "
                        "your phone\").")
        out = _run([_tailscale(), "serve", "--bg", str(port)], timeout=30)
        if out is None:
            return fail("https_off", "Tailscale didn't answer (it may be waiting for HTTPS to be turned "
                        f"on). In the admin console ({ADMIN_DNS_URL}) turn on MagicDNS and HTTPS "
                        "Certificates, then try again.")
        if out.returncode != 0:
            text = (out.stderr or "") + (out.stdout or "")
            if _permission_problem(text):
                return fail("needs_root", "Tailscale only lets an administrator change this. Run these "
                            "in a terminal on this computer (the second one lets OpenClave do it itself "
                            "next time), then check again:", commands=root_commands(port))
            return fail("failed", f"tailscale serve failed: {text.strip()[:300] or 'no message'}")
        url = _served(_tailscale(), port)[0] or (f"https://{st['dns_name']}" if st["dns_name"] else "")
        if not url:
            return fail("failed", "Tailscale serves OpenClave now, but didn't say at which address. "
                        "Run `tailscale serve status` and put the https address in openclave.toml "
                        "as [network] public_url.")
    try:
        toml_edit.set_key(toml_path, "network", "public_url", url)
    except (OSError, ValueError) as exc:
        return fail("save_failed", f"Couldn't save the address to {toml_path}: {exc}", url=url,
                    mobile_url=f"{url}/mobile/")
    return {"ok": True, "reason": "", "message": "", "url": url, "mobile_url": f"{url}/mobile/",
            "commands": []}


def main(argv: list[str]) -> int:
    """Exit codes: status → 0 ready, 10 not installed, 11 not signed in.
    setup → 0 done, 3 needs root (commands printed), 1 anything else."""
    cmd = argv[0] if argv else ""
    opts = dict(zip(argv[1::2], argv[2::2]))
    port = int(opts.get("--port", "8777"))
    if cmd == "status":
        st = status(port)
        if "--json" in argv:
            print(json.dumps(st))
        return 0 if st["logged_in"] else (11 if st["installed"] else 10)
    if cmd == "setup":
        toml = opts.get("--toml") or str(Path(os.environ.get("OPENCLAVE_HOME") or "~/.openclave").expanduser()
                                         / "openclave.toml")
        result = setup(port, toml)
        if "--json" in argv:
            print(json.dumps(result))
        elif result["ok"]:
            print(f"Saved [network] public_url = \"{result['url']}\" in {toml}")
        else:
            print(result["message"])
            for line in result["commands"]:
                print(f"  {line}")
        return 0 if result["ok"] else (3 if result["reason"] == "needs_root" else 1)
    print(__doc__.strip().splitlines()[-2].strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
