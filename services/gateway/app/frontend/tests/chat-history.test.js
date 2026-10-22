import test from 'node:test';
import assert from 'node:assert/strict';
import {createHistoryPager} from '../src/network.js';

test('latest refresh keeps oldest cursor; upward requests are deduplicated and merged', async () => {
  let state, resolveOlder;
  const paths = [];
  const pager = createHistoryPager(async path => {
    paths.push(path);
    if (path.includes('cursor=')) return new Promise(resolve => {resolveOlder = resolve;});
    return paths.length === 1 ? {items: [{id:'b',created_at:'2'}],next_cursor:'older'} : {items:[{id:'c',created_at:'3'}],next_cursor:'wrong-newer-cursor'};
  }, next => {state = next;});
  await pager.load();
  await pager.load();
  assert.equal(state.cursor, 'older');
  const first = pager.load({older:true});
  const second = pager.load({older:true});
  assert.equal(paths.length, 3);
  resolveOlder({items:[{id:'a',created_at:'1'}],next_cursor:null});
  await Promise.all([first,second]);
  assert.deepEqual(state.messages.map(m=>m.id), ['a','b','c']);
  assert.equal(state.loadingOlder, false);
  await pager.load({older:true});
  assert.equal(paths.length, 3);
});

test('history reset fences in-flight pages and reconnect retains incoming live messages', async () => {
  let state, resolve;
  const pager = createHistoryPager(() => new Promise(r=>{resolve=r;}), next=>{state=next;});
  const pending = pager.load();
  pager.reset();
  resolve({items:[{id:'stale'}],next_cursor:'stale'});
  await pending;
  assert.deepEqual(state.messages, []);
  const reconnect = pager.load({replace:true});
  pager.merge([{id:'live',created_at:'2'}]);
  resolve({items:[{id:'saved',created_at:'1'}],next_cursor:null});
  await reconnect;
  assert.deepEqual(state.messages.map(m=>m.id), ['saved','live']);
});

test('prepending messages preserves the visible anchor instead of jumping to the bottom', async () => {
  const {captureScrollAnchor, restoreScrollAnchor, nearBottom} = await import('../src/chat-scroll.js');
  const container = {scrollTop: 300, scrollHeight: 1200, clientHeight: 400, getBoundingClientRect:()=>({top:0})};
  let position=320;
  const element={dataset:{messageId:'anchor'},getBoundingClientRect:()=>({top:position-container.scrollTop,bottom:position-container.scrollTop+50})};
  container.querySelectorAll=()=>[element];
  const anchor=captureScrollAnchor(container);
  position+=600; container.scrollHeight+=600;
  restoreScrollAnchor(container,anchor);
  assert.equal(container.scrollTop,900);
  assert.equal(element.getBoundingClientRect().top,20);
  assert.equal(nearBottom(container),false);
  container.scrollTop=1400;
  assert.equal(nearBottom(container),true);
});

test('legacy provider exceptions are presented without URLs or tracebacks', async()=>{
  const {jobErrorMessage}=await import('../src/network.js');
  assert.match(jobErrorMessage("Client error '402 Payment Required' for url 'https://provider'"),/баланс/);
  assert.doesNotMatch(jobErrorMessage('Traceback https://provider'),/https|Traceback/);
});

test('a rejected stale request after clearing history cannot display an obsolete error', async()=>{
  let reject;
  const pager=createHistoryPager(()=>new Promise((resolve,fail)=>{reject=fail;}),()=>{});
  const request=pager.load();
  pager.reset();
  reject(new Error('Old generation cursor'));
  await assert.doesNotReject(request);
});
