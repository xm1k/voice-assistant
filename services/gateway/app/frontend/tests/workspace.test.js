import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {createServer as createHttpServer} from 'node:http';
import {fileURLToPath} from 'node:url';
import {createServer} from 'vite';
import react from '@vitejs/plugin-react';

test('live workspace renders calendar and shared input on every desktop tab without demo data', async () => {
  const httpServer = createHttpServer();
  const server = await createServer({root: fileURLToPath(new URL('../', import.meta.url)), configFile: false,
    plugins: [react()], server: {middlewareMode: true, hmr: {server: httpServer}, watch: null}, appType: 'custom'});
  try {
    const {default: Workspace} = await server.ssrLoadModule('/src/Workspace.jsx');
    const props = {
      user: {username: 'Рабочий пользователь', timezone: 'Europe/Moscow'},
      setView() {}, api: async () => ({}), onRecordsChanged() {}, onSend() {}, onAudio() {}, onClear() {},
      messages: [{id: 'live', role: 'assistant', text: 'Ответ сервера'}], connected: true, historyReady: true,
      taskRecords: [{id: 'task', kind: 'task', title: 'Рабочая задача', status: 'active'}], reminderRecords: [], notices: [],
      Navigation: () => null, Composer: ({busy}) => React.createElement('textarea', {'aria-label': 'Общий ввод', disabled: busy}),
    };
    for (const view of ['assistant', 'tasks', 'reminders']) {
      const html = renderToStaticMarkup(React.createElement(Workspace, {...props, view}));
      assert.match(html, /workspace-calendar/);
      assert.match(html, /Общий ввод/);
      assert.match(html, /Панель помощника/);
      assert.match(html, /Рабочий пользователь/);
      assert.doesNotMatch(html, /Демо|Синхронизация команды|Ближайшее/);
      if (view === 'assistant') {assert.match(html, /Ответ сервера/); assert.match(html, /Очистить чат/);}
      if (view === 'tasks') {assert.match(html, /Рабочая задача/); assert.doesNotMatch(html, /disabled=""[^>]*aria-label="Завершить задачу"/);}
    }
    const offline = renderToStaticMarkup(React.createElement(Workspace, {...props, view: 'tasks', connected: false}));
    assert.match(offline, /Соединение восстанавливается/);
    assert.match(offline, /aria-label="Общий ввод" disabled=""/);
    const waiting = renderToStaticMarkup(React.createElement(Workspace, {...props, view: 'tasks', pending: true}));
    assert.match(waiting, /Помощник ждёт уточнения в чате/);
    const originalMatchMedia = globalThis.matchMedia;
    globalThis.matchMedia = () => ({matches: false});
    try {
      const mobileTasks = renderToStaticMarkup(React.createElement(Workspace, {...props, view: 'tasks'}));
      assert.doesNotMatch(mobileTasks, /workspace-calendar|Панель помощника|Общий ввод/);
      const mobileChat = renderToStaticMarkup(React.createElement(Workspace, {...props, view: 'assistant'}));
      assert.match(mobileChat, /Общий ввод/);
      const mobileCalendar = renderToStaticMarkup(React.createElement(Workspace, {...props, view: 'calendar'}));
      assert.match(mobileCalendar, /month-calendar/);
      assert.doesNotMatch(mobileCalendar, /Общий ввод/);
    } finally {
      if (originalMatchMedia) globalThis.matchMedia = originalMatchMedia;
      else delete globalThis.matchMedia;
    }
  } finally {await server.close();}
});
