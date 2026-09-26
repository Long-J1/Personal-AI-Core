'use strict';
// CoreManager 单测：复用/冲突/启动/超时/崩溃/收尸，全部用注入的假依赖
const test = require('node:test');
const assert = require('node:assert');
const { EventEmitter } = require('node:events');

const {
  CoreManager,
  ShellError,
  probeHealth,
  buildCoreCommand,
  buildCoreEnv,
} = require('../src/core-manager');

function fakeChild() {
  const c = new EventEmitter();
  c.pid = 424242424242;   // 必定无效的 pid：taskkill 必失败，走兜底 kill()
  c.killed = false;
  c.kill = () => { c.killed = true; return true; };
  c.stdout = new EventEmitter();
  c.stderr = new EventEmitter();
  return c;
}

function fetchOk() {
  return async () => ({ ok: true, json: async () => ({ version: '0.2.0', provider: 'ollama' }) });
}
function fetchDown() {
  return async () => { throw new Error('ECONNREFUSED'); };
}

// ---------- 纯函数 ----------

test('probeHealth: 通则 ok，不通不抛错', async () => {
  const ok = await probeHealth('http://x', fetchOk());
  assert.equal(ok.ok, true);
  assert.equal(ok.version, '0.2.0');
  const bad = await probeHealth('http://x', fetchDown());
  assert.equal(bad.ok, false);
  assert.match(bad.detail, /ECONNREFUSED/);
});

test('buildCoreCommand: 开发期用 python，打包后用自带 exe', () => {
  const dev = buildCoreCommand({ isPackaged: false, projectRoot: 'C:/proj', port: 8000, env: {} });
  assert.equal(dev.cmd, 'python');
  assert.ok(dev.args.includes('uvicorn'));
  assert.ok(dev.args.includes('8000'));
  assert.equal(dev.cwd, 'C:/proj');

  const prod = buildCoreCommand({ isPackaged: true, resourcesPath: 'C:/app/resources', port: 8000 });
  assert.ok(prod.cmd.endsWith('personal-ai-core.exe'));
  assert.ok(prod.cmd.includes('core'));
});

test('buildCoreEnv: 打包后数据/日志指到 AppData，开发期不越界', () => {
  const prod = buildCoreEnv({
    baseEnv: { PATH: 'x' }, isPackaged: true, appDataDir: 'C:/AppData/Personal AI', port: 8000,
  });
  assert.ok(prod.PAI_DATA_DIR.includes('Personal AI'));
  assert.ok(prod.PAI_DATA_DIR.includes('data'));
  assert.ok(prod.PAI_LOG_DIR.includes('logs'));
  assert.equal(prod.PAI_PORT, '8000');

  const dev = buildCoreEnv({ baseEnv: { PATH: 'x' }, isPackaged: false, port: 8000 });
  assert.equal(dev.PAI_DATA_DIR, undefined);
  assert.equal(dev.PAI_LOG_DIR, undefined);
});

// ---------- ensure 流程 ----------

function makeManager(overrides = {}) {
  const calls = { spawned: [] };
  const child = fakeChild();
  const deps = {
    port: 8000,
    fetchFn: fetchDown(),
    connectFn: async () => false,
    spawnFn: (cmd, args, opts) => { calls.spawned.push({ cmd, args, opts }); return child; },
    isPackaged: false,
    projectRoot: 'C:/proj',
    healthTimeoutMs: 500,
    pollIntervalMs: 10,
    ...overrides,
  };
  const mgr = new CoreManager(deps);
  return { mgr, child, calls };
}

test('ensure: Core 已在运行 → 复用，不重复启动', async () => {
  const { mgr, calls } = makeManager({ fetchFn: fetchOk() });
  const r = await mgr.ensure();
  assert.equal(r.mode, 'connected');
  assert.equal(calls.spawned.length, 0, '已有实例时不许拉第二个 Core');
});

test('ensure: 端口被别的程序占 → port-conflict，不静默失败', async () => {
  const { mgr, calls } = makeManager({ connectFn: async () => true });  // 端口通但健康检查不过
  await assert.rejects(
    () => mgr.ensure(),
    (err) => err instanceof ShellError && err.code === 'port-conflict',
  );
  assert.equal(calls.spawned.length, 0);
});

test('ensure: 正常启动 → 轮询健康通过 → spawned', async () => {
  let n = 0;
  const fetchFn = async () => {
    n += 1;
    if (n <= 1) throw new Error('ECONNREFUSED');   // 第一次（复用检查）不通
    return { ok: true, json: async () => ({ version: '0.2.0' }) };
  };
  const { mgr, calls } = makeManager({ fetchFn });
  const r = await mgr.ensure();
  assert.equal(r.mode, 'spawned');
  assert.equal(calls.spawned.length, 1);
  assert.ok(mgr.child, '启动后应持有子进程句柄');
});

test('ensure: 启动超时 → spawn-timeout 且收尸（不留孤儿）', async () => {
  const { mgr, child } = makeManager({ healthTimeoutMs: 60, pollIntervalMs: 10 });
  await assert.rejects(
    () => mgr.ensure(),
    (err) => err instanceof ShellError && err.code === 'spawn-timeout',
  );
  assert.equal(mgr.child, null, '超时后不许继续持有子进程');
  assert.equal(child.killed, true, '超时必须杀掉 Core');
});

test('ensure: Core 启动即退 → core-failed 带输出尾巴', async () => {
  const child = fakeChild();
  const mgr = new CoreManager({
    port: 8000,
    fetchFn: fetchDown(),
    connectFn: async () => false,
    spawnFn: () => {
      setImmediate(() => {
        child.stdout.emit('data', 'Traceback: something exploded\n');
        child.emit('exit', 1, null);
      });
      return child;
    },
    isPackaged: false,
    projectRoot: 'C:/proj',
    healthTimeoutMs: 500,
    pollIntervalMs: 10,
  });
  await assert.rejects(
    () => mgr.ensure(),
    (err) => err instanceof ShellError
      && err.code === 'core-failed'
      && /something exploded/.test(err.output || ''),
  );
});

test('运行中崩溃 → 触发 crashed 事件（错误页的依据）', async () => {
  let n = 0;
  const fetchFn = async () => {
    n += 1;
    if (n <= 1) throw new Error('down');
    return { ok: true, json: async () => ({ version: '0.2.0' }) };
  };
  const { mgr, child } = makeManager({ fetchFn });
  await mgr.ensure();

  const crashed = new Promise((resolve) => mgr.once('crashed', resolve));
  child.emit('exit', 1, null);
  const info = await crashed;
  assert.equal(info.code, 1);
  assert.equal(mgr.child, null);
});

test('kill: 主动关闭不触发 crashed（正常退出≠崩溃）', async () => {
  let n = 0;
  const fetchFn = async () => {
    n += 1;
    if (n <= 1) throw new Error('down');
    return { ok: true, json: async () => ({ version: '0.2.0' }) };
  };
  const { mgr, child } = makeManager({ fetchFn });
  await mgr.ensure();

  let crashedFired = false;
  mgr.once('crashed', () => { crashedFired = true; });
  const ok = mgr.kill();
  assert.equal(ok, true);
  child.emit('exit', 0, null);
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(crashedFired, false, '主动关闭不应报崩溃');
});
