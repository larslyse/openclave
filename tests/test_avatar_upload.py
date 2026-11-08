import io
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from PIL import Image

import main


class AvatarUploadTest(unittest.TestCase):
    def test_custom_avatar_is_normalized_and_served(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(main, "AVATAR_DIR", Path(folder)):
            source = io.BytesIO()
            Image.new("RGB", (500, 300), "green").save(source, "JPEG")
            class Request:
                def __init__(self, body): self.content = body
                async def body(self): return self.content
            self.assertEqual(asyncio.run(main.set_agent_avatar("rick", Request(source.getvalue()))), {"ok": True})
            avatar = main.agent_avatar("rick")
            image = Image.open(avatar.path)
            self.assertLessEqual(max(image.size), 256)
            with self.assertRaises(HTTPException) as invalid:
                asyncio.run(main.set_agent_avatar("rick", Request(b"not an image")))
            self.assertEqual(invalid.exception.status_code, 400)
            with self.assertRaises(HTTPException) as unknown:
                asyncio.run(main.set_agent_avatar("unknown", Request(source.getvalue())))
            self.assertEqual(unknown.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
