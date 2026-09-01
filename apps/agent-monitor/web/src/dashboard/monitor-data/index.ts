// dashboard 监控数据层出口(纯 TS,零 React;事件守卫/折叠/表面折叠/帧协议)
export * from './types';
export {
  SURFACE_TYPES,
  applyEvent,
  deriveMessage,
  foldEvents,
  messagesAt,
  newSurface,
} from './surface';
export { RunFold } from './fold';
export { mergeEvents, sortedEvents } from './ws';
export type { HubFrame, SessionMeta, ViewerFrame } from './ws';
