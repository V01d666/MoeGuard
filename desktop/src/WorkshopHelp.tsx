import {useState} from 'react';
import {Glyph} from './Art.tsx';
import {createTranslator} from './i18n.ts';

const topics=['library','manage','text','upload','portrait','motion','preview','edit','enable','tasks','credits','mail'] as const;

export function WorkshopHelp({t,onFeedback,onClose}:{
  t:ReturnType<typeof createTranslator>['t']; onFeedback:()=>void; onClose:()=>void;
}) {
  const [query,setQuery]=useState('');
  const term=query.trim().toLocaleLowerCase();
  const entries=topics.map(topic=>({
    topic, question:t(`help.${topic}.q`), answer:t(`help.${topic}.a`),
  })).filter(({question,answer})=>`${question} ${answer}`.toLocaleLowerCase().includes(term));
  return <>
    <div className="help-feedback">
      <Glyph name="mail"/>
      <p>{t('help.feedbackStart')}<button type="button" className="link" onClick={onFeedback}>{t('help.feedbackLink')}</button>{t('help.feedbackEnd')}</p>
    </div>
    <input type="search" className="help-search" aria-label={t('help.search')}
      placeholder={t('help.search')} value={query} onChange={event=>setQuery(event.target.value)}/>
    <div className="help-scroll">
      {entries.map(({topic,question,answer})=><details key={topic}>
        <summary>{question}</summary><p>{answer}</p>
      </details>)}
      {!entries.length&&<p className="subtle" role="status">{t('help.noResults')}</p>}
    </div>
    <div className="dialog-actions"><button className="secondary" onClick={onClose}>{t('common.close')}</button></div>
  </>;
}
