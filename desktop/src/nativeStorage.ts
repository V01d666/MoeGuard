import {Channel, invoke, isTauri} from '@tauri-apps/api/core';
import {getCurrentWindow} from '@tauri-apps/api/window';
import {actions, fixture, initial, isCatalogRole, type Action, type Portrait, type Role, type State} from './workshop.ts';
import type {WorkshopStorage} from './workshopStorage.ts';

export const isNativeWorkshop = isTauri;
type Record = {schema: number; revision: number; state: State};
type Close = {preventDefault(): void};
export type WatchClose = (listener: (event:Close) => Promise<void>) => Promise<() => void>;
export type NativeDownloads = {
  download(id:string,onProgress:(percent:number)=>void):Promise<Role>;
  cancel(id:string):Promise<boolean>;
};

type NativeRole = Omit<Role,'actions'> & {actions:string[]};
function workshopRole(role: NativeRole): Role {
  return {...role,actions:actions.filter(key=>role.actions.includes(key))};
}
export async function readNativeInventory() {
  return (await invoke<NativeRole[]>('catalog_inventory')).map(workshopRole);
}
export const nativeDownloads: NativeDownloads = {
  async download(id, onProgress) {
    return workshopRole(await invoke<NativeRole>('catalog_download',{id,progress:new Channel<number>(onProgress)}));
  },
  cancel:id=>invoke<boolean>('catalog_cancel',{id}),
};

export type RevisionRef = {remote_task_id:string; result_sha256:string};
export const readActionPreview = (id:string, action:Action, installed:RevisionRef|null) =>
  invoke<string>('workshop_action_preview',{id,action,installed});
export type InstalledPet = RevisionRef & {role_id:string; package_version:number; name:string; actions:Action[]; cover?:string};
export type RevisionTask = RevisionRef & {status:'queued'|'running'|'cancel_requested'|'cancelled'|'failed'|'succeeded'; progress:number};
export type RevisionView = {pending:boolean; task:RevisionTask|null; task_kind?:'portraits'; portrait_edit_task?:boolean; candidate_count?:number;
  motion_edit?:boolean; actions?:Action[]; installed?:boolean;
  available_art?:number|null; available_motion:number|null; legacy_session?:boolean};
export type PortraitGallery = RevisionRef & {items:Portrait[]; name:string; profile_id:string};
export type MotionDraft = RevisionRef & {action:Action; src:string};
export type MailReadMarker = {read_key?:string; unread?:boolean};
export type MailMessage = MailReadMarker & {message_id:string; direction:'user'|'operator'; body:string; created_at:number};
export type MailThread = {owner:string; messages:MailMessage[];
  limits:{max_body_chars:number; daily_limit:number; remaining_today:number}};
export type PendingMail = {owner:string; client_message_id:string; body:string; attach_diagnostics?:boolean};
type MailReceipt = Omit<MailMessage,'direction'|'body'> & {remaining_today:number};
export type Announcement = MailReadMarker & {announcement_id:string; title:string; body:string; published_at:number};
const revision = <T>(request:object) => invoke<T>('workshop_revision',{request});
export type CreditView = {owner:string; available_art:number; available_motion:number};
export const nativeCredits = {
  balance:()=>revision<CreditView>({op:'credit_balance'}),
  redeem:(owner:string,code:string)=>revision<CreditView>({op:'credit_redeem',owner,code}),
};
export const readNativeMail = ()=>revision<MailThread|null>({op:'mailbox'});
export const sendNativeMail = (mail:PendingMail)=>revision<MailReceipt>({op:'mailbox_send',...mail});
export const readNativeAnnouncements = ()=>revision<Announcement[]|null>({op:'announcements'});
export const markNativeMailRead = (key:string)=>revision<null>({op:'mail_mark',key});
export const nativeRevision = {
  mode:async()=>{
    const mode=await revision<unknown>({op:'mode'});
    if (mode!=='service'&&mode!=='preview') throw new Error('invalid workshop mode');
    return mode;
  },
  inspect:()=>revision<RevisionView|null>({op:'inspect'}),
  submit:(id:string,mode:'add'|'modify',actions:Action[],instruction:string)=>
    revision<RevisionView>({op:'submit',id,mode,actions,instruction}),
  submitCustom:(id:string,name:string,actions:Action[],png:string,source?:{mode:'add'|'modify'; instruction:string; source_remote:string})=>
    revision<RevisionView>({op:'submit_initial',id,name,actions,png,...source}),
  submitPortraits:(owner:string,text:State['text'],count:number,fallback_name:string)=>
    revision<RevisionView>({op:'submit_portraits',owner,text,count,fallback_name}),
  editPortrait:(owner:string,name:string,png:string,instruction:string)=>
    revision<RevisionView>({op:'edit_portrait',owner,name,png,instruction}),
  portraitGallery:(ref:RevisionRef)=>revision<PortraitGallery>({op:'portrait_gallery',remote:ref.remote_task_id,digest:ref.result_sha256}),
  finishPortraits:(ref:RevisionRef)=>revision<null>({op:'finish_portraits',remote:ref.remote_task_id,digest:ref.result_sha256}),
  resume:()=>revision<RevisionView>({op:'resume'}),
  dismiss:(remote:string)=>revision<null>({op:'dismiss',remote}),
  cancel:(remote:string)=>revision<RevisionTask>({op:'cancel',remote}),
  saveMotion:(ref:RevisionRef)=>revision<null>({op:'save_motion',remote:ref.remote_task_id,digest:ref.result_sha256}),
  previewMotion:(action:Action)=>revision<MotionDraft>({op:'preview',action}),
  discardMotionEdit:(ref:RevisionRef)=>revision<null>({op:'discard_motion_edit',remote:ref.remote_task_id,digest:ref.result_sha256}),
  petLibrary:()=>revision<InstalledPet[]|null>({op:'pet_library'}),
  enableInstalledPet:(ref:RevisionRef)=>revision<null>({op:'enable_installed_pet',remote:ref.remote_task_id,digest:ref.result_sha256}),
  enableCatalogPet:(id:string)=>revision<null>({op:'enable_catalog_pet',id}),
};

export async function openNativeWorkshop(): Promise<{state:State; storage:WorkshopStorage; watchClose: WatchClose}> {
  const record = await invoke<Record|null>('load_workshop');
  if (record && (record.schema!==1 || !Number.isSafeInteger(record.revision) ||
      !record.state?.text || !record.state.balance || !Array.isArray(record.state.roles))) {
    throw new Error('invalid native checkpoint');
  }
  let expected=record?.revision ?? 0;
  return {
    state:record?.state ?? initial(),
    storage:{async save(state) {
      // The installed package owns these bytes; retain only the catalog role's local name/state.
      const compact={...state,roles:state.roles.map(role=>isCatalogRole(role.id)?{...role,portrait:fixture(role.id,0)}:role)};
      expected=await invoke<number>('save_workshop',{expected,state:compact});
    }},
    watchClose:listener=>getCurrentWindow().onCloseRequested(listener),
  };
}
