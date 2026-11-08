// Quick replies: only explicit lettered choices (A, B, ...) become buttons.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {makeChatContext} = require('./chat_harness.js');

const {context} = makeChatContext();
const choices = text => JSON.parse(JSON.stringify(vm.runInContext(`OpenClaveChat.choicesIn(${JSON.stringify(text)})`, context)));

assert.deepEqual(choices('Which one?\n\nA) Sonnet\nB) Opus\nC) Keep it'),
  [{letter: 'A', text: 'Sonnet'}, {letter: 'B', text: 'Opus'}, {letter: 'C', text: 'Keep it'}]);
assert.deepEqual(choices('Pick:\n- **A.** Fast\n- **B.** Cheap\n\nOr tell me something else.').map(c => c.text), ['Fast', 'Cheap']);
assert.deepEqual(choices('What would make me useful?'), []);            // open question: nothing
assert.deepEqual(choices('Steps:\n1. Do this\n2. Then that'), []);      // numbered list is not a choice
assert.deepEqual(choices('B) only\nC) wrong start'), []);
assert.deepEqual(choices('A) one'), []);                                // a single option is no choice
console.log('quick replies ok');
