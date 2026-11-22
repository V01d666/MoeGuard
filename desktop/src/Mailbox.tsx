import {useEffect, useRef, useState} from 'react';
import type {Locale, Message} from './i18n.ts';
import {isNativeWorkshop, markNativeMailRead, readNativeAnnouncements, readNativeMail, sendNativeMail,
  type Announcement, type MailReadMarker, type MailThread, type PendingMail} from './nativeStorage.ts';

const sessionErrors = new Set(['revision_session_missing','revision_session_expired',
  'revision_session_invalid','revision_session_rejected','revision_session_unavailable',
  'revision_session_disabled','revision_session_mismatch']);
type View<T> = {notice:Message} | {data:T};
type Translator = (key:Message,args?:Record<string,number>)=>string;
type Composer = {pending:PendingMail|null; setPending:(value:PendingMail|null)=>void;
  setBusy:(value:boolean)=>void};

function useRead<T>(read:()=>Promise<T|null>, unavailable:Message, enabled=true) {
  const [view,setView] = useState<View<T>>(()=>({notice:isNativeWorkshop()?'mail.loading':'mail.offline'}));
  useEffect(()=>{
    if (!isNativeWorkshop()||!enabled) return;
    let mounted=true;
    void read().then(data=>{
      if (mounted) setView(data===null?{notice:'mail.offline'}:{data});
    }).catch(error=>{
      if (mounted) setView({notice:sessionErrors.has(error)?'mail.session':unavailable});
    });
    return ()=>{mounted=false;};
  },[read,unavailable,enabled]);
  return [view,setView] as const;
}

function stamp(seconds:number, date:Intl.DateTimeFormat) {
  const when = new Date(seconds*1000);
  return Number.isFinite(when.getTime())?<time dateTime={when.toISOString()}>{date.format(when)}</time>:null;
}

type ReadActions = {seen:ReadonlySet<string>; mark:(key:string)=>void};

function unreadRow({unread,read_key}:MailReadMarker, seen:ReadonlySet<string>) {
  return !!(unread&&read_key&&!seen.has(read_key));
}

function ReadMark({t,read_key,unread,seen,mark}:MailReadMarker & ReadActions & {t:Translator}) {
  const [state,setState]=useState<'idle'|'saving'|'failed'>('idle');
  if (!read_key||!unreadRow({read_key,unread},seen)) return null;
  return <span className="mail-mark">
    <button type="button" className="link" disabled={state==='saving'} onClick={async()=>{
      setState('saving');
      try {await markNativeMailRead(read_key);mark(read_key);}
      catch {setState('failed');}
    }}>{t('mail.markRead')}</button>
    {state==='failed'&&<span role="status">{t('mail.markFailed')}</span>}
  </span>;
}

function Thread({t,date,pending,setPending,setBusy,model,seen,mark}:Composer & ReadActions & {
  t:Translator;date:Intl.DateTimeFormat;model:ReturnType<typeof useRead<MailThread>>}) {
  const [view,setView]=model;
  const [draft,setDraft]=useState('');
  const [preview,setPreview]=useState(false);
  const [attachDiagnostics,setAttachDiagnostics]=useState(false);
  const [sending,setSending]=useState(false);
  const sendingNow=useRef(false);
  const [status,setStatus]=useState<Message|null>(null);
  if ('notice' in view) return <p role="status">{t(view.notice)}</p>;
  const {owner,limits,messages}=view.data;
  const matchesOwner=!pending||pending.owner===owner;
  const body=draft.replace(/\r\n?/g,'\n').trim();
  const size=[...body].length;
  const valid=size>0&&size<=limits.max_body_chars&&!/[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]/u.test(body);
  async function send() {
    if (sendingNow.current||!matchesOwner||(!pending&&(!preview||!valid||!limits.remaining_today))) return;
    const mail=pending??{owner,body,attach_diagnostics:attachDiagnostics,client_message_id:'cm_'+crypto.randomUUID().replaceAll('-','')};
    sendingNow.current=true;
    setBusy(true);
    setSending(true);
    setPending(mail);
    setStatus(null);
    try {
      const {remaining_today,message_id,created_at}=await sendNativeMail(mail);
      setView({data:{owner,limits:{...limits,remaining_today},messages:messages.some(msg=>msg.message_id===message_id)
        ?messages:[...messages,{message_id,created_at,body:mail.body,direction:'user' as const}].slice(-50)}});
      setPending(null);
      setDraft('');
      setPreview(false);
      setAttachDiagnostics(false);
      setStatus('mail.sent');
    } catch (error) {
      if (error==='mailbox_invalid_message') {
        setPending(null);
        setDraft(mail.body);
        setPreview(false);
        setStatus('mail.invalid');
      } else if (typeof error==='string'&&sessionErrors.has(error)) setStatus('mail.sendSession');
      else setStatus('mail.sendUncertain');
    } finally {
      sendingNow.current=false;
      setSending(false);
      setBusy(false);
    }
  }
  return <>
    {messages.length===0?<p>{t('mail.empty')}</p>:
      <ol className="mail-thread" tabIndex={0} aria-label={t('mail.thread')}>
        {messages.map(message=><li key={message.message_id}>
            <div className="mail-meta"><strong>{t(message.direction==='operator'?'mail.operator':'mail.user')}</strong>
              {stamp(message.created_at,date)}{message.direction==='operator'&&
                <ReadMark key={message.read_key} t={t} {...message} seen={seen} mark={mark}/>}</div>
            <p className="mail-body">{message.body}</p>
          </li>)}
      </ol>}
    <form className="mail-composer" onSubmit={event=>{event.preventDefault();
      if (pending||preview) void send(); else if (valid&&limits.remaining_today) setPreview(true);
    }}>
      <p className="subtle">{t('mail.remaining',{n:limits.remaining_today})}</p>
      {!matchesOwner?<p>{t('mail.otherAccount')}</p>:pending||preview?<>
        <p>{t(pending?'mail.pending':'mail.preview')}</p>
        <p className="mail-body mail-preview" tabIndex={0}>{pending?.body??body}</p>
        {(pending?pending.attach_diagnostics:attachDiagnostics)&&<p className="subtle">{t('mail.diagnosticsDetails')}</p>}
      </>:<>
        <label htmlFor="mail-draft">{t('mail.compose')}</label>
        <textarea id="mail-draft" rows={3} value={draft} onChange={event=>{
          setDraft(event.target.value);setStatus(null);
        }} aria-describedby="mail-length"/>
        <p id="mail-length" className={size>0&&!valid?'mail-invalid':'subtle'}>
          {size>0&&!valid&&size<=limits.max_body_chars?t('mail.invalid'):t('mail.length',{n:size,max:limits.max_body_chars})}
        </p>
        <label className="mail-diagnostics"><input type="checkbox" checked={attachDiagnostics} onChange={event=>setAttachDiagnostics(event.target.checked)}/>{t('mail.attachDiagnostics')}</label>
      </>}
      <div className="dialog-actions">
        {(pending||preview)&&<button type="button" className="secondary" disabled={sending} onClick={()=>{
          setPending(null);if (pending) setDraft('');setPreview(false);setStatus(null);
        }}>{t(pending?'mail.stopRetry':'mail.edit')}</button>}
        {matchesOwner&&<button type="submit" className="primary"
          disabled={sending||(!pending&&(!valid||!limits.remaining_today))}>
          {t(sending?'mail.sending':pending?'mail.retry':preview?'mail.send':'mail.review')}
        </button>}
      </div>
      {status&&<p role="status">{t(status)}</p>}
    </form>
  </>;
}

function Announcements({t,date,view,seen,mark}:ReadActions & {
  t:Translator;date:Intl.DateTimeFormat;view:View<Announcement[]>}) {
  if ('notice' in view) return <p role="status">{t(view.notice)}</p>;
  if (!view.data.length) return <p>{t('mail.announcementsEmpty')}</p>;
  return <ol className="mail-thread" tabIndex={0} aria-label={t('mail.announcements')}>
    {view.data.map(entry=><li key={entry.announcement_id}>
      <div className="mail-meta"><strong>{entry.title}</strong>{stamp(entry.published_at,date)}
        <ReadMark key={entry.read_key} t={t} {...entry} seen={seen} mark={mark}/></div>
      <p className="mail-body">{entry.body}</p>
    </li>)}
  </ol>;
}

export function Mailbox({t,locale,...composer}:Composer & {t:Translator;locale:Locale}) {
  const [showAnnouncements,setShowAnnouncements]=useState(false);
  const [loadedAnnouncements,setLoadedAnnouncements]=useState(false);
  const thread=useRead(readNativeMail,'mail.unavailable');
  const [announcements]=useRead(readNativeAnnouncements,'mail.announcementsError',loadedAnnouncements);
  const [seen,setSeen]=useState<ReadonlySet<string>>(()=>new Set());
  const mark=(key:string)=>setSeen(prev=>new Set(prev).add(key));
  const messages='data' in thread[0]?thread[0].data.messages:[];
  const threadCount=messages.filter(row=>row.direction==='operator'&&unreadRow(row,seen)).length;
  const announcementCount='data' in announcements?announcements.data.filter(row=>unreadRow(row,seen)).length:0;
  const label=(key:Message,count:number)=>count?`${t(key)} · ${t('mail.unreadCount',{n:count})}`:undefined;
  const badge=(count:number)=>count>0&&<span className="mail-unread" aria-hidden="true">{count}</span>;
  const date=new Intl.DateTimeFormat(locale,{dateStyle:'medium',timeStyle:'short'});
  return <>
    {isNativeWorkshop()&&<nav className="tabs mail-tabs" aria-label={t('mail.title')}>
      <button type="button" aria-pressed={!showAnnouncements} aria-label={label('mail.thread',threadCount)}
        onClick={()=>setShowAnnouncements(false)}>{t('mail.thread')}{badge(threadCount)}</button>
      <button type="button" aria-pressed={showAnnouncements} aria-label={label('mail.announcements',announcementCount)} onClick={()=>{
        setLoadedAnnouncements(true);setShowAnnouncements(true);
      }}>{t('mail.announcements')}{badge(announcementCount)}</button>
    </nav>}
    <section hidden={showAnnouncements}><Thread t={t} date={date} {...composer} model={thread} seen={seen} mark={mark}/></section>
    {loadedAnnouncements&&<section hidden={!showAnnouncements}>
      <Announcements t={t} date={date} view={announcements} seen={seen} mark={mark}/>
    </section>}
  </>;
}
