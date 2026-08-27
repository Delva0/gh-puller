import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { ThemeProvider } from 'next-themes';
import { LanguageProvider } from '@gh-puller/ui';
import App from './App';
import './styles.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <LanguageProvider>
      <ThemeProvider attribute="data-theme" defaultTheme="system" enableSystem>
        <App />
      </ThemeProvider>
    </LanguageProvider>
  </StrictMode>,
);
