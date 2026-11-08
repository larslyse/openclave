// Shared chat: refresh() (app back in the foreground) updates the open
// conversation in place, without the "Loading conversation…" blank, and
// leaves the DOM alone when nothing changed.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {makeChatContext} = require('./chat_harness.js');

const {context, form, messages, flush} = makeChatContext();
let turns = [{role: 'user', content: 'hi'}, {role: 'assistant', content: 'hello'}];
let fetches = 0;
context.fakeApi = async path => {
  if (path.includes('/conversations/')) { fetches++; return {turns: [...turns], summary: null}; }
  return {};
};
(async () => {
  context.__form = form; context.__messages = messages;
  const chat = vm.runInContext(`OpenClaveChat.mountChat({agent:{id:'rick',name:'Rick'}, conversationId:'thread',
    api:(p,o)=>fakeApi(p,o), form:__form, messages:__messages})`, context);
  await chat.loaded;
  assert.match(messages.innerHTML, /hello/);
  const drawn = messages.innerHTML;
  let blanked = false;
  const watch = setInterval(() => { if (/Loading conversation/.test(messages.innerHTML)) blanked = true; }, 0);
  await chat.refresh(); await flush();
  assert.equal(fetches, 2, 'refresh fetches the conversation again');
  assert.equal(messages.innerHTML, drawn, 'unchanged conversation is not redrawn');
  turns.push({role: 'assistant', content: 'a reply that landed while away'});
  await chat.refresh(); await flush();
  clearInterval(watch);
  assert.match(messages.innerHTML, /landed while away/, 'new turn shows');
  assert.equal(blanked, false, 'never shows the loading state');
  chat.destroy();
  console.log('Refresh checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
