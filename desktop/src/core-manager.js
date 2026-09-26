'use strict';
/**
 * Core 生命周期管理（桌面壳的心脏，D011）：
 *   检查已运行 → 端口冲突识别 → 启动 → 健康检查 → 崩溃监听 → 关闭时收尸
 *
 * 所有外部依赖（fetch/spawn/net）都可注入，便于 node:test 单测。
 * 原则：Core 是灵魂，这里只负责"把它请出来、看着它、送走它"。
 */
const net = require('node:net');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { EventEmitter } = require('node:events');

const DEFAULT_PORT = 8000;
const DEFAULT_HEALTH_TIMEOUT_MS = 60_000;   // PyInstaller 冷启动也够用
const DEFAULT_POLL_INTERVAL_MS = 400;

class ShellError extends Error {
  constructor(code, message, extra = {}) {
    super(message);
    this.name = 'ShellError';
    this.code = code;           // port-conflict | core-failed | spawn-timeout | spawn-failed
    Object.assign(this, extra);
  }
}

/** 探测 Core 健康（/api/status）。返回 {ok, detail}。 */
async function probeHealth(baseUrl, fetchFn, timeoutMs = 2000) {
  try {
    const res = await fetchFn(`${baseUrl}/api/status`, {
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (!res.ok) return { ok: false, detail: `HTTP ${res.status}` };
    const json = await res.json();
    return { ok: true, version: json.version, provider: json.provider };
  } catch (err) {
    return { ok: false, detail: err && err.message ? err.message : String(err) };
  }
}

/** TCP 端口通不通（通但健康检查不过 = 被别的程序占了）。 */
function isPortOpen(port, connectFn, timeoutMs = 1500) {
  if (connectFn) return Promise.resolve(connectFn(port, timeoutMs));
  return new Promise((resolve) => {
    const sock = net.connect({ port, host: '127.0.0.1' });
    let done = false;
    const finish = (v) => { if (!done) { done = true; try { sock.destroy(); } catch {} resolve(v); } };
    sock.setTimeout(timeoutMs, () => finish(false));
    sock.on('connect', () => finish(true));
    sock.on('error', () => finish(false));
  });
}

/** 装配启动 Core 的命令（打包后用自带 exe，开发期用 python）。 */
function buildCoreCommand({ isPackaged, resourcesPath, projectRoot, env = {}, port }) {
  if (isPackaged) {
    const coreDir = path.join(resourcesPath, 'core');
    return {
      cmd: path.join(coreDir, 'personal-ai-core.exe'),
      args: [],
      cwd: coreDir,
    };
  }
  return {
    cmd: env.PAI_PYTHON || 'python',
    args: [
      '-m', 'uvicorn', 'interfaces.webapp:app',
      '--host', '127.0.0.1',
      '--port', String(port),
      '--log-level', 'warning',
    ],
    cwd: projectRoot,
  };
}

/** 装配 Core 进程环境：打包后数据/日志全部指到 %APPDATA%（安装目录只读）。 */
function buildCoreEnv({ baseEnv, isPackaged, appDataDir, port }) {
  const env = { ...baseEnv };
  env.PAI_PORT = String(port);
  if (isPackaged && appDataDir) {
    env.PAI_DATA_DIR = path.join(appDataDir, 'data');
    env.PAI_LOG_DIR = path.join(appDataDir, 'logs');
  }
  return env;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

class CoreManager extends EventEmitter {
  /**
   * deps: { port, fetchFn, spawnFn, connectFn, log, isPackaged, resourcesPath,
   *         projectRoot, appDataDir, baseEnv, healthTimeoutMs, pollIntervalMs }
   */
  constructor(deps = {}) {
    super();
    this.port = deps.port || DEFAULT_PORT;
    this.baseUrl = `http://127.0.0.1:${this.port}`;
    this.fetchFn = deps.fetchFn || globalThis.fetch;
    this.spawnFn = deps.spawnFn || spawn;
    this.connectFn = deps.connectFn || null;
    this.log = deps.log || (() => {});
    this.isPackaged = Boolean(deps.isPackaged);
    this.resourcesPath = deps.resourcesPath || '';
    this.projectRoot = deps.projectRoot || '';
    this.appDataDir = deps.appDataDir || '';
    this.baseEnv = deps.baseEnv || process.env;
    this.healthTimeoutMs = deps.healthTimeoutMs ?? DEFAULT_HEALTH_TIMEOUT_MS;
    this.pollIntervalMs = deps.pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS;

    this.child = null;
    this._stopping = false;
    this._output = [];        // Core stdout/stderr 尾巴（错误页展示用）
    this._outputLimit = 40;
  }

  tailOutput() {
    return this._output.join('\n').slice(-2000);
  }

  _trackOutput(chunk) {
    const text = String(chunk);
    for (const line of text.split(/\r?\n/)) {
      if (line.trim()) this._output.push(line);
    }
    if (this._output.length > this._outputLimit) {
      this._output.splice(0, this._output.length - this._outputLimit);
    }
  }

  /** 主入口：能连就复用，不能连就拉起；失败抛 ShellError（永不白屏的依据）。 */
  async ensure() {
    const health = await probeHealth(this.baseUrl, this.fetchFn);
    if (health.ok) {
      this.log(`复用已运行的 Core（v${health.version ?? '?'}）`);
      return { mode: 'connected' };
    }

    // 端口被占但健康检查不过 → 不是我们的 Core，别静默失败
    if (await isPortOpen(this.port, this.connectFn)) {
      throw new ShellError(
        'port-conflict',
        `端口 ${this.port} 已被其他程序占用，Personal AI Core 无法启动。`,
        { port: this.port },
      );
    }

    const cmd = buildCoreCommand({
      isPackaged: this.isPackaged,
      resourcesPath: this.resourcesPath,
      projectRoot: this.projectRoot,
      env: this.baseEnv,
      port: this.port,
    });
    const env = buildCoreEnv({
      baseEnv: this.baseEnv,
      isPackaged: this.isPackaged,
      appDataDir: this.appDataDir,
      port: this.port,
    });

    let child;
    try {
      child = this.spawnFn(cmd.cmd, cmd.args, {
        cwd: cmd.cwd,
        env,
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch (err) {
      throw new ShellError('spawn-failed', `无法启动 Core（${cmd.cmd}）：${err.message}`, {
        cmd: cmd.cmd,
      });
    }

    this.child = child;
    child.stdout?.on('data', (d) => this._trackOutput(d));
    child.stderr?.on('data', (d) => this._trackOutput(d));
    child.on('error', (err) => {
      this._childError = err;
      this.log(`Core 进程错误：${err.message}`);
    });
    child.on('exit', (code, signal) => {
      if (this._stopping) return;
      this.log(`Core 意外退出 code=${code} signal=${signal}`);
      this.child = null;
      this.emit('crashed', {
        code,
        signal,
        output: this.tailOutput(),
      });
    });

    this.log(`正在启动 Core：${cmd.cmd} ${cmd.args.join(' ')}`);

    const deadline = Date.now() + this.healthTimeoutMs;
    while (Date.now() < deadline) {
      if (this._childError) {
        throw new ShellError('spawn-failed', `无法启动 Core（${cmd.cmd}）：${this._childError.message}`, {
          cmd: cmd.cmd,
          output: this.tailOutput(),
        });
      }
      const h = await probeHealth(this.baseUrl, this.fetchFn);
      if (h.ok) {
        this.log('Core 健康检查通过');
        return { mode: 'spawned' };
      }
      if (this.child === null) {
        // 已经退出了（exit 回调里置空）
        throw new ShellError(
          'core-failed',
          'Core 启动后立即退出，可能是运行环境损坏或数据目录不可写。',
          { output: this.tailOutput() },
        );
      }
      await sleep(this.pollIntervalMs);
    }

    // 超时：收尸，别留孤儿
    this.kill();
    throw new ShellError(
      'spawn-timeout',
      `Core 在 ${Math.round(this.healthTimeoutMs / 1000)} 秒内未通过健康检查。`,
      { output: this.tailOutput() },
    );
  }

  /** 关闭自己拉起的 Core（Windows 下连子进程树一起收，绝不留孤儿）。 */
  kill() {
    const child = this.child;
    if (!child) return false;
    this._stopping = true;
    this.child = null;
    try {
      if (process.platform === 'win32') {
        const { execFileSync } = require('node:child_process');
        try {
          execFileSync('taskkill', ['/pid', String(child.pid), '/T', '/F'], { windowsHide: true });
        } catch {
          child.kill();
        }
      } else {
        child.kill('SIGTERM');
      }
    } catch (err) {
      this.log(`关闭 Core 失败：${err.message}`);
      return false;
    }
    this.log('已关闭 Core');
    return true;
  }
}

module.exports = {
  CoreManager,
  ShellError,
  probeHealth,
  isPortOpen,
  buildCoreCommand,
  buildCoreEnv,
  DEFAULT_PORT,
};
