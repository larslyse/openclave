// Auto voice must never send noise (Rick's backlog, 2026-09-29): an empty
// transcript (nothing heard) is dropped silently, and switching Auto off
// before the transcript comes back discards it. Nothing reaches the agent.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {makeChatContext} = require('./chat_harness.js');

const {context, els, form, messages, flush} = makeChatContext();
const calls = [];
let transcript = '', releaseTranscribe = null;
context.fakeApi = async (path, options) => {
  calls.push(path);
  if (path.endsWith('/voice/transcribe')) {
    if (releaseTranscribe === 'hold') await new Promise(r => { releaseTranscribe = r; });
    return {transcript};
  }
  if (path.endsWith('/voice/reply')) return {conversation_id: 'c1', reply: 'Ok', audio_base64: null};
  return {turns: [], summary: null};
};
let onUtterance = null;
const blob = {type: 'audio/webm', arrayBuffer: async () => new Uint8Array([1]).buffer};
context.fakeAudio = {
  unlock() {}, async startRecording() {}, async stopRecording() { return blob; }, cancelRecording() {},
  async startAuto(h) { onUtterance = h.onUtterance; }, stopAuto() {}, async play() {},
};
(async () => {
  context.__form = form; context.__messages = messages;
  const chat = vm.runInContext(`OpenClaveChat.mountChat({agent:{id:'rick',name:'Rick'}, conversationId:null,
    api:(p,o)=>fakeApi(p,o), form:__form, messages:__messages, canTalk:true, audio:fakeAudio})`, context);
  await chat.loaded;
  const auto = els.get('.auto-toggle-input');
  auto.checked = true; await auto.onchange(); await flush();

  // Noise: whisper hears nothing -> no bubble, no reply, no error.
  await onUtterance(blob); await flush();
  assert.ok(!calls.some(p => p.endsWith('/voice/reply')), 'empty transcript is not sent');
  assert.ok(!messages.innerHTML.includes('error'), 'no error bubble in Auto');

  // Auto switched off while a recording is being transcribed: dropped.
  transcript = 'Thank you.'; releaseTranscribe = 'hold';
  const turn = onUtterance(blob); await flush();
  auto.checked = false; await auto.onchange();
  releaseTranscribe(); await turn; await flush();
  assert.ok(!calls.some(p => p.endsWith('/voice/reply')), 'nothing sent after Auto was switched off');
  assert.ok(!messages.innerHTML.includes('Thank you'), 'the transcript is not shown');
  assert.equal(els.get('textarea').value, '', 'composer unchanged');

  // A manual recording that heard nothing says so instead of sending.
  releaseTranscribe = null; transcript = '';
  const mic = els.get('.mic-btn');
  await mic.onclick(); await flush(); await mic.onclick(); await flush();
  assert.ok(messages.innerHTML.includes("Didn't catch any speech"), 'manual: tells the owner nothing was heard');
  assert.ok(!calls.some(p => p.endsWith('/voice/reply')), 'manual: still nothing sent');
  chat.destroy();
  console.log('Auto voice noise checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
