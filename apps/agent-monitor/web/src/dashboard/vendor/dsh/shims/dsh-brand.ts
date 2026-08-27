/**
 * dsh-brand 最小 shim(vendor 面板类型层依赖;仅类型,零运行时)。
 */
declare const BRAND: unique symbol

/** 携编译期品牌 `B` 的字符串(单一品牌参数,dsh 原语)。 */
export type Branded<B extends string> = string & { readonly [BRAND]: B }
