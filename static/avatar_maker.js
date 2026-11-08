// Avatar maker: pick a picture (address, file, paste or drop), drag and zoom
// it into the circle, optionally cartoonify it, save it as the agent's avatar.
//
//   OpenClaveAvatarMaker.open({agentId, agentName, color, onSaved})
//
// With a colour, "Tint in agent colour" is on by default: the picture is shown
// in shades of that colour, and follows it when the colour changes later.
//
// Cropping happens here in a canvas; only the cropped square is sent to the
// server (POST /api/avatar/cartoon for a style preview, then the existing
// POST /api/agents/<id>/avatar to save). Pictures from an address are fetched
// by the server (POST /api/avatar/fetch), which only reaches the public
// internet; see avatar_maker.py.
(function () {
  'use strict';

  const STAGE = 300;     // px, the editing square
  const OUT = 384;       // px, the crop sent to the server (saved at 256)
  const STYLES = [['original', 'Original'], ['cartoon', 'Cartoon'], ['comic', 'Comic'], ['poster', 'Poster']];

  const CSS = `
  .avm-back{position:fixed;inset:0;z-index:1000;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;padding:16px}
  .avm{width:min(680px,100%);max-height:calc(100vh - 32px);overflow:auto;background:#1a1e22;color:#e7e4de;border:1px solid #30373a;border-radius:12px;padding:18px 20px;font:14px/1.5 system-ui,sans-serif;box-shadow:0 10px 40px rgba(0,0,0,.5)}
  .avm h3{margin:0 0 12px;font-size:17px}
  .avm-src{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
  .avm-src input{flex:1;min-width:220px;background:#121517;color:#e7e4de;border:1px solid #30373a;border-radius:6px;padding:7px 9px;font:inherit}
  .avm button{background:#2A2F34;color:#e7e4de;border:1px solid #30373a;border-radius:6px;padding:7px 14px;font:inherit;cursor:pointer}
  .avm button:hover{border-color:#9dd6b9}
  .avm button.primary{background:#9dd6b9;color:#121517;border-color:#9dd6b9;font-weight:600}
  .avm button:disabled{opacity:.5;cursor:default}
  .avm-hint{color:#a4aaa9;font-size:12.5px;margin:6px 0 0}
  .avm-body{display:flex;gap:20px;flex-wrap:wrap;margin-top:14px}
  .avm-stage{position:relative;width:${STAGE}px;height:${STAGE}px;flex:0 0 auto;border-radius:10px;overflow:hidden;background:#0d0f11 repeating-conic-gradient(#15191c 0 25%,#0d0f11 0 50%) 0 0/20px 20px;cursor:grab;touch-action:none;user-select:none}
  .avm-stage.dragging{cursor:grabbing}
  .avm-stage.drop{outline:2px dashed #9dd6b9;outline-offset:-6px}
  .avm-stage canvas{display:block}
  .avm-stage .avm-empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;text-align:center;padding:30px;color:#a4aaa9;font-size:13px}
  .avm-side{flex:1;min-width:220px;display:flex;flex-direction:column;gap:12px}
  .avm-zoom{display:flex;align-items:center;gap:8px;color:#a4aaa9;font-size:13px}
  .avm-zoom input{flex:1}
  .avm-styles{display:flex;gap:6px;flex-wrap:wrap}
  .avm-styles button{padding:5px 11px;font-size:13px}
  .avm-styles button.on{background:#9dd6b9;color:#121517;border-color:#9dd6b9}
  .avm-previews{display:flex;align-items:center;gap:14px}
  .avm-previews img{border-radius:50%;background:#0d0f11;display:block}
  .avm-tint{display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer}
  .avm-tint span{width:16px;height:16px;border-radius:50%;display:inline-block;border:1px solid #30373a}
  .avm-msg{min-height:20px;color:#a4aaa9;font-size:13px}
  .avm-msg.err{color:#E8877A}
  .avm-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:14px}`;

  function el(html) { const t = document.createElement('template'); t.innerHTML = html.trim(); return t.content.firstChild; }

  async function errorText(res, fallback) {
    try { return (await res.json()).detail || fallback; } catch { return fallback; }
  }

  function open({agentId, agentName = '', color = '', onSaved = () => {}}) {
    const hasColor = /^#[0-9a-f]{6}$/i.test(color);
    if (!document.getElementById('avm-css')) {
      const style = document.createElement('style');
      style.id = 'avm-css';
      style.textContent = CSS;
      document.head.appendChild(style);
    }
    const back = el(`<div class="avm-back" role="dialog" aria-modal="true" aria-label="Make avatar">
      <div class="avm">
        <h3>Avatar for ${agentName.replace(/[<&>"]/g, '')}</h3>
        <div class="avm-src">
          <input type="url" placeholder="Paste a picture address (https://…)" data-url>
          <button type="button" data-fetch>Fetch</button>
          <button type="button" data-file>Choose file…</button>
          <input type="file" accept="image/*" hidden data-input>
        </div>
        <p class="avm-hint">Or paste a picture (Ctrl+V) or drop it on the square. Then drag it into place and zoom.</p>
        <div class="avm-body">
          <div class="avm-stage" data-stage><canvas width="${STAGE}" height="${STAGE}"></canvas><div class="avm-empty">No picture yet</div></div>
          <div class="avm-side">
            <label class="avm-zoom">Zoom <input type="range" min="1" max="4" step="0.01" value="1" data-zoom disabled></label>
            <div><div class="avm-hint" style="margin:0 0 6px">Style</div>
              <div class="avm-styles">${STYLES.map(([k, l]) => `<button type="button" data-style="${k}"${k === 'original' ? ' class="on"' : ''}>${l}</button>`).join('')}</div></div>
            <label class="avm-zoom" data-strength-row hidden>Strength <input type="range" min="1" max="3" step="1" value="2" data-strength></label>
            ${hasColor ? `<label class="avm-tint"><input type="checkbox" data-tint checked> Tint in agent colour <span style="background:${color}"></span></label>` : ''}
            <div><div class="avm-hint" style="margin:0 0 6px">Preview</div>
              <div class="avm-previews"><img width="96" height="96" alt="" data-prev><img width="40" height="40" alt="" data-prev><img width="28" height="28" alt="" data-prev></div></div>
            <div class="avm-msg" data-msg></div>
          </div>
        </div>
        <div class="avm-actions"><button type="button" data-cancel>Cancel</button><button type="button" class="primary" data-save disabled>Save avatar</button></div>
      </div></div>`);
    document.body.appendChild(back);
    const q = s => back.querySelector(s);
    const stage = q('[data-stage]'), canvas = stage.querySelector('canvas'), ctx = canvas.getContext('2d');
    const zoomInput = q('[data-zoom]'), strengthInput = q('[data-strength]'), msg = q('[data-msg]'), saveBtn = q('[data-save]');
    const say = (text, err = false) => { msg.textContent = text; msg.classList.toggle('err', err); };

    let img = null;             // the loaded picture
    let base = 1, zoom = 1;     // base: scale where the picture just covers the square
    let cx = 0, cy = 0;         // picture point shown at the centre of the square
    let style = 'original';
    let result = null;          // Blob of the styled picture, untinted (what Save stores)
    const tintBox = q('[data-tint]');
    const tinted = () => !!tintBox?.checked;
    let previewSeq = 0, previewTimer = null;

    function clamp() {
      const half = STAGE / 2 / (base * zoom);
      cx = Math.min(Math.max(cx, half), img.width - half);
      cy = Math.min(Math.max(cy, half), img.height - half);
    }
    function draw() {
      ctx.clearRect(0, 0, STAGE, STAGE);
      if (!img) return;
      const s = base * zoom;
      ctx.drawImage(img, STAGE / 2 - cx * s, STAGE / 2 - cy * s, img.width * s, img.height * s);
      // Darken outside the circle the avatar is shown in.
      ctx.save();
      ctx.fillStyle = 'rgba(10,12,14,.55)';
      ctx.beginPath();
      ctx.rect(0, 0, STAGE, STAGE);
      ctx.arc(STAGE / 2, STAGE / 2, STAGE / 2 - 1, 0, Math.PI * 2, true);
      ctx.fill('evenodd');
      ctx.strokeStyle = 'rgba(157,214,185,.8)';
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(STAGE / 2, STAGE / 2, STAGE / 2 - 1, 0, Math.PI * 2);
      ctx.stroke();
      ctx.restore();
    }
    function cropBlob() {
      const out = document.createElement('canvas');
      out.width = out.height = OUT;
      const s = base * zoom * (OUT / STAGE);
      out.getContext('2d').drawImage(img, OUT / 2 - cx * s, OUT / 2 - cy * s, img.width * s, img.height * s);
      return new Promise(resolve => out.toBlob(resolve, 'image/png'));
    }
    function schedulePreview() {
      clearTimeout(previewTimer);
      previewTimer = setTimeout(preview, style === 'original' ? 60 : 250);
    }
    async function preview() {
      if (!img) return;
      const seq = ++previewSeq;
      let blob = await cropBlob(), shown;
      const server = async (query, body) => {
        const res = await fetch(`/api/avatar/cartoon?${query}`, {method: 'POST', body});
        if (!res.ok) throw new Error(await errorText(res, 'Preview failed'));
        return res.blob();
      };
      try {
        if (style !== 'original') {
          say('Cartoonifying…');
          blob = await server(`style=${style}&strength=${strengthInput.value}`, blob);
        }
        shown = tinted() ? await server(`style=original&color=${encodeURIComponent(color)}`, blob) : blob;
      } catch (error) { if (seq === previewSeq) say(error.message, true); return; }
      if (seq !== previewSeq) return;
      result = blob;
      const url = URL.createObjectURL(shown);
      back.querySelectorAll('[data-prev]').forEach(p => { if (p.src.startsWith('blob:')) URL.revokeObjectURL(p.src); p.src = url; });
      saveBtn.disabled = false;
      say('');
    }
    function load(src) {
      const picture = new Image();
      picture.onload = () => {
        img = picture;
        base = Math.max(STAGE / img.width, STAGE / img.height);
        zoom = 1; zoomInput.value = '1'; zoomInput.disabled = false;
        cx = img.width / 2; cy = img.height / 2;
        stage.querySelector('.avm-empty')?.remove();
        draw(); schedulePreview(); say('');
      };
      picture.onerror = () => say("That isn't a picture this browser can show.", true);
      picture.src = src;
    }
    function loadFile(file) {
      if (!file || !file.type.startsWith('image/')) { say('Choose a picture file.', true); return; }
      load(URL.createObjectURL(file));
    }

    // Sources
    q('[data-fetch]').onclick = async () => {
      const url = q('[data-url]').value.trim();
      if (!url) { q('[data-url]').focus(); return; }
      say('Fetching…');
      q('[data-fetch]').disabled = true;
      try {
        const res = await fetch('/api/avatar/fetch', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({url})});
        if (!res.ok) throw new Error(await errorText(res, 'Fetch failed'));
        load((await res.json()).data_url);
      } catch (error) { say(error.message, true); }
      finally { q('[data-fetch]').disabled = false; }
    };
    q('[data-url]').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); q('[data-fetch]').click(); } });
    q('[data-file]').onclick = () => q('[data-input]').click();
    q('[data-input]').onchange = e => loadFile(e.target.files[0]);
    const onPaste = e => {
      const item = [...(e.clipboardData?.items || [])].find(i => i.type.startsWith('image/'));
      if (item) { e.preventDefault(); loadFile(item.getAsFile()); return; }
      const text = e.clipboardData?.getData('text') || '';
      if (/^https?:\/\//i.test(text.trim()) && document.activeElement !== q('[data-url]')) {
        e.preventDefault(); q('[data-url]').value = text.trim(); q('[data-fetch]').click();
      }
    };
    document.addEventListener('paste', onPaste);
    stage.addEventListener('dragover', e => { e.preventDefault(); stage.classList.add('drop'); });
    stage.addEventListener('dragleave', () => stage.classList.remove('drop'));
    stage.addEventListener('drop', e => {
      e.preventDefault(); stage.classList.remove('drop');
      const file = e.dataTransfer.files?.[0];
      if (file) { loadFile(file); return; }
      const url = e.dataTransfer.getData('text/uri-list') || e.dataTransfer.getData('text/plain');
      if (url) { q('[data-url]').value = url.trim(); q('[data-fetch]').click(); }
    });

    // Drag to move, wheel or slider to zoom (around the centre).
    let drag = null;
    stage.addEventListener('pointerdown', e => {
      if (!img) return;
      drag = {x: e.clientX, y: e.clientY, cx, cy};
      stage.setPointerCapture(e.pointerId);
      stage.classList.add('dragging');
    });
    stage.addEventListener('pointermove', e => {
      if (!drag) return;
      const s = base * zoom;
      cx = drag.cx - (e.clientX - drag.x) / s;
      cy = drag.cy - (e.clientY - drag.y) / s;
      clamp(); draw();
    });
    const endDrag = () => { if (!drag) return; drag = null; stage.classList.remove('dragging'); schedulePreview(); };
    stage.addEventListener('pointerup', endDrag);
    stage.addEventListener('pointercancel', endDrag);
    const setZoom = z => {
      zoom = Math.min(4, Math.max(1, z));
      zoomInput.value = String(zoom);
      clamp(); draw(); schedulePreview();
    };
    zoomInput.addEventListener('input', () => setZoom(Number(zoomInput.value)));
    stage.addEventListener('wheel', e => { if (!img) return; e.preventDefault(); setZoom(zoom * (e.deltaY < 0 ? 1.08 : 1 / 1.08)); }, {passive: false});

    // Style
    back.querySelectorAll('[data-style]').forEach(b => b.onclick = () => {
      style = b.dataset.style;
      back.querySelectorAll('[data-style]').forEach(x => x.classList.toggle('on', x === b));
      q('[data-strength-row]').hidden = style === 'original';
      schedulePreview();
    });
    strengthInput.addEventListener('change', schedulePreview);
    tintBox?.addEventListener('change', schedulePreview);

    // Save / close
    const close = () => { document.removeEventListener('paste', onPaste); document.removeEventListener('keydown', onKey); back.remove(); };
    const onKey = e => { if (e.key === 'Escape') close(); };
    document.addEventListener('keydown', onKey);
    q('[data-cancel]').onclick = close;
    back.addEventListener('pointerdown', e => { if (e.target === back) close(); });
    saveBtn.onclick = async () => {
      if (!result) return;
      saveBtn.disabled = true;
      say('Saving…');
      try {
        const res = await fetch(`/api/agents/${encodeURIComponent(agentId)}/avatar${tinted() ? '?tint=1' : ''}`, {method: 'POST', body: result});
        if (!res.ok) throw new Error(await errorText(res, 'Save failed'));
        close();
        onSaved();
      } catch (error) { say(error.message, true); saveBtn.disabled = false; }
    };
    q('[data-url]').focus();
  }

  window.OpenClaveAvatarMaker = {open};
})();
