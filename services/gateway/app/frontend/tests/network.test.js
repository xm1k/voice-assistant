import test from "node:test";
import assert from "node:assert/strict";
import {collectPages, mergeMessages, eventGate} from "../src/network.js";
test("collects opaque cursors and retains filters", async () => {
 const paths=[]; const result=await collectPages(async path => {paths.push(path); return paths.length===1 ? {items:[1],next_cursor:"a+/="} : {items:[2],next_cursor:null};}, "/records?kind=task");
 assert.deepEqual(result,[1,2]); assert.equal(new URL(paths[1],"http://local").searchParams.get("cursor"),"a+/="); assert.match(paths[1],/kind=task/);
});
test("rejects repeated cursor instead of infinite fetching", async()=> {await assert.rejects(collectPages(async()=>({items:[],next_cursor:"same"}),"/records"),/cursor/);});
test("message reconciliation dedupes IDs and preserves chronological order",()=>{assert.deepEqual(mergeMessages([{id:"b",created_at:"2"}],[{id:"a",created_at:"1"},{id:"b",created_at:"2",text:"updated"}]).map(x=>x.id),["a","b"]);});
test("event IDs are handled once",()=>{const accept=eventGate();assert.equal(accept("a"),true);assert.equal(accept("a"),false);assert.equal(accept("b"),true);});
