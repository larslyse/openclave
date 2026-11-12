// OpenClave — the one chat implementation, shared by mobile (/mobile/) and the
// desktop agent pages (dashboard_common.js): desktop and mobile chat must
// behave the same. Each UI keeps its own layout
// and CSS; everything a chat *does* lives here, so a feature can't land in one
// UI and not the other:
//   - conversation display (summary, bubbles, attachments, reply time)
//   - durable sending through /chat-requests (queue, retries, survives reload)
//   - drafts, photo attachments (upload, preview, paste)
//   - voice: record, Auto (hands-free), EN/NO, typing during a voice reply
//   - the "Working… Ns" timer with phase labels
//
// Classic script (no module syntax) so it loads the same way in the mobile
// app, in the sandboxed desktop iframe and in the trusted desktop shell.
// Exposes window.OpenClaveChat.
(function () {
  'use strict';

  // The Keeper (brand/svg): small mascot poses for working and failed replies.
  const KEEPER = pose => `<img class="keeper keeper-${pose}" src="/static/mobile/icons/keeper-${pose}.svg" alt="" width="28" height="28">`;

  // Coarse phase labels — see request_phase.py. Unknown activity falls back to "Working".
  const PHASE_LABELS = {
    retrieving: 'Looking things up', thinking: 'Thinking', 'using tools': 'Using tools',
    transcribing: 'Listening back', speaking: 'Synthesizing reply',
    // Codex route, read live from its session log (llm_client.codex_phase_for)
    'searching the web': 'Searching the web', 'reading notes': 'Reading notes',
    'writing files': 'Writing to its files', 'updating plans': 'Updating plan and backlog',
    'reading home assistant': 'Reading Home Assistant',
    'updating home assistant': 'Updating Home Assistant',
    'using home assistant': 'Using Home Assistant',
  };
  const ATTACHMENT_REF_RE = /!?\[[^\]]*\]\(attachments\/[^/]+\/[A-Za-z0-9]+\)/g;
  const VOICE_LANG_KEY = 'openclave.voiceLang';
  const VOICE_REVIEW_KEY = 'openclave.voiceReview';  // '1': check transcript before sending
  const HANDOVER_RE = /^\/handover(?:\s+(.+))?$/i;  // "/handover stash"
  const MIC_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="18" height="18"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>';
  const ATTACH_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="18" height="18"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>';

  // ── small helpers ──────────────────────────────────────────────────────

  function escapeHtml(value) {
    const node = document.createElement('div');
    node.textContent = value ?? '';
    return node.innerHTML;
  }

  function uuid() {
    if (window.crypto?.randomUUID) return window.crypto.randomUUID();
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
      const r = Math.random() * 16 | 0;
      return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
    });
  }

  // localStorage can be missing or throw (private mode, sandboxed iframe).
  function storage(ls) {
    const s = () => { try { return ls || window.localStorage; } catch { return null; } };
    return {
      get: k => { try { return s()?.getItem(k) ?? null; } catch { return null; } },
      set: (k, v) => { try { s()?.setItem(k, v); } catch {} },
      remove: k => { try { s()?.removeItem(k); } catch {} },
    };
  }

  // Cache-first open: the last few
  // conversations' saved turns are kept on the device and drawn at once,
  // then replaced by the server's copy. Only turns the server returned are
  // stored, never queued or pending messages, and the copy stays small.
  const TRANSCRIPT_CACHE_KEY = 'openclave.cache.transcripts';
  const CACHE_CONVERSATIONS = 8, CACHE_TURNS = 40, CACHE_CHARS = 60000;
  function transcriptCache(store) {
    const read = () => {
      try { const c = JSON.parse(store.get(TRANSCRIPT_CACHE_KEY) || '{}'); return c && typeof c.items === 'object' ? c : {order: [], items: {}}; }
      catch { return {order: [], items: {}}; }
    };
    return {
      get: id => read().items[id] || null,
      put(id, detail) {
        if (!id || !detail || !Array.isArray(detail.turns)) return;
        let turns = detail.turns.slice(-CACHE_TURNS);
        while (turns.length > 1 && JSON.stringify(turns).length > CACHE_CHARS) turns = turns.slice(1);
        const c = read();
        c.items[id] = {turns, summary: detail.summary || null};
        c.order = [id, ...(c.order || []).filter(x => x !== id)];
        for (const old of c.order.splice(CACHE_CONVERSATIONS)) delete c.items[old];
        store.set(TRANSCRIPT_CACHE_KEY, JSON.stringify(c));
      },
      drop(id) { const c = read(); delete c.items[id]; c.order = (c.order || []).filter(x => x !== id); store.set(TRANSCRIPT_CACHE_KEY, JSON.stringify(c)); },
    };
  }

  // Attachments: photos go to the model as images; PDFs and text files are
  // read by the server and added as text (vault.attachment_text), so every
  // agent takes them whatever its model.
  const DOC_TYPES = {pdf: 'application/pdf', txt: 'text/plain', md: 'text/markdown', markdown: 'text/markdown', csv: 'text/csv', json: 'application/json'};
  const ATTACH_ACCEPT = 'image/*,' + Object.keys(DOC_TYPES).map(e => '.' + e).join(',') + ',' + [...new Set(Object.values(DOC_TYPES))].join(',');
  function attachmentType(file) {
    if (file.type && file.type.startsWith('image/')) return file.type;
    const ext = (file.name || '').split('.').pop().toLowerCase();
    return DOC_TYPES[ext] || (Object.values(DOC_TYPES).includes(file.type) ? file.type : null);
  }
  // A sent attachment in a message bubble: the picture, or a file chip.
  function attachmentHtml(a, src) {
    if (!a.media_type || a.media_type.startsWith('image/')) return `<img class="msg-attachment" src="${src}" alt="${escapeHtml(a.filename || '')}">`;
    return `<a class="msg-file" href="${src}" target="_blank" rel="noopener">📄 ${escapeHtml(a.filename || 'file')}</a>`;
  }

  // Autojump: "on" per device; the queue of replies not yet opened.
  const AUTOJUMP_KEY = 'openclave.autojump';
  const AUTOJUMP_QUEUE_KEY = 'openclave.autojump.queue';
  const AUTOJUMP_POLL_MS = 4000, AUTOJUMP_IDLE_MS = 5000;

  const draftKey = (agentId, conversationId) => `openclave.draft.${agentId}.${conversationId || 'new'}`;
  // The spoken language: en, or the configured second one ([voice]
  // second_language, learned from /voice/languages; stored per browser).
  const voiceLang = store => (/^[a-z]{2,3}$/.test(store.get(VOICE_LANG_KEY) || '') ? store.get(VOICE_LANG_KEY) : 'en');

  async function blobToBase64(blob) {
    const bytes = new Uint8Array(await blob.arrayBuffer());
    let binary = '';
    for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
    return btoa(binary);
  }

  function base64ToBlob(base64, mimeType) {
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return new Blob([bytes], {type: mimeType});
  }

  // Downscale before upload: a full phone photo is far more resolution than a
  // vision model needs, and close-ups of plants/parts tolerate it well.
  async function downscaleImage(file, maxDim = 1280, quality = 0.82) {
    const bitmap = await createImageBitmap(file).catch(() => null);
    if (!bitmap) return file;
    const scale = Math.min(1, maxDim / Math.max(bitmap.width, bitmap.height));
    const w = Math.round(bitmap.width * scale), h = Math.round(bitmap.height * scale);
    const canvas = document.createElement('canvas');
    canvas.width = w; canvas.height = h;
    canvas.getContext('2d').drawImage(bitmap, 0, 0, w, h);
    bitmap.close?.();
    const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', quality));
    return blob || file;
  }

  function nearBottom(el) { return el.scrollHeight - el.scrollTop - el.clientHeight < 100; }
  // Mobile WebKit doesn't always finish layout for fresh HTML before the next
  // line runs; two rAFs reliably land at the true bottom.
  function scrollToBottom(el) {
    requestAnimationFrame(() => {
      el.scrollTop = el.scrollHeight;
      requestAnimationFrame(() => { el.scrollTop = el.scrollHeight; });
    });
  }
  function autosize(textarea, max = 150) {
    textarea.style.height = 'auto';
    // scrollHeight leaves out the border; with border-box sizing (desktop)
    // that 2px shortfall showed a scrollbar in an empty box.
    const borderBox = window.getComputedStyle?.(textarea).boxSizing === 'border-box';
    const border = borderBox ? (textarea.offsetHeight || 0) - (textarea.clientHeight || 0) : 0;
    textarea.style.height = `${Math.min(textarea.scrollHeight + border, max)}px`;
  }

  // Small grey line under each reply: which model answered and how long it
  // took, e.g. "GPT-6 Sol · 18s" (makes slow models
  // visible when choosing). Older replies have no model recorded.
  // Quick replies: when a reply ends its question with
  // lettered options, one per line ("A) text", "**B.** text", "- C: text"),
  // they become buttons under the last reply. Only explicit A, B, C... lists
  // starting at A count, so open questions never get invented choices; the
  // shared instructions tell agents to write choices this way.
  const CHOICE_RE = /^\s*(?:[-*]\s+)?\**\(?([A-H])[).:]\**\s+(.+?)\s*$/;
  function choicesIn(text) {
    const lines = String(text || '').split('\n').map(l => l.trim()).filter(Boolean).slice(-12);
    let best = [];
    for (let i = 0; i < lines.length; i++) {
      const run = [];
      for (let j = i; j < lines.length; j++) {
        const m = lines[j].match(CHOICE_RE);
        if (!m || m[1] !== String.fromCharCode(65 + run.length)) break;
        run.push({letter: m[1], text: m[2].replace(/\*\*/g, '').trim()});
      }
      if (run.length >= 2 && run[0].letter === 'A') best = run;
    }
    return best;
  }

  function replyMetaHtml(ms, modelName) {
    const parts = [];
    if (modelName) parts.push(escapeHtml(modelName));
    if (Number.isFinite(ms) && ms >= 0) parts.push(`${Math.max(1, Math.round(ms / 1000))}s`);
    return parts.length
      ? `<div class="reply-meta" title="Model that answered · time spent, including retrieval and tools">${parts.join(' · ')}</div>`
      : '';
  }

  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); return true; } catch {}
    // Sandboxed iframes and plain HTTP may block the async clipboard API.
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
    document.body.appendChild(area);
    area.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch {}
    area.remove();
    return ok;
  }

  // Desktop: open another agent's page on a new conversation with `text` as
  // its draft. Agent pages keep their storage under openclave.frame.<agent>.
  // in the shell (dashboard_shell.js), and "#new" makes the page start a new
  // conversation instead of opening the latest.
  function forwardToAgentPage(targetId, text) {
    try { localStorage.setItem(`openclave.frame.${targetId}.${draftKey(targetId, null)}`, text); } catch {}
    window.location.href = `/${encodeURIComponent(targetId)}#new`;
  }

  // ── audio: local (mobile, desktop shell) ───────────────────────────────
  //
  // One interface, two implementations: createLocalAudio uses this page's own
  // mic and speaker; createBridgeAudio (desktop iframe) asks the trusted shell
  // to do it, because a sandboxed iframe can't open the mic ("SecurityError:
  // Invalid security origin", tested).
  //   unlock(), startRecording(), stopRecording() -> Blob, cancelRecording(),
  //   startAuto({onUtterance(blob) -> Promise, onStatus(text)}), stopAuto(),
  //   play(base64, mime) -> Promise (resolves when playback ends or fails)

  // Levels follow the room: fixed START 8 / SILENCE 5 never see "silence"
  // over a fan or a TV, so Auto would only send at the 45 s cap. The floor is the quiet level measured while idle.
  const VAD = {
    START: 8, SILENCE: 5,       // minimum RMS of time-domain samples, 0-128 scale
    START_X: 2.5, SILENCE_X: 1.6, // ... or this many times the room's floor
    SILENCE_MS: 900,            // quiet this long ends an utterance
    MIN_SPEECH_MS: 350,         // loud time needed to count as speech; a click or tap is dropped
    MAX_RECORDING_MS: 45000,    // safety cap
    POLL_MS: 100,
  };

  function createLocalAudio({insecureHint = ''} = {}) {
    const mimeType = ['audio/mp4', 'audio/webm;codecs=opus', 'audio/webm']
      .find(t => window.MediaRecorder?.isTypeSupported?.(t)) || '';
    let player = null, unlocked = false;
    let recorder = null, recordStream = null, chunks = [];
    let auto = null;

    function getPlayer() {
      if (!player) {
        player = document.createElement('audio');
        player.setAttribute('playsinline', '');
        player.style.display = 'none';
        document.body.appendChild(player);
      }
      return player;
    }

    async function getMicStream() {
      // Secure-context-only APIs: on plain HTTP they don't exist at all.
      if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
        throw new Error(insecureHint || 'Voice needs the HTTPS address of OpenClave (microphone access is blocked on plain HTTP).');
      }
      try {
        return await navigator.mediaDevices.getUserMedia({audio: true});
      } catch (error) {
        throw new Error(`Microphone error (${error.name}): ${error.message || 'check the browser\'s microphone permission for this site.'}`);
      }
    }

    function blobFrom(parts, rec) { return new Blob(parts, {type: rec?.mimeType || mimeType || 'audio/webm'}); }

    return {
      // iOS Safari allows play() only inside a real gesture, and ties that
      // permission to the element. So unlock ONE element with a silent WAV
      // from the mic tap / Auto toggle and reuse it for every reply.
      unlock() {
        if (unlocked) return;
        unlocked = true;
        const p = getPlayer();
        p.src = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=';
        p.play().then(() => p.pause()).catch(() => { unlocked = false; });
      },
      async startRecording() {
        recordStream = await getMicStream();
        chunks = [];
        recorder = new MediaRecorder(recordStream, mimeType ? {mimeType} : undefined);
        recorder.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
        recorder.start();
      },
      stopRecording() {
        const rec = recorder, stream = recordStream;
        recorder = null; recordStream = null;
        if (!rec) return Promise.resolve(null);
        return new Promise(resolve => {
          rec.onstop = () => { stream?.getTracks().forEach(t => t.stop()); resolve(blobFrom(chunks, rec)); };
          rec.stop();
        });
      },
      cancelRecording() {
        if (recorder && recorder.state !== 'inactive') { recorder.onstop = null; recorder.stop(); }
        recordStream?.getTracks().forEach(t => t.stop());
        recorder = null; recordStream = null;
      },
      // Hands-free: volume-based voice activity detection. Thresholds are a
      // first pass, not tuned on a real phone/room.
      async startAuto({onUtterance, onStatus = () => {}}) {
        const stream = await getMicStream();
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const analyser = ctx.createAnalyser();
        analyser.fftSize = 2048;
        ctx.createMediaStreamSource(stream).connect(analyser);
        const buf = new Uint8Array(1024);
        const state = {stream, ctx, timer: null, phase: 'idle', rec: null, parts: [], speechStart: 0, silenceStart: null, voicedMs: 0, on: true, floor: null};
        const startLevel = () => Math.max(VAD.START, (state.floor || 0) * VAD.START_X);
        const silenceLevel = () => Math.max(VAD.SILENCE, (state.floor || 0) * VAD.SILENCE_X);
        auto = state;
        const volume = () => {
          analyser.getByteTimeDomainData(buf);
          let sum = 0;
          for (let i = 0; i < buf.length; i++) { const v = buf[i] - 128; sum += v * v; }
          return Math.sqrt(sum / buf.length);
        };
        const startRec = () => {
          state.parts = [];
          state.rec = new MediaRecorder(stream, mimeType ? {mimeType} : undefined);
          state.rec.ondataavailable = e => { if (e.data.size) state.parts.push(e.data); };
          state.rec.start();
          state.phase = 'recording';
          state.speechStart = Date.now();
          state.silenceStart = null;
          state.voicedMs = 0;
          onStatus('Listening…');
        };
        const stopAndSend = async () => {
          state.phase = 'processing';
          onStatus('Got it…');
          const blob = await new Promise(resolve => {
            state.rec.addEventListener('stop', () => resolve(blobFrom(state.parts, state.rec)), {once: true});
            state.rec.stop();
          });
          if (!state.on) return;  // Auto switched off meanwhile: drop it
          // Wait for the whole turn (incl. playback) so Auto never records
          // the agent's own voice coming out of the speaker.
          try { await onUtterance(blob); } catch {}
          if (state.on) { state.phase = 'idle'; onStatus('Listening for you…'); tick(); }
        };
        const tick = () => {
          if (!state.on || state.phase === 'processing') return;
          if (state.phase === 'idle') {
            const level = volume();
            if (level > startLevel()) startRec();
            // Slow-moving average of the idle level: the room's background.
            else state.floor = state.floor === null ? level : state.floor * 0.95 + level * 0.05;
          } else {
            const elapsed = Date.now() - state.speechStart;
            if (elapsed > VAD.MAX_RECORDING_MS) { stopAndSend(); return; }
            if (volume() < silenceLevel()) {
              if (state.silenceStart === null) state.silenceStart = Date.now();
              else if (Date.now() - state.silenceStart > VAD.SILENCE_MS) {
                // Only the loud time counts (the silence after a click used
                // to make it "speech", and whisper heard "Thank you" in it).
                if (state.voicedMs >= VAD.MIN_SPEECH_MS) { stopAndSend(); return; }
                state.rec.onstop = null; state.rec.stop(); state.rec = null;
                state.phase = 'idle';
                onStatus('Listening for you…');
              }
            } else {
              state.silenceStart = null;
              state.voicedMs += VAD.POLL_MS;
            }
          }
          state.timer = setTimeout(tick, VAD.POLL_MS);
        };
        onStatus('Listening for you…');
        tick();
      },
      stopAuto() {
        const state = auto;
        auto = null;
        if (!state) return;
        state.on = false;
        clearTimeout(state.timer);
        if (state.rec && state.rec.state !== 'inactive') state.rec.stop();
        state.stream.getTracks().forEach(t => t.stop());
        state.ctx.close?.();
      },
      get micActive() { return Boolean(recorder || auto); },
      play(base64, mime) {
        const p = getPlayer();
        return new Promise(resolve => {
          const url = URL.createObjectURL(base64ToBlob(base64, mime || 'audio/mpeg'));
          p.src = url;
          const done = () => { URL.revokeObjectURL(url); resolve(); };
          p.addEventListener('ended', done, {once: true});
          // If the unlock didn't take, don't hang Auto on a play that never
          // starts; the per-bubble player is still there to tap.
          p.play().catch(done);
        });
      },
    };
  }

  // ── audio: bridge (desktop iframe → trusted shell) ──────────────────────

  function createBridgeAudio(target = window.parent) {
    let seq = 0;
    const waiting = new Map();
    let autoHandlers = null;
    let micActive = false;
    window.addEventListener('message', event => {
      const msg = event.data;
      if (!msg || msg.__openclaveBridge !== true) return;
      if (msg.kind === 'audio-result') {
        const cb = waiting.get(msg.id);
        if (cb) { waiting.delete(msg.id); cb(msg); }
      } else if (msg.kind === 'audio-event' && autoHandlers) {
        if (msg.event === 'status') autoHandlers.onStatus?.(msg.text || '');
        if (msg.event === 'utterance') {
          Promise.resolve(autoHandlers.onUtterance(msg.blob)).catch(() => {}).finally(() => {
            target.postMessage({__openclaveBridge: true, kind: 'audio', op: 'utteranceDone', id: ++seq, seq: msg.seq}, '*');
          });
        }
      }
    });
    const call = (op, extra = {}) => new Promise((resolve, reject) => {
      const id = ++seq;
      waiting.set(id, msg => (msg.error ? reject(new Error(msg.error)) : resolve(msg.value)));
      target.postMessage({__openclaveBridge: true, kind: 'audio', op, id, ...extra}, '*');
    });
    return {
      unlock() { call('unlock').catch(() => {}); },
      async startRecording() { await call('startRecording'); micActive = true; },
      async stopRecording() { micActive = false; return call('stopRecording'); },
      cancelRecording() { micActive = false; call('cancelRecording').catch(() => {}); },
      async startAuto(handlers) { autoHandlers = handlers; await call('startAuto'); micActive = true; },
      stopAuto() { autoHandlers = null; micActive = false; call('stopAuto').catch(() => {}); },
      get micActive() { return micActive; },
      play(base64, mime) { return call('play', {base64, mime}).catch(() => {}); },
    };
  }

  // Shell side of the bridge. The iframe is untrusted (agent-written HTML),
  // so the shell shows its own always-visible indicator with a Stop button
  // whenever the mic is on: an agent page can't listen without the owner seeing it.
  function serveAudioBridge(iframe) {
    const audio = createLocalAudio();
    const pill = document.createElement('div');
    pill.style.cssText = 'position:fixed;top:10px;left:50%;transform:translateX(-50%);z-index:9999;display:none;align-items:center;gap:10px;padding:6px 8px 6px 12px;border-radius:999px;background:#3a1512;border:1px solid #c0392b;color:#ffd9d4;font:600 13px system-ui,sans-serif;box-shadow:0 2px 10px rgba(0,0,0,.4)';
    pill.innerHTML = '<span style="width:9px;height:9px;border-radius:50%;background:#e74c3c"></span><span data-text>Microphone on</span><button type="button" style="border:0;border-radius:999px;padding:3px 10px;background:#c0392b;color:#fff;font:inherit;cursor:pointer">Stop</button>';
    document.body.appendChild(pill);
    const showPill = text => {
      pill.style.display = text ? 'flex' : 'none';
      if (text) pill.querySelector('[data-text]').textContent = text;
    };
    let utteranceSeq = 0;
    const utteranceDone = new Map();
    const post = msg => iframe.contentWindow?.postMessage({__openclaveBridge: true, ...msg}, '*');
    pill.querySelector('button').onclick = () => {
      audio.cancelRecording();
      audio.stopAuto();
      showPill('');
      post({kind: 'audio-event', event: 'stopped'});
    };
    window.addEventListener('message', async event => {
      if (event.source !== iframe.contentWindow) return;
      const msg = event.data;
      if (!msg || msg.__openclaveBridge !== true || msg.kind !== 'audio') return;
      const respond = payload => post({kind: 'audio-result', id: msg.id, ...payload});
      try {
        switch (msg.op) {
          case 'unlock': audio.unlock(); respond({}); break;
          case 'startRecording': await audio.startRecording(); showPill('Microphone on — recording for this agent'); respond({}); break;
          case 'stopRecording': { const blob = await audio.stopRecording(); showPill(''); respond({value: blob}); break; }
          case 'cancelRecording': audio.cancelRecording(); showPill(''); respond({}); break;
          case 'startAuto':
            await audio.startAuto({
              onStatus: text => post({kind: 'audio-event', event: 'status', text}),
              onUtterance: blob => new Promise(resolve => {
                const seqNo = ++utteranceSeq;
                utteranceDone.set(seqNo, resolve);
                post({kind: 'audio-event', event: 'utterance', blob, seq: seqNo});
              }),
            });
            showPill('Microphone on — Auto is listening');
            respond({});
            break;
          case 'stopAuto': audio.stopAuto(); showPill(''); respond({}); break;
          case 'utteranceDone': { const done = utteranceDone.get(msg.seq); utteranceDone.delete(msg.seq); done?.(); break; }
          case 'play':
            if (typeof msg.base64 !== 'string' || msg.base64.length > 20_000_000) { respond({error: 'bad audio'}); break; }
            await audio.play(msg.base64, typeof msg.mime === 'string' && msg.mime.startsWith('audio/') ? msg.mime : 'audio/mpeg');
            respond({});
            break;
          default: respond({error: 'unknown audio op'});
        }
      } catch (error) {
        showPill(audio.micActive ? pill.querySelector('[data-text]').textContent : '');
        respond({error: error.message || String(error)});
      }
    });
    // Leaving or hiding the page must not leave the mic running.
    window.addEventListener('pagehide', () => { audio.cancelRecording(); audio.stopAuto(); });
    return audio;
  }

  // ── the chat ────────────────────────────────────────────────────────────

  function composerHtml(o) {
    const attach = o.canAttach ? `
      <input type="file" class="attach-input" accept="${ATTACH_ACCEPT}" multiple style="display:none">
      <button type="button" class="attach-btn" aria-label="Attach photo" title="Attach photo">${ATTACH_ICON}</button>` : '';
    const mic = o.canTalk ? `
      <button type="button" class="attach-btn mic-btn" aria-label="Record voice message" title="Record voice message">${MIC_ICON}</button>
      <label class="auto-toggle" title="Auto: listen and reply hands-free">
        <input type="checkbox" class="auto-toggle-input"><span class="auto-toggle-track"></span><span class="auto-toggle-label">Auto</span>
      </label>` : '';
    const lang = o.canTalk ? `<button type="button" class="lang-btn" aria-label="Voice language" title="Language you speak in voice messages">${voiceLang(o.store).toUpperCase()}</button>` : '';
    const reviewOn = o.store.get(VOICE_REVIEW_KEY) === '1';
    const review = o.canTalk ? `<button type="button" class="lang-btn review-btn${reviewOn ? ' on' : ''}" aria-pressed="${reviewOn}" title="Check: put what you said in the text box to fix before sending (not in Auto)">✎</button>` : '';
    const sub = lang + review + (o.subHtml || '');
    // The sub row (EN/NO, and on mobile Quick jump) runs the full width under
    // the text box and buttons, so Quick jump can be a long bar (squeezed
    // under the text box it would be ~100px on a phone).
    return `<div class="attach-preview"></div>${o.canTalk ? '<div class="auto-status"></div>' : ''}<div class="composer-row">${attach}${mic}<div class="input-col"><textarea aria-label="Message" rows="1" placeholder="Message ${escapeHtml(o.agent.name)}…"></textarea></div><button type="submit" class="send">Send</button></div>${sub ? `<div class="input-sub">${sub}</div>` : ''}`;
  }

  // o: {agent:{id,name}, conversationId, api, form, messages, scroller,
  //     markdown(text)->html, canAttach, canTalk, store, pendingKey,
  //     subHtml, enterSends, audio, emptyClass, isCurrent(), notify(name),
  //     onConversationChange(id), onReply(), onHandover(conversationId, agentHint),
  //     onAutojump(agentId, conversationId), onTitle(conversationTitle)}
  function mountChat(o) {
    const {agent, api, form, messages} = o;
    const scroller = o.scroller || messages;
    const store = o.store || storage();
    const cache = transcriptCache(store);
    const md = o.markdown || (text => `<p>${escapeHtml(text)}</p>`);
    const pendingKey = o.pendingKey || 'openclave.pending';
    const emptyClass = o.emptyClass || 'center-state';
    const canTalk = Boolean(o.canTalk && o.audio);
    let conversationId = o.conversationId || null;
    let destroyed = false;
    let workTimer = null, phaseTimer = null, pollTimer = null;
    let streamed = {id: null, text: ''};  // the running request's text so far (startWorkTimer)
    const alive = () => !destroyed && (!o.isCurrent || o.isCurrent());

    form.innerHTML = composerHtml({...o, canTalk, store});
    const q = s => form.querySelector(s);
    const textarea = q('textarea');
    const button = q('button.send');
    const preview = q('.attach-preview');

    const getPending = () => {
      try {
        const saved = JSON.parse(store.get(pendingKey) || '[]');
        return Array.isArray(saved) ? saved : saved ? [{...saved, accepted: true}] : [];
      } catch { return []; }
    };
    const setPending = request => {
      const requests = getPending().filter(r => r.requestId !== request.requestId);
      requests.push(request);
      store.set(pendingKey, JSON.stringify(requests));
    };
    const removePending = request => store.set(pendingKey,
      JSON.stringify(getPending().filter(r => r.requestId !== request.requestId)));
    const requestsHere = () => getPending().filter(r => r.agentId === agent.id &&
      (!conversationId || r.conversationId === conversationId));

    // Draft
    let key = draftKey(agent.id, conversationId);

    // A new chat gets its id from the first send/upload/voice reply; the
    // draft being typed moves to the new key with it.
    function setConversation(id) {
      if (!id || id === conversationId) return;
      conversationId = id;
      const oldKey = key;
      key = draftKey(agent.id, id);
      if (textarea.value) store.set(key, textarea.value);
      store.remove(oldKey);
      o.onConversationChange?.(id);
    }
    textarea.value = store.get(key) || '';
    autosize(textarea);
    textarea.addEventListener('input', () => { store.set(key, textarea.value); autosize(textarea); });
    if (o.enterSends) {
      textarea.addEventListener('keydown', e => {
        if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
      });
    }

    // Messages
    function messageHtml(turn) {
      if (turn.role === 'user') {
        const thumbs = (turn.attachments || [])
          .map(a => attachmentHtml(a, `/${agent.id}/conversations/${conversationId}/attachments/${a.id}`))
          .join('');
        const text = turn.content.replace(ATTACHMENT_REF_RE, '').trim();
        // Another agent's message (agent_messages.py), not the owner's.
        if (turn.from_agent) {
          return `<div class="bubble from-agent"><div class="from-label">From ${escapeHtml(turn.from_name || turn.from_agent)}</div>${md(text)}</div>`;
        }
        return `<div class="bubble user">${thumbs}${text ? escapeHtml(text) : ''}</div>`;
      }
      return `<div class="bubble assistant">${md(turn.content)}${replyMetaHtml(turn.elapsed_ms, turn.model_name)}</div>`;
    }
    const emptyHtml = () => `<div class="${emptyClass}">Start a conversation with ${escapeHtml(agent.name)}.</div>`;
    let detail = {turns: [], summary: null};
    // After the first exchange the server names the chat in the background
    // (conversation_titles.schedule), a few seconds after the reply: look
    // again so the header shows the short name, not the first message.
    function watchAutoName() {
      if (!o.onTitle || !conversationId) return;
      const id = conversationId, before = detail.title;
      [4000, 12000].forEach(ms => setTimeout(async () => {
        if (!alive() || conversationId !== id) return;
        try {
          const fresh = await api(`/${agent.id}/conversations/${id}`);
          if (alive() && conversationId === id && fresh.title && fresh.title !== before) {
            detail.title = fresh.title;
            o.onTitle(fresh.title);
          }
        } catch { /* the next refresh catches up */ }
      }, ms));
    }

    // An archived chat opened from Recaps (recaps.py): its card on top, the
    // transcript below. The next message brings it back into the chat list
    // (the server restores it), so there's nothing else to press.
    const RECAP_LABEL = {concluded: 'Concluded', open: 'Left open', dropped: 'Dropped'};
    function recapHtml() {
      if (!detail.archived) return '';
      const r = detail.recap || {};
      const when = r.started && r.last_active && r.started !== r.last_active ? `${r.started} → ${r.last_active}` : (r.last_active || '');
      return `<div class="recap-banner">
        <div class="recap-banner-head"><span class="recap-status ${escapeHtml(r.status || 'none')}">${escapeHtml(RECAP_LABEL[r.status] || 'No recap yet')}</span><span class="recap-when">Archived${when ? ` · ${escapeHtml(when)}` : ''}</span></div>
        ${r.about ? `<p class="recap-about">${escapeHtml(r.about)}</p>` : '<p class="recap-about"><button type="button" class="recap-write">Write recap</button></p>'}
        ${r.open_ends ? `<p class="recap-open"><b>Open:</b> ${escapeHtml(r.open_ends)}</p>` : ''}
        <p class="recap-hint">Write a message to pick this chat up again: it moves back to your chats.</p></div>`;
    }
    // A purged chat (purge.py) says so, with Undo.
    function purgedHtml() {
      return detail.purged ? '<div class="purged-banner">Purged: only the summary is kept. <button type="button" class="link-btn purge-undo">Undo</button></div>' : '';
    }
    function wirePurged() {
      const undo = messages.querySelector('.purge-undo');
      if (!undo) return;
      undo.onclick = async () => {
        undo.disabled = true;
        try {
          await api(`/${agent.id}/conversations/${conversationId}/purge/undo`, {method: 'POST'});
          detail = await api(`/${agent.id}/conversations/${conversationId}`);
          if (alive()) renderDetail();
        } catch (error) { undo.disabled = false; undo.textContent = error.message || 'Could not undo'; }
      };
    }
    function wireRecap() {
      wirePurged();
      const button = messages.querySelector('.recap-write');
      if (!button) return;
      button.onclick = async () => {
        button.disabled = true; button.textContent = 'Writing…';
        try {
          await api(`/${agent.id}/conversations/${conversationId}/recap`, {method: 'POST'});
          detail = await api(`/${agent.id}/conversations/${conversationId}`);
          if (alive()) renderDetail();
        } catch (error) { button.disabled = false; button.textContent = error.message || 'Could not write it'; }
      };
    }

    function showQuickReplies() {
      const last = detail.turns[detail.turns.length - 1];
      const choices = last && last.role !== 'user' && !detail.archived ? choicesIn(last.content) : [];
      const bubbles = messages.querySelectorAll('.bubble.assistant');
      const bubble = bubbles[bubbles.length - 1];
      if (!choices.length || !bubble) return;
      bubble.insertAdjacentHTML('beforeend', `<div class="quick-replies" role="group" aria-label="Quick replies">${choices.map(c =>
        `<button type="button" class="retry-btn quick-reply" data-choice="${escapeHtml(c.letter + ') ' + c.text)}" title="${escapeHtml(c.text)}"><b>${c.letter}</b> ${escapeHtml(c.text.length > 60 ? c.text.slice(0, 57) + '…' : c.text)}</button>`).join('')}</div>`);
      bubble.querySelectorAll('.quick-reply').forEach(btn => btn.addEventListener('click', () => {
        if (submitting) return;
        textarea.value = btn.dataset.choice;
        bubble.querySelector('.quick-replies')?.remove();
        submit();
      }));
    }

    function renderDetail() {
      if (conversationId && !detail.cached && !detail.archived) cache.put(conversationId, detail);
      if (detail.title) o.onTitle?.(detail.title);
      messages.innerHTML = `${recapHtml()}${purgedHtml()}${detail.summary ? `<details class="summary"><summary>Earlier conversation summary</summary><div>${escapeHtml(detail.summary)}</div></details>` : ''}${detail.turns.map(messageHtml).join('') || emptyHtml()}`;
      wireRecap();
      showQuickReplies();
      // Desktop chat lives in a sandboxed iframe. External sites cannot load
      // inside it, so a user click must navigate the real tab instead.
      messages.querySelectorAll('.bubble.assistant a[href]').forEach(link => {
        try {
          if (!/^https?:$/.test(new URL(link.href, location.href).protocol)) return;
          link.target = o.externalLinkTarget || '_blank';
          link.rel = 'noopener noreferrer';
        } catch (_) { /* Leave malformed links untouched. */ }
      });
    }
    const clearEmpty = () => messages.querySelector(`.${emptyClass}`)?.remove();

    // Working timer
    function stopWorkTimer() {
      clearInterval(workTimer); workTimer = null;
      clearInterval(phaseTimer); phaseTimer = null;
    }
    function startWorkTimer(request) {
      stopWorkTimer();
      let bubble = messages.querySelector('.pending');
      if (!bubble) {
        bubble = document.createElement('div');
        bubble.className = 'bubble assistant pending';
        messages.appendChild(bubble);
      }
      bubble.setAttribute('aria-live', 'off');
      // Durable requests can be stopped; voice turns (no requestId) can't.
      // .stream-text: the reply so far while an API route streams it.
      bubble.innerHTML = `<div class="stream-text" hidden></div>${KEEPER('thinking')}<span class="pending-label"></span>${request.requestId ? ' <button type="button" class="stop-btn" title="Stop this reply">Stop</button>' : ''}`;
      const streamEl = bubble.querySelector('.stream-text');
      let shown = '';
      const showText = text => {
        if (!text || text === shown || !bubble.isConnected) return;
        const atBottom = nearBottom(scroller);
        shown = text;
        if (request.requestId) streamed = {id: request.requestId, text};
        streamEl.innerHTML = md(text);
        streamEl.hidden = false;
        bubble.classList.add('streaming');
        if (atBottom) scrollToBottom(scroller);
      };
      // monitor() rebuilds this bubble every poll: put back what was already
      // streamed at once, or the text blanks until the next phase reply.
      if (request.requestId && streamed.id === request.requestId) showText(streamed.text);
      const stopBtn = bubble.querySelector('.stop-btn');
      if (stopBtn) stopBtn.onclick = () => { stopBtn.disabled = true; cancelRequest(request); };
      const label = bubble.querySelector('.pending-label') || bubble;
      const update = () => {
        if (!bubble.isConnected) { stopWorkTimer(); return; }
        const text = PHASE_LABELS[request.phase] || 'Working';
        label.textContent = Number.isFinite(request.startedAt)
          ? `${text}… ${Math.max(0, Math.floor((Date.now() - request.startedAt) / 1000))}s`
          : `${text}…`;
      };
      update();
      workTimer = setInterval(update, 250);
      const pollPhase = async () => {
        try {
          const state = await api(`/${agent.id}/phase`);
          request.phase = state.phase;
          showText(state.text);
        } catch { /* cosmetic only */ }
      };
      pollPhase();
      phaseTimer = setInterval(pollPhase, 700);
    }

    // Status line above the composer
    let statusEl = null;
    let submitting = false, monitoring = false, voiceTurnActive = false, notice = '';
    const heldMessages = [];
    const failed = []; // {request, error}: shown with a Retry button until retried
    function setBusy(busy, label = '') {
      textarea.disabled = busy; button.disabled = busy;
      statusEl?.remove(); statusEl = null;
      if (label) {
        statusEl = document.createElement('div');
        statusEl.className = 'status chat-status';
        statusEl.textContent = label;
        form.parentNode?.insertBefore(statusEl, form);
      }
    }

    // Attachments
    let pendingAttachments = []; // {id, previewUrl, uploading, failed}
    function renderPreview() {
      if (!preview) return;
      preview.innerHTML = pendingAttachments.map((a, i) => `
        <div class="attach-thumb${a.uploading ? ' uploading' : ''}${a.failed ? ' failed' : ''}${a.media_type?.startsWith('image/') ? '' : ' file'}" data-idx="${i}" title="${escapeHtml(a.filename || '')}">
          ${a.media_type?.startsWith('image/') ? `<img src="${a.previewUrl}">` : `<span class="attach-file">📄<small>${escapeHtml((a.filename || '').slice(0, 14))}</small></span>`}
          ${a.uploading ? '' : `<button type="button" class="attach-remove" data-idx="${i}" aria-label="Remove">×</button>`}
        </div>`).join('');
      preview.querySelectorAll('.attach-remove').forEach(btn => btn.onclick = () => {
        const idx = Number(btn.dataset.idx);
        URL.revokeObjectURL(pendingAttachments[idx].previewUrl);
        pendingAttachments.splice(idx, 1);
        renderPreview();
      });
    }
    async function uploadAttachment(file) {
      const type = attachmentType(file);
      const blob = type.startsWith('image/') ? await downscaleImage(file) : file;
      const body = {filename: file.name || 'photo.jpg', media_type: type.startsWith('image/') ? (blob.type || 'image/jpeg') : type, data_base64: await blobToBase64(blob)};
      if (conversationId) body.conversation_id = conversationId;
      return api(`/${agent.id}/attachments`, {method: 'POST', body: JSON.stringify(body)});
    }
    async function addFiles(files) {
      for (const file of files) {
        const type = attachmentType(file);
        if (!type) { setBusy(false, `${file.name || 'That file'}: only photos, PDFs and text files can be attached.`); continue; }
        const entry = {id: null, previewUrl: URL.createObjectURL(file), uploading: true, failed: false, media_type: type, filename: file.name};
        pendingAttachments.push(entry);
        renderPreview();
        try {
          const result = await uploadAttachment(file);
          entry.id = result.attachment.id;
          entry.uploading = false;
          setConversation(result.conversation_id);
        } catch {
          entry.uploading = false;
          entry.failed = true;
        }
        renderPreview();
      }
    }
    if (o.canAttach) {
      const attachInput = q('.attach-input');
      q('.attach-btn:not(.mic-btn)').onclick = () => attachInput.click();
      attachInput.addEventListener('change', () => { addFiles([...attachInput.files]); attachInput.value = ''; });
      textarea.addEventListener('paste', event => {
        const files = [...(event.clipboardData?.items || [])]
          .filter(item => item.type.startsWith('image/'))
          .map(item => item.getAsFile())
          .filter(Boolean);
        if (files.length) addFiles(files);
      });
    }

    // Durable sending
    async function accept(request) {
      sentHere = true;  // Autojump: The owner is done here and waiting
      const result = await api(`/${agent.id}/chat-requests`, {method: 'POST', body: JSON.stringify({
        request_id: request.requestId, conversation_id: request.conversationId,
        message: request.message, attachments: request.attachments || [],
      })});
      request.conversationId = result.conversation_id;
      request.accepted = true;
      setPending(request);
      setConversation(result.conversation_id);
      return result;
    }

    async function monitor() {
      if (monitoring || !alive()) return;
      monitoring = true;
      clearTimeout(pollTimer);
      let finished = false;
      try {
        const states = [];
        for (const request of requestsHere()) {
          let status;
          try {
            status = request.accepted
              // watching=1: the server skips the reply push while this is on screen.
              ? await api(`/${agent.id}/chat-requests/${request.requestId}${document.hidden ? '' : '?watching=1'}`)
              : await accept(request);
          } catch (error) {
            if (error.status === 404) status = await accept(request);
            else if ([400, 409, 422].includes(error.status)) {
              removePending(request);
              notice = error.message;
              continue;
            } else throw error;
          }
          if (['completed', 'failed', 'interrupted', 'cancelled'].includes(status.status)) {
            removePending(request);
            finished = true;
            if (status.status === 'completed') o.notify?.(agent.name);
            else if (status.status !== 'cancelled') failed.push({request, error: status.error || `Request ${status.status}.`});
          } else states.push({request, status});
        }
        if (!alive()) return;
        if (conversationId) {
          try { detail = await api(`/${agent.id}/conversations/${conversationId}`); }
          catch (error) { if (error.status !== 404) throw error; }
        }
        if (!alive()) return;
        const atBottom = nearBottom(scroller);
        stopWorkTimer();
        renderDetail();
        if (finished && detail.turns.length === 2) watchAutoName();
        if (!detail.turns.length && states.length) clearEmpty();
        for (const {request, status} of states) {
          if (status.status === 'queued') {
            const thumbs = (request.attachments || []).map(id => attachmentHtml((request.attachmentMeta || {})[id] || {}, `/${agent.id}/conversations/${request.conversationId}/attachments/${id}`)).join('');
            messages.insertAdjacentHTML('beforeend', `<div class="bubble user">${thumbs}${escapeHtml(request.message)}<div class="work-time">Queued · <button type="button" class="link-btn" data-cancel-request="${escapeHtml(request.requestId)}">Cancel</button></div></div>`);
          }
        }
        messages.querySelectorAll('[data-cancel-request]').forEach(btn => btn.onclick = () => {
          const found = states.find(s => s.request.requestId === btn.dataset.cancelRequest);
          if (found) { btn.disabled = true; cancelRequest(found.request); }
        });
        renderFailed();
        const running = states.find(s => s.status.status === 'running');
        if (running) startWorkTimer(running.request);
        if (atBottom) scrollToBottom(scroller);
        const count = states.filter(s => s.status.status === 'queued').length;
        // Updating replies must not reset the next photo or text being composed.
        if (!submitting && !voiceTurnActive) setBusy(false, notice || (count ? `${count} queued — new messages will be queued` : running ? 'Working — new messages will be queued' : ''));
        if (finished) o.onReply?.();
      } catch {
        if (alive() && !submitting) setBusy(false, 'Connection lost. Your queued messages will retry.');
      } finally {
        monitoring = false;
        if (alive() && requestsHere().length) pollTimer = setTimeout(monitor, 2000);
      }
    }

    // Stop a running reply (CLI routes) or cancel a queued one. API routes
    // can't be interrupted; the reply then finishes and the owner is told so.
    async function cancelRequest(request) {
      try {
        const result = await api(`/${agent.id}/chat-requests/${request.requestId}/cancel`, {method: 'POST'});
        notice = result.stoppable ? '' : "Nothing to stop: the reply is just finishing.";
      } catch (error) {
        if (error.status === 404) removePending(request);
        notice = error.message || 'Could not stop it.';
      }
      if (!alive()) return;
      setBusy(false, notice);
      monitor();
    }

    // A failed or interrupted reply gets an error bubble with Retry, which
    // sends the same message (and photos) again into this conversation.
    function renderFailed() {
      for (const f of failed) {
        messages.insertAdjacentHTML('beforeend', `<div class="bubble assistant error">${KEEPER('error')}${escapeHtml(f.error)}<div class="retry-row"><button type="button" class="retry-btn" data-retry="${escapeHtml(f.request.requestId)}">Retry</button></div></div>`);
      }
      messages.querySelectorAll('[data-retry]').forEach(btn => btn.onclick = () => retry(btn.dataset.retry));
    }
    function retry(requestId) {
      const i = failed.findIndex(f => f.request.requestId === requestId);
      if (i < 0) return;
      const {request} = failed.splice(i, 1)[0];
      setPending({...request, requestId: uuid(), conversationId: conversationId || request.conversationId,
        accepted: false, startedAt: Date.now()});
      notice = '';
      monitor();
    }

    // Messages typed during a voice turn go out, in order, into the voice
    // conversation once its reply text is back. In a brand-new chat whose
    // voice turn failed there's no conversation yet: the first creates it.
    async function flushHeld() {
      const held = heldMessages.splice(0);
      if (!held.length || !alive()) return;
      for (const request of held) { request.conversationId = conversationId; setPending(request); }
      if (!conversationId) {
        try { await accept(held[0]); } catch { /* monitor() retries it */ }
        for (const request of held.slice(1)) { request.conversationId = conversationId; setPending(request); }
      }
      await monitor();
    }

    function clearComposer() {
      store.remove(key);
      key = draftKey(agent.id, conversationId);
      textarea.value = '';
      autosize(textarea);
      pendingAttachments = [];
      renderPreview();
    }

    async function submit() {
      const text = textarea.value.trim();
      const attachmentIds = pendingAttachments.filter(a => a.id && !a.failed).map(a => a.id);
      if (submitting || (!text && !attachmentIds.length) || button.disabled || pendingAttachments.some(a => a.uploading)) return;
      // Chat commands (vault: Build Docs/Chat commands.md). Never sent to the agent.
      const command = !attachmentIds.length && text.match(HANDOVER_RE);
      if (command) {
        if (!o.onHandover) { setBusy(false, 'Hand over from the agent\'s own page or the mobile app.'); return; }
        if (!conversationId) { setBusy(false, 'Nothing to hand over yet: this chat is empty.'); return; }
        clearComposer();
        setBusy(false, '');
        o.onHandover(conversationId, command[1] || '');
        return;
      }
      if (reviewing && speakChecked && text && !attachmentIds.length && !voiceTurnActive) {
        reviewing = false;
        clearComposer();
        speakChecked(text);
        return;
      }
      reviewing = false;
      const request = {requestId: uuid(), agentId: agent.id, conversationId, message: text,
        attachments: attachmentIds, startedAt: Date.now(), accepted: false,
        attachmentMeta: Object.fromEntries(pendingAttachments.filter(a => a.id && !a.failed).map(a => [a.id, {media_type: a.media_type, filename: a.filename}]))};
      if (voiceTurnActive) {
        heldMessages.push(request);
        const thumbs = pendingAttachments.filter(a => a.id && !a.failed).map(a => attachmentHtml(a, a.previewUrl)).join('');
        clearEmpty();
        messages.insertAdjacentHTML('beforeend', `<div class="bubble user">${thumbs}${escapeHtml(text)}<div class="work-time">Queued</div></div>`);
        scrollToBottom(scroller);
        clearComposer();
        return;
      }
      submitting = true;
      notice = '';
      setPending(request);
      setBusy(true, 'Sending…');
      try {
        await accept(request);
        if (!alive()) return;
        pendingAttachments.forEach(a => URL.revokeObjectURL(a.previewUrl));
        clearComposer();
      } catch (error) {
        if ([400, 409, 422].includes(error.status)) {
          removePending(request);
          notice = error.message;
        } else {
          // Saved payload includes photo ids so delivery can retry after reload.
          notice = 'Delivery uncertain. Retrying safely…';
          clearComposer();
        }
      } finally {
        submitting = false;
        if (alive()) {
          setBusy(false, notice);
          await monitor();
        }
      }
    }
    if (form.tagName === 'FORM') form.addEventListener('submit', event => { event.preventDefault(); submit(); });
    else button.addEventListener('click', event => { event.preventDefault(); submit(); });

    // Voice
    let recording = false, autoOn = false, onShellStop = null, recordTimer = null;
    let reviewing = false, speakChecked = null;  // Check mode: next Send speaks the reply
    if (canTalk) {
      const audio = o.audio;
      const micBtn = q('.mic-btn');
      const autoInput = q('.auto-toggle-input');
      const autoStatus = q('.auto-status');
      const langBtn = q('.lang-btn');
      const setAutoStatus = text => {
        if (autoStatus) autoStatus.innerHTML = text ? `<span class="listening-dot"></span>${escapeHtml(text)}` : '';
      };
      const reviewBtn = q('.review-btn');
      if (reviewBtn) reviewBtn.onclick = () => {
        const on = store.get(VOICE_REVIEW_KEY) !== '1';
        store.set(VOICE_REVIEW_KEY, on ? '1' : '0');
        reviewBtn.classList.toggle('on', on);
        reviewBtn.setAttribute('aria-pressed', String(on));
      };
      speakChecked = text => sendVoiceTurn(null, {given: text});
      let languages = ['en', 'no'];  // until the server says
      langBtn.onclick = () => {
        const next = languages[(languages.indexOf(voiceLang(store)) + 1) % languages.length] || 'en';
        store.set(VOICE_LANG_KEY, next);
        langBtn.textContent = next.toUpperCase();
      };
      api(`/${agent.id}/voice/languages`).then(data => {
        if (!Array.isArray(data?.languages) || !data.languages.length) return;
        languages = data.languages;
        if (!languages.includes(voiceLang(store))) store.set(VOICE_LANG_KEY, 'en');
        langBtn.textContent = voiceLang(store).toUpperCase();
        langBtn.hidden = languages.length < 2;  // English only: no switch
      }).catch(() => {});

      // Returns once the whole turn (incl. reply playback) is done — Auto
      // awaits it so it never records the agent's own voice.
      // blob: a recording to transcribe. given: an already-checked transcript
      // (Check mode), which skips transcription. review: transcribe only and
      // put the text in the box for the owner to fix, then Send speaks the reply.
      // auto: from Auto mode; dropped silently if nothing was heard or Auto
      // was switched off before the transcript came back.
      async function sendVoiceTurn(blob, {given = null, review = false, auto = false} = {}) {
        if ((!blob && !given) || !alive()) return;
        // The text box stays usable during a voice turn: typed
        // messages are held until the reply text is back (see flushHeld);
        // sending them straight away could take the agent's lock first (the
        // voice reply then fails as busy) or start a second conversation.
        voiceTurnActive = true;
        setBusy(false, 'Voice reply on the way — typed messages are sent after it');
        clearEmpty();
        const request = {startedAt: Date.now()};
        startWorkTimer(request);
        scrollToBottom(scroller);
        try {
          // Transcribe first so the owner sees what they said before the reply.
          const language = voiceLang(store);
          let transcript = given;
          if (!transcript) {
            const transcribeBody = {audio_base64: await blobToBase64(blob), media_type: blob.type || 'audio/webm', language};
            ({transcript} = await api(`/${agent.id}/voice/transcribe`, {method: 'POST', body: JSON.stringify(transcribeBody)}));
          }
          if (!alive()) return;
          if (!transcript || (auto && !autoOn)) {
            stopWorkTimer();
            messages.querySelector('.pending')?.remove();
            if (!transcript && !auto) messages.insertAdjacentHTML('beforeend', `<div class="bubble assistant error">Didn't catch any speech — try again.</div>`);
            voiceTurnActive = false;
            flushHeld();
            return;
          }
          if (review) {
            stopWorkTimer();
            messages.querySelector('.pending')?.remove();
            textarea.value = textarea.value.trim() ? `${textarea.value.trim()} ${transcript}` : transcript;
            store.set(key, textarea.value);
            autosize(textarea);
            textarea.focus();
            reviewing = true;
            voiceTurnActive = false;
            setBusy(false, 'Check the text, then Send: the reply will be spoken.');
            return;
          }
          const said = `<div class="bubble user">${escapeHtml(transcript)}</div>`;
          const pendingBubble = messages.querySelector('.pending');
          if (pendingBubble) pendingBubble.insertAdjacentHTML('beforebegin', said);
          else messages.insertAdjacentHTML('beforeend', said);
          scrollToBottom(scroller);

          const replyBody = {transcript, language};
          if (conversationId) replyBody.conversation_id = conversationId;
          const result = await api(`/${agent.id}/voice/reply`, {method: 'POST', body: JSON.stringify(replyBody)});
          stopWorkTimer();
          messages.querySelector('.pending')?.remove();
          setConversation(result.conversation_id);
          if (!alive()) return;
          const meta = replyMetaHtml(result.elapsed_ms, result.model_name);
          // audio_base64 can be missing: a TTS failure is non-fatal and the
          // reply text still arrives. The per-bubble <audio controls> is for
          // manual replay; automatic playback goes through `audio`.
          if (result.audio_base64) {
            const url = URL.createObjectURL(base64ToBlob(result.audio_base64, result.audio_mime || 'audio/mpeg'));
            messages.insertAdjacentHTML('beforeend', `<div class="bubble assistant">${md(result.reply)}<audio controls preload="auto" class="voice-replay" src="${url}"></audio>${meta}</div>`);
          } else {
            messages.insertAdjacentHTML('beforeend', `<div class="bubble assistant">${md(result.reply)}<div class="status">(voice reply unavailable — text only)</div>${meta}</div>`);
          }
          scrollToBottom(scroller);
          // Reply text is in: held messages can go now, before playback ends.
          voiceTurnActive = false;
          flushHeld();
          o.onReply?.();
          if (result.audio_base64) await audio.play(result.audio_base64, result.audio_mime || 'audio/mpeg');
        } catch (error) {
          stopWorkTimer();
          messages.querySelector('.pending')?.remove();
          messages.insertAdjacentHTML('beforeend', `<div class="bubble assistant error">${escapeHtml(error.message || 'Voice message failed — try again.')}</div>`);
          scrollToBottom(scroller);
          voiceTurnActive = false;
          flushHeld();
        } finally {
          voiceTurnActive = false;
          // Held messages may be running now; monitor() owns the status then.
          if (alive() && !requestsHere().length && !reviewing) setBusy(false);
        }
      }

      // While recording: "Recording 0:04 — tap the mic again to send".
      const startRecordTimer = () => {
        const started = Date.now();
        const tick = () => {
          const secs = Math.floor((Date.now() - started) / 1000);
          setAutoStatus(`Recording ${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, '0')} — tap the mic again to send`);
        };
        tick();
        recordTimer = setInterval(tick, 500);
      };
      const stopRecordTimer = () => { clearInterval(recordTimer); recordTimer = null; setAutoStatus(''); };

      micBtn.onclick = async () => {
        audio.unlock();
        if (recording) {
          recording = false;
          micBtn.classList.remove('recording');
          stopRecordTimer();
          const blob = await audio.stopRecording();
          sendVoiceTurn(blob, {review: store.get(VOICE_REVIEW_KEY) === '1'});
          return;
        }
        try {
          await audio.startRecording();
          recording = true;
          micBtn.classList.add('recording');
          startRecordTimer();
        } catch (error) { setBusy(false, error.message); }
      };

      autoInput.onchange = async () => {
        audio.unlock();
        if (autoInput.checked) {
          try {
            await audio.startAuto({onUtterance: blob => sendVoiceTurn(blob, {auto: true}), onStatus: setAutoStatus});
            autoOn = true;
            micBtn.disabled = true;
            micBtn.style.opacity = '.4';
          } catch (error) {
            autoInput.checked = false;
            setBusy(false, error.message);
          }
        } else {
          audio.stopAuto();
          autoOn = false;
          micBtn.disabled = false;
          micBtn.style.opacity = '';
          setAutoStatus('');
        }
      };
      // The desktop shell's own Stop button turns the mic off from outside.
      onShellStop = event => {
        const msg = event.data;
        if (!alive() || !msg || msg.__openclaveBridge !== true || msg.kind !== 'audio-event' || msg.event !== 'stopped') return;
        recording = false; autoOn = false;
        clearInterval(recordTimer); recordTimer = null;
        micBtn.classList.remove('recording');
        autoInput.checked = false;
        micBtn.disabled = false; micBtn.style.opacity = '';
        setAutoStatus('');
      };
      window.addEventListener('message', onShellStop);
    }

    // Message actions: right-click (mouse) or long-press (touch) on a bubble
    // opens Copy / Send to another agent (o.onForward decides how to get
    // there: mobile route, desktop agent page). Selecting part of a message
    // must keep working: with a selection inside the
    // bubble, right-click leaves the browser's own menu alone; on touch the
    // phone's own selection and its Copy callout run, and our menu sits
    // under the bubble with "Copy selection" first so it never covers it.
    let menuEl = null, pressTimer = null, pressStart = null, lastPointer = 'mouse';
    const bubbleText = bubble => {
      const clone = bubble.cloneNode(true);
      clone.querySelectorAll('.reply-meta, .work-time, audio, .retry-row, .quick-replies, .status').forEach(e => e.remove());
      return (clone.innerText || clone.textContent || '').trim();
    };
    const selectionIn = bubble => {
      const sel = window.getSelection?.();
      if (!sel || sel.isCollapsed || !sel.rangeCount) return '';
      const range = sel.getRangeAt(0);
      if (!bubble.contains(range.commonAncestorContainer) && !range.intersectsNode(bubble)) return '';
      return sel.toString().trim();
    };
    function closeMenu() { menuEl?.remove(); menuEl = null; }
    function openMenu(bubble, x, y, below) {
      closeMenu();
      const text = bubbleText(bubble);
      if (!text) return;
      const picked = selectionIn(bubble);
      menuEl = document.createElement('div');
      menuEl.className = 'msg-menu';
      menuEl.innerHTML = `${picked ? '<button type="button" data-act="copy-sel">Copy selection</button>' : ''}<button type="button" data-act="copy">${picked ? 'Copy whole message' : 'Copy'}</button>${o.onForward ? '<button type="button" data-act="forward">Send to another agent…</button>' : ''}`;
      if (below) {
        // Touch: under the bubble (above it when there's no room), not on the text.
        const r = bubble.getBoundingClientRect();
        y = r.bottom + 6 + 150 > window.innerHeight ? r.top - 6 - 150 : r.bottom + 6;
      }
      menuEl.style.left = `${Math.max(8, Math.min(x, window.innerWidth - 230))}px`;
      menuEl.style.top = `${Math.max(8, Math.min(y, window.innerHeight - 260))}px`;
      document.body.appendChild(menuEl);
      // Tapping a menu button must not clear the selection it copies.
      menuEl.addEventListener('pointerdown', event => event.preventDefault());
      menuEl.addEventListener('mousedown', event => event.preventDefault());
      const copyBtn = (act, value) => {
        const btn = menuEl.querySelector(`[data-act=${act}]`);
        if (btn) btn.onclick = async event => {
          const ok = await copyText(value);
          event.target.textContent = ok ? 'Copied' : 'Copy failed';
          setTimeout(closeMenu, 700);
        };
      };
      copyBtn('copy', text);
      copyBtn('copy-sel', picked);
      const fwd = menuEl.querySelector('[data-act=forward]');
      if (fwd) fwd.onclick = async () => {
        try {
          const targets = await api(`/${agent.id}/move-targets`);
          if (!menuEl) return;
          menuEl.innerHTML = targets.map(t => `<button type="button" data-target="${escapeHtml(t.id)}">${escapeHtml(t.name)}</button>`).join('');
          menuEl.querySelectorAll('[data-target]').forEach(b => b.onclick = () => {
            closeMenu();
            o.onForward(b.dataset.target, `From ${agent.name}:\n${text}`);
          });
        } catch { closeMenu(); }
      };
    }
    const bubbleAt = target => target?.closest?.('.bubble:not(.pending)');
    const onContextMenu = event => {
      const bubble = bubbleAt(event.target);
      if (!bubble) return;
      // A long-press on a phone also fires contextmenu; blocking it would stop
      // the phone selecting text. The press timer opens our menu instead.
      if ((event.pointerType || lastPointer) === 'touch') return;
      if (selectionIn(bubble)) return; // the browser's menu copies the selection
      event.preventDefault();
      openMenu(bubble, event.clientX, event.clientY);
    };
    const onPointerDown = event => {
      lastPointer = event.pointerType || 'mouse';
      if (menuEl && !menuEl.contains(event.target)) closeMenu();
      if (event.pointerType !== 'touch') return;
      const bubble = bubbleAt(event.target);
      if (!bubble) return;
      pressStart = {x: event.clientX, y: event.clientY};
      clearTimeout(pressTimer);
      pressTimer = setTimeout(() => openMenu(bubble, pressStart.x, pressStart.y, true), 550);
    };
    const onPointerMove = event => {
      if (pressStart && Math.hypot(event.clientX - pressStart.x, event.clientY - pressStart.y) > 10) clearTimeout(pressTimer);
    };
    const onPointerEnd = () => clearTimeout(pressTimer);
    const onDocDown = event => { if (menuEl && !menuEl.contains(event.target) && !messages.contains(event.target)) closeMenu(); };
    messages.addEventListener('contextmenu', onContextMenu);
    messages.addEventListener('pointerdown', onPointerDown);
    messages.addEventListener('pointermove', onPointerMove);
    messages.addEventListener('pointerup', onPointerEnd);
    messages.addEventListener('pointercancel', onPointerEnd);
    document.addEventListener('pointerdown', onDocDown, true);

    // Load
    async function load() {
      const cached = conversationId && cache.get(conversationId);
      if (cached) {
        // Last time's copy straight away, then the server's in place.
        detail = {...cached, cached: true};
        renderDetail();
        scrollToBottom(scroller);
        if (requestsHere().length) monitor();
        await refresh();
        return;
      }
      if (conversationId) {
        messages.innerHTML = `<div class="${emptyClass}">Loading conversation…</div>`;
        try { detail = await api(`/${agent.id}/conversations/${conversationId}`); }
        catch (error) {
          if (alive()) messages.innerHTML = `<div class="${emptyClass}">${escapeHtml(error.message || 'Failed to load conversation.')}</div>`;
          return;
        }
        if (!alive()) return;
      }
      renderDetail();
      scrollToBottom(scroller);
      if (requestsHere().length) monitor();
    }
    const loaded = load();

    // Back on screen (phone app brought to the front): bring this chat up to
    // date in place. Tearing it down and loading again blanked the screen
    // with "Loading conversation…" every time the owner switched back.
    async function refresh() {
      if (!alive() || !conversationId) return;
      if (requestsHere().length) { monitor(); return; }  // monitor redraws when the reply lands
      const wasCached = Boolean(detail.cached);
      const before = JSON.stringify({...detail, cached: undefined});
      let fresh;
      try { fresh = await api(`/${agent.id}/conversations/${conversationId}`); }
      catch (error) {
        if (error.status === 404 && wasCached && alive()) {
          cache.drop(conversationId);
          messages.innerHTML = `<div class="${emptyClass}">${escapeHtml(error.message || 'Conversation not found.')}</div>`;
        }
        return;
      }
      if (!alive() || requestsHere().length) return;
      if (JSON.stringify(fresh) === before) { if (wasCached) { detail = fresh; cache.put(conversationId, fresh); } return; }
      const atBottom = nearBottom(scroller);
      detail = fresh;
      renderDetail();
      renderFailed();
      if (atBottom) scrollToBottom(scroller);
    }

    // ── Autojump (opt-in per device) ──────────────────────────────────────
    // When another agent's reply is ready, open it. It only switches by
    // itself when the owner is waiting here: they have sent something in this chat
    // since opening it, the text box is empty and not focused, nothing is
    // recording or speaking, and he hasn't touched or scrolled for a few
    // seconds. Otherwise (typing, a draft, reading) the jump waits and a small
    // "<Agent> replied · Jump" cue shows instead. Several replies queue in the
    // order they finished; the queue is shared by every chat on this device.
    let sentHere = false, lastInteraction = Date.now(), ajTimer = null;
    const ajCue = document.createElement('div');
    ajCue.className = 'autojump-cue';
    ajCue.hidden = true;
    form.prepend(ajCue);
    const touched = () => { lastInteraction = Date.now(); };
    scroller.addEventListener('scroll', touched, {passive: true});
    document.addEventListener('pointerdown', touched, true);
    document.addEventListener('keydown', touched, true);
    const loadQueue = () => {
      try { const q = JSON.parse(store.get(AUTOJUMP_QUEUE_KEY) || '{}'); return {after: Number(q.after) || 0, items: Array.isArray(q.items) ? q.items : []}; }
      catch { return {after: 0, items: []}; }
    };
    const saveQueue = q => store.set(AUTOJUMP_QUEUE_KEY, JSON.stringify({after: q.after, items: q.items.slice(-20)}));
    function canJumpNow() {
      return sentHere && !textarea.value.trim() && document.activeElement !== textarea && !recording && !autoOn
        && !voiceTurnActive && !pendingAttachments.length && Date.now() - lastInteraction > AUTOJUMP_IDLE_MS
        && !document.querySelector('.quick-switch, .handover-overlay, .msg-menu');
    }
    function jump(item, q) {
      q.items = q.items.filter(i => i.finished_ms !== item.finished_ms);
      saveQueue(q);
      sentHere = false;
      o.onAutojump(item.agent_id, item.conversation_id);
    }
    function showCue(q) {
      if (!q.items.length) { ajCue.hidden = true; ajCue.innerHTML = ''; return; }
      const first = q.items[0];
      const more = q.items.length - 1;
      ajCue.innerHTML = `<span>${escapeHtml(first.agent_name || first.agent_id)} replied${more ? ` · +${more} more` : ''}</span>`
        + '<button type="button" class="link-btn" data-aj-go>Jump</button><button type="button" class="link-btn" data-aj-x aria-label="Dismiss">×</button>';
      ajCue.hidden = false;
    }
    ajCue.onclick = event => {
      const q = loadQueue();
      if (!q.items.length) return;
      if (event.target.closest?.('[data-aj-go]')) jump(q.items[0], q);
      else if (event.target.closest?.('[data-aj-x]')) { q.items.shift(); saveQueue(q); showCue(q); }
    };
    async function autojumpTick() {
      ajTimer = null;
      if (!alive()) return;
      if (!o.onAutojump || store.get(AUTOJUMP_KEY) !== 'on') { ajCue.hidden = true; }
      else if (!document.hidden) {
        try {
          const q = loadQueue();
          const res = await api(`/api/replies?after=${q.after}`);
          if (!alive()) return;
          if (!q.after) q.after = res.now_ms;  // first run on this device: only replies from now on
          else for (const r of res.replies) {
            q.after = Math.max(q.after, r.finished_ms);
            if (!q.items.some(i => i.finished_ms === r.finished_ms)) q.items.push(r);
          }
          // This agent's own replies show right here.
          q.items = q.items.filter(i => i.agent_id !== agent.id);
          saveQueue(q);
          if (q.items.length && canJumpNow()) jump(q.items[0], q);
          showCue(q);
        } catch { /* try again next tick */ }
      }
      if (alive()) ajTimer = setTimeout(autojumpTick, AUTOJUMP_POLL_MS);
    }
    if (o.onAutojump) ajTimer = setTimeout(autojumpTick, 500);

    return {
      loaded,
      refresh,
      submit,
      get conversationId() { return conversationId; },
      retry,
      destroy() {
        destroyed = true;
        clearTimeout(ajTimer);
        ajCue.remove();
        scroller.removeEventListener('scroll', touched);
        document.removeEventListener('pointerdown', touched, true);
        document.removeEventListener('keydown', touched, true);
        stopWorkTimer();
        clearTimeout(pollTimer);
        statusEl?.remove();
        if (onShellStop) window.removeEventListener('message', onShellStop);
        clearInterval(recordTimer);
        clearTimeout(pressTimer);
        closeMenu();
        messages.removeEventListener('contextmenu', onContextMenu);
        messages.removeEventListener('pointerdown', onPointerDown);
        messages.removeEventListener('pointermove', onPointerMove);
        messages.removeEventListener('pointerup', onPointerEnd);
        messages.removeEventListener('pointercancel', onPointerEnd);
        document.removeEventListener('pointerdown', onDocDown, true);
        if (canTalk) {
          if (recording) o.audio.cancelRecording();
          if (autoOn) o.audio.stopAuto();
        }
      },
    };
  }

  // ── Purge (purge.py): drop the transcript, keep a short summary ─────────
  // One dialog for both UIs. Shows what would be kept (editable; empty is
  // fine), which agent files mention the chat, then purges on confirm.
  // Undo is in the chat itself afterwards. o: {agent:{id,name}, conversationId, api}
  // Resolves to true when purged, else null.
  async function openPurge(o) {
    const overlay = document.createElement('div');
    overlay.className = 'handover-overlay';
    overlay.innerHTML = `<div class="handover-dialog" role="dialog" aria-modal="true" aria-label="Purge conversation"><h2>Purge conversation</h2><p>Reading the chat…</p></div>`;
    document.body.appendChild(overlay);
    const dialog = overlay.querySelector('.handover-dialog');
    return new Promise(async resolve => {
      const close = value => { overlay.remove(); resolve(value); };
      overlay.addEventListener('click', e => { if (e.target === overlay) close(null); });
      let info;
      try { info = await o.api(`/${o.agent.id}/conversations/${o.conversationId}/purge/preview`, {method: 'POST'}); }
      catch (err) { dialog.innerHTML = `<h2>Purge conversation</h2><p>${escapeHtml(err.message || 'Could not read the chat.')}</p><div class="handover-actions"><button type="button" class="handover-cancel">Close</button></div>`; dialog.querySelector('.handover-cancel').onclick = () => close(null); return; }
      dialog.innerHTML = `<h2>Purge "${escapeHtml(info.title)}"</h2>
        <p>The chat and its name stay; its ${info.turns} messages are replaced by what's below. The full transcript is archived, so Undo in the chat brings it back.</p>
        <div class="handover-label">Kept as a summary (edit it, or leave it empty)</div>
        <textarea class="purge-summary" rows="6" placeholder="Nothing worth keeping">${escapeHtml(info.summary || '')}</textarea>
        ${info.mentioned_in.length ? `<p class="handover-hint">Mentioned in ${info.mentioned_in.map(escapeHtml).join(', ')}: those files stay as they are.</p>` : ''}
        <div class="handover-error" role="alert"></div>
        <div class="handover-actions"><button type="button" class="handover-cancel">Cancel</button><button type="button" class="handover-go purge-go">Purge</button></div>`;
      dialog.querySelector('.handover-cancel').onclick = () => close(null);
      dialog.querySelector('.purge-go').onclick = async event => {
        event.target.disabled = true;
        try {
          await o.api(`/${o.agent.id}/conversations/${o.conversationId}/purge`, {method: 'POST', body: JSON.stringify({summary: dialog.querySelector('.purge-summary').value})});
          close(true);
        } catch (err) { event.target.disabled = false; dialog.querySelector('.handover-error').textContent = err.message || 'Could not purge.'; }
      };
    });
  }

  // ── Handover: a conversation and its files to another agent ────────────
  // One dialog for both UIs, opened by `/handover <agent>`
  // in the chat or a conversation's Hand over button. It picks the agent and
  // the files to send (those the chat mentions are ticked); o.run(targetId,
  // files) does the move: mobile calls the API, the desktop page asks its
  // trusted shell. The new agent answers "Handover registered" (handover.py).
  // o: {agent:{id,name}, conversationId, api, targetHint, run, onDone(result), openTarget(targetId)}
  // Resolves to the result, or null if cancelled.
  async function openHandover(o) {
    const [targets, files] = await Promise.all([
      o.api(`/${o.agent.id}/move-targets`),
      o.api(`/${o.agent.id}/conversations/${o.conversationId}/handover-files`),
    ]);
    const hint = (o.targetHint || '').trim().toLowerCase();
    const chosen = hint && (targets.find(a => a.id === hint || a.name.toLowerCase() === hint)
      || targets.find(a => a.name.toLowerCase().startsWith(hint)));
    const fileRow = f => `<label class="handover-file"><input type="checkbox" value="${escapeHtml(f.path)}"${f.suggested ? ' checked' : ''}><span>${escapeHtml(f.path)}</span>${f.suggested ? '<em>in this chat</em>' : ''}</label>`;
    const suggested = files.filter(f => f.suggested), others = files.filter(f => !f.suggested);
    const overlay = document.createElement('div');
    overlay.className = 'handover-overlay';
    overlay.innerHTML = `<div class="handover-dialog" role="dialog" aria-modal="true" aria-label="Move to another agent">
      <h2>Move to another agent</h2>
      <label class="handover-to">To <select class="handover-target"><option value="">Choose agent…</option>${targets.map(a =>
        `<option value="${escapeHtml(a.id)}"${chosen && chosen.id === a.id ? ' selected' : ''}>${escapeHtml(a.name)}${a.clave ? ` (${escapeHtml(a.clave)})` : ''}</option>`).join('')}</select></label>
      <div class="handover-label">Files to send with it (none is fine)</div>
      <div class="handover-files">${suggested.map(fileRow).join('') || `<div class="handover-none">None of ${escapeHtml(o.agent.name)}'s files are mentioned in this chat.</div>`}
        ${others.length ? `<details><summary>Other files (${others.length})</summary>${others.map(fileRow).join('')}</details>` : ''}</div>
      <p class="handover-hint">The chat moves as it is, with the files you tick, and the new agent notes the handover in one line.</p>
      <div class="handover-error" role="alert">${hint && !chosen ? `No agent called "${escapeHtml(o.targetHint.trim())}". Choose one above.` : ''}</div>
      <div class="handover-actions"><button type="button" class="handover-cancel">Cancel</button><button type="button" class="handover-go">Move</button></div>
    </div>`;
    document.body.appendChild(overlay);
    const q = s => overlay.querySelector(s);
    const select = q('.handover-target'), go = q('.handover-go'), error = q('.handover-error');
    const syncGo = () => { go.disabled = !select.value; };
    select.onchange = () => { error.textContent = ''; syncGo(); };
    syncGo();
    (chosen ? go : select).focus();
    return new Promise(resolve => {
      const close = value => { document.removeEventListener('keydown', onKey, true); overlay.remove(); resolve(value); };
      const onKey = e => { if (e.key === 'Escape') { e.stopPropagation(); close(null); } };
      document.addEventListener('keydown', onKey, true);
      overlay.addEventListener('click', e => { if (e.target === overlay) close(null); });
      q('.handover-cancel').onclick = () => close(null);
      go.onclick = async () => {
        const target = targets.find(a => a.id === select.value);
        if (!target) return;
        const picked = [...overlay.querySelectorAll('.handover-file input:checked')].map(i => i.value);
        go.disabled = true; select.disabled = true;
        go.textContent = 'Moving…';
        error.textContent = '';
        try {
          const result = await o.run(target.id, picked);
          if (!result) { close(null); return; }  // cancelled at the shell's confirm
          o.onDone?.(result);
          const moved = result.moved || [];
          const failed = (result.failed || []).map(f => f.path);
          q('.handover-dialog').innerHTML = `<h2>Moved to ${escapeHtml(target.name)}</h2>
            <p>${moved.length ? `Files moved: ${moved.map(escapeHtml).join(', ')}.` : 'No files moved.'}${failed.length ? ` Still with ${escapeHtml(o.agent.name)}: ${failed.map(escapeHtml).join(', ')}.` : ''}</p>
            <div class="handover-actions"><button type="button" class="handover-cancel">Close</button>${o.openTarget ? `<button type="button" class="handover-go">Open in ${escapeHtml(target.name)}</button>` : ''}</div>`;
          q('.handover-cancel').onclick = () => close(result);
          const open = q('.handover-go');
          if (open) { open.onclick = () => { close(result); o.openTarget(target.id); }; open.focus(); }
        } catch (err) {
          error.textContent = err.message || 'Handover failed.';
          go.textContent = 'Hand over';
          select.disabled = false; syncGo();
        }
      };
    });
  }

  // ── Conversations menu: a "Conversations ▾"
  // button that lists an agent's chats (titles and order as the conversation
  // list, a 'draft' tag, the open one marked). Picking one calls onPick; the
  // caller remounts its chat, and drafts are kept per conversation, so
  // nothing typed is lost. Mouse or keyboard: Arrow Down on the button opens,
  // arrows/Home/End move, Enter picks, Escape or a click outside closes.
  // Used on the main dashboard's agent tiles, right of the name.
  function conversationMenu(o) {
    const store = o.store || storage();
    const wrap = document.createElement('div');
    wrap.className = 'convo-menu';
    wrap.innerHTML = `<button type="button" class="convo-menu-btn" aria-haspopup="listbox" aria-expanded="false" title="Switch conversation">Conversations ▾</button>
      <div class="convo-pop" role="listbox" aria-label="${escapeHtml(o.agent.name)} conversations" hidden></div>`;
    const btn = wrap.querySelector('.convo-menu-btn');
    const pop = wrap.querySelector('.convo-pop');
    const items = () => [...pop.querySelectorAll('.convo-pick')];
    const focusItem = i => { const all = items(); if (all.length) all[(i + all.length) % all.length].focus(); };
    const date = iso => { try { return iso ? new Date(`${iso}T00:00:00`).toLocaleDateString(undefined, {month: 'short', day: 'numeric'}) : ''; } catch { return iso; } };
    const close = (refocus = false) => {
      if (pop.hidden) return;
      pop.hidden = true;
      btn.setAttribute('aria-expanded', 'false');
      if (refocus) btn.focus();
    };
    async function open() {
      pop.hidden = false;
      btn.setAttribute('aria-expanded', 'true');
      pop.innerHTML = '<div class="convo-pop-state">Loading…</div>';
      let convos;
      try { convos = await o.api(`/${o.agent.id}/conversations`); }
      catch { if (!pop.hidden) pop.innerHTML = '<div class="convo-pop-state">Could not load conversations.</div>'; return; }
      if (pop.hidden) return;
      if (!convos.length) { pop.innerHTML = '<div class="convo-pop-state">No chats yet.</div>'; return; }
      const currentId = o.currentId();
      pop.innerHTML = convos.map(c => {
        const current = c.id === currentId;
        const draft = store.get(draftKey(o.agent.id, c.id))?.trim();
        return `<button type="button" class="convo-pick${current ? ' current' : ''}" role="option" aria-selected="${current}" data-id="${escapeHtml(c.id)}">`
          + `<span class="t">${escapeHtml(c.title)}${draft ? '<span class="draft-tag">draft</span>' : ''}</span><span class="d">${escapeHtml(date(c.date))}</span></button>`;
      }).join('');
      const all = items();
      (all.find(el => el.classList.contains('current')) || all[0]).focus();
    }
    btn.onclick = () => (pop.hidden ? open() : close());
    btn.onkeydown = e => { if (e.key === 'ArrowDown' && pop.hidden) { e.preventDefault(); open(); } };
    pop.onclick = e => {
      const pick = e.target.closest('.convo-pick');
      if (!pick) return;
      close();
      if (pick.dataset.id !== o.currentId()) o.onPick(pick.dataset.id);
    };
    pop.onkeydown = e => {
      const i = items().indexOf(document.activeElement);
      if (e.key === 'ArrowDown') { e.preventDefault(); focusItem(i + 1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); focusItem(i - 1); }
      else if (e.key === 'Home') { e.preventDefault(); focusItem(0); }
      else if (e.key === 'End') { e.preventDefault(); focusItem(-1); }
      else if (e.key === 'Tab') close();
    };
    document.addEventListener('mousedown', e => { if (!wrap.contains(e.target)) close(); });
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && !pop.hidden) close(true); });
    return wrap;
  }

  window.OpenClaveChat = {
    mountChat, choicesIn, conversationMenu, openHandover, openPurge, createLocalAudio, createBridgeAudio, serveAudioBridge, storage, copyText, forwardToAgentPage,
    escapeHtml, uuid, blobToBase64, base64ToBlob, draftKey, voiceLang, VOICE_LANG_KEY, AUTOJUMP_KEY, AUTOJUMP_QUEUE_KEY,
  };
})();
