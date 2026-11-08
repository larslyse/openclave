// The shared chat (static/chat_core.js): durable queue, photo drafts; plus the
// mobile conversation list's rename/delete (static/mobile/app.js).
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {element, makeChatContext} = require('./chat_harness.js');

const {context, els, form, messages, flush, runTimer} = makeChatContext({loadApp: true});
const records = new Map();
const sent = [];
context.fakeApi = async (path, options) => {
  if (path.endsWith('/chat-requests') && options?.method === 'POST') {
    const body = JSON.parse(options.body);
    sent.push(body);
    const record = {request_id: body.request_id, conversation_id: 'thread', status: sent.length === 1 ? 'running' : 'queued'};
    records.set(body.request_id, record);
    return record;
  }
  if (path.endsWith('/attachments')) return {conversation_id: 'thread', attachment: {id: 'photo1234'}};
  if (path.includes('/chat-requests/')) return records.get(path.split('/').pop().split('?')[0]);
  return {turns: [], summary: null};
};

(async () => {
  const chat = vm.runInContext(`OpenClaveChat.mountChat({agent:{id:'stash',name:'Stash'}, conversationId:'thread',
    api:(p,o)=>fakeApi(p,o), form:__form, messages:__messages, canAttach:true})`, Object.assign(context, {__form: form, __messages: messages}));
  await chat.loaded;
  const textarea = els.get('textarea');
  const send = els.get('button.send');
  textarea.value = 'first';
  await chat.submit();
  assert.equal(send.disabled, false, 'send stays enabled while model is running');
  assert.equal(textarea.disabled, false);
  textarea.value = 'second';
  await chat.submit();
  assert.equal(sent.length, 2, 'second message accepted while first is running');
  assert.match(messages.innerHTML, /Queued/);
  const input = els.get('.attach-input');
  input.files = [{type: 'image/jpeg', name: 'p.jpg', arrayBuffer: async () => new Uint8Array([1, 2]).buffer}];
  input.listeners.change();
  await flush();
  textarea.value = 'next photo';
  records.get(sent[0].request_id).status = 'completed';
  await runTimer();
  await flush();
  assert.equal(textarea.value, 'next photo', 'incoming reply preserves draft');
  assert.match(els.get('.attach-preview').innerHTML, /blob:preview/, 'incoming reply preserves photo');
  await chat.submit();
  assert.deepEqual(sent[2].attachments, ['photo1234']);
  assert.equal(sent[2].message, 'next photo');
  chat.destroy();

  // Mobile conversation list: rename and delete.
  const rename = element(); rename.dataset = {rename: 'thread'};
  const remove = element(); remove.dataset = {delete: 'thread'};
  const listEls = new Map([['[data-conversation]', []], ['[data-rename]', [rename]], ['[data-delete]', [remove]]]);
  context.document.querySelector = s => listEls.get(s) || element();
  context.document.querySelectorAll = s => listEls.get(s) || [];
  const edits = [];
  context.fakeApi = async (path, options) => {
    if (options) edits.push({path, ...options});
    return [{id: 'thread', title: 'Old title', date: '2026-09-22'}];
  };
  context.window.prompt = () => 'New title';
  context.window.confirm = () => false;
  context.window.alert = message => { throw new Error(message); };
  vm.runInContext('api = fakeApi; shell = () => {}; renderToken = 1;', context);
  await vm.runInContext("renderConversations({id:'stash',name:'Stash'},1)", context);
  await rename.onclick();
  assert.equal(edits[0].method, 'PATCH');
  assert.equal(JSON.parse(edits[0].body).title, 'New title');
  await remove.onclick();
  assert.equal(edits.length, 1, 'canceling deletion makes no request');
  context.window.confirm = () => true;
  await remove.onclick();
  assert.equal(edits[1].method, 'DELETE');
  assert.equal(edits[1].path, '/stash/conversations/thread');
  console.log('Chat checks: queue, photo drafts; mobile rename, delete and cancellation passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
