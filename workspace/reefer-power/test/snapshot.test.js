'use strict';

// 验证快照 + 流水截断重放路径：强制每 3 批次快照，重启后跨快照归并一致
import { test } from 'node:test';
import { mkdtempSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';

const SERVER = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..', 'src', 'server.js');
let counter = 0;

function start(dir, extraEnv = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [SERVER, '--test-faults'], {
      env: { ...process.env, PORT: '0', REEFER_DATA_DIR: dir, ...extraEnv },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let buf = '';
    child.stdout.on('data', (d) => {
      buf += d;
      const m = buf.match(/http:\/\/localhost:(\d+)\//);
      if (m) resolve({ child, port: Number(m[1]) });
    });
    child.stderr.on('data', () => {});
    child.unref();
    setTimeout(() => reject(new Error('start timeout')), 5000);
  });
}
async function stop(proc) {
  if (!proc || proc.child.exitCode !== null) return;
  proc.child.kill('SIGTERM');
  await new Promise((r) => proc.child.once('exit', r));
}
async function call(proc, url, body, cid) {
  const res = await fetch(`http://localhost:${proc.port}${url}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', ...(cid ? { 'x-command-id': cid } : {}) },
    body: JSON.stringify(body),
  });
  return { status: res.status, json: await res.json() };
}
const j = (proc, url, body, cid) => call(proc, url, body, cid ?? `x${++counter}`);

test('快照周期重启：跨快照归并唯一 + 命令标识回放首次答复', async () => {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'reefer-snap-'));
  let proc = await start(dir, { REEFER_SNAPSHOT_EVERY: '3' });
  try {
    await j(proc, '/api/topology', {
      boards: [{ id: 'B1' }],
      circuits: [{ id: 'C1', boardId: 'B1', phase: '3P', ratedCurrentA: 63 }],
      sockets: [{ id: 'S1', circuitId: 'C1', allowedBoxTypes: ['T'], reachAreas: ['A'] }],
    }, 'topo-1');
    await j(proc, '/api/reefers', {
      reefers: [{ id: 'R1', boxType: 'T', phase: '3P', powerKw: 5, area: 'A', tempPriority: 3, insulationHours: 8 }],
    }, 'reef-1');
    const r1 = await j(proc, '/api/reservations', {}, 'resv-1');
    const socketId = r1.json.reserved[0].socketId;
    await j(proc, '/api/connect', { reeferId: 'R1', socketId }, 'conn-1');
    await j(proc, '/api/samples', {
      kind: 'current', socketId, sensorBootId: 'B1', measuredAt: Date.now(), value: 9.5,
    }, 'samp-1');
    // 此时已 5 批次，至少产生一次快照
  } finally {
    await stop(proc);
  }

  // 重启：从快照重放剩余批次
  proc = await start(dir, { REEFER_SNAPSHOT_EVERY: '3' });
  try {
    const st = await (await fetch(`http://localhost:${proc.port}/api/state`)).json();
    if (st.reefers[0].status !== 'POWERED') throw new Error('跨快照恢复后 R1 应为 POWERED');
    if (st.circuits[0].loadA !== 9.5) throw new Error(`跨快照实时负载应为 9.5A，实际 ${st.circuits[0].loadA}`);

    // 旧命令标识 + 相同载荷 -> 首次答复回放
    const replay = await j(proc, '/api/connect', { reeferId: 'R1', socketId: st.reefers[0].socketId }, 'conn-1');
    if (replay.status !== 200 || replay.json.replayed !== true) {
      throw new Error('命令标识重放失败: ' + JSON.stringify(replay.json));
    }

    // 旧命令标识 + 不同载荷 -> 冲突拒绝
    const bad = await j(proc, '/api/connect', { reeferId: 'R1', socketId: 'S1', bogus: 1 }, 'conn-1');
    if (bad.status !== 409 || bad.json.error.code !== 'IDEMPOTENCY_CONFLICT') {
      throw new Error('应返回 IDEMPOTENCY_CONFLICT');
    }
  } finally {
    await stop(proc);
  }
});
