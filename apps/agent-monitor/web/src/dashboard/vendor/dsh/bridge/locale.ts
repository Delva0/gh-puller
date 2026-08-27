/**
 * bridge locale 面:安装时注入 vendor 的 zh/en 字典('conversation' | 'trajectory'),
 * 查不到时回退 common 字典,再退"键名"本身;语言切换经宿主 useLanguage 驱动
 * setDshLocale()(每次切换升级 revision,renderer 按 (ns, revision) 重派生 t)。
 */
import { zh as zhCommon } from '../shims/common-locale/zh'
import { en as enCommon } from '../shims/common-locale/en'
import type { LocaleFace, Translate } from '@dsh/ui-slots'
import { createObservable } from './observable'

export type DshLang = 'zh' | 'en'

let currentLang: DshLang = 'zh'
const ticks = createObservable({ revision: 0 })

/** 宿主语言切换入口(识别 en,其余按缺省 zh)。 */
export function setDshLocale(lang: string | null | undefined): void {
  const next: DshLang = lang === 'en' ? 'en' : 'zh'
  if (next === currentLang) return
  currentLang = next
  ticks.set({ revision: ticks.getSnapshot().revision + 1 })
}

/** install.ts 注入的真实字典(按命名空间)。 */
const dicts = new Map<string, { zh: Record<string, string>; en: Record<string, string> }>()

export function registerDshDict(ns: string, zh: Record<string, string>, en: Record<string, string>): void {
  dicts.set(ns, { zh, en })
}

function resolve(ns: string, key: string, params?: Record<string, unknown>): string {
  const extra = dicts.get(ns) ?? { zh: {}, en: {} }
  const table = currentLang === 'en' ? extra.en : extra.zh
  let text = table[key]
  if (text === undefined) {
    const common: Record<string, string> = currentLang === 'en' ? enCommon : zhCommon
    text = common[key]
  }
  if (text === undefined) return key
  if (params == null) return text
  for (const [name, value] of Object.entries(params)) {
    text = text.split(`{${name}}`).join(String(value))
  }
  return text
}

const tCache = new Map<string, Translate>()

function bind(ns: string): Translate {
  let t = tCache.get(ns)
  if (t === undefined) {
    t = ((key: string, params?: Record<string, unknown>) => resolve(ns, key, params)) as Translate
    tCache.set(ns, t)
  }
  return t
}

/** locale 面:renderer 的 host.locale 契约(同步观察 + 命名空间绑定)。 */
export function createDshLocaleFace(): LocaleFace {
  return {
    getSnapshot: () => ticks.getSnapshot(),
    subscribe: (fn) => ticks.subscribe(fn),
    bind: (ns: string) => bind(ns),
  }
}
