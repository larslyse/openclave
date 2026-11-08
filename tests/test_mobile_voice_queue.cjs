// Voice in the shared chat (static/chat_core.js), same on mobile and desktop:
// the text box stays enabled during a voice turn; typed messages are held
// until the voice reply text is back, then sent into the voice conversation
// (never before the voice reply, never a second chat); the EN/NO choice
// reaches both voice calls.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {makeChatContext} = require('./chat_harness.js');

const {context, els, form, messages, storage, flush} = makeChatContext();
const calls = [];
const deferred = {};
const wait = name => new Promise(resolve => { deferred[name] = resolve; });
context.fakeApi = async (path, options) => {
  calls.push({path, body: options?.body ? JSON.parse(options.body) : null});
  if (path.endsWith('/voice/transcribe')) return wait('transcribe');
  if (path.endsWith('/voice/reply')) return wait('reply');
  if (path.endsWith('/chat-requests')) {
    const body = JSON.parse(options.body);
    return {request_id: body.request_id, conversation_id: body.conversation_id || 'fresh', status: 'running'};
  }
  if (path.includes('/chat-requests/')) return {status: 'running'};
  return {turns: [], summary: null};
};
const played = [];
context.fakeAudio = {
  unlock() {}, async startRecording() {}, async stopRecording() { return {type: 'audio/webm', arrayBuffer: async () => new Uint8Array([1]).buffer}; },
  cancelRecording() {}, async startAuto() {}, stopAuto() {}, async play(b64, mime) { played.push(mime); },
};

(async () => {
  storage.set('openclave.voiceLang', 'no');   // EN/NO chip set to Norwegian
  context.__form = form; context.__messages = messages;
  const chat = vm.runInContext(`OpenClaveChat.mountChat({agent:{id:'rick',name:'Rick'}, conversationId:null,
    api:(p,o)=>fakeApi(p,o), form:__form, messages:__messages, canTalk:true, audio:fakeAudio})`, context);
  await chat.loaded;
  const textarea = els.get('textarea');
  const mic = els.get('.mic-btn');
  await mic.onclick(); await flush();   // start recording
  mic.onclick(); await flush();         // stop -> voice turn starts
  assert.equal(calls.at(-1).path, '/rick/voice/transcribe');
  assert.equal(textarea.disabled, false, 'text box stays usable during a voice turn');

  textarea.value = 'typed during voice';
  await chat.submit();
  assert.equal(calls.filter(c => c.path.endsWith('/chat-requests')).length, 0, 'held until the voice reply is back');
  assert.match(messages.innerHTML, /typed during voice[\s\S]*Queued/);
  assert.equal(textarea.value, '', 'text box cleared for the next message');

  deferred.transcribe({transcript: 'hello'}); await flush();
  assert.equal(calls.filter(c => c.path.endsWith('/chat-requests')).length, 0, 'still held while the reply is generated');
  deferred.reply({conversation_id: 'voiceconv', transcript: 'hello', reply: 'hi', audio_base64: 'AAAA', audio_mime: 'audio/mpeg', elapsed_ms: 1000});
  await flush();

  const paths = calls.map(c => c.path);
  const post = calls.find(c => c.path === '/rick/chat-requests');
  assert.ok(post, 'held message sent after the voice reply');
  assert.ok(paths.indexOf('/rick/voice/reply') < paths.indexOf('/rick/chat-requests'));
  assert.equal(post.body.message, 'typed during voice');
  assert.equal(post.body.conversation_id, 'voiceconv', 'sent into the voice conversation, not a new one');
  assert.equal(calls.find(c => c.path.endsWith('/voice/transcribe')).body.language, 'no', 'chosen language reaches speech-to-text');
  assert.equal(calls.find(c => c.path.endsWith('/voice/reply')).body.language, 'no', 'and the reply');
  assert.deepEqual(played, ['audio/mpeg'], 'reply audio played through the audio device');

  // After the voice turn, typing goes straight out as usual.
  textarea.value = 'after voice';
  await chat.submit();
  assert.equal(calls.filter(c => c.path === '/rick/chat-requests').at(-1).body.message, 'after voice');

  // EN/NO chip toggles and is remembered.
  els.get('.lang-btn').onclick();
  assert.equal(storage.get('openclave.voiceLang'), 'en');
  chat.destroy();
  console.log('Voice checks: composer open during voice, held messages sent after the reply, voice language passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
