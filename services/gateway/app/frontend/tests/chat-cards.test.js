import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {createServer} from 'vite';
import react from '@vitejs/plugin-react';
import {fileURLToPath} from 'node:url';
import {createServer as createHttpServer} from 'node:http';

test('chat cards render trusted snapshots, escape text, and keep legacy messages', async () => {
  const httpServer = createHttpServer(); // Attach HMR without opening any network listener.
  const server = await createServer({root: fileURLToPath(new URL('../', import.meta.url)), configFile: false,
    plugins: [react()], server: {middlewareMode: true, hmr: {server: httpServer}, watch: null}, appType: 'custom'});
  try {
    const {default: ChatMessage} = await server.ssrLoadModule('/src/ChatMessage.jsx');
    const {default: ChatHistory} = await server.ssrLoadModule('/src/ChatHistory.jsx');
    const history = busy => renderToStaticMarkup(React.createElement(ChatHistory, {
      messages: [], busy, ready: true, connected: true,
    }));
    assert.match(history('transcription'), /bubble user processing/);
    assert.match(history('transcription'), /Распознаю голос/);
    assert.doesNotMatch(history('transcription'), /Готовлю ответ|Чем помочь/);
    assert.match(history('assistant'), /bubble assistant processing/);
    assert.match(history('assistant'), /Готовлю ответ/);
    assert.doesNotMatch(history(false), /processing/);

    const render = message => renderToStaticMarkup(React.createElement(ChatMessage, {message, timezone: 'Europe/Moscow', api: async () => ({}), onOpen: () => {}}));
    assert.match(render({role: 'assistant', text: 'Старый ответ'}), /Старый ответ/);
    const record = {id: 'a', title: '<script>bad</script>', kind: 'meeting', status: 'active', start_at: '2027-12-15T11:00:00Z', end_at: '2027-12-15T12:00:00Z', version: 1};
    const html = render({role: 'assistant', text: 'Встреча добавлена', cards: [{action: 'created', record}]});
    assert.match(html, /Встреча добавлена/);
    assert.match(html, /14:00/);
    assert.match(html, /15:00/);
    assert.match(html, /2027/);
    assert.match(html, /&lt;script&gt;/);
    assert.doesNotMatch(html, /<script>/);
    assert.match(html, /Открыть/);
    assert.match(html, /<details class="chat-records" open=""/);
    const many = render({role:'assistant', text:'Вот ваши задачи', cards:Array.from({length:20},(_,i)=>({action:'found',record:{...record,id:String(i)}}))});
    assert.match(many, /Записи \(20\)/);
    assert.match(many, /<details class="chat-records">/);
    assert.doesNotMatch(many, /<details[^>]*open=/);
    const deleted = render({role: 'assistant', text: 'Удалено', cards: [{action: 'deleted', record}]});
    assert.doesNotMatch(deleted, /<button/);
    assert.match(deleted, /Удалено/);
    assert.doesNotMatch(render({role: 'user', text: 'Привет', cards: [{action: 'created', record}]}), /chat-record-card/);
    const found = render({role: 'assistant', text: 'Вот результаты', cards: [{action: 'found', record}, {action: 'found', record: {...record, id: 'b', title: 'Другой результат', status: 'completed'}}]});
    assert.match(found, /Другой результат/);
    assert.match(found, /Выполнено/);
    const {draftFromRecord, recordFields} = await server.ssrLoadModule('/src/Calendar.jsx');
    const overnight = {...record, title: 'Ночная встреча', start_at: '2027-12-15T20:30:00Z', end_at: '2027-12-15T21:30:00Z'};
    const draft = draftFromRecord(overnight, 'Europe/Moscow');
    assert.equal(draft.date, '2027-12-15');
    assert.equal(draft.endDate, '2027-12-16');
    const fields = recordFields({...draft, title: 'Новое название'}, 'Europe/Moscow');
    assert.equal(fields.end_at, '2027-12-15T21:30:00.000Z');
    assert.equal(fields.start_at, '2027-12-15T20:30:00.000Z');
  } finally { await server.close(); }
});

test('opening a card fetches current data and exact recurrence; deleted and 404 are safe', async () => {
  const {openCardRecord} = await import('../src/chat-cards.js');
  const paths = [];
  const card = {action: 'found', record: {id: 'a', title: 'old', occurrence_at: '2027-12-15T11:00:00+00:00'}};
  const current = await openCardRecord(async path => {paths.push(path); return {id: 'a', title: 'current', version: 2};}, card);
  assert.equal(current.title, 'current');
  const url = new URL(paths[0], 'http://local');
  assert.equal(url.searchParams.get('occurrence_at'), card.record.occurrence_at);
  await assert.rejects(openCardRecord(async () => {throw Object.assign(new Error('404'), {status: 404});}, card), /больше недоступна/);
  await assert.rejects(openCardRecord(async () => {throw new Error('must not fetch');}, {...card, action: 'deleted'}), /удалена/);
});
