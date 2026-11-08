"""Web Push to the user's devices (reminders.py, and anything else that needs to
reach them while no OpenClave page is open).

Standard Web Push: VAPID (RFC 8292) to identify this server, payload
encrypted with aes128gcm (RFC 8291). Implemented on `cryptography` directly
rather than adding pywebpush and its dependencies to the system Python.
On an iPhone this only works for the Home Screen app (iOS 16.4+), and the
permission has to be granted from a tap (the bell toggle on /mobile/).

Ops state under OPENCLAVE_HOME/state: push_keys.json (the
VAPID private key) and push_subscriptions.json (one entry per device).
"""
import base64
import json
import os
import struct
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from config import CONFIG

KEYS_FILE = CONFIG.state_path("push_keys.json")
SUBSCRIPTIONS_FILE = CONFIG.state_path("push_subscriptions.json")
# Push services want a contact in the VAPID claims. Use this instance's public
# address when configured; the mailto fallback keeps local development valid.
VAPID_SUBJECT = f"{CONFIG.public_url}/mobile/" if CONFIG.public_url else "mailto:openclave@localhost"
TTL_SECONDS = 24 * 3600

_lock = threading.Lock()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _raw_public(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def _write_json(path: Path, data, private: bool = False) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    if private:
        os.chmod(tmp, 0o600)
    tmp.replace(path)


def _vapid_key() -> ec.EllipticCurvePrivateKey:
    with _lock:
        try:
            pem = json.loads(KEYS_FILE.read_text(encoding="utf-8"))["private_pem"]
            return serialization.load_pem_private_key(pem.encode(), password=None)
        except (OSError, ValueError, KeyError):
            key = ec.generate_private_key(ec.SECP256R1())
            pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()).decode()
            _write_json(KEYS_FILE, {"private_pem": pem}, private=True)
            return key


def public_key() -> str:
    """applicationServerKey for pushManager.subscribe()."""
    return _b64(_raw_public(_vapid_key().public_key()))


def encrypt(payload: bytes, p256dh: str, auth: str) -> bytes:
    """RFC 8291 aes128gcm body: header (salt, record size, sender key) + one record."""
    ua_public = _unb64(p256dh)
    auth_secret = _unb64(auth)
    sender = ec.generate_private_key(ec.SECP256R1())
    sender_public = _raw_public(sender.public_key())
    shared = sender.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public))
    ikm = HKDF(hashes.SHA256(), 32, salt=auth_secret,
               info=b"WebPush: info\x00" + ua_public + sender_public).derive(shared)
    salt = os.urandom(16)
    cek = HKDF(hashes.SHA256(), 16, salt=salt, info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt=salt, info=b"Content-Encoding: nonce\x00").derive(ikm)
    record = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)  # \x02 = last (only) record
    return salt + struct.pack("!IB", 4096, len(sender_public)) + sender_public + record


def vapid_authorization(endpoint: str) -> str:
    key = _vapid_key()
    parts = urlsplit(endpoint)
    header = _b64(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = _b64(json.dumps({"aud": f"{parts.scheme}://{parts.netloc}", "exp": int(time.time()) + 12 * 3600,
                              "sub": VAPID_SUBJECT}, separators=(",", ":")).encode())
    signing_input = f"{header}.{claims}".encode()
    r, s = decode_dss_signature(key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
    token = f"{header}.{claims}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    return f"vapid t={token}, k={_b64(_raw_public(key.public_key()))}"


def subscriptions() -> list[dict]:
    try:
        data = json.loads(SUBSCRIPTIONS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def add_subscription(sub: dict, label: str = "") -> None:
    endpoint = sub.get("endpoint")
    keys = sub.get("keys") or {}
    if not (isinstance(endpoint, str) and endpoint.startswith("https://") and keys.get("p256dh") and keys.get("auth")):
        raise ValueError("not a push subscription")
    with _lock:
        subs = [s for s in subscriptions() if s.get("endpoint") != endpoint]
        subs.append({"endpoint": endpoint, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]},
                     "label": label[:120], "added": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        _write_json(SUBSCRIPTIONS_FILE, subs, private=True)


def remove_subscription(endpoint: str) -> bool:
    with _lock:
        subs = subscriptions()
        kept = [s for s in subs if s.get("endpoint") != endpoint]
        if len(kept) == len(subs):
            return False
        _write_json(SUBSCRIPTIONS_FILE, kept, private=True)
        return True


def send(sub: dict, payload: dict) -> int:
    """POST one push. Returns the push service's HTTP status."""
    body = encrypt(json.dumps(payload).encode(), sub["keys"]["p256dh"], sub["keys"]["auth"])
    res = requests.post(sub["endpoint"], data=body, timeout=15, headers={
        "Authorization": vapid_authorization(sub["endpoint"]),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(TTL_SECONDS),
        "Urgency": "high",
    })
    return res.status_code


def send_all(title: str, body: str, url: str = "/mobile/", tag: str | None = None) -> int:
    """Push to every subscribed device; drops subscriptions the push service
    says are gone. Returns how many accepted it."""
    payload = {"title": title, "body": body[:400], "url": url, "tag": tag}
    delivered = 0
    for sub in subscriptions():
        try:
            status = send(sub, payload)
        except Exception as exc:
            print(f"push to {sub.get('label') or sub.get('endpoint', '')[:40]} failed: {exc}")
            continue
        if status in (404, 410):
            remove_subscription(sub["endpoint"])
        elif 200 <= status < 300:
            delivered += 1
        else:
            print(f"push to {sub.get('label') or sub.get('endpoint', '')[:40]} refused: HTTP {status}")
    return delivered
