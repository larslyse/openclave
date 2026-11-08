// Minimal fake DOM + vm context for driving static/chat_core.js (and, when
// needed, static/mobile/app.js) from Node without a browser. Elements are
// looked up by the exact selector the code uses.
const fs = require('node:fs');
const vm = require('node:vm');

function element(tag = 'div') {
  const el = {
    tagName: tag.toUpperCase(), innerHTML: '', value: '', disabled: false, checked: false, style: {}, dataset: {},
    scrollHeight: 0, clientHeight: 0, scrollTop: 0, isConnected: true, parentNode: null,
    classList: {set: new Set(), add(c) { this.set.add(c); }, remove(c) { this.set.delete(c); }, contains(c) { return this.set.has(c); }, toggle(c, on) { if (on ?? !this.set.has(c)) this.set.add(c); else this.set.delete(c); }},
    listeners: {}, addEventListener(name, fn) { this.listeners[name] = fn; }, removeEventListener() {},
    querySelector() { return null; }, querySelectorAll() { return []; },
    insertAdjacentHTML(_, html) { this.innerHTML += html; }, setAttribute() {}, remove() {},
    insertBefore() {}, prepend() {}, appendChild(child) { this.innerHTML += child.textContent || ''; },
    play() { return Promise.resolve(); }, pause() {}, focus() {},
    set textContent(value) { this.innerHTML = String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;'); },
    get textContent() { return this.innerHTML; },
  };
  return el;
}

// Returns {context, els, form, messages, calls, flush, setApi}.
function makeChatContext({extraGlobals = {}, loadApp = false} = {}) {
  const els = new Map();
  const form = element('form');
  const messages = element();
  form.parentNode = element();
  const formParts = ['textarea', 'button.send', '.attach-preview', '.attach-input', '.attach-btn:not(.mic-btn)',
    '.mic-btn', '.auto-toggle-input', '.auto-status', '.lang-btn', '.review-btn'];
  for (const sel of formParts) els.set(sel, element(sel === 'textarea' ? 'textarea' : 'div'));
  form.querySelector = sel => els.get(sel) || null;
  const storage = new Map();
  let timer = null;
  const context = vm.createContext({
    console, Date, Number, JSON, Math, Set, Map, Error, Promise, Uint8Array, Object, Array, String,
    Blob: class { constructor(parts, options) { this.type = options?.type; } async arrayBuffer() { return new Uint8Array([1, 2]).buffer; } },
    btoa: s => Buffer.from(s, 'binary').toString('base64'),
    atob: s => Buffer.from(s, 'base64').toString('binary'),
    document: {hidden: false, body: element(), querySelector: () => null, querySelectorAll: () => [], createElement: tag => element(tag), addEventListener() {}, removeEventListener() {}},
    window: {addEventListener() {}, removeEventListener() {}, crypto: {randomUUID: () => `request-${Math.random().toString(16).slice(2)}`}},
    localStorage: {getItem: k => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, String(v)), removeItem: k => storage.delete(k)},
    location: {hash: ''}, history: {replaceState() {}},
    URL: {createObjectURL: () => 'blob:preview', revokeObjectURL() {}},
    fetch: async () => ({ok: true, json: async () => ({voice_available: false, secure_mobile_url: ''})}),
    createImageBitmap: async () => null,
    setTimeout: fn => { timer = fn; return 1; }, clearTimeout() {},
    setInterval: () => 1, clearInterval() {}, requestAnimationFrame: fn => fn(),
    ...extraGlobals,
  });
  context.window.localStorage = context.localStorage;
  vm.runInContext(fs.readFileSync('static/chat_core.js', 'utf8'), context);
  context.OpenClaveChat = context.window.OpenClaveChat;
  if (loadApp) {
    let source = fs.readFileSync('static/mobile/app.js', 'utf8');
    source = source.slice(0, source.indexOf("window.addEventListener('hashchange', render);"));
    vm.runInContext(source, context);
  }
  const flush = async () => { for (let i = 0; i < 60; i++) await Promise.resolve(); };
  return {context, els, form, messages, storage, flush, runTimer: () => timer && timer()};
}

module.exports = {element, makeChatContext};
