// Autojump (shared chat): opens another agent's finished reply only while
// The owner is waiting (sent here, empty box, idle); otherwise queues it and shows
// a cue. Replies queue in finishing order; this agent's own are skipped.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {makeChatContext} = require('./chat_harness.js');

let now = 1_000_000;
class FakeDate extends Date { static now() { return now; } }
const timers = [];
const {context, els, form, messages, storage, flush} = makeChatContext({extraGlobals: {
  Date: FakeDate,
  setTimeout: (fn, ms) => { timers.push({fn, ms}); return timers.length; },
}});
const tick = async () => {
  const i = timers.findIndex(t => t.ms === 500 || t.ms === 4000);
  assert.ok(i >= 0, 'autojump is polling');
  const [{fn}] = timers.splice(i, 1);
  await fn(); await flush();
};
let replies = [];
const jumps = [];
context.fakeApi = async (path, options) => {
  if (path.startsWith('/api/replies')) {
    const after = Number(new URLSearchParams(path.split('?')[1]).get('after'));
    return {now_ms: now, replies: replies.filter(r => r.finished_ms > after)};
  }
  if (path.endsWith('/chat-requests') && options?.method === 'POST') {
    const body = JSON.parse(options.body);
    return {request_id: body.request_id, conversation_id: 'thread', status: 'running'};
  }
  if (path.includes('/chat-requests/')) return {status: 'running'};
  return {turns: [], summary: null};
};
context.jumps = jumps;
const reply = (agent, ms) => replies.push({agent_id: agent, agent_name: agent[0].toUpperCase() + agent.slice(1), conversation_id: `${agent}-c`, finished_ms: ms});

(async () => {
  storage.set('openclave.autojump', 'on');
  const created = [], make = context.document.createElement;
  context.document.createElement = tag => { const el = make(tag); created.push(el); return el; };
  context.__form = form; context.__messages = messages;
  const chat = vm.runInContext(`OpenClaveChat.mountChat({agent:{id:'rick',name:'Rick'}, conversationId:'thread',
    api:(p,o)=>fakeApi(p,o), form:__form, messages:__messages, onAutojump:(a,c)=>jumps.push([a,c])})`, context);
  await chat.loaded;
  await tick();  // first run: baseline, nothing older counts
  const queue = () => JSON.parse(storage.get('openclave.autojump.queue')).items.map(i => i.agent_id);

  // Just opened this chat (reading): no jump, a cue instead.
  now += 1000; reply('cathie', now);
  now += 10_000; await tick();
  assert.equal(jumps.length, 0, 'no jump while reading a chat he has not sent in');
  assert.deepEqual(queue(), ['cathie']);
  const cue = created.find(el => el.className === 'autojump-cue');
  assert.match(cue.innerHTML, /Cathie replied/, 'cue names the agent');
  assert.equal(cue.hidden, false, 'cue visible');

  // Typing a draft after sending: still no jump.
  els.get('textarea').value = 'hello';
  await chat.submit(); await flush();
  els.get('textarea').value = 'half a thought';
  now += 10_000; await tick();
  assert.equal(jumps.length, 0, 'never while there is text in the box');

  // Sent here, empty box, idle: two replies finished; the first one opens.
  els.get('textarea').value = '';
  now += 1000; reply('reba', now);
  now += 1000; reply('rick', now);  // this agent's own reply shows in place
  now += 10_000; await tick();
  assert.equal(JSON.stringify(jumps), JSON.stringify([['cathie', 'cathie-c']]), 'first finished reply opens');
  assert.deepEqual(queue(), ['reba'], 'the other one waits; own reply dropped');

  // Turned off: nothing happens and the queue stays untouched by polling.
  storage.delete('openclave.autojump');
  await tick();
  assert.equal(jumps.length, 1);
  chat.destroy();
  console.log('Autojump checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
