'use strict';
/**
 * Personal AI 桌面壳主进程（D011）。
 *
 * 分层铁律：Desktop App 只负责 启动/停止/连接/状态/入口/打包。
 * 所有 Conversation / Context / Memory / 数据操作一律走 Core API，
 * 这里绝不碰 SQLite、绝不直接改 Context。
 */
const path = require('node:path');
const { app, BrowserWindow, shell, ipcMain } = require('electron');

const { CoreManager, ShellError, DEFAULT_PORT } = require('./src/core-manager');
const { ensureOllama, killOllama } = require('./src/ollama-manager');

const PORT = Number(process.env.PAI_PORT || DEFAULT_PORT);
const CORE_URL = `http://127.0.0.1:${PORT}`;

let mainWindow = null;
let core = null;
let ollamaChild = null;
let booting = false;
let quitting = false;

const log = (...args) => console.log('[shell]', ...args);

function appDataDir() {
  return path.join(app.getPath('appData'), 'Personal AI');
}

function logDir() {
  return process.env.PAI_LOG_DIR || (core?.isPackaged ? path.join(appDataDir(), 'logs') : path.join(__dirname, '..', 'logs'));
}

/* ---------- 窗口与页面 ---------- */

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1180,
    height: 860,
    minWidth: 900,
    minHeight: 640,
    title: 'Personal AI',
    show: false,
    backgroundColor: '#f4f5f8',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  mainWindow.once('ready-to-show', () => mainWindow.show());

  // 初始加载页：Core 还没就绪时用户看到的是它，绝不白屏
  mainWindow.loadFile(path.join(__dirname, 'loading.html'));

  // 下载 = 导出数据包（Core 的 /api/data/export 等）；外部链接交给系统浏览器
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith(CORE_URL)) {
      mainWindow.webContents.downloadURL(url);
    } else if (url.startsWith('http://') || url.startsWith('https://')) {
      shell.openExternal(url);
    }
    return { action: 'deny' };
  });

  // 页面内导航只允许 Core 自己（防止被外链带跑）
  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith(CORE_URL)) {
      event.preventDefault();
      if (url.startsWith('http://') || url.startsWith('https://')) shell.openExternal(url);
    }
  });

  mainWindow.on('closed', () => { mainWindow = null; });
}

let lastState = null;

function sendState(state) {
  lastState = state;
  mainWindow?.webContents.send('core:state', state);
}

function showLoading(text) {
  sendState({ phase: 'loading', text });
}

async function showCore() {
  await mainWindow.loadURL(CORE_URL);
}

function showError(err) {
  const info = {
    code: err.code || 'unknown',
    message: err.message || String(err),
    output: err.output || '',
    logDir: logDir(),
    port: PORT,
  };
  log('显示错误页：', info.code, info.message);
  if (!mainWindow) return;
  mainWindow.loadFile(path.join(__dirname, 'error.html'));
  mainWindow.webContents.once('did-finish-load', () => {
    mainWindow?.webContents.send('core:error', info);
  });
}

/* ---------- 启动流程 ---------- */

async function boot() {
  if (booting) return;
  booting = true;
  try {
    showLoading('正在检查 Personal AI Core…');

    // 1) Core：能连就复用，连不上就拉起，都不行就明确报错（绝不白屏）
    const started = await core.ensure();
    showLoading(started.mode === 'connected' ? '连接到已运行的 Core…' : 'Core 已启动，加载界面…');

    // 2) Ollama 尽力而为（不阻塞主流程；失败只是"模型离线"）
    ensureOllama({
      log,
      env: process.env,
    }).then((r) => {
      if (r.started && r.child) ollamaChild = r.child;
      if (!r.running && r.started) log('Ollama 正在启动，模型稍后上线');
    }).catch((e) => log('Ollama 检查失败：', e.message));

    // 3) 健康检查过了才加载主界面
    if (!mainWindow) return;
    await showCore();

    // 自检模式：启动链路走通后自动优雅退出（验证"关闭不留孤儿 Core"）
    if (process.env.PAI_SHELL_SELFTEST === '1') {
      log('SELFTEST_OK');
      app.quit();
    }
  } catch (err) {
    if (err instanceof ShellError) showError(err);
    else showError(new ShellError('unknown', err.message || String(err)));
    // 自检模式：失败链路也要能自动收场，把错误码打出来当断言
    if (process.env.PAI_SHELL_SELFTEST === '1') {
      const code = (err && err.code) || 'unknown';
      log(`SELFTEST_FAIL:${code}`);
      setTimeout(() => app.quit(), 800);
    }
  } finally {
    booting = false;
  }
}

/* ---------- IPC（仅状态/重试/打开日志，不碰数据） ---------- */

ipcMain.handle('app:retry', async () => {
  log('用户点击重试');
  showLoading('正在重新连接…');
  await boot();
  return true;
});

// 加载页可能错过早先的状态事件（页面还没挂上监听），主动补拉一次
ipcMain.handle('app:state', () => lastState);

ipcMain.handle('app:open-logs', async () => {
  const dir = logDir();
  await shell.openPath(dir);
  return dir;
});

/* ---------- 生命周期 ---------- */

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.whenReady().then(() => {
    // 数据目录与安装目录分离（安装在 Program Files 也只往这里写）
    try { app.setAppUserModelId('com.personalai.core'); } catch {}

    core = new CoreManager({
      port: PORT,
      isPackaged: app.isPackaged,
      resourcesPath: process.resourcesPath,
      projectRoot: path.join(__dirname, '..'),
      appDataDir: appDataDir(),
      baseEnv: process.env,
      log,
    });

    // 运行中崩溃 → 错误页（不是白屏）
    core.on('crashed', (info) => {
      if (quitting || !mainWindow) return;
      showError(new ShellError('core-crashed',
        'Personal AI Core 进程意外退出，界面已断开连接。',
        { output: info.output || '' }));
    });

    createWindow();
    boot();
  });

  app.on('before-quit', (event) => {
    if (quitting) return;
    quitting = true;
    event.preventDefault();
    log('正在关闭…');
    try { core?.kill(); } catch (e) { log('关 Core 出错：', e.message); }
    try { killOllama(ollamaChild, log); } catch (e) { log('关 Ollama 出错：', e.message); }
    app.exit(0);
  });

  app.on('window-all-closed', () => {
    app.quit();
  });
}
