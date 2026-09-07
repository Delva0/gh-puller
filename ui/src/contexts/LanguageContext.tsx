'use client';

import { createContext, useContext, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import en from './messages/en';
import zh from './messages/zh';

export type Lang = 'zh' | 'en';
// Widen the const English catalog so Chinese can merge over it with English fallback values.
type Messages = Record<string, string>;

/** Built-in catalogs with English fallback values for missing Chinese entries. */
const baseDicts: Record<Lang, Messages> = { zh: { ...en, ...zh }, en };

interface Language {
  lang: Lang;
  setLang: (lang: Lang) => void;
  /** Resolves a key with `{name}` interpolation, then falls back to English and the key itself. */
  t: (key: string, vars?: Record<string, string | number>) => string;
}

const LanguageContext = createContext<Language | null>(null);

// Prefer localStorage, then browser language; SSR always uses English.
const detect = (): Lang => {
  if (typeof window === 'undefined') return 'en';
  try {
    const saved = localStorage.getItem('lang');
    if (saved === 'zh' || saved === 'en') return saved;
  } catch {
    // Private browsing may disable localStorage, so fall back to browser language.
  }
  return navigator.language?.toLowerCase().startsWith('zh') ? 'zh' : 'en';
};

export const LanguageProvider = ({ children, extraMessages }: {
  children: ReactNode;
  /** Consumer messages merged into each catalog, overriding matching dotted keys. */
  extraMessages?: { en?: Messages; zh?: Messages };
}) => {
  const dicts = useMemo<Record<Lang, Messages>>(() => ({
    en: { ...baseDicts.en, ...extraMessages?.en },
    zh: { ...baseDicts.zh, ...extraMessages?.zh },
  }), [extraMessages]);
  // Start in English so SSR matches the first hydrated frame; detection requires browser state.
  const [lang, setLangState] = useState<Lang>('en');
  const isFirstRender = useRef(true);

  // Apply the detected client language after hydration.
  useEffect(() => {
    setLangState(detect());
  }, []);

  useEffect(() => {
    // Skip the mount frame to avoid overwriting a saved language before detection completes.
    if (isFirstRender.current) {
      isFirstRender.current = false;
      return;
    }
    localStorage.setItem('lang', lang);
    document.documentElement.lang = lang;
  }, [lang]);

  const setLang = (l: Lang) => setLangState(l);
  const t: Language['t'] = (key, vars) => {
    let text: string = dicts[lang][key] ?? dicts.en[key] ?? key;
    if (vars) {
      for (const [k, v] of Object.entries(vars)) {
        text = text.replace(`{${k}}`, String(v));
      }
    }
    return text;
  };

  return (
    <LanguageContext.Provider value={{ lang, setLang, t }}>{children}</LanguageContext.Provider>
  );
};

export const useLanguage = (): Language => {
  const ctx = useContext(LanguageContext);
  if (!ctx) throw new Error('useLanguage 须在 LanguageProvider 内使用');
  return ctx;
};
