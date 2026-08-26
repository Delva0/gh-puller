'use client';

import React, { useState, useEffect } from 'react';
import { useLanguage } from '../contexts/LanguageContext';
import {
  DEFAULT_LOAD_GENERATORS_CONFIG,
  strippedTarget,
  type GeneratorsConfig,
  type GeneratorConfigItem,
  type ProviderConfigItem,
  type TargetConfig,
} from './target';

interface TargetSelectorProps {
  /** 注册表配置加载器(可注入;缺省 GET /api/generators/config) */
  loadConfig?: () => Promise<GeneratorsConfig>;
  value: TargetConfig;
  onChange: (value: TargetConfig) => void;

  // File filter configuration(原 UserSelector 继承的过滤面,结构不变)
  showFileFilters?: boolean;
  excludedDirs?: string;
  setExcludedDirs?: (value: string) => void;
  excludedFiles?: string;
  setExcludedFiles?: (value: string) => void;
  includedDirs?: string;
  setIncludedDirs?: (value: string) => void;
  includedFiles?: string;
  setIncludedFiles?: (value: string) => void;
}

/**
 * Generator → 生成器配置的 target selector(按注册表 configKind 渲染):
 * - file 类(cc/dsh/codex):config_path 输入(各 CLI 原生配置文件;模型/凭证/端点随文件);
 * - object 类(llm):Provider → Model → API Key / Base URL。
 * 消费方只管 TargetConfig(公开部分 + object 类请求态凭证);切换 generator 后按
 * 注册表补齐对应 kind 的缺省并清空他类字段;凭证输入不落 URL/localStorage
 * (会话层由调用方存);file 类不携带凭证字段(后端 422 语义)。
 */
export default function TargetSelector({
  loadConfig = DEFAULT_LOAD_GENERATORS_CONFIG,
  value,
  onChange,
  showFileFilters = false,
  excludedDirs = '',
  setExcludedDirs,
  excludedFiles = '',
  setExcludedFiles,
  includedDirs = '',
  setIncludedDirs,
  includedFiles = '',
  setIncludedFiles,
}: TargetSelectorProps) {
  const [isFilterSectionOpen, setIsFilterSectionOpen] = useState(false);
  const [filterMode, setFilterMode] = useState<'exclude' | 'include'>('exclude');
  const { t } = useLanguage();

  const [config, setConfig] = useState<GeneratorsConfig | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showDefaultDirs, setShowDefaultDirs] = useState(false);
  const [showDefaultFiles, setShowDefaultFiles] = useState(false);

  const update = (patch: Partial<TargetConfig>) => onChange({ ...value, ...patch });

  // 侧边态:provider 已选但当前 generator 不支持 → 重置为该 generator 默认
  const selectedGenerator = config?.generators.find((g) => g.id === value.generator);
  const selectedProvider = config?.providers.find((p) => p.id === value.provider);

  useEffect(() => {
    const fetchConfig = async () => {
      try {
        setIsLoading(true);
        setError(null);
        const data = await loadConfig();
        setConfig(data);
        if (!value.generator) {
          const dt = data.defaultTarget;
          const gc = dt.generator_config || {};
          const kind = data.generators.find((g) => g.id === dt.generator)?.configKind;
          onChange(kind === 'file'
            ? { generator: dt.generator, config_path: gc.config_path || '' }
            : {
                generator: dt.generator,
                provider: gc.provider || '',
                model: gc.model || '',
              });
        }
      } catch (err) {
        console.error('Failed to fetch generators config:', err);
        setError('Failed to load generator configurations. Using defaults.');
      } finally {
        setIsLoading(false);
      }
    };
    fetchConfig();
    // loadConfig 未入 deps:缺省为模块级常量(稳定引用);消费方注入时须同样传稳定引用
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value.generator, onChange]);

  const handleGeneratorChange = (newGenerator: string) => {
    const gen = config?.generators.find((g: GeneratorConfigItem) => g.id === newGenerator);
    if (gen?.configKind === 'file') {
      // file 类:全新对象(仅 config_path;不携 object 类字段/凭证 —— 后端 422 语义)
      onChange({ generator: newGenerator, config_path: gen.configDefault || '' });
      return;
    }
    const prov = config?.providers.find((p: ProviderConfigItem) => p.id === gen?.defaultProvider);
    onChange({
      generator: newGenerator,
      provider: gen?.defaultProvider || '',
      model: prov?.models[0] || '',
      api_key: value.api_key,
      base_url: value.base_url,
    });
  };

  const handleProviderChange = (newProvider: string) => {
    const prov = config?.providers.find((p: ProviderConfigItem) => p.id === newProvider);
    onChange({
      ...value,
      provider: newProvider,
      model: prov?.models[0] || '',
    });
  };

  // Default excluded directories from config.py
  const defaultExcludedDirs =
`./.venv/
./venv/
./env/
./virtualenv/
./node_modules/
./bower_components/
./jspm_packages/
./.git/
./.svn/
./.hg/
./.bzr/
./__pycache__/
./.pytest_cache/
./.mypy_cache/
./.ruff_cache/
./.coverage/
./dist/
./build/
./out/
./target/
./bin/
./obj/
./docs/
./_docs/
./site-docs/
./_site/
./.idea/
./.vscode/
./.vs/
./.eclipse/
./.settings/
./logs/
./log/
./tmp/
./temp/
./.eng`;

  // Default excluded files from config.py
  const defaultExcludedFiles =
`package-lock.json
yarn.lock
pnpm-lock.yaml
npm-shrinkwrap.json
poetry.lock
Pipfile.lock
requirements.txt.lock
Cargo.lock
composer.lock
.lock
.DS_Store
Thumbs.db
desktop.ini
*.lnk
.env
.env.*
*.env
*.cfg
*.ini
.flaskenv
.gitignore
.gitattributes
.gitmodules
.github
.gitlab-ci.yml
.prettierrc
.eslintrc
.eslintignore
.stylelintrc
.editorconfig
.jshintrc
.pylintrc
.flake8
mypy.ini
pyproject.toml
tsconfig.json
webpack.config.js
babel.config.js
rollup.config.js
vitest.config.js
karma.conf.js
jest.config.js
tsconfig.spec.json
*.min.js
*.min.css
*.bundle.js
*.bundle.css
*.map
*.gz
*.zip
*.tar
*.tgz
*.rar
*.pyc
*.pyo
*.pyd
*.so
*.dll
*.class
*.exe
*.o
*.a
*.jpg
*.jpeg
*.png
*.gif
*.ico
*.svg
*.webp
*.mp3
*.wav
*.avi
*.mov
*.webm
*.csv
*.tsv
*.xls
*.xlsx
*.db
*.sqlite
*.sqlite3
*.pdf
*.docx
*.pptx`;

  if (isLoading) {
    return (
      <div className="flex flex-col gap-2">
        <div className="text-sm text-[var(--muted)]">Loading generator configurations...</div>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="space-y-4">
        {error && (
          <div className="text-sm text-red-500 mb-2">{error}</div>
        )}

        {/* Generator Selection */}
        <div>
          <label htmlFor="generator-dropdown" className="block text-xs font-medium text-[var(--foreground)] mb-1.5">
            {t("form.generator")}
          </label>
          <select
            id="generator-dropdown"
            value={value.generator}
            onChange={(e) => handleGeneratorChange(e.target.value)}
            className="input-japanese block w-full px-2.5 py-1.5 text-sm rounded-md bg-transparent text-[var(--foreground)] focus:outline-none focus:border-[var(--accent-primary)]"
          >
            <option value="" disabled>{t("form.selectGenerator")}</option>
            {config?.generators.map((g) => (
              <option key={g.id} value={g.id}>{g.name}</option>
            ))}
          </select>
          {selectedGenerator && (
            <p className="text-[10px] text-[var(--muted)] mt-1">
              {t("form.generatorCapability")}: {selectedGenerator.capability}
            </p>
          )}
        </div>

        {/* file 类:生成器配置 = 各 CLI 原生配置文件路径(模型/凭证/端点随文件,服务端纯透传) */}
        {selectedGenerator?.configKind === 'file' && (
          <div>
            <label htmlFor="config-path-input" className="block text-xs font-medium text-[var(--foreground)] mb-1.5">
              {t("form.configFilePath")}
            </label>
            <input
              id="config-path-input"
              type="text"
              value={value.config_path || ''}
              onChange={(e) => update({ config_path: e.target.value })}
              placeholder={selectedGenerator.configDefault || selectedGenerator.configPathEnv || t("form.configFilePathPlaceholder")}
              className="input-japanese block w-full px-2.5 py-1.5 text-sm rounded-md bg-transparent text-[var(--foreground)] focus:outline-none focus:border-[var(--accent-primary)]"
            />
            <p className="text-[10px] text-[var(--muted)] mt-1">
              {t("form.configPathNote")}
            </p>
            {selectedGenerator.configDefault && (
              <p className="text-[10px] text-[var(--muted)] mt-1">
                {t("form.configPathDefault")}: {selectedGenerator.configDefault}
              </p>
            )}
          </div>
        )}

        {selectedGenerator?.configKind === 'object' && (
          <>
            {/* Provider Selection */}
            <div>
              <label htmlFor="provider-dropdown" className="block text-xs font-medium text-[var(--foreground)] mb-1.5">
                {t("form.modelProvider")}
              </label>
              <select
                id="provider-dropdown"
                value={value.provider}
                onChange={(e) => handleProviderChange(e.target.value)}
                disabled={!selectedGenerator}
                className="input-japanese block w-full px-2.5 py-1.5 text-sm rounded-md bg-transparent text-[var(--foreground)] focus:outline-none focus:border-[var(--accent-primary)] disabled:opacity-50"
              >
                <option value="" disabled>{t("form.selectProvider")}</option>
                {(selectedGenerator?.providers || []).map((pid) => {
                  const p = config?.providers.find((x) => x.id === pid);
                  return <option key={pid} value={pid}>{p?.name || pid}</option>;
                })}
              </select>
            </div>

            {/* Model Selection */}
            <div>
              <label htmlFor="model-dropdown" className="block text-xs font-medium text-[var(--foreground)] mb-1.5">
                {t("form.modelSelection")}
              </label>
              <input
                id="model-dropdown"
                type="text"
                value={value.model}
                onChange={(e) => update({ model: e.target.value })}
                placeholder={t("form.customModelPlaceholder")}
                className="input-japanese block w-full px-2.5 py-1.5 text-sm rounded-md bg-transparent text-[var(--foreground)] focus:outline-none focus:border-[var(--accent-primary)]"
                list="target-model-options"
              />
              <datalist id="target-model-options">
                {(selectedProvider?.models || []).map((m) => (
                  <option key={m} value={m} />
                ))}
              </datalist>
              {!selectedProvider?.supportsCustomModel && (
                <p className="text-[10px] text-[var(--muted)] mt-1">{t("form.modelCatalogLocked")}</p>
              )}
            </div>

            {/* API Key / Base URL(请求态,仅本次会话) */}
            <div>
              <label htmlFor="api-key-input" className="block text-xs font-medium text-[var(--foreground)] mb-1.5">
                {t("form.apiKey")}
              </label>
              <input
                id="api-key-input"
                type="password"
                autoComplete="off"
                value={value.api_key || ''}
                onChange={(e) => update({ api_key: e.target.value || undefined })}
                placeholder={selectedProvider?.apiKeyEnv || t("form.apiKeyPlaceholder")}
                className="input-japanese block w-full px-2.5 py-1.5 text-sm rounded-md bg-transparent text-[var(--foreground)] focus:outline-none focus:border-[var(--accent-primary)]"
              />
              {selectedProvider?.baseUrlEnv && (
                <input
                  id="base-url-input"
                  type="text"
                  value={value.base_url || ''}
                  onChange={(e) => update({ base_url: e.target.value || undefined })}
                  placeholder={`${selectedProvider.baseUrlEnv}${selectedProvider.baseUrlDefault ? ` (${selectedProvider.baseUrlDefault})` : ''}`}
                  className="input-japanese block w-full px-2.5 py-1.5 mt-2 text-sm rounded-md bg-transparent text-[var(--foreground)] focus:outline-none focus:border-[var(--accent-primary)]"
                />
              )}
              <p className="text-[10px] text-[var(--muted)] mt-1">
                {t("form.targetCredsNote")}
              </p>
            </div>
          </>
        )}

        {showFileFilters && (
          <div className="mt-4">
            <button
              type="button"
              onClick={() => setIsFilterSectionOpen(!isFilterSectionOpen)}
              className="flex items-center text-sm text-[var(--accent-primary)] hover:text-[var(--accent-primary)]/80 transition-colors"
            >
              <span className="mr-1.5 text-xs">{isFilterSectionOpen ? '▼' : '►'}</span>
              {t("form.advancedOptions")}
            </button>

            {isFilterSectionOpen && (
              <div className="mt-3 p-3 border border-[var(--border-color)]/70 rounded-md bg-[var(--background)]/30">
                {/* Filter Mode Selection */}
                <div className="mb-4">
                  <label className="block text-sm font-medium text-[var(--foreground)] mb-2">
                    {t("form.filterMode")}
                  </label>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => setFilterMode('exclude')}
                      className={`flex-1 px-3 py-2 rounded-md border text-sm transition-colors ${
                        filterMode === 'exclude'
                          ? 'bg-[var(--accent-primary)]/10 border-[var(--accent-primary)] text-[var(--accent-primary)]'
                          : 'border-[var(--border-color)] text-[var(--foreground)] hover:bg-[var(--background)]'
                      }`}
                    >
                      {t("form.excludeMode")}
                    </button>
                    <button
                      type="button"
                      onClick={() => setFilterMode('include')}
                      className={`flex-1 px-3 py-2 rounded-md border text-sm transition-colors ${
                        filterMode === 'include'
                          ? 'bg-[var(--accent-primary)]/10 border-[var(--accent-primary)] text-[var(--accent-primary)]'
                          : 'border-[var(--border-color)] text-[var(--foreground)] hover:bg-[var(--background)]'
                      }`}
                    >
                      {t("form.includeMode")}
                    </button>
                  </div>
                  <p className="text-xs text-[var(--muted)] mt-1">
                    {filterMode === 'exclude'
                      ? (t("form.excludeModeDescription"))
                      : (t("form.includeModeDescription"))
                    }
                  </p>
                </div>

                {/* Directories Section */}
                <div className="mb-4">
                  <label className="block text-sm font-medium text-[var(--muted)] mb-1.5">
                    {filterMode === 'exclude'
                      ? (t("form.excludedDirs"))
                      : (t("form.includedDirs"))
                    }
                  </label>
                  <textarea
                    value={filterMode === 'exclude' ? excludedDirs : includedDirs}
                    onChange={(e) => {
                      if (filterMode === 'exclude') {
                        setExcludedDirs?.(e.target.value);
                      } else {
                        setIncludedDirs?.(e.target.value);
                      }
                    }}
                    rows={4}
                    className="block w-full rounded-md border border-[var(--border-color)]/50 bg-[var(--input-bg)] text-[var(--foreground)] px-3 py-2 text-sm focus:border-[var(--accent-primary)] focus:ring-1 focus:ring-opacity-50 shadow-sm"
                    placeholder={filterMode === 'exclude'
                      ? (t("form.enterExcludedDirs"))
                      : (t("form.enterIncludedDirs"))
                    }
                  />
                  {filterMode === 'exclude' && (
                    <>
                      <div className="flex mt-1.5">
                        <button
                          type="button"
                          onClick={() => setShowDefaultDirs(!showDefaultDirs)}
                          className="text-xs text-[var(--accent-primary)] hover:text-[var(--accent-primary)]/80 transition-colors"
                        >
                          {showDefaultDirs ? (t("form.hideDefault")) : (t("form.viewDefault"))}
                        </button>
                      </div>
                      {showDefaultDirs && (
                        <div className="mt-2 p-2 rounded bg-[var(--background)]/50 text-xs">
                          <p className="mb-1 text-[var(--muted)]">{t("form.defaultNote")}</p>
                          <pre className="whitespace-pre-wrap font-mono text-[var(--muted)] overflow-y-auto max-h-32">{defaultExcludedDirs}</pre>
                        </div>
                      )}
                    </>
                  )}
                </div>

                {/* Files Section */}
                <div>
                  <label className="block text-sm font-medium text-[var(--muted)] mb-1.5">
                    {filterMode === 'exclude'
                      ? (t("form.excludedFiles"))
                      : (t("form.includedFiles"))
                    }
                  </label>
                  <textarea
                    value={filterMode === 'exclude' ? excludedFiles : includedFiles}
                    onChange={(e) => {
                      if (filterMode === 'exclude') {
                        setExcludedFiles?.(e.target.value);
                      } else {
                        setIncludedFiles?.(e.target.value);
                      }
                    }}
                    rows={4}
                    className="block w-full rounded-md border border-[var(--border-color)]/50 bg-[var(--input-bg)] text-[var(--foreground)] px-3 py-2 text-sm focus:border-[var(--accent-primary)] focus:ring-1 focus:ring-opacity-50 shadow-sm"
                    placeholder={filterMode === 'exclude'
                      ? (t("form.enterExcludedFiles"))
                      : (t("form.enterIncludedFiles"))
                    }
                  />
                  {filterMode === 'exclude' && (
                    <>
                      <div className="flex mt-1.5">
                        <button
                          type="button"
                          onClick={() => setShowDefaultFiles(!showDefaultFiles)}
                          className="text-xs text-[var(--accent-primary)] hover:text-[var(--accent-primary)]/80 transition-colors"
                        >
                          {showDefaultFiles ? (t("form.hideDefault")) : (t("form.viewDefault"))}
                        </button>
                      </div>
                      {showDefaultFiles && (
                        <div className="mt-2 p-2 rounded bg-[var(--background)]/50 text-xs">
                          <p className="mb-1 text-[var(--muted)]">{t("form.defaultNote")}</p>
                          <pre className="whitespace-pre-wrap font-mono text-[var(--muted)] overflow-y-auto max-h-32">{defaultExcludedFiles}</pre>
                        </div>
                      )}
                    </>
                  )}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// 兼容旧出口名(UserSelector → TargetSelector;消费方已迁移,不再新增用法)
export { strippedTarget };
