'use strict';
// Ollama 尽力而为拉起的单测（重点：模型目录自动挑选——D 盘坑）
const test = require('node:test');
const assert = require('node:assert');
const { EventEmitter } = require('node:events');

const { ensureOllama, pickModelsDir, dirHasModels } = require('../src/ollama-manager');

// 假文件系统：dir → 有没有 manifest 文件（统一分隔符，兼容 Windows 反斜杠）
function fakeFs(modelDirs) {
  const norm = (p) => String(p).replace(/\\/g, '/');
  const hit = (p) => modelDirs.some((d) => norm(p).startsWith(norm(d)) && norm(p).endsWith('manifests'));
  return {
    existsSync: (p) => hit(p),
    readdirSync: (p) => (hit(p) ? ['hf.co/model/Q4'] : []),
  };
}

test('dirHasModels: 有 manifest 才算有模型', () => {
  const fsImpl = fakeFs(['C:/home/.ollama/models']);
  assert.equal(dirHasModels('C:/home/.ollama/models', fsImpl), true);
  assert.equal(dirHasModels('D:/other/models', fsImpl), false);
});

test('pickModelsDir: 设了环境变量就听环境变量', () => {
  const got = pickModelsDir({
    env: { OLLAMA_MODELS: 'E:/m' },
    homeDir: 'C:/home',
    dOllamaPath: 'D:/.ollama/models',
    fsImpl: fakeFs(['C:/home/.ollama/models']),
  });
  assert.equal(got, 'E:/m');
});

test('pickModelsDir: 默认目录有货 → 不干预（返回 null）', () => {
  const got = pickModelsDir({
    env: {},
    homeDir: 'C:/home',
    dOllamaPath: 'D:/.ollama/models',
    fsImpl: fakeFs(['C:/home/.ollama/models']),
  });
  assert.equal(got, null);
});

test('pickModelsDir: 默认目录是空的、D 盘有货 → 指向 D 盘（本机的坑）', () => {
  const got = pickModelsDir({
    env: {},
    homeDir: 'C:/home',
    dOllamaPath: 'D:/.ollama/models',
    fsImpl: fakeFs(['D:/.ollama/models']),
  });
  assert.equal(got, 'D:/.ollama/models');
});

test('pickModelsDir: 哪都没有 → null（新装环境，交给用户拉模型）', () => {
  const got = pickModelsDir({
    env: {},
    homeDir: 'C:/home',
    dOllamaPath: 'D:/.ollama/models',
    fsImpl: fakeFs([]),
  });
  assert.equal(got, null);
});

// ---------- ensureOllama ----------

function fetchUp() {
  return async () => ({ ok: true, json: async () => ({ version: '0.32.15' }) });
}
function fetchDown() {
  return async () => { throw new Error('ECONNREFUSED'); };
}

test('ensureOllama: 已在跑 → 复用，不重复启动', async () => {
  let spawned = 0;
  const r = await ensureOllama({
    fetchFn: fetchUp(),
    spawnFn: () => { spawned += 1; return new EventEmitter(); },
    env: {},
  });
  assert.equal(r.running, true);
  assert.equal(r.started, false);
  assert.equal(spawned, 0);
});

test('ensureOllama: 没跑 → 带对模型目录拉起', async () => {
  let captured = null;
  const child = new EventEmitter();
  child.pid = 1;
  const r = await ensureOllama({
    fetchFn: fetchDown(),
    spawnFn: (cmd, args, opts) => { captured = { cmd, args, opts }; return child; },
    env: { USERPROFILE: 'C:/home' },
    fsImpl: fakeFs(['D:/.ollama/models']),
  });
  assert.equal(r.started, true);
  assert.equal(r.running, false, '不等就绪时如实报告');
  assert.equal(captured.cmd, 'ollama');
  assert.deepEqual(captured.args, ['serve']);
  assert.equal(captured.opts.env.OLLAMA_MODELS, 'D:\\.ollama\\models', '必须把模型目录指对');
});

test('ensureOllama: 拉不起来也永不抛错（只是模型离线）', async () => {
  const r = await ensureOllama({
    fetchFn: fetchDown(),
    spawnFn: () => { throw new Error('not found'); },
    env: {},
  });
  assert.equal(r.running, false);
  assert.equal(r.started, false);
  assert.match(r.error, /not found/);
});
