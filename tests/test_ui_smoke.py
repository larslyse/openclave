"""UI smoke test in real headless Chromium against a throwaway server
(tests/ui_server.py: temp vault copy, fake model). Checks the layout rules
The owner has asked for, which unit tests can't see:

  - mobile: Quick jump sits under the message box, Send isn't covered, and
    the hold menu opens above the composer, on the right of the screen, with
    the agent names left-aligned (asked for twice, 2026-09-27)
  - mobile and desktop: a message sends and the reply shows "model · time"
  - desktop: the agent page's chat (sandboxed iframe) loads with no errors

Skipped when Chromium or the websockets package isn't available.
"""
import asyncio
import json
import os
import shutil
import site
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

import agents

HERE = Path(__file__).resolve().parent
FIXTURE_ROOT = HERE / "fixtures" / "Agents"
try:
    import websockets  # noqa: F401
    sys.path.insert(0, str(HERE))
    from ui_cdp import CHROMIUM, Browser
    # OPENCLAVE_SKIP_UI_TESTS=1: the CI unit job; the ui job runs these.
    HAVE_DEPS = bool(shutil.which(CHROMIUM)) and not os.environ.get("OPENCLAVE_SKIP_UI_TESTS")
except ImportError:
    HAVE_DEPS = False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(HAVE_DEPS, "needs chromium and websockets")
class UiSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        vault_agents = cls.tmp / ".openclave" / "agents"
        shutil.copytree(FIXTURE_ROOT, vault_agents)
        (vault_agents.parent / "openclave.md").write_text("# Synthetic OpenClave test concept\n", encoding="utf-8")
        dashboard = (HERE.parent / "templates" / "agent_dashboard.html").read_text(encoding="utf-8")
        for folder in vault_agents.iterdir():
            if not folder.is_dir():
                continue
            config_file = folder / agents.AGENT_FILE
            config = json.loads(config_file.read_text(encoding="utf-8")) if config_file.exists() else {}
            agent = next((a for a in agents.AGENTS.values() if a.folder == folder.name), None)
            name = config.get("name", agent.name if agent else folder.name)
            agent_id = config.get("id", agent.id if agent else folder.name.lower())
            color = config.get("color", agent.color if agent else "#6688AA")
            html = (dashboard.replace("{{ID}}", agent_id).replace("{{NAME}}", name)
                    .replace("{{INITIALS}}", name[:1]).replace("{{COLOR}}", color))
            (folder / "dashboard.html").write_text(html, encoding="utf-8")
        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.server = subprocess.Popen([sys.executable, str(HERE / "ui_server.py"), str(cls.port)],
                                      # PYTHONUSERBASE keeps packages installed with pip --user
                                      # (~/.local) importable once HOME points at the temp dir.
                                      env={**os.environ, "HOME": str(cls.tmp),
                                           "PYTHONUSERBASE": site.getuserbase(),
                                           "OPENCLAVE_HOME": str(cls.tmp / ".openclave")},
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                urllib.request.urlopen(f"{cls.base}/api/version", timeout=1)
                break
            except OSError:
                time.sleep(0.5)
        else:
            cls.server.kill()
            raise RuntimeError("UI test server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        try:
            cls.server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.server.kill()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_async(self, coro):
        return asyncio.run(asyncio.wait_for(coro, 120))

    async def _mobile(self):
        br = Browser()
        await br.start(390, 844)
        try:
            await br.goto(f"{self.base}/mobile/#/agents", wait=3)
            await br.goto(f"{self.base}/mobile/#/agents/spice/new", wait=5)
            # Another agent replies while the owner sits in this chat: Quick jump must
            # still show it as new and at the bottom (2026-10-05: the menu used
            # the agent list fetched when the chat opened, so it never did).
            await br.eval("fetch('/stash/chat-requests',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request_id:'order-smoke',conversation_id:null,message:'hi',attachments:[]})}); return 1;")
            await asyncio.sleep(3)
            box = lambda sel: br.eval(f"const r=document.querySelector({sel!r}).getBoundingClientRect(); return {{x:r.x,y:r.y,w:r.width,h:r.height}};")
            ta, qj, send = await box(".composer textarea"), await box("#switch-btn"), await box(".composer .send")
            self.assertGreaterEqual(qj["y"], ta["y"] + ta["h"] - 1, "Quick jump is under the message box")
            self.assertGreater(qj["w"], 200, "Quick jump is a long bar")
            # Much bigger and orange, the core multitasking button (2026-10-12).
            self.assertGreaterEqual(qj["h"], 44, "Quick jump is big")
            self.assertEqual(await br.eval("return getComputedStyle(document.querySelector('#switch-btn')).backgroundColor;"), "rgb(249, 115, 22)", "orange")
            home = await box(".composer [data-home]")
            self.assertLess(abs(home["y"] - qj["y"]), 2, "in a chat, All agents is on the Quick jump row")
            self.assertLessEqual(home["x"] + home["w"], qj["x"] + 1, "just left of Quick jump")
            self.assertLessEqual(send["x"] + send["w"], 390, "Send is on screen")
            top = await br.eval(f"const e=document.elementFromPoint({send['x'] + send['w'] / 2}, {send['y'] + send['h'] / 2}); return e.closest('.send') ? 'send' : e.className;")
            self.assertEqual(top, "send", "nothing covers Send")
            # Notifications: icon only, in the top bar left of the model picker (2026-09-29).
            bell, badge = await box(".topbar .notify-btn"), await box(".topbar .model-badge")
            self.assertLessEqual(bell["x"] + bell["w"], badge["x"], "bell is left of the model picker")
            self.assertLess(bell["w"], 50, "bell is icon only")

            # Agent screens: avatar next to the name, and left of it the
            # button to the agent's own page (2026-10-05).
            title, feature = await box(".topbar h1 .title-text"), await box("#feature-btn")
            pic = await box(".topbar h1 .avatar img")
            self.assertLessEqual(feature["x"] + feature["w"], pic["x"], "feature button is left of the avatar")
            self.assertLessEqual(pic["x"] + pic["w"], title["x"], "avatar is right before the name")

            # Hold Quick jump: the menu opens above the composer, right side, names left-aligned.
            await br.click(qj["x"] + qj["w"] / 2, qj["y"] + qj["h"] / 2, hold_s=0.6)
            items = await br.eval("return [...document.querySelectorAll('.quick-switch-item')].map(i=>{const r=i.getBoundingClientRect(); const t=[...i.childNodes].find(n=>n.nodeType===3); const rr=document.createRange(); rr.selectNodeContents(t); const tr=rr.getBoundingClientRect(); return {x:r.x,w:r.width,right:r.right,bottom:r.bottom,textX:tr.x};});")
            composer_top = await br.eval("return document.querySelector('.composer').getBoundingClientRect().top;")
            toggle_bg = await br.eval("const t=document.querySelector('.quick-switch-toggle'); return t ? getComputedStyle(t).backgroundColor : null;")
            await br.release(10, 10)
            if toggle_bg is not None:
                self.assertNotIn(toggle_bg, ("transparent", "rgba(0, 0, 0, 0)"), "the Autojump row is solid, the chat doesn't show through")
            self.assertGreater(len(items), 3, "menu lists the other agents")
            self.assertTrue(all(i["bottom"] <= composer_top + 1 for i in items), f"menu sits above the composer (composer top {composer_top:.0f}, lowest item {max(i['bottom'] for i in items):.0f})")
            self.assertTrue(all(i["right"] > 390 * 0.6 for i in items), "menu is on the right of the screen")
            self.assertEqual(len({round(i["textX"]) for i in items}), 1, "agent names start at the same x (left-aligned)")
            order = await br.eval("return [...document.querySelectorAll('.quick-switch-item')].map(i=>i.dataset.agent);")
            activity = {a["id"]: a.get("last_activity_ms") or -1 for a in await br.eval("return fetch('/api/agents').then(r=>r.json());")}
            self.assertEqual(order[-1], "stash", "the agent with the latest message is at the bottom, nearest the thumb")
            self.assertTrue(await br.eval("return !!document.querySelector('.quick-switch-item[data-agent=stash] .new-tag');"),
                            "a reply that landed while in this chat shows as new")
            # Unread replies sit nearest the thumb, then latest activity (app.js drawItems);
            # earlier tests can leave other agents with unread replies.
            unread = set(await br.eval("return [...document.querySelectorAll('.quick-switch-item')].filter(i=>i.querySelector('.new-tag')).map(i=>i.dataset.agent);"))
            rank = lambda a: (a in unread, activity[a])
            self.assertEqual([rank(a) for a in order], sorted(rank(a) for a in order), "older ones above it")

            # A hold released on no agent keeps the menu open; tapping Quick jump closes it.
            qx, qy = qj["x"] + qj["w"] / 2, qj["y"] + qj["h"] / 2
            count = "return document.querySelectorAll('.quick-switch-item').length;"
            self.assertGreater(await br.eval(count), 3, "menu stays open after releasing on no agent")
            await br.click(qx, qy)
            self.assertEqual(await br.eval(count), 0, "tapping Quick jump again closes the menu")
            # Tap Quick jump: the same agent list, then tap an agent to jump.
            await br.click(qx, qy)
            await asyncio.sleep(0.5)
            tapped = await br.eval("return [...document.querySelectorAll('.quick-switch-item')].map(i=>{const r=i.getBoundingClientRect(); return {id:i.dataset.agent,x:r.x+r.width/2,y:r.y+r.height/2};});")
            self.assertEqual(len(tapped), len(items), "tap opens the same agent list as hold")
            await br.eval("window.__drawn=[]; new MutationObserver(()=>window.__drawn.push(location.hash+'|'+(document.querySelector('#convo-list')?'list':document.querySelector('#chat')?'chat':'?'))).observe(document.getElementById('app'),{childList:true}); return 1;")
            await br.click(tapped[0]["x"], tapped[0]["y"])
            await asyncio.sleep(1.5)
            drawn = [d for d in await br.eval("return window.__drawn;") if not d.endswith("|?")]
            list_at = next((i for i, d in enumerate(drawn) if d == f"#/agents/{tapped[0]['id']}/conversations|list"), None)
            chat_at = next((i for i, d in enumerate(drawn) if d.endswith("|chat") and f"/agents/{tapped[0]['id']}/" in d), None)
            self.assertIsNotNone(list_at, f"the target's list is drawn first (iOS swipe-back preview), got {drawn}")
            self.assertLess(list_at, chat_at, "then its chat")
            loc = await br.eval("return location.hash;")
            self.assertIn(f"#/agents/{tapped[0]['id']}", loc, "tapping an agent jumps to it")
            self.assertEqual(await br.eval(count), 0, "menu closes after jumping")
            # After a jump, Back (Safari's edge swipe) and our own swipe right both
            # open the jumped-to agent's conversation list, never the previous agent.
            target_list = f"#/agents/{tapped[0]['id']}/conversations"
            await br.eval("history.back(); return 1;")
            await asyncio.sleep(1)
            self.assertEqual(await br.eval("return location.hash;"), target_list, "Back after Quick jump opens that agent's list")
            await br.eval("history.forward(); return 1;")
            await asyncio.sleep(1.5)
            swipe = ("const a=document.querySelector('#chat'); const t=(x)=>new Touch({identifier:1,target:a,clientX:x,clientY:400});"
                     "a.dispatchEvent(new TouchEvent('touchstart',{touches:[t(40)],changedTouches:[t(40)],bubbles:true}));"
                     "a.dispatchEvent(new TouchEvent('touchmove',{touches:[t(120)],changedTouches:[t(120)],bubbles:true}));"
                     "a.dispatchEvent(new TouchEvent('touchend',{touches:[],changedTouches:[t(250)],bubbles:true})); return 1;")
            await br.eval(swipe)
            await asyncio.sleep(1)
            self.assertEqual(await br.eval("return location.hash;"), target_list, "swipe right in the chat opens that agent's list")

            # Quick jump on the conversation list: long bar at the bottom, same
            # menu above it; an agent opens its list, and Back goes where ‹ goes.
            await br.goto(f"{self.base}/mobile/#/agents", wait=3)
            await br.eval("location.hash = '#/agents/spice/conversations'; return 1;")
            await asyncio.sleep(2)
            bar = await br.eval("const r=document.querySelector('.list-footer #switch-btn').getBoundingClientRect(); return {x:r.x+r.width/2, y:r.y+r.height/2, w:r.width, bottom:r.bottom};")
            self.assertGreater(bar["w"], 300, "Quick jump spans the list footer")
            self.assertGreaterEqual(await br.eval("return document.querySelector('.list-footer #switch-btn').getBoundingClientRect().height;"), 48, "and is big there too")
            self.assertGreater(bar["bottom"], 844 - 80, "at the bottom of the screen")
            await br.click(bar["x"], bar["y"])
            await asyncio.sleep(0.5)
            listed = await br.eval("return [...document.querySelectorAll('.quick-switch-item')].map(i=>{const r=i.getBoundingClientRect(); return {id:i.dataset.agent,x:r.x+r.width/2,y:r.y+r.height/2,bottom:r.bottom,right:r.right};});")
            self.assertGreater(len(listed), 3)
            self.assertNotIn("spice", [i["id"] for i in listed], "current agent not listed")
            self.assertTrue(all(i["bottom"] <= bar["y"] for i in listed) and all(i["right"] > 390 * 0.6 for i in listed), "menu above the bar, on the right")
            await br.click(listed[0]["x"], listed[0]["y"])
            await asyncio.sleep(1.5)
            self.assertEqual(await br.eval("return location.hash;"), f"#/agents/{listed[0]['id']}/conversations", "opens that agent's conversation list")
            await br.eval("history.back(); return 1;")
            await asyncio.sleep(1)
            self.assertEqual(await br.eval("return location.hash;"), "#/agents", "Back goes to the agent list, like ‹")
            # All agents under Quick jump on the list too: back to all agents.
            await br.eval("location.hash = '#/agents/spice/conversations'; return 1;")
            await asyncio.sleep(2)
            pos = await br.eval("const q=document.querySelector('.list-footer #switch-btn').getBoundingClientRect(), h=document.querySelector('.list-footer [data-home]').getBoundingClientRect(); return {q:q.bottom, h:h.top, w:h.width};")
            self.assertGreaterEqual(pos["h"], pos["q"], "All agents under Quick jump on the list")
            await br.eval("document.querySelector('.list-footer [data-home]').click(); return 1;")
            await asyncio.sleep(1.5)
            self.assertEqual(await br.eval("return location.hash;"), "#/agents", "All agents opens the agent list")

            # The yellow "new" light: Stash's reply (sent at the start) shows on
            # the agent list, in Quick jump and on that conversation, until it's opened.
            await br.goto(f"{self.base}/mobile/#/agents", wait=3)
            stash_new = "return !!document.querySelector('.row[data-agent=stash] .new-tag');"
            self.assertTrue(await br.eval(stash_new), "agent list shows Stash's new reply")
            await br.eval("location.hash = '#/agents/spice/new'; return 1;")
            await asyncio.sleep(2.5)
            qj = await br.eval("const r=document.querySelector('#switch-btn').getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2};")
            await br.click(qj["x"], qj["y"]); await asyncio.sleep(0.5)
            self.assertTrue(await br.eval("return !!document.querySelector('.quick-switch-item[data-agent=stash] .new-tag');"), "so does Quick jump")
            await br.click(qj["x"], qj["y"]); await asyncio.sleep(0.3)
            await br.eval("location.hash = '#/agents/stash/conversations'; return 1;")
            await asyncio.sleep(2)
            lit = await br.eval("return [...document.querySelectorAll('.conversation-item')].filter(c=>c.querySelector('.new-tag')).map(c=>c.querySelector('[data-conversation]').dataset.conversation);")
            self.assertEqual(len(lit), 1, "and the conversation it's in")
            # A long title shrinks with "…"; the tag stays on screen (2026-10-13).
            await br.eval(f"return fetch('/stash/conversations/{lit[0]}',{{method:'PATCH',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{title:'A very long conversation title that runs far past the edge of a phone screen'}})}}).then(r=>r.status);")
            await br.eval("location.reload(); return 1;")
            await asyncio.sleep(3)
            fit = await br.eval(f"const row=document.querySelector('[data-conversation=\"{lit[0]}\"] .row-title'), tag=row.querySelector('.new-tag'), t=row.querySelector('.title-text'); const r=row.getBoundingClientRect(), g=tag.getBoundingClientRect(); return {{inside: g.left >= r.left && g.right <= r.right + 0.5, w: g.width, cut: t.scrollWidth > t.clientWidth}};")
            self.assertTrue(fit["inside"] and fit["w"] > 20, f"tag visible on a long title: {fit}")
            self.assertTrue(fit["cut"], "the title is what gets cut")
            await br.eval(f"document.querySelector('[data-conversation=\"{lit[0]}\"]').click(); return 1;")
            await asyncio.sleep(2.5)
            await br.eval("history.back(); return 1;")
            await asyncio.sleep(2)
            self.assertEqual(await br.eval("return document.querySelectorAll('.conversation-item .new-tag').length;"), 0, "opened: the light is gone from the list")
            await br.eval("location.hash = '#/agents'; return 1;")
            await asyncio.sleep(2)
            self.assertFalse(await br.eval(stash_new), "and from the agent list")

            # Control Panel from the phone: button top left of the overview,
            # phone layout with nothing scrolling sideways, back to the app.
            await br.goto(f"{self.base}/mobile/#/agents", wait=3)
            self.assertEqual(await br.eval("return document.querySelector('.topbar .control-btn')?.getAttribute('href');"), "/control/?from=mobile")
            await br.eval("document.querySelector('.topbar .control-btn').click(); return 1;")
            await asyncio.sleep(5)
            self.assertEqual(await br.eval("return location.pathname;"), "/control/")
            wide = "return [document.documentElement.scrollWidth, ...[...document.querySelectorAll('section')].filter(s=>s.scrollWidth>s.clientWidth+2).map(s=>s.querySelector('h2')?.textContent)];"
            self.assertEqual(await br.eval(wide), [390], "Control Panel fits the phone width")
            await br.eval("[...document.querySelectorAll('#agents button, #agents .link')].find(b=>b.textContent.startsWith('Edit'))?.click(); return 1;")
            await asyncio.sleep(1.5)
            self.assertTrue(await br.eval("return !!document.querySelector('#agents .edit-row');"), "Edit panel opens")
            self.assertEqual(await br.eval(wide), [390], "still fits with the Edit panel open")
            self.assertEqual(await br.eval("return document.getElementById('back-link').getAttribute('href');"), "/mobile/#/agents")

            # New agent from the phone: form, then its first chat with a ready draft.
            await br.goto(f"{self.base}/mobile/#/agents", wait=3)
            await br.eval("document.querySelector('#add-agent').click(); return 1;")
            await asyncio.sleep(1.5)
            self.assertEqual(await br.eval("return location.hash;"), "#/create-agent")
            self.assertGreater(await br.eval("return document.querySelectorAll('.create-agent select option').length;"), 3, "model choices")
            await br.eval("const f=document.querySelector('#create-agent'); f.name.value='Testy'; f.purpose.value='UI smoke test agent'; f.requestSubmit(); return 1;")
            await asyncio.sleep(2.5)
            hash_ = await br.eval("return location.hash;")
            self.assertRegex(hash_, r"^#/agents/testy/conversations/[\w-]+$", "lands in the agent's own Onboarding chat")
            self.assertIn("a few quick questions", await br.eval("return document.querySelector('.messages').innerText;"), "its first question is visible")
            self.assertEqual(await br.eval("return document.querySelector('.composer textarea').value;"), "", "no extra draft")
            await br.eval("const t=document.querySelector('.composer textarea'); t.value='hiking'; t.dispatchEvent(new Event('input')); document.querySelector('.composer .send').click(); return 1;")
            await asyncio.sleep(4)
            self.assertEqual(await br.eval("return location.hash;"), hash_, "the reply stays in that chat")
            self.assertEqual(await br.eval("return fetch('/testy/conversations').then(r=>r.json()).then(l=>l.length);"), 1, "exactly one conversation")
            await br.eval("history.back(); return 1;")
            await asyncio.sleep(1)
            self.assertEqual(await br.eval("return location.hash;"), "#/agents/testy/conversations", "Back opens its conversation list")

            await br.goto(f"{self.base}/mobile/#/agents/spice/new", wait=4)
            await br.eval("const t=document.querySelector('.composer textarea'); t.value='hi'; t.dispatchEvent(new Event('input')); document.querySelector('.composer .send').click(); return 1;")
            await asyncio.sleep(5)
            meta = await br.eval("return [...document.querySelectorAll('.reply-meta')].map(e=>e.textContent);")
            self.assertTrue(meta and "·" in meta[-1], f"reply shows model · time, got {meta}")

            # Autojump: switched on at the top of the Quick jump menu; after
            # sending here and going idle, another agent's reply opens.
            qj = await br.eval("const r=document.querySelector('#switch-btn').getBoundingClientRect(); return {x:r.x+r.width/2, y:r.y+r.height/2};")
            await br.click(qj["x"], qj["y"])
            await asyncio.sleep(0.5)
            await br.eval("document.querySelector('[data-autojump]').click(); return 1;")
            self.assertEqual(await br.eval("return document.querySelector('[data-autojump] .toggle-state').textContent;"), "On")
            await br.click(20, 120)  # tap outside closes the menu
            await asyncio.sleep(5)  # its first poll sets the starting point: only replies after it count
            await br.eval("fetch('/stash/chat-requests',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request_id:'aj-smoke-m',conversation_id:null,message:'hi',attachments:[]})}); return 1;")
            for _ in range(25):
                await asyncio.sleep(1)
                if (await br.eval("return location.hash;")).startswith("#/agents/stash/conversations/"):
                    break
            self.assertTrue((await br.eval("return location.hash;")).startswith("#/agents/stash/conversations/"), "Autojump opened Stash's reply")
            await br.eval("history.back(); return 1;")
            await asyncio.sleep(1)
            self.assertEqual(await br.eval("return location.hash;"), "#/agents/stash/conversations", "Back from it goes to Stash's list")
            # The feature button opens the agent's page; with none yet, it offers to create one.
            await br.eval("document.querySelector('#feature-btn').click(); return 1;")
            await asyncio.sleep(1)
            self.assertEqual(await br.eval("return location.hash;"), "#/agents/stash/console")
            self.assertEqual(await br.eval("return document.querySelector('#feature-btn').getAttribute('aria-pressed');"), "true", "lit on the page")
            page = await br.eval("return document.querySelector('#console-body').textContent;")
            self.assertIn("Nothing here.", page)
            self.assertIn("Create new custom page?", page)
            await br.eval("document.querySelector('#feature-btn').click(); return 1;")
            await asyncio.sleep(1)
            self.assertEqual(await br.eval("return location.hash;"), "#/agents/stash/conversations", "pressed again: back to the chats")
            # An agent-built page (page.json): cards per row, search across sections.
            spice = self.tmp / ".openclave" / "agents" / "Spice"
            (spice / "pantry.md").write_text("## Spices\n\n| Item | Qty |\n|---|---|\n| Saffron | 1 |\n| Cumin | 2 |\n\n## Oils\n\n| Item | Qty |\n|---|---|\n| Olive oil | 1 |\n")
            (spice / "page.json").write_text('{"title": "Pantry", "blocks": [{"type": "table", "file": "pantry.md", "search": true}]}')
            await br.goto(f"{self.base}/mobile/#/agents/spice/console", wait=3)
            self.assertEqual(await br.eval("return document.querySelector('.custom-page h2')?.textContent;"), "Pantry")
            self.assertEqual(await br.eval("return document.querySelectorAll('.page-card').length;"), 3)
            await br.eval("const i=document.querySelector('.page-search'); i.value='olive'; i.dispatchEvent(new Event('input')); return 1;")
            visible = "return [...document.querySelectorAll('.page-card')].filter(c=>!c.hidden && !c.closest('.page-section').hidden).map(c=>c.querySelector('.page-card-title').textContent);"
            self.assertEqual(await br.eval(visible), ["Olive oil"], "search finds items across sections")
            wide = await br.eval("return document.documentElement.scrollWidth;")
            self.assertLessEqual(wide, 390, "nothing scrolls sideways")
            self.assertEqual([l for l in br.logs if "EXC" in l], [], "no page errors")
        finally:
            await br.stop()

    async def _desktop(self):
        br = Browser()
        await br.start(1400, 900)
        try:
            await br.goto(f"{self.base}/rick", wait=6)
            _, kids = await br.frames()
            self.assertTrue(kids, "agent page iframe present")
            f = kids[0]
            ok = await br.eval("return !!document.querySelector('.composer textarea') && !!document.querySelector('.mic-btn') && !!document.querySelector('.lang-btn');", f)
            self.assertTrue(ok, "shared composer rendered in the agent page")

            tabs = await br.eval("return [...document.querySelectorAll('#agent-tabs .agent-tab')].map(a=>a.textContent.trim());", f)
            self.assertGreater(len(tabs), 3, "agent tabs in the header")
            self.assertEqual(await br.eval("return [...document.querySelectorAll('#agent-tabs .agent-tab.current')].map(a=>a.textContent.trim());", f), ["Rick"], "the current agent is a tab too, highlighted")

            # Drag the first tab past the second: the new order is saved and survives a reload.
            drag = ("const t=[...document.querySelectorAll('#agent-tabs .agent-tab')], a=t[0], b=t[1], r=b.getBoundingClientRect(), dt=new DataTransfer();"
                    "a.dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer:dt}));"
                    "b.dispatchEvent(new DragEvent('dragover',{bubbles:true,cancelable:true,dataTransfer:dt,clientX:r.right-2,clientY:r.top+5}));"
                    "a.dispatchEvent(new DragEvent('dragend',{bubbles:true,dataTransfer:dt})); return 1;")
            await br.eval(drag, f)
            await asyncio.sleep(1)
            expected = [tabs[1], tabs[0]] + tabs[2:]
            await br.goto(f"{self.base}/rick", wait=6)
            _, kids = await br.frames()
            f = kids[0]
            reordered = await br.eval("return [...document.querySelectorAll('#agent-tabs .agent-tab')].map(a=>a.textContent.trim());", f)
            self.assertEqual(reordered, expected, "dragged tab order persists")

            # View menu: left of the model picker; unticking panes hides them without leaving gaps.
            pos = await br.eval("const v=document.querySelector('#view-menu').getBoundingClientRect(), m=document.querySelector('.header-right .mp-button').getBoundingClientRect(); return {v:v.right, m:m.left};", f)
            self.assertLessEqual(pos["v"], pos["m"], "View is left of the model picker")
            await asyncio.sleep(0.5)
            bell = await br.eval("const b=document.querySelector('#notify-btn'), r=b.getBoundingClientRect(), v=document.querySelector('#view-menu').getBoundingClientRect(); return {hidden:b.hidden, right:r.right, w:r.width, v:v.left};", f)
            self.assertFalse(bell["hidden"], "bell shows once the shell reports its state")
            self.assertLessEqual(bell["right"], bell["v"], "bell is left of View")
            self.assertLess(bell["w"], 50, "bell is icon only")
            self.assertEqual(await br.eval("return document.querySelectorAll('body > .notify-btn').length;"), 0, "no floating bell over the agent page")
            labels = await br.eval("return [...document.querySelectorAll('.view-item:has(input[data-pane])')].map(l=>l.textContent.trim());", f)
            self.assertEqual(labels, ["Chat", "Current Plan", "Backlog", "Kanban"], "no option to hide the chat itself")
            toggle = "const b=document.querySelector('.view-item input[data-pane=%s]'); b.checked=!b.checked; b.dispatchEvent(new Event('change',{bubbles:true})); return 1;"
            vis = "return getComputedStyle(document.querySelector('%s')).display !== 'none';"
            for pane in ("conversations", "plan", "backlog", "kanban"):
                await br.eval(toggle % pane, f)
            for sel in (".col-left", ".col-right", ".kanban-panel"):
                self.assertFalse(await br.eval(vis % sel, f), f"{sel} hidden")
            mid = await br.eval("const r=document.querySelector('.col-mid').getBoundingClientRect(); return {l:r.left, r:r.right, b:r.bottom};", f)
            self.assertLess(mid["l"], 30, "chat fills the space on the left")
            self.assertGreater(mid["r"], 1400 - 30, "chat fills the space on the right")
            self.assertGreater(mid["b"], 900 - 30, "chat fills the space where the kanban was")
            for pane in ("conversations", "plan", "backlog", "kanban"):
                await br.eval(toggle % pane, f)
            self.assertTrue(await br.eval(vis % ".col-left", f) and await br.eval(vis % ".kanban-panel", f), "panes come back")
            self.assertEqual(await br.eval("return document.querySelector('.layout').style.gridTemplateColumns;", f), "", "all on = default layout")
            await br.eval("const t=document.querySelector('.composer textarea'); t.value='hi desktop'; t.dispatchEvent(new Event('input')); document.querySelector('.composer .send').click(); return 1;", f)
            await asyncio.sleep(5)
            text = await br.eval("return document.getElementById('chat-window').innerText;", f)
            self.assertIn("Fake reply from rick.", text)

            # Move the conversation: the agent tabs light up and a click on one picks it (no typing).
            await br.eval("window.confirm = () => true; return 1;")  # the shell's confirm, top frame
            await asyncio.sleep(1)
            await br.eval("document.querySelector('.convo-item .convo-handover').click(); return 1;", f)
            await asyncio.sleep(1)
            self.assertTrue(await br.eval("return document.body.classList.contains('picking-agent') && !!document.querySelector('.pick-agent-hint');", f), "tabs highlighted to pick a target")
            await br.eval("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'})); return 1;", f)
            self.assertFalse(await br.eval("return document.body.classList.contains('picking-agent');", f), "Esc cancels")
            before = await br.eval("return document.querySelectorAll('.convo-item').length;", f)
            await br.eval("document.querySelector('.convo-item .convo-handover').click(); return 1;", f)
            await asyncio.sleep(1)
            await br.eval("document.querySelector('#agent-tabs .agent-tab:not(.current)').click(); return 1;", f)
            await asyncio.sleep(1.5)
            # The tab picked the agent; the file dialog opens with it chosen.
            self.assertTrue(await br.eval("return !!document.querySelector('.handover-target').value;", f), "target preselected")
            await br.eval("document.querySelector('.handover-go').click(); return 1;", f)
            await asyncio.sleep(2)
            await br.eval("document.querySelector('.handover-cancel').click(); return 1;", f)  # Close
            self.assertEqual(await br.eval("return location.href;", f), "about:srcdoc", "picking a tab doesn't navigate")
            after = await br.eval("return document.querySelectorAll('.convo-item').length;", f)
            self.assertEqual(after, before - 1, "conversation moved to the clicked agent")

            # Autojump (View menu): after sending here and going idle, another
            # agent's finished reply opens that agent's page.
            await br.eval("const b=document.querySelector('.view-item input[data-autojump]'); b.checked=true; b.dispatchEvent(new Event('change',{bubbles:true})); return 1;", f)
            await asyncio.sleep(1)
            await br.eval("const t=document.querySelector('.composer textarea'); t.value='waiting here'; t.dispatchEvent(new Event('input')); document.querySelector('.composer .send').click(); return 1;", f)
            await asyncio.sleep(1)
            await br.eval("fetch('/spice/chat-requests',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request_id:'aj-smoke-1',conversation_id:null,message:'hi',attachments:[]})}); return 1;")
            for _ in range(25):
                await asyncio.sleep(1)
                if (await br.eval("return location.pathname;")) == "/spice":
                    break
            self.assertEqual(await br.eval("return location.pathname;"), "/spice", "Autojump opened the agent whose reply finished")
            self.assertEqual([l for l in br.logs if "EXC" in l], [], "no page errors")
        finally:
            await br.stop()

    async def _dashboard(self):
        br = Browser()
        await br.start(1400, 900)
        try:
            await br.goto(f"{self.base}/", wait=3)
            # Conversations menu right of each tile's name on the main dashboard
            # (2026-10-05): switch the tile's chat, mouse and keyboard, drafts kept.
            for rid, text in (("convo-a", "alpha message"), ("convo-b", "bravo message")):
                await br.eval("return fetch('/rick/chat-requests',{method:'POST',headers:{'Content-Type':'application/json'},"
                              f"body:JSON.stringify({{request_id:'{rid}',conversation_id:null,message:'{text}',attachments:[]}})}}).then(r=>r.status);")
                await asyncio.sleep(3)
            await br.goto(f"{self.base}/", wait=5)
            pos = await br.eval("const t=document.querySelector('.tile[data-agent-id=rick]'), n=t.querySelector('.tile-name').getBoundingClientRect(), b=t.querySelector('.convo-menu-btn').getBoundingClientRect(); return {n:n.right, bl:b.left, nt:n.top, nb:n.bottom, bt:b.top, tr:t.getBoundingClientRect().right, br:b.right, inLink:!!t.querySelector('.convo-menu-btn').closest('a')};")
            self.assertLessEqual(pos["n"], pos["bl"], "Conversations is right of the agent name")
            self.assertLess(pos["bt"], pos["nb"], "on the same row as the name")
            self.assertLessEqual(pos["br"], pos["tr"], "inside the tile")
            self.assertFalse(pos["inLink"], "not inside the link to the agent page")
            self.assertEqual(await br.eval("return document.querySelectorAll('header .convo-menu-btn').length;"), 0)
            picks = "return [...document.querySelector('.tile[data-agent-id=rick]').querySelectorAll('.convo-pick')].map(b=>({t:b.querySelector('.t').textContent, cur:b.classList.contains('current')}));"
            await br.eval("document.querySelector('.tile[data-agent-id=rick]').querySelector('.convo-menu-btn').click(); return 1;")
            await asyncio.sleep(1)
            listed = await br.eval(picks)
            self.assertEqual(len(listed), 2, "lists this agent's conversations")
            transcript = "return document.querySelector('.tile[data-agent-id=rick]').querySelector('.tile-chat-msgs').textContent;"
            await br.eval("[...document.querySelector('.tile[data-agent-id=rick]').querySelectorAll('.convo-pick')].find(b=>b.textContent.includes('alpha')).click(); return 1;")
            await asyncio.sleep(1.5)
            self.assertTrue(await br.eval("return document.querySelector('.tile[data-agent-id=rick]').querySelector('.convo-pop').hidden;"), "picking closes the menu")
            self.assertIn("alpha message", await br.eval(transcript))
            await br.eval("const t=document.querySelector('.tile[data-agent-id=rick]').querySelector('.tile-chat-input textarea'); t.value='unsent alpha draft'; t.dispatchEvent(new Event('input',{bubbles:true})); return 1;")
            # Keyboard: open with the button focused + ArrowDown, move, Enter picks.
            key = lambda k, code, vk: br.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": k, "code": code, "windowsVirtualKeyCode": vk, **({"text": "\r"} if k == "Enter" else {})})
            keyup = lambda k, code, vk: br.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": k, "code": code, "windowsVirtualKeyCode": vk})
            await br.eval("document.querySelector('.tile[data-agent-id=rick]').querySelector('.convo-menu-btn').focus(); return 1;")
            await key("ArrowDown", "ArrowDown", 40); await keyup("ArrowDown", "ArrowDown", 40)
            await asyncio.sleep(1)
            self.assertTrue(await br.eval("return document.activeElement.classList.contains('current') && document.activeElement.textContent.includes('alpha');"), "focus starts on the open chat")
            self.assertIn("draft", await br.eval("return document.querySelector('.tile[data-agent-id=rick]').querySelector('.convo-pick.current .draft-tag')?.textContent || '';"), "draft tag shows")
            await key("ArrowDown", "ArrowDown", 40); await keyup("ArrowDown", "ArrowDown", 40)
            await key("Enter", "Enter", 13); await keyup("Enter", "Enter", 13)
            await asyncio.sleep(1.5)
            self.assertIn("bravo message", await br.eval(transcript), "Enter switched to the other conversation")
            # Escape closes; back to alpha by mouse: its draft is still there.
            await br.eval("document.querySelector('.tile[data-agent-id=rick]').querySelector('.convo-menu-btn').click(); return 1;")
            await asyncio.sleep(1)
            await key("Escape", "Escape", 27); await keyup("Escape", "Escape", 27)
            self.assertTrue(await br.eval("return document.querySelector('.tile[data-agent-id=rick]').querySelector('.convo-pop').hidden;"), "Escape closes the menu")
            await br.eval("document.querySelector('.tile[data-agent-id=rick]').querySelector('.convo-menu-btn').click(); return 1;")
            await asyncio.sleep(1)
            await br.eval("[...document.querySelector('.tile[data-agent-id=rick]').querySelectorAll('.convo-pick')].find(b=>b.textContent.includes('alpha')).click(); return 1;")
            await asyncio.sleep(1.5)
            self.assertEqual(await br.eval("return document.querySelector('.tile[data-agent-id=rick]').querySelector('.tile-chat-input textarea').value;"), "unsent alpha draft", "draft preserved")
            await br.eval("document.querySelector('.tile[data-agent-id=rick]').querySelector('.convo-menu-btn').click(); return 1;")
            await asyncio.sleep(1)
            await br.eval("document.querySelector('header').dispatchEvent(new MouseEvent('mousedown',{bubbles:true})); return 1;")
            self.assertTrue(await br.eval("return document.querySelector('.tile[data-agent-id=rick]').querySelector('.convo-pop').hidden;"), "a click outside closes it")
            self.assertEqual([l for l in br.logs if "EXC" in l], [], "no page errors")
        finally:
            await br.stop()

    # Partial copy (2026-10-12): selecting part of a message and copying
    # it must work; the message menu must not steal the selection.
    SELECT_JS = ("const b=[...document.querySelectorAll('%s')].find(e=>e.textContent.includes(%r)); const w=document.createTreeWalker(b,NodeFilter.SHOW_TEXT);"
                 "let n; while((n=w.nextNode()) && !n.data.includes(%r)); const i=n.data.indexOf(%r), r=document.createRange(); r.setStart(n,i); r.setEnd(n,i+%d);"
                 "getSelection().removeAllRanges(); getSelection().addRange(r); const g=r.getBoundingClientRect(); return {x:g.x+3, y:g.y+g.height/2};")

    async def _right_click(self, br, x, y):
        await br.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        await br.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "right", "clickCount": 1})
        await br.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "right", "clickCount": 1})
        await asyncio.sleep(0.4)

    async def _check_mouse_copy(self, br, bubbles, frame=None, dx=0, dy=0):
        """Drag-select, right-click with and without a selection, in a user and an assistant bubble."""
        for word, length in (("partial", 7), ("Fake reply", 4)):
            # Mouse drag across text: a real partial selection.
            pt = await br.eval(self.SELECT_JS % (bubbles, word, word, word, length), frame)
            x0, y = pt["x"] + dx, pt["y"] + dy
            await br.eval("getSelection().removeAllRanges(); return 1;", frame)
            await br.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x0, "y": y})
            await br.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x0, "y": y, "button": "left", "buttons": 1, "clickCount": 1})
            for step in range(1, 6):
                await br.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x0 + 8 * step, "y": y, "button": "left", "buttons": 1})
            await br.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x0 + 40, "y": y, "button": "left", "clickCount": 1})
            picked = await br.eval("return getSelection().toString();", frame)
            self.assertTrue(picked and len(picked) < 12, f"{word}: mouse drag selects part of a message, got {picked!r}")
        for word, length in (("partial", 7), ("Fake reply", 4)):
            # Right-click on a selection: the browser's own menu (Copy), selection kept.
            sel = self.SELECT_JS % (bubbles, word, word, word, length)
            pt = await br.eval(sel, frame)
            await self._right_click(br, pt["x"] + dx, pt["y"] + dy)
            self.assertEqual(await br.eval("return document.querySelectorAll('.msg-menu').length;", frame), 0,
                             f"{word}: right-click on a selection leaves the browser's menu (Copy) alone")
            self.assertEqual(await br.eval("return getSelection().toString();", frame), word[:length], f"{word}: selection kept")
            # No selection: our menu with Copy / Send to another agent.
            await br.eval("getSelection().removeAllRanges(); return 1;", frame)
            await self._right_click(br, pt["x"] + dx, pt["y"] + dy)
            self.assertEqual(await br.eval("return [...document.querySelectorAll('.msg-menu button')].map(b=>b.textContent)[0];", frame), "Copy",
                             f"{word}: no selection: the message menu with Copy")

    async def _partial_copy(self):
        msg = "please partial copy test"
        post = ("return fetch('/%s/chat-requests',{method:'POST',headers:{'Content-Type':'application/json'},"
                "body:JSON.stringify({request_id:'%s',conversation_id:null,message:'" + msg + "',attachments:[]})}).then(r=>r.status);")
        br = Browser()
        await br.start(1400, 900)
        try:
            await br.goto(f"{self.base}/", wait=2)
            await br.eval(post % ("fresh", "pc-fresh"))
            await asyncio.sleep(4)
            # Desktop agent page (sandboxed iframe).
            await br.goto(f"{self.base}/fresh", wait=6)
            _, kids = await br.frames()
            f = kids[0]
            await br.eval("[...document.querySelectorAll('.convo-item')].find(c=>c.textContent.includes('partial'))?.click(); return 1;", f)
            await asyncio.sleep(2)
            off = await br.eval("const r=document.querySelector('iframe').getBoundingClientRect(); return {x:r.x, y:r.y};")
            await self._check_mouse_copy(br, "#chat-window .bubble", f, off["x"], off["y"])
            self.assertEqual([l for l in br.logs if "EXC" in l], [], "no page errors")
        finally:
            await br.stop()
        br = Browser()  # fresh browser: the agent page's mouse state doesn't carry over
        await br.start(1400, 900)
        try:
            # Dashboard tile chat.
            await br.goto(f"{self.base}/", wait=4)
            tile = ".tile[data-agent-id=fresh]"
            await br.eval(f"document.querySelector('{tile} .convo-menu-btn').click(); return 1;")
            await asyncio.sleep(1)
            await br.eval(f"[...document.querySelectorAll('{tile} .convo-pick')].find(b=>b.textContent.includes('partial')).click(); return 1;")
            await asyncio.sleep(1.5)
            await self._check_mouse_copy(br, f"{tile} .bubble")
            self.assertEqual([l for l in br.logs if "EXC" in l], [], "no page errors")
        finally:
            await br.stop()

        br = Browser()
        await br.start(390, 844)
        try:
            await br.send("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
            await br.goto(f"{self.base}/mobile/#/agents/fresh/conversations", wait=3)
            await br.eval("[...document.querySelectorAll('[data-conversation]')].find(c=>c.textContent.includes('partial')).click(); return 1;")
            await asyncio.sleep(2.5)
            style = await br.eval("const s=getComputedStyle(document.querySelector('.messages .bubble')); return [s.userSelect || s.webkitUserSelect, s.webkitTouchCallout];")
            self.assertEqual(style[0], "text", "message text is selectable on the phone")
            self.assertNotEqual(style[1], "none", "the phone's own Copy callout is allowed")
            await br.eval("window.__copied=[]; navigator.clipboard.writeText = t => { window.__copied.push(t); return Promise.resolve(); }; return 1;")
            for word, length in (("partial", 7), ("Fake reply", 4)):
                pt = await br.eval(self.SELECT_JS % (".messages .bubble", word, word, word, length))
                bub = await br.eval(f"const r=[...document.querySelectorAll('.messages .bubble')].find(e=>e.textContent.includes({word!r})).getBoundingClientRect(); return {{top:r.top, bottom:r.bottom}};")
                # Long-press with the finger on the selected text.
                tp = [{"x": pt["x"], "y": pt["y"]}]
                await br.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": tp})
                await asyncio.sleep(0.8)
                await br.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
                await asyncio.sleep(0.3)
                menu = await br.eval("const m=document.querySelector('.msg-menu'); if(!m) return null; const r=m.getBoundingClientRect(); return {top:r.top, bottom:r.bottom, items:[...m.querySelectorAll('button')].map(b=>b.textContent)};")
                self.assertIsNotNone(menu, f"{word}: long-press still opens the message menu")
                self.assertTrue(menu["top"] >= bub["bottom"] - 1 or menu["bottom"] <= bub["top"] + 1, f"{word}: the menu doesn't cover the message")
                self.assertEqual(menu["items"][:2], ["Copy selection", "Copy whole message"], word)
                self.assertEqual(await br.eval("return getSelection().toString();"), word[:length], f"{word}: long-press kept the selection")
                await br.eval("document.querySelector('.msg-menu [data-act=copy-sel]').click(); return 1;")
                await asyncio.sleep(0.3)
                self.assertEqual(await br.eval("return window.__copied.at(-1);"), word[:length], f"{word}: Copy selection copies just that part")
                await asyncio.sleep(0.6)
            # Whole-message Copy still there; dragging selection handles is not a swipe back.
            await br.eval("getSelection().removeAllRanges(); return 1;")
            pt = await br.eval(self.SELECT_JS % (".messages .bubble", "partial", "partial", "partial", 7))
            await br.eval("getSelection().removeAllRanges(); return 1;")
            await br.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": pt["x"], "y": pt["y"]}]})
            await asyncio.sleep(0.8)
            await br.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
            await asyncio.sleep(0.3)
            await br.eval("document.querySelector('.msg-menu [data-act=copy]').click(); return 1;")
            await asyncio.sleep(0.3)
            self.assertEqual(await br.eval("return window.__copied.at(-1);"), msg, "Copy copies the whole message")
            here = await br.eval("return location.hash;")
            await br.eval(self.SELECT_JS % (".messages .bubble", "partial", "partial", "partial", 7))
            swipe = ("const a=document.querySelector('#chat'); const t=(x)=>new Touch({identifier:1,target:a,clientX:x,clientY:400});"
                     "a.dispatchEvent(new TouchEvent('touchstart',{touches:[t(40)],changedTouches:[t(40)],bubbles:true}));"
                     "a.dispatchEvent(new TouchEvent('touchmove',{touches:[t(120)],changedTouches:[t(120)],bubbles:true}));"
                     "a.dispatchEvent(new TouchEvent('touchend',{touches:[],changedTouches:[t(250)],bubbles:true})); return 1;")
            await br.eval(swipe)
            await asyncio.sleep(1)
            self.assertEqual(await br.eval("return location.hash;"), here, "dragging a selection doesn't swipe back")
            self.assertEqual([l for l in br.logs if "EXC" in l], [], "no page errors")
        finally:
            await br.stop()

    # Cost tiers in every model picker (2026-10-12): a dot in the tier
    # colour (green/amber/violet, never red) plus $/$$/$$$.
    TIER_RGB = {"rgb(34, 197, 94)": "$", "rgb(245, 158, 11)": "$$", "rgb(168, 85, 247)": "$$$"}
    DOT_JS = "const d=%s; return d && d.classList.contains('cost-dot') ? getComputedStyle(d).backgroundColor : null;"

    async def _cost_dots(self):
        br = Browser()
        await br.start(1400, 900)
        try:
            await br.goto(f"{self.base}/control/", wait=4)
            selects = await br.eval("return [...document.querySelectorAll('#agents select, #new-agent-model, #fallback-model')].map(s=>{const d=s.previousElementSibling; return {dot: d && d.classList.contains('cost-dot') ? getComputedStyle(d).backgroundColor : null, unknown: !!d && d.classList.contains('unknown'), off: !s.value, text: s.selectedOptions[0]?.textContent || ''};});")
            self.assertGreater(len(selects), 3)
            for sel in selects:
                if sel["off"]:
                    continue  # fallback "Off"
                self.assertIn(sel["dot"], self.TIER_RGB, sel)
                self.assertIn(f"· {self.TIER_RGB[sel['dot']]}", sel["text"], "option shows the matching $ label")
            options = await br.eval("return [...document.querySelectorAll('#new-agent-model option')].map(o=>o.textContent);")
            self.assertTrue(all(o.endswith(("· $", "· $$", "· $$$")) for o in options), options)
            # The dot follows a new choice.
            await br.eval("const s=document.querySelector('#new-agent-model'); s.value=[...s.options].find(o=>o.textContent.endsWith('· $$$')).value; s.dispatchEvent(new Event('change')); return 1;")
            self.assertEqual(await br.eval(self.DOT_JS % "document.querySelector('#new-agent-model').previousElementSibling"), "rgb(168, 85, 247)")
            # Desktop agent page.
            await br.goto(f"{self.base}/rick", wait=6)
            _, kids = await br.frames()
            f = kids[0]
            dot = await br.eval(self.DOT_JS % "document.getElementById('model-cost-dot')", f)
            self.assertIn(dot, self.TIER_RGB)
            pos = await br.eval("const d=document.getElementById('model-cost-dot').getBoundingClientRect(), s=document.querySelector('.header-right .mp-button').getBoundingClientRect(); return [d.right, s.left, d.width];", f)
            self.assertLessEqual(pos[0], pos[1], "dot left of the picker")
            self.assertGreater(pos[2], 5, "dot visible")
            self.assertIn(f"· {self.TIER_RGB[dot]}", await br.eval("return document.getElementById('model-select').selectedOptions[0].textContent;", f))
        finally:
            await br.stop()
        br = Browser()
        await br.start(390, 844)
        try:
            await br.goto(f"{self.base}/mobile/#/agents/spice/model", wait=4)
            # The model table (2026-11-04): API models show $/$$/$$$ in the tier colour, CLI ones "sub".
            rows = await br.eval("return [...document.querySelectorAll('.mp-row')].map(r=>({route: r.querySelector('.mp-col-route').textContent, color: getComputedStyle(r.querySelector('.mp-col-price span')).color, label: r.querySelector('.mp-col-price').textContent}));")
            self.assertGreater(len(rows), 3)
            for row in rows:
                if row["route"] == "API" and row["label"] != "?":
                    self.assertIn(row["color"], self.TIER_RGB, row)
                    self.assertEqual(row["label"], self.TIER_RGB[row["color"]], row)
                elif row["route"] == "CLI":
                    self.assertEqual(row["label"], "sub", row)
            self.assertLessEqual(await br.eval("return document.documentElement.scrollWidth;"), 390, "nothing scrolls sideways")
            await br.goto(f"{self.base}/mobile/#/create-agent", wait=3)
            self.assertIn(await br.eval(self.DOT_JS % "document.querySelector('#create-cost .cost-dot')"), self.TIER_RGB)
            self.assertEqual([l for l in br.logs if "EXC" in l], [], "no page errors")
        finally:
            await br.stop()

    # Stash's page: one text box + Add appends "- [ ] <text>" to his buying
    # list (2026-10-12), no chat round-trip.
    async def _page_form(self):
        stash = self.tmp / ".openclave" / "agents" / "Stash"
        (stash / "buying.md").write_text("# Buying\n\n| Item | Status |\n|---|---|\n| LED strip | Wanted |\n")
        (stash / "page.json").write_text('{"title": "Inventory", "blocks": [{"type": "form", "file": "buying.md", "title": "Add to AliExpress list", "placeholder": "Item"}, {"type": "table", "file": "buying.md"}]}')
        br = Browser()
        await br.start(390, 844)
        try:
            await br.goto(f"{self.base}/mobile/#/agents/stash/console", wait=3)
            box = await br.eval("const i=document.querySelector('.page-form-input'), b=document.querySelector('.page-form-btn'); const r=i.getBoundingClientRect(), q=b.getBoundingClientRect(); return {iw:r.width, by:q.top, iy:r.top, br:q.right, bx:q.x+q.width/2, byc:q.y+q.height/2};")
            self.assertGreater(box["iw"], 200, "a wide text box")
            self.assertLess(abs(box["by"] - box["iy"]), 2, "Add sits beside it")
            self.assertLessEqual(box["br"], 390)
            status = "return document.querySelector('.page-form-status').textContent;"
            await br.click(box["bx"], box["byc"])
            await asyncio.sleep(0.5)
            self.assertIn("Type something", await br.eval(status), "empty: an error, nothing sent")
            await br.eval("const i=document.querySelector('.page-form-input'); i.focus(); return 1;")
            await br.send("Input.insertText", {"text": "USB-C breakout"})
            await br.click(box["bx"], box["byc"])
            await asyncio.sleep(1)
            self.assertEqual(await br.eval(status), "Added ✓")
            self.assertEqual(await br.eval("return document.querySelector('.page-form-input').value;"), "", "box cleared")
            self.assertEqual(await br.eval("return [...document.querySelectorAll('.page-form-items li')].map(l=>l.textContent);"), ["USB-C breakout"])
            self.assertTrue((stash / "buying.md").read_text().endswith("\n\n- [ ] USB-C breakout\n"), "appended to the note")
            await br.goto(f"{self.base}/mobile/#/agents/stash/console", wait=3)
            self.assertEqual(await br.eval("return [...document.querySelectorAll('.page-form-items li')].map(l=>l.textContent);"), ["USB-C breakout"], "still there after a reload")
            self.assertEqual(await br.eval("return document.querySelectorAll('.page-card').length;"), 1, "the table still reads the same file")
            # A server-side refusal shows as an error.
            (stash / "page.json").write_text('{"blocks": [{"type": "form", "file": "mission.md"}]}')
            await br.eval("const i=document.querySelector('.page-form-input'); i.value='x'; document.querySelector('.page-form-row').requestSubmit(); return 1;")
            await asyncio.sleep(1)
            self.assertIn("core file", await br.eval(status), "a forbidden file: the server says why")
            self.assertEqual(await br.eval("return document.documentElement.scrollWidth;"), 390, "nothing scrolls sideways")
            self.assertEqual([l for l in br.logs if "EXC" in l], [], "no page errors")
        finally:
            (stash / "page.json").unlink(missing_ok=True)
            await br.stop()

    # Open instantly (Rick's backlog, 2026-10-05): with the server slow, the
    # agent list, a conversation list and a chat seen before show at once
    # from the phone's copy, then update in place.
    SLOW_FETCH = ("const real = window.fetch; window.fetch = (url, ...rest) => {"
                  " const u = String(url); const slow = localStorage.getItem('__slow') === '1' && (!rest[0] || !rest[0].method || rest[0].method === 'GET')"
                  " && (/\\/api\\/agents$/.test(u) || /\\/conversations(\\/[^/]+)?$/.test(u));"
                  " return slow ? new Promise(r => setTimeout(r, 2500)).then(() => real(url, ...rest)) : real(url, ...rest); };")

    async def _cache_first(self):
        post = ("return fetch('/rocky/chat-requests',{method:'POST',headers:{'Content-Type':'application/json'},"
                "body:JSON.stringify({request_id:'%s',conversation_id:null,message:'%s',attachments:[]})}).then(r=>r.status);")
        br = Browser()
        await br.start(390, 844)
        try:
            await br.send("Page.addScriptToEvaluateOnNewDocument", {"source": self.SLOW_FETCH})
            await br.goto(f"{self.base}/mobile/#/agents", wait=2)
            await br.eval(post % ("cache-1", "cached chat text"))
            await asyncio.sleep(4)
            # Warm the copies: agent list, Rocky's list, the chat.
            await br.goto(f"{self.base}/mobile/#/agents/rocky/conversations", wait=3)
            convo = await br.eval("return [...document.querySelectorAll('[data-conversation]')].find(b=>b.textContent.includes('cached chat'))?.dataset.conversation;")
            self.assertTrue(convo)
            before = await br.eval("return document.querySelectorAll('.conversation-item').length;")
            await br.eval(f"location.hash = '#/agents/rocky/conversations/{convo}'; return 1;")
            await asyncio.sleep(2.5)
            await br.eval("location.hash = '#/agents'; return 1;")
            await asyncio.sleep(2)
            # A reply in a new chat lands while the app is closed.
            await br.eval(post % ("cache-2", "brand new while away"))
            await asyncio.sleep(4)
            await br.eval("localStorage.setItem('__slow','1'); return 1;")

            await br.goto(f"{self.base}/mobile/#/agents/rocky/conversations", wait=0.8)
            text = await br.eval("return document.querySelector('#app').innerText;")
            self.assertNotIn("Loading", text, "no loading state")
            self.assertEqual(await br.eval("return document.querySelectorAll('.conversation-item').length;"), before, "last time's list at once")
            self.assertEqual(await br.eval("return document.querySelectorAll('.conversation-item .new-tag').length;"), 0, "no 'new' from the copy")
            await asyncio.sleep(3.5)
            self.assertEqual(await br.eval("return document.querySelectorAll('.conversation-item').length;"), before + 1, "server's list swapped in")
            self.assertIn("brand new while away", await br.eval("return document.querySelector('#convo-list').innerText;"))
            self.assertEqual(await br.eval("return document.querySelectorAll('.conversation-item .new-tag').length;"), 1, "its 'new' comes from the server")

            await br.goto(f"{self.base}/mobile/#/agents/rocky/conversations/{convo}", wait=0.8)
            msgs = await br.eval("return document.querySelector('.messages').innerText;")
            self.assertIn("cached chat text", msgs, "the chat at once")
            self.assertNotIn("Loading conversation", msgs)
            await asyncio.sleep(3.5)
            self.assertIn("Fake reply", await br.eval("return document.querySelector('.messages').innerText;"))

            await br.goto(f"{self.base}/mobile/#/agents", wait=0.8)
            self.assertGreater(await br.eval("return document.querySelectorAll('.row[data-agent]').length;"), 3, "agent list at once")
            await asyncio.sleep(3.5)
            self.assertTrue(await br.eval("return !!document.querySelector('.row[data-agent=rocky] .new-tag');"), "then the server's 'new msg'")
            self.assertEqual([l for l in br.logs if "EXC" in l], [], "no page errors")
        finally:
            await br.eval("localStorage.removeItem('__slow'); return 1;")
            await br.stop()

    # Streaming (Rick's backlog, 2026-10-13): on the API routes the pending
    # bubble shows the reply as it's written, then the finished reply.
    async def _streaming(self):
        br = Browser()
        await br.start(390, 844)
        try:
            await br.goto(f"{self.base}/mobile/#/agents/greg/new", wait=3)
            await br.eval("const t=document.querySelector('textarea'); t.value='stream please'; t.dispatchEvent(new Event('input')); return 1;")
            await br.eval("document.querySelector('button.send').click(); return 1;")
            await asyncio.sleep(2.5)
            bubble = "const b=document.querySelector('.bubble.pending'); return b ? [b.className, b.querySelector('.stream-text')?.innerHTML || '', b.innerText] : null;"
            first = await br.eval(bubble)
            self.assertIsNotNone(first, "still working")
            self.assertIn("streaming", first[0])
            self.assertIn("<strong>part one</strong>", first[1], "the text so far, as markdown")
            self.assertNotIn("part three", first[1])
            self.assertRegex(first[2], r"Working|Thinking", "the timer line stays under it")
            self.assertIn("Stop", first[2])
            await asyncio.sleep(1.6)
            self.assertIn("part two", (await br.eval(bubble))[1], "it grows")
            await asyncio.sleep(4)
            self.assertIsNone(await br.eval("return document.querySelector('.bubble.pending') && 1;"), "done")
            final = await br.eval("return [...document.querySelectorAll('.bubble.assistant')].pop().innerText;")
            self.assertIn("part three", final)
            self.assertEqual(await br.eval("return document.documentElement.scrollWidth;"), 390)
            self.assertEqual([l for l in br.logs if "EXC" in l], [], "no page errors")
        finally:
            await br.stop()

    # Weekly cleanup (Rick's backlog, 2026-10-13; moved from the Control Panel
    # to Rick's phone page 2026-10-14): nothing changes until a button is pressed.
    async def _cleanup(self):
        greg = self.tmp / ".openclave" / "agents" / "Greg" / "conversations"
        greg.mkdir(parents=True, exist_ok=True)
        old = time.time() - 40 * 86400
        for cid, title in (("20260901-100000-aaaaaaaa", "Old plant chat"), ("20260902-100000-bbbbbbbb", "Another old one")):
            f = greg / f"{cid}.md"
            f.write_text(f"---\nAgent: Greg\nDate: '2026-09-01'\nTitle: {title}\n---\n\n### User (10:00)\nhello\n\n### Greg (10:00)\nhi\n\n"
                         "### User (10:01)\nmore\n\n### Greg (10:01)\nok\n")
            os.utime(f, (old, old))
        card = "[...document.querySelectorAll('.cleanup-item')].find(r=>r.textContent.includes('%s'))"
        br = Browser()
        await br.start(390, 844)
        try:
            await br.goto(f"{self.base}/mobile/#/agents/rick/console", wait=4)
            self.assertEqual(await br.eval("return document.querySelector('#console-body').firstElementChild.id;"), "cleanup", "cleanup is at the top of Rick's page")
            await br.eval("document.querySelector('#cleanup [data-run-review]').click(); return 1;")
            await asyncio.sleep(3)
            # The run posts in Rick's Cleanup and Audit chats; drop them so
            # tests that count Rick's conversations don't depend on order.
            await asyncio.sleep(3)
            for f in (self.tmp / ".openclave" / "agents" / "Rick" / "conversations").glob("*.md"):
                if f.stem in ("cleanup", "audit"):
                    f.unlink()
            rows = await br.eval("return [...document.querySelectorAll('.cleanup-item')].filter(r=>r.dataset.id.includes(':greg:')).map(r=>r.innerText);")
            self.assertEqual(len(rows), 2, rows)
            self.assertIn("no activity for 40 days", rows[0])
            self.assertEqual(len(list(greg.glob("*.md"))), 2, "nothing changed by checking")
            self.assertEqual(await br.eval("return document.documentElement.scrollWidth;"), 390, "fits a phone")
            buttons = await br.eval(f"const b={card % 'Old plant chat'}.querySelector('[data-apply]').getBoundingClientRect(); return {{h:b.height,w:b.width}};")
            self.assertGreaterEqual(buttons["h"], 40, "thumb-sized buttons")
            # Read the chat in place before deciding (2026-11-05).
            await br.eval(f"{card % 'Old plant chat'}.querySelector('[data-peek]').click(); return 1;")
            await asyncio.sleep(1)
            peek = await br.eval(f"return {card % 'Old plant chat'}.querySelector('.cleanup-transcript').innerText;")
            self.assertIn("hello", peek)
            self.assertIn("Greg", peek)
            # Archive one, Keep the other.
            await br.eval(f"{card % 'Old plant chat'}.querySelector('[data-apply]').click(); return 1;")
            await asyncio.sleep(1.5)
            self.assertFalse((greg / "20260901-100000-aaaaaaaa.md").exists())
            self.assertTrue((greg / "archived" / "20260901-100000-aaaaaaaa.md").exists())
            self.assertIn("Old plant chat", await br.eval("return document.querySelector('.cleanup-archived').textContent;"))
            await br.eval(f"{card % 'Another old one'}.querySelector('[data-keep]').click(); return 1;")
            await asyncio.sleep(1)
            self.assertTrue((greg / "20260902-100000-bbbbbbbb.md").exists(), "Keep leaves it alone")
            await br.eval("location.reload(); return 1;")
            await asyncio.sleep(3)
            self.assertNotIn("Another old one", await br.eval("return document.querySelector('#cleanup').innerText;"), "and it stays kept")
            # Restore.
            await br.eval("document.querySelector('.cleanup-archived').open=true; [...document.querySelectorAll('[data-restore]')].find(b=>b.dataset.restore.includes('aaaaaaaa')).click(); return 1;")
            await asyncio.sleep(1.5)
            self.assertTrue((greg / "20260901-100000-aaaaaaaa.md").exists(), "restored")
            self.assertEqual([l for l in br.logs if "EXC" in l], [], "no page errors")
            # Other agents' pages don't get it; the Control Panel points here.
            await br.goto(f"{self.base}/mobile/#/agents/stash/console", wait=2)
            self.assertFalse(await br.eval("return !!document.querySelector('#cleanup');"))
            await br.goto(f"{self.base}/control/", wait=3)
            self.assertIn("/mobile/#/agents/rick/console", await br.eval("return document.querySelector('#weekly-review a').getAttribute('href');"))
            # Weekly review checklist (2026-11-04): each tick saves at once.
            self.assertGreaterEqual(await br.eval("return document.querySelectorAll('#weekly-review [data-review]').length;"), 9)
            await br.eval("const c=document.querySelector('[data-review=remove_old_models]'); c.click(); return 1;")
            await asyncio.sleep(1)
            review = await br.eval("return fetch('/api/settings').then(r=>r.json()).then(s=>s.weekly_review);")
            self.assertFalse(review["remove_old_models"])
            self.assertTrue(review["add_new_models"])
            await br.eval("document.querySelector('[data-review=remove_old_models]').click(); return 1;")
            await asyncio.sleep(1)
            # The model picker over the selects: a sortable table.
            await br.eval("document.querySelector('#agents .mp-button').click(); return 1;")
            await asyncio.sleep(0.4)
            self.assertEqual(await br.eval("return [...document.querySelectorAll('.mp-pop th')].map(t=>t.textContent);"),
                             ["Vendor", "Model", "Route", "Speed", "Latency", "Price", "Released"])
            await br.eval("[...document.querySelectorAll('.mp-pop th')].find(t=>t.textContent==='Released').click(); return 1;")
            dates = await br.eval("return [...document.querySelectorAll('.mp-pop td.mp-col-released')].map(t=>t.textContent.slice(0,7)).filter(Boolean);")
            self.assertEqual(dates, sorted(dates, reverse=True), "sorted newest first")
            self.assertTrue(all(len(n) <= 16 for n in await br.eval("return [...document.querySelectorAll('.mp-pop .mp-name')].map(t=>t.textContent);")))
            self.assertEqual([l for l in br.logs if "EXC" in l], [], "no Control Panel errors")
        finally:
            for f in greg.rglob("*.md"):
                f.unlink()
            await br.stop()

    # Compact model label (2026-10-12): short name plus average seconds.
    async def _model_label(self):
        times = json.loads(urllib.request.urlopen(f"{self.base}/api/model-response-times").read())
        agents_ = json.loads(urllib.request.urlopen(f"{self.base}/api/agents").read())
        rick = next(a for a in agents_ if a["id"] == "rick")
        br = Browser()
        await br.start(390, 844)
        try:
            await br.goto(f"{self.base}/mobile/#/agents/rick/new", wait=3)
            text = await br.eval("return document.querySelector('.topbar .model-badge').textContent;")
            self.assertNotIn("Claude", text)
            self.assertNotIn("subscription", text)
            secs = times.get(rick["model_id"], {}).get("average_ms")
            if secs:
                self.assertTrue(text.endswith(f" · {round(secs / 1000)}s"), text)
            box = await br.eval("const r=document.querySelector('.topbar .model-badge').getBoundingClientRect(); return {w:r.width, sw:document.documentElement.scrollWidth};")
            self.assertLessEqual(box["w"], 160)
            self.assertEqual(box["sw"], 390)
            await br.eval("document.querySelector('.topbar .model-badge').click(); return 1;")
            await asyncio.sleep(2)
            # The model table (2026-11-04): short names, measured seconds in Latency.
            rows = await br.eval("return [...document.querySelectorAll('.mp-row .mp-name')].map(r=>r.textContent);")
            self.assertTrue(rows and not any(r.startswith("Claude") or "subscription" in r for r in rows), rows)
            latency = await br.eval("return [...document.querySelectorAll('.mp-row td.mp-col-latency')].map(r=>r.textContent);")
            self.assertTrue(any(t.endswith("s") for t in latency), latency)
            self.assertEqual(await br.eval("return document.documentElement.scrollWidth;"), 390, "no sideways scroll")
            self.assertEqual([l for l in br.logs if "EXC" in l], [], "no page errors")
        finally:
            await br.stop()

    # The chat header names the conversation under the agent (Rick's backlog,
    # 2026-10-14); a long name shrinks with "…" and the controls stay put.
    async def _chat_title(self):
        fresh = self.tmp / ".openclave" / "agents" / "Fresh" / "conversations"
        fresh.mkdir(parents=True, exist_ok=True)
        cid, long_title = "20261014-090000-cccccccc", "Sleep, sauna and the very long list of everything else we talked about"
        (fresh / f"{cid}.md").write_text(f"---\nAgent: Fresh\nDate: '2026-10-14'\nTitle: {long_title}\n---\n\n### User (09:00)\nhello\n\n### Fresh (09:00)\nhi\n")
        br = Browser()
        await br.start(390, 844)
        try:
            await br.goto(f"{self.base}/mobile/#/agents/fresh/conversations/{cid}", wait=3)
            box = lambda sel: br.eval(f"const r=document.querySelector({sel!r}).getBoundingClientRect(); return {{x:r.x,y:r.y,w:r.width,h:r.height}};")
            self.assertEqual(await br.eval("return document.querySelector('.topbar .title-sub').textContent;"), long_title)
            self.assertEqual(await br.eval("return document.querySelector('.topbar .title-text').textContent;"), "Fresh", "agent name stays")
            name, sub, badge = await box(".topbar .title-text"), await box(".topbar .title-sub"), await box(".topbar .model-badge")
            self.assertGreater(sub["y"], name["y"], "the conversation is on the line under the agent")
            self.assertTrue(await br.eval("const t=document.querySelector('.topbar .title-sub'); return t.scrollWidth > t.clientWidth;"), "a long name is cut")
            self.assertLessEqual(sub["x"] + sub["w"], badge["x"], "it never runs into the model picker")
            self.assertLess(badge["x"] + badge["w"], 391, "model picker on screen")
            self.assertEqual(await br.eval("return document.documentElement.scrollWidth;"), 390, "fits a phone")
            # Renamed elsewhere: the header follows when the chat refreshes.
            await br.eval(f"return fetch('/fresh/conversations/{cid}',{{method:'PATCH',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{title:'Sauna'}})}}).then(r=>r.status);")
            await br.eval("document.dispatchEvent(new Event('visibilitychange')); return 1;")
            await asyncio.sleep(2)
            self.assertEqual(await br.eval("return document.querySelector('.topbar .title-sub').textContent;"), "Sauna")
            await br.goto(f"{self.base}/mobile/#/agents/fresh/new", wait=2)
            self.assertEqual(await br.eval("return document.querySelector('.topbar .title-sub').textContent;"), "New chat")
            self.assertEqual([l for l in br.logs if "EXC" in l], [], "no page errors")
        finally:
            await br.stop()

    # Desktop Dash tab (2026-10-14/16): right of Files, a tab like Chat
    # and Files that shows the agent's own page (the phone's) in the same left
    # pane, widened for it; Rick's has the weekly cleanup.
    async def _desktop_page(self):
        br = Browser()
        await br.start(1400, 900)
        try:
            await br.goto(f"{self.base}/rick", wait=6)
            _, kids = await br.frames()
            f = kids[0]
            tabs = await br.eval("return [...document.querySelectorAll('.left-tab')].map(b=>({t:b.textContent, x:b.getBoundingClientRect().x}));", f)
            self.assertEqual([t["t"] for t in tabs], ["Chat", "Files", "Dash", "Tools", "Skills", "Recaps"])
            self.assertGreater(tabs[2]["x"], tabs[1]["x"], "Dash is right of Files")
            box = "const r=document.querySelector(%r).getBoundingClientRect(); return {x:r.x,y:r.y,w:r.width,h:r.height};"
            chat_pane = await br.eval(box % "#convo-pane", f)
            left_before = await br.eval(box % ".col-left", f)
            await br.eval("document.querySelector('.left-tab[data-tab=dash]').click(); return 1;", f)
            await asyncio.sleep(4)
            pane = await br.eval(box % "#embed-pane", f)
            dash = await br.eval(box % "iframe[data-view=console]")
            for k in ("x", "y", "w", "h"):
                self.assertLess(abs(pane[k] - dash[k]), 1.5, f"the page sits exactly in the pane ({k}): {pane} {dash}")
            self.assertLess(abs(pane["y"] - chat_pane["y"]), 30, "where the chat list was")
            self.assertGreaterEqual((await br.eval(box % ".col-left", f))["w"], 419, "pane widened for the page")
            # Content wider than the pane: the pane grows to fit (up to 60% of the window).
            await br.eval("const d=document.querySelector('iframe[data-view=console]').contentDocument; const s=d.createElement('section'); s.style.overflowX='auto'; s.innerHTML='<div style=\"width:700px;height:20px\"></div>'; d.querySelector('#console-body').prepend(s); return 1;")
            await asyncio.sleep(1.5)
            wide = (await br.eval(box % ".col-left", f))["w"]
            self.assertGreater(wide, 700, "widened for wide content")
            self.assertLessEqual(wide, 1400 * 0.6 + 1, "but at most 60% of the window")
            dash = await br.eval(box % "iframe[data-view=console]")
            self.assertLess(abs((await br.eval(box % "#embed-pane", f))["w"] - dash["w"]), 1.5, "and the page follows")
            inner = "const d=document.querySelector('iframe[data-view=console]').contentDocument;"
            self.assertTrue(await br.eval(f"{inner} return !!d.querySelector('#cleanup');"), "Rick's page with the cleanup")
            self.assertEqual(await br.eval(f"{inner} return getComputedStyle(d.querySelector('.topbar')).display;"), "none", "no phone top bar")
            self.assertEqual(await br.eval(f"{inner} return getComputedStyle(d.querySelector('.console-actions')).display;"), "none", "no chat/files buttons")
            self.assertEqual(await br.eval("return document.querySelector('.left-tab.active').dataset.tab;", f), "dash")
            # Back to Chat: the page goes, the column is your own width again.
            await br.eval("document.querySelector('.left-tab[data-tab=convos]').click(); return 1;", f)
            await asyncio.sleep(0.8)
            self.assertEqual(await br.eval("return getComputedStyle(document.querySelector('iframe[data-view=console]')).display;"), "none")
            self.assertLess(abs((await br.eval(box % ".col-left", f))["w"] - left_before["w"]), 1.5, "own column width back")
            self.assertFalse(await br.eval("return document.querySelector('#convo-pane').hidden;", f))
            self.assertEqual([l for l in br.logs if "EXC" in l], [], "no page errors")
        finally:
            await br.stop()

    def test_desktop_page(self):
        self.run_async(self._desktop_page())

    # Recaps on desktop (2026-11-03): a compact list in the sidebar (name, how
    # it ended, date); a click shows the recap and the transcript in the chat
    # on the right. The tab row scrolls with ‹ › when the tabs don't fit.
    async def _desktop_recaps(self):
        archived = self.tmp / ".openclave" / "agents" / "Greg" / "conversations" / "archived"
        archived.mkdir(parents=True, exist_ok=True)
        (archived / "20261001-100000.md").write_text(
            "---\nAgent: Greg\nDate: '2026-10-01'\nTitle: Tomato beds\nRecap:\n  about: South bed, after May 17.\n"
            "  status: concluded\n  open_ends: ''\n  made: '2026-10-02'\n---\n\n### User (10:00)\nNorth or south?\n\n"
            "### Greg (10:01)\nSouth bed.\n", encoding="utf-8")
        br = Browser()
        await br.start(1100, 800)
        try:
            await br.goto(f"{self.base}/greg", wait=6)
            _, kids = await br.frames()
            f = kids[0]
            arrows = "return [...document.querySelectorAll('.tabs-arrow')].map(b=>!b.hidden);"
            overflow = await br.eval("const r=document.querySelector('.left-tabs'); return r.scrollWidth > r.clientWidth + 1;", f)
            self.assertEqual(await br.eval(arrows, f), [False, overflow], "› only when tabs are cut off")
            await br.eval("document.querySelector('.left-tab[data-tab=recaps]').click(); return 1;", f)
            await asyncio.sleep(1.5)
            tab_in_view = await br.eval("const t=document.querySelector('.left-tab[data-tab=recaps]').getBoundingClientRect(), r=document.querySelector('.left-tabs').getBoundingClientRect(); return t.right <= r.right + 1 && t.left >= r.left - 1;", f)
            dbg = await br.eval("const t=document.querySelector('.left-tab[data-tab=recaps]').getBoundingClientRect(), row=document.querySelector('.left-tabs'), r=row.getBoundingClientRect(); return {tl:t.left,tr:t.right,rl:r.left,rr:r.right,sl:row.scrollLeft,sw:row.scrollWidth,cw:row.clientWidth};", f)
            self.assertTrue(tab_in_view, f"the chosen tab scrolls into view {dbg}")
            self.assertFalse(await br.eval("return document.querySelector('#recaps-pane').hidden;", f))
            rows = await br.eval("return [...document.querySelectorAll('#recaps-list .recap-item')].map(e=>e.innerText);", f)
            self.assertEqual(len(rows), 1)
            self.assertIn("Tomato beds", rows[0])
            self.assertIn("Concluded", rows[0])
            self.assertNotIn("South bed", rows[0], "no summary text in the list")
            await br.eval("document.querySelector('#recaps-list .recap-item').click(); return 1;", f)
            await asyncio.sleep(2)
            banner = await br.eval("const b=document.querySelector('#chat-window .recap-banner'); return b ? b.innerText : null;", f)
            self.assertIn("South bed, after May 17.", banner or "")
            self.assertIn("North or south?", await br.eval("return document.querySelector('#chat-window').innerText;", f))
            self.assertEqual([l for l in br.logs if "EXC" in l], [], "no page errors")
        finally:
            await br.stop()

    def test_desktop_recaps(self):
        self.run_async(self._desktop_recaps())

    # Chat folders (folders.py, 2026-11-03): chats grouped under plain folder
    # headers; drag a chat onto a folder to move it. Phone: Folder button.
    async def _folders(self):
        root = self.tmp / ".openclave" / "agents" / "R2"
        (root / "folders.json").write_text('{"folders": ["Zigbee", "Empty one"]}', encoding="utf-8")
        convos = root / "conversations"
        convos.mkdir(parents=True, exist_ok=True)
        (convos / "20261101-100000-aaaaaaaa.md").write_text(
            "---\nAgent: R2\nDate: '2026-11-01'\nTitle: Coordinator move\nFolder: Zigbee\n---\n\n### User (10:00)\nWhere?\n\n### R2 (10:01)\nHallway.\n", encoding="utf-8")
        (convos / "20261102-100000-bbbbbbbb.md").write_text(
            "---\nAgent: R2\nDate: '2026-11-02'\nTitle: Router reboot\n---\n\n### User (10:00)\nReboot?\n\n### R2 (10:01)\nDone.\n", encoding="utf-8")
        groups_js = "return [...document.querySelectorAll('#convo-list .folder-group')].map(g=>[g.querySelector('.folder-name').textContent, [...g.querySelectorAll('.convo-item .t')].map(t=>t.textContent)]);"
        br = Browser()
        await br.start(1100, 800)
        try:
            await br.goto(f"{self.base}/r2", wait=6)
            _, kids = await br.frames()
            f = kids[0]
            groups = await br.eval(groups_js, f)
            self.assertEqual(groups[:2], [["Zigbee", ["Coordinator move"]], ["Empty one", []]])
            self.assertEqual(groups[-1][0], "No topic")
            self.assertIn("Router reboot", groups[-1][1])
            # Drag "Router reboot" onto the empty folder.
            await br.eval("""const row=[...document.querySelectorAll('.convo-item')].find(e=>e.innerText.includes('Router reboot'));
              const target=[...document.querySelectorAll('.folder-group')][1];
              const dt=new DataTransfer();
              row.dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer:dt}));
              target.dispatchEvent(new DragEvent('dragover',{bubbles:true,cancelable:true,dataTransfer:dt}));
              target.dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:dt})); return 1;""", f)
            await asyncio.sleep(1.5)
            groups = await br.eval(groups_js, f)
            self.assertEqual(groups[1], ["Empty one", ["Router reboot"]])
            self.assertFalse(await br.eval("return !!document.querySelector('.folder-count');", f), "no counts")
            self.assertEqual(await br.eval("return getComputedStyle(document.querySelector('.folder-fold')).color;", f),
                             await br.eval("return getComputedStyle(document.querySelector('.folder-new')).color;", f))
            accent = await br.eval("const d=document.createElement('i'); d.style.color=getComputedStyle(document.documentElement).getPropertyValue('--accent'); document.body.appendChild(d); const c=getComputedStyle(d).color; d.remove(); return c;", f)
            self.assertEqual(await br.eval("return getComputedStyle(document.querySelector('.folder-fold')).color;", f), accent, "agent colour")
            self.assertEqual(await br.eval("return parseFloat(getComputedStyle(document.querySelector('.folder-name')).fontSize);", f),
                             await br.eval("return parseFloat(getComputedStyle(document.querySelector('.folder-chats .convo-item .t')).fontSize);", f), "topics as big as chats (2026-11-04)")
            # "+ new topic" sits next to "+ new chat" and looks the same.
            self.assertEqual(await br.eval("const t=document.getElementById('new-topic'), c=document.getElementById('new-convo'); return [t.className, t.parentElement===c.parentElement, t.textContent, Math.round(t.getBoundingClientRect().top)===Math.round(c.getBoundingClientRect().top)];", f),
                             ["new-convo", True, "+ new topic", True])
            self.assertFalse(await br.eval("return !!document.querySelector('.folder-add');", f), "no second button under the list")
            await br.eval("window.prompt=()=>'Made here'; document.getElementById('new-topic').click(); return 1;", f)
            await asyncio.sleep(1.5)
            self.assertIn("Made here", await br.eval("return [...document.querySelectorAll('.folder-name')].map(e=>e.textContent);", f))
            await br.eval("document.querySelector('.folder-new').click(); return 1;", f)
            await asyncio.sleep(2)
            listed = await br.eval("return fetch('/r2/conversations').then(r=>r.json());", f)
            self.assertEqual(sum(1 for c in listed if c["folder"] == "Zigbee"), 2, "+ starts a chat in that folder")
            await br.eval("document.querySelector('.folder-fold').click(); return 1;", f)
            self.assertTrue(await br.eval("return document.querySelector('.folder-group').classList.contains('folded');", f))
            self.assertEqual([l for l in br.logs if "EXC" in l], [], "no page errors")
            await br.stop()
            br = Browser()
            await br.start(390, 844)
            await br.goto(f"{self.base}/mobile/#/agents/r2/conversations", wait=5)
            names = await br.eval("return [...document.querySelectorAll('.folder-group .folder-name')].map(e=>e.textContent);")
            agent_color = await br.eval("return fetch('/api/agents').then(r=>r.json()).then(l=>l.find(a=>a.id==='r2').color);")
            self.assertEqual(await br.eval("return document.querySelector('.folder-list').style.getPropertyValue('--folder');"), agent_color)
            self.assertEqual(names[-1], "No topic")
            self.assertEqual(names[:2], ["Zigbee", "Empty one"])
            self.assertTrue(await br.eval("return !!document.querySelector('[data-folder-move]') && !!document.querySelector('#new-folder') && document.querySelectorAll('[data-folder-new]').length >= 2;"))
            self.assertEqual(await br.eval("const t=document.getElementById('new-folder'), c=document.getElementById('new-chat'); return [t.className, t.textContent, Math.round(t.getBoundingClientRect().top)===Math.round(c.getBoundingClientRect().top)];"),
                             ["primary", "New topic", True])
            self.assertEqual(await br.eval("return parseFloat(getComputedStyle(document.querySelector('.folder-name')).fontSize);"),
                             await br.eval("return parseFloat(getComputedStyle(document.querySelector('.folder-chats .row-title')).fontSize);"), "same size on the phone")
            self.assertLessEqual(await br.eval("return document.documentElement.scrollWidth;"), 390, "no sideways scroll")
            self.assertEqual([l for l in br.logs if "EXC" in l], [], "no page errors")
        finally:
            await br.stop()

    def test_folders(self):
        self.run_async(self._folders())

    # Home dashboard (2026-11-03): as wide as the Control Panel, three tiles a
    # row; clicking an agent's round icon opens a quick edit (name, colour,
    # icon); the maintainer inbox lives in the Control Panel now.
    async def _quick_edit(self):
        agents_now = json.loads(urllib.request.urlopen(f"{self.base}/api/agents").read())
        r2 = next(a for a in agents_now if a["id"] == "r2")
        br = Browser()
        await br.start(1700, 1000)
        try:
            await br.goto(f"{self.base}/", wait=5)
            self.assertLessEqual(await br.eval("return document.body.getBoundingClientRect().width;"), 1100)
            cols = await br.eval("return getComputedStyle(document.getElementById('grid')).gridTemplateColumns.split(' ').length;")
            self.assertEqual(cols, 3)
            self.assertFalse(await br.eval("return !!document.getElementById('inbox-panel');"), "inbox moved to the Control Panel")
            self.assertEqual(await br.eval("return [...document.querySelectorAll('.view-switch button')].map(b=>b.textContent);"),
                             ["Boxes", "Group chat"])
            mid = await br.eval("const d=document.querySelector('.open-dashboard').getBoundingClientRect(), c=document.querySelector('.claves-btn').getBoundingClientRect(), v=document.querySelector('.view-switch').getBoundingClientRect(); return [d.right < v.left, v.right < c.left];")
            self.assertEqual(mid, [True, True], "Boxes / Group chat in the middle, Claves on the right")
            pos = await br.eval("const d=document.querySelector('.open-dashboard').getBoundingClientRect(), v=document.querySelector('.view-switch').getBoundingClientRect(), r=document.querySelector('.clave-row').getBoundingClientRect(), h=document.querySelector('header').getBoundingClientRect(); return [d.left-h.left, h.right-r.right, d.height, v.height];")
            self.assertLess(pos[0], 5, "Open agent page on the left")
            self.assertLess(pos[1], 5, "Claves (and its picker) on the right")
            self.assertEqual(await br.eval("const r=document.querySelector('.clave-row'); return [r.tagName, r.querySelector('legend').textContent, document.querySelector('.claves-btn').textContent];"),
                             ["FIELDSET", "Claves", "Organize"], "a box labelled Claves holding Organize and the picker")
            self.assertEqual(await br.eval("return document.querySelector('.view-switch').closest('fieldset').querySelector('legend').textContent;"), "View")
            self.assertGreater(pos[2], pos[3], "Open agent page is the bigger button")
            self.assertLess(pos[2], pos[3] * 1.5, "but not much bigger (about 30%)")
            await br.eval("document.querySelector('.tile[data-agent-id=r2] .tile-icon').click(); return 1;")
            await asyncio.sleep(0.5)
            self.assertTrue(await br.eval("return !!document.querySelector('.qe .qe-icon') && !!document.querySelector('.qe input[type=color]');"))
            await br.eval("const q=document.querySelector('.qe'); q.querySelector('.qe-name').value='Artoo'; const c=q.querySelector('.qe-color'); c.value='#123456'; c.dispatchEvent(new Event('input')); q.querySelector('.qe-save').click(); return 1;")
            await asyncio.sleep(1.5)
            now = next(a for a in json.loads(urllib.request.urlopen(f"{self.base}/api/agents").read()) if a["id"] == "r2")
            self.assertEqual((now["name"], now["color"]), ("Artoo", "#123456"))
            self.assertEqual(await br.eval("return document.querySelector('.tile[data-agent-id=r2] .tile-name').textContent;"), "Artoo")
            self.assertFalse(await br.eval("return !!document.querySelector('.qe');"), "closes after saving")
            # Change icon: a picture, Cartoon, tinted in the agent colour (2026-11-04).
            await br.eval("document.querySelector('.tile[data-agent-id=r2] .tile-icon').click(); return 1;")
            await asyncio.sleep(0.4)
            await br.eval("document.querySelector('.qe .qe-icon').click(); return 1;")
            await asyncio.sleep(0.4)
            self.assertTrue(await br.eval("return document.querySelector('.avm [data-tint]').checked;"), "tint on by default")
            await br.eval("""const c=document.createElement('canvas'); c.width=c.height=200; const x=c.getContext('2d');
                x.fillStyle='#ddd'; x.fillRect(0,0,200,200); x.fillStyle='#333'; x.beginPath(); x.arc(100,100,60,0,7); x.fill();
                c.toBlob(b=>{const dt=new DataTransfer(); dt.items.add(new File([b],'p.png',{type:'image/png'}));
                  const i=document.querySelector('.avm [data-input]'); i.files=dt.files; i.dispatchEvent(new Event('change'));}); return 1;""")
            await asyncio.sleep(1)
            await br.eval("document.querySelector('.avm [data-style=cartoon]').click(); return 1;")
            await asyncio.sleep(2.5)
            self.assertEqual(await br.eval("return document.querySelector('.avm [data-msg]').textContent;"), "", "cartoon preview without error")
            self.assertTrue(await br.eval("return document.querySelector('.avm [data-prev]').src.startsWith('blob:');"))
            await br.eval("document.querySelector('.avm [data-save]').click(); return 1;")
            await asyncio.sleep(1.5)
            self.assertFalse(await br.eval("return !!document.querySelector('.avm');"), "maker closes after saving")
            avatars = self.tmp / ".openclave" / "state" / "custom_avatars"
            self.assertTrue((avatars / "r2.base.png").exists(), "untinted picture kept for re-tinting")
            from PIL import Image
            before = Image.open(avatars / "r2.png").convert("RGB").resize((1, 1), Image.BOX).getpixel((0, 0))
            urllib.request.urlopen(urllib.request.Request(f"{self.base}/api/agents/r2/color", data=b'{"color": "#cc2200"}',
                                                          headers={"Content-Type": "application/json"}, method="POST"))
            after = Image.open(avatars / "r2.png").convert("RGB").resize((1, 1), Image.BOX).getpixel((0, 0))
            self.assertLess(before[0], before[2], "tinted in #123456 (blue)")
            self.assertGreater(after[0], after[2], "re-tinted in #cc2200 (red)")
            for f in ("r2.png", "r2.base.png"):
                (avatars / f).unlink(missing_ok=True)
            self.assertEqual([l for l in br.logs if "EXC" in l], [], "no page errors")
            # "Dashboard" opens the page of the agent used last, like clicking its box.
            await br.goto(f"{self.base}/", wait=4)
            await br.eval("document.querySelector('[data-open-dashboard]').click(); return 1;")
            await asyncio.sleep(2)
            path = await br.eval("return location.pathname;")
            ids = [a["id"] for a in json.loads(urllib.request.urlopen(f"{self.base}/api/agents").read())]
            self.assertIn(path.strip("/"), ids)
            await br.goto(f"{self.base}/control/", wait=4)
            self.assertTrue(await br.eval("return !!document.getElementById('inbox-list') && !document.getElementById('inbox-list').innerText.includes('Loading');"))
        finally:
            await br.stop()
            for path, body in ((f"/api/agents/r2/name", {"name": r2["name"], "tagline": r2["tagline"]}), (f"/api/agents/r2/color", {"color": r2["color"]})):
                urllib.request.urlopen(urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST"))

    # Claves (claves.py, 2026-11-04): the overview turns them on, drag moves,
    # Ctrl-drag shares, every clave gets a channel in the group chat and a row
    # on the phone, and the phone has a per-agent checklist.
    async def _claves(self):
        def api(path, body=None, method=None):
            req = urllib.request.Request(f"{self.base}{path}", method=method or ("POST" if body is not None else "GET"),
                                         data=None if body is None else json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", "Origin": self.base})
            return json.loads(urllib.request.urlopen(req).read())
        br = Browser()
        await br.start(1400, 1000)
        try:
            await br.goto(f"{self.base}/", wait=4)
            await br.eval("document.querySelector('.claves-btn').click(); return 1;")
            await asyncio.sleep(1)
            self.assertTrue(await br.eval("return !!document.querySelector('.cl-off');"), "explains claves while off")
            await br.eval("document.getElementById('cl-enabled').click(); return 1;")
            await asyncio.sleep(1.2)
            names = await br.eval("return [...document.querySelectorAll('.cl-card .cl-name')].map(e=>e.textContent);")
            self.assertEqual(names, ["Personal"])
            everyone = sorted(a["id"] for a in api("/api/agents"))
            self.assertEqual(sorted(api("/api/claves")["claves"][0]["members"]), everyone)
            await br.eval("window.prompt = () => 'RnD'; document.getElementById('cl-new').click(); return 1;")
            await asyncio.sleep(1.2)
            drag = """const [agent, zoneSel, ctrl] = arguments;
              const chip = document.querySelector(`.cl-card[data-clave=personal] .cl-agent[data-agent=${agent}]`);
              const zone = document.querySelector(zoneSel);
              const dt = new DataTransfer();
              chip.dispatchEvent(new DragEvent('dragstart', {bubbles: true, dataTransfer: dt}));
              const r = zone.getBoundingClientRect();
              const opts = {bubbles: true, cancelable: true, dataTransfer: dt, ctrlKey: ctrl, clientX: r.left + 5, clientY: r.bottom - 2};
              zone.dispatchEvent(new DragEvent('dragover', opts));
              zone.dispatchEvent(new DragEvent('drop', opts));
              chip.dispatchEvent(new DragEvent('dragend', {bubbles: true, dataTransfer: dt}));
              return 1;"""
            await br.eval(f"return (function(){{{drag}}}).apply(null, ['r2', '.cl-zone.members[data-clave=rnd]', false]);")
            await asyncio.sleep(1)
            # Ctrl-drag asks to clone (cancelled here: clone_agent has its own tests,
            # and a clone would change the roster the other UI tests use).
            await br.eval("window.__asked = ''; window.confirm = text => { window.__asked = text; return false; }; return 1;")
            await br.eval(f"return (function(){{{drag}}}).apply(null, ['stash', '.cl-zone.members[data-clave=rnd]', true]);")
            await asyncio.sleep(0.5)
            self.assertIn("Clone Stash into RnD", await br.eval("return window.__asked;"))
            await br.eval(f"return (function(){{{drag}}}).apply(null, ['stash', '.cl-zone.lead[data-clave=rnd]', false]);")
            await asyncio.sleep(1)
            data = {c["id"]: c for c in api("/api/claves")["claves"]}
            self.assertNotIn("r2", data["personal"]["members"], "plain drag moves")
            self.assertNotIn("stash", data["personal"]["members"], "one agent, one clave")
            self.assertEqual(data["rnd"]["lead"], "stash")
            self.assertEqual(set(data["rnd"]["members"]), {"r2", "stash"})
            self.assertEqual(await br.eval("return new URL(document.querySelector('.cl-card[data-clave=rnd] .cl-add').href).search;"),
                             "?clave=rnd", "+ New agent box in the members section")
            # Two claves: a picker right of the Claves button; Boxes shows the picked one.
            self.assertEqual(await br.eval("return [...document.querySelectorAll('#clave-pick .clave-chip')].map(c=>[c.textContent, c.getAttribute('aria-pressed')]);"),
                             [["Personal", "true"], ["RnD", "false"]])
            await br.eval("document.querySelector('#clave-pick [data-clave=rnd]').click(); return 1;")
            await asyncio.sleep(0.8)
            await br.eval("document.querySelector('.view-switch [data-view=tiles]').click(); return 1;")
            await asyncio.sleep(1.5)
            self.assertEqual(sorted(await br.eval("return [...document.querySelectorAll('.tile[data-agent-id]')].map(t=>t.dataset.agentId);")), ["r2", "rick", "stash"], "the manager shows in every clave")
            self.assertEqual(api("/api/claves")["active"], "rnd")
            tabs = [t["id"] for t in api("/r2/agent-tabs")]
            self.assertEqual(sorted(tabs), ["r2", "rick", "stash"], "agent page tabs follow the active clave")
            # The agent page: the same picker left of the bell; switching redraws the tabs.
            await br.goto(f"{self.base}/r2", wait=5)
            _, kids = await br.frames()
            f = kids[0]
            order = await br.eval("return [...document.querySelector('.header-right').children].filter(e=>!e.hidden).map(e=>e.id||e.className).slice(0,3);", f)
            self.assertEqual(order[:2], ["clave-pick", "notify-btn"], "clave picker left of the bell")
            self.assertEqual(sorted(await br.eval("return [...document.querySelectorAll('.agent-tab')].map(t=>t.dataset.agent);", f)), ["r2", "rick", "stash"])
            await br.eval("document.querySelector('#clave-pick [data-clave=personal]').click(); return 1;", f)
            await asyncio.sleep(1)
            self.assertEqual(api("/api/claves")["active"], "personal")
            personal = {c["id"]: c for c in api("/api/claves")["claves"]}["personal"]["members"]
            self.assertEqual(sorted(await br.eval("return [...document.querySelectorAll('.agent-tab')].map(t=>t.dataset.agent);", f)),
                             sorted(set(personal) | {"r2", "rick"}), "tabs redrawn for Personal (r2's own tab stays)")
            await br.goto(f"{self.base}/", wait=3)
            # The group chat lists a channel per clave.
            await br.eval("document.querySelector('.view-switch [data-view=chat]').click(); return 1;")
            await asyncio.sleep(1.5)
            channels = await br.eval("return [...document.querySelectorAll('#cv-side [data-room] .cv-name')].map(e=>e.textContent);")
            self.assertEqual(channels, ["everyone", "Personal", "RnD"])
            await br.eval("document.querySelector('#cv-side [data-clave=rnd]').click(); return 1;")
            await asyncio.sleep(0.8)
            self.assertIn("RnD", await br.eval("return document.querySelector('#cv-head .cv-title').textContent;"))
            # Own channels (2026-11-05): + Add channel, pick members, it opens.
            self.assertIn("Agents – private chat", await br.eval("return document.querySelector('#cv-side').textContent;"))
            await br.eval("document.querySelector('#cv-side [data-channel-new]').click(); return 1;")
            await asyncio.sleep(0.5)
            await br.eval("document.querySelector('.channel-name').value='garage'; document.querySelector('.handover-file input[value=r2]').click(); document.querySelector('.channel-save').click(); return 1;")
            await asyncio.sleep(1.5)
            self.assertEqual(await br.eval("return document.querySelector('#cv-head .cv-title').textContent;"), "# garage")
            self.assertIn("Members", await br.eval("return document.querySelector('#cv-head').textContent;"))
            made = api("/api/channels")
            self.assertEqual([(c["name"], c["members"]) for c in made], [("garage", ["r2"])])
            urllib.request.urlopen(urllib.request.Request(f"{self.base}/api/channels/{made[0]['id']}", method="DELETE"))
            self.assertEqual([l for l in br.logs if "EXC" in l], [], "no page errors")
        finally:
            await br.stop()
        phone = Browser()
        await phone.start(390, 844)
        try:
            # Shared profile: leave the desktop home on Boxes for the next test.
            await phone.goto(f"{self.base}/", wait=1)
            await phone.eval("localStorage.setItem('openclave.home.view', 'tiles'); return 1;")
            await phone.goto(f"{self.base}/mobile/#/agents", wait=3)
            await asyncio.sleep(1)
            rows = await phone.eval("return [...document.querySelectorAll('.room-row .row-title')].map(e=>e.textContent);")
            self.assertEqual(rows, ["everyone", "Personal", "RnD", "Add channel"])
            await phone.goto(f"{self.base}/mobile/#/agents/r2/claves", wait=2)
            await asyncio.sleep(0.8)
            ticks = await phone.eval("return [...document.querySelectorAll('.tool-row input')].map(b=>[b.value,b.checked]);")
            self.assertEqual(ticks, [["personal", False], ["rnd", True]])
            await phone.eval("document.querySelector('.tool-row input[value=personal]').click(); return 1;")
            await asyncio.sleep(1)
            self.assertEqual(api("/api/agents/r2/claves")["selected"], "personal", "picking another clave moves it")
        finally:
            await phone.stop()
            api("/api/claves/enabled", {"enabled": False})

    def test_claves(self):
        self.run_async(self._claves())

    def test_quick_edit(self):
        self.run_async(self._quick_edit())

    # Tools (2026-10-16): a Tools button next to Files on the phone and a
    # Tools tab right of Dash on desktop, both showing the same tick boxes;
    # every tick saves at once.
    async def _tools(self):
        br = Browser()
        await br.start(390, 844)
        try:
            await br.goto(f"{self.base}/mobile/#/agents/stash/console", wait=4)
            await br.eval("document.querySelector('#console-tools').click(); return 1;")
            await asyncio.sleep(2)
            self.assertTrue((await br.eval("return location.hash;")).endswith("/stash/tools"))
            names = await br.eval("return [...document.querySelectorAll('.tool-row input')].map(i=>i.value);")
            self.assertIn("make_pdf", names)
            await br.eval("const b=document.querySelector('.tool-row input[value=make_pdf]'); b.click(); return 1;")
            await asyncio.sleep(1.5)
            self.assertEqual(await br.eval("return document.querySelector('#tools-status').textContent;"), "Saved")
            saved = await br.eval("return fetch('/api/agents/stash/tools').then(r=>r.json()).then(t=>t.selected);")
            self.assertNotIn("make_pdf", saved, "unticked and saved")
            await br.eval("document.querySelector('.tool-row input[value=make_pdf]').click(); return 1;")
            await asyncio.sleep(1.5)
            saved = await br.eval("return fetch('/api/agents/stash/tools').then(r=>r.json()).then(t=>t.selected);")
            self.assertIn("make_pdf", saved, "ticked again")
            self.assertLessEqual(await br.eval("return document.documentElement.scrollWidth;"), 390, "no sideways scroll")
            await br.stop()
            br = Browser()
            await br.start(1400, 900)
            await br.goto(f"{self.base}/stash", wait=6)
            _, kids = await br.frames()
            await br.eval("document.querySelector('.left-tab[data-tab=tools]').click(); return 1;", kids[0])
            await asyncio.sleep(4)
            inner = "const d=document.querySelector('iframe[data-view=tools]').contentDocument;"
            self.assertGreater(await br.eval(f"{inner} return d.querySelectorAll('.tool-row input').length;"), 3)
            self.assertEqual(await br.eval("return getComputedStyle(document.querySelector('iframe[data-view=tools]')).display;"), "block", "in the left pane, like Chat and Files")
            await br.eval("document.querySelector('.left-tab[data-tab=skills]').click(); return 1;", kids[0])
            await asyncio.sleep(4)
            self.assertEqual(await br.eval("return getComputedStyle(document.querySelector('iframe[data-view=skills]')).display;"), "block", "Skills opens the same way")
            self.assertIn("Import a skill", await br.eval("return document.querySelector('iframe[data-view=skills]').contentDocument.body.innerText;"))
            self.assertEqual([l for l in br.logs if "EXC" in l], [], "no page errors")
        finally:
            await br.stop()

    def test_tools(self):
        self.run_async(self._tools())

    def test_chat_title(self):
        self.run_async(self._chat_title())

    def test_model_label(self):
        self.run_async(self._model_label())

    def test_cleanup(self):
        self.run_async(self._cleanup())

    def test_streaming(self):
        self.run_async(self._streaming())

    def test_cache_first(self):
        self.run_async(self._cache_first())

    # Reba's page (2026-10-12): her sliders log to tracking.md from a
    # page.json "sliders" block, with the activity log under them; a page an
    # agent changes shows Keep / Revert until the owner decides.
    REBA_PAGE = {"title": "Reba", "blocks": [
        {"type": "sliders", "title": "Progress and pain", "metrics": [
            {"id": "dorsiflexion", "label": "Dorsiflexion", "min": 6, "max": 15, "step": 0.5, "unit": "cm"},
            {"id": "pf", "label": "PF", "min": 0, "max": 10, "step": 1}]},
        {"type": "table", "file": "tracking.md", "title": "Activity log", "sections": ["General Activities", "Rehab Activities"]}]}

    async def _reba_page(self):
        import json as _json
        reba = self.tmp / ".openclave" / "agents" / "Reba"
        tracking = reba / "tracking.md"
        tracking.write_text("# Reba — Tracking\n\n## Progress\n\n| Date | dorsiflexion | pf |\n|---|---|---|\n| 01-10-2026 | 9 cm | 4 |\n\n"
                            "## General Activities\n\n| Date | hike |\n|---|---|\n| 02-10-2026 | Dunes |\n\n"
                            "## Rehab Activities\n\n| Date | HSR |\n|---|---|\n| 03-10-2026 | 145 kg |\n")
        (reba / "page.json").write_text(_json.dumps(self.REBA_PAGE))
        br = Browser()
        await br.start(390, 844)
        try:
            await br.goto(f"{self.base}/mobile/#/agents/reba/console", wait=3)
            self.assertEqual(await br.eval("return document.querySelectorAll('.page-sliders input[type=range]').length;"), 2)
            self.assertEqual(await br.eval("return document.querySelector('.page-sliders input[name=pf]').value;"), "4", "starts at the latest logged value")
            self.assertFalse(await br.eval("return !!document.querySelector('.page-changed');"), "first version: nothing to decide")
            cards = await br.eval("return [...document.querySelectorAll('.page-card-title')].map(c=>c.textContent);")
            self.assertEqual(cards, ["02-10-2026", "03-10-2026"], "activity log under the sliders")
            order = await br.eval("const s=document.querySelector('.page-sliders').getBoundingClientRect().top, t=document.querySelector('.page-table').getBoundingClientRect().top; return s < t;")
            self.assertTrue(order, "sliders first")
            await br.eval("const r=document.querySelector('.page-sliders input[name=pf]'); r.value='7'; r.dispatchEvent(new Event('input')); document.querySelector('.page-sliders form').requestSubmit(); return 1;")
            await asyncio.sleep(1.5)
            self.assertEqual(await br.eval("return document.querySelector('.page-sliders .log-status').textContent;"), "Logged.")
            self.assertRegex(tracking.read_text(), r"\| 9 cm \| 7 \|", "logged to tracking.md")

            # Reba rewrites her page: The owner sees it with Keep / Revert.
            (reba / "page.json").write_text(_json.dumps({"blocks": [{"type": "text", "text": "Reba's new idea"}]}))
            await br.eval("location.reload(); return 1;"); await asyncio.sleep(3)
            self.assertIn("Reba changed this page", await br.eval("return document.querySelector('.page-changed')?.textContent || '';"))
            self.assertIn("Reba's new idea", await br.eval("return document.querySelector('#console-body').innerText;"))
            await br.eval("window.confirm = () => true; document.querySelector('[data-page-revert]').click(); return 1;")
            await asyncio.sleep(2)
            self.assertEqual(await br.eval("return document.querySelectorAll('.page-sliders input[type=range]').length;"), 2, "Revert brings the sliders back")
            self.assertFalse(await br.eval("return !!document.querySelector('.page-changed');"))
            self.assertIn('"sliders"', (reba / "page.json").read_text())
            # Keep: the banner goes and stays gone.
            (reba / "page.json").write_text(_json.dumps({**self.REBA_PAGE, "title": "Reba 2"}))
            await br.eval("location.reload(); return 1;"); await asyncio.sleep(3)
            await br.eval("document.querySelector('[data-page-keep]').click(); return 1;")
            await asyncio.sleep(1)
            await br.eval("location.reload(); return 1;"); await asyncio.sleep(3)
            self.assertFalse(await br.eval("return !!document.querySelector('.page-changed');"), "kept")
            self.assertEqual(await br.eval("return document.documentElement.scrollWidth;"), 390)
            self.assertEqual([l for l in br.logs if "EXC" in l], [], "no page errors")
        finally:
            (reba / "page.json").unlink(missing_ok=True)
            await br.stop()

    def test_reba_page(self):
        self.run_async(self._reba_page())

    # Phone Files button (2026-10-12): right of Open chat on the agent's
    # page; lists only that agent's folder; edit, save, and a stale save or a
    # mission/persona save is guarded.
    # Purge (2026-11-05): phone row action, dialog, purged banner with Undo.
    async def _purge(self):
        convos = self.tmp / ".openclave" / "agents" / "Stash" / "conversations"
        convos.mkdir(parents=True, exist_ok=True)
        cid = "20261101-100000-cccccccc"
        f = convos / f"{cid}.md"
        f.write_text("---\nAgent: Stash\nDate: '2026-11-01'\nTitle: Purge me\n---\n\n### User (10:00)\nhi\n\n### Stash (10:00)\nhello\n")
        original = f.read_text()
        br = Browser()
        await br.start(390, 844)
        try:
            await br.goto(f"{self.base}/mobile/#/agents/stash/conversations", wait=3)
            await br.eval(f"document.querySelector('[data-purge=\"{cid}\"]').click(); return 1;")
            await asyncio.sleep(1.5)
            self.assertIn("Purge \"Purge me\"", await br.eval("return document.querySelector('.handover-dialog h2').textContent;"))
            await br.eval("document.querySelector('.purge-go').click(); return 1;")
            await asyncio.sleep(1.5)
            self.assertNotIn("### User", f.read_text())
            await br.goto(f"{self.base}/mobile/#/agents/stash/c/{cid}", wait=3)
            await br.eval(f"location.hash = '#/agents/stash/conversations'; return 1;")
            await asyncio.sleep(1)
            await br.eval(f"document.querySelector('.row[data-conversation=\"{cid}\"]').click(); return 1;")
            await asyncio.sleep(2)
            self.assertTrue(await br.eval("return !!document.querySelector('.purged-banner');"), "the chat says it was purged")
            await br.eval("document.querySelector('.purge-undo').click(); return 1;")
            await asyncio.sleep(1.5)
            self.assertEqual(f.read_text(), original, "Undo brings the transcript back")
            self.assertEqual([l for l in br.logs if "EXC" in l], [], "no page errors")
        finally:
            await br.stop()

    async def _phone_files(self):
        stash = self.tmp / ".openclave" / "agents" / "Stash"
        (stash / "notes-ui.md").write_text("first line\n")
        br = Browser()
        await br.start(390, 844)
        try:
            await br.goto(f"{self.base}/mobile/#/agents/stash/console", wait=3)
            pos = await br.eval("const c=document.querySelector('#console-chat').getBoundingClientRect(), f=document.querySelector('#console-files').getBoundingClientRect(); return [c.right, f.left, Math.abs(c.top-f.top)];")
            self.assertLessEqual(pos[0], pos[1], "Files is right of Open chat")
            self.assertLess(pos[2], 2, "on the same row")
            await br.eval("document.querySelector('#console-files').click(); return 1;")
            await asyncio.sleep(1.5)
            self.assertEqual(await br.eval("return location.hash;"), "#/agents/stash/files")
            paths = await br.eval("return [...document.querySelectorAll('.file-row')].map(r=>r.dataset.path);")
            self.assertIn("notes-ui.md", paths)
            self.assertFalse(any(p.startswith(("conversations/", ".")) or ".." in p for p in paths), paths)
            self.assertIn("core", await br.eval("return document.querySelector('.file-row[data-path=\"mission.md\"]').textContent;"))
            await br.eval("document.querySelector('.file-row[data-path=\"notes-ui.md\"]').click(); return 1;")
            await asyncio.sleep(1.5)
            self.assertEqual(await br.eval("return document.querySelector('.file-text').value;"), "first line\n")
            await br.eval("const t=document.querySelector('.file-text'); t.value='edited on the phone\\n'; t.dispatchEvent(new Event('input')); document.querySelector('#file-save').click(); return 1;")
            await asyncio.sleep(1)
            self.assertEqual(await br.eval("return document.querySelector('.file-status').textContent;"), "Saved ✓")
            self.assertEqual((stash / "notes-ui.md").read_text(), "edited on the phone\n")
            await br.eval("location.reload(); return 1;"); await asyncio.sleep(3)
            self.assertEqual(await br.eval("return document.querySelector('.file-text').value;"), "edited on the phone\n", "still there after a refresh")
            # The agent writes meanwhile: the save is refused, the owner's text stays.
            (stash / "notes-ui.md").write_text("Stash wrote this\n")
            await br.eval("const t=document.querySelector('.file-text'); t.value='my version\\n'; t.dispatchEvent(new Event('input')); document.querySelector('#file-save').click(); return 1;")
            await asyncio.sleep(1)
            self.assertIn("changed this file since you opened it", await br.eval("return document.querySelector('.file-status').textContent;"))
            self.assertEqual(await br.eval("return document.querySelector('.file-text').value;"), "my version\n")
            self.assertEqual((stash / "notes-ui.md").read_text(), "Stash wrote this\n")
            # Mission: asks first; saying no saves nothing.
            mission = (stash / "mission.md").read_text()
            await br.eval("location.hash = '#/agents/stash/files/mission.md'; return 1;"); await asyncio.sleep(1.5)
            await br.eval("window.__asked=0; window.confirm = () => { window.__asked++; return false; }; const t=document.querySelector('.file-text'); t.value += 'x'; document.querySelector('#file-save').click(); return 1;")
            await asyncio.sleep(0.8)
            self.assertEqual(await br.eval("return window.__asked;"), 1, "mission needs a confirmation")
            self.assertEqual((stash / "mission.md").read_text(), mission)
            self.assertLessEqual(await br.eval("return document.documentElement.scrollWidth;"), 390)
            self.assertEqual([l for l in br.logs if "EXC" in l], [], "no page errors")
        finally:
            (stash / "notes-ui.md").unlink(missing_ok=True)
            await br.stop()

    def test_purge(self):
        self.run_async(self._purge())

    def test_phone_files(self):
        self.run_async(self._phone_files())

    def test_page_form(self):
        self.run_async(self._page_form())

    def test_cost_dots(self):
        self.run_async(self._cost_dots())

    def test_partial_copy(self):
        self.run_async(self._partial_copy())

    def test_dashboard_tile_conversations(self):
        self.run_async(self._dashboard())

    def test_mobile_layout_and_send(self):
        self.run_async(self._mobile())

    def test_desktop_agent_page_chat(self):
        self.run_async(self._desktop())


@unittest.skipUnless(HAVE_DEPS, "needs chromium and websockets")
class FirstRunUiTest(unittest.TestCase):
    """A blank install (no agents; no model unless the test sets a key): the
    first steps on the home page and the phone, and nothing shown that can't
    be used yet."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        home = cls.tmp / ".openclave"
        (home / "agents").mkdir(parents=True)
        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "XAI_API_KEY")}
        cls.server = subprocess.Popen([sys.executable, str(HERE / "ui_server.py"), str(cls.port)],
                                      env={**env, "HOME": str(cls.tmp), "PATH": "/usr/bin:/bin",
                                           "PYTHONUSERBASE": site.getuserbase(), "OPENCLAVE_HOME": str(home)},
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                urllib.request.urlopen(f"{cls.base}/api/version", timeout=1)
                break
            except OSError:
                time.sleep(0.5)
        else:
            cls.server.kill()
            raise RuntimeError("UI test server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        try:
            cls.server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.server.kill()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    async def _first_run(self):
        br = Browser()
        await br.start(1400, 900)
        try:
            await br.goto(f"{self.base}/", wait=3)
            steps = await br.eval("return [...document.querySelectorAll('#welcome .fr-step')].map(s=>s.className);")
            self.assertEqual(len(steps), 3)
            self.assertTrue(all("todo" in s for s in steps), steps)
            self.assertEqual(await br.eval("return document.querySelector('#welcome .fr-btn:not(.quiet)').getAttribute('href');"),
                             "/control/#setup", "the first step is adding a model")
            self.assertEqual(await br.eval("return document.querySelector('[data-first-run=manager]').getAttribute('href');"), "/control/#manager")
            hidden = await br.eval("return ['.open-dashboard','.view-box','.clave-row'].map(s=>getComputedStyle(document.querySelector(s)).display);")
            self.assertEqual(hidden, ["none"] * 3, "nothing to open, view or organize with no agents")
            self.assertEqual(await br.eval("return document.querySelector('.add-tile').textContent.trim();"), "+New agent")
            await br.shot("first-run-desktop.png")
            self.assertEqual([l for l in br.logs if "EXC" in l], [], "no page errors")
        finally:
            await br.stop()
        br = Browser()
        await br.start(390, 844)
        try:
            await br.goto(f"{self.base}/mobile/", wait=4)
            self.assertTrue(await br.eval("return !!document.querySelector('#welcome .fr-step');"), "the same steps on the phone")
            self.assertEqual(await br.eval("return document.querySelector('[data-first-run=manager]').getAttribute('href');"), "#/create-agent/manager")
            self.assertFalse(await br.eval("return !!document.querySelector('#open-room, [data-channel-new], .clave-chip');"),
                             "no group chat, channels or Claves with no agents")
            self.assertEqual(await br.eval("return document.documentElement.scrollWidth;"), 390, "fits a phone")
            await br.shot("first-run-phone.png")
            # Straight to the manager's card in New agent.
            await br.eval("document.querySelector('[data-first-run=manager]').click(); return 1;")
            await asyncio.sleep(2)
            self.assertTrue(await br.eval("return document.activeElement === document.querySelector('.template.focused .template-add input');"),
                            "the manager's name box is ready to type in")
            self.assertEqual([l for l in br.logs if "EXC" in l], [], "no page errors")
        finally:
            await br.stop()

    def test_first_run(self):
        asyncio.run(asyncio.wait_for(self._first_run(), 120))


if __name__ == "__main__":
    unittest.main()
