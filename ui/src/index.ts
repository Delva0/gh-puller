// @gh-puller/ui 出口(本项目共享 UI 组件:Markdown/ThemeToggle/StateBadge/语言上下文/监控 WS 工具等)
// 从 apps/deepwiki-webui/web 提炼,现由 deepwiki-webui 与 agent-dashboard 共同消费(直接消费 TS 源码,无构建)
export { default as Markdown } from './components/Markdown';
export { default as ThemeToggle } from './components/ThemeToggle';
export { default as StateBadge } from './components/StateBadge';
export { default as TargetSelector } from './components/TargetSelector';
export { default as TokenInput } from './components/TokenInput';
export { default as ConfigurationModal } from './components/ConfigurationModal';
export { default as WikiTypeSelector } from './components/WikiTypeSelector';
export { default as ModelSelectionModal } from './components/ModelSelectionModal';
export * from './components/target';
export type { CodeTarget } from './components/CodeViewer';
export type { PhaseStatus } from './components/CodeMap';
export { default as WikiTreeView } from './components/WikiTreeView';
export { LanguageProvider, useLanguage } from './contexts/LanguageContext';
export type { Lang } from './contexts/LanguageContext';
export { monitorWsUrl } from './utils/monitorWs';
export type {
  CodemapCitation,
  CodemapStep,
  CodemapSection,
  CodemapData,
  CodemapPhase,
  CodemapEvent,
} from './types/codemap';
// 监控会话页(对话/轨迹;宿主 apps/agent-dashboard 主面板组装)
export { default as MonitorSessionList } from './components/MonitorSessionList';
export { default as MonitorStatusBar } from './components/MonitorStatusBar';
// 监控数据层(纯 TS 折叠/快照/时间轴/搜索;零 React)
export * from './monitor';
// 监控宿主接线层(hub 连接/会话 store;依赖 window,仅限浏览器端)
export { useMonitorSocket } from './hooks/useMonitorSocket';
export type { ConnStatus } from './hooks/useMonitorSocket';
export { useMonitorSession, sessionStore } from './hooks/useMonitorSession';
// dsh 1:1 对话/轨迹面板(消息):vended dsh 组件 + 桥接装配
export { default as DshConversationPanel } from './vendor/dsh/bridge/DshPanels';
export type { DshInstall } from './vendor/dsh/bridge/install';
