// Voice "Check" mode (✎): the transcript goes into the text box instead of
// being sent; Send then asks for the spoken reply with the corrected text.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {makeChatContext} = require('./chat_harness.js');

const {context, els, form, messages, storage, flush} = makeChatContext();
const calls = [];
context.fakeApi = async (path, options) => {
  calls.push({path, body: options?.body ? JSON.parse(options.body) : null});
  if (path.endsWith('/voice/transcribe')) return {transcript: 'vann tomatene i morgen'};
  if (path.endsWith('/voice/reply')) return {conversation_id: 'c1', reply: 'Ok', audio_base64: null, elapsed_ms: 900};
  return {turns: [], summary: null};
};
context.fakeAudio = {
  unlock() {}, async startRecording() {}, async stopRecording() { return {type: 'audio/webm', arrayBuffer: async () => new Uint8Array([1]).buffer}; },
  cancelRecording() {}, async startAuto() {}, stopAuto() {}, async play() {},
};
(async () => {
  storage.set('openclave.voiceReview', '1');
  context.__form = form; context.__messages = messages;
  const chat = vm.runInContext(`OpenClaveChat.mountChat({agent:{id:'greg',name:'Greg'}, conversationId:null,
    api:(p,o)=>fakeApi(p,o), form:__form, messages:__messages, canTalk:true, audio:fakeAudio})`, context);
  await chat.loaded;
  const mic = els.get('.mic-btn'), textarea = els.get('textarea');
  await mic.onclick(); await flush();
  mic.onclick(); await flush();
  assert.equal(textarea.value, 'vann tomatene i morgen', 'transcript lands in the text box');
  assert.ok(!calls.some(c => c.path.endsWith('/voice/reply')), 'nothing sent yet');
  textarea.value = 'Vann tomatene i morgen tidlig';  // The owner fixes it
  await chat.submit(); await flush();
  const reply = calls.find(c => c.path.endsWith('/voice/reply'));
  assert.ok(reply, 'Send asks for the spoken reply');
  assert.equal(reply.body.transcript, 'Vann tomatene i morgen tidlig');
  assert.equal(calls.filter(c => c.path.endsWith('/voice/transcribe')).length, 1, 'not transcribed again');
  assert.ok(!calls.some(c => c.path.endsWith('/chat-requests')), 'not sent as a text message');
  assert.equal(textarea.value, '');
  // The chip turns it off again.
  els.get('.review-btn').onclick();
  assert.equal(storage.get('openclave.voiceReview'), '0');
  chat.destroy();
  console.log('Voice Check-mode checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
