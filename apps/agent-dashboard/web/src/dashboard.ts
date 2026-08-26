// agent-dashboard 专用面聚合出口(不在 @gh-puller/ui 内;deepwiki 的 Next 图不接触
// 浏览器面板树 —— useLayoutEffect/useSyncExternalStore 仅此消费方可见)
export { default as DshConversationPanel } from './dashboard/vendor/dsh/bridge/DshPanels';
export type { DshConversationPanelProps } from './dashboard/vendor/dsh/bridge/DshPanels';
export type { DshInstall } from './dashboard/vendor/dsh/bridge/install';
export { useMonitorSocket } from './dashboard/hooks/useMonitorSocket';
export type { ConnStatus } from './dashboard/hooks/useMonitorSocket';
export { useMonitorSession, sessionStore } from './dashboard/hooks/useMonitorSession';
export { default as MonitorSessionList } from './dashboard/components/MonitorSessionList';
export { default as MonitorStatusBar } from './dashboard/components/MonitorStatusBar';
export { monitorWsUrl } from './dashboard/utils/monitorWs';
