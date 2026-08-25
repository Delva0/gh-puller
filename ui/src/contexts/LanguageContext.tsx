'use client';

import { createContext, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import en from './messages/en';
import zh from './messages/zh';

export type Lang = 'zh' | 'en';
// en 字典经 as const 导出字面量类型,统一放宽为字符串值字典;zh 合并自 en,键缺省即回退 en 文案
type Messages = Record<string, string>;

/** 内置字典:en 为底,zh 合并自 en(缺键回退 en);extraMessages 由消费方补充(如 webui 的大量文案) */
const baseDicts: Record<Lang, Messages> = { zh: { ...en, ...zh }, en };

interface Language {
  lang: Lang;
  setLang: (lang: Lang) => void;
  /** 取词:t(key, vars) 内插 {name}(en/zh 键必须一致,缺键回退 en,再退键名)。 */
  t: (key: string, vars?: Record<string, string | number>) => string;
}

const LanguageContext = createContext<Language | null>(null);

// localStorage 优先(ze),其次浏览器语言(zh 前缀 → zh,其余 en);SSR 下固定 en
const detect = (): Lang => {
  if (typeof window === 'undefined') return 'en';
  try {
    const saved = localStorage.getItem('lang');
    if (saved === 'zh' || saved === 'en') return saved;
  } catch {
    // 无痕模式等 localStorage 不可用时:按浏览器语言
  }
  return navigator.language?.toLowerCase().startsWith('zh') ? 'zh' : 'en';
};

export const LanguageProvider = ({ children, extraMessages }: {
  children: ReactNode;
  /** 消费方补充文案(点号键);并入对应语言字典,可覆盖同名键 */
  extraMessages?: { en?: Messages; zh?: Messages };
}) => {
  const dicts = useMemo<Record<Lang, Messages>>(() => ({
    en: { ...baseDicts.en, ...extraMessages?.en },
    zh: { ...baseDicts.zh, ...extraMessages?.zh },
  }), [extraMessages]);
  const [lang, setLangState] = useState<Lang>(detect);

  useEffect(() => {
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
