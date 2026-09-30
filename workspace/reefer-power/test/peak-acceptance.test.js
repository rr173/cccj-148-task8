'use strict';

import { test } from 'node:test';
import {
  newDataDir,
  startServer,
  stopServer,
  restart,
  post,
  getState,
  assert,
  assertEq,
} from './helpers.js';

// 分时功率预算 + 冷藏负荷削峰 的验收测试。
// 每个用例使用独立数据目录与独立进程，互不影响。

async function advance(proc, ms) {
  const r = await fetch(`http://localhost:${proc.port}/test/clock/advance`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ ms }),
  });
  return r.json();
}
async function clock(proc) {
  return (await (await fetch(`http://localhost:${proc.port}/test/clock`)).json()).now;
}

// 建 2 块配电板（各 1 条回路），并按 reefers 描述接电、写入新鲜电流。
async function seed(proc, reefers, { boards = 2 } = {}) {
  await post(proc, '/api/topology', {
    boards: [
      { id: 'B1', name: '板1' },
      ...(boards >= 2 ? [{ id: 'B2', name: '板2' }] : []),
    ],
    circuits: [
      { id: 'C1', boardId: 'B1', phase: '3P', ratedCurrentA: 400, name: '回路1' },
      ...(boards >= 2 ? [{ id: 'C2', boardId: 'B2', phase: '3P', ratedCurrentA: 400, name: '回路2' }] : []),
    ],
    sockets: reefers.map((r, i) => ({
      id: `S${i + 1}`,
      circuitId: r.board === 2 ? 'C2' : 'C1',
      allowedBoxTypes: ['A1'],
      reachAreas: ['Z'],
    })),
  });
  await post(proc, '/api/reefers', {
    reefers: reefers.map((r) => ({
      id: r.id, boxType: 'A1', phase: '3P', ratedCurrentA: r.a, area: 'Z',
      cargoGrade: r.grade ?? 'FROZEN', setpointC: r.set ?? -18,
      allowableRiseC: r.rise ?? 8, insulationHours: r.ins ?? 6,
      tempPriority: r.prio ?? 3,
    })),
  });
  const res = await post(proc, '/api/reservations', {});
  const sockOf = Object.fromEntries(res.json.reserved.map((x) => [x.reeferId, x.socketId]));
  for (const r of reefers) {
    await post(proc, '/api/connect', { reeferId: r.id, socketId: sockOf[r.id] });
  }
  const now = await clock(proc);
  for (const r of reefers) {
    await post(proc, '/api/samples', {
      kind: 'current', socketId: sockOf[r.id], sensorBootId: `B-${r.id}`,
      measuredAt: now - 2000, value: r.a,
    });
  }
  return { sockOf, now };
}

async function registerBudget(proc, { startAt, endAt, shipLimitA, boardLimitsA, operatorId = 'OPS' }) {
  return post(proc, '/api/budgets', {
    operatorId, startAt, endAt, shipLimitA, boardLimitsA,
  });
}

function assertActionsOnePerBox(actions) {
  const ids = actions.map((a) => a.reeferId);
  assertEq(new Set(ids).size, ids.length, '每个箱最多一个动作');
}

// ------------------------------------------------------------------
test('P1. 无需削峰：预算宽松时不产生动作', async () => {
  const dir = newDataDir();
  const proc = await startServer(dir);
  try {
    const { now } = await seed(proc, [
      { id: 'L1', a: 20, grade: 'FROZEN' },
      { id: 'L2', a: 15, grade: 'FROZEN' },
    ]);
    await registerBudget(proc, { startAt: now - 1000, endAt: now + 600000, shipLimitA: 100 });
    const pub = await post(proc, '/api/peak/plans', { operatorId: 'OPS' });
    assertEq(pub.json.feasible, true, '可行');
    assertEq(pub.json.actions.length, 0, '无需任何动作');
    const st = (await getState(proc)).json;
    assertEq(st.peak.load.shipNetA, 35, '净载 35A');
    assert(st.peak.load.shipWithinLimit, '全船在上限内');
  } finally {
    await stopServer(proc);
  }
});

// ------------------------------------------------------------------
test('P2. 仅限功率即可达标：低风险箱被温和压缩，高风险箱不动', async () => {
  const dir = newDataDir();
  const proc = await startServer(dir);
  try {
    const { now } = await seed(proc, [
      { id: 'L1', a: 20, grade: 'FROZEN' },
      { id: 'L2', a: 20, grade: 'GENERAL' },
      { id: 'H1', a: 20, grade: 'PHARMA', set: 5, rise: 2 },
    ]);
    // 毛载 60，上限 45 -> 需释放 15
    await registerBudget(proc, { startAt: now - 1000, endAt: now + 600000, shipLimitA: 45 });
    const pub = await post(proc, '/api/peak/plans', { operatorId: 'OPS' });
    assertEq(pub.json.feasible, true, '可行');
    assert(pub.json.actions.every((a) => a.kind === 'LIMIT'), '全部为限功率');
    assertActionsOnePerBox(pub.json.actions);
    const touched = pub.json.actions.map((a) => a.reeferId).sort();
    assert(!touched.includes('H1'), '高风险箱 H1 不被限功率');
    assert(touched.includes('L1') || touched.includes('L2'), '至少压一个低风险箱');

    const exe = await post(proc, '/api/peak/actions/execute', {
      operatorId: 'OPS', actionIds: pub.json.actions.map((a) => a.actionId),
    });
    assert(exe.json.withinLimit, '执行后在上限内');
    assert(exe.json.shipNetA <= 45, `净载 ${exe.json.shipNetA} ≤ 45`);
    const st = (await getState(proc)).json;
    assert(st.peak.savings.currentSavingA >= 15 - 0.5, `当前节省 ${st.peak.savings.currentSavingA}A`);
  } finally {
    await stopServer(proc);
  }
});

// ------------------------------------------------------------------
test('P3. 必须断电：限功率不足时仅低风险箱短时断电，中/高风险受保护', async () => {
  const dir = newDataDir();
  const proc = await startServer(dir);
  try {
    const { now } = await seed(proc, [
      { id: 'L1', a: 20, grade: 'FROZEN' },
      { id: 'L2', a: 20, grade: 'GENERAL' },
      { id: 'L3', a: 20, grade: 'FROZEN' },
      { id: 'M1', a: 20, grade: 'FRESH', rise: 6 },
      { id: 'H1', a: 20, grade: 'PHARMA', set: 5, rise: 2 },
      { id: 'H2', a: 20, grade: 'HIGH', set: 2, rise: 2 },
    ]);
    // 毛载 120，上限 60 -> 限功率最多约 47A，仍需对低风险箱断电
    await registerBudget(proc, { startAt: now - 1000, endAt: now + 30 * 60000, shipLimitA: 60 });
    const pub = await post(proc, '/api/peak/plans', { operatorId: 'OPS' });
    assertEq(pub.json.feasible, true, '可行');
    assertActionsOnePerBox(pub.json.actions);
    const cuts = pub.json.actions.filter((a) => a.kind === 'CUT');
    assert(cuts.length >= 1, '至少一个短时断电');
    for (const c of cuts) {
      assert(['L1', 'L2', 'L3'].includes(c.reeferId), `断电仅限低风险箱: ${c.reeferId}`);
      assert(c.restoreBy != null, '断电必须给出恢复最迟时刻');
      assert(c.durationMin >= 2, '断电持续至少 2 分钟');
      assert(c.safeCutMinutes >= c.durationMin, '断电时长不超过保冷安全时长');
    }
    const touched = new Set(pub.json.actions.map((a) => a.reeferId));
    assert(!touched.has('H1'), 'H1 高风险无动作');
    assert(!touched.has('H2'), 'H2 高风险不受控');
    const m1 = pub.json.actions.find((a) => a.reeferId === 'M1');
    if (m1) assertEq(m1.kind, 'LIMIT', '中风险箱只允许限功率，不允许断电');

    const exe = await post(proc, '/api/peak/actions/execute', {
      operatorId: 'OPS', actionIds: pub.json.actions.map((a) => a.actionId),
    });
    assert(exe.json.withinLimit, '执行后在上限内');

    // 物理不可达（上限低于受保护负载）时必须判不可行且拒绝执行
    await registerBudget(proc, { startAt: now - 1000, endAt: now + 30 * 60000, shipLimitA: 10 });
    const bad = await post(proc, '/api/peak/plans', { operatorId: 'OPS' });
    assertEq(bad.json.feasible, false, '上限低于受保护负载 -> 不可行');
    assert(bad.json.infeasibleReasons.length >= 1, '给出无法满足预算的原因');
  } finally {
    await stopServer(proc);
  }
});

// ------------------------------------------------------------------
test('P4. 过期电流读数回退铭牌估算', async () => {
  const dir = newDataDir();
  const proc = await startServer(dir);
  try {
    const { now, sockOf } = await seed(proc, [
      { id: 'L1', a: 20, grade: 'FROZEN' },
      { id: 'L2', a: 20, grade: 'FROZEN' },
    ]);
    // 推进 10 分钟，使 5 分钟新鲜窗口外的电流读数过期
    await advance(proc, 10 * 60000);
    const t = await clock(proc);
    let st = (await getState(proc)).json;
    for (const rid of ['L1', 'L2']) {
      const row = st.peak.reefers.find((x) => x.reeferId === rid);
      assertEq(row.currentBasis, 'NAMEPLATE', `${rid} 读数过期回退铭牌`);
      assertEq(row.currentFresh, false, `${rid} 标记为不新鲜`);
    }
    // 即使读数过期，仍能据铭牌规划（上限 30 -> 释放 10）
    await registerBudget(proc, { startAt: t - 1000, endAt: t + 600000, shipLimitA: 30 });
    const pub = await post(proc, '/api/peak/plans', { operatorId: 'OPS' });
    assertEq(pub.json.feasible, true, '据铭牌可规划');
    assertEq(pub.json.actions.length >= 1, true, '产生限功率动作');
  } finally {
    await stopServer(proc);
  }
});

// ------------------------------------------------------------------
test('P5. 重叠预算合并取最严上限；任一下级上限不被突破', async () => {
  const dir = newDataDir();
  const proc = await startServer(dir);
  try {
    const { now } = await seed(proc, [
      { id: 'L1', a: 20, grade: 'FROZEN' },
      { id: 'L2', a: 20, grade: 'FROZEN' },
    ], { boards: 1 });
    // 宽松全船 + 更严配电板：有效上限 = min
    await registerBudget(proc, { startAt: now - 1000, endAt: now + 600000, shipLimitA: 1000, boardLimitsA: { B1: 30 }, operatorId: 'A' });
    await registerBudget(proc, { startAt: now - 1000, endAt: now + 600000, shipLimitA: 25, operatorId: 'B' });
    const st = (await getState(proc)).json;
    assertEq(st.peak.budget.active, true, '预算生效');
    assertEq(st.peak.budget.shipLimitA, 25, '全船取最严 25');
    assertEq(st.peak.budget.boardLimitsA.B1, 30, '配电板 30');
    assertEq(st.peak.budget.sourceBudgetIds.length, 2, '两个区间重叠合并');

    const pub = await post(proc, '/api/peak/plans', { operatorId: 'A' });
    assertEq(pub.json.feasible, true, '可行');
    const exe = await post(proc, '/api/peak/actions/execute', {
      operatorId: 'A', actionIds: pub.json.actions.map((a) => a.actionId),
    });
    assert(exe.json.withinLimit, '执行后两级上限都不突破');
    const after = (await getState(proc)).json;
    assert(after.peak.load.shipNetA <= 25, '全船 ≤25');
    const b1 = after.peak.load.boards.find((b) => b.boardId === 'B1');
    assert(b1.netA <= 30, '配电板 ≤30');
  } finally {
    await stopServer(proc);
  }
});

// ------------------------------------------------------------------
test('P6. 并发旧修订冲突：旧修订操作返回最新版本；同一请求标识只产生一次结果', async () => {
  const dir = newDataDir();
  const proc = await startServer(dir);
  try {
    const { now } = await seed(proc, [
      { id: 'L1', a: 20, grade: 'FROZEN' },
      { id: 'L2', a: 20, grade: 'FROZEN' },
    ]);
    await registerBudget(proc, { startAt: now - 1000, endAt: now + 600000, shipLimitA: 30 });
    const rev1 = await post(proc, '/api/peak/plans', { operatorId: 'A' });
    assertEq(rev1.json.rev, 1, '修订 1');

    // 值班员 B 基于最新发布修订 2
    const rev2 = await post(proc, '/api/peak/plans', { operatorId: 'B' });
    assertEq(rev2.json.rev, 2, '修订 2');

    // A 拿旧修订号 expectedRev=1 操作 -> 409 并返回当前最新版本
    const stale = await post(proc, '/api/peak/plans', { operatorId: 'A', expectedRev: 1 }, { expectStatus: 409 });
    assertEq(stale.json.error.code, 'REVISION_STALE', '旧修订冲突');
    assertEq(stale.json.error.currentRev, 2, '返回最新修订号 2');

    // 同一请求标识重复发送：两次内容一致 -> 回放首次答复，不产生修订 3
    const body = { operatorId: 'A' };
    const first = await post(proc, '/api/peak/plans', body, { commandId: 'fixed-cid-1' });
    const rev3 = first.json.rev;
    const replay = await post(proc, '/api/peak/plans', body, { commandId: 'fixed-cid-1' });
    assert(replay.json.replayed, '标记回放');
    assertEq(replay.json.rev, rev3, '回放同一修订号，不新增');
    const st = (await getState(proc)).json;
    assertEq(st.peak.revisions.length, 3, '仅 3 个修订（无重复）');

    // 同一请求标识 + 不同载荷 -> 幂等冲突
    const clash = await post(proc, '/api/peak/plans', { operatorId: 'DIFFERENT' }, { commandId: 'fixed-cid-1', expectStatus: 409 });
    assertEq(clash.json.error.code, 'IDEMPOTENCY_CONFLICT', '命令标识复用冲突');

    // 两名值班员并发发布：串行裁决，各自得到不同修订，箱不被重复控制
    const results = await Promise.all([
      post(proc, '/api/peak/plans', { operatorId: 'P1' }),
      post(proc, '/api/peak/plans', { operatorId: 'P2' }),
    ]);
    const revs = results.map((r) => r.json.rev).sort((a, b) => a - b);
    assertEq(revs[1], revs[0] + 1, '并发发布得到连续修订号');
    const st2 = (await getState(proc)).json;
    const pending = st2.peak.actions.filter((a) => a.status === 'PENDING');
    const byBox = new Map();
    for (const a of pending) byBox.set(a.reeferId, (byBox.get(a.reeferId) ?? 0) + 1);
    for (const [, n] of byBox) assertEq(n, 1, '同箱无重复待执行动作');
  } finally {
    await stopServer(proc);
  }
});

// ------------------------------------------------------------------
test('P7. 计划失效重算：只撤销未执行动作，已生效事实不消失', async () => {
  const dir = newDataDir();
  const proc = await startServer(dir);
  try {
    const { now } = await seed(proc, [
      { id: 'L1', a: 20, grade: 'FROZEN' },
      { id: 'L2', a: 20, grade: 'FROZEN' },
      { id: 'L3', a: 20, grade: 'FROZEN' },
      { id: 'L4', a: 20, grade: 'FROZEN' },
      { id: 'L5', a: 20, grade: 'FROZEN' },
    ]);
    // 毛载 100，上限 20 -> 限功率最多约 39A，必须对四台低风险箱短时断电
    await registerBudget(proc, { startAt: now - 1000, endAt: now + 600000, shipLimitA: 20 });
    const rev1 = await post(proc, '/api/peak/plans', { operatorId: 'A' });
    assertActionsOnePerBox(rev1.json.actions);
    const cutActions = rev1.json.actions.filter((a) => a.kind === 'CUT');
    assertEq(cutActions.length, 4, '方案含四台断电');
    // 只执行前两个，保留第三个未执行
    const ordered = rev1.json.actions;
    const executeIds = ordered.slice(0, 2).map((a) => a.actionId);
    const executedReefers = ordered.slice(0, 2).map((a) => a.reeferId);
    await post(proc, '/api/peak/actions/execute', { operatorId: 'A', actionIds: executeIds });
    const pendingReefer = ordered[2].reeferId;

    // 现场电流变化（L5 上升）使原计算失效
    const sockL5 = (await getState(proc)).json.circuits
      .flatMap((c) => c.sockets).find((x) => x.reeferId === 'L5').id;
    await post(proc, '/api/samples', {
      kind: 'current', socketId: sockL5, sensorBootId: 'B-L5',
      measuredAt: (await clock(proc)) - 1000, value: 26,
    });
    const before = (await getState(proc)).json;
    const prevRev = before.peak.currentRevision;
    assertEq(prevRev.stale, true, '基线变化且仍有未执行动作 -> 当前修订标记失效');

    const rec = await post(proc, '/api/peak/recompute', { operatorId: 'A' });
    const newRev = rec.json.rev;
    assert(newRev > prevRev.rev, '生成更新的修订');
    // 已生效动作不在作废列表；未执行动作被作废
    for (const rid of executedReefers) {
      assert(!rec.json.invalidated.some((x) => x.reeferId === rid), `${rid} 已生效，不在作废列表`);
    }
    assert(rec.json.invalidated.some((x) => x.reeferId === pendingReefer), `${pendingReefer} 未执行动作被作废`);
    // 已生效事实保留
    const st = (await getState(proc)).json;
    for (const rid of executedReefers) {
      const eff = st.peak.actions.find((a) => a.reeferId === rid && a.status === 'EFFECTIVE');
      assertEq(eff.status, 'EFFECTIVE', `${rid} 已生效动作不消失`);
    }
    // 新修订每箱至多一个待执行动作，且不重复控制已生效箱
    const newPending = st.peak.actions.filter((a) => a.rev === newRev && a.status === 'PENDING');
    for (const rid of executedReefers) {
      assert(!newPending.some((a) => a.reeferId === rid), `${rid} 已受控，新建议不重复控制`);
    }
    const ids = newPending.map((a) => a.reeferId);
    assertEq(new Set(ids).size, ids.length, '新建议每箱唯一');
    // 执行新建议后仍达标且不重复控制
    if (newPending.length) {
      const exe2 = await post(proc, '/api/peak/actions/execute', { operatorId: 'A', actionIds: newPending.map((a) => a.id) });
      assert(exe2.json.withinLimit, `重算并执行后达标（净载 ${exe2.json.shipNetA}A）`);
      const st2 = (await getState(proc)).json;
      const eff2 = st2.peak.actions.filter((a) => a.status === 'EFFECTIVE');
      assertEq(new Set(eff2.map((a) => a.reeferId)).size, eff2.length, '生效动作无同箱重复');
    }
  } finally {
    await stopServer(proc);
  }
});

// ------------------------------------------------------------------
test('P8. 预算结束后分批恢复：按可用余量、风险优先；途中遇更低上限暂停重算', async () => {
  const dir = newDataDir();
  const proc = await startServer(dir);
  try {
    const { now, sockOf } = await seed(proc, [
      { id: 'LA', a: 20, grade: 'FROZEN' },
      { id: 'LB', a: 20, grade: 'FROZEN' },
      { id: 'LC', a: 20, grade: 'FROZEN' },
    ]);
    await registerBudget(proc, { startAt: now - 1000, endAt: now + 20 * 60000, shipLimitA: 20 });
    const pub = await post(proc, '/api/peak/plans', { operatorId: 'A' });
    assertEq(pub.json.feasible, true, '可行（需断电两台低风险箱）');
    await post(proc, '/api/peak/actions/execute', {
      operatorId: 'A', actionIds: pub.json.actions.map((a) => a.actionId),
    });
    const cuts = pub.json.actions.filter((a) => a.kind === 'CUT').map((a) => a.reeferId);
    assertEq(cuts.length, 2, '两台断电');

    // 在断电期间让 LA 明显回温 -> 风险更高，恢复时应优先 LA
    const tMid = await clock(proc);
    await post(proc, '/api/samples', {
      kind: 'temperature', reeferId: 'LA', sensorBootId: 'T-LA',
      measuredAt: tMid - 60000, value: -18,
    });
    await post(proc, '/api/samples', {
      kind: 'temperature', reeferId: 'LA', sensorBootId: 'T-LA',
      measuredAt: tMid - 1000, value: -12,
    });

    // 推进到预算结束
    await advance(proc, 21 * 60000);
    // 第一批（maxBatchSize=1）应恢复风险更高的 LA
    const b1 = await post(proc, '/api/peak/restore', { operatorId: 'A', maxBatchSize: 1 });
    assertEq(b1.json.restored, 1, '恢复 1 台');
    assertEq(b1.json.items[0].reeferId, 'LA', '风险更高的 LA 优先恢复');
    assertEq(b1.json.budgetActive, false, '预算已结束');
    const st1 = (await getState(proc)).json;
    assertEq(st1.peak.recovery.total, 2, '本会话待恢复总数 2');
    assertEq(st1.peak.recovery.recoveredCount, 1, '已恢复 1');

    // 第二批恢复剩余
    const b2 = await post(proc, '/api/peak/restore', { operatorId: 'A', maxBatchSize: 5 });
    assertEq(b2.json.restored, 1, '恢复剩余 1 台');
    const st2 = (await getState(proc)).json;
    assertEq(st2.peak.recovery.state, 'COMPLETED', '恢复完成');
    assertEq(st2.peak.actions.filter((a) => a.status === 'EFFECTIVE').length, 0, '无残留生效动作');
    assertEq(st2.peak.load.shipNetA, 60, '全部恢复，净载回 60');
  } finally {
    await stopServer(proc);
  }
});

test('P8b. 恢复途中收到更低上限：暂停恢复、重新计算、负载不瞬间越限', async () => {
  const dir = newDataDir();
  const proc = await startServer(dir);
  try {
    const { now } = await seed(proc, [
      { id: 'LA', a: 20, grade: 'FROZEN' },
      { id: 'LB', a: 20, grade: 'FROZEN' },
      { id: 'LC', a: 20, grade: 'FROZEN' },
      { id: 'H1', a: 20, grade: 'PHARMA', set: 5, rise: 2 },
    ]);
    // 初始上限 40（毛载 80）
    await registerBudget(proc, { startAt: now - 1000, endAt: now + 60 * 60000, shipLimitA: 40 });
    const pub = await post(proc, '/api/peak/plans', { operatorId: 'A' });
    await post(proc, '/api/peak/actions/execute', {
      operatorId: 'A', actionIds: pub.json.actions.map((a) => a.actionId),
    });
    const afterExec = (await getState(proc)).json;
    assert(afterExec.peak.load.shipNetA <= 40, '执行后 ≤40');

    // 恢复途中叠加更严上限 20：在当前生效动作下没有任何一台可安全归还（归还即越限）
    const t = await clock(proc);
    await registerBudget(proc, { startAt: t - 1000, endAt: now + 60 * 60000, shipLimitA: 20, operatorId: 'B' });
    const r = await post(proc, '/api/peak/restore', { operatorId: 'A', maxBatchSize: 2 });
    assertEq(r.json.paused, true, '恢复暂停');
    assertEq(r.json.restored, 0, '无可用余量，一台都不恢复（不瞬间反弹越限）');
    assert(r.json.revisionPublished != null, '发布了重算新修订');
    // 当前生效负载未因恢复尝试而增加（未归还任何电流）
    let st = (await getState(proc)).json;
    const netBeforeAdvice = st.peak.load.shipNetA;
    assertEq(netBeforeAdvice, afterExec.peak.load.shipNetA, '恢复尝试不改变已生效负载');

    // 值班员执行新修订的待执行动作后，净载降到更严上限以内
    const newPending = r.json.actions.filter((a) => a.status === 'PENDING');
    assert(newPending.length >= 1, '新建议包含进一步削峰动作');
    if (newPending.length) {
      const exe = await post(proc, '/api/peak/actions/execute', {
        operatorId: 'A', actionIds: newPending.map((a) => a.actionId),
      });
      assert(exe.json.withinLimit, '执行新建议后在上限内');
    }
    st = (await getState(proc)).json;
    assert(st.peak.load.shipNetA <= 20, `执行更严预算建议后净载 ${st.peak.load.shipNetA} ≤ 20`);
    const eff = st.peak.actions.filter((a) => a.status === 'EFFECTIVE');
    assertEq(new Set(eff.map((a) => a.reeferId)).size, eff.length, '无同箱重复控制');
  } finally {
    await stopServer(proc);
  }
});

// ------------------------------------------------------------------
test('P9. 持久化边界崩溃恢复：发布/执行/恢复后退出，重启得到唯一状态', async () => {
  const dir = newDataDir();
  let proc = await startServer(dir, { snapshotEvery: 3 });
  try {
    const { now } = await seed(proc, [
      { id: 'L1', a: 20, grade: 'FROZEN' },
      { id: 'L2', a: 20, grade: 'FROZEN' },
      { id: 'H1', a: 20, grade: 'PHARMA', set: 5, rise: 2 },
    ]);
    await registerBudget(proc, { startAt: now - 1000, endAt: now + 600000, shipLimitA: 40 });

    // 9a. 发布方案后崩溃（事实落盘、内存未归并）
    await post(proc, '/test/faults/arm', { factType: 'peakPlanPublished' });
    const crashedPub = await post(proc, '/api/peak/plans', { operatorId: 'A' }, { tolerateCrash: true });
    await new Promise((r) => setTimeout(r, 400));
    proc = await restart(proc, dir);

    let st = (await getState(proc)).json;
    assertEq(st.peak.currentRevision != null, true, '重启后修订存在');
    assertEq(st.peak.actions.filter((a) => a.status === 'PENDING').length >= 1, true, '待执行动作被重放');
    // 同一请求标识重放 -> 首次答复，不产生第二个修订
    const replay = await post(proc, '/api/peak/plans', { operatorId: 'A' }, { commandId: crashedPub.commandId });
    assert(replay.json.replayed, '发布请求重放首次答复');
    const st2 = (await getState(proc)).json;
    assertEq(st2.peak.revisions.length, st.peak.revisions.length, '重放不新增修订');
    const revNo = st.peak.currentRevision.rev;
    const pending = st2.peak.actions.filter((a) => a.status === 'PENDING');
    assertEq(new Set(pending.map((a) => a.reeferId)).size, pending.length, '无同箱重复控制');

    // 9b. 执行限功率后崩溃
    await post(proc, '/test/faults/clear');
    await post(proc, '/test/faults/arm', { factType: 'shavingActionExecuted' });
    const crashedExe = await post(proc, '/api/peak/actions/execute', {
      operatorId: 'A', actionIds: pending.map((a) => a.id),
    }, { tolerateCrash: true });
    await new Promise((r) => setTimeout(r, 400));
    proc = await restart(proc, dir);
    st = (await getState(proc)).json;
    assert(st.peak.savings.currentSavingA > 0, '重启后节省量仍在');
    // 重放执行请求 -> 首次答复，不重复扣减
    const exeReplay = await post(proc, '/api/peak/actions/execute', {
      operatorId: 'A', actionIds: pending.map((a) => a.id),
    }, { commandId: crashedExe.commandId });
    assert(exeReplay.json.replayed, '执行请求重放');
    st = (await getState(proc)).json;
    const eff = st.peak.actions.filter((a) => a.status === 'EFFECTIVE');
    assertEq(new Set(eff.map((a) => a.reeferId)).size, eff.length, '无重复生效动作');
    assert(st.peak.load.shipNetA <= 40, '净载未越限');

    // 9c. 预算结束、恢复批次后崩溃
    await advance(proc, 20 * 60000);
    await post(proc, '/test/faults/clear');
    await post(proc, '/test/faults/arm', { factType: 'restoreBatchRecorded' });
    const crashedRest = await post(proc, '/api/peak/restore', { operatorId: 'A', maxBatchSize: 5 }, { tolerateCrash: true });
    await new Promise((r) => setTimeout(r, 400));
    proc = await restart(proc, dir);
    st = (await getState(proc)).json;
    assertEq(st.peak.recovery.state, 'COMPLETED', '恢复批次已落盘，重启后完成');
    // 重放恢复请求 -> 不重复记账
    const energyBefore = st.peak.savings.cumulativeEnergyAmin;
    const restReplay = await post(proc, '/api/peak/restore', { operatorId: 'A', maxBatchSize: 5 }, { commandId: crashedRest.commandId });
    assert(restReplay.json.replayed, '恢复请求重放');
    const st3 = (await getState(proc)).json;
    assertEq(st3.peak.savings.cumulativeEnergyAmin, energyBefore, '累计节省量不重复计算');
    assertEq(st3.peak.actions.filter((a) => a.status === 'EFFECTIVE').length, 0, '无残留生效动作');
    assert(st3.peak.load.shipNetA <= 60, '实际负载不越过已结束预算外的物理额定');
  } finally {
    await stopServer(proc);
  }
});
