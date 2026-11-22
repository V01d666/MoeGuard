import test from 'node:test';
import assert from 'node:assert/strict';
import manifest from '../i18n/manifest.json' with {type:'json'};
import en from '../i18n/en.json' with {type:'json'};
import zh from '../i18n/zh-CN.json' with {type:'json'};
import {createTranslator, resolveLocale, dictionaries} from '../src/i18n.ts';

const params = text => [...new Set([...text.matchAll(/\{(\w+)\}/g)].map(x=>x[1]))].sort();

test('every manifest locale has all messages with the same named parameters', ()=>{
  assert.deepEqual(Object.keys(dictionaries).sort(),Object.keys(manifest.languages).sort());
  assert.deepEqual(Object.keys(en).sort(),Object.keys(zh).sort());
  for (const [key,value] of Object.entries(zh)) {
    assert.ok(value.trim() && en[key]?.trim(),key);
    assert.deepEqual(params(en[key]),params(value),key);
  }
});

test('translator formats counts and leaves names inserted as literal text', ()=>{
  const {t, quantity} = createTranslator('en');
  assert.equal(quantity('images',1),'1 image');
  assert.equal(quantity('images',1200),'1,200 images');
  assert.equal(t('download.role',{name:'{name} <role> 气泡'}),'Download {name} <role> 气泡');
  assert.equal(createTranslator('zh-CN').quantity('images',1),'1 张');
  assert.equal(resolveLocale('en'),'en');
  assert.equal(resolveLocale('en-US'),'zh-CN');
  assert.equal(resolveLocale(null),'zh-CN');
});
