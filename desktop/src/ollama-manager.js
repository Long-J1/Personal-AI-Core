'use strict';
/**
 * Ollama 尽力而为拉起（D011）：
 *   本机的坑——Ollama 服务没跑时模型列表可能是空的（模型实际在 D:\.ollama\models），
 *   直接 `ollama serve` 会读默认空目录，Core 会报"本机没有模型"。
 *   这里按 env → 默认目录 → D 盘后备 的顺序自动挑对模型目录。
 *
 * 注意：Core 不依赖这里成功——Ollama 不通只是"模型离线"，Core 本身照常工作。
 */
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');

const OLLAMA_URL = 'http://127.0.0.1:11434';

function dirHasModels(dir, fsImpl = fs) {
  try {
    const manifests = path.join(dir, 'manifests');
    if (!fsImpl.existsSync(manifests)) return false;
    const entries = fsImpl.readdirSync(manifests, { recursive: true });
    return entries.length > 0;
  } catch {
    return false;
  }
}

/**
 * 该给 ollama serve 设哪个 OLLAMA_MODELS？
 * 返回 null = 不设（用它自己的默认行为）。
 */
function pickModelsDir({ env = process.env, homeDir, dOllamaPath, fsImpl = fs } = {}) {
  if (env.OLLAMA_MODELS) return env.OLLAMA_MODELS;
  const home = homeDir || env.USERPROFILE || '';
  const defaultDir = home ? path.join(home, '.ollama', 'models') : null;
  if (defaultDir && dirHasModels(defaultDir, fsImpl)) return null;  // 默认目录有货，不用干预
  if (dOllamaPath && dirHasModels(dOllamaPath, fsImpl)) return dOllamaPath;  // 后备：D 盘
  return null;
}

async function probe(url, fetchFn, timeoutMs = 2000) {
  try {
    const res = await fetchFn(`${url}/api/version`, { signal: AbortSignal.timeout(timeoutMs) });
    if (res.ok) {
      const j = await res.json().catch(() => ({}));
      return { running: true, version: j.version || '?' };
    }
    return { running: false, detail: `HTTP ${res.status}` };
  } catch (err) {
    return { running: false, detail: err && err.message ? err.message : String(err) };
  }
}

/**
 * 尽力而为：在跑就复用；没跑就带对模型目录拉起来。
 * 永不抛错（失败只是 {running:false}，UI 显示"模型离线"）。
 *
 * 返回 {running, started, child?, modelsDir?}
 */
async function ensureOllama({
  fetchFn = globalThis.fetch,
  spawnFn = spawn,
  env = process.env,
  fsImpl = fs,
  log = () => {},
  waitReadyMs = 0,
} = {}) {
  const alive = await probe(OLLAMA_URL, fetchFn);
  if (alive.running) return { running: true, started: false, version: alive.version };

  const exe = env.PAI_OLLAMA_EXE || 'ollama';
  const dPath = 'D:\\.ollama\\models';   // 本机模型后备位置（D011 实测）
  const modelsDir = pickModelsDir({
    env,
    homeDir: env.USERPROFILE || '',
    dOllamaPath: dPath,
    fsImpl,
  });

  let child;
  try {
    child = spawnFn(exe, ['serve'], {
      env: { ...(modelsDir ? { ...env, OLLAMA_MODELS: modelsDir } : env) },
      windowsHide: true,
      stdio: ['ignore', 'ignore', 'pipe'],
    });
  } catch (err) {
    log(`Ollama 拉起失败：${err.message}`);
    return { running: false, started: false, error: err.message };
  }
  child.stderr?.on('data', () => {});
  child.on('error', (err) => log(`Ollama 进程错误：${err.message}`));
  log(`已尝试拉起 Ollama serve（models=${modelsDir || '默认'}）`);

  // 可选：等它就绪（默认不等，Core 状态栏每 15s 自己会探）
  const deadline = Date.now() + waitReadyMs;
  while (waitReadyMs > 0 && Date.now() < deadline) {
    const h = await probe(OLLAMA_URL, fetchFn, 1000);
    if (h.running) return { running: true, started: true, child, modelsDir };
    await new Promise((r) => setTimeout(r, 300));
  }
  return { running: false, started: true, child, modelsDir };
}

function killOllama(child, log = () => {}) {
  if (!child) return false;
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
    log('已关闭本壳启动的 Ollama');
    return true;
  } catch (err) {
    log(`关闭 Ollama 失败：${err.message}`);
    return false;
  }
}

module.exports = { ensureOllama, killOllama, pickModelsDir, dirHasModels, OLLAMA_URL };
