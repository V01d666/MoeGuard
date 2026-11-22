import {useEffect, useRef, useState, type ChangeEvent, type ReactNode} from 'react';
import {createRoot} from 'react-dom/client';
import {invoke} from '@tauri-apps/api/core';
import {listen} from '@tauri-apps/api/event';
import {actions, actionPlan, bundledRole, canEnable, catalog, discardDemoJob, fixture, generationLane, initial, isCatalogRole, imageRoleName, reconcileNativeRoles, reduce, selectedRole, activeCandidate, type WorkshopMode,
  type Action, type Field, type Portrait, type Role, type State, type WorkshopEvent} from './workshop.ts';
import {openDemo, persist, restore} from './demoStorage.ts';
import {isNativeWorkshop, nativeCredits, nativeDownloads, nativeRevision, openNativeWorkshop, readActionPreview, readNativeInventory,
  type CreditView, type InstalledPet, type MotionDraft, type NativeDownloads, type PendingMail, type PortraitGallery, type RevisionRef, type RevisionTask, type RevisionView, type WatchClose} from './nativeStorage.ts';
import {Mailbox} from './Mailbox.tsx';
import {WorkshopHelp} from './WorkshopHelp.tsx';
import roleManifest from '../role-catalog-v1.json';
import {createCheckpoint, type WorkshopStorage} from './workshopStorage.ts';
import {Art, Glyph, MoeGuardMark, RoleCardArt} from './Art.tsx';
import {rollCharacter} from './characterRoll.ts';
import {createTranslator, defaultLocale, type Locale, type Message} from './i18n.ts';
import {localePreference} from './localePreference.ts';
import {useUiLocale} from './useUiLocale.ts';
import './workshop.css';

type Dialog = {kind:'download' | 'manageRole' | 'deleteRole'; role:Role} | {kind:'credits' | 'feedback' | 'help' | 'motion' | 'inputRejected'} | {kind:'cancelTask';remote:string} | {kind:'discardMotion';task:RevisionRef};
const fields = ['name','kind','style','hair','eyes','outfit','accessory'] as const satisfies readonly Field[];
const packsMb = new Map(roleManifest.map(role=>[role.id,Math.round(role.bytes/1024**2*10)/10]));
const catalogProfiles = new Map(roleManifest.map(role=>[role.id,role.role_id]));
const catalogPosters = import.meta.glob<string>('../assets/posters/*/poster.webp', {
  eager:true, import:'default', query:'?url',
});
const localFileLimit=64*1024*1024;
const previewFileLimit=20*1024*1024;
const uploadImageEdge=2048;
const pngPrefix='data:image/png;base64,';
const sampleCode = 'MG-ABCD-EFGH-JKLM-NPQR';
function dataUrl(file:File):Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error);
    reader.onload = () => resolve(String(reader.result));
    reader.readAsDataURL(file);
  });
}
function rasterizePicture(image:HTMLImageElement):string {
  const scale=Math.min(1,uploadImageEdge/Math.max(image.naturalWidth,image.naturalHeight));
  const canvas=document.createElement('canvas');
  canvas.width=Math.max(1,Math.round(image.naturalWidth*scale));
  canvas.height=Math.max(1,Math.round(image.naturalHeight*scale));
  const context=canvas.getContext('2d');
  if (!context) throw new Error('image conversion unavailable');
  context.drawImage(image,0,0,canvas.width,canvas.height);
  const png=canvas.toDataURL('image/png');
  if (!png.startsWith(pngPrefix)) throw new Error('image conversion failed');
  return png;
}
async function readImage(file:File):Promise<Portrait> {
  if (!file.size||file.size>localFileLimit) throw new Error('image size exceeds local limit');
  let src=await dataUrl(file);
  const image=new Image();image.src=src;
  try {
    await image.decode();
    if (!/^data:image\/(png|jpeg|gif|webp|bmp|avif|apng|x-icon|vnd\.microsoft\.icon);base64,/i.test(src)||
        file.size>previewFileLimit||Math.max(image.naturalWidth,image.naturalHeight)>4096) {
      src=rasterizePicture(image);
    }
  } catch (error) {
    if (!isNativeWorkshop()) throw error;
    src=await invoke<string>('decode_workshop_picture',{input:src.slice(src.indexOf(',')+1)});
  }
  return {id:crypto.randomUUID(),src,localName:imageRoleName(file.name)};
}
async function portraitPng(src:string, preserveIdentity=false) {
  const image=new Image();image.src=src;await image.decode();
  if (preserveIdentity && src.startsWith(pngPrefix+'iVBORw0KGgo') &&
      src.length-pngPrefix.length<=24*1024*1024 &&
      Math.max(image.naturalWidth,image.naturalHeight)<=4096) return src.slice(pngPrefix.length);
  return rasterizePicture(image).slice(pngPrefix.length);
}
function Modal({title,close,children,className}: {title:string;close:()=>void;children:ReactNode;className?:string}) {
  const element = useRef<HTMLDialogElement>(null);
  useEffect(() => {element.current?.showModal();}, []);
  return <dialog ref={element} className={className} aria-labelledby="dialog-title" onCancel={event=>{event.preventDefault();close();}}>
    <h2 id="dialog-title">{title}</h2>{children}
  </dialog>;
}
function ActionPreview({role,action,installed,label,loading,error}: {
  role:Role; action:Action; installed:InstalledPet|null; label:string; loading:string; error:string;
}) {
  const [src,setSrc] = useState('');
  const [failed,setFailed] = useState(false);
  const id = installed?.role_id??role.id;
  const remote = installed?.remote_task_id;
  const digest = installed?.result_sha256;
  useEffect(()=>{
    let current = true;
    let url: string|undefined;
    void readActionPreview(id,action,remote&&digest?{remote_task_id:remote,result_sha256:digest}:null)
      .then(result=>{
        if (!current) return;
        const prefix='data:image/png;base64,';
        if (!result.startsWith(prefix)) throw new Error('Unexpected preview encoding');
        const pixels = Uint8Array.from(atob(result.slice(prefix.length)), value=>value.charCodeAt(0));
        url=URL.createObjectURL(new Blob([pixels],{type:'image/png'}));
        setSrc(url);
      }).catch(()=>{if (current) setFailed(true);});
    return ()=>{current=false;if(url) URL.revokeObjectURL(url);};
  },[id,action,remote,digest]);
  if (failed) return <div className="empty" role="status">{error}</div>;
  if (!src) return <div className="empty" role="status">{loading}</div>;
  return <img className="pet-art" src={src} alt={label} onError={()=>setFailed(true)}/>;
}

const waitingFaces=['(｡•̀ᴗ-)✧','(•̀ᴗ•́)و','( ˘▽˘)っ','(｡•ᴗ•｡)'];
const canCancel = (task:RevisionTask|null|undefined) => task?.status==='queued'||task?.status==='running';

function RoleCardName({name}:{name:string}) {
  const viewport=useRef<HTMLSpanElement>(null);
  useEffect(()=>{
    const clip=viewport.current;
    const text=clip?.firstElementChild;
    if (!clip||!text) return;
    const measure=()=>{
      const overflow=Math.max(0,text.getBoundingClientRect().width-clip.clientWidth);
      clip.classList.toggle('scrolling',overflow>1);
      clip.style.setProperty('--name-travel',`-${overflow}px`);
      clip.style.setProperty('--name-duration',`${1200+overflow*25}ms`);
    };
    const observer=new ResizeObserver(measure);
    observer.observe(clip);
    observer.observe(text);
    measure();
    return ()=>observer.disconnect();
  },[name]);
  return <span className="tile-name">
    <span className="tile-name-viewport" ref={viewport}><span className="tile-name-text">{name}</span></span>
  </span>;
}

function GenerationFooter({label,progress,waiting,children}:{
  label:string; progress?:number; waiting:boolean; children?:ReactNode;
}) {
  const [face,setFace] = useState(0);
  useEffect(()=>{
    if (!waiting || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const timer=window.setInterval(()=>setFace(index=>(index+1)%waitingFaces.length),2400);
    return ()=>window.clearInterval(timer);
  },[waiting]);
  return <div className="generation-footer">
    <div className="generation-summary">
      {waiting&&<span className="generation-face" aria-hidden="true">{waitingFaces[face]}</span>}
      <div className="generation-progress">
        <span role="status">{label}</span>
        {progress!==undefined&&<progress aria-label={label} max={100} value={progress}/>}
      </div>
    </div>
    {children&&<div className="generation-controls">{children}</div>}
  </div>;
}
function App({base,storage,watchClose,remote,startLocale,mode='browser',initialInstalled}: {
  base:State; storage:WorkshopStorage|null; watchClose?:WatchClose; remote?:NativeDownloads;
  startLocale:Locale; mode?:WorkshopMode; initialInstalled?:InstalledPet[]|null;
}) {
  const [s,setState] = useState(base);
  const stateRef = useRef(base);
  const [dialog,setDialog] = useState<Dialog|null>(null);
  const [roleDeleteFailed,setRoleDeleteFailed] = useState(false);
  const [mailPending,setMailPending] = useState<PendingMail|null>(null);
  const [mailBusy,setMailBusy] = useState(false);
  const [input,setInput] = useState('');
  const [credits,setCredits] = useState<CreditView|null>(null);
  const [creditNotice,setCreditNotice] = useState<Message|null>(null);
  const [cancelUnconfirmed,setCancelUnconfirmed] = useState<string|null>(null);
  const [allInstalledPets,setInstalledPets] = useState<InstalledPet[]>(initialInstalled??[]);
  const removedIds=new Set(s.removedRoles?.flatMap(id=>[id,catalogProfiles.get(id)??id]));
  const installedPets=new Map(allInstalledPets.filter(pet=>!removedIds.has(pet.role_id)).map(pet=>[pet.role_id,pet]));
  const cloudRoles=removedIds.has(bundledRole.id)?[bundledRole,...catalog]:catalog;
  const [nativePlayback,setNativePlayback] = useState(Array.isArray(initialInstalled));
  const [editing,setEditing] = useState(false);
  const [instruction,setInstruction] = useState('');
  const [notice,setNotice] = useState<Message|null>(null);
  const {locale,t,number,quantity} = useUiLocale(startLocale,()=>setNotice('locale.saveError'));
  const [previewRun,setPreviewRun] = useState(0);
  useEffect(()=>{
    document.documentElement.lang=locale;
    document.title=t('windowTitle');
  },[locale,t]);

  const [revision,setRevision] = useState<RevisionView|null>();
  const [revisionBusy,setRevisionBusy] = useState(false);
  const revisionInFlight = useRef<Promise<void>|null>(null);
  const revisionPoll = useRef<Promise<RevisionView|null|undefined>|null>(null);
  const closing = useRef(false);
  const [motionSaveAttempt,setMotionSaveAttempt] = useState('');
  const [motionDraft,setMotionDraft] = useState<MotionDraft|null>(null);
  const motionPreviewAttempt = useRef('');
  const [generationSubmitting,setGenerationSubmitting] = useState<'create'|'edit'|'motion'|null>(null);
  const [portraitLoadAttempt,setPortraitLoadAttempt] = useState('');
  const [portraitAppendAttempt,setPortraitAppendAttempt] = useState('');
  const [portraitGallery,setPortraitGallery] = useState<PortraitGallery|null>(null);
  const [portraitChoice,setPortraitChoice] = useState('');
  const [loadedPortrait,setLoadedPortrait] = useState('');
  const [artCredits,setArtCredits] = useState<CreditView|null>(null);
  const [storageIssue,setStorageIssue] = useState(!storage);
  const [checkpoint] = useState(()=>storage && createCheckpoint(storage,
    ()=>setStorageIssue(true), ()=>setStorageIssue(false)));
  function dispatch(event: WorkshopEvent) {
    const next = reduce(stateRef.current,event);
    if (next === stateRef.current) return;
    stateRef.current = next;
    checkpoint?.schedule(next);
    setState(next);
  }
  const [downloads,setDownloads] = useState<Record<string,number>>({});
  const [cancelling,setCancelling] = useState<string|null>(null);
  const [loadingFiles,setLoadingFiles] = useState(false);
  const [fileDrag,setFileDrag] = useState(false);
  const [slider,setSlider] = useState({position:0,max:0});
  const library = useRef<HTMLElement>(null);
  const strip = useRef<HTMLDivElement>(null);
  const picker = useRef<HTMLInputElement>(null);
  const stage = useRef<HTMLDivElement>(null);
  const role = selectedRole(s), candidate = activeCandidate(s), plan = actionPlan(s);
  const portraitTask = !!revision?.pending && revision.task_kind==='portraits';
  const portraitEditTask = portraitTask && !!revision?.portrait_edit_task;
  const portraitSubmitting = generationSubmitting==='create'||generationSubmitting==='edit';
  const portraitCreateTask = (portraitTask && !portraitEditTask) || generationSubmitting==='create';
  const revisionTask = revision?.task;
  const canCancelTask = canCancel(revisionTask);
  const taskRunning = canCancelTask||revisionTask?.status==='cancel_requested';
  const portraitCreateReady = portraitCreateTask && revisionTask?.status==='succeeded';
  const portraitEditReady = portraitEditTask && revisionTask?.status==='succeeded';
  const motionReady = !!revision?.pending&&!portraitTask&&revisionTask?.status==='succeeded';
  const approveMotion = motionReady && !!revision?.motion_edit && !revision.installed;
  const draftAction = approveMotion ? revision.actions?.[0] : undefined;
  const draft = approveMotion && motionDraft?.remote_task_id===revisionTask?.remote_task_id &&
    motionDraft?.result_sha256===revisionTask?.result_sha256 ? motionDraft : null;
  const gallery = portraitCreateReady && portraitGallery?.remote_task_id===revisionTask?.remote_task_id
    && portraitGallery?.result_sha256===revisionTask?.result_sha256 ? portraitGallery : null;
  const portraitLoadFailed = portraitCreateReady && !gallery && !revisionBusy && portraitLoadAttempt===revisionTask?.remote_task_id;
  const portraitLoading = portraitSubmitting || (portraitTask && taskRunning) ||
    (portraitCreateReady && !gallery && !portraitLoadFailed) ||
    (portraitEditReady && (revisionBusy || portraitAppendAttempt!==revisionTask?.remote_task_id));
  const savedPortrait = gallery && s.roles.find(r=>r.id===gallery.profile_id)?.portrait;
  const portrait = gallery?.items.find(item=>item.id===(savedPortrait?.id??portraitChoice));
  const demoBusy = s.job?.status === 'running';
  const motionSaveFailed = motionReady&&!approveMotion&&!revisionBusy&&motionSaveAttempt===revisionTask?.remote_task_id;
  const loadingMotionPreview = approveMotion&&!!draftAction&&!draft&&
    (revisionBusy||motionPreviewAttempt.current!==revisionTask?.remote_task_id);
  const savingMotion = motionReady&&(!approveMotion&&!motionSaveFailed||
    (revisionBusy&&motionSaveAttempt===revisionTask?.remote_task_id));
  const generationWaiting = !!generationSubmitting||taskRunning||demoBusy||portraitLoading||loadingMotionPreview||savingMotion||
    (!!revision?.pending&&!revisionTask&&revisionBusy);
  const busy = demoBusy || revisionBusy || !!revision?.pending;
  const isArt = portraitTask || portraitSubmitting || s.candidates.length>0;
  const previewCandidates = portraitCreateTask ? gallery?.items??[] : s.candidates;
  const candidateLabel = (index:number) => t('preview.candidate',{n:index<26?String.fromCharCode(65+index):index+1});
  const revising = !!candidate || plan?.mode==='revise';
  const actionName = (key:Action) => actions.includes(key)?t(`action.${key}`):key;
  const portraitSlotCount = portraitTask ? revision.candidate_count??s.count : s.count;
  const existing = s.existing && actionName(s.existing);
  const previewAction = s.existing ?? (role?.actions.includes('idle')?'idle':null);
  useEffect(()=>{
    stage.current?.style.removeProperty('--preview-scale');
  },[role?.id,candidate?.id,portrait?.id,previewAction,draft?.src]);
  useEffect(()=>{
    const target=stage.current;
    if (!target) return;
    const wheel=(event:WheelEvent)=>{
      if (!target.querySelector('.pet-art')||!event.deltaY||event.ctrlKey) return;
      event.preventDefault();
      const current=Number(target.style.getPropertyValue('--preview-scale')||1);
      const multiplier=event.deltaY<0?1.12:1/1.12;
      const next=Math.max(.55,Math.min(2.5,current*multiplier));
      target.style.setProperty('--preview-scale',String(next));
    };
    target.addEventListener('wheel',wheel,{passive:false});
    return ()=>target.removeEventListener('wheel',wheel);
  },[]);
  useEffect(()=>{
    function preventFileNavigation(event:DragEvent) {
      if (event.defaultPrevented||!event.dataTransfer?.types.includes('Files')) return;
      event.preventDefault();
      event.dataTransfer.dropEffect='none';
    }
    window.addEventListener('dragover',preventFileNavigation);
    window.addEventListener('drop',preventFileNavigation);
    return ()=>{
      window.removeEventListener('dragover',preventFileNavigation);
      window.removeEventListener('drop',preventFileNavigation);
    };
  },[]);
  const artDemo=generationLane(mode,'art')==='demo';
  const customMotion=mode==='service'&&!!role&&!isCatalogRole(role.id);
  const installedSource=role&&installedPets.get(catalogProfiles.get(role.id)??role.id);
  const motionLane=customMotion&&(!role.actions.length||installedSource)?'native':generationLane(mode,'motion',role?.id);
  const revisionTarget=motionLane==='native';
  const canEditPortrait=mode==='service'&&!!candidate&&!!revision&&!!checkpoint;
  const generationUnavailable=candidate?(mode==='service'?!checkpoint:!artDemo):motionLane==='unavailable';
  const waitingForMotionBridge=(candidate?mode==='service':revisionTarget) && !revision;
  const artBalance=mode==='service'?(revision?revision.available_art:credits?.available_art):s.balance.art;
  const motionBalance=mode==='service'?(revision?revision.available_motion:credits?.available_motion):s.balance.motion;
  const label = t(isArt ? 'preview.save' : 'preview.enable');
  const serviceEnableReady = nativePlayback && !!role && (isCatalogRole(role.id)||!!installedSource) && canEnable(s);
  const dismiss = () => {
    if ((dialog?.kind==='feedback'&&mailBusy)||
        ((dialog?.kind==='credits'||dialog?.kind==='deleteRole'||dialog?.kind==='discardMotion')&&revisionInFlight.current)) return;
    if (dialog?.kind==='credits') setInput('');
    setDialog(null);
  };
  const tell = (key:Message) => setNotice(key);
  const resetEdit = () => {setEditing(false);setInstruction('');};
  function withRevision(operation:(lastRead:RevisionView|null|undefined)=>Promise<void>, failure:Message='notice.taskError', refreshOnFailure=true) {
    if (revisionInFlight.current || closing.current) return;
    setRevisionBusy(true);
    // The native bridge serializes requests. Let explicit operations wait for
    // the current status read without putting the controls into that read's busy state.
    revisionInFlight.current = (revisionPoll.current??Promise.resolve(undefined)).then(operation).then(()=>{
      setNotice(current=>current==='notice.legacySession'?null:current);
    }).catch(async error=>{
      if (error==='revision_input_rejected') {
        setRevision(prev=>prev?{...prev,pending:false,task:null,task_kind:undefined}:prev);
        if (!closing.current) {setNotice(null);setDialog({kind:'inputRejected'});}
        return;
      }
      if (typeof error==='string' && error.startsWith('revision_session_')) {
        setRevision(undefined);setCredits(null);
        tell('notice.legacySession');
        return;
      }
      tell(error==='revision_credits_shortfall'?'notice.motionLow':error==='portrait_credits_shortfall'?'notice.artLow':error==='portrait_edit_unavailable'?'notice.portraitEditUnavailable':failure);
      if (closing.current||!refreshOnFailure) return;
      try {setRevision(await nativeRevision.inspect());}
      catch {setRevision(prev=>prev?{...prev,available_art:null,available_motion:null}:prev);}
    }).finally(()=>{revisionInFlight.current=null;setRevisionBusy(false);});
  }
  useEffect(()=>{
    if (mode==='service') void withRevision(async()=>{
      if (initialInstalled) applyPetLibrary(initialInstalled);
      else await refreshPetLibrary();
      setRevision(await nativeRevision.inspect());
    });
  },[mode]);
  useEffect(()=>{
    if (mode!=='service') return;
    let active=true;
    const openMailbox=()=>void invoke<boolean>('workshop_revision',{request:{op:'take_tray_mailbox'}})
      .then(pending=>{if(active&&pending) setDialog({kind:'feedback'});}).catch(()=>{});
    const subscription=listen('tray-mailbox',openMailbox);
    void subscription.then(()=>{if(active) openMailbox();}).catch(()=>{});
    return ()=>{active=false;void subscription.then(unlisten=>unlisten()).catch(()=>{});};
  },[mode]);
  function applyPetLibrary(versions:InstalledPet[]|null) {
    setNativePlayback(Array.isArray(versions));
    setInstalledPets(versions??[]);
    for (const role of stateRef.current.roles) {
      const installed=versions?.find(pet=>pet.role_id===(catalogProfiles.get(role.id)??role.id));
      if (installed?.actions) dispatch({kind:'custom-installed',id:role.id,actions:installed.actions});
    }
  }
  async function refreshPetLibrary(required=false) {
    try {applyPetLibrary(await nativeRevision.petLibrary());}
    catch (error) {
      if (required) throw error;
    }
  }
  function saveGeneratedMotion() {
    if (!motionReady||!revisionTask||revisionInFlight.current||closing.current) return;
    setMotionSaveAttempt(revisionTask.remote_task_id);
    void withRevision(async()=>{
      await nativeRevision.saveMotion(revisionTask);
      await refreshPetLibrary(true);
      setRevision(current=>current?.task?.remote_task_id===revisionTask.remote_task_id?
        {...current,pending:false,task:null}:current);
      setMotionDraft(null);
      tell('notice.installed');
    },'notice.installError',false);
  }
  useEffect(()=>{
    if (motionReady&&!approveMotion&&!revisionBusy&&motionSaveAttempt!==revisionTask?.remote_task_id) saveGeneratedMotion();
  },[motionReady,approveMotion,motionSaveAttempt,revisionTask?.remote_task_id,revisionBusy]);
  useEffect(()=>{
    if (!draftAction||!revisionTask||revisionBusy||revisionInFlight.current||
        motionPreviewAttempt.current===revisionTask.remote_task_id) return;
    motionPreviewAttempt.current=revisionTask.remote_task_id;
    void withRevision(async()=>{
      const result=await nativeRevision.previewMotion(draftAction);
      if (result.action!==draftAction||result.remote_task_id!==revisionTask.remote_task_id||
          result.result_sha256!==revisionTask.result_sha256||!result.src.startsWith('data:image/gif;base64,')) throw new Error('motion preview mismatch');
      setMotionDraft(result);
    },'notice.previewError',false);
  },[draftAction,revisionTask?.remote_task_id,revisionBusy]);
  useEffect(()=>{
    if (mode!=='service'||!taskRunning) return;
    let active=true;
    const timer=window.setInterval(()=>{
      if (closing.current||revisionInFlight.current||revisionPoll.current) return;
      revisionPoll.current=nativeRevision.inspect().then(view=>{
        if (active&&!closing.current) {
          setRevision(view);
          setCancelUnconfirmed(null);
          setNotice(current=>current==='notice.cancelUnconfirmed'||current==='notice.taskError'||current==='notice.legacySession'?null:current);
        }
        return view;
      }).catch(error=>{
        if (active&&!closing.current) {
          tell(typeof error==='string'&&error.startsWith('revision_session_')?'notice.legacySession':'notice.taskError');
        }
        return undefined;
      }).finally(()=>{revisionPoll.current=null;});
    },3000);
    return ()=>{active=false;window.clearInterval(timer);};
  },[mode,taskRunning,revisionTask?.remote_task_id]);
  function submitMotion() {
    if (!role || !plan || busy || motionLane==='unavailable'||revisionInFlight.current||closing.current) return;
    const steer=plan.mode==='revise' ? instruction.replace(/\s+/g,' ').trim() : '';
    if (revisionTarget) {
      if (revision?.available_motion==null||revision.available_motion<plan.actions.length) return;
      setGenerationSubmitting('motion');
      void withRevision(async()=>{
        try {
          await checkpoint?.flush();
          if (customMotion||installedSource) {
            const image=!customMotion?'':installedSource?await portraitPng(role.portrait.src,true).catch(()=>''):await portraitPng(role.portrait.src);
            setRevision(await nativeRevision.submitCustom(installedSource?.role_id??role.id,role.name,plan.actions,image,
              installedSource?{mode:plan.mode==='revise'?'modify':'add',instruction:steer,source_remote:installedSource.remote_task_id}:undefined));
          } else setRevision(await nativeRevision.submit(role.id,plan.mode==='revise'?'modify':'add',plan.actions,steer));
          resetEdit();
        } finally {
          setGenerationSubmitting(null);
        }
      });
    } else {
      dispatch({kind:'begin',job:{id:crypto.randomUUID(),type:'motion',target:role.id,plan,instruction:steer}});
      resetEdit();
    }
    dismiss();
  }
  const open = (d: Dialog) => {
    setInput(d.kind==='manageRole'?d.role.name:'');setDialog(d);
    if (d.kind==='credits'&&mode==='service') {
      setCredits(null);setCreditNotice(null);
      void withRevision(async()=>{
        try {
          const view=await nativeCredits.balance();
          setCredits(view);
          setRevision(previous=>previous?{...previous,available_art:view.available_art,available_motion:view.available_motion}:previous);
        }
        catch (error) {
          if (typeof error==='string'&&error.startsWith('revision_session_')) {
            setRevision(previous=>previous?{...previous,available_art:null,available_motion:null}:previous);
            setCreditNotice('credits.sessionChanged');
          } else setCreditNotice('credits.balanceUnavailable');
        }
      });
    }
    if (d.kind==='motion'&&revisionTarget&&revision) {
      void withRevision(async()=>setRevision(await nativeRevision.inspect()));
    }
  };
  function deleteRole(id:string) {
    if (busy) return;
    setRoleDeleteFailed(false);
    void withRevision(async()=>{
      dispatch({kind:'delete-role',id});
      try {await checkpoint?.flush();}
      catch {setRoleDeleteFailed(true);return;}
      setDialog(null);
      tell('roleManage.removed');
    },'roleManage.deleteError',false);
  }
  function redeemCredits() {
    if (mode!=='service') {tell('notice.noRedeem');return;}
    if (!credits||revisionInFlight.current||closing.current) return;
    void withRevision(async()=>{
      setCreditNotice(null);
      let view:CreditView;
      try {view=await nativeCredits.redeem(credits.owner,input);}
      catch (error) {
        if (typeof error==='string'&&error.startsWith('revision_session_')) {
          setCredits(null);
          setRevision(previous=>previous?{...previous,available_art:null,available_motion:null}:previous);
          setCreditNotice('credits.sessionChanged');
        } else setCreditNotice(error==='credit_invalid'?'credits.invalid':'credits.uncertain');
        return;
      }
      setCredits(view);setInput('');setCreditNotice('credits.redeemed');
      setRevision(previous=>previous?{...previous,available_art:view.available_art,available_motion:view.available_motion}:previous);
      if (!closing.current) {
        try {setRevision(await nativeRevision.inspect());}
        catch { /* The redemption receipt already supplied both updated balances. */ }
      }
    });
  }
  const refreshSlider = () => {
    const node = strip.current;
    if (!node) return;
    const max=Math.max(0,node.scrollWidth-node.clientWidth);
    const position=node.scrollLeft;
    setSlider(current=>current.max===max&&current.position===position?current:{position,max});
  };
  useEffect(() => {
    if (!watchClose || !checkpoint) return;
    const unlisten = watchClose(async (event) => {
      if (closing.current) {event.preventDefault();return;}
      closing.current = true;
      try {
        // Tauri awaits this handler before destroying the window. Keep the first close request alive.
        const active = revisionInFlight.current;
        if (active) {
          setNotice('notice.closing');
          await active;
        }
        await checkpoint.flush();
      }
      catch {
        event.preventDefault();
        closing.current = false;
        setNotice('notice.draftError');
      }
    });
    void unlisten.catch(()=>setStorageIssue(true));
    return () => {void unlisten.then(stop=>stop()).catch(()=>{});};
  },[checkpoint,watchClose]);
  useEffect(() => {
    if (!s.job || s.job.status!=='running') return;
    const {id} = s.job;
    const timer = window.setTimeout(() => {dispatch({kind:'complete',id}); tell('notice.complete');},1050);
    return () => window.clearTimeout(timer);
  },[s.job]);
  useEffect(() => {
    const panel = library.current;
    const node = strip.current;
    if (!panel || !node) return;
    const observer = new ResizeObserver(refreshSlider);
    observer.observe(node);
    const scrollLibrary = (event:WheelEvent) => {
      if (event.ctrlKey || Math.abs(event.deltaX)>=Math.abs(event.deltaY)) return;
      const unit = event.deltaMode===WheelEvent.DOM_DELTA_LINE ? 16 :
        event.deltaMode===WheelEvent.DOM_DELTA_PAGE ? node.clientWidth : 1;
      const limit = Math.max(0,node.scrollWidth-node.clientWidth);
      const next = Math.max(0,Math.min(limit,node.scrollLeft+event.deltaY*unit));
      if (next===node.scrollLeft) return;
      event.preventDefault();
      node.scrollLeft=next;
    };
    panel.addEventListener('wheel',scrollLibrary,{passive:false});
    return () => {
      observer.disconnect();
      panel.removeEventListener('wheel',scrollLibrary);
    };
  },[]);
  useEffect(refreshSlider,[s.roles.length]);
  useEffect(() => {
    const pending = Object.entries(downloads);
    if (remote || !pending.length || busy) return;
    const timer = window.setTimeout(() => {
      const next:Record<string,number> = {};
      for (const [id,value] of pending) {
        if (value>=80) {dispatch({kind:'download',id});tell('notice.demoDownload');}
        else next[id]=value+20;
      }
      setDownloads(next);
    },230);
    return () => window.clearTimeout(timer);
  },[downloads,busy,remote]);
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(()=>setNotice(null),3500);
    return () => window.clearTimeout(timer);
  },[notice]);
  function clearDownload(id:string) {
    setDownloads(prev=>Object.fromEntries(Object.entries(prev).filter(([key])=>key!==id)));
  }
  async function download(id:string) {
    dismiss();
    if (!remote) {
      dispatch({kind:'download',id});
      tell('notice.demoDownload');
      return;
    }
    setDownloads(prev=>({...prev,[id]:0}));
    try {
      const role=await remote.download(id,progress=>setDownloads(prev=>id in prev?{...prev,[id]:progress}:prev));
      dispatch({kind:'catalog-installed',role});
      tell('notice.downloaded');
    } catch (error) {
      tell(error==='catalog_cancelled'?'notice.downloadCancelled':error==='catalog_busy'?'notice.downloadBusy':
        error==='catalog_integrity'?'notice.downloadInvalid':'notice.downloadError');
    } finally {clearDownload(id);setCancelling(null);}
  }
  async function cancelDownload(id:string) {
    if (!remote) {clearDownload(id);tell('notice.downloadCancelled');return;}
    setCancelling(id);
    try {if (!await remote.cancel(id)) setCancelling(null);}
    catch {setCancelling(null);tell('notice.cancelError');}
  }
  async function pickFiles(event:ChangeEvent<HTMLInputElement>) {
    const files=Array.from(event.target.files ?? []); event.target.value='';
    await loadFiles(files);
  }
  async function loadFiles(files:File[]) {
    if (!files.length||loadingFiles||busy) return;
    if (files.length>4) {
      tell('notice.invalidFiles'); return;
    }
    setLoadingFiles(true);
    try {
      const images:Portrait[]=[];
      for (const file of files) {
        try {images.push(await readImage(file));}
        catch { /* Keep processing the other images in this drop. */ }
      }
      if (!images.length) {tell('notice.readError');return;}
      dispatch({kind:'files',items:images});
      if (images.length<files.length) tell('notice.partialImageRead');
    }
    finally {setLoadingFiles(false);}
  }
  function generateArt(append = false) {
    if (!artDemo) return;
    const cost=append?1:s.count;
    if (s.balance.art < cost) {tell('notice.artLow');return;}
    const id=crypto.randomUUID();
    const items=Array.from({length:cost},(_,i) => fixture(`${id}-${i}`, (s.candidates.length+i+(append?3:0))%9));
    dispatch({kind:'begin',job:{id,type:'art',cost,items,append,instruction:append?instruction.trim():undefined}});
    resetEdit();
  }
  function confirmInput() {
    if (s.job?.status==='failed') {dispatch({kind:'resume'});return;}
    if (s.tab==='image') {resetEdit();dispatch({kind:'local-candidates'});return;}
    if (mode==='service') {
      if (!checkpoint||!revision||revision.pending||!validText) return;
      confirmPortraitCost('create');
      return;
    }
    if (!artDemo) return;
    if (s.balance.art<s.count) {tell('notice.artLow');return;}
    setInput('');setArtConfirm('create');
  }
  const [artConfirm,setArtConfirm] = useState<'create'|'edit'|null>(null);
  const artCost=artConfirm==='edit'?1:s.count;
  function confirmPortraitCost(kind:'create'|'edit') {
    setArtCredits(null);setArtConfirm(kind);
    void withRevision(async()=>{try {setArtCredits(await nativeCredits.balance());} catch {setArtCredits(null);}});
  }
  const validText=s.text.kind.trim() || s.text.description.trim();
  const motionCost=plan?.actions.length ?? 0;
  const jobLabel=s.job?.type==='art' ? t(s.job.append?'job.refine':'job.art') : t(s.job?.plan.mode==='revise'?'job.revise':'job.motion');
  function submitPortraits() {
    if (!artCredits||artCredits.available_art<artCost||!checkpoint||revisionInFlight.current||closing.current) return;
    const edit=artConfirm==='edit';
    if (edit&&(!candidate||!instruction.trim())) return;
    setArtConfirm(null);
    setGenerationSubmitting(edit?'edit':'create');
    void withRevision(async()=>{
      try {
        await checkpoint.flush();
        setPortraitGallery(null);setLoadedPortrait('');
        const fallback=t('portrait.defaultName');
        setRevision(edit&&candidate
          ?await nativeRevision.editPortrait(artCredits.owner,(candidate.localName??s.text.name.trim())||fallback,await portraitPng(candidate.src),instruction.replace(/\s+/g,' ').trim())
          :await nativeRevision.submitPortraits(artCredits.owner,s.text,s.count,fallback));
        resetEdit();
      } finally {
        setGenerationSubmitting(null);
      }
    });
  }
  async function readPortraitGallery(task:RevisionRef) {
    const result=await nativeRevision.portraitGallery(task);
    if (result.remote_task_id!==task.remote_task_id||result.result_sha256!==task.result_sha256
      ||!/^pet-[0-9a-f]{32}$/.test(result.profile_id)||result.items.length<1||result.items.length>4
      ||result.items.some((item,i)=>item.id!==`${task.remote_task_id}-${i+1}`||!item.src.startsWith(pngPrefix))) throw new Error('portrait mismatch');
    return result;
  }
  function loadPortraits() {
    if (!portraitCreateReady||!revisionTask||revisionInFlight.current||closing.current) return;
    setPortraitLoadAttempt(revisionTask.remote_task_id);
    void withRevision(async()=>{
      const result=await readPortraitGallery(revisionTask);
      setPortraitChoice(result.items[0].id);setLoadedPortrait('');setPortraitGallery(result);
      setNotice(current=>current==='portrait.loadError'?null:current);
    },'portrait.loadError',false);
  }
  useEffect(()=>{
    if (portraitCreateReady&&!gallery&&!revisionBusy&&portraitLoadAttempt!==revisionTask?.remote_task_id) loadPortraits();
  },[portraitCreateReady,gallery,revisionBusy,portraitLoadAttempt,revisionTask?.remote_task_id]);
  function appendPortraitEdits() {
    if (!portraitEditReady||!revisionTask||!checkpoint||revisionInFlight.current||closing.current) return;
    setPortraitAppendAttempt(revisionTask.remote_task_id);
    void withRevision(async()=>{
      const result=portraitGallery?.remote_task_id===revisionTask.remote_task_id&&portraitGallery.result_sha256===revisionTask.result_sha256?
        portraitGallery:await readPortraitGallery(revisionTask);
      setPortraitGallery(result);
      dispatch({kind:'append-candidates',items:result.items.map(item=>({...item,localName:result.name}))});
      await checkpoint.flush();
      await nativeRevision.finishPortraits(result);
      setRevision(prev=>prev?{...prev,pending:false,task:null,task_kind:undefined,portrait_edit_task:false}:prev);
      setPortraitGallery(null);
      tell('notice.portraitAppended');
    },'portrait.saveError',false);
  }
  useEffect(()=>{
    if (portraitEditReady&&!revisionBusy&&portraitAppendAttempt!==revisionTask?.remote_task_id) appendPortraitEdits();
  },[portraitEditReady,revisionBusy,portraitAppendAttempt,revisionTask?.remote_task_id]);
  function savePortrait() {
    if (!gallery||!portrait||loadedPortrait!==portrait.id||!checkpoint) return;
    void withRevision(async()=>{
      if (savedPortrait && (savedPortrait.id!==portrait.id||savedPortrait.src!==portrait.src)) throw new Error('portrait conflict');
      dispatch({kind:'save-native-portrait',id:gallery.profile_id,name:gallery.name,portrait});
      await checkpoint.flush();
      await nativeRevision.finishPortraits(gallery);
      setRevision(prev=>prev?{...prev,pending:false,task:null,task_kind:undefined}:prev);
      setPortraitGallery(null);setLoadedPortrait('');tell('notice.artSaved');
    },'portrait.saveError',false);
  }
  return <main className="workshop">
    <header className="top">
      <div className="mark"><MoeGuardMark/></div><h1>{t('workshop.title')}</h1>
      <div className="live" role="status">{storageIssue?t('notice.noStorage')+' ':''}{notice&&t(notice)}</div>
    </header>
    <div className="overview">
      <section className="panel library" ref={library} aria-label={t('library.title')}>
        <div className="section-head"><div><h2>{t('library.title')}</h2><p>{t('library.intro')}</p></div>
          <small>{t('library.ready',{n:s.roles.length})}</small>
        </div>
        <div className="library-row">
          <button type="button" className={`tile create ${s.selected===null?'chosen':''}`} disabled={busy} onClick={()=>{resetEdit();dispatch({kind:'new'});}} aria-pressed={s.selected===null}><Glyph name="plus" size={26}/><span>{t('library.new')}</span></button>
          <div className="strip" ref={strip} onScroll={refreshSlider} tabIndex={0} aria-label={t('library.browse')}>
            {s.roles.map(r=><button type="button" className={`tile owned ${s.selected===r.id?'chosen':''}`} key={r.id} disabled={busy} aria-pressed={s.selected===r.id} aria-label={r.name}
              onClick={()=>{resetEdit();dispatch({kind:'select',id:r.id});}} onDoubleClick={()=>open({kind:'manageRole',role:r})}
              onKeyDown={e=>{if (e.key==='F2') {e.preventDefault();open({kind:'manageRole',role:r});}}}>
              <RoleCardArt portrait={r.portrait} poster={isCatalogRole(r.id)?undefined:installedPets.get(r.id)?.cover}/>
              {mode!=='service'&&s.active===r.id&&<span className="active-dot" title={t('library.active')}/>}<RoleCardName name={r.name}/>
            </button>)}
            {cloudRoles.filter(r=>!s.roles.some(o=>o.id===r.id)).map(r=>{
              const progress=downloads[r.id];
              return <button type="button" className="tile cloud" key={r.id} disabled={cancelling===r.id || (progress===undefined && (busy || (!!remote&&Object.keys(downloads).length>0)))}
                aria-label={progress===undefined?t('download.role',{name:r.name}):cancelling===r.id?t('download.cancelling',{name:r.name}):t('download.progress',{name:r.name,n:progress})}
                onClick={()=>{if (progress===undefined) open({kind:'download',role:r}); else void cancelDownload(r.id);}}>
                <RoleCardArt portrait={r.portrait} poster={catalogPosters[`../assets/posters/${r.id}/poster.webp`]}/>
                <span className="download-mark" style={progress===undefined?undefined:{background:`conic-gradient(var(--blue) ${progress}%, #d5e9fa 0)`}}><span><Glyph name={progress===undefined?'cloud':'close'}/></span></span><RoleCardName name={r.name}/>
              </button>;
            })}
          </div>
        </div>
        {slider.max>0&&<input aria-label={t('library.scroll')} className="library-range" type="range" min="0" max={slider.max} value={slider.position}
          onChange={e=>{if (strip.current) strip.current.scrollLeft=Number(e.target.value);}}/>}
      </section>
      <aside className="panel dashboard" aria-label={t('info.title')}>
        <div className="dash-card"><Glyph name="image"/><span>{t('info.art')}</span><strong>{artBalance==null?'—':number.format(artBalance)}</strong></div>
        <div className="dash-card"><Glyph name="video"/><span>{t('info.motion')}</span><strong>{motionBalance==null?'—':number.format(motionBalance)}</strong></div>
        <button className="dash-card" disabled={mode==='service'&&revisionBusy} onClick={()=>open({kind:'credits'})}><Glyph name="ticket"/><span>{t('info.credits')}</span><b>›</b></button>
        <button className="dash-card" onClick={()=>open({kind:'help'})}><Glyph name="book"/><span>{t('help.title')}</span><b>›</b></button>
      </aside>
    </div>
    <div className="desk">
      <section className="panel composition" aria-label={t('create.title')}>
        <div className="section-head"><div><h2>{t('create.title')}</h2><p>{t('create.intro')}</p></div></div>
        <div className="tabs" aria-label={t('create.method')}>
          <button aria-pressed={s.tab==='text'} disabled={busy||loadingFiles} onClick={()=>dispatch({kind:'tab',tab:'text'})}><Glyph name="spark"/>{t('create.text')}</button>
          <button aria-pressed={s.tab==='image'} disabled={busy||loadingFiles} onClick={()=>dispatch({kind:'tab',tab:'image'})}><Glyph name="image"/>{t('create.image')}</button>
        </div>
        <div className="tab-body" key={s.tab}>
          {s.tab==='text' ? <>
            <div className="form-grid">
              {fields.map(key=><label key={key}>{t(`field.${key}`)}<input placeholder={t(`hint.${key}`)} maxLength={key==='name'?24:60} value={s.text[key]} disabled={busy} onChange={e=>dispatch({kind:'text',key,value:e.target.value})}/></label>)}
              <label>{t('create.candidateCount')}<select disabled={busy} value={s.count} onChange={e=>dispatch({kind:'count',count:Number(e.target.value)})}>{[1,2,3,4].map(n=><option key={n} value={n}>{quantity('images',n)}</option>)}</select></label>
            </div>
            <div className="extra"><label>{t('field.description')}<input maxLength={480} placeholder={t('hint.description')} value={s.text.description} disabled={busy} onChange={e=>dispatch({kind:'text',key:'description',value:e.target.value})}/></label><button type="button" className="secondary roll" disabled={busy} title={t('create.randomHint')} aria-label={t('create.random')} onClick={()=>dispatch({kind:'roll',values:rollCharacter(s.text)})}><Glyph name="dice" size={20}/></button></div>
          </> : <>
            <div className="image-picker" data-dragging={fileDrag} onDragOver={event=>{
              if (!event.dataTransfer.types.includes('Files')) return;
              event.preventDefault();
              event.dataTransfer.dropEffect=busy||loadingFiles?'none':'copy';
              setFileDrag(!busy&&!loadingFiles);
            }} onDragLeave={event=>{
              if (!(event.relatedTarget instanceof Node&&event.currentTarget.contains(event.relatedTarget))) setFileDrag(false);
            }} onDrop={event=>{
              event.preventDefault();
              setFileDrag(false);
              void loadFiles(Array.from(event.dataTransfer.files));
            }}>
              <input type="file" ref={picker} hidden multiple accept="image/*,.heic,.heif,.tif,.tiff,.dng,.jxl" onChange={e=>void pickFiles(e)}/>
              <button type="button" aria-describedby="image-upload-tip image-upload-name" disabled={busy||loadingFiles} onClick={()=>picker.current?.click()}>
                {s.files.length? <span className="upload-thumbnails" aria-hidden="true">
                  {s.files.map(image=><span key={image.id} className="tile upload-thumbnail">
                    <RoleCardArt portrait={image}/>
                  </span>)}
                </span> : <Glyph name="plus" size={28}/>}
                <strong>{loadingFiles?t('files.reading'):s.files.length?t('files.selected',{n:s.files.length}):t('files.choose')}</strong>
                <small>{t('files.types')}</small><small id="image-upload-name">{t('files.nameHint')}</small>
              </button>
            </div>
            <p id="image-upload-tip" className="subtle">{t('files.tip')}</p>
          </>}
        </div>
        <div className="footer-action"><button type="button" className="primary" disabled={busy||loadingFiles||(!s.job&&(s.tab==='text'?(!validText||(!artDemo&&(!revision||!checkpoint))):!s.files.length))} onClick={confirmInput}>
          {demoBusy?t('job.demoBusy',{action:jobLabel}):t(s.job?.status==='failed'?'job.resume':'common.confirm')}</button>
          {s.job?.status==='failed'&&<button className="secondary" onClick={()=>dispatch({kind:'clear-failed'})}>{t('job.abandon')}</button>}
        </div>
      </section>
      <section className="panel preview" aria-label={t('preview.title')}>
        <div className="section-head"><div><h2>{t('preview.title')}</h2><p>{isArt?t('preview.choose'):role?.name??t('preview.intro')}</p></div>
          {role&&!isArt&&<span className="dev-tag">{t(canEnable(s)?'preview.ready':'preview.imageOnly')}</span>}
        </div>
        <div className="preview-body">
          <div className={isArt?'actions candidate-list':'actions'} aria-label={t(isArt?'preview.candidates':'preview.actions')} aria-busy={portraitLoading||undefined}>
            {portraitCreateTask&&!gallery ? Array.from({length:portraitSlotCount},(_,i)=>
              <button className="candidate-slot" key={i} type="button" disabled>{candidateLabel(i)}</button>
            ) : isArt ? previewCandidates.map((p,i)=><button key={p.id} type="button" aria-pressed={p.id===(portraitCreateTask?portrait?.id:s.candidate)} disabled={portraitCreateTask?!!savedPortrait||revisionBusy:busy} onClick={()=>{
                if (portraitCreateTask) {if (p.id!==portrait?.id) {setLoadedPortrait('');setPortraitChoice(p.id);}}
                else dispatch({kind:'candidate',id:p.id});
              }}>{candidateLabel(i)}</button>) : actions.map(key=>{
              const available=!!role?.actions.includes(key);
              const active=!!role && (available?s.existing===key:key==='idle'||s.missing.includes(key));
              return <button type="button" key={key} className={available?'available':'missing'} disabled={busy||!role} aria-pressed={active}
                onClick={()=>{
                  if (key==='idle'&&!available) tell('notice.requiredIdle');
                  else {
                    if (available) setPreviewRun(run=>run+1);
                    resetEdit();dispatch({kind:'action',action:key});
                  }
                }}>
                <i/><span>{t(`action.${key}`)}</span>
              </button>;
            })}
          </div>
          <div className={generationWaiting?'stage stage-generating':'stage'} ref={stage} aria-busy={generationWaiting||undefined}>
            <span className="stage-label">{draft?t('preview.motionDraft',{action:actionName(draft.action)}):isArt?t('preview.image'):previewAction?actionName(previewAction):t(role?'preview.image':'preview.waiting')}</span>
            {draft?<img className="pet-art" src={draft.src} alt={actionName(draft.action)}/>:
              portraitCreateTask?(portrait?<img key={portrait.id} className="pet-art" alt={t('preview.artAlt')} src={portrait.src}
              onLoad={()=>setLoadedPortrait(portrait.id)} onError={()=>{setLoadedPortrait('');tell('notice.previewError');}}/>:!generationWaiting&&
                <div className="empty"><Glyph name="image" size={38}/></div>):
              !isArt&&remote&&role&&previewAction&&(isCatalogRole(role.id)||installedSource)?
                <ActionPreview key={`${role.id}/${previewAction}/${installedSource?.result_sha256??''}/${previewRun}`}
                  role={role} action={previewAction} installed={installedSource??null} label={actionName(previewAction)}
                  loading={t('preview.actionLoading')} error={t('notice.previewError')}/>:
                candidate||role?<Art key={candidate?.id??role?.id} portrait={candidate??role!.portrait} alt={t('preview.artAlt')} motion={!!existing&&!isArt&&!remote}/>:
                  !generationWaiting&&<div className="empty"><Glyph name="image" size={38}/><strong>{t('preview.empty')}</strong><span>{t('preview.emptyHint')}</span></div>}
          </div>
        </div>
        <div className="footer-action preview-actions">
          {generationSubmitting ? <GenerationFooter label={t(portraitSubmitting?'portrait.submitting':'status.submittingMotion')} waiting/> : revision?.pending ? <>
            {portraitCreateReady ? gallery ?
              <button className="primary" disabled={revisionBusy||!checkpoint||!portrait||loadedPortrait!==portrait.id} onClick={savePortrait}>{t(savedPortrait?'status.finish':'preview.save')}</button>:
              <GenerationFooter label={t(portraitLoadFailed?'portrait.loadError':'portrait.loading')} waiting={!portraitLoadFailed}>
                {portraitLoadFailed&&<button className="secondary" onClick={loadPortraits}>{t('portrait.retryLoad')}</button>}
              </GenerationFooter>
            :portraitEditReady ? <GenerationFooter
              label={t(!revisionBusy&&portraitAppendAttempt===revisionTask?.remote_task_id?'portrait.saveError':'status.addingPortrait')}
              waiting={revisionBusy}>
              {!revisionBusy&&portraitAppendAttempt===revisionTask?.remote_task_id&&
                <button className="primary" onClick={appendPortraitEdits}>{t('status.retrySaveMotion')}</button>}
            </GenerationFooter>:approveMotion&&revisionTask ? <GenerationFooter
              label={t(loadingMotionPreview?'status.loadingMotionEdit':savingMotion?'status.savingMotion':'status.motionEditReady')}
              waiting={loadingMotionPreview||savingMotion}>
              <button className="secondary" disabled={revisionBusy||loadingMotionPreview} onClick={()=>open({kind:'discardMotion',task:revisionTask})}>{t('common.cancel')}</button>
              <button className="primary" disabled={revisionBusy||loadingMotionPreview} onClick={saveGeneratedMotion}>{t('status.installMotionEdit')}</button>
            </GenerationFooter>:motionReady ? <GenerationFooter
              label={t(motionSaveFailed?'notice.installError':'status.savingMotion')}
              waiting={savingMotion}>
              {motionSaveFailed&&
                <button className="primary" onClick={saveGeneratedMotion}>{t('status.retrySaveMotion')}</button>}
            </GenerationFooter> : <GenerationFooter
              label={revisionTask?t(`status.${revisionTask.status}`,{n:revisionTask.progress}):t('status.awaiting')}
              progress={taskRunning&&revisionTask?.status!=='cancel_requested'?revisionTask?.progress:undefined}
              waiting={taskRunning}>
              {revisionTask&&(revisionTask.status==='failed'||revisionTask.status==='cancelled')?
                <button className="secondary" disabled={revisionBusy} onClick={()=>void withRevision(async()=>{
                  await nativeRevision.dismiss(revisionTask.remote_task_id);
                  setRevision({...revision,pending:false,task:null});
                })}>{t('status.dismiss')}</button>:
                <button className="secondary" disabled={revisionBusy} onClick={()=>{
                  if (revisionTask&&revisionPoll.current) return;
                  void withRevision(async()=>{
                    setRevision(revisionTask?await nativeRevision.inspect():await nativeRevision.resume());
                    setCancelUnconfirmed(null);
                    setNotice(current=>current==='notice.cancelUnconfirmed'?null:current);
                    if (!revisionTask) resetEdit();
                  });
                }}>{t(revisionTask?'status.refresh':'status.resume')}</button>}
              {revisionTask&&canCancelTask&&
                <button className="secondary" disabled={revisionBusy||cancelUnconfirmed===revisionTask.remote_task_id}
                  onClick={()=>open({kind:'cancelTask',remote:revisionTask.remote_task_id})}>{t('status.cancelTask')}</button>}
            </GenerationFooter>}
          </> : editing ? <>
            <button className="secondary" disabled={busy} onClick={resetEdit}>{t('common.cancel')}</button>
            <textarea className="direction-input" aria-label={t('preview.direction')} autoFocus disabled={busy}
              value={instruction} rows={1} maxLength={60} placeholder={t('preview.directionHint')} onChange={e=>setInstruction(e.target.value)}/>
            <button className="primary" disabled={!instruction.trim()||busy||(isArt&&generationUnavailable)} onClick={()=>{
              if (canEditPortrait) confirmPortraitCost('edit');
              else if (isArt) generateArt(true);
              else open({kind:'motion'});
            }}>{t('common.ok')}</button>
          </> : <>
            <button className="secondary" disabled={busy||waitingForMotionBridge||generationUnavailable||!!s.job||!(candidate||plan)}
              onClick={()=>{if (revising) setEditing(true);else open({kind:'motion'});}}>
              <Glyph name={revising?'edit':'spark'}/>
              <span>{t(candidate&&generationUnavailable?'create.artUnavailable':plan&&generationUnavailable?'preview.unavailable':revising?'common.edit':'common.generate')}</span>
            </button>
            {mode==='service'&&revision===undefined&&<button className="secondary" disabled={revisionBusy} onClick={()=>void withRevision(async()=>setRevision(await nativeRevision.inspect()))}>{t('status.refreshTask')}</button>}
            <button className="primary finish" aria-disabled={busy||(!candidate&&(mode==='service'?!serviceEnableReady:!canEnable(s)))} disabled={busy||(!candidate&&mode==='service'&&!serviceEnableReady)} onClick={()=>{
              if (candidate) {resetEdit();dispatch({kind:'save',id:crypto.randomUUID()});tell('notice.artSaved');}
              else if (mode==='service') {
                if (role&&serviceEnableReady) void withRevision(async()=>{
                  if (installedSource) await nativeRevision.enableInstalledPet(installedSource);
                  else await nativeRevision.enableCatalogPet(role.id);
                },'petProbe.enableError',false);
              } else if (canEnable(s)) {dispatch({kind:'enable'});tell('notice.enabled');}
              else tell('notice.noAction');
            }}>{label}</button>
          </>}
        </div>
      </section>
    </div>
    {artConfirm&&<Modal title={t(artConfirm==='edit'?'confirm.editPortrait':'confirm.art')} close={()=>setArtConfirm(null)}>
      <p>{mode==='service'?(artCredits?t('confirm.artRemaining',{n:artCredits.available_art}):t(revisionBusy?'confirm.accountReading':'confirm.creditError')):t('confirm.artRemaining',{n:s.balance.art})}</p>
      <p>{t(mode==='service'?'confirm.accountCost':'confirm.demoCost',{n:artCost})}</p>
      <div className="dialog-actions"><button className="secondary" onClick={()=>setArtConfirm(null)}>{t('common.cancel')}</button>
        <button className="primary" disabled={mode==='service'&&(revisionBusy||(artCredits?.available_art??-1)<artCost)} onClick={()=>{
          if (mode==='service') submitPortraits();else {setArtConfirm(null);generateArt();}
        }}>{t('common.ok')}</button></div>
    </Modal>}
    {dialog?.kind==='download'&&<Modal title={t('download.role',{name:dialog.role.name})} close={dismiss}><p>{t('download.size',{n:packsMb.get(dialog.role.id)??'—'})}</p>{!remote&&<p className="subtle">{t('download.demo')}</p>}<div className="dialog-actions"><button className="secondary" onClick={dismiss}>{t('common.cancel')}</button><button className="primary" onClick={()=>void download(dialog.role.id)}>{t('common.ok')}</button></div></Modal>}
    {dialog?.kind==='manageRole'&&<Modal title={t('roleManage.title')} close={dismiss}>
      <form onSubmit={event=>{
        event.preventDefault();
        if (!input.trim()) return;
        dispatch({kind:'rename',id:dialog.role.id,name:input});
        dismiss();
      }}>
        <label>{t('rename.name')}<input autoFocus maxLength={24} value={input} onChange={e=>setInput(e.target.value)}/></label>
        <div className="dialog-actions role-manage-actions">
          <button type="button" className="secondary role-delete" onClick={()=>{
            setRoleDeleteFailed(false);
            setDialog({kind:'deleteRole',role:dialog.role});
          }}>{t('roleManage.delete')}</button>
          <button type="button" className="secondary" onClick={dismiss}>{t('common.cancel')}</button>
          <button className="primary" disabled={!input.trim()}>{t('common.save')}</button>
        </div>
      </form>
    </Modal>}
    {dialog?.kind==='deleteRole'&&<Modal title={t('roleManage.confirmTitle')} close={dismiss}>
      <p>{t('roleManage.confirmDetail',{name:dialog.role.name})}</p>
      {roleDeleteFailed&&<p className="role-delete-error" role="alert">{t('roleManage.deleteError')}</p>}
      <div className="dialog-actions">
        <button className="secondary" autoFocus disabled={revisionBusy}
          onClick={()=>setDialog({kind:'manageRole',role:dialog.role})}>{t('common.cancel')}</button>
        <button className="primary role-delete-confirm" disabled={busy}
          onClick={()=>deleteRole(dialog.role.id)}>{t('roleManage.confirm')}</button>
      </div>
    </Modal>}
    {dialog?.kind==='motion'&&plan&&role&&<Modal title={t(plan.mode==='revise'?'confirm.revise':'confirm.generate')} close={dismiss}>
      <p>{revisionTarget?(revisionBusy?t(revision?.legacy_session?'confirm.accountReading':'confirm.reading'):revision?.available_motion==null?t('confirm.creditError'):t('confirm.motionRemaining',{n:revision.available_motion})):t('confirm.demoMotionRemaining',{n:s.balance.motion})}</p>
      <p>{t(revisionTarget?(revision?.legacy_session?'confirm.accountCost':'confirm.testCost'):'confirm.demoCost',{n:motionCost})}</p>
      <div className="dialog-actions"><button className="secondary" onClick={dismiss}>{t('common.cancel')}</button><button className="primary"
        disabled={busy||(revisionTarget?(revision?.available_motion??-1)<motionCost:s.balance.motion<motionCost)} onClick={submitMotion}>{t('common.ok')}</button></div>
    </Modal>}
    {dialog?.kind==='inputRejected'&&<Modal title={t('notice.inputRejected')} close={dismiss}>
      <div className="dialog-actions"><button className="primary" autoFocus onClick={dismiss}>{t('common.ok')}</button></div>
    </Modal>}
    {dialog?.kind==='discardMotion'&&<Modal title={t('motionEdit.cancelTitle')} close={dismiss}>
      <div className="dialog-actions">
        <button className="secondary" autoFocus disabled={revisionBusy} onClick={dismiss}>{t('motionEdit.keep')}</button>
        <button className="primary role-delete-confirm" disabled={revisionBusy} onClick={()=>void withRevision(async()=>{
          await nativeRevision.discardMotionEdit(dialog.task);
          setRevision(prev=>prev?{...prev,pending:false,task:null}:prev);
          setMotionDraft(null);
          setDialog(null);
          tell('notice.motionDiscarded');
        },'notice.motionDiscardError',false)}>{t('motionEdit.discard')}</button>
      </div>
    </Modal>}
    {dialog?.kind==='cancelTask'&&canCancelTask&&revisionTask?.remote_task_id===dialog.remote&&<Modal title={t('status.cancelTask')} close={dismiss}>
      <p>{t('status.cancelDetail')}</p>
      <div className="dialog-actions">
        <button className="secondary" autoFocus onClick={dismiss}>{t('status.keepTask')}</button>
        <button className="primary" disabled={revisionBusy||revision?.task?.remote_task_id!==dialog.remote} onClick={()=>{
          const remote=dialog.remote;
          dismiss();
          void withRevision(async latest=>{
            if (latest!==undefined&&(latest?.task?.remote_task_id!==remote||!canCancel(latest.task))) return;
            setCancelUnconfirmed(remote);
            const task=await nativeRevision.cancel(remote);
            setRevision(prev=>prev?.task?.remote_task_id===remote?{...prev,task,available_motion:null}:prev);
            setCancelUnconfirmed(null);
          },'notice.cancelUnconfirmed',false);
        }}>{t('status.cancelTask')}</button>
      </div>
    </Modal>}
    {dialog?.kind==='credits'&&<Modal title={t('credits.title')} close={dismiss} className="credit-dialog">
      <dl className="credit-balance">
        <div><dt><Glyph name="image"/>{t('info.art')}</dt><dd>{artBalance==null?'—':number.format(artBalance)}</dd></div>
        <div><dt><Glyph name="video"/>{t('info.motion')}</dt><dd>{motionBalance==null?'—':number.format(motionBalance)}</dd></div>
      </dl>
      <form className="credit-form" onSubmit={event=>{event.preventDefault();redeemCredits();}}>
        <label htmlFor="credit-code">{t('credits.code')}</label>
        <div className="credit-code-row">
          <input id="credit-code" placeholder={sampleCode} autoFocus value={input} spellCheck={false}
            maxLength={128} disabled={revisionBusy} onChange={e=>setInput(e.target.value)}/>
          <button className="primary" disabled={revisionBusy||(mode==='service'&&!credits)||!/^MG[A-HJ-NP-Z2-9]{16}$/.test(input.toUpperCase().replace(/[\s-]+/g,''))}>{t('credits.redeem')}</button>
        </div>
      </form>
      {creditNotice&&<p className="credit-notice" role="status">{t(creditNotice)}</p>}
      <div className="dialog-actions credit-actions">
        <a className="secondary credit-get" href="https://ifdian.net/a/moeguard" target="_blank" rel="noopener noreferrer"
          onClick={event=>{
            if (!isNativeWorkshop()) return;
            event.preventDefault();
            void invoke('open_redemption_page').catch(()=>setCreditNotice('credits.openFailed'));
          }}><Glyph name="ticket"/>{t('credits.get')}<Glyph name="external" size={15}/></a>
        <button className="secondary" disabled={revisionBusy} onClick={dismiss}>{t('common.close')}</button>
      </div>
    </Modal>}
    {dialog?.kind==='help'&&<Modal title={t('help.title')} close={dismiss} className="workshop-help">
      <WorkshopHelp t={t} onFeedback={()=>open({kind:'feedback'})} onClose={dismiss}/>
    </Modal>}
    {dialog?.kind==='feedback'&&<Modal title={t('mail.title')} close={dismiss}>
      <Mailbox t={t} locale={locale} pending={mailPending} setPending={setMailPending} setBusy={setMailBusy}/>
      <div className="dialog-actions"><button className="secondary" disabled={mailBusy} onClick={dismiss}>{t('common.close')}</button></div>
    </Modal>}
  </main>;
}

const mount=createRoot(document.getElementById('root')!);
void (async()=>{
  const startLocale=await localePreference.load().catch(()=>defaultLocale);
  const {t}=createTranslator(startLocale);
  document.documentElement.lang=startLocale;
  document.title=t('windowTitle');
  if (isNativeWorkshop()) {
    const [native,inventory,mode] = await Promise.allSettled([openNativeWorkshop(),readNativeInventory(),nativeRevision.mode()]);
    if (native.status==='rejected') {
      mount.render(<main role="alert">{t('startup.draftError')}</main>);
    } else if (inventory.status==='rejected') {
      mount.render(<main role="alert">{t('startup.libraryError')}</main>);
    } else if (mode.status==='rejected') {
      mount.render(<main role="alert">{t('startup.modeError')}</main>);
    } else {
      const initialInstalled=mode.value==='service'?await nativeRevision.petLibrary().catch(()=>null):null;
      mount.render(<App base={discardDemoJob(reconcileNativeRoles(native.value.state,inventory.value),mode.value)}
        storage={native.value.storage} watchClose={native.value.watchClose} remote={nativeDownloads}
        startLocale={startLocale} mode={mode.value} initialInstalled={initialInstalled}/>);
    }
    return;
  }
  try {
    const db=await openDemo();
    mount.render(<App storage={{save:s=>persist(db,s)}} base={await restore(db)} startLocale={startLocale}/>);
  } catch {mount.render(<App storage={null} base={initial()} startLocale={startLocale}/>);}
})();
