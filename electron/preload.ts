import { contextBridge, ipcRenderer } from 'electron';
import type {
  BackendInfo,
  DiscordInfo,
  LogEntry,
  LogExportResult,
  SchedulerInfo,
  ScoutApi,
  UpdaterState,
} from '../shared/types';

const sub = <T>(channel: string, cb: (val: T) => void): (() => void) => {
  const handler = (_e: unknown, val: T) => cb(val);
  ipcRenderer.on(channel, handler);
  return () => ipcRenderer.removeListener(channel, handler);
};

const api: ScoutApi = {
  app: {
    version: () => ipcRenderer.invoke('app:version'),
    openExternal: (url) => ipcRenderer.invoke('app:openExternal', url),
    systemConfig: () => ipcRenderer.invoke('app:systemConfig'),
    setSystemConfig: (partial) => ipcRenderer.invoke('app:setSystemConfig', partial),
  },
  backend: {
    info: (): Promise<BackendInfo> => ipcRenderer.invoke('backend:info'),
    port: (): Promise<number | null> => ipcRenderer.invoke('backend:port'),
    restart: () => ipcRenderer.invoke('backend:restart'),
    onStatus: (cb) => sub<BackendInfo>('backend:info', cb),
    logs: (limit?: number): Promise<LogEntry[]> => ipcRenderer.invoke('backend:logs', limit),
    exportLogs: (): Promise<LogExportResult> => ipcRenderer.invoke('backend:exportLogs'),
  },
  scheduler: {
    info: (): Promise<SchedulerInfo> => ipcRenderer.invoke('scheduler:info'),
    setConfig: (partial): Promise<SchedulerInfo> => ipcRenderer.invoke('scheduler:setConfig', partial),
    runNow: (job): Promise<SchedulerInfo> => ipcRenderer.invoke('scheduler:runNow', job),
  },
  discord: {
    info: (): Promise<DiscordInfo> => ipcRenderer.invoke('discord:info'),
    setConfig: (partial): Promise<DiscordInfo> => ipcRenderer.invoke('discord:setConfig', partial),
    sendNow: (): Promise<DiscordInfo> => ipcRenderer.invoke('discord:sendNow'),
    test: (url: string): Promise<{ ok: boolean; error?: string }> =>
      ipcRenderer.invoke('discord:test', url),
  },
  updater: {
    state: (): Promise<UpdaterState> => ipcRenderer.invoke('updater:state'),
    check: (): Promise<UpdaterState> => ipcRenderer.invoke('updater:check'),
    downloadAndInstall: (): Promise<void> => ipcRenderer.invoke('updater:downloadAndInstall'),
    onState: (cb) => sub<UpdaterState>('updater:state', cb),
  },
  window: {
    minimize: () => ipcRenderer.send('window:minimize'),
    maximize: () => ipcRenderer.send('window:maximize'),
    close: () => ipcRenderer.send('window:close'),
    isMaximized: () => ipcRenderer.invoke('window:isMaximized'),
    onMaximizeChange: (cb) => sub<boolean>('window:maximizeChange', cb),
  },
};

contextBridge.exposeInMainWorld('scout', api);
