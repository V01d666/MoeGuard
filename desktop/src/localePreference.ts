import {invoke, isTauri} from '@tauri-apps/api/core';
import {listen} from '@tauri-apps/api/event';
import {isLocale, resolveLocale, type Locale} from './i18n.ts';

const previewKey = 'moeguard.ui-locale';
export const localePreference = {
  async load():Promise<Locale> {
    return resolveLocale(isTauri()?await invoke('load_ui_locale'):localStorage.getItem(previewKey));
  },
  async save(locale:Locale):Promise<void> {
    if (isTauri()) await invoke('save_ui_locale',{locale});
    else localStorage.setItem(previewKey,locale);
  },
  async watch(update:(locale:Locale)=>void):Promise<()=>void> {
    if (isTauri()) {
      let notified=false;
      const stop=await listen<unknown>('ui-locale-changed',event=>{
        if (isLocale(event.payload)) {notified=true;update(event.payload);}
      });
      // A native change can land between the first read and subscribing.
      try {const current=await this.load(); if (!notified) update(current);}
      catch { /* retain the initial locale until the next event */ }
      return stop;
    }
    const changed=(event:StorageEvent)=>{if (event.key===previewKey) update(resolveLocale(event.newValue));};
    window.addEventListener('storage',changed);
    try {update(await this.load());} catch { /* preview storage may be disabled */ }
    return ()=>window.removeEventListener('storage',changed);
  },
};
