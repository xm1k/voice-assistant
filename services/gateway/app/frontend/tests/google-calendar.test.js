import test from 'node:test';
import assert from 'node:assert/strict';
import {syncMap,syncLabel,canRetrySync,retrySync} from '../src/google-calendar.js';
test('REST map replaces stale chat snapshots and preserves deleted record problems', () => {
  const map=syncMap([{record_id:'deleted',status:'failed'},{record_id:'meeting',status:'pending'},{record_id:'meeting',status:'synced'}]);
  assert.equal(map.meeting.status,'synced'); assert.equal(map.deleted.status,'failed');
});
test('only explicit synchronized state and connected account claim Google success', () => {
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
  assert.deepEqual(calls,[['/api/v1/integrations/google-calendar/operations/a%2Fb/retry',{method:'POST'}]]);
});

test('unexported meetings remain local and policy failures cannot be retried', () => {
  assert.equal(syncLabel(null,{configured:true,status:'connected'}),'Только в Голосе');
  for (const code of ['reconnect_required','calendar_unavailable',undefined]) assert.equal(canRetrySync({status:'failed',operation_id:'id',code}),false);
  assert.equal(canRetrySync({status:'failed',operation_id:'id',code:'failed'}),true);
  assert.equal(syncLabel({status:'conflict',error:'Событие изменено.'},{configured:true,status:'connected'}),'Google Calendar: Событие изменено.');
});

test('service outage and reconnect remain visible even for deleted meetings', () => {
  assert.match(syncLabel({status:'synced'},{configured:false,status:'unavailable'}),/недоступен/);
  assert.match(syncLabel({status:'disconnected',code:'reconnect_required'},{configured:true,status:'reconnect_required'}),/заново/);
});
