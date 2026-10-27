import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {createServer} from 'vite';
import react from '@vitejs/plugin-react';
import {fileURLToPath} from 'node:url';
import {createServer as createHttpServer} from 'node:http';
test('connection form separates credentials, calendar selection and connected state',async()=>{
 const server=await createServer({root:fileURLToPath(new URL('../',import.meta.url)),configFile:false,plugins:[react()],server:{middlewareMode:true,hmr:{server:createHttpServer()},watch:null},appType:'custom'});
 try {
  const {YandexConnectionForm}=await server.ssrLoadModule('/src/YandexCalendar.jsx');
  const render=props=>renderToStaticMarkup(React.createElement(YandexConnectionForm,{connection:{configured:true,status:'disconnected'},secure:true,...props}));
  const initial=render({}); assert.match(initial,/type="password"/); assert.match(initial,/Проверить доступ/); assert.doesNotMatch(initial,/Подключить выбранный календарь|Синхронизировать встречи|localStorage|test-secret/);
  assert.match(render({secure:false}),/HTTPS/); assert.match(render({secure:false}),/disabled=""/);
  const selection=render({discovery:{generation:4,calendars:[{id:'safe-id',name:'Тестовый & календарь'}]}}); assert.match(selection,/Тестовый &amp; календарь/); assert.match(selection,/Подключить выбранный календарь/); assert.match(selection,/<button type="button" class="secondary-button" disabled="">/);
  const connected=render({connection:{configured:true,status:'connected',calendar_name:'Мой календарь'}}); assert.match(connected,/Мой календарь/); assert.match(connected,/Отключить/); assert.doesNotMatch(connected,/Синхронизировать встречи/); assert.doesNotMatch(connected,/type="password"|Проверить доступ/);
  assert.match(render({connection:{configured:true,status:'reconnect_required'}}),/Доступ отозван/);
  assert.doesNotMatch(render({connection:{configured:true,status:'reconnect_required'}}),/Синхронизировать встречи/);
  assert.match(initial,/Как получить пароль приложения/); assert.match(initial,/https:\/\/id.yandex.ru\/security\/app-passwords/);
  assert.match(render({connection:{configured:false,status:'disconnected'}}),/администратор/);
 }finally{await server.close();}
});
