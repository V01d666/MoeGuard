// Development fixture only. The native service adapter must not reuse simulated balances or results.
export const actions = [
  'idle', 'notice', 'click_reaction', 'dragging', 'patrol', 'welcome', 'peek_left', 'peek_right',
] as const;
export type Action = typeof actions[number];
export type Tab = 'text' | 'image';
export type Portrait = {id: string; src: string; localName?: string};
export type Role = {id: string; name: string; portrait: Portrait; actions: Action[]};
export type Plan = {mode: 'generate' | 'revise'; actions: Action[]};
const names = [['lumen', 'Lumen'], ['poppy', 'Poppy'], ['rook', 'Rook'], ['raiden', 'Raiden'],
  ['caelus', 'Caelus'], ['arona', 'Arona'], ['phoenix', 'Phoenix'], ['changfeng', 'Changfeng']] as const;
export const bundledRole:Role = {id:'whale',name:'鲸鱼娘',portrait:fixture('whale',3),
  actions:['idle','click_reaction','dragging','peek_left']};
export const catalog = names.map(([id, name], i): Role => ({id, name, portrait: fixture(id, i),
  actions:['idle', 'click_reaction', 'dragging', 'peek_left']}));
export function fixture(id: string, i: number): Portrait { return {id, src: `demo:${i % 9}`}; }
const defaultText = {name:'', kind:'', style:'', hair:'', eyes:'', outfit:'', accessory:'', description:''};
export type Field = keyof typeof defaultText;
type JobInput = {id:string; type:'art'; cost:number; items:Portrait[]; append?:boolean; instruction?:string}
  | {id:string; type:'motion'; target:string; plan:Plan; instruction:string};
type Job = JobInput & {status:'running' | 'failed'};
export type State = {
  tab:Tab; text:typeof defaultText; count:number; files:Portrait[]; candidates:Portrait[]; candidate:string|null;
  roles:Role[]; removedRoles?:string[]; selected:string|null; existing:Action|null; missing:Action[]; active:string|null;
  balance:{art:number; motion:number}; job:Job|null;
};
export function initial(): State {
  return {tab:'text', text:{...defaultText}, count:2, files:[], candidates:[], candidate:null,
    roles:[bundledRole],
    selected:null, existing:null, missing:[], active:'whale', balance:{art:12,motion:18}, job:null};
}
export const selectedRole = (s: State) => s.roles.find(r => r.id === s.selected);
export const activeCandidate = (s: State) => s.candidates.find(p => p.id === s.candidate);
export function imageRoleName(filename: string): string {
  const stem = filename.replace(/\.[^./\\]+$/, '').replace(/[\x00-\x1f\x7f]/g, '').trim();
  return [...stem].slice(0,24).join('').trimEnd();
}
export const canEnable = (s: State) => !!selectedRole(s)?.actions.includes('idle');
export const isCatalogRole = (id: string) => id===bundledRole.id || catalog.some(role=>role.id===id);
export type WorkshopMode = 'browser' | 'preview' | 'service';
export function generationLane(mode: WorkshopMode, kind: 'art'|'motion', roleId?:string) {
  if (kind==='motion' && roleId && isCatalogRole(roleId) && mode!=='browser') {
    return mode==='service'?'native':'unavailable';
  }
  return mode==='service'?'unavailable':'demo';
}
export function discardDemoJob(state:State, mode:WorkshopMode):State {
  // Only the separate native revision journal can restore work in a service build.
  return mode==='service' && state.job ? {...state,job:null} : state;
}
export function reconcileNativeRoles(s: State, verified: Role[]): State {
  // Owned catalog roles come from the native inventory, not old demo snapshots.
  const roles = [...verified.map(role=>({...role,name:s.roles.find(old=>old.id===role.id)?.name ?? role.name})),
    ...s.roles.filter(role=>!isCatalogRole(role.id))].filter(role=>!s.removedRoles?.includes(role.id));
  const selected = roles.find(role=>role.id===s.selected);
  const fallback = s.active && roles.some(role=>role.id==='whale') ? 'whale' : null;
  return {...s,roles,selected:selected?.id ?? null,
    active:roles.some(role=>role.id===s.active)?s.active:fallback,
    existing:s.existing && selected?.actions.includes(s.existing)?s.existing:null,
    missing:selected?s.missing.filter(action=>!selected.actions.includes(action)):[]};
}
export function actionPlan(s: State): Plan | null {
  const role = selectedRole(s);
  if (!role) return null;
  if (s.existing && role.actions.includes(s.existing)) return {mode:'revise', actions:[s.existing]};
  const next = actions.filter(a => !role.actions.includes(a) && (s.missing.includes(a) || a === 'idle'));
  return next.length ? {mode:'generate', actions:next} : null;
}
export type WorkshopEvent =
  | {kind:'tab'; tab:Tab} | {kind:'text'; key:Field; value:string} | {kind:'count'; count:number}
  | {kind:'roll'; values:Partial<State['text']>} | {kind:'files'|'candidates'|'append-candidates'; items:Portrait[]}
  | {kind:'local-candidates'|'new'|'enable'|'resume'|'clear-failed'} | {kind:'candidate'|'select'|'download'|'save'; id:string}
  | {kind:'catalog-installed'; role:Role}
  | {kind:'custom-installed'; id:string; actions:Action[]}
  | {kind:'save-native-portrait'; id:string; name:string; portrait:Portrait}
  | {kind:'rename'; id:string; name:string} | {kind:'delete-role'; id:string}
  | {kind:'action'; action:Action} | {kind:'apply-actions'; plan:Plan|null}
  | {kind:'begin'; job:JobInput} | {kind:'job-failed'|'complete'; id:string};
function spendMotion(s: State, plan: Plan|null): State {
  const role = selectedRole(s);
  const expected = actionPlan(s);
  if (!role || !plan || !expected || plan.mode !== expected.mode ||
    plan.actions.join() !== expected.actions.join() || plan.actions.length > s.balance.motion) return s;
  const updated = {...role, actions:[...new Set([...role.actions, ...plan.actions])]};
  return {...s, roles:s.roles.map(r => r.id === role.id ? updated : r),
    existing:plan.actions[0], missing:[], balance:{...s.balance, motion:s.balance.motion-plan.actions.length}};
}
export function reduce(s: State, e: WorkshopEvent): State {
  if (s.job?.status === 'running' && !['complete','job-failed','catalog-installed'].includes(e.kind)) return s;
  switch (e.kind) {
    case 'tab': return {...s, tab:e.tab};
    case 'text': return {...s, text:{...s.text,[e.key]:e.value}};
    case 'count': return {...s, count:Math.max(1, Math.min(4,e.count))};
    case 'roll': return {...s, text:{...s.text, ...e.values}};
    case 'files': return {...s, files:e.items.slice(0,4)};
    case 'local-candidates':
      return s.files.length ? reduce({...s,files:[]},{kind:'candidates',items:s.files}) : s;
    case 'new': return {...s,selected:null,candidates:[],candidate:null,existing:null,missing:[],job:null};
    case 'candidates': return {...s,selected:null,candidates:e.items,candidate:e.items[0]?.id ?? null,existing:null,missing:[]};
    case 'append-candidates': {
      const appended=e.items.filter(item=>!s.candidates.some(candidate=>candidate.id===item.id));
      return appended.length?{...s,candidates:[...s.candidates,...appended],candidate:appended[0].id}:s;
    }
    case 'candidate': return s.candidates.some(p => p.id === e.id) ? {...s,candidate:e.id} : s;
    case 'select': {
      const role = s.roles.find(r => r.id===e.id); if (!role) return s;
      return {...s,selected:role.id,candidates:[],candidate:null,existing:role.actions.includes('idle')?'idle':null,missing:[],job:null};
    }
    case 'save': {
      const p = activeCandidate(s); if (!p || s.roles.some(r=>r.id===e.id)) return s;
      const role = {id:e.id,name:(p.localName ?? s.text.name.trim()) || '未命名桌宠',portrait:p,actions:[]};
      return reduce({...s,roles:[...s.roles,role]}, {kind:'select',id:role.id});
    }
    case 'save-native-portrait': {
      if (s.roles.some(role=>role.id===e.id)) return s;
      const role={id:e.id,name:e.name,portrait:e.portrait,actions:[]};
      return reduce({...s,roles:[...s.roles,role]}, {kind:'select',id:role.id});
    }
    case 'rename': {
      const name = e.name.trim().slice(0,24);
      return name ? {...s,roles:s.roles.map(r => r.id === e.id ? {...r,name} : r)} : s;
    }
    case 'delete-role': {
      if (!s.roles.some(role=>role.id===e.id)) return s;
      const selected=s.selected===e.id;
      const roleJob=s.job?.type==='motion'&&s.job.target===e.id;
      return {...s,roles:s.roles.filter(role=>role.id!==e.id),removedRoles:[...(s.removedRoles??[]),e.id],
        active:s.active===e.id?null:s.active,
        ...(selected?{selected:null,existing:null,missing:[]}:{}),
        ...(roleJob?{job:null}:{})};
    }
    case 'download': {
      const role = e.id===bundledRole.id?bundledRole:catalog.find(r=>r.id===e.id);
      return role?reduce(s,{kind:'catalog-installed',role}):s;
    }
    case 'catalog-installed':
      return s.roles.some(r=>r.id===e.role.id) ? s : {...s,roles:[...s.roles,e.role],
        removedRoles:s.removedRoles?.filter(id=>id!==e.role.id)};
    case 'custom-installed': {
      const installed = actions.filter(action=>e.actions.includes(action));
      const role = s.roles.find(r=>r.id===e.id);
      if (!role || (role.actions.length===installed.length && installed.every(action=>role.actions.includes(action)))) return s;
      return {...s,roles:s.roles.map(r=>r===role?{...r,actions:installed}:r),
        ...(s.selected===e.id?{
          existing:s.existing&&installed.includes(s.existing)?s.existing:null,
          missing:s.missing.filter(action=>!installed.includes(action)),
        }:{})};
    }
    case 'action': {
      const role = selectedRole(s); if (!role) return s;
      if (role.actions.includes(e.action)) return {...s,existing:e.action,missing:[]};
      if (e.action==='idle') return s;
      return {...s,existing:null,missing:s.missing.includes(e.action) ? s.missing.filter(a=>a!==e.action) : [...s.missing,e.action]};
    }
    case 'apply-actions': return spendMotion(s,e.plan);
    case 'enable': return canEnable(s) && s.selected ? {...s,active:s.selected} : s;
    case 'begin': {
      if (s.job) return s;
      if (e.job.type==='art' && (e.job.cost<1 || e.job.cost>s.balance.art)) return s;
      if (e.job.type==='motion' && (e.job.target!==s.selected || !e.job.plan.actions.length || e.job.plan.actions.length>s.balance.motion)) return s;
      return {...s,job:{...e.job,status:'running'}};
    }
    case 'job-failed': return s.job?.id===e.id && s.job.status==='running' ? {...s,job:{...s.job,status:'failed'}} : s;
    case 'resume': return s.job?.status==='failed' ? {...s,job:{...s.job,status:'running'}} : s;
    case 'clear-failed': return s.job?.status==='failed' ? {...s,job:null} : s;
    case 'complete': {
      const job = s.job; if (job?.id!==e.id || job.status!=='running') return s;
      const base = {...s,job:null};
      return job.type==='motion' ? (s.selected===job.target ? spendMotion(base,job.plan) : base) :
        {...reduce(base,{kind:'candidates',items:[...(job.append?s.candidates:[]),...job.items]}),
          candidate:job.append ? job.items[0]?.id ?? s.candidate : job.items[0]?.id ?? null,
          balance:{...s.balance,art:s.balance.art-job.cost}};
    }
  }
}
