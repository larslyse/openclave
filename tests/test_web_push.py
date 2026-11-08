import base64
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

import web_push


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class WebPushTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        for patcher in (patch.object(web_push, "KEYS_FILE", tmp / "k.json"),
                        patch.object(web_push, "SUBSCRIPTIONS_FILE", tmp / "s.json")):
            patcher.start()
            self.addCleanup(patcher.stop)
        # A "browser": its own key pair and auth secret, as pushManager.subscribe() makes.
        self.ua_key = ec.generate_private_key(ec.SECP256R1())
        self.ua_public = self.ua_key.public_key().public_bytes(serialization.Encoding.X962,
                                                                serialization.PublicFormat.UncompressedPoint)
        self.auth = b"0123456789abcdef"
        self.sub = {"endpoint": "https://web.push.apple.com/abc",
                    "keys": {"p256dh": web_push._b64(self.ua_public), "auth": web_push._b64(self.auth)}}

    def _decrypt(self, body: bytes) -> bytes:
        """What the browser does with an aes128gcm push body (RFC 8291)."""
        salt, rs, idlen = body[:16], *struct.unpack("!IB", body[16:21])
        sender_public = body[21:21 + idlen]
        shared = self.ua_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), sender_public))
        ikm = HKDF(hashes.SHA256(), 32, salt=self.auth, info=b"WebPush: info\x00" + self.ua_public + sender_public).derive(shared)
        cek = HKDF(hashes.SHA256(), 16, salt=salt, info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
        nonce = HKDF(hashes.SHA256(), 12, salt=salt, info=b"Content-Encoding: nonce\x00").derive(ikm)
        plain = AESGCM(cek).decrypt(nonce, body[21 + idlen:], None)
        self.assertEqual(plain[-1:], b"\x02")
        return plain[:-1]

    def test_payload_decrypts_on_the_browser_side(self):
        body = web_push.encrypt(b'{"title":"Reba"}', self.sub["keys"]["p256dh"], self.sub["keys"]["auth"])
        self.assertEqual(self._decrypt(body), b'{"title":"Reba"}')

    def test_vapid_token_verifies_with_published_key(self):
        auth = web_push.vapid_authorization(self.sub["endpoint"])
        token, key = auth.removeprefix("vapid t=").split(", k=")
        self.assertEqual(key, web_push.public_key())
        header, claims, sig = token.split(".")
        self.assertEqual(json.loads(_unb64(claims))["aud"], "https://web.push.apple.com")
        raw = _unb64(sig)
        pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), _unb64(key))
        pub.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                   f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))  # raises if wrong
        self.assertEqual(web_push.public_key(), key)  # key persists

    def test_send_all_counts_and_drops_expired(self):
        web_push.add_subscription(self.sub, "iPhone app")
        web_push.add_subscription({**self.sub, "endpoint": "https://fcm.googleapis.com/gone"}, "old")
        statuses = {"https://web.push.apple.com/abc": 201, "https://fcm.googleapis.com/gone": 410}
        with patch.object(web_push.requests, "post", side_effect=lambda url, **kw: MagicMock(status_code=statuses[url])) as post:
            self.assertEqual(web_push.send_all("Reba", "How did the race go?", url="/mobile/"), 1)
        body = post.call_args_list[0].kwargs["data"]
        self.assertEqual(json.loads(self._decrypt(body))["body"], "How did the race go?")
        self.assertEqual([s["label"] for s in web_push.subscriptions()], ["iPhone app"])

    def test_rejects_non_subscriptions(self):
        with self.assertRaises(ValueError):
            web_push.add_subscription({"endpoint": "http://evil"}, "")


if __name__ == "__main__":
    unittest.main()
