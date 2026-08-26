'use client';

import { useTheme } from 'next-themes';

export default function ThemeToggle() {
  const { resolvedTheme, setTheme } = useTheme();

  return (
    <button
      type="button"
      className="theme-toggle-button cursor-pointer bg-transparent border border-[var(--border-color)] text-[var(--foreground)] hover:border-[var(--accent-primary)] active:bg-[var(--accent-secondary)]/10 rounded-md p-2 transition-all duration-300"
      title="Toggle theme"
      aria-label="Toggle theme"
      onClick={() => setTheme(resolvedTheme === 'dark' ? 'light' : 'dark')}
    >
      {/* 日/月图标可见性由 theme.css 的 [data-theme] 规则控制,类名必须保持静态(theme-agnostic):
          SSR 时 theme 为 undefined、客户端首渲染却是 localStorage 解析值,把 theme 写进 className 必然水合不一致。
          Dark Reader 等扩展会在水合前给初始渲染的 svg 注入 --darkreader-inline-stroke 等属性,
          所有新加入初始 SSR 树的 svg 元素(根与每个带 stroke 的子元素)都必须带 suppressHydrationWarning */}
      <div className="relative w-5 h-5">
        {/* Sun icon (light mode) */}
        <div className="theme-toggle-sun absolute inset-0 transition-opacity duration-300">
          <svg viewBox="0 0 24 24" fill="none" className="w-5 h-5" aria-label="Light Mode" suppressHydrationWarning>
            <circle cx="12" cy="12" r="5" stroke="currentColor" strokeWidth="2" suppressHydrationWarning />
            <path d="M12 2V4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" suppressHydrationWarning />
            <path d="M12 20V22" stroke="currentColor" strokeWidth="2" strokeLinecap="round" suppressHydrationWarning />
            <path d="M4 12L2 12" stroke="currentColor" strokeWidth="2" strokeLinecap="round" suppressHydrationWarning />
            <path d="M22 12L20 12" stroke="currentColor" strokeWidth="2" strokeLinecap="round" suppressHydrationWarning />
            <path d="M19.778 4.22183L17.6569 6.34315" stroke="currentColor" strokeWidth="2" strokeLinecap="round" suppressHydrationWarning />
            <path d="M6.34309 17.6569L4.22177 19.7782" stroke="currentColor" strokeWidth="2" strokeLinecap="round" suppressHydrationWarning />
            <path d="M19.778 19.7782L17.6569 17.6569" stroke="currentColor" strokeWidth="2" strokeLinecap="round" suppressHydrationWarning />
            <path d="M6.34309 6.34315L4.22177 4.22183" stroke="currentColor" strokeWidth="2" strokeLinecap="round" suppressHydrationWarning />
          </svg>
        </div>

        {/* Moon icon (dark mode) */}
        <div className="theme-toggle-moon absolute inset-0 transition-opacity duration-300">
          <svg viewBox="0 0 24 24" fill="none" className="w-5 h-5" aria-label="Dark Mode" suppressHydrationWarning>
            <path
              d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              fill="none"
              suppressHydrationWarning
            />
          </svg>
        </div>
      </div>
    </button>
  );
}
