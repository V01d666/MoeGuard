import {useEffect, useMemo, useRef, useState} from 'react';
import {createTranslator, isLocale, type Locale} from './i18n.ts';
import {localePreference} from './localePreference.ts';

export function useUiLocale(initialLocale:Locale, onError:()=>void) {
  const [locale,setLocale] = useState(initialLocale);
  const [languageBusy,setLanguageBusy] = useState(false);
  const savingLocale = useRef<Promise<void>|null>(null);
  const events = useRef(0);
  const translator = useMemo(()=>createTranslator(locale),[locale]);
  useEffect(()=>{
    const stop=localePreference.watch(value=>{events.current++;setLocale(value);});
    return ()=>{void stop.then(unwatch=>unwatch()).catch(()=>{});};
  },[]);

  function changeLocale(value:string) {
    if (!isLocale(value) || savingLocale.current || value===locale) return;
    setLanguageBusy(true);
    const version=events.current;
    savingLocale.current=localePreference.save(value).then(()=>{
      if (version===events.current) setLocale(value);
    }).catch(onError).finally(()=>{savingLocale.current=null;setLanguageBusy(false);});
  }

  return {locale,languageBusy,savingLocale,changeLocale,...translator};
}
