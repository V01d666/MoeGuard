import {useEffect, useRef, useState, type KeyboardEvent, type MouseEvent, type ReactNode} from 'react';
import {createRoot} from 'react-dom/client';
import {invoke, isTauri} from '@tauri-apps/api/core';
import {getCurrentWindow} from '@tauri-apps/api/window';
import {listen} from '@tauri-apps/api/event';
import {defaultLocale, languages, type Locale, type Message} from './i18n.ts';
import {localePreference} from './localePreference.ts';
import {useUiLocale} from './useUiLocale.ts';
import {PatrolSettings} from './PatrolSettings.tsx';
import {Glyph} from './Art.tsx';
import {actions, type Action} from './workshop.ts';
import afdianIcon from '../../resources/icons/afdian.svg';
import githubIcon from '../../resources/icons/github.svg';
import './workshop.css';
import './settings.css';

type HotkeyView = [shortcut:string, active:boolean];
type ImportedRole = {key:string; name:string; version:number};
type ActivePet = {key:string; cover:string; actions:string[]; defaultFps:number; rates:Partial<Record<Action,number>>};
type PetView = {roles:{key:string; name:string}[]; active:ActivePet|null};
const roleTransfer = <T,>(op:string) => invoke<T>('settings_role_transfer',{request:{op}});
const petSettings = <T,>(request:object) => invoke<T>('settings_pet',{request});

const settingsTabs = [
  {id:'general',label:'settings.general',icon:'adjustments'},
  {id:'pet',label:'settings.petTab',icon:'paw'},
  {id:'patrol',label:'patrol.title',icon:'shield'},
  {id:'about',label:'settings.aboutTab',icon:'info'},
] as const;
type SettingsTab = typeof settingsTabs[number]['id'];

function SettingsPane({tab,active,children}:{tab:SettingsTab;active:SettingsTab;children:ReactNode}) {
  return <div id={`settings-panel-${tab}`} role="tabpanel" aria-labelledby={`settings-tab-${tab}`}
    className="settings-pane" hidden={tab!==active} tabIndex={0}>
    {children}
  </div>;
}

type UpdatePhase = 'idle'|'checking'|'current'|'available'|'downloading'|'closeWorkshop'|'failed'|'restarting';
type UpdateView = {version:string; autoCheck:boolean; phase:UpdatePhase; available:string|null};

function SettingsUpdate({t}:{t:(key:Message)=>string}) {
  const [view,setView] = useState<UpdateView|null>(null);
  const [requesting,setRequesting] = useState(false);
  const [requestFailed,setRequestFailed] = useState(false);
  useEffect(()=>{
    let mounted=true;
    const subscription=listen<UpdateView>('windows-update-changed',event=>{
      if (mounted) setView(event.payload);
    });
    void subscription.then(()=>invoke<UpdateView|null>('settings_update',{request:{op:'view'}}))
      .then(value=>{if (mounted) setView(value);}).catch(()=>{});
    return ()=>{
      mounted=false;
      void subscription.then(unlisten=>unlisten()).catch(()=>{});
    };
  },[]);

  if (!view) return null;
  const busy=requesting||view.phase==='checking'||view.phase==='downloading'||view.phase==='restarting';
  const message=requestFailed?t('update.failed'):view.phase==='idle'?'':t(`update.${view.phase}`)
    .replace('{version}',view.available??'');

  async function update(request:object) {
    setRequesting(true);setRequestFailed(false);
    try {setView(await invoke<UpdateView|null>('settings_update',{request}));}
    catch {setRequestFailed(true);}
    finally {setRequesting(false);}
  }

  return <section className="panel settings-about" aria-labelledby="update-title">
    <div className="settings-about-heading">
      <h2 id="update-title">{t('update.title')} · v{view.version}</h2>
      <div className="settings-inline-actions">
        <button type="button" className="secondary" disabled={busy}
          onClick={()=>void update({op:'check'})}>{t('update.check')}</button>
        {view.available&&<button type="button" className="primary" disabled={busy}
          onClick={()=>void update({op:'apply'})}>{t('update.install')}</button>}
      </div>
    </div>
    <label className="settings-update-toggle">
      <input type="checkbox" checked={view.autoCheck} disabled={busy}
        onChange={event=>void update({op:'set_auto',enabled:event.target.checked})}/>
      {t('update.autoCheck')}
    </label>
    {message&&<p role="status">{message}</p>}
  </section>;
}

function SettingsAbout({t}:{t:(key:Message)=>string}) {
  const [clientId,setClientId] = useState<string|null>(null);
  const [notice,setNotice] = useState<Message|null>(null);
  const idInput = useRef<HTMLInputElement>(null);
  useEffect(()=>{
    let mounted=true;
    void invoke<string|null>('settings_support_id').then(id=>{
      if (mounted) setClientId(id);
    }).catch(()=>{
      if (mounted) setNotice('settings.clientIdReadError');
    });
    return ()=>{mounted=false;};
  },[]);

  async function openLink(event:MouseEvent<HTMLAnchorElement>, command:'open_redemption_page'|'open_project_repository') {
    event.preventDefault();
    setNotice(null);
    try {await invoke(command);}
    catch {setNotice('settings.aboutLinkError');}
  }

  async function copyId(event:MouseEvent<HTMLButtonElement>) {
    if (!clientId) return;
    const button=event.currentTarget;
    try {
      try {await navigator.clipboard.writeText(clientId);}
      catch {
        idInput.current?.select();
        const copied=document.execCommand('copy');
        button.focus();
        if (!copied) throw new Error('Clipboard unavailable');
      }
      setNotice('settings.clientIdCopied');
    } catch {setNotice('settings.clientIdCopyError');}
  }

  return <section className="panel settings-about" aria-labelledby="about-title">
    <div className="settings-about-heading">
      <h2 id="about-title">{t('settings.about')}</h2>
      <div className="settings-inline-actions">
        <a className="secondary" href="https://ifdian.net/a/moeguard"
          onClick={event=>void openLink(event,'open_redemption_page')}>
          <img src={afdianIcon} alt=""/>{t('settings.afdian')}
        </a>
        <a className="secondary" href="https://github.com/V01d666/MoeGuard"
          onClick={event=>void openLink(event,'open_project_repository')}>
          <img src={githubIcon} alt=""/>GitHub
        </a>
      </div>
    </div>
    <label htmlFor="settings-client-id">{t('settings.clientId')}</label>
    <div className="settings-id-row">
      <input id="settings-client-id" ref={idInput} type="text" readOnly spellCheck={false}
        value={clientId??''} placeholder={t('settings.clientIdUnavailable')}/>
      <button type="button" className="secondary" disabled={!clientId} onClick={event=>void copyId(event)}>
        {t('settings.clientIdCopy')}
      </button>
    </div>
    {notice&&<p role="status">{t(notice)}</p>}
  </section>;
}

function Settings({initialLocale}:{initialLocale:Locale}) {
  const [tab,setTab] = useState<SettingsTab>('general');
  const [failed,setFailed] = useState(false);
  const [actionBusy,setActionBusy] = useState(false);
  const [actionError,setActionError] = useState<Message|null>(null);
  const [hotkey,setHotkey] = useState<HotkeyView|null>(null);
  const [recording,setRecording] = useState(false);
  const capture = useRef(false);
  const shortcutInput = useRef<HTMLInputElement>(null);
  const [keyBusy,setKeyBusy] = useState(false);
  const [keyError,setKeyError] = useState<Message|null>(null);
  const [transferAvailable,setTransferAvailable] = useState(false);
  const [petView,setPetView] = useState<PetView|null>(null);
  const [petError,setPetError] = useState(false);
  const [switching,setSwitching] = useState('');
  const [actionChoice,setActionChoice] = useState<Action>('idle');
  const [fpsDraft,setFpsDraft] = useState<number|null>(null);
  const savingFps = useRef(false);
  const [fpsBusy,setFpsBusy] = useState(false);
  const viewRequest = useRef(0);
  const [fpsError,setFpsError] = useState(false);
  const [roleBusy,setRoleBusy] = useState(false);
  const [roleNotice,setRoleNotice] = useState<Message|null>(null);
  const {locale,t,languageBusy,changeLocale} = useUiLocale(initialLocale,()=>setFailed(true));
  const native = isTauri();
  const tabs = native?settingsTabs:settingsTabs.slice(0,1);
  const active = petView?.active;
  const availableActions = actions.filter(action=>active?.actions.includes(action));
  const selectedAction = availableActions.includes(actionChoice)?actionChoice:availableActions[0];
  const currentFps = active&&(active.rates[selectedAction]??active.defaultFps);
  useEffect(()=>{
    document.documentElement.lang=locale;
    document.title=t('settings.windowTitle');
  },[locale,t]);
  useEffect(()=>{
    setActionChoice('idle');setFpsDraft(null);setFpsError(false);
  },[active?.key]);
  useEffect(()=>{
    if (!isTauri()) return;
    let mounted=true;
    void invoke<HotkeyView|null>('load_pet_hotkey').then(value=>{
      if (mounted&&value) setHotkey(value);
    }).catch(()=>{});
    void roleTransfer<ImportedRole[]>('list').then(roles=>{
      if (mounted) setTransferAvailable(Array.isArray(roles));
    }).catch(()=>{});
    void refreshPets();
    const playback=listen('pet-playback-changed',()=>void refreshPets());
    const shortcut=listen('pet-hotkey-current',stopCapture);
    return ()=>{
      mounted=false;
      viewRequest.current++;
      if (capture.current) void invoke('capture_pet_hotkey',{recording:false}).catch(()=>{});
      for (const subscription of [playback,shortcut]) void subscription.then(stop=>stop()).catch(()=>{});
    };
  },[]);

  async function refreshPets() {
    const id=++viewRequest.current;
    try {
      const view=await petSettings<PetView|null>({op:'view'});
      if (viewRequest.current===id) {setPetView(view);setPetError(false);}
    } catch {
      if (viewRequest.current===id) setPetError(true);
    }
  }

  async function switchPet(key:string) {
    if (!key||key===active?.key||roleBusy||fpsBusy||switching) return;
    setSwitching(key);setRoleNotice(null);setFpsError(false);
    try {
      const selected=await petSettings<ActivePet>({op:'switch',key});
      setPetView(view=>view?{...view,active:selected}:view);
      setActionChoice('idle');setFpsDraft(null);
    } catch {
      setRoleNotice('settings.petSwitchError');
    } finally {setSwitching('');}
  }

  async function saveFps(fps:number) {
    if (!active||!selectedAction||switching||savingFps.current) return;
    if (fps===currentFps) {setFpsDraft(null);return;}
    savingFps.current=true;setFpsBusy(true);setFpsError(false);
    try {
      const updated=await petSettings<ActivePet>({op:'fps',key:active.key,action:selectedAction,fps});
      viewRequest.current++;
      setPetView(view=>view?.active?.key===updated.key?{...view,active:updated}:view);
    } catch {setFpsError(true);}
    finally {savingFps.current=false;setFpsBusy(false);setFpsDraft(null);}
  }

  async function transfer(op:'import'|'export') {
    if (roleBusy) return;
    setRoleBusy(true);setRoleNotice(null);
    try {
      if (op==='import') {
        const role=await roleTransfer<ImportedRole|null>(op);
        if (role) {
          await refreshPets();
          setRoleNotice('settings.roleImported');
        }
      } else if (await roleTransfer<boolean|null>(op)) {
        setRoleNotice('settings.roleExported');
      }
    } catch (error) {
      setRoleNotice(error==='role_transfer_exists'?'settings.roleFileExists':
        error==='role_transfer_conflict'?'settings.roleConflict':'settings.roleTransferFailed');
    } finally {setRoleBusy(false);}
  }

  function stopCapture() {
    if (!capture.current) return;
    capture.current=false;setRecording(false);
    void invoke('capture_pet_hotkey',{recording:false}).catch(()=>{});
  }

  function startCapture() {
    if (keyBusy||capture.current) return;
    capture.current=true;setRecording(true);setKeyError(null);
    shortcutInput.current?.focus();
    void invoke('capture_pet_hotkey',{recording:true}).catch(()=>{});
  }

  async function saveShortcut(shortcut:string) {
    stopCapture();
    setKeyBusy(true);
    setKeyError(null);
    try {
      const value = await invoke<HotkeyView>('save_pet_hotkey',{shortcut});
      setHotkey(value);
    } catch(error) {
      setKeyError(error==='pet_hotkey_invalid'?'settings.shortcutInvalid':
        error==='pet_hotkey_conflict'?'settings.shortcutConflict':'settings.shortcutError');
    } finally {setKeyBusy(false);}
  }

  function recordShortcut(event:KeyboardEvent<HTMLInputElement>) {
    if (!capture.current||event.key==='Tab') return;
    event.preventDefault();event.stopPropagation();
    if (event.repeat||event.nativeEvent.isComposing) return;
    if (event.key==='Escape') {stopCapture();return;}
    if (['Control','Alt','Shift','Meta','AltGraph'].includes(event.key)) return;
    const modifiers=[event.ctrlKey&&'Ctrl',event.altKey&&'Alt',event.shiftKey&&'Shift',event.metaKey&&'Win'].filter(Boolean);
    if (!modifiers.length&&['Delete','Backspace'].includes(event.key)) {void saveShortcut('');return;}
    const key=/^(?:Key|Digit)([A-Z0-9])$/.exec(event.code)?.[1]??(/^F(?:[1-9]|10|11)$/.test(event.key)?event.key:'');
    if (!key||!(event.ctrlKey||event.altKey||event.metaKey)) {setKeyError('settings.shortcutInvalid');return;}
    void saveShortcut([...modifiers,key].join('+'));
  }

  async function openWorkshop() {
    setActionBusy(true);
    setActionError(null);
    try {
      await invoke('open_workshop');
      await getCurrentWindow().close();
    } catch {
      setActionError('settings.workshopError');
    } finally {
      setActionBusy(false);
    }
  }

  function navigateTabs(event:KeyboardEvent<HTMLDivElement>) {
    const current = tabs.findIndex(item=>item.id===tab);
    let next: number;
    switch (event.key) {
      case 'ArrowUp': next=(current+tabs.length-1)%tabs.length;break;
      case 'ArrowDown': next=(current+1)%tabs.length;break;
      case 'Home': next=0;break;
      case 'End': next=tabs.length-1;break;
      default: return;
    }
    event.preventDefault();
    setTab(tabs[next].id);
    event.currentTarget.querySelectorAll('button')[next]?.focus();
  }

  return <main className="settings">
    <header><h1 id="settings-title">{t('settings.title')}</h1></header>
    <div className="settings-workspace">
      <div className="settings-tabs" role="tablist" aria-orientation="vertical" aria-labelledby="settings-title"
        onKeyDown={navigateTabs}>
        {tabs.map(({id,label,icon})=><button key={id} id={`settings-tab-${id}`} type="button" role="tab"
          aria-selected={tab===id} aria-controls={`settings-panel-${id}`} tabIndex={tab===id?0:-1}
          onClick={()=>setTab(id)}>
          <Glyph name={icon} size={19}/>{t(label)}
        </button>)}
      </div>
      <SettingsPane tab="general" active={tab}>
        <section className="panel" aria-labelledby="settings-language-label">
          <div className="settings-row">
            <label id="settings-language-label" className="language-label" htmlFor="settings-language">
              <Glyph name="language" size={19}/>{t('locale.label')}
            </label>
            <select id="settings-language" value={locale} disabled={languageBusy} onChange={event=>{
              setFailed(false);changeLocale(event.target.value);
            }}>
              {Object.entries(languages).map(([id,name])=><option key={id} lang={id} value={id}>{name}</option>)}
            </select>
          </div>
          {failed&&<p role="alert">{t('locale.saveError')}</p>}
        </section>
        {hotkey&&<section className="panel" aria-labelledby="shortcut-title">
          <h2 id="shortcut-title">{t('settings.shortcutTitle')}</h2>
          <div className="settings-row shortcut-row">
            <label htmlFor="pet-shortcut">{t('settings.shortcutLabel')}</label>
            <input id="pet-shortcut" ref={shortcutInput} aria-describedby="shortcut-hint" readOnly
              value={recording?t('settings.shortcutCapture'):hotkey[0]||t('settings.shortcutOff')}
              onKeyDown={recordShortcut} onBlur={stopCapture}/>
            <button type="button" className="secondary" disabled={keyBusy||recording} onClick={startCapture}>{t('common.edit')}</button>
          </div>
          <p id="shortcut-hint">{t(recording?'settings.shortcutCaptureHint':'settings.shortcutHint')}<br/>{t('settings.shortcutUsage')}</p>
          {(keyError||(!recording&&hotkey[0]&&!hotkey[1]))&&<p role="alert">{t(keyError??'settings.shortcutConflict')}</p>}
        </section>}
      </SettingsPane>
      {native&&<>
        <SettingsPane tab="pet" active={tab}>
          <section className="panel settings-appearance" aria-labelledby="appearance-title">
            <h2 id="appearance-title">{t('settings.appearance')}</h2>
            {petView&&<div className="pet-appearance-row" aria-busy={!!switching||undefined}>
              <div className="settings-pet-preview">
                {active?.cover?<img src={active.cover} alt={t('settings.petPreview')} draggable={false}/>:
                  <Glyph name="image" size={28}/>}
              </div>
              <div className="pet-appearance-controls">
                <label htmlFor="settings-pet-select">{t('settings.currentPet')}</label>
                <select id="settings-pet-select" value={switching||active?.key||''}
                  disabled={!petView.roles.length||roleBusy||fpsBusy||!!switching}
                  onChange={event=>void switchPet(event.currentTarget.value)}>
                  <option value="" disabled>{t(petView.roles.length?'settings.choosePet':'settings.noReadyPets')}</option>
                  {active&&!petView.roles.some(role=>role.key===active.key)&&
                    <option value={active.key} disabled>{t('settings.currentPet')}</option>}
                  {petView.roles.map(role=><option value={role.key} key={role.key}>{role.name}</option>)}
                </select>
                {active&&selectedAction&&<div className="pet-fps">
                  <div className="pet-fps-heading">
                    <label htmlFor="settings-pet-fps">{t('settings.actionFps')}</label>
                    <output htmlFor="settings-pet-fps" aria-hidden="true">{t('settings.fpsValue',{n:fpsDraft??currentFps??1})}</output>
                  </div>
                  <div className="pet-fps-controls">
                    <select id="settings-pet-action" aria-label={t('settings.fpsAction')} value={selectedAction}
                      disabled={fpsBusy||!!switching} onChange={event=>{
                        setActionChoice(event.currentTarget.value as Action);setFpsDraft(null);setFpsError(false);
                      }}>
                      {availableActions.map(action=><option key={action} value={action}>{t(`action.${action}`)}</option>)}
                    </select>
                    <input id="settings-pet-fps" type="range" min="1" max="60" step="1" value={fpsDraft??currentFps??1}
                      aria-valuetext={t('settings.fpsValue',{n:fpsDraft??currentFps??1})}
                      disabled={fpsBusy||!!switching} onChange={event=>setFpsDraft(Number(event.currentTarget.value))}
                      onPointerUp={event=>void saveFps(Number(event.currentTarget.value))}
                      onKeyUp={event=>void saveFps(Number(event.currentTarget.value))}
                      onBlur={event=>void saveFps(Number(event.currentTarget.value))}/>
                  </div>
                </div>}
                {fpsError&&<p role="alert">{t('settings.fpsError')}</p>}
              </div>
            </div>}
            {petError&&<p role="alert">{t('settings.petLoadError')}</p>}
            <div className="settings-inline-actions">
              <button type="button" className="secondary" disabled={actionBusy||languageBusy||roleBusy||!!switching}
                onClick={()=>void openWorkshop()}>{t('settings.workshop')}</button>
              {transferAvailable&&<>
                <button type="button" className="secondary" disabled={roleBusy||!!switching}
                  onClick={()=>void transfer('import')}>{t('settings.roleImport')}</button>
                <button type="button" className="secondary" disabled={roleBusy||!!switching||(!!petView&&!active)}
                  onClick={()=>void transfer('export')}>{t('settings.roleExport')}</button>
              </>}
            </div>
            {roleNotice&&<p role="status">{t(roleNotice)}</p>}
            {actionError&&<p role="alert">{t(actionError)}</p>}
          </section>
        </SettingsPane>
        <SettingsPane tab="patrol" active={tab}>
          <PatrolSettings t={t} active={tab==='patrol'}/>
        </SettingsPane>
        <SettingsPane tab="about" active={tab}>
          <SettingsUpdate t={t}/>
          <SettingsAbout t={t}/>
        </SettingsPane>
      </>}
    </div>
    {native&&<footer><button type="button" className="secondary" disabled={languageBusy}
      onClick={()=>void getCurrentWindow().close().catch(()=>{})}>{t('common.close')}</button></footer>}
  </main>;
}

const mount=createRoot(document.getElementById('root')!);
void localePreference.load().catch(()=>defaultLocale).then(initialLocale=>mount.render(<Settings initialLocale={initialLocale}/>));
