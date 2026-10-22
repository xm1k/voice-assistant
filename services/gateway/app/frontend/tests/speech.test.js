import test from 'node:test';
import assert from 'node:assert/strict';
import {createSpeaker} from '../src/speech.js';

test('HTTP/WebSocket delivery, reload and a second tab speak each reply only once', async () => {
  const said = [], values = new Map();
  let pending = Promise.resolve();
  const env = {speechSynthesis:{speak:utterance=>said.push(utterance.text),cancel:()=>{}}, SpeechSynthesisUtterance: class {constructor(text){this.text=text;}},
    localStorage:{getItem:key=>values.get(key),setItem:(key,value)=>values.set(key,value)},
    navigator:{locks:{request:(key,callback)=>{const result=pending.then(callback);pending=result.catch(()=>{});return result;}}}};
  const a=createSpeaker('alice',env), b=createSpeaker('alice',env);
  await Promise.all([a.speakOnce('message-1','Первый ответ'),a.speakOnce('message-1','Первый ответ'),b.speakOnce('message-1','Первый ответ')]);
  await createSpeaker('alice',env).speakOnce('message-1','Первый ответ');
  assert.deepEqual(said,['Первый ответ']);
  await a.speakOnce('message-2','Первый ответ'); // A new message may legitimately have the same text.
  assert.equal(said.length,2);
});
