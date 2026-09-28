import { useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import { Download, LoaderCircle, RotateCcw, Unplug, Upload } from 'lucide-react';
import { Modal } from './components';
import { PasswordInput } from './controls';
import { LanguageContext, useText } from './i18n';
import { download } from './db';
import { availabilityHint, credentialTestHint, fieldDescription, fieldValue, numericPolicy, uiLabel, validPreferences } from './ui-model';
import type { Availability } from './availability';
import { api } from './api';
import { emptyCredentials, emptyPreferences, preferencesSchema, settingsFor, type Catalog, type ConfigField,
  type ConfigValues, type Credentials, type Preferences } from './types';
import type { ModelDiscovery } from './useModels';
import type { ConfigurationValidation } from './useValidation';

function ConfigInput({ field, value, onChange, onDraft, consumers }: {
  field: ConfigField; value: ConfigValues[string]; onChange: (value: ConfigValues[string]) => void;
  onDraft: (reason: string) => void; consumers?: ReactNode;
}) {
  const language = useContext(LanguageContext);
  const t = useText();
  const effective = fieldValue(field, value);
  const format = (v: ConfigValues[string]) => v === null ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v);
  const [text, setText] = useState(format(effective));
  const [invalid, setInvalid] = useState(false);
  useEffect(() => { setText(format(effective)); setInvalid(false); }, [effective]);
  const label = uiLabel(field.key, language);
  const description = fieldDescription(field, language);
  const shared = { name: field.key, 'aria-label': label, 'aria-description': description || undefined };
  let control: ReactNode;
  if (field.choices.length) {
    control = <select {...shared} value={JSON.stringify(effective)} onChange={e => onChange(JSON.parse(e.target.value))}>
      {field.choices.map(choice => <option key={JSON.stringify(choice.value)} value={JSON.stringify(choice.value)} disabled={Boolean(choice.reason)}>
        {typeof choice.value === 'boolean' ? (choice.value ? t('开启') : language === 'zh' ? '关闭' : 'Off') : String(choice.value)}
        {choice.reason ? ` · ${choice.reason}` : ''}</option>)}
    </select>;
  } else if (field.type === 'boolean') {
    control = <input {...shared} type="checkbox" role="switch" checked={Boolean(effective)} onChange={e => onChange(e.target.checked)} />;
  } else if (field.type === 'integer' || field.type === 'number') {
    const policy = numericPolicy(field);
    control = <><input {...shared} {...policy} type="number" value={text} aria-invalid={invalid}
      onChange={e => {
        setText(e.target.value);
        const next = Number(e.target.value);
        const valid = Boolean(e.target.value) && Number.isFinite(next) && next >= policy.min && next <= policy.max && (field.type !== 'integer' || Number.isInteger(next));
        setInvalid(!valid);
        onDraft(valid ? '' : `${policy.min} – ${policy.max}`);
        if (valid) onChange(next);
      }} onBlur={() => { setText(format(effective)); setInvalid(false); onDraft(''); }} />
      {invalid && <small className="error-text">{policy.min} – {policy.max}</small>}</>;
  } else if (field.type === 'string') {
    control = <input {...shared} value={String(effective ?? '')} onChange={e => onChange(e.target.value)} />;
  } else {
    control = <textarea {...shared} value={text} placeholder="JSON" aria-invalid={invalid} onChange={e => {
      setText(e.target.value);
      try { onChange(JSON.parse(e.target.value)); setInvalid(false); onDraft(''); }
      catch { setInvalid(true); onDraft('Invalid JSON'); }
    }} onBlur={() => { setText(format(effective)); setInvalid(false); onDraft(''); }} />;
  }
  return <label className={`config-field ${field.type === 'boolean' && !field.choices.length ? 'switch-field' : ''}`}>
    <span className="config-label"><span title={description || undefined}>{label}</span>{consumers}</span>{control}
  </label>;
}

function AvailabilityDot({ status }: { status: Availability }) {
  const language = useContext(LanguageContext);
  const t = useText();
  return <span className={`availability-dot ${status.pending ? 'pending' : status.available ? 'available' : 'unavailable'}`} role="img"
    aria-label={t(status.pending ? '待校验' : status.available ? '可用' : '不可用')} title={availabilityHint(status, language)} />;
}

export function SettingsPanel({ preferences, credentials, catalog, onChange, onClose, configured, discovery, validation, onDraft }: {
  preferences: Preferences; credentials: Credentials; catalog: Catalog;
  onChange: (preferences: Preferences, credentials: Credentials) => void; onClose: () => void; configured: Set<string>;
  discovery: ModelDiscovery;
  validation: ConfigurationValidation; onDraft: (key: string, reason: string) => void;
}) {
  const t = useText();
  const language = useContext(LanguageContext);
  const [tab, setTab] = useState('model');
  const [notice, setNotice] = useState('');
  const [keyStatus, setKeyStatus] = useState<Record<string, string>>({});
  const [selectedTool, setSelectedTool] = useState('');
  const tested = useRef<Record<string, string>>({});
  const file = useRef<HTMLInputElement>(null);
  const tools = validation.tools;
  const credentialSpecs = Object.assign({}, ...catalog.agents.map(agent => agent.credentials)) as Catalog['agents'][number]['credentials'];
  const toolFields = [...new Map(catalog.agents.flatMap(item => item.fields).filter(field => field.tool).map(field => [field.key, field])).values()];
  const visibleKeys = [...new Set(tools.filter(tool => !selectedTool || tool.id === selectedTool).flatMap(tool => tool.configuration))]
    .filter(key => credentialSpecs[key] ? validation.fields[key]?.active !== false : toolFields.some(field => field.key === key));
  useEffect(() => { if (selectedTool && !tools.some(tool => tool.id === selectedTool)) setSelectedTool(''); }, [tools, selectedTool]);
  async function testKey(key: string, value: string, force = false) {
    if (key !== 'api_key' && !credentialSpecs[key]?.testable) return;
    if (!value.trim() && !(key === 'api_key' && configured.has(key))) return;
    const signature = key === 'api_key' ? JSON.stringify([preferences.connection.base_url ?? catalog.defaults.base_url, value]) : value;
    if (tested.current[key] === signature && (!force || keyStatus[key] === 'loading')) return;
    tested.current[key] = signature;
    if (key === 'api_key') { await discovery.refresh(value); return; }
    setKeyStatus(status => ({ ...status, [key]: 'loading' }));
    validation.refresh();
    try {
      await api('/credentials/test', 'POST', { name: key, value });
      if (tested.current[key] === signature) setKeyStatus(status => ({ ...status, [key]: '连接成功' }));
    } catch (error) {
      if (tested.current[key] === signature) setKeyStatus(status => ({ ...status, [key]: error instanceof Error ? error.message : '操作失败，请重试' }));
    } finally { validation.refresh(); }
  }
  const consumers = (key: string) => <span className="config-consumers" aria-label={t('使用此配置的工具')}>
    {tools.filter(tool => tool.configuration.includes(key)).map(tool => <button type="button" key={tool.id}
      title={`${t('筛选工具配置')}: ${tool.id}`} className={tool.id === selectedTool ? 'selected' : ''}
      onClick={event => { event.preventDefault(); setSelectedTool(selectedTool === tool.id ? '' : tool.id); }}>{tool.id}</button>)}</span>;
  const keyField = (key: string, label: string, links?: ReactNode) => <label key={key}>
    <span className="config-label"><span>{label}</span>{links}</span><PasswordInput autoComplete="off" name={key} aria-label={label}
    value={credentials[key] ?? ''} onChange={e => {
      delete tested.current[key];
      onChange(preferences, { ...credentials, [key]: e.target.value }); setKeyStatus(status => ({ ...status, [key]: '' }));
    }} onCommit={value => void testKey(key, value)}
    placeholder={t(configured.has(key) ? '当前服务端会话已保留，留空可继续使用' : '仅当前页面与活跃会话保留')}
    actions={(key === 'api_key' || credentialSpecs[key]?.testable) && <button type="button" className="icon-button" aria-label={key === 'api_key' ? t('测试连接') : `${t('测试连接')} ${label}`}
      title={credentialTestHint(key, language) || t('测试连接')}
      disabled={key === 'api_key' ? discovery.status === 'loading' : keyStatus[key] === 'loading'}
      onMouseDown={event => event.preventDefault()} onClick={() => void testKey(key, credentials[key] ?? '', true)}>
      {(key === 'api_key' ? discovery.status === 'loading' : keyStatus[key] === 'loading') ? <LoaderCircle size={16} className="spin" /> : <Unplug size={16} />}</button>} />
    {keyStatus[key] && keyStatus[key] !== 'loading' && <small role="status">{t(keyStatus[key])}</small>}</label>;
  async function importConfig(selected?: File) {
    if (!selected) return;
    try {
      if (selected.size > 1024 * 1024) throw new Error('Too large');
      const body = JSON.parse(await selected.text());
      if (body.format !== 'agent-chat-settings') throw new Error('Unknown format');
      const next = preferencesSchema.parse(body.preferences);
      if (!validPreferences(next, catalog)) throw new Error('Out of range');
      onChange(next, credentials); setNotice('配置已导入，密钥不会从文件读取。');
    } catch { setNotice('配置文件无效'); }
    finally { if (file.current) file.current.value = ''; }
  }
  return <Modal title={t('设置')} wide onClose={onClose}>
    <div className="settings-layout"><nav className="settings-tabs" role="tablist" aria-label={t('设置')}>
      {[['model', t('模型'), '输入即保存。密钥仅保留在当前页面和活跃会话内存。'], ['agent', 'Agent', '每个 agent 分别保存自己的配置。'],
        ['tools', t('工具'), '使用此工具的 agent 共用这些配置；已开始的会话保留原配置。']].map(([id, label, tip]) =>
        <button key={id} type="button" role="tab" aria-selected={tab === id} aria-controls={`settings-${id}`}
          title={t(tip)} id={`settings-tab-${id}`} onClick={() => setTab(id)}>{label}</button>)}
    </nav><div className="settings-page" key={tab} role="tabpanel" id={`settings-${tab}`} aria-labelledby={`settings-tab-${tab}`}>
      {tab === 'model' && <><section><h3>{t('模型连接')}</h3>
        <label>{t('模型地址')}<input type="url" value={preferences.connection.base_url ?? catalog.defaults.base_url}
          onChange={e => onChange({ ...preferences, connection: { base_url: e.target.value } }, credentials)}
          onBlur={() => void testKey('api_key', credentials.api_key ?? '')} placeholder="https://api.example.com/v1" /></label>
        {keyField('api_key', t('模型 API Key'))}
        {discovery.status !== 'idle' && <p className={`settings-note ${discovery.status === 'error' ? 'error-text' : ''}`} role="status">
          {discovery.status === 'loading' ? t('检测模型中…') : discovery.status === 'success' ? `${t('连接成功')} · ${discovery.models.length} ${t('模型')}` : t(discovery.error)}</p>}
      </section><section className="settings-section"><h3>{t('语言')}</h3><select aria-label={t('语言')} value={preferences.language}
          onChange={e => onChange({ ...preferences, language: e.target.value as Preferences['language'] }, credentials)}>
          <option value="zh">简体中文</option><option value="en">English</option></select>
      </section></>}
      {tab === 'agent' && catalog.agents.map(capability => <section className="settings-section" key={capability.id}>
        <div className="settings-section-title"><AvailabilityDot status={validation.agent(capability.id)} /><h3>{capability.name}</h3></div>
        {capability.fields.filter(field => !field.tool).map(field => <ConfigInput key={field.key} field={field}
          onDraft={reason => onDraft(`agent:${capability.id}:${field.key}`, reason)}
          value={settingsFor(capability.id, preferences, catalog).options[field.key]} onChange={next => onChange({ ...preferences,
            agents: { ...preferences.agents, [capability.id]: { ...preferences.agents[capability.id], [field.key]: next } } }, credentials)} />)}
      </section>)}
      {tab === 'tools' && <>
        <div className="tool-index" aria-label={t('工具')}>
          {tools.map(tool => <button key={tool.id} type="button" className="tool-node" data-tool={tool.id} aria-label={tool.id}
            aria-pressed={selectedTool === tool.id} title={availabilityHint(validation.tool(tool.id), language)}
            onClick={() => setSelectedTool(selectedTool === tool.id ? '' : tool.id)}>
            <AvailabilityDot status={validation.tool(tool.id)} /><span>{tool.id}</span></button>)}
        </div>
        <div className="shared-config-heading"><h3>{t('共享配置')}</h3>
          <button type="button" className="text-button" onClick={() => setSelectedTool('')} disabled={!selectedTool}>{t('全部配置')}</button></div>
        {visibleKeys.map(key => <section className="shared-config" data-config={key} key={key}>
          {credentialSpecs[key] ? keyField(key, uiLabel(key, language), consumers(key)) : <ConfigInput
            field={toolFields.find(field => field.key === key)!}
            value={key in preferences.tools ? preferences.tools[key] : toolFields.find(field => field.key === key)!.default}
            consumers={consumers(key)} onDraft={reason => onDraft(`tool:${key}`, reason)}
            onChange={next => onChange({ ...preferences, tools: { ...preferences.tools, [key]: next } }, credentials)} />}
        </section>)}
        {!visibleKeys.length && <p className="settings-note">{t('此工具无需浏览器配置')}</p>}
      </>}
    </div></div>
    {notice && <p className="settings-feedback" role="status">{t(notice)}</p>}
    <footer className="settings-footer">
      <button className="secondary" type="button" onClick={() => { onChange({ ...emptyPreferences }, { ...emptyCredentials }); tested.current = {}; setKeyStatus({}); setNotice('已恢复默认配置，当前页面的密钥已清空。'); }}><RotateCcw size={14} />{t('恢复默认配置')}</button>
      <span />
      <button className="secondary" type="button" onClick={() => file.current?.click()}><Upload size={14} />{t('导入配置')}</button>
      <button className="secondary" type="button" onClick={() => download('agent-chat-settings.json', { format: 'agent-chat-settings', preferences })}><Download size={14} />{t('导出配置')}</button>
      <input ref={file} type="file" accept="application/json,.json" hidden aria-label={t('导入配置')} onChange={e => void importConfig(e.target.files?.[0])} />
    </footer>
  </Modal>;
}
