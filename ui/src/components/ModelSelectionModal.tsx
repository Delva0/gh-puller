'use client';

import React, {useEffect, useState} from 'react';
import {useLanguage} from '../contexts/LanguageContext';
import TargetSelector from './TargetSelector';
import WikiTypeSelector from './WikiTypeSelector';
import TokenInput from './TokenInput';
import type { TargetConfig } from './target';

interface ModelSelectionModalProps {
  isOpen: boolean;
  onClose: () => void;
  target: TargetConfig;
  setTarget: (value: TargetConfig) => void;
  onApply: (token?: string) => void;

  // Wiki type options
  isComprehensiveView: boolean;
  setIsComprehensiveView: (value: boolean) => void;

  // File filter options - optional
  excludedDirs?: string;
  setExcludedDirs?: (value: string) => void;
  excludedFiles?: string;
  setExcludedFiles?: (value: string) => void;
  includedDirs?: string;
  setIncludedDirs?: (value: string) => void;
  includedFiles?: string;
  setIncludedFiles?: (value: string) => void;
  showFileFilters?: boolean;
  showWikiType: boolean;

  // Token input for refresh
  showTokenInput?: boolean;
  repositoryType?: 'github' | 'gitlab' | 'bitbucket';
  // Authentication
  authRequired?: boolean;
  authCode?: string;
  setAuthCode?: (code: string) => void;
  isAuthLoading?: boolean;
}

export default function ModelSelectionModal({
  isOpen,
  onClose,
  target,
  setTarget,
  onApply,
  isComprehensiveView,
  setIsComprehensiveView,
  excludedDirs = '',
  setExcludedDirs,
  excludedFiles = '',
  setExcludedFiles,
  includedDirs = '',
  setIncludedDirs,
  includedFiles = '',
  setIncludedFiles,
  showFileFilters = false,
  authRequired = false,
  authCode = '',
  setAuthCode,
  isAuthLoading,
  showWikiType = true,
  showTokenInput = false,
  repositoryType = 'github',
}: ModelSelectionModalProps) {
  const { t } = useLanguage();

  // Local state for form values (to only apply changes when the user clicks "Submit")
  const [localTarget, setLocalTarget] = useState<TargetConfig>(target);
  const [localIsComprehensiveView, setIsComprehensiveViewLocal] = useState(isComprehensiveView);
  const [localExcludedDirs, setLocalExcludedDirs] = useState(excludedDirs);
  const [localExcludedFiles, setLocalExcludedFiles] = useState(excludedFiles);
  const [localIncludedDirs, setLocalIncludedDirs] = useState(includedDirs);
  const [localIncludedFiles, setLocalIncludedFiles] = useState(includedFiles);

  // Token input state
  const [localAccessToken, setLocalAccessToken] = useState('');
  const [localSelectedPlatform, setLocalSelectedPlatform] = useState<'github' | 'gitlab' | 'bitbucket'>(repositoryType);
  const [showTokenSection, setShowTokenSection] = useState(showTokenInput);

  // Reset local state when modal is opened
  useEffect(() => {
    if (isOpen) {
      setLocalTarget(target);
      setIsComprehensiveViewLocal(isComprehensiveView);
      setLocalExcludedDirs(excludedDirs);
      setLocalExcludedFiles(excludedFiles);
      setLocalIncludedDirs(includedDirs);
      setLocalIncludedFiles(includedFiles);
      setLocalSelectedPlatform(repositoryType);
      setLocalAccessToken('');
      setShowTokenSection(showTokenInput);
    }
  }, [isOpen, target, isComprehensiveView, excludedDirs, excludedFiles, includedDirs, includedFiles, repositoryType, showTokenInput]);

  // Handler for applying changes
  const handleApply = () => {
    setTarget(localTarget);
    setIsComprehensiveView(localIsComprehensiveView);
    if (setExcludedDirs) setExcludedDirs(localExcludedDirs);
    if (setExcludedFiles) setExcludedFiles(localExcludedFiles);
    if (setIncludedDirs) setIncludedDirs(localIncludedDirs);
    if (setIncludedFiles) setIncludedFiles(localIncludedFiles);

    // Pass token to onApply if needed
    if (showTokenInput) {
      onApply(localAccessToken);
    } else {
      onApply();
    }
    onClose();
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 overflow-y-auto">
      <div className="flex min-h-screen items-center justify-center p-4 text-center bg-black/50">
        <div className="relative transform overflow-hidden rounded-lg bg-[var(--card-bg)] text-left shadow-xl transition-all sm:my-8 sm:max-w-lg sm:w-full">
          {/* Modal header with close button */}
          <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border-color)]">
            <h3 className="text-lg font-medium text-[var(--accent-primary)]">
              <span className="text-[var(--accent-primary)]">{t("form.modelSelection")}</span>
            </h3>
            <button
              type="button"
              onClick={onClose}
              className="text-[var(--muted)] hover:text-[var(--foreground)] focus:outline-none transition-colors"
            >
              <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>

          {/* Modal body */}
          <div className="p-6">
            {/* Wiki Type Selector */}
            {
              showWikiType && <WikiTypeSelector
                    isComprehensiveView={localIsComprehensiveView}
                    setIsComprehensiveView={setIsComprehensiveViewLocal}
                />
            }

            {/* Divider */}
            <div className="my-4 border-t border-[var(--border-color)]/30"></div>

            {/* Target Selector */}
            <TargetSelector
              value={localTarget}
              onChange={setLocalTarget}
              showFileFilters={showFileFilters}
              excludedDirs={localExcludedDirs}
              setExcludedDirs={showFileFilters ? (value: string) => setLocalExcludedDirs(value) : undefined}
              excludedFiles={localExcludedFiles}
              setExcludedFiles={showFileFilters ? (value: string) => setLocalExcludedFiles(value) : undefined}
              includedDirs={localIncludedDirs}
              setIncludedDirs={showFileFilters ? (value: string) => setLocalIncludedDirs(value) : undefined}
              includedFiles={localIncludedFiles}
              setIncludedFiles={showFileFilters ? (value: string) => setLocalIncludedFiles(value) : undefined}
            />

            {/* Token Input Section for refresh */}
            {showTokenInput && (
              <>
                <div className="my-4 border-t border-[var(--border-color)]/30"></div>
                <TokenInput
                  selectedPlatform={localSelectedPlatform}
                  setSelectedPlatform={setLocalSelectedPlatform}
                  accessToken={localAccessToken}
                  setAccessToken={setLocalAccessToken}
                  showTokenSection={showTokenSection}
                  onToggleTokenSection={() => setShowTokenSection(!showTokenSection)}
                  allowPlatformChange={false} // Don't allow platform change during refresh
                />
              </>
            )}
            {/* Authorization Code Input */}
            {isAuthLoading && (
                <div className="mb-4 p-3 bg-[var(--background)]/50 rounded-md border border-[var(--border-color)] text-sm text-[var(--muted)]">
                  Loading authentication status...
                </div>
            )}
            {!isAuthLoading && authRequired && (
                <div className="mb-4 p-4 bg-[var(--background)]/50 rounded-md border border-[var(--border-color)]">
                  <label htmlFor="authCode" className="block text-sm font-medium text-[var(--foreground)] mb-2">
                    {t("form.authorizationCode")}
                  </label>
                  <input
                      type="password"
                      id="authCode"
                      value={authCode || ''}
                      onChange={(e) => setAuthCode?.(e.target.value)}
                      className="input-japanese block w-full px-3 py-2 text-sm rounded-md bg-transparent text-[var(--foreground)] focus:outline-none focus:border-[var(--accent-primary)]"
                      placeholder="Enter your authorization code"
                  />
                  <div className="flex items-center mt-2 text-xs text-[var(--muted)]">
                    <svg xmlns="http://www.w3.org/2000/svg" className="h-4 w-4 mr-1 text-[var(--muted)]"
                         fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                            d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                    </svg>
                    {t("form.authorizationRequired")}
                  </div>
                </div>
            )}
          </div>

          {/* Modal footer */}
          <div className="flex items-center justify-end gap-2 px-6 py-4 border-t border-[var(--border-color)]">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-sm font-medium rounded-md border border-[var(--border-color)]/50 text-[var(--muted)] bg-transparent hover:bg-[var(--background)] hover:text-[var(--foreground)] transition-colors"
            >
              {t("common.cancel")}
            </button>
            <button
              type="button"
              onClick={handleApply}
              className="px-4 py-2 text-sm font-medium rounded-md border border-transparent bg-[var(--accent-primary)]/90 text-white hover:bg-[var(--accent-primary)] transition-colors"
            >
              {t("common.submit")}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
