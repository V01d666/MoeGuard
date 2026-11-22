import manifest from '../i18n/manifest.json' with {type:'json'};
import zh from '../i18n/zh-CN.json' with {type:'json'};
import en from '../i18n/en.json' with {type:'json'};

export type Locale = keyof typeof manifest.languages;
export type Message = keyof typeof zh;
export const languages = manifest.languages;
export const defaultLocale = manifest.defaultLocale as Locale;
export const dictionaries: Record<Locale, Record<Message, string>> = {'zh-CN':zh, en};

export function isLocale(value: unknown): value is Locale {
  return typeof value === 'string' && Object.hasOwn(languages,value);
}

export function resolveLocale(value: unknown): Locale {
  return isLocale(value) ? value : defaultLocale;
}

export function createTranslator(locale: Locale) {
  const number = new Intl.NumberFormat(locale);
  const plurals = new Intl.PluralRules(locale);
  const t = (key: Message, args: Record<string,string|number> = {}) =>
    dictionaries[locale][key].replace(/\{(\w+)\}/g, (match,key:string) => {
      const value = args[key];
      return typeof value === 'number' ? number.format(value) : value ?? match;
    });
  const quantity = (key:'images', n:number) => t(`${key}.${plurals.select(n)==='one'?'one':'other'}`, {n});
  return {t, number, quantity};
}
