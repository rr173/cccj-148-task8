'use strict';

import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Store } from './store.js';
import { DomainError, errBody, commandHash, payloadHash } from './util.js';
import { trippedImpact } from './planner.js';
import { now, advanceClock, clockOffset } from './clock.js';
import { buildPeakView } from './peak/views.js';
import {
  cmdTopology,
  cmdReefers,
  cmdReserve,
  cmdConnect,
  cmdSample,
  cmdTrip,
  cmdCircuitReset,
  cmdMigrationCreate,
  cmdMigrationStep,
  cmdMigrationCancel,
  cmdState,
} from './commands.js';
import {
  cmdBudgetRegister,
  cmdBudgetRevoke,
  cmdPeakPublish,
  cmdShavingExecute,
  cmdPeakRecompute,
  cmdPeakCancel,
  cmdRestore,
} from './peak/commands.js';
import {
  cmdPrecoolObserve,
  cmdCertIssue,
  cmdWaiverGrant,
  cmdGateScan,
  cmdPendingResolve,
  cmdSetpointAdjust,
} from './precool/commands.js';
import { buildPrecoolView, releaseDecision } from './precool/views.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WEB_DIR = path.join(__dirname, '..', 'web');

const ROUTES = {
  '/api/topology': { POST: cmdTopology },
  '/api/reefers': { POST: cmdReefers },
  '/api/reservations': { POST: cmdReserve },
  '/api/connect': { POST: cmdConnect },
  '/api/samples': { POST: cmdSample },
  '/api/circuits/trip': { POST: cmdTrip },
  '/api/circuits/reset': { POST: cmdCircuitReset },
  '/api/migrations': { POST: cmdMigrationCreate },
  '/api/migrations/steps': { POST: cmdMigrationStep },
  '/api/migrations/cancel': { POST: cmdMigrationCancel },
  // —— 分时功率预算与冷藏负荷削峰 ——
  '/api/budgets': { POST: cmdBudgetRegister },
  '/api/budgets/revoke': { POST: cmdBudgetRevoke },
  '/api/peak/plans': { POST: cmdPeakPublish },
  '/api/peak/actions/execute': { POST: cmdShavingExecute },
  '/api/peak/recompute': { POST: cmdPeakRecompute },
  '/api/peak/cancel': { POST: cmdPeakCancel },
  '/api/peak/restore': { POST: cmdRestore },
  // —— 装船前预冷合格证与放行互锁 ——
  '/api/precool/observe': { POST: cmdPrecoolObserve },
  '/api/precool/certificates': { POST: cmdCertIssue },
  '/api/precool/waivers': { POST: cmdWaiverGrant },
  '/api/precool/gate-scan': { POST: cmdGateScan },
  '/api/precool/pending/resolve': { POST: cmdPendingResolve },
  '/api/precool/setpoint': { POST: cmdSetpointAdjust },
};

export function createApp({ dir, testFaults = false } = {}) {
  const store = new Store(dir || path.join(__dirname, '..', 'data'));
  let chain = Promise.resolve();

  function readBody(req) {
    return new Promise((resolve, reject) => {
      const chunks = [];
      let size = 0;
      req.on('data', (c) => {
        size += c.length;
        if (size > 1_048_576) {
          reject(new DomainError('BAD_REQUEST', '请求体超过 1MB'));
          req.destroy();
          return;
        }
        chunks.push(c);
      });
      req.on('end', () => {
        if (!chunks.length) return resolve({});
        try {
          resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')));
        } catch {
          reject(new DomainError('BAD_REQUEST', '请求体不是合法 JSON'));
        }
      });
      req.on('error', reject);
    });
  }

  function send(res, status, payload) {
    const buf = Buffer.from(JSON.stringify(payload));
    res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'content-length': buf.length });
    res.end(buf);
  }

  async function handleApi(req, res) {
    const url = new URL(req.url, 'http://localhost');
    if (req.method === 'GET' && url.pathname === '/api/state') {
      const at = now();
      const base = cmdState(store.state, {}, at).reply;
      return send(res, 200, {
        ok: true,
        ...base,
        peak: buildPeakView(store.state, at),
        precool: buildPrecoolView(store.state, at),
      });
    }

    // 放行判定查询（只读）：装船确认/岸桥可先查当前合格证
    if (req.method === 'GET' && url.pathname === '/api/precool/release') {
      const voyageId = url.searchParams.get('voyageId');
      const reeferId = url.searchParams.get('reeferId');
      if (!voyageId || !reeferId) {
        return send(res, 400, errBody(new DomainError('BAD_REQUEST', '必须提供 voyageId 与 reeferId')));
      }
      const at = now();
      return send(res, 200, { ok: true, at, voyageId, reeferId, ...releaseDecision(store.state, voyageId, reeferId, at) });
    }
    if (req.method === 'GET' && url.pathname === '/api/precool/board') {
      const at = now();
      return send(res, 200, { ok: true, ...buildPrecoolView(store.state, at) });
    }

    const route = ROUTES[url.pathname];
    if (!route || req.method !== 'POST') {
      return send(res, 404, errBody(new DomainError('NOT_FOUND', `无此接口: ${req.method} ${url.pathname}`)));
    }

    let body;
    try {
      body = await readBody(req);
    } catch (e) {
      return send(res, 400, errBody(e));
    }

    let commandId = (req.headers['x-command-id'] || body.commandId || null)?.toString() || null;
    // 预冷写接口的 operationId 同样采用首次结果语义，且优先于自动命令标识头：
    // 同 operationId 回放首次结果（含首次失败），同键不同载荷 -> IDEMPOTENCY_CONFLICT。
    if (url.pathname.startsWith('/api/precool/') && body.operationId) {
      commandId = `op:${String(body.operationId)}`;
    }
    const isSample = url.pathname === '/api/samples';
    // 样本按纯内容摘要去重（同传感器/测量时刻/值即首次答复）；其余命令按 (命令标识,摘要) 去重。
    // 摘要必须带路径名命名空间，否则不同接口上同构载荷（如 observe 与 issue）会互相误命中回放。
    const crypto = (await import('node:crypto')).createHash;
    const hash = isSample
      ? crypto('sha256').update(`${url.pathname}\0${payloadHash(body)}`).digest('hex')
      : crypto('sha256').update(`${url.pathname}\0${commandId ?? '__nocmd__'}\0${payloadHash(body)}`).digest('hex');

    // 查、执行、提交必须串行，保证两名电气员并发提交时先到者成功
    const keyMode = isSample ? 'content' : 'cmd';
    const job = chain.then(() => execute(route.POST, body, commandId, hash, keyMode));
    chain = job.catch(() => {});
    try {
      const { status, reply } = await job;
      return send(res, status, { ok: status < 400, ...reply });
    } catch (e) {
      const status = e instanceof DomainError ? (e.code === 'NOT_FOUND' ? 404 : e.code === 'CONFLICT' ? 409 : 400) : 500;
      if (status === 500) console.error(e);
      return send(res, status, errBody(e));
    }
  }

  function execute(handler, body, commandId, hash, keyMode) {
    const lookup = store.lookup(commandId, hash, keyMode);
    if (lookup.kind === 'replay') {
      const r = lookup.receipt;
      return Promise.resolve({ status: r.status, reply: { replayed: true, commandId: r.commandId, ...r.reply } });
    }
    if (lookup.kind === 'payload-conflict') {
      const stored = store.receipts.get(store.commandIndex.get(commandId));
      const e = new DomainError(
        'CONFLICT',
        `命令标识 ${commandId} 已用于不同载荷摘要，拒绝改写当前接电状态`,
        { code: 'IDEMPOTENCY_CONFLICT', originalSeq: stored?.seq, originalReply: stored?.reply }
      );
      return Promise.reject(e);
    }

    let result;
    try {
      result = handler(store.state, body, now());
    } catch (e) {
      // 业务拒绝同样按命令标识留痕，重复提交回放首次拒绝。
      // 预冷命令在“拒绝前”可能已产生自动失效事实（如本次读数过期使旧证书失效）：
      // 这些事实必须随拒绝原子落盘——失效是已经发生的事实，不能因为签发被拒而消失。
      if (!(e instanceof DomainError)) throw e;
      const reply = errBody(e);
      const status = e.code === 'NOT_FOUND' ? 404 : e.code === 'CONFLICT' ? 409 : 400;
      const facts = Array.isArray(e.extra?.facts) ? e.extra.facts : [];
      if (facts.length) {
        const { reply: durableReply, status: durableStatus } = store.commit({ commandId, hash, facts, reply, keyMode, status });
        return Promise.resolve({ status: durableStatus, reply: durableReply });
      }
      store.commit({ commandId, hash, facts: [], reply, keyMode, status });
      return Promise.resolve({ status, reply });
    }

    const { status: outStatus, reply: outReply } = store.commit({
      commandId,
      hash,
      facts: result.facts,
      reply: result.reply,
      enrich: result.enrich,
      keyMode,
      status: 200,
    });
    return Promise.resolve({ status: outStatus, reply: outReply });
  }

  function handleFaults(req, res, body) {
    if (!testFaults) return send(res, 404, errBody(new DomainError('NOT_FOUND', '故障注入未启用')));
    if (req.method === 'POST' && req.url === '/test/faults/arm') {
      store.faultArm = { factType: String(body.factType), at: Date.now() };
      return send(res, 200, { ok: true, armed: store.faultArm });
    }
    if (req.method === 'POST' && req.url === '/test/faults/clear') {
      store.faultArm = null;
      return send(res, 200, { ok: true });
    }
    if (req.method === 'GET' && req.url === '/test/faults') {
      return send(res, 200, { ok: true, armed: store.faultArm });
    }
    // 测试虚拟时钟：推进量作为 clockAdvanced 事实落盘，重启后仍一致
    if (req.method === 'POST' && req.url === '/test/clock/advance') {
      const ms = Number(body.ms ?? 0);
      const offsetMs = advanceClock(ms);
      store.commit({
        commandId: null,
        hash: `clock@${offsetMs}`,
        facts: [{ type: 'clockAdvanced', at: now(), ms, offsetMs }],
        reply: { now: now(), offsetMs },
        status: 200,
      });
      return send(res, 200, { ok: true, now: now(), offsetMs });
    }
    if (req.method === 'GET' && req.url === '/test/clock') {
      return send(res, 200, { ok: true, now: now(), wallNow: Date.now(), offsetMs: clockOffset() });
    }
    return send(res, 404, errBody(new DomainError('NOT_FOUND', 'no route')));
  }

  const MIME = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
  };

  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://localhost');
    try {
      if (url.pathname.startsWith('/api/')) return await handleApi(req, res);
      if (url.pathname.startsWith('/test/')) {
        const body = req.method === 'POST' ? await readBody(req) : {};
        return handleFaults(req, res, body);
      }
      // 静态操作台
      let rel = url.pathname === '/' ? '/index.html' : url.pathname;
      const file = path.normalize(path.join(WEB_DIR, rel));
      if (!file.startsWith(WEB_DIR)) {
        res.writeHead(403);
        return res.end('forbidden');
      }
      const data = fs.readFileSync(file);
      res.writeHead(200, { 'content-type': MIME[path.extname(file)] || 'application/octet-stream' });
      res.end(data);
    } catch (e) {
      if (e.code === 'ENOENT') return send(res, 404, errBody(new DomainError('NOT_FOUND', '文件不存在')));
      console.error(e);
      send(res, 500, errBody(new DomainError('INTERNAL', e.message)));
    }
  });

  return { server, store };
}

export function start({ port = Number(process.env.PORT ?? 8080), host = '0.0.0.0', dir, testFaults } = {}) {
  const { server, store } = createApp({ dir, testFaults });
  return new Promise((resolve) => {
    server.listen(port, host, () => resolve({ server, store, port: server.address().port }));
  });
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const testFaults = process.env.REEFER_TEST_FAULTS === '1' || process.argv.includes('--test-faults');
  const dir = process.env.REEFER_DATA_DIR || undefined;
  start({ dir, testFaults }).then(({ port }) => {
    console.log(`冷藏箱岸电模块已启动: http://localhost:${port}/  (操作台)`);
    console.log(`HTTP 接口前缀: /api/...   故障注入: ${testFaults ? '启用' : '关闭'}`);
  });
}
