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
      {/* CSS controls icon visibility so class names remain stable across SSR and hydration.
          Browser extensions may inject SVG attributes before hydration, so every SVG element
          in the initial tree that carries a stroke must suppress hydration warnings. */}
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
