// Shared chat: a failed reply shows an error bubble with Retry; Retry sends
// the same message again (new request id) into the same conversation.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {makeChatContext} = require('./chat_harness.js');

const {context, els, form, messages, flush, runTimer} = makeChatContext();
const posts = [];
let status = 'running';
context.fakeApi = async (path, options) => {
  if (path.endsWith('/chat-requests') && options?.method === 'POST') {
    const body = JSON.parse(options.body);
    posts.push(body);
    return {request_id: body.request_id, conversation_id: 'thread', status: 'running'};
  }
  if (path.includes('/chat-requests/')) return {status, error: status === 'failed' ? 'codex-cli error: boom' : null};
  return {turns: [], summary: null};
};
(async () => {
  context.__form = form; context.__messages = messages;
  const chat = vm.runInContext(`OpenClaveChat.mountChat({agent:{id:'rick',name:'Rick'}, conversationId:'thread',
    api:(p,o)=>fakeApi(p,o), form:__form, messages:__messages})`, context);
  await chat.loaded;
  els.get('textarea').value = 'please do the thing';
  await chat.submit();
  status = 'failed';
  await runTimer(); await flush();
  assert.match(messages.innerHTML, /codex-cli error: boom[\s\S]*Retry/, 'failure shown with a Retry button');
  status = 'running';
  chat.retry(posts[0].request_id);
  await flush();
  assert.equal(posts.length, 2, 'Retry sends again');
  assert.equal(posts[1].message, 'please do the thing');
  assert.equal(posts[1].conversation_id, 'thread');
  assert.notEqual(posts[1].request_id, posts[0].request_id, 'as a new request');
  assert.doesNotMatch(messages.innerHTML, /boom/, 'error bubble gone once retried');
  chat.destroy();
  console.log('Retry checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
