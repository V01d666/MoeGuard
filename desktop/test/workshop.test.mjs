import assert from 'node:assert/strict';
import {test} from 'node:test';
import {initial, reduce, actionPlan, canEnable, catalog, generationLane, discardDemoJob, imageRoleName, reconcileNativeRoles, selectedRole} from '../src/workshop.ts';

const run = (...events) => events.reduce(reduce, initial());
const portraits = [{id:'a', src:'demo:a'}, {id:'b', src:'demo:b'}];
const saved = [{kind:'candidates', items:portraits}, {kind:'save', id:'own'}];

test('deleted pets stay out of the library until an official role is added again', () => {
  const state = run(...saved, {kind:'delete-role',id:'own'}, {kind:'select',id:'whale'}, {kind:'delete-role',id:'whale'});
  assert.deepEqual(state.roles, []);
  assert.equal(state.selected, null);
  assert.equal(state.active, null);
  const restored = reconcileNativeRoles(JSON.parse(JSON.stringify(state)), initial().roles);
  assert.deepEqual(restored.roles, []);
  const added = reduce(restored,{kind:'catalog-installed',role:initial().roles[0]});
  assert.deepEqual(reconcileNativeRoles(added, initial().roles).roles, initial().roles);
  assert.deepEqual(added.removedRoles,['own']);
});

test('service builds never use demo generation, even when a session cannot be restored', () => {
  assert.equal(generationLane('service', 'art'), 'unavailable');
  assert.equal(generationLane('service', 'motion', 'own'), 'unavailable');
  assert.equal(generationLane('service', 'motion', 'whale'), 'native');
  assert.equal(generationLane('preview', 'art'), 'demo');
  assert.equal(generationLane('preview', 'motion', 'own'), 'demo');
  assert.equal(generationLane('preview', 'motion', 'whale'), 'unavailable');
  assert.equal(generationLane('browser', 'motion', 'whale'), 'demo');
});

test('service startup ignores a saved demo job without deleting the uploaded draft', () => {
  const base = run({kind:'tab',tab:'image'}, {kind:'files',items:portraits},
    {kind:'begin',job:{id:'demo',type:'art',cost:2,items:portraits}});
  assert.equal(discardDemoJob(base,'preview'), base);
  const service = discardDemoJob(base,'service');
  assert.equal(service.job, null);
  assert.equal(service.tab, 'image');
  assert.deepEqual(service.files, portraits);
  assert.deepEqual(service.balance, base.balance);
  assert.equal(base.job.id, 'demo');
});

test('new role clears D but keeps the current tab, text and selected local files', () => {
  const state = run({kind:'tab', tab:'image'}, {kind:'text', key:'description', value:'圆圆的鲸鱼'},
    {kind:'files', items:portraits}, ...saved, {kind:'new'});
  assert.equal(state.tab, 'image'); assert.equal(state.text.description, '圆圆的鲸鱼');
  assert.deepEqual(state.files, portraits); assert.equal(state.selected, null);
  assert.deepEqual(state.candidates, []); assert.equal(state.roles.at(-1).id, 'own');
});

test('local images are capped at four; only the selected candidate is saved', () => {
  const uploaded = run({kind:'files', items:Array.from({length:6}, (_, i) => ({id:String(i),src:`demo:${i}`}))});
  assert.equal(uploaded.files.length, 4);
  const confirmed = reduce(uploaded,{kind:'local-candidates'});
  assert.equal(confirmed.files.length, 0);
  assert.equal(confirmed.candidates.length, 4);
  const state = [{kind:'candidate', id:'2'}, {kind:'save', id:'own'}].reduce(reduce,confirmed);
  assert.deepEqual(state.roles.at(-1).portrait, {id:'2',src:'demo:2'});
  assert.equal(state.balance.art, initial().balance.art);
});

test('idle starts selected and is required; existing action changes cannot mix with additions', () => {
  let state = run(...saved, {kind:'action', action:'idle'}, {kind:'action', action:'click_reaction'});
  assert.deepEqual(actionPlan(state), {mode:'generate',actions:['idle','click_reaction']});
  assert.equal(canEnable(state), false);
  state = reduce(state, {kind:'apply-actions', plan:actionPlan(state)});
  assert.equal(canEnable(state), true);
  state = reduce(reduce(state, {kind:'action',action:'dragging'}), {kind:'action',action:'click_reaction'});
  assert.deepEqual(actionPlan(state), {mode:'revise',actions:['click_reaction']});
  assert.equal(state.balance.motion, initial().balance.motion - 2);
});

test('stale completion and duplicate download do not change the selected role or charge twice', () => {
  const a = run(...saved); const plan = actionPlan(a);
  const b = reduce(a, {kind:'new'});
  assert.equal(reduce(b, {kind:'apply-actions', plan}), b);
  let c = reduce(a, {kind:'download', id:'rook'});
  assert.equal(c.selected, 'own');
  assert.equal(reduce(c, {kind:'download', id:'rook'}), c);
  assert.deepEqual(actionPlan(reduce(c, {kind:'select', id:'rook'})), {mode:'revise',actions:['idle']});
  assert.equal(catalog.length, 8);
});

test('restore interrupted generation and apply each completion once', () => {
  let s = run({kind:'text',key:'description',value:'圆圆的鲸鱼'}, {kind:'begin', job:{id:'x',type:'art',cost:2,label:'立绘',items:portraits}});
  s = JSON.parse(JSON.stringify(s));
  s = reduce(s, {kind:'job-failed', id:'x'});
  assert.equal(s.job.status, 'failed'); assert.equal(s.balance.art, initial().balance.art);
  s = reduce(s, {kind:'resume'});
  assert.equal(s.job.id, 'x'); assert.equal(s.job.status, 'running');
  s = reduce(s, {kind:'complete', id:'x'});
  assert.deepEqual(s.candidates, portraits); assert.equal(s.balance.art, initial().balance.art - 2);
  assert.equal(reduce(s, {kind:'complete',id:'x'}), s);
});
test('image uploads use the selected filename and still support role renaming',()=>{
  const first={id:'a',src:'demo:1',localName:imageRoleName('mypet.png')};
  const second={id:'b',src:'demo:2',localName:imageRoleName('第二只.参考.JPG')};
  let state=reduce(initial(),{kind:'text',key:'name',value:'Earlier text prompt'});
  state=reduce(state,{kind:'files',items:[first,second]});
  state=reduce(state,{kind:'local-candidates'});
  state=reduce(state,{kind:'candidate',id:second.id});
  state=reduce(state,{kind:'save',id:'imported'});
  assert.equal(selectedRole(state).name,'第二只.参考');
  assert.equal(first.localName,'mypet');
  state=reduce(state,{kind:'rename',id:'imported',name:'新名字'});
  assert.equal(selectedRole(state).name,'新名字');
  assert.equal(state.text.name,'Earlier text prompt');
});
