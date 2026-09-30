'use strict';

// 测试工具：在临时数据目录上拉起真实 HTTP 进程，故障注入后重启验证归并
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SERVER = path.join(__dirname, '..', 'src', 'server.js');

let counter = 0;
export function newDataDir() {
  const dir = mkdtempSync(path.join(os.tmpdir(), `reefer-t${process.pid}-${counter++}-`));
  return dir;
}

export function startServer(dir, { port = 0, snapshotEvery } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(
      process.execPath,
      [SERVER, '--test-faults'],
      {
        env: {
          ...process.env,
          PORT: String(port),
          REEFER_DATA_DIR: dir,
          ...(snapshotEvery != null ? { REEFER_SNAPSHOT_EVERY: String(snapshotEvery) } : {}),
        },
        stdio: ['ignore', 'pipe', 'pipe'],
      }
    );
    let buf = '';
    child.stdout.on('data', (d) => {
      buf += d;
      const m = buf.match(/http:\/\/localhost:(\d+)\//);
      if (m) resolve({ child, port: Number(m[1]) });
    });
    child.stderr.on('data', () => {});
    child.on('error', reject);
    child.unref();
    setTimeout(() => reject(new Error('server start timeout')), 5000);
  });
}

export function stopServer(proc) {
  if (!proc || proc.child.killed || proc.child.exitCode !== null || proc.child.signalCode !== null) {
    return Promise.resolve();
  }
  proc.child.kill('SIGTERM');
  return new Promise((resolve) => {
    if (proc.child.exitCode !== null) return resolve();
    proc.child.once('exit', resolve);
    // 兜底：SIGTERM 不生效时强杀
    setTimeout(() => {
      try {
        proc.child.kill('SIGKILL');
      } catch {}
      resolve();
    }, 3000).unref();
  });
}

export async function restart(proc, dir, opts = {}) {
  await stopServer(proc);
  // 崩溃退出(os._exit)后原端口可能仍处于 TIME_WAIT，重启动用新随机端口
  return startServer(dir, opts);
}

let cmdSeq = 0;
export async function api(proc, method, url, body, { commandId, expectStatus, tolerateCrash } = {}) {
  const headers = { 'content-type': 'application/json' };
  const cid = commandId ?? `cmd-${++cmdSeq}-${Math.random().toString(36).slice(2, 8)}`;
  if (method === 'POST') headers['x-command-id'] = cid;

  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 4000);
  let res;
  try {
    res = await fetch(`http://localhost:${proc.port}${url}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: ctrl.signal,
    });
  } catch (e) {
    clearTimeout(timer);
    if (tolerateCrash) return { status: 0, json: { crashed: true }, commandId: cid };
    throw e;
  }
  clearTimeout(timer);
  const json = await res.json();
  if (expectStatus !== undefined && res.status !== expectStatus) {
    throw new Error(`期望 HTTP ${expectStatus}，实际 ${res.status}: ${JSON.stringify(json)}`);
  }
  return { status: res.status, json, commandId: cid };
}

export const post = (proc, url, body, opts = {}) => api(proc, 'POST', url, body, opts);
export const getState = (proc) => api(proc, 'GET', '/api/state');

export function assert(cond, msg) {
  if (!cond) throw new Error(`断言失败: ${msg}`);
}
export function assertEq(actual, expected, msg) {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`断言失败: ${msg}\n  期望: ${JSON.stringify(expected)}\n  实际: ${JSON.stringify(actual)}`);
  }
}

// 10kW 三相 / 8kW 三相 等常用数值（U=400V, cosφ=0.85）
export function topologySimple() {
  return {
    boards: [{ id: 'B1', name: '主配电板' }],
    circuits: [
      { id: 'C1', boardId: 'B1', name: '回路1', phase: '3P', ratedCurrentA: 63 },
      { id: 'C2', boardId: 'B1', name: '回路2', phase: '3P', ratedCurrentA: 63 },
    ],
    sockets: [
      { id: 'S1', circuitId: 'C1', allowedBoxTypes: ['A1'], reachAreas: ['BAY-A'] },
      { id: 'S2', circuitId: 'C1', allowedBoxTypes: ['A1'], reachAreas: ['BAY-A'] },
      { id: 'S3', circuitId: 'C2', allowedBoxTypes: ['A1'], reachAreas: ['BAY-A'] },
      { id: 'S4', circuitId: 'C2', phase: '1P', ratedCurrentA: 16, allowedBoxTypes: ['A1'], reachAreas: ['BAY-A'] },
    ],
  };
}
