import {useEffect, useRef, useState, type FormEvent} from 'react';
import {invoke} from '@tauri-apps/api/core';
import type {Message} from './i18n.ts';
import {Glyph} from './Art.tsx';

type Field = 'idle_threshold_sec'|'patrol_interval_sec'|'device_index'|'face_match_threshold'|
  'stranger_cooldown_sec'|'video_clip_sec'|'retention_days';
type Toggle = 'auto_patrol_enabled'|'blur_stranger_faces'|'motion_recording_enabled';
type Config = Record<Field,number> & Record<Toggle,boolean>;
type View = {consented:boolean; registered:boolean; config:Config};
type Reply = Partial<View> & {id?:number; busy?:boolean; pending?:boolean; preview?:string; events?:string[]; error?:string};
type Confirm = 'consent'|'withdraw'|'evidence_delete'|'evidence_delete_all';
const fields: [Field, Message, number, number, number][] = [
  ['device_index','patrol.camera',0,32,1], ['idle_threshold_sec','patrol.away',30,600,1],
  ['patrol_interval_sec','patrol.interval',0.5,5,0.1], ['face_match_threshold','patrol.threshold',0.1,0.9,0.001],
  ['stranger_cooldown_sec','patrol.cooldown',5,300,1], ['video_clip_sec','patrol.clip',5,60,1],
  ['retention_days','patrol.retention',1,90,1],
];
const toggles: [Toggle, Message][] = [
  ['auto_patrol_enabled','patrol.lock'], ['blur_stranger_faces','patrol.blur'],
  ['motion_recording_enabled','patrol.motion'],
];
const errorLabels: Record<string, Message> = {
  patrol_component_missing:'patrol.componentMissing', patrol_unavailable:'patrol.unavailable',
  setup_required:'patrol.setupRequired', patrol_busy:'patrol.busy', camera_unavailable:'patrol.cameraError',
  face_not_found:'patrol.faceError', withdraw_incomplete:'patrol.withdrawError', evidence_unavailable:'patrol.evidenceError',
};
async function call(op:string, extra:object={}):Promise<Reply> {
  const result=await invoke<Reply|null>('patrol_settings',{request:{op,...extra}})??{};
  if (result.error) throw result.error;
  return result;
}
async function wait(id:number, alive:()=>boolean) {
  for (let i=0; i<310 && alive(); i++) {
    const result=await call('poll',{id});
    if (!result.pending) return result;
    await new Promise(resolve=>setTimeout(resolve,125));
  }
  throw 'settings_failed';
}

export function PatrolSettings({t,active}:{t:(key:Message)=>string;active:boolean}) {
  const [supported,setSupported]=useState(true);
  const [view,setView]=useState<View|null>(null);
  const [busy,setBusy]=useState(false);
  const [notice,setNotice]=useState<Message|null>(null);
  const [preview,setPreview]=useState<{id:number; jpg:string}|null>(null);
  const [events,setEvents]=useState<string[]|null>(null);
  const [event,setEvent]=useState('');
  const [confirm,setConfirm]=useState<Confirm|null>(null);
  const dialog=useRef<HTMLDialogElement>(null);
  const mounted=useRef(false);
  const inFlight=useRef(false);
  useEffect(()=>{
    mounted.current=true;
    load();
    return ()=>{mounted.current=false;};
  },[]);
  useEffect(()=>{if (confirm) dialog.current?.showModal();},[confirm]);
  useEffect(()=>{
    if (active||!preview) return;
    setPreview(null);
    void call('cancel',{id:preview.id}).catch(()=>{});
  },[active,preview]);
  const alive=()=>mounted.current;
  function apply(result:Reply) {
    if (result.config && typeof result.consented==='boolean' && typeof result.registered==='boolean') {
      setView({config:result.config,consented:result.consented,registered:result.registered});
    }
    if (result.events) {setEvents(result.events);setEvent('');}
  }
  async function task(op:string,extra:object={}) {
    const {id}=await call(op,extra);
    if (typeof id!=='number') throw 'settings_failed';
    return {id,reply:await wait(id,alive)};
  }
  function run(action:()=>Promise<void>) {
    if (inFlight.current) return;
    inFlight.current=true;setBusy(true);setNotice(null);
    void action().catch(error=>{
      if (!alive()) return;
      if (error==='patrol_unsupported') setSupported(false);
      else setNotice(errorLabels[String(error)]??'patrol.error');
    }).finally(()=>{inFlight.current=false;if (alive()) setBusy(false);});
  }
  function load() {run(async()=>apply((await task('view')).reply));}
  function save(event:FormEvent) {
    event.preventDefault();
    if (!view) return;
    run(async()=>{apply((await task('save',{config:view.config})).reply);setNotice('patrol.saved');});
  }
  function change(key:Field|Toggle,value:number|boolean) {
    setView(previous=>previous?{...previous,config:{...previous.config,[key]:value}}:previous);
  }
  function confirmAction(op:Confirm) {
    setConfirm(null);
    run(async()=>{
      apply((await task(op,{confirmed:true,event:op==='evidence_delete'?event:undefined})).reply);
      if (op==='withdraw' && events) apply((await task('evidence')).reply);
      setNotice(op==='withdraw'?'patrol.withdrawn':'patrol.saved');
    });
  }
  if (!supported) return null;
  return <section className="panel patrol-settings" aria-labelledby="patrol-title">
    <header className="patrol-header">
      <div className="patrol-heading">
        <h2 id="patrol-title">{t('patrol.title')}</h2>
        {view?.consented&&<span className="patrol-owner">{t(view.registered?'patrol.registered':'patrol.unregistered')}</span>}
      </div>
      <button type="button" className="secondary patrol-refresh" disabled={busy||!!preview} onClick={load}>
        <Glyph name="refresh" size={15}/>{t('patrol.load')}
      </button>
    </header>
    {view&&<div className="patrol-buttons patrol-controls">
      {!view.consented
        ?<button type="button" className="secondary" disabled={busy}
          onClick={()=>setConfirm('consent')}>{t('patrol.consent')}</button>
        :!preview&&<button type="button" className="secondary" disabled={busy} onClick={()=>run(async()=>{
          const {id,reply}=await task('enroll');
          if (!reply.preview||!/^[A-Za-z0-9+/]+={0,2}$/.test(reply.preview)||reply.preview.length>131072) throw 'camera_unavailable';
          setPreview({id,jpg:reply.preview});
        })}>{t('patrol.preview')}</button>}
      <button type="button" className="secondary" disabled={busy||!!preview}
        onClick={()=>run(async()=>apply((await task('evidence')).reply))}>{t('patrol.evidence')}</button>
    </div>}
    {view&&<>
      {preview&&<div className="patrol-preview">
        <img src={`data:image/jpeg;base64,${preview.jpg}`} alt={t('patrol.preview')}/>
        <div className="patrol-buttons">
          <button type="button" className="secondary" disabled={busy} onClick={()=>run(async()=>{
            setPreview(null);await call('cancel',{id:preview.id});
          })}>{t('common.cancel')}</button>
          <button type="button" className="primary" disabled={busy} onClick={()=>run(async()=>{
            setPreview(null);await call('register',{id:preview.id});
            const reply=await wait(preview.id,alive);
            if (reply.registered!==true) throw 'settings_failed';
            apply((await task('view')).reply);
            setNotice('patrol.registered');
          })}>{t('patrol.register')}</button>
        </div>
      </div>}
      <details className="patrol-preferences"><summary>{t('patrol.preferences')}</summary>
        <form onSubmit={save}><fieldset disabled={busy||!!preview}>
          <div className="patrol-toggles">
            {toggles.map(([key,label])=><label key={key} className="patrol-toggle">
              <input type="checkbox" checked={view.config[key]} disabled={key==='auto_patrol_enabled'&&!(view.consented&&view.registered)}
                onChange={e=>change(key,e.currentTarget.checked)}/>{t(label)}
            </label>)}
          </div>
          <div className="patrol-fields">
            {fields.map(([key,label,min,max,step])=><label key={key}>
              {t(label)}<input type="number" min={min} max={key==='device_index'?Math.max(max,view.config[key]):max} step={step}
                required value={Number.isNaN(view.config[key])?'':view.config[key]}
                onChange={e=>change(key,e.currentTarget.valueAsNumber)}/>
            </label>)}
          </div>
          <button type="submit" className="secondary">{t('common.save')}</button>
        </fieldset></form>
      </details>
      {events&&<div className="patrol-evidence">
        <p>{t('patrol.evidenceHint')}</p>
        {events.length?<select aria-label={t('patrol.evidence')} value={event} onChange={e=>setEvent(e.currentTarget.value)}>
          <option value="">{t('patrol.chooseEvidence')}</option>
          {events.map(name=><option key={name} value={name}>{name}</option>)}
        </select>:<p>{t('patrol.noEvidence')}</p>}
        <div className="patrol-buttons">
          <button type="button" className="secondary" disabled={busy||!!preview} onClick={()=>run(async()=>{
            await task('evidence_open',{event:event||null});
          })}>{t(event?'patrol.openEvent':'patrol.openEvidence')}</button>
          <button type="button" className="secondary" disabled={busy||!!preview||!event}
            onClick={()=>setConfirm('evidence_delete')}>{t('patrol.deleteEvidence')}</button>
          <button type="button" className="secondary" disabled={busy||!!preview||!events.length}
            onClick={()=>setConfirm('evidence_delete_all')}>{t('patrol.deleteAll')}</button>
        </div>
      </div>}
      {view.consented&&<div className="patrol-privacy">
        <button type="button" className="secondary" disabled={busy||!!preview}
          onClick={()=>setConfirm('withdraw')}>{t('patrol.withdraw')}</button>
      </div>}
    </>}
    {notice&&<p role="status">{t(notice)}</p>}
    {confirm&&<dialog ref={dialog} aria-labelledby="patrol-confirm" onCancel={()=>setConfirm(null)}>
      <h3 id="patrol-confirm">{t(confirm==='consent'?'patrol.consent':confirm==='withdraw'?'patrol.withdraw':
        confirm==='evidence_delete'?'patrol.deleteEvidence':'patrol.deleteAll')}</h3>
      <p>{t(confirm==='consent'?'patrol.risk':confirm==='withdraw'?'patrol.withdrawHint':'patrol.evidenceHint')}</p>
      {confirm==='evidence_delete'&&<p>{event}</p>}
      <div className="patrol-buttons">
        <button type="button" className="secondary" onClick={()=>setConfirm(null)} autoFocus>{t('common.cancel')}</button>
        <button type="button" className="primary" onClick={()=>confirmAction(confirm)}>{t(confirm==='consent'?'patrol.agree':'common.ok')}</button>
      </div>
    </dialog>}
  </section>;
}
