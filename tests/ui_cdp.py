"""Tiny Chrome DevTools driver for the UI smoke test (tests/test_ui_smoke.py):
evaluate JS in the page or in a sandboxed child frame, click at coordinates,
take screenshots. Chromium runs headless with site isolation off so the
sandboxed agent-page iframe stays in-process and reachable."""
import asyncio, base64, json, os, shutil, signal, socket, subprocess, time, urllib.request
import websockets

# The browser and where its profiles and screenshots go. The defaults suit the
# snap Chromium, which can only write under ~/snap/chromium/common; CI points
# OPENCLAVE_UI_CHROMIUM at the Chrome it installs and OPENCLAVE_UI_DIR at a
# temp folder.
CHROMIUM = os.environ.get("OPENCLAVE_UI_CHROMIUM") or "chromium"
UI_DIR = os.environ.get("OPENCLAVE_UI_DIR") or os.path.expanduser("~/snap/chromium/common")


def _free_port():
    # A new debug port per browser: with one fixed port, a browser still
    # shutting down from the previous test kept it, and the next test
    # attached to that old browser (and its storage) instead of its own.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
PROFILE = os.path.join(UI_DIR, "openclave-ui-test-profile")
SHOTS = os.path.join(UI_DIR, "openclave-ui-test-shots")


class Browser:
    async def start(self, width=1400, height=900):
        # Its own port and a fresh profile every time: browser storage
        # (Autojump, seen replies, drafts) left by an earlier test made later
        # ones depend on run order.
        port = _free_port()
        self.profile = f"{PROFILE}-{port}"
        shutil.rmtree(self.profile, ignore_errors=True)
        os.makedirs(self.profile, exist_ok=True); os.makedirs(SHOTS, exist_ok=True)
        self.proc = subprocess.Popen([
            CHROMIUM, "--headless=new", "--no-sandbox", "--disable-gpu", f"--remote-debugging-port={port}",
            f"--user-data-dir={self.profile}", "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
            "--autoplay-policy=no-user-gesture-required", "--disable-site-isolation-trials", "--disable-features=IsolateOrigins,site-per-process", f"--window-size={width},{height}", "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)  # own group, see stop()
        for _ in range(50):
            try:
                pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
                page = next(p for p in pages if p["type"] == "page")
                break
            except Exception:
                await asyncio.sleep(0.2)
        self.ws = await websockets.connect(page["webSocketDebuggerUrl"], max_size=50_000_000)
        self.seq = 0; self.waiting = {}; self.contexts = {}; self.logs = []
        self.reader = asyncio.create_task(self._read())
        await self.send("Runtime.enable"); await self.send("Page.enable")
        await self.send("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": width < 600})

    async def _read(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            if "id" in msg and msg["id"] in self.waiting:
                self.waiting.pop(msg["id"]).set_result(msg)
            elif msg.get("method") == "Runtime.executionContextCreated":
                ctx = msg["params"]["context"]
                aux = ctx.get("auxData", {})
                if aux.get("isDefault"): self.contexts[aux.get("frameId")] = ctx["id"]
            elif msg.get("method") == "Runtime.exceptionThrown":
                self.logs.append("EXC " + json.dumps(msg["params"]["exceptionDetails"].get("exception", {}).get("description", ""))[:300])
            elif msg.get("method") == "Runtime.consoleAPICalled" and msg["params"]["type"] in ("error", "warning"):
                self.logs.append("CONSOLE " + " ".join(str(a.get("value", a.get("description", ""))) for a in msg["params"]["args"])[:300])

    async def send(self, method, params=None):
        self.seq += 1
        fut = asyncio.get_event_loop().create_future()
        self.waiting[self.seq] = fut
        await self.ws.send(json.dumps({"id": self.seq, "method": method, "params": params or {}}))
        msg = await asyncio.wait_for(fut, 30)
        if "error" in msg: raise RuntimeError(f"{method}: {msg['error']}")
        return msg.get("result", {})

    async def goto(self, url, wait=3.0):
        self.contexts.clear()
        await self.send("Page.navigate", {"url": url})
        await asyncio.sleep(wait)

    async def frames(self):
        tree = (await self.send("Page.getFrameTree"))["frameTree"]
        top = tree["frame"]["id"]
        children = [c["frame"]["id"] for c in tree.get("childFrames", [])]
        return top, children

    async def eval(self, expr, frame=None):
        params = {"expression": f"(async () => {{ {expr} }})()", "awaitPromise": True, "returnByValue": True}
        if frame: params["contextId"] = self.contexts[frame]
        res = await self.send("Runtime.evaluate", params)
        if "exceptionDetails" in res: raise RuntimeError(res["exceptionDetails"].get("exception", {}).get("description", res["exceptionDetails"]))
        return res["result"].get("value")

    async def click(self, x, y, hold_s=0.0):
        await self.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        await self.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
        if hold_s:
            await asyncio.sleep(hold_s)
            return
        await self.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})

    async def release(self, x, y):
        await self.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})

    async def shot(self, name):
        data = (await self.send("Page.captureScreenshot", {"format": "png"}))["data"]
        path = os.path.join(SHOTS, name)
        open(path, "wb").write(base64.b64decode(data))
        return path

    async def stop(self):
        self.reader.cancel()
        await self.ws.close()
        # Stop the whole process group: the snap launcher's real Chromium
        # outlived terminate() and kept the debug port, so every later UI
        # test run talked to a stale browser and failed.
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        shutil.rmtree(self.profile, ignore_errors=True)
