import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {createServer as createHttpServer} from 'node:http';
import {fileURLToPath} from 'node:url';
import {createServer} from 'vite';
import react from '@vitejs/plugin-react';

test('reminders expose edit, complete and delete; editor supports completing reminders', async () => {
  const httpServer = createHttpServer();
  const server = await createServer({root: fileURLToPath(new URL('../', import.meta.url)), configFile: false,
    plugins: [react()], server: {middlewareMode: true, hmr: {server: httpServer}, watch: null}, appType: 'custom'});
  try {
    const {default: Reminders, selectReminders} = await server.ssrLoadModule('/src/Reminders.jsx');
    const {RecordEditor, draftFromRecord} = await server.ssrLoadModule('/src/Calendar.jsx');
    const active = {id: 'active', kind: 'reminder', title: 'Позвонить Ивану', status: 'active', remind_at: '2027-12-15T08:00:00Z', version: 2};
    const completed = {...active, id: 'completed', title: 'Уже сделано', status: 'completed'};
    const props = {items: [], records: [active, completed], timezone: 'Europe/Moscow', api: async () => ({}), onRead: () => {}, onChanged: () => {}};
    const html = renderToStaticMarkup(React.createElement(Reminders, props));
    assert.match(html, /Позвонить Ивану/);
    assert.match(html, /Выполнено/);
    assert.match(html, /Изменить/);
    assert.match(html, /Удалить/);
    assert.doesNotMatch(html, /Уже сделано/);
    assert.match(html, /Выполненные/);
    const oldCompleted = {...completed, id: 'old', remind_at: '2020-01-01T08:00:00Z'};
    assert.deepEqual(selectReminders([active, completed, oldCompleted, {...completed, kind: 'task'}], 'completed').map(item => item.id), ['completed', 'old']);
    assert.deepEqual(selectReminders([active, completed], 'upcoming').map(item => item.id), ['active']);
    const modal = renderToStaticMarkup(React.createElement(RecordEditor, {record: active, draft: draftFromRecord(active, props.timezone), timezone: props.timezone, api: props.api}));
    assert.match(modal, /Выполнено/);
    assert.match(modal, /Удалить/);
    assert.match(modal, /Сохранить/);
    const finishedModal = renderToStaticMarkup(React.createElement(RecordEditor, {record: completed, draft: draftFromRecord(completed, props.timezone), timezone: props.timezone, api: props.api}));
    assert.doesNotMatch(finishedModal, />Выполнено</);
  } finally { await server.close(); }
});

test('record actions include version and exact occurrence instead of changing the series', async () => {
  const {completeRecord, deleteRecord} = await import('../src/record-actions.js');
  const requests = [];
  const api = async (path, options) => {requests.push({path, ...options}); return {};};
  const record = {id: 'reminder', version: 3, recurrence: {frequency: 'daily'}, occurrence_at: '2027-12-15T08:00:00+00:00'};
  await completeRecord(api, record);
  assert.deepEqual(JSON.parse(requests[0].body), {fields: {status: 'completed'}, version: 3, scope: 'occurrence', occurrence_at: record.occurrence_at});
  assert.ok(requests[0].headers['Idempotency-Key']);
  await deleteRecord(api, record);
  const url = new URL(requests[1].path, 'http://local');
  assert.equal(requests[1].method, 'DELETE');
  assert.equal(url.searchParams.get('version'), '3');
  assert.equal(url.searchParams.get('scope'), 'occurrence');
  assert.equal(url.searchParams.get('occurrence_at'), record.occurrence_at);
  await completeRecord(api, {id: 'whole-series', version: 4, recurrence: {frequency: 'daily'}});
  assert.equal(JSON.parse(requests[2].body).scope, 'series');
  await assert.rejects(completeRecord(async () => {throw new Error('Запись изменилась');}, record), /Запись изменилась/);
});
