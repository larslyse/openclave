// Shared chat, cache-first open (Rick's backlog, 2026-10-05): a conversation
// opened before is drawn from the device's copy at once, with no "Loading
// conversation…", then replaced in place by the server's. Only turns the
// server returned are stored (never queued messages), and the copy is capped.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {makeChatContext} = require('./chat_harness.js');

const KEY = 'openclave.cache.transcripts';
const turn = (role, content) => ({role, content});

function mount(ctx, conversationId) {
  ctx.context.__form = ctx.form; ctx.context.__messages = ctx.messages; ctx.context.__id = conversationId;
  return vm.runInContext(`OpenClaveChat.mountChat({agent:{id:'rick',name:'Rick'}, conversationId:__id,
    api:(p,o)=>fakeApi(p,o), form:__form, messages:__messages})`, ctx.context);
}

(async () => {
  // 1. First open: nothing cached, loads from the server and keeps a copy.
  const first = makeChatContext();
  let turns = [turn('user', 'hi'), turn('assistant', 'hello there')];
  first.context.fakeApi = async path => path.includes('/conversations/') ? {turns: [...turns], summary: null} : {};
  let chat = mount(first, 'thread');
  await chat.loaded;
  chat.destroy();
  const saved = JSON.parse(first.storage.get(KEY));
  assert.deepEqual(saved.order, ['thread']);
  assert.match(JSON.stringify(saved.items.thread.turns), /hello there/);

  // 2. Next open: the copy shows before the server answers, then the server's.
  const second = makeChatContext();
  second.storage.set(KEY, first.storage.get(KEY));
  // A queued message for this chat lives in the pending store; it must never
  // end up in the transcript copy.
  second.storage.set('openclave.pending', JSON.stringify([{requestId: 'q1', agentId: 'rick', conversationId: 'thread', message: 'QUEUED-TEXT', accepted: true}]));
  let answer;
  second.context.fakeApi = path => {
    if (path.includes('/conversations/')) return new Promise(resolve => { answer = resolve; });
    if (path.includes('/chat-requests/')) return Promise.resolve({status: 'queued'});
    return Promise.resolve({});
  };
  chat = mount(second, 'thread');
  await second.flush();
  assert.match(second.messages.innerHTML, /hello there/, 'cached turns drawn at once');
  assert.doesNotMatch(second.messages.innerHTML, /Loading conversation/, 'no loading state');
  turns.push(turn('assistant', 'reply that landed meanwhile'));
  answer({turns: [...turns], summary: null});
  await chat.loaded; await second.flush();
  chat.destroy();
  assert.doesNotMatch(second.storage.get(KEY), /QUEUED-TEXT/, 'queued messages are never cached');

  // 3. Without anything pending the server's copy replaces the cached one in place.
  const third = makeChatContext();
  third.storage.set(KEY, first.storage.get(KEY));
  third.context.fakeApi = path => path.includes('/conversations/') ? new Promise(resolve => { answer = resolve; }) : Promise.resolve({});
  chat = mount(third, 'thread');
  await third.flush();
  assert.doesNotMatch(third.messages.innerHTML, /landed meanwhile/);
  answer({turns: [...turns], summary: null});
  await chat.loaded; await third.flush();
  assert.match(third.messages.innerHTML, /landed meanwhile/, 'server copy swapped in');
  assert.match(third.storage.get(KEY), /landed meanwhile/, 'and saved');
  chat.destroy();

  // 4. Deleted on the server since: say so, and forget the copy.
  const fourth = makeChatContext();
  fourth.storage.set(KEY, first.storage.get(KEY));
  fourth.context.fakeApi = async path => { if (path.includes('/conversations/')) throw Object.assign(new Error('Conversation not found.'), {status: 404}); return {}; };
  chat = mount(fourth, 'thread');
  await chat.loaded; await fourth.flush();
  assert.match(fourth.messages.innerHTML, /not found/);
  assert.equal(JSON.parse(fourth.storage.get(KEY)).items.thread, undefined);
  chat.destroy();

  // 5. Small: at most 40 turns per conversation and 8 conversations.
  const fifth = makeChatContext();
  const many = Array.from({length: 50}, (_, i) => turn(i % 2 ? 'assistant' : 'user', `turn ${i}`));
  fifth.context.fakeApi = async path => path.includes('/conversations/') ? {turns: many, summary: null} : {};
  for (let i = 0; i < 10; i++) { chat = mount(fifth, `c${i}`); await chat.loaded; chat.destroy(); }
  const capped = JSON.parse(fifth.storage.get(KEY));
  assert.equal(capped.order.length, 8);
  assert.deepEqual(Object.keys(capped.items).sort(), ['c2', 'c3', 'c4', 'c5', 'c6', 'c7', 'c8', 'c9']);
  assert.equal(capped.items.c9.turns.length, 40);
  assert.equal(capped.items.c9.turns.at(-1).content, 'turn 49', 'keeps the newest turns');
  console.log('Cache-first chat checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
