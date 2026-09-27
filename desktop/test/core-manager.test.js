'use strict';
// CoreManager 单测：复用/冲突/启动/超时/崩溃/收尸，全部用注入的假依赖
const test = require('node:test');
const assert = require('node:assert');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const {
  CoreManager,
  ShellError,
  probeHealth,
  buildCoreCommand,
  buildCoreEnv,
  findFreePort,
  readLastPort,
  writeLastPort,
} = require('../src/core-manager');

function fakeChild() {
  const c = new EventEmitter();
  c.pid = 424242424242;   // 必定无效的 pid：taskkill 必失败，走兜底 kill()
  c.killed = false;
  c.kill = () => { c.killed = true; return true; };
  c.stdout = new EventEmitter();
  c.stderr = new EventEmitter();
  // CoreManager 会调用 child.stdout.on('data', ...) 和 child.stderr.on('data', ...)
  c.stdout.on = c.stdout.on.bind(c.stdout);
  c.stderr.on = c.stderr.on.bind(c.stderr);
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

test('重试不粘连：kill 后再 ensure，新 Core 崩溃必须照常报 crashed', async () => {
  let n = 0;
  const fetchFn = async () => {
    n += 1;
    if (n <= 1) throw new Error('down');
    return { ok: true, json: async () => ({ version: '0.2.0' }) };
  };
  const { mgr, child } = makeManager({ fetchFn });
  await mgr.ensure();
  mgr.kill();                       // 上一轮收尸留下 _stopping=true
  child.emit('exit', 0, null);

  // 第二轮：复用同一个 manager（点"重试"就是这条路）
  const child2 = fakeChild();
  mgr.spawnFn = () => child2;
  let n2 = 0;
  mgr.fetchFn = async () => {
    n2 += 1;
    if (n2 <= 1) throw new Error('down');
    return { ok: true, json: async () => ({ version: '0.2.0' }) };
  };
  await mgr.ensure();

  const crashed = new Promise((resolve) => mgr.once('crashed', resolve));
  child2.emit('exit', 1, null);      // _stopping 若没复位，这行会被静默吞掉
  const info = await crashed;
  assert.equal(info.code, 1, '重试后的崩溃必须能报出来');
});

test('重试不粘连：异步 error 事件不许粘住新一轮 spawn', async () => {
  // 真实粘连路径是异步 error 事件（同步 throw 从不写 _childError）
  const child1 = fakeChild();
  const mgr = new CoreManager({
    port: 8000,
    fetchFn: async () => { throw new Error('down'); },
    connectFn: async () => false,
    spawnFn: () => {
      setImmediate(() => child1.emit('error', new Error('boom-async')));
      return child1;
    },
    isPackaged: false,
    projectRoot: 'C:/proj',
    healthTimeoutMs: 500,
    pollIntervalMs: 10,
  });
  await assert.rejects(() => mgr.ensure(), (e) => e.code === 'spawn-failed');
  assert.ok(mgr._childError, '异步 error 应该被记录下来');

  // 第二轮：新 child 干净，必须能 spawned（复位行被删这里就挂）
  const child2 = fakeChild();
  let n = 0;
  mgr.spawnFn = () => child2;
  mgr.fetchFn = async () => {
    n += 1;
    if (n <= 1) throw new Error('down');
    return { ok: true, json: async () => ({ version: '0.2.0' }) };
  };
  const r = await mgr.ensure();
  assert.equal(r.mode, 'spawned', '上一轮的异步 error 不许秒杀新一轮');
});

// ---------- 端口记忆与扫描（之前零覆盖） ----------

test('findFreePort: 跳过被占端口', async () => {
  const p = await findFreePort(async (port) => port === 8000);
  assert.equal(p, 8001);
});

test('findFreePort: 全满返回 null', async () => {
  const p = await findFreePort(async () => true);
  assert.equal(p, null);
});

test('端口记忆文件：写入→读回→损坏→null→越界→null', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pai-port-'));
  assert.equal(readLastPort(dir), null);
  writeLastPort(dir, 8005, () => {});
  assert.equal(readLastPort(dir), 8005);
  fs.writeFileSync(path.join(dir, 'core-port.json'), '{broken', 'utf8');
  assert.equal(readLastPort(dir), null);
  writeLastPort(dir, 9000, () => {});
  assert.equal(readLastPort(dir), null, '越界端口不认');
  assert.equal(readLastPort(''), null, '无数据目录返回 null');
});

test('ensure: 8000 被占 → 切 8001 启动，探活用新地址并写记忆', async () => {
  const urls = [];
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pai-port-'));
  let n = 0;
  const fetchFn = async (url) => {
    urls.push(url);
    n += 1;
    if (n <= 1) throw new Error('down');  // 8000 复用检查失败
    return { ok: true, json: async () => ({ version: '0.2.0' }) };
  };
  const calls = { spawned: [] };
  const child = fakeChild();
  const mgr = new CoreManager({
    port: 8000,
    fetchFn,
    connectFn: async (port) => port === 8000,  // 只有 8000 被占
    spawnFn: (cmd, args, opts) => {
      calls.spawned.push({ cmd, args, opts });
      return child;
    },
    isPackaged: false,
    projectRoot: 'C:/proj',
    appDataDir: dir,
    healthTimeoutMs: 500,
    pollIntervalMs: 10,
  });
  const r = await mgr.ensure();
  assert.equal(r.mode, 'spawned');
  assert.equal(mgr.port, 8001);
  assert.ok(urls.some((u) => u.includes(':8001/')), '探活必须用新端口地址');
  assert.equal(calls.spawned[0].opts.env.PAI_PORT, '8001');
  assert.equal(readLastPort(dir), 8001);
});

test('ensure: 并发调用合流，只 spawn 一次', async () => {
  let spawns = 0;
  const child = fakeChild();
  let n = 0;
  const mgr = new CoreManager({
    port: 8000,
    fetchFn: async () => {
      n += 1;
      if (n <= 1) {
        await new Promise((r) => setTimeout(r, 30));
        throw new Error('down');
      }
      return { ok: true, json: async () => ({ version: '0.2.0' }) };
    },
    connectFn: async () => false,
    spawnFn: () => { spawns += 1; return child; },
    isPackaged: false,
    projectRoot: 'C:/proj',
    healthTimeoutMs: 500,
    pollIntervalMs: 10,
  });
  const [a, b] = await Promise.all([mgr.ensure(), mgr.ensure()]);
  assert.equal(a.mode, 'spawned');
  assert.equal(b.mode, 'spawned');
  assert.equal(spawns, 1, '并发 ensure 不许拉起双 Core');
});

test('运行中正常退出（code=0）不算崩溃', async () => {
  let n = 0;
  const fetchFn = async () => {
    n += 1;
    if (n <= 1) throw new Error('down');
    return { ok: true, json: async () => ({ version: '0.2.0' }) };
  };
  const { mgr, child } = makeManager({ fetchFn });
  await mgr.ensure();
  let fired = false;
  mgr.once('crashed', () => { fired = true; });
  child.emit('exit', 0, null);
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(fired, false, 'code=0 是正常退出，不该弹错误页');
});

test('kill 失败保留句柄（不静默造孤儿）', () => {
  if (process.platform !== 'win32') return;  // taskkill 分支只在 Windows 可测
  const { mgr, child } = makeManager({});
  child.kill = () => { throw new Error('kill fail'); };
  mgr.child = child;  // 直接挂载，绕过 spawn
  const ok = mgr.kill();
  assert.equal(ok, false);
  assert.equal(mgr.child, child, '句柄必须保留以便重试');
});

test('非法显式端口被忽略并记日志', () => {
  const logs = [];
  const mgr = new CoreManager({ port: 99999, log: (m) => logs.push(m) });
  assert.equal(mgr.port, 8000);
  assert.ok(logs.some((m) => String(m).includes('非法')));
  const mgr2 = new CoreManager({ port: 'abc' });
  assert.equal(mgr2.port, 8000);
});
