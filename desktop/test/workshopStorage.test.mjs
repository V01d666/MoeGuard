import assert from 'node:assert/strict';
import {test} from 'node:test';
import {initial} from '../src/workshop.ts';
import {createCheckpoint} from '../src/workshopStorage.ts';

const snapshot = name => ({...initial(), text:{...initial().text, name}});
const turn = () => new Promise(resolve=>setImmediate(resolve));

test('serializes native writes, skips intermediate snapshots, and flushes newest state', async () => {
  const held = Promise.withResolvers();
  const calls=[];
  const store=createCheckpoint({save:async s=>{calls.push(s.text.name); if(calls.length===1) await held.promise;}});
  store.schedule(snapshot('一'));
  await turn();
  store.schedule(snapshot('二'));
  store.schedule(snapshot('三'));
  let flushed=false;
  const closing=store.flush().then(()=>{flushed=true;});
  await turn();
  assert.deepEqual(calls,['一']); assert.equal(flushed,false);
  held.resolve(); await closing;
  assert.deepEqual(calls,['一','三']);
});

test('a failed native write keeps latest pending, retries on close, and never resets state', async () => {
  let failures=0;
  const calls=[];
  const store=createCheckpoint({save:async s=>{
    calls.push(s.text.name);
    if(calls.length===1) throw new Error('disk');
  }},()=>{failures++;});
  store.schedule(snapshot('未保存'));
  await turn();
  assert.equal(failures,1);
  await store.flush();
  assert.deepEqual(calls,['未保存','未保存']);
});

test('permanent errors reject close flush without discarding the uncommitted snapshot', async () => {
  const store=createCheckpoint({save:async()=>{throw new Error('conflict');}});
  store.schedule(snapshot('新草稿'));
  await assert.rejects(store.flush(),/conflict/);
  await assert.rejects(store.flush(),/conflict/);
});
