'use strict';
// 桌面壳与页面之间唯一的安全桥（contextIsolation 下只暴露这几个能力，不碰数据）
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('pai', {
  onState: (cb) => ipcRenderer.on('core:state', (_e, s) => cb(s)),
  onError: (cb) => ipcRenderer.on('core:error', (_e, s) => cb(s)),
  getState: () => ipcRenderer.invoke('app:state'),
  retry: () => ipcRenderer.invoke('app:retry'),
  openLogs: () => ipcRenderer.invoke('app:open-logs'),
});
