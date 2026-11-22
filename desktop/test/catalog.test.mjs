import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {catalog, initial, reconcileNativeRoles, reduce} from '../src/workshop.ts';

const arona = {...catalog.find(role=>role.id==='arona'), portrait:{id:'arona',src:'data:image/png;base64,AA=='}, actions:['idle','peek_right']};

test('Windows candidates include the native pet and installed updater, separate from preview', () => {
  const read = path=>readFileSync(new URL(path,import.meta.url),'utf8');
  const {scripts}=JSON.parse(read('../package.json'));
  assert.match(scripts['build:windows'],/tauri build --no-bundle --config src-tauri\/tauri\.candidate\.conf\.json --features legacy-workshop,pet-win32,windows-update -- --locked/);
  assert.match(scripts['build:windows:patrol'],/--features patrol-worker,windows-update -- --locked/);
  const release = JSON.parse(read('../src-tauri/tauri.candidate.conf.json'));
  assert.equal(release.productName,'MoeGuard');
  assert.notEqual(release.identifier,JSON.parse(read('../src-tauri/tauri.conf.json')).identifier);
  const featureGuard = read('../src-tauri/src/role_catalog/mod.rs').match(/#\[cfg\(all\(\s*feature = "legacy-workshop",[^]*?compile_error!\([^;]+;/)?.[0] ?? '';
  assert.match(featureGuard,/any\(not\(windows\), feature = "revision-loopback"\)/);
});

test('native inventory replaces fake roles and uses verified actions while retaining the user name', () => {
  const base = reduce(reduce(reduce(initial(),{kind:'download',id:'arona'}),{kind:'select',id:'arona'}),{kind:'rename',id:'arona',name:'阿罗娜'});
  const native = reconcileNativeRoles({...base,existing:'dragging'},[base.roles[0],arona]);
  assert.deepEqual(native.roles.find(r=>r.id==='arona'),{...arona,name:'阿罗娜'});
  assert.equal(native.existing,null);
  assert.equal(native.selected,'arona');
  assert.equal(native.active,'whale');
  assert.deepEqual(native.balance,base.balance);
});

test('an unverified catalog role is not restored as owned or active', () => {
  const base = reduce(reduce(initial(),{kind:'download',id:'arona'}),{kind:'select',id:'arona'});
  const native = reconcileNativeRoles({...base,active:'arona'},[base.roles[0]]);
  assert.deepEqual(native.roles,initial().roles);
  assert.equal(native.selected,null);
  assert.equal(native.active,'whale');
});

test('a download completing during a job joins the library once without selecting or charging', () => {
  const base = reduce(initial(),{kind:'begin',job:{id:'job',type:'art',label:'立绘',cost:1,items:[]}});
  const installed = reduce(base,{kind:'catalog-installed',role:arona});
  assert.equal(installed.roles.at(-1),arona);
  assert.equal(reduce(installed,{kind:'catalog-installed',role:arona}),installed);
  assert.equal(installed.job,base.job);
  assert.equal(installed.selected,base.selected);
  assert.equal(installed.active,base.active);
  assert.equal(installed.balance,base.balance);
});
