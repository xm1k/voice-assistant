import test from 'node:test';
import assert from 'node:assert/strict';
import {syncMap,syncLabel,canRetrySync,retrySync} from '../src/yandex-calendar.js';
test('REST map replaces stale chat snapshots and preserves deleted record problems', () => {
  const map=syncMap([{record_id:'deleted',status:'failed'},{record_id:'meeting',status:'pending'},{record_id:'meeting',status:'synced'}]);
  assert.equal(map.meeting.status,'synced'); assert.equal(map.deleted.status,'failed');
});
test('only explicit synchronized state and connected account claim Яндекс success', () => {
  const connected={configured:true,status:'connected'};
  assert.match(syncLabel({status:'synced'},connected),/синхронизировано/);
  for (const status of ['pending','failed','missing','conflict']) assert.doesNotMatch(syncLabel({status},connected),/синхронизировано/);
  assert.doesNotMatch(syncLabel({status:'synced'},{configured:true,status:'reconnect_required'}),/синхронизировано/);
  assert.match(syncLabel(null,{configured:false}),/не настроен/);
});
test('retry rejects policy blocks and uses operation id for technical failures', async () => {
  const calls=[],api=async (...args) => calls.push(args);
  for (const status of ['conflict','missing','synced','pending','disconnected']) {
    const item={status,operation_id:'operation'}; assert.equal(canRetrySync(item),false); await assert.rejects(retrySync(api,item));
  }
  await retrySync(api,{status:'failed',code:'delivery_exhausted',operation_id:'a/b'});
  assert.deepEqual(calls,[['/api/v1/integrations/yandex-calendar/operations/a%2Fb/retry',{method:'POST'}]]);
});

test('unexported meetings remain local and policy failures cannot be retried', () => {
  assert.equal(syncLabel(null,{configured:true,status:'connected'}),'Только в Голосе');
  for (const code of ['reconnect_required','calendar_unavailable',undefined]) assert.equal(canRetrySync({status:'failed',operation_id:'id',code}),false);
  assert.equal(canRetrySync({status:'failed',operation_id:'id',code:'failed'}),true);
  assert.equal(syncLabel({status:'conflict',error:'Событие изменено.'},{configured:true,status:'connected'}),'Яндекс Календарь: Событие изменено.');
});

test('service outage and reconnect remain visible even for deleted meetings', () => {
  assert.match(syncLabel({status:'synced'},{configured:false,status:'unavailable'}),/недоступен/);
  assert.match(syncLabel({status:'disconnected',code:'reconnect_required'},{configured:true,status:'reconnect_required'}),/заново/);
});

import {credentialsAllowed,discoverCalendars} from '../src/yandex-calendar.js';
test('credentials use POST body only and clear before success or failure', async () => {
  const secret={username:'test-user',password:'test-secret'}; let cleared=0; const calls=[];
  const result=await discoverCalendars(async (path,options) => {calls.push([path,options]); return {calendars:[]};},secret,true,()=>cleared++);
  assert.equal(cleared,2); assert.deepEqual(result,{calendars:[]});
  assert.equal(calls[0][0],'/api/v1/integrations/yandex-calendar/discover'); assert.equal(calls[0][1].method,'POST'); assert.deepEqual(JSON.parse(calls[0][1].body),secret);
  await assert.rejects(discoverCalendars(async()=>{throw new Error('private');},secret,true,()=>cleared++)); assert.equal(cleared,4);
  await assert.rejects(discoverCalendars(()=>{throw new Error('must not submit');},secret,false,()=>cleared++),/HTTPS/); assert.equal(cleared,5);
});
test('external HTTP refuses credentials while HTTPS and loopback work',()=>{
  assert.equal(credentialsAllowed(new URL('http://example.com')),false);
  assert.equal(credentialsAllowed(new URL('https://example.com')),true);
  for (const host of ['localhost','127.0.0.1','[::1]']) assert.equal(credentialsAllowed(new URL(`http://${host}`)),true);
  assert.equal(credentialsAllowed(new URL('http://localhost.attacker.example')),false);
});
