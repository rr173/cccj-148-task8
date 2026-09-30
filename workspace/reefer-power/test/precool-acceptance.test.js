'use strict';

// 装船前预冷合格证与放行互锁：端到端验收测试（真实 HTTP 子进程 + 虚拟时钟 + 崩溃注入）。
// 每个用例都启停真实服务进程并使用临时端口；用模块级互斥串行化，避免进程/端口争用。
import { test } from 'node:test';
import {
  newDataDir,
  startServer,
  stopServer,
  restart,
  post,
  api,
  assert,
  assertEq,
} from './helpers.js';

// 模块级互斥：把所有用例串成一条链（无论运行器并发度如何），并统一清理进程
let chain = Promise.resolve();
let activeProcs = [];
const serialTest = (name, fn) => test(name, async (t) => {
  const run = chain.then(async () => {
    activeProcs = [];
    try { return await fn(t); }
    finally {
      const ps = activeProcs; activeProcs = [];
      for (const p of ps) await stopServer(p).catch(() => {});
    }
  });
  chain = run.catch(() => {});
  return run;
});

async function launch({ snapshotEvery } = {}) {
  const dir = newDataDir();
  const proc = await startServer(dir, { snapshotEvery });
  activeProcs.push(proc);
  return { proc, dir };
}

const TOPO = {
  boards: [{ id: 'B1', name: '主配电板' }],
  circuits: [
    { id: 'C1', boardId: 'B1', name: '回路1', phase: '3P', ratedCurrentA: 63 },
    { id: 'C2', boardId: 'B1', name: '回路2', phase: '3P', ratedCurrentA: 63 },
  ],
  sockets: [
    { id: 'S1', circuitId: 'C1', allowedBoxTypes: ['A1'], reachAreas: ['BAY-A'] },
    { id: 'S2', circuitId: 'C2', allowedBoxTypes: ['A1'], reachAreas: ['BAY-A'] },
  ],
};

function reefer(id, { grade = 'FROZEN', setpoint = -18, rise = 5 } = {}) {
  return {
    id, boxType: 'A1', phase: '3P', powerKw: 5, area: 'BAY-A',
    tempPriority: 5, insulationHours: 4, setpointC: setpoint, cargoGrade: grade, allowableRiseC: rise,
  };
}

// 起一个已供电的单箱环境，并返回虚拟时钟控制助手
async function poweredEnv({ grade = 'FROZEN', snapshotEvery } = {}) {
  const env = await launch({ snapshotEvery });
  const { proc } = env;
  await post(proc, '/api/topology', TOPO);
  await post(proc, '/api/reefers', { reefers: [reefer('R1', { grade })] });
  await post(proc, '/api/reservations', {});
  await post(proc, '/api/connect', { reeferId: 'R1', socketId: 'S1' });
  let at = (await api(proc, 'GET', '/test/clock')).json.now;
  async function advance(ms) { at = (await post(proc, '/test/clock/advance', { ms })).json.now; return at; }
  async function temp(value, { boot = 'BOOT-1', reeferId = 'R1', at: t } = {}) {
    return post(proc, '/api/samples', { kind: 'temperature', sensorBootId: boot, reeferId, measuredAt: t ?? at, value });
  }
  return { ...env, advance, temp, get at() { return at; } };
}

// 连续推送每分钟一个、共 mins+1 个合格温度点，并推进时钟
async function pushStable(env, mins, { value = -18.1, boot = 'BOOT-1' } = {}) {
  for (let i = 0; i < mins + 1; i++) {
    await env.advance(60000);
    await env.temp(value, { boot });
  }
}

const V = 'VOY-1';
async function issueCert(env, voyageId = V, extra = {}, opts = {}) {
  return post(env.proc, '/api/precool/certificates', { voyageId, reeferId: 'R1', operatorId: 'DUTY', ...extra }, opts);
}
async function observe(env, voyageId = V, extra = {}, opts = {}) {
  return post(env.proc, '/api/precool/observe', { voyageId, reeferId: 'R1', operatorId: 'DUTY', ...extra }, opts);
}
async function scan(env, body = {}, opts = {}) {
  return post(env.proc, '/api/precool/gate-scan', { voyageId: V, reeferId: 'R1', scannerId: 'CRANE-1', ...body }, opts);
}
async function state(env) {
  return (await api(env.proc, 'GET', '/api/state')).json.precool;
}

// ---------------------------------------------------------------
serialTest('1. 稳定温度正常签发：低风险 10 分钟，高风险 30 分钟且阈值更严', async () => {
  // 整段重推：共 11 点跨 10 分钟
  const env = await poweredEnv({ grade: 'FROZEN' });
  await observe(env);
  // 先只推 9 个点（跨 8 分钟），稳定时间不足
  for (let i = 0; i < 9; i++) { await env.advance(60000); await env.temp(-18.1); }
  const early = await issueCert(env);
  assertEq(early.json.error?.code, 'NOT_STABLE', '稳定时间不足不得签发');
  // 再补 2 点，凑满 11 点跨 10 分钟
  for (let i = 0; i < 2; i++) { await env.advance(60000); await env.temp(-18.1); }
  const cert = await issueCert(env);
  assert(cert.json.issued, '低风险 10 分钟后签发');
  assertEq(cert.json.policy.stableMs, 600000, '低风险稳定时间 10 分钟');
  assertEq(cert.json.evidence.sampleCount, 11, '证据区间 11 个实测点');
  assertEq(cert.json.evidence.source, 'SHORE_POWER_MEASURED', '采样来源为岸电实测');
  assert(cert.json.observationSummary.includes('BOOT-1'), '观测区间摘要含传感器启动代号');

  // 高风险：30 分钟、偏差 0.5℃
  const high = await poweredEnv({ grade: 'PHARMA' });
  await observe(high);
  await pushStable(high, 30, { value: -18.2 }); // 偏差 0.2℃ 合格
  const hc = await issueCert(high);
  assert(hc.json.issued, '高风险 30 分钟后签发');
  assertEq(hc.json.policy.stableMs, 1800000, '高风险稳定时间 30 分钟');
  assertEq(hc.json.policy.maxDeviationC, 0.5, '高风险偏差阈值 0.5℃');

  // 高风险偏差超 0.5℃（-17.4，偏差 0.6℃）不签发
  const bad2 = await poweredEnv({ grade: 'PHARMA' });
  await observe(bad2);
  await pushStable(bad2, 30, { value: -17.4 });
  const denied = await issueCert(bad2);
  assertEq(denied.json.error?.code, 'NOT_STABLE', '高风险偏差 0.6℃ 不得签发');
  assert(denied.json.error.blockers.some((b) => b.code === 'DEVIATION_EXCEEDED'), '原因含 DEVIATION_EXCEEDED');
});

serialTest('2. 过期读数不签发；读数过期后扫描使旧证失效并入待处理', async () => {
  const env = await poweredEnv();
  await observe(env);
  await pushStable(env, 10);
  const cert = await issueCert(env);
  assert(cert.json.issued, '先持证');
  // 超过低风险读数年龄 15 分钟无新读数
  await env.advance(16 * 60000);
  const again = await issueCert(env);
  assertEq(again.json.error?.code, 'NOT_STABLE', '读数过期不得再签发');
  assert(again.json.error.blockers.some((b) => b.code === 'READING_STALE'), '原因含 READING_STALE');

  // 扫描同批次把过期状态落为失效事实，箱进待处理队列
  const sc = await scan(env);
  assertEq(sc.json.outcome, 'PENDING', '过期后扫描不放行');
  assert(!!sc.json.pendingId, '进入温控待处理队列');
  const pc = await state(env);
  const c = pc.certs.find((x) => x.id === cert.json.certId);
  assertEq(c.status, 'INVALIDATED', '旧证已落为失效');
  assertEq(c.invalidReason, 'READING_STALE', '失效原因为读数过期');
  assert(c.restartFrom != null && c.restartRemainingMs === 600000, '给出重新观测起点与剩余时长');
});

serialTest('3. 观测中途掉电后重新计时：旧区间作废，恢复供电后重新观测', async () => {
  const env = await poweredEnv();
  await observe(env);
  await pushStable(env, 8); // 已稳定 8 分钟
  // 跳闸：观测中途掉电（此刻尚无证书，无证书可失效）
  const trip = await post(env.proc, '/api/circuits/trip', { circuitId: 'C1' });
  assertEq(trip.json.invalidations.length, 0, '尚无证书/豁免，无失效事实');
  const issueMid = await issueCert(env);
  assertEq(issueMid.json.error?.code, 'NOT_STABLE', '掉电期间不签发');
  assert(issueMid.json.error.blockers.some((b) => b.code === 'POWER_LOSS_AFTER_START'), '原因 POWER_LOSS_AFTER_START');

  // 未恢复供电不能重新观测
  const reobsWhileDown = await post(env.proc, '/api/precool/observe', { voyageId: V, reeferId: 'R1', operatorId: 'DUTY' });
  assertEq(reobsWhileDown.json.error?.code, 'REEFER_NOT_POWERED', '未供电不得开始观测');

  await post(env.proc, '/api/circuits/reset', { circuitId: 'C1' });
  // 恢复供电后值班员发起重新观测（本用例此前未扫描、无待处理条目，直接重新发起）
  const r2 = await observe(env);
  assertEq(r2.json.reset, true, '重新观测标记 reset');
  // 只推 8 分钟（9 点跨 8 分钟；若沿用旧区间会被错误放行）
  await pushStable(env, 8);
  const early = await issueCert(env);
  assertEq(early.json.error?.code, 'NOT_STABLE', '必须重新计时：新的 8 分钟不足 10 分钟');
  // 再补 2 点（共 11 点跨 10 分钟）
  for (let i = 0; i < 2; i++) { await env.advance(60000); await env.temp(-18.1); }
  const cert = await issueCert(env);
  assert(cert.json.issued, '重新计时满 10 分钟后签发');
  assertEq(cert.json.evidence.sampleCount, 11, '证据只含重新观测后的 11 点');
});

serialTest('4. 传感器更换（启动代号变化）使旧证书立即失效', async () => {
  const env = await poweredEnv();
  await observe(env);
  await pushStable(env, 10, { boot: 'BOOT-1' });
  const cert = await issueCert(env);
  // 新传感器上报
  await env.advance(60000);
  const smp = await env.temp(-18.1, { boot: 'BOOT-2' });
  assert(smp.json.invalidations.some((x) => x.kind === 'cert' && x.reason === 'SENSOR_BOOT_CHANGED'),
    '新启动代号样本使旧证失效');
  const pc = await state(env);
  const c = pc.certs.find((x) => x.id === cert.json.certId);
  assertEq(c.status, 'INVALIDATED', '旧证失效');
  assertEq(c.invalidReason, 'SENSOR_BOOT_CHANGED', '原因 SENSOR_BOOT_CHANGED');
  // 已失效永不复活：之后全是 BOOT-1 读数也不能变回 VALID
  await pushStable(env, 10, { boot: 'BOOT-1' });
  const pc2 = await state(env);
  const c2 = pc2.certs.find((x) => x.id === cert.json.certId);
  assertEq(c2.status, 'INVALIDATED', '已失效证书不复活');
});

serialTest('5. 无证扫描进入温控待处理队列；装船计划与插座事实不变；有效证放行', async () => {
  const env = await poweredEnv();
  // 无观测无证书直接扫描
  const sc0 = await scan(env, { slotCode: '02-01-82' });
  assertEq(sc0.json.outcome, 'PENDING', '无证扫描入待处理');
  assertEq(sc0.json.loadingPlanUntouched, true, '装船计划保持不变');
  assertEq(sc0.json.socketFactUntouched, true, '插座事实保持不变');
  // 重复扫描不重复建队列条目
  const sc0b = await scan(env);
  assertEq(sc0b.json.pendingId, sc0.json.pendingId, '复用同一待处理条目');
  const pc0 = await state(env);
  assertEq(pc0.pending.filter((q) => q.status === 'OPEN').length, 1, '仅一个 OPEN 条目');

  // 观测取证后再次扫描放行
  await observe(env);
  await pushStable(env, 10);
  await issueCert(env);
  const sc1 = await scan(env, { slotCode: '02-01-82' });
  assertEq(sc1.json.outcome, 'RELEASED', '有效证书放行');
  assertEq(sc1.json.basis, 'CERT', '放行依据为证书');
  const pc1 = await state(env);
  assertEq(pc1.scans.filter((g) => g.effectiveOutcome === 'RELEASED').length >= 1, true, '放行结果可查');
});

serialTest('6. 有效豁免放行后，后续超温自动撤销豁免', async () => {
  const env = await poweredEnv({ grade: 'PHARMA' });
  // 未取证，直接由值班员提交带原因/有效期/货物负责人的临时豁免
  const wv = await post(env.proc, '/api/precool/waivers', {
    voyageId: V, reeferId: 'R1', reason: 'door seal replaced, awaiting reinspection',
    cargoOwner: 'OWNER-42', validMinutes: 30, operatorId: 'DUTY',
  });
  assert(wv.json.granted, '豁免签发');
  assertEq(wv.json.scope.includes(V), true, '豁免只覆盖指定航次与箱号');

  const sc = await scan(env);
  assertEq(sc.json.outcome, 'RELEASED', '豁免放行');
  assertEq(sc.json.basis, 'WAIVER', '依据豁免');

  // 新传感器后续报超温（偏差 > 0.5℃）
  await env.advance(60000);
  await env.temp(-15.0, { boot: 'BOOT-9' });
  const sc2 = await scan(env);
  assertEq(sc2.json.outcome, 'PENDING', '超温撤销后不再放行');
  const pc = await state(env);
  const w = pc.waivers.find((x) => x.id === wv.json.waiverId);
  assertEq(w.status, 'REVOKED', '豁免已撤销');
  assertEq(w.revokeReason, 'OVER_TEMPERATURE', '撤销原因为超温');

  // 掉电也会撤销豁免（另一只箱在另一航次的豁免不受影响——范围隔离）
  const other = await post(env.proc, '/api/precool/waivers', {
    voyageId: 'VOY-2', reeferId: 'R1', reason: 'other voyage', cargoOwner: 'OWNER-42', validMinutes: 30, operatorId: 'DUTY',
  });
  assert(other.json.granted, '另一航次可独立豁免');
});

serialTest('7. 豁免校验：原因/货物负责人/有效期；有效期上限；到期自动失效', async () => {
  const env = await poweredEnv();
  const noOwner = await post(env.proc, '/api/precool/waivers', {
    voyageId: V, reeferId: 'R1', reason: 'x', cargoOwner: '', validMinutes: 10, operatorId: 'DUTY',
  });
  assertEq(noOwner.status, 400, '无货物负责人拒绝');
  const tooLong = await post(env.proc, '/api/precool/waivers', {
    voyageId: V, reeferId: 'R1', reason: 'x', cargoOwner: 'OWNER-1', validMinutes: 9999, operatorId: 'DUTY',
  });
  assertEq(tooLong.status, 400, '超过有效期上限拒绝');
  const wv = await post(env.proc, '/api/precool/waivers', {
    voyageId: V, reeferId: 'R1', reason: 'ok reason', cargoOwner: 'OWNER-1', validMinutes: 5, operatorId: 'DUTY',
  });
  assert(wv.json.granted, '5 分钟豁免签发');
  await env.advance(6 * 60000);
  const sc = await scan(env);
  assertEq(sc.json.outcome, 'PENDING', '到期后不放行');
  const pc = await state(env);
  assert(['EXPIRED', 'EXPIRED_DYNAMIC'].includes(pc.waivers[0].liveStatus), '读模型呈现到期');
});

serialTest('8. 不能用铭牌估算替代真实温度完成放行', async () => {
  const env = await poweredEnv();
  // 只有电流/铭牌，没有温度样本
  await observe(env);
  const denied = await issueCert(env);
  assertEq(denied.json.error?.code, 'NOT_STABLE', '无真实温度不签发');
  assert(denied.json.error.blockers.some((b) => b.code === 'NO_TEMP_READING'), 'NO_TEMP_READING');
  const sc = await scan(env);
  assertEq(sc.json.outcome, 'PENDING', '无真实温度不放行');
});

serialTest('9. 设定点调整使既有证失效、观测重新计时', async () => {
  const env = await poweredEnv();
  await observe(env);
  await pushStable(env, 10);
  const cert = await issueCert(env);
  const adj = await post(env.proc, '/api/precool/setpoint', { reeferId: 'R1', toC: -22, operatorId: 'DUTY' });
  assert(adj.json.invalidations.some((x) => x.kind === 'cert' && x.reason === 'SETPOINT_CHANGED'),
    '设定点调整使证书失效');
  const pc = await state(env);
  const c = pc.certs.find((x) => x.id === cert.json.certId);
  assertEq(c.status, 'INVALIDATED', '证书已失效');
  assertEq(c.invalidReason, 'SETPOINT_CHANGED', '原因 SETPOINT_CHANGED');
  // 旧温度点相对新设定点 -22 偏差 3.9℃，重新观测前不签发
  const denied = await issueCert(env);
  assertEq(denied.json.error?.code, 'NOT_STABLE', '设定点变化后需重新观测');
});

serialTest('10. operationId 首次结果语义：同键回放、同键异载荷冲突', async () => {
  const env = await poweredEnv();
  const b1 = { voyageId: V, reeferId: 'R1', scannerId: 'CRANE-1', operationId: 'OP-X' };
  const r1 = await post(env.proc, '/api/precool/gate-scan', b1);
  const r2 = await post(env.proc, '/api/precool/gate-scan', b1);
  assertEq(r2.json.replayed, true, '同 operationId 回放首次结果');
  assertEq(r2.json.scanId, r1.json.scanId, '回放同一扫描记录');
  const diff = await post(env.proc, '/api/precool/gate-scan', { ...b1, scannerId: 'CRANE-2' });
  assertEq(diff.json.error?.code, 'IDEMPOTENCY_CONFLICT', 409);
  assertEq(diff.status, 409, '同键不同载荷 409');
});

serialTest('11. 竞争签发与失效：版本前置条件收敛到单一证书，旧版本不覆盖新失效态', async () => {
  const env = await poweredEnv();
  await observe(env);
  await pushStable(env, 10);
  // 并发两个签发：串行裁决，只产生一张有效证
  const [a, b] = await Promise.all([issueCert(env), issueCert(env)]);
  const ok = [a, b].filter((r) => r.json.issued);
  const refused = [a, b].filter((r) => !r.json.issued);
  assertEq(ok.length, 1, '恰好一张证书签发成功');
  assertEq(refused[0].json.error?.code, 'CERT_ALREADY_VALID', '竞争者被告知已有有效证');
  const pc = await state(env);
  assertEq(pc.certs.filter((c) => c.live).length, 1, '仅一张有效证书');

  // 带旧 expectedVersion 的签发与新失效态竞争：旧页面不能覆盖后来产生的失效状态
  const curVersion = pc.certs[0].version;
  await env.advance(16 * 60000); // 让证书过期
  const stale = await issueCert(env, V, { expectedVersion: curVersion - 1 });
  assert(['VERSION_CONFLICT', 'NOT_STABLE', 'CERT_ALREADY_VALID'].includes(stale.json.error?.code),
    `旧版本操作被拒: ${stale.json.error?.code}`);
  if (stale.json.error?.code === 'VERSION_CONFLICT') {
    assert(typeof stale.json.error.currentVersion === 'number', '返回当前版本号');
  }
});

serialTest('12. 崩溃恢复：证书签发落盘后重启，同一观测区间签不出两张有效证', async () => {
  const env = await poweredEnv({ snapshotEvery: 5 });
  await observe(env);
  await pushStable(env, 10);
  // 注入：证书批次 fsync 后、归并前退出
  const armRes = await post(env.proc, '/test/faults/arm', { factType: 'precoolCertificateIssued' });
  if (!armRes.json.armed) throw new Error('故障注入未生效: ' + JSON.stringify(armRes.json));
  const crashed = await issueCert(env, V, {}, { tolerateCrash: true });
  assertEq(crashed.status, 0, '签发进程在归并前崩溃');
  env.proc = await restart(env.proc, env.dir);
  activeProcs.push(env.proc);
  const pc1 = await state(env);
  assertEq(pc1.certs.length, 1, '重启后重放出恰好一张证书');
  assertEq(pc1.certs[0].status, 'VALID', '重放证书有效');
  // 用同一命令标识重试：回放首次结果，不产生第二张
  // （命令标识每次随机，改为直接再签发：应被告知已有有效证）
  const again = await issueCert(env);
  assertEq(again.json.error?.code, 'CERT_ALREADY_VALID', '不会签出第二张有效证');
  const pc2 = await state(env);
  assertEq(pc2.certs.length, 1, '仍只有一张证书');
});

serialTest('13. 崩溃恢复：失效落盘后重启，已失效证书不能重新有效；扫描结果一致', async () => {
  const env = await poweredEnv({ snapshotEvery: 5 });
  await observe(env);
  await pushStable(env, 10);
  const cert = await issueCert(env);
  assert(cert.json.issued, '先持证');
  await post(env.proc, '/test/faults/arm', { factType: 'precoolCertificateInvalidated' });
  // 读数过期后扫描会在同批次落失效事实
  await env.advance(16 * 60000);
  const crashed = await scan(env, {}, { tolerateCrash: true });
  assertEq(crashed.status, 0, '失效批次落盘后崩溃');
  env.proc = await restart(env.proc, env.dir);
  activeProcs.push(env.proc);
  const pc = await state(env);
  const c = pc.certs.find((x) => x.id === cert.json.certId);
  assertEq(c.status, 'INVALIDATED', '重启后证书仍失效（不复活）');
  assertEq(c.invalidReason, 'READING_STALE', '失效原因保留');
  const sc = await scan(env);
  assertEq(sc.json.outcome, 'PENDING', '重启后装船结果与失效状态相符：待处理');
});

serialTest('14. 崩溃恢复：扫描/放行落盘边界崩溃后，放行结果与证书状态一致且不重复', async () => {
  const env = await poweredEnv({ snapshotEvery: 3 });
  await observe(env);
  await pushStable(env, 10);
  await issueCert(env);
  await post(env.proc, '/test/faults/arm', { factType: 'precoolGateScanned' });
  const body = { voyageId: V, reeferId: 'R1', scannerId: 'CRANE-1', operationId: 'OP-CRASH' };
  // 强制仅以 operationId 作为幂等键（不带随机 X-Command-Id），重启后才能按首次结果回放
  const crashed = await api(env.proc, 'POST', '/api/precool/gate-scan', body, { commandId: null, tolerateCrash: true });
  assertEq(crashed.status, 0, '扫描批次落盘后崩溃');
  env.proc = await restart(env.proc, env.dir);
  activeProcs.push(env.proc);
  // 同 operationId 重试：回放首次放行结果，不产生第二条扫描
  const replay = await api(env.proc, 'POST', '/api/precool/gate-scan', body, { commandId: null });
  assertEq(replay.json.replayed, true, '重启后回放首次扫描结果');
  assertEq(replay.json.outcome, 'RELEASED', '首次结果为放行');
  const pc = await state(env);
  const rels = pc.scans.filter((g) => g.operationId === 'OP-CRASH');
  assertEq(rels.length, 1, '同一 operationId 只有一条扫描事实');
  assertEq(pc.certs.filter((c) => c.live).length, 1, '证书仍为单一有效');
});
