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
const DEFAULT_HEALTH_TIMEOUT_MS = 90_000;   // 增加到 90s，防冷启动/机器忙时被误判
const DEFAULT_POLL_INTERVAL_MS = 400;
const PORT_RANGE_START = 8000;
const PORT_RANGE_END = 8099;

const fs = require('node:fs');

/** 端口记忆文件：放在数据目录，跟随重装/卸载不丢。 */
function getPortMemoryPath(appDataDir) {
  return path.join(appDataDir, 'core-port.json');
}

/** 读取上次用过的端口。 */
function readLastPort(appDataDir) {
  if (!appDataDir) return null;
  const p = getPortMemoryPath(appDataDir);
  try {
    const txt = fs.readFileSync(p, 'utf8');
    const obj = JSON.parse(txt);
    if (typeof obj.port === 'number' && obj.port >= PORT_RANGE_START && obj.port <= PORT_RANGE_END) {
      return obj.port;
    }
  } catch {}
  return null;
}

/** 写入本次选定的端口。 */
function writeLastPort(appDataDir, port) {
  if (!appDataDir) return;
  const p = getPortMemoryPath(appDataDir);
  try {
    fs.writeFileSync(p, JSON.stringify({ port, updated: Date.now() }), 'utf8');
  } catch {}
}

/** 扫描 8000-8099 找第一个能 bind 的端口。 */
async function findFreePort(connectFn) {
  for (let p = PORT_RANGE_START; p <= PORT_RANGE_END; p++) {
    if (!(await isPortOpen(p, connectFn, 200))) {
      return p;
    }
  }
  return null; // 全占了（极不可能）
}

class ShellError extends Error {
  constructor(code, message, extra = {}) {
    super(message);
    this.name = 'ShellError';
    this.code = code;           // port-conflict | core-failed | spawn-timeout | spawn-failed
    Object.assign(this, extra);
  }
}

/** 探测 Core 健康（/api/status）。返回 {ok, detail}。 */
async function probeHealth(baseUrl, fetchFn, timeoutMs = 5000) {
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
    // 优先级：显式传入 > 记忆文件 > 默认 8000
    const explicit = deps.port;
    const remembered = deps.appDataDir ? readLastPort(deps.appDataDir) : null;
    this.port = explicit || remembered || DEFAULT_PORT;
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
    this._output = [];
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

  /** 尝试在当前端口启动；若端口被占则扫描 8000-8099 找空闲端口重试（最多全范围）。 */
  async _trySpawnOnPort(port) {
    const self = this;
    // 重试前清场：上次失败的 childError / kill 留下的 _stopping 会粘住新一轮
    // （踩过的坑：点"重试"秒挂，或 Core 崩了被 _stopping 吞掉不报）
    self._childError = null;
    self._stopping = false;
    const cmd = buildCoreCommand({
      isPackaged: this.isPackaged,
      resourcesPath: this.resourcesPath,
      projectRoot: this.projectRoot,
      env: this.baseEnv,
      port,
    });
    const env = buildCoreEnv({
      baseEnv: this.baseEnv,
      isPackaged: this.isPackaged,
      appDataDir: this.appDataDir,
      port,
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

    self.child = child;
    child.stdout?.on('data', (d) => self._trackOutput(d));
    child.stderr?.on('data', (d) => self._trackOutput(d));
    child.on('error', (err) => {
      self._childError = err;
      self.log(`Core 进程错误：${err.message}`);
    });
    child.on('exit', (code, signal) => {
      if (self._stopping) return;
      self.log(`Core 意外退出 code=${code} signal=${signal}`);
      self.child = null;
      self.emit('crashed', {
        code,
        signal,
        output: self.tailOutput(),
      });
    });

    self.log(`正在启动 Core：${cmd.cmd} ${cmd.args.join(' ')} (port=${port})`);

    const deadline = Date.now() + self.healthTimeoutMs;
    while (Date.now() < deadline) {
      if (self._childError) {
        throw new ShellError('spawn-failed', `无法启动 Core（${cmd.cmd}）：${self._childError.message}`, {
          cmd: cmd.cmd,
          output: self.tailOutput(),
        });
      }
      const h = await probeHealth(self.baseUrl, self.fetchFn);
      if (h.ok) {
        self.log('Core 健康检查通过');
        return true;
      }
      if (self.child === null) {
        throw new ShellError(
          'core-failed',
          'Core 启动后立即退出，可能是运行环境损坏或数据目录不可写。',
          { output: self.tailOutput() },
        );
      }
      await sleep(self.pollIntervalMs);
    }
    return false; // 超时
  }

  /** 主入口：能连就复用，不能连就拉起；失败抛 ShellError（永不白屏的依据）。 */
  async ensure() {
    const self = this;
    // 1) 先探当前端口是否已有健康 Core
    const health = await probeHealth(self.baseUrl, self.fetchFn);
    if (health.ok) {
      self.log(`复用已运行的 Core（v${health.version ?? '?'}，port=${self.port}）`);
      return { mode: 'connected' };
    }

    // 2) 当前端口被占但不是我们的 Core → 扫描找空闲端口
    if (await isPortOpen(self.port, self.connectFn)) {
      self.log(`端口 ${self.port} 被占用，自动寻找空闲端口...`);
      const freePort = await findFreePort(self.connectFn);
      if (!freePort) {
        throw new ShellError(
          'port-conflict',
          `端口 ${PORT_RANGE_START}-${PORT_RANGE_END} 全被占用，Personal AI Core 无法启动。`,
          { port: self.port },
        );
      }
      self.port = freePort;
      self.baseUrl = `http://127.0.0.1:${self.port}`;
      self.log(`切换到空闲端口 ${self.port}`);
    }

    // 3) 在选定端口尝试启动（健康检查超时 90s）
    const ok = await self._trySpawnOnPort(self.port);
    if (!ok) {
      // 超时：收尸，别留孤儿
      self.kill();
      throw new ShellError(
        'spawn-timeout',
        `Core 在 ${Math.round(self.healthTimeoutMs / 1000)} 秒内未通过健康检查。`,
        { output: self.tailOutput() },
      );
    }

    // 4) 启动成功，持久化端口供下次复用
    if (self.appDataDir) {
      writeLastPort(self.appDataDir, self.port);
    }
    return { mode: 'spawned' };
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
