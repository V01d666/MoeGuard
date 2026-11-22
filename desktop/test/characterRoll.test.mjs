import assert from 'node:assert/strict';
import test from 'node:test';
import {rollCharacter} from '../src/characterRoll.ts';
import {initial, reduce} from '../src/workshop.ts';

test('rolling again changes the name and theme, preserves notes and names the saved pet',()=>{
  const base=initial();
  const first=rollCharacter(base.text,()=>0);
  const second=rollCharacter(first,()=>0);
  assert.notEqual(first.kind,second.kind);
  assert.notEqual(first.name,second.name);
  base.text.name=first.name;
  base.text.description='保留蓝色背包';
  const state=reduce(base,{kind:'roll',values:second});
  assert.equal(state.text.name,second.name);
  assert.equal(state.text.description,base.text.description);
  assert.equal(state.text.kind,second.kind);
  const draft=reduce(state,{kind:'candidates',items:[{id:'portrait',src:'preview'}]});
  const saved=reduce(draft,{kind:'save',id:'rolled-pet'});
  assert.equal(saved.roles.find(role=>role.id==='rolled-pet').name,second.name);
});

test('the same theme can roll different appearances',()=>{
  const source=values=>()=>values.shift();
  const empty=initial().text;
  const first=rollCharacter(empty,source([0,0,0,0,0,0,0]));
  const second=rollCharacter(empty,source([0,.9,.9,.9,.9,.9,.9]));
  assert.equal(first.kind,second.kind);
  for(const field of ['name','style','hair','eyes','outfit','accessory']) {
    assert.notEqual(first[field],second[field]);
  }
});

test('the dice can reach at least one hundred distinct character concepts',()=>{
  const concepts=new Set();
  const names=new Set();
  const empty=initial().text;
  for(let index=0; index<512; index++) {
    const roll=rollCharacter(empty,()=> (index+.5)/512);
    concepts.add(roll.kind);
    names.add(roll.name);
    assert(roll.outfit&&roll.accessory);
  }
  assert(concepts.size>100);
  assert(names.size>=40);
});
