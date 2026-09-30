'use strict';

import { test, before, after } from 'node:test';
import {
  newDataDir,
  startServer,
  stopServer,
  restart,
  post,
  getState,
  assert,
  assertEq,
  topologySimple,
} from './helpers.js';

let proc;
let dir;

before(async () => {
  dir = newDataDir();
  proc = await startServer(dir);
  await seedTopology();
});
after(async () => {
  await stopServer(proc);
});

async function seedTopology(topology = topologySimple()) {
  const r = await post(proc, '/api/topology', topology);
  return r.json;
}

function reefer(id, { powerKw = 5, phase = '3P', boxType = 'A1', area = 'BAY-A', prio = 3, insulation = 4 } = {}) {
  return { id, powerKw, phase, boxType, area, tempPriority: prio, insulationHours: insulation };
}

// ---------------------------------------------------------------
test('1. 正常预留并接电：自动分配插座，预留转供电，一插座/一箱唯一', async () => {
  const r = await post(proc, '/api/reefers', { reefers: [reefer('R1', { prio: 5 }), reefer('R2', { prio: 1 })] });
  assertEq(r.json.imported, 2, '导入 2 箱');

  const res = await post(proc, '/api/reservations', {});
  const ids = res.json.reserved.map((x) => x.reeferId).sort();
  assertEq(ids, ['R1', 'R2'], '两箱均获得预留');
  const s1 = res.json.reserved.find((x) => x.reeferId === 'R1').socketId;
  const s2 = res.json.reserved.find((x) => x.reeferId === 'R2').socketId;
  assert(s1 !== s2, '两个插座不同');

  // 接电终端必须提交 箱号+插座号+命令标识
  const conn = await post(proc, '/api/connect', { reeferId: 'R1', socketId: s1 });
  assert(conn.json.powered, 'R1 已供电');

  let st = (await getState(proc)).json;
  const sock1 = st.circuits.flatMap((c) => c.sockets).find((s) => s.id === s1);
  assertEq(sock1.status, 'POWERED', '插座状态 POWERED');
  assertEq(sock1.reeferId, 'R1', '插座绑定 R1');
  const r1 = st.reefers.find((x) => x.id === 'R1');
  assertEq(r1.status, 'POWERED', 'R1 视图 POWERED');
  assertEq(r1.socketId, s1, 'R1 绑定插座');

  // 同箱不能再占用第二个插座（它的预留已转 POWER）；别的箱也不能抢这个插座
  const dup1 = await post(proc, '/api/connect', { reeferId: 'R1', socketId: s2 }, { expectStatus: 409 });
  assert(['LEASE_POWER', 'SOCKET_OCCUPIED', 'SOCKET_MISMATCH'].includes(dup1.json.error.code),
    `R1 已送电后再次接电被拒绝: ${dup1.json.error.code}`);
  const dup2 = await post(proc, '/api/connect', { reeferId: 'R2', socketId: s1 }, { expectStatus: 409 });
  assertEq(dup2.json.error.code, 'SOCKET_MISMATCH', 'R2 不能用 R1 的预留插座');

  // 错误插座号拒绝
  const bad = await post(proc, '/api/connect', { reeferId: 'R2', socketId: 'S4' }, { expectStatus: 409 });
  assertEq(bad.json.error.code, 'SOCKET_MISMATCH', '必须使用被预留的插座');

  const conn2 = await post(proc, '/api/connect', { reeferId: 'R2', socketId: s2 });
  assert(conn2.json.powered, 'R2 已供电');
  st = (await getState(proc)).json;
  const active = st.circuits.flatMap((c) => c.sockets).filter((s) => s.leaseState === 'POWER');
  assertEq(active.length, 2, '恰好 2 个供电租约');
});

// ---------------------------------------------------------------
test('2. 容量或相制不匹配被拒绝；无预留直连被拒绝', async () => {
  const d = newDataDir();
  const p = await startServer(d);
  try {
    await post(p, '/api/topology', {
      boards: [{ id: 'B1', name: '板1' }],
      circuits: [
        { id: 'C1', boardId: 'B1', phase: '3P', ratedCurrentA: 32, name: '小容量三相回路' },
        { id: 'C2', boardId: 'B1', phase: '1P', ratedCurrentA: 16, name: '单相回路' },
      ],
      sockets: [
        { id: 'A1S', circuitId: 'C1', allowedBoxTypes: ['A1'], reachAreas: ['BAY-A'] },
        { id: 'A2S', circuitId: 'C1', allowedBoxTypes: ['A1'], reachAreas: ['BAY-A'] },
        { id: 'B1S', circuitId: 'C2', phase: '1P', ratedCurrentA: 16, allowedBoxTypes: ['A1'], reachAreas: ['BAY-A'] },
      ],
    });

    // 30A 的三相箱：两台 30A > 32A 回路（第二台容量拒绝）
    await post(p, '/api/reefers', {
      reefers: [
        { id: 'RA', boxType: 'A1', phase: '3P', ratedCurrentA: 30, area: 'BAY-A', tempPriority: 5, insulationHours: 4 },
        { id: 'RB', boxType: 'A1', phase: '3P', ratedCurrentA: 30, area: 'BAY-A', tempPriority: 4, insulationHours: 4 },
      ],
    });
    let res = await post(p, '/api/reservations', {});
    assertEq(res.json.reserved.map((x) => x.reeferId), ['RA'], '仅 RA 获得预留');
    const rb = res.json.rejected.find((x) => x.reeferId === 'RB');
    assert(rb.reasons.includes('CIRCUIT_CAPACITY'), `RB 容量越限: ${rb.reasons}`);
    assert(
      rb.reasons.includes('SOCKET_OCCUPIED') || rb.reasons.includes('SOCKET_RATING_EXCEEDED'),
      `RB 无其他合适插座: ${rb.reasons}`
    );

    // 相制不匹配：三相箱面对唯一空闲的 1P 插座
    await post(p, '/api/reefers', {
      reefers: [{ id: 'RC', boxType: 'A1', phase: '3P', ratedCurrentA: 10, area: 'BAY-A', tempPriority: 3, insulationHours: 4 }],
    });
    res = await post(p, '/api/reservations', {});
    const rc = res.json.rejected.find((x) => x.reeferId === 'RC');
    assert(rc.reasons.includes('PHASE_MISMATCH'), `相制不匹配拒绝: ${rc.reasons}`);

    // 额定电流越限：20A 单相箱 > 16A 插座
    await post(p, '/api/reefers', {
      reefers: [{ id: 'RD', boxType: 'A1', phase: '1P', ratedCurrentA: 20, area: 'BAY-A', tempPriority: 3, insulationHours: 4 }],
    });
    res = await post(p, '/api/reservations', {});
    const rd = res.json.rejected.find((x) => x.reeferId === 'RD');
    assert(rd.reasons.includes('SOCKET_RATING_EXCEEDED'), `插座额定越限: ${rd.reasons}`);
    assert(rd.reasons.includes('PHASE_MISMATCH'), `三相插座相制不符也计入: ${rd.reasons}`);

    // 箱型 / 可达性不匹配
    await post(p, '/api/reefers', {
      reefers: [{ id: 'RE', boxType: 'A2', phase: '3P', ratedCurrentA: 5, area: 'BAY-X', tempPriority: 3, insulationHours: 4 }],
    });
    res = await post(p, '/api/reservations', {});
    const re = res.json.rejected.find((x) => x.reeferId === 'RE');
    assert(re.reasons.includes('BOX_TYPE_NOT_ALLOWED'), '箱型不允许');
    assert(re.reasons.includes('AREA_UNREACHABLE'), '电缆不可达');

    // 无预留直接接电
    const noRes = await post(p, '/api/connect', { reeferId: 'RB', socketId: 'A2S' }, { expectStatus: 409 });
    assertEq(noRes.json.error.code, 'NO_RESERVATION', '无预留拒绝接电');

    // 容量校核也在送电终端把关：RA 正常送电
    const conn = await post(p, '/api/connect', { reeferId: 'RA', socketId: 'A1S' });
    assert(conn.json.powered, 'RA 送电成功');
  } finally {
    await stopServer(p);
  }
});

// ---------------------------------------------------------------
test('3. 采样重复与倒序：重复回放、载荷不同拒绝、超偏差样本隔离不参与计算', async () => {
  const t0 = Date.now();
  const s = {
    kind: 'current',
    socketId: 'S1',
    sensorBootId: 'BOOT-A',
    measuredAt: t0 - 1000,
    value: 17.2,
  };
  const first = await post(proc, '/api/samples', s);
  assert(first.json.accepted, '首次采样接受');

  // 完全重复（同命令标识 & 同载荷）：回放首次答复
  const dup = await post(proc, '/api/samples', s, { commandId: first.commandId });
  assertEq(dup.status, 200, '重复提交 200');
  assert(dup.json.replayed, '标记为回放');

  // 相同样本但新命令 + 载荷相同：服务端按载荷哈希也回放首次答复
  const dup2 = await post(proc, '/api/samples', s);
  assert(dup2.json.duplicate === true || dup2.json.replayed === true, '同一样本键重复返回首次答复');

  // 相同命令标识但载荷摘要不同 -> 拒绝且不改状态
  const conflict = await post(
    proc,
    '/api/samples',
    { ...s, value: 99 },
    { commandId: first.commandId, expectStatus: 409 }
  );
  assertEq(conflict.json.error.code, 'IDEMPOTENCY_CONFLICT', '命令标识复用冲突被拒绝');

  // 同一样本键数值不同（无 commandId 复用）也拒绝
  const vConflict = await post(proc, '/api/samples', { ...s, value: 99 }, { expectStatus: 409 });
  assertEq(vConflict.json.error.code, 'SAMPLE_CONFLICT', '同键异值拒绝改写');

  // 倒序样本：测量时刻倒退超过允许偏差（默认 5000ms）
  const ooo = await post(proc, '/api/samples', {
    kind: 'current',
    socketId: 'S1',
    sensorBootId: 'BOOT-A',
    measuredAt: t0 - 60_000,
    value: 1.0,
  });
  assertEq(ooo.json.accepted, false, '倒序样本被隔离');
  assertEq(ooo.json.reason, 'OUT_OF_ORDER_CLOCK_SKEW', '隔离原因正确');

  // 在偏差窗口内的小倒退允许
  const near = await post(proc, '/api/samples', {
    kind: 'current',
    socketId: 'S1',
    sensorBootId: 'BOOT-A',
    measuredAt: t0 - 1_500,
    value: 18.0,
  });
  assert(near.json.accepted, '偏差窗口内样本接受');

  // 隔离样本不参与负载：S1 实时负载应取最新接受样本 18.0，而不是隔离的 1.0
  const st = (await getState(proc)).json;
  const sock = st.circuits.flatMap((c) => c.sockets).find((x) => x.id === 'S1');
  assertEq(sock.measuredCurrentA, 18, 'S1 负载取最新接受值 18.0A');
  const q = st.samples.quarantined.find((x) => x.reason === 'OUT_OF_ORDER_CLOCK_SKEW');
  assert(q && q.value === 1.0, '操作台可见隔离样本');
});

// ---------------------------------------------------------------
test('4. 跳闸冻结 + 按温控优先级生成有序迁移并逐步完成', async () => {
  // 新配电板：C1 上两个供电箱，C2 上一个备用插座；再备一个跨回路插座
  const d = newDataDir();
  const p = await startServer(d);
  try {
    await post(p, '/api/topology', {
      boards: [{ id: 'B1', name: '板1' }],
      circuits: [
        { id: 'C1', boardId: 'B1', phase: '3P', ratedCurrentA: 63, name: '跳闸回路' },
        { id: 'C2', boardId: 'B1', phase: '3P', ratedCurrentA: 63, name: '备用回路' },
        { id: 'C3', boardId: 'B1', phase: '3P', ratedCurrentA: 63, name: '其他回路' },
      ],
      sockets: [
        { id: 'T1', circuitId: 'C1', allowedBoxTypes: ['A1'], reachAreas: ['BAY-A'] },
        { id: 'T2', circuitId: 'C1', allowedBoxTypes: ['A1'], reachAreas: ['BAY-A'] },
        { id: 'U1', circuitId: 'C2', allowedBoxTypes: ['A1'], reachAreas: ['BAY-A'] },
        { id: 'U2', circuitId: 'C2', allowedBoxTypes: ['A1'], reachAreas: ['BAY-A'] },
        { id: 'U3', circuitId: 'C2', allowedBoxTypes: ['A1'], reachAreas: ['BAY-A'] },
        { id: 'W1', circuitId: 'C3', allowedBoxTypes: ['A1'], reachAreas: ['BAY-A'] },
      ],
    });
    await post(p, '/api/reefers', {
      reefers: [
        reefer('H1', { powerKw: 3, prio: 2, insulation: 6 }),
        reefer('H2', { powerKw: 3, prio: 9, insulation: 2 }),
      ],
    });
    const res = await post(p, '/api/reservations', {});
    const h1s = res.json.reserved.find((x) => x.reeferId === 'H1').socketId;
    const h2s = res.json.reserved.find((x) => x.reeferId === 'H2').socketId;
    await post(p, '/api/connect', { reeferId: 'H1', socketId: h1s });
    await post(p, '/api/connect', { reeferId: 'H2', socketId: h2s });

    // 跳闸登记：立即冻结，返回受影响箱与剩余保温时限
    const trip = await post(p, '/api/circuits/trip', { circuitId: 'C1' });
    assertEq(trip.json.tripped, true, '跳闸已登记');
    const affIds = trip.json.impact.affected.map((a) => a.reeferId);
    assertEq(affIds, ['H2', 'H1'], '按温控优先级降序排列');
    const h2 = trip.json.impact.affected.find((a) => a.reeferId === 'H2');
    assert(h2.remainingMinutes > 110 && h2.remainingMinutes <= 120, `H2 剩余保温约 120 分钟，实际 ${h2.remainingMinutes}`);

    let st = (await getState(p)).json;
    assertEq(st.circuits.find((c) => c.id === 'C1').state, 'TRIPPED', '回路 TRIPPED');
    for (const id of affIds) {
      assertEq(st.reefers.find((r) => r.id === id).status, 'POWER_LOSS', `${id} 掉电`);
    }

    // 跳闸回路上禁止新接电/预留
    await post(p, '/api/reefers', { reefers: [reefer('H9', { powerKw: 3, prio: 9 })] });
    const rejRes = await post(p, '/api/reservations', {});
    assert(!rejRes.json.reserved.find((x) => x.reeferId === 'H9' && x.circuitId === 'C1'), '冻结回路不接新箱');

    // 生成迁移方案（先看草案）
    const dry = await post(p, '/api/migrations', { circuitId: 'C1', commit: false });
    assertEq(dry.json.steps.length, 2, '两步迁移');
    assertEq(dry.json.steps[0].reeferId, 'H2', '高优先级先迁');
    assert(!dry.json.committed, '草案未落任何事实');
    const tgt0 = dry.json.steps[0].targetSocketId;
    const tgt1 = dry.json.steps[1].targetSocketId;
    assert(
      [tgt0, tgt1].every((t) => ['U1', 'U2', 'U3'].includes(t)),
      `目标在备用回路 C2: ${tgt0}, ${tgt1}`
    );
    assert(tgt0 !== tgt1, '两个目标不同');

    // 正式提交：首个目标被预占
    const created = await post(p, '/api/migrations', { circuitId: 'C1' });
    assert(created.json.planId, '返回方案号');
    const planId = created.json.planId;
    st = (await getState(p)).json;
    const claimed = st.circuits.flatMap((c) => c.sockets).find((s) => s.status === 'CLAIMED');
    assertEq(claimed.id, tgt0, '首个目标已锁定 CLAIMED');

    // 步骤未完成前不能跳到下一步
    const skip = await post(p, '/api/migrations/steps',
      { planId, index: 1, action: 'unplug' }, { expectStatus: 409 });
    assertEq(skip.json.error.code, 'STEP_ORDER', '强制顺序执行');

    // 逐步确认：拔除 -> 占用 -> 送电
    const u = await post(p, '/api/migrations/steps', { planId, index: 0, action: 'unplug' });
    assertEq(u.json.state, 'UNPLUGGED', '步骤0 已拔除');
    st = (await getState(p)).json;
    // 拔除后原插座不能被他人抢占：H2 仍是 FROZEN/UNPLUGGED 持有者
    const fromView = st.circuits.flatMap((c) => c.sockets).find((x) => x.id === h2s);
    assert(['FROZEN'].includes(fromView.status), '原插座仍被迁移中的箱持有，未释放');

    const occ = await post(p, '/api/migrations/steps', { planId, index: 0, action: 'occupy' });
    assertEq(occ.json.state, 'OCCUPIED', '步骤0 目标已占用');
    const rp = await post(p, '/api/migrations/steps', { planId, index: 0, action: 'repower' });
    assertEq(rp.json.state, 'DONE', '步骤0 送电完成');

    st = (await getState(p)).json;
    let h2v = st.reefers.find((r) => r.id === 'H2');
    assertEq(h2v.status, 'POWERED', 'H2 恢复供电');
    assertEq(h2v.socketId, tgt0, 'H2 在新插座');
    assert(!st.insulation['H2'], 'H2 掉电计时清除');
    // 原插座已释放
    const releasedFrom = st.circuits.flatMap((c) => c.sockets).find((x) => x.id === h2s);
    assertEq(releasedFrom.status, 'AVAILABLE', '原插座已释放（无双重占用）');

    // 完成第二步
    await post(p, '/api/migrations/steps', { planId, index: 1, action: 'unplug' });
    await post(p, '/api/migrations/steps', { planId, index: 1, action: 'occupy' });
    await post(p, '/api/migrations/steps', { planId, index: 1, action: 'repower' });

    st = (await getState(p)).json;
    const plan = st.plans.find((x) => x.id === planId);
    assertEq(plan.state, 'COMPLETED', '方案完成');
    assert(plan.steps.every((s) => s.state === 'DONE'), '全部步骤 DONE');
    for (const id of ['H1', 'H2']) {
      assertEq(st.reefers.find((r) => r.id === id).status, 'POWERED', `${id} 最终供电`);
    }
    // 全局不变量：恰好两个 POWER 租约，无双重占用
    const poweredSocks = st.circuits.flatMap((c) => c.sockets).filter((s) => s.status === 'POWERED');
    const uniq = new Set(poweredSocks.map((s) => s.reeferId));
    assertEq(uniq.size, 2, '无双重占用');
    assertEq(poweredSocks.length, 2, '两个供电插座');
  } finally {
    await stopServer(p);
  }
});

// ---------------------------------------------------------------
test('5. 两名电气员争抢同一备用插座：先提交者成功，后者得到冲突与可重算结果', async () => {
  const d = newDataDir();
  const p = await startServer(d);
  try {
    await post(p, '/api/topology', {
      boards: [{ id: 'B1', name: '板1' }],
      circuits: [
        { id: 'K1', boardId: 'B1', phase: '3P', ratedCurrentA: 63 },
        { id: 'K2', boardId: 'B1', phase: '3P', ratedCurrentA: 63 },
        { id: 'K3', boardId: 'B1', phase: '3P', ratedCurrentA: 16 },
        { id: 'K4', boardId: 'B1', phase: '3P', ratedCurrentA: 63 },
      ],
      sockets: [
        { id: 'K1S', circuitId: 'K1', allowedBoxTypes: ['A1'], reachAreas: ['BAY-A'] },
        { id: 'K2S', circuitId: 'K2', allowedBoxTypes: ['A1'], reachAreas: ['BAY-A'] },
        { id: 'SP1', circuitId: 'K3', allowedBoxTypes: ['A1'], reachAreas: ['BAY-A'] },
        { id: 'SP2', circuitId: 'K4', allowedBoxTypes: ['A1'], reachAreas: ['BAY-A'] },
      ],
    });
    await post(p, '/api/reefers', { reefers: [reefer('E1'), reefer('E2')] });
    const res = await post(p, '/api/reservations', {});
    const e1s = res.json.reserved.find((x) => x.reeferId === 'E1').socketId;
    const e2s = res.json.reserved.find((x) => x.reeferId === 'E2').socketId;
    await post(p, '/api/connect', { reeferId: 'E1', socketId: e1s });
    await post(p, '/api/connect', { reeferId: 'E2', socketId: e2s });
    // 两条回路分别跳闸（SP1 为两者最优备用，SP2 次之）
    await post(p, '/api/circuits/trip', { circuitId: 'K1' });
    await post(p, '/api/circuits/trip', { circuitId: 'K2' });

    // 电气员 A 先提交 K1 的方案
    const a = await post(p, '/api/migrations', { circuitId: 'K1' });
    assertEq(a.status ?? a.status, 200, 'A 提交成功');
    assertEq(a.json.steps[0].targetSocketId, 'SP1', 'A 抢到 SP1');

    // 电气员 B 提交 K2 的方案：理想目标也是 SP1 -> 冲突
    const b = await post(p, '/api/migrations', { circuitId: 'K2' }, { expectStatus: 409 });
    assertEq(b.json.error.code, 'TARGET_CONTENDED', 'B 得到争用冲突');
    assertEq(b.json.error.conflicts[0].targetSocketId, 'SP1', '冲突目标 SP1');
    assertEq(b.json.error.conflicts[0].heldByReeferId, 'E1', '占用人为 E1');
    // 服务端给出在最新占用下可重新计算的备选
    const alt = b.json.error.alternative;
    assert(alt, '返回备选');
    assertEq(alt.steps[0].targetSocketId, 'SP2', '备选改用 SP2');

    // B 按备选重新计算并提交 -> 成功
    const b2 = await post(p, '/api/migrations', { circuitId: 'K2', recompute: true });
    assertEq(b2.json.steps[0].targetSocketId, 'SP2', 'B 重算后占 SP2');

    const st = (await getState(p)).json;
    const sp1 = st.circuits.flatMap((c) => c.sockets).find((s) => s.id === 'SP1');
    const sp2 = st.circuits.flatMap((c) => c.sockets).find((s) => s.id === 'SP2');
    assertEq(sp1.status, 'CLAIMED', 'SP1 属 A 的方案');
    assertEq(sp2.status, 'CLAIMED', 'SP2 属 B 的方案');
    assertEq(sp1.reeferId, 'E1', 'SP1 -> E1');
    assertEq(sp2.reeferId, 'E2', 'SP2 -> E2');
  } finally {
    await stopServer(p);
  }
});

// ---------------------------------------------------------------
test('6. 迁移中途取消：尚未执行步骤释放目标，已完成步骤保留', async () => {
  const d = newDataDir();
  const p = await startServer(d);
  try {
    await post(p, '/api/topology', {
      boards: [{ id: 'B1', name: '板1' }],
      circuits: [
        { id: 'M1', boardId: 'B1', phase: '3P', ratedCurrentA: 63 },
        { id: 'M2', boardId: 'B1', phase: '3P', ratedCurrentA: 63 },
      ],
      sockets: [
        { id: 'M1A', circuitId: 'M1', allowedBoxTypes: ['A1'], reachAreas: ['BAY-A'] },
        { id: 'M1B', circuitId: 'M1', allowedBoxTypes: ['A1'], reachAreas: ['BAY-A'] },
        { id: 'M2A', circuitId: 'M2', allowedBoxTypes: ['A1'], reachAreas: ['BAY-A'] },
        { id: 'M2B', circuitId: 'M2', allowedBoxTypes: ['A1'], reachAreas: ['BAY-A'] },
      ],
    });
    await post(p, '/api/reefers', { reefers: [reefer('J1', { powerKw: 3, prio: 8 }), reefer('J2', { powerKw: 3, prio: 2 })] });
    const res = await post(p, '/api/reservations', {});
    const j1s = res.json.reserved.find((x) => x.reeferId === 'J1').socketId;
    const j2s = res.json.reserved.find((x) => x.reeferId === 'J2').socketId;
    await post(p, '/api/connect', { reeferId: 'J1', socketId: j1s });
    await post(p, '/api/connect', { reeferId: 'J2', socketId: j2s });
    await post(p, '/api/circuits/trip', { circuitId: 'M1' });

    const created = await post(p, '/api/migrations', { circuitId: 'M1' });
    const planId = created.json.planId;
    const step1Target = created.json.steps[1].targetSocketId;

    // 完成步骤 0，然后在步骤 1 尚未执行时取消方案
    await post(p, '/api/migrations/steps', { planId, index: 0, action: 'unplug' });
    await post(p, '/api/migrations/steps', { planId, index: 0, action: 'occupy' });
    await post(p, '/api/migrations/steps', { planId, index: 0, action: 'repower' });

    await post(p, '/api/migrations/cancel', { planId });

    const st = (await getState(p)).json;
    const plan = st.plans.find((x) => x.id === planId);
    assertEq(plan.state, 'CANCELLED', '方案已取消');
    assertEq(plan.steps[0].state, 'DONE', '步骤0 保留完成');
    assertEq(plan.steps[1].state, 'CANCELLED', '步骤1 取消');

    // 未执行步骤的目标插座被释放，可被他人使用
    const target1 = st.circuits.flatMap((c) => c.sockets).find((s) => s.id === step1Target);
    assertEq(target1.status, 'AVAILABLE', '未执行目标已释放');

    // J2 仍断电但持有原插座（未释放），可重新规划迁移
    const j2 = st.reefers.find((r) => r.id === 'J2');
    assertEq(j2.status, 'POWER_LOSS', 'J2 仍断电');
    assert(j2.socketId === j2s, 'J2 原插座未被释放');

    const again = await post(p, '/api/migrations', { circuitId: 'M1' });
    assertEq(again.json.steps.length, 1, '重新规划仅含 J2');
    assertEq(again.json.steps[0].reeferId, 'J2', '重新迁移 J2');
    await post(p, '/api/migrations/steps', { planId: again.json.planId, index: 0, action: 'unplug' });
    await post(p, '/api/migrations/steps', { planId: again.json.planId, index: 0, action: 'occupy' });
    await post(p, '/api/migrations/steps', { planId: again.json.planId, index: 0, action: 'repower' });
    const st2 = (await getState(p)).json;
    assertEq(st2.reefers.find((r) => r.id === 'J2').status, 'POWERED', 'J2 二次迁移成功');
  } finally {
    await stopServer(p);
  }
});

// ---------------------------------------------------------------
test('7. 换插座事务中断后的恢复：各持久化边界崩溃均归并唯一结果', async () => {
  const d = newDataDir();
  let p = await startServer(d);

  // 7a. 送电边界崩溃（事实已落盘，内存未归并）
  await post(p, '/api/topology', topologySimple());
  await post(p, '/api/reefers', { reefers: [reefer('Q1', { prio: 5 })] });
  const res = await post(p, '/api/reservations', {});
  const q1s = res.json.reserved[0].socketId;
  const connectCmd = { reeferId: 'Q1', socketId: q1s };

  await post(p, '/test/faults/arm', { factType: 'powered' });
  const crashed = await post(p, '/api/connect', connectCmd, { tolerateCrash: true });
  // 等待进程退出
  await new Promise((r) => setTimeout(r, 500));

  p = await restart(p, d);
  let st = (await getState(p)).json;
  const q1 = st.reefers.find((r) => r.id === 'Q1');
  assertEq(q1.status, 'POWERED', '重启后 Q1 已归并为 POWERED（唯一结果）');

  // 同一命令标识再次提交 -> 首次答复，不重复送电、不产生第二条租约
  const replay = await post(p, '/api/connect', connectCmd, { commandId: crashed.commandId });
  assert(replay.json.replayed, '重放得到首次答复（replayed）');
  assert(replay.json.powered, '首次答复内容为 powered=true');
  st = (await getState(p)).json;
  const q1sView = st.circuits.flatMap((c) => c.sockets).filter((x) => x.reeferId === 'Q1');
  assertEq(q1sView.length, 1, 'Q1 仍只占一个插座，无双重占用');

  // 7b. 迁移创建（目标预占）边界崩溃
  const d2 = newDataDir();
  let p2 = await startServer(d2);
  await post(p2, '/api/topology', {
    boards: [{ id: 'B1', name: '板1' }],
    circuits: [
      { id: 'V1', boardId: 'B1', phase: '3P', ratedCurrentA: 63 },
      { id: 'V2', boardId: 'B1', phase: '3P', ratedCurrentA: 63 },
    ],
    sockets: [
      { id: 'V1S', circuitId: 'V1', allowedBoxTypes: ['A1'], reachAreas: ['BAY-A'] },
      { id: 'V2S', circuitId: 'V2', allowedBoxTypes: ['A1'], reachAreas: ['BAY-A'] },
    ],
  });
  await post(p2, '/api/reefers', { reefers: [reefer('W1')] });
  const r2 = await post(p2, '/api/reservations', {});
  await post(p2, '/api/connect', { reeferId: 'W1', socketId: r2.json.reserved[0].socketId });
  await post(p2, '/api/circuits/trip', { circuitId: 'V1' });
  await post(p2, '/test/faults/arm', { factType: 'migrationCreated' });
  const crashedCreate = await post(p2, '/api/migrations', { circuitId: 'V1' }, { tolerateCrash: true });
  assert(crashedCreate.json.crashed || crashedCreate.json.planId, '迁移创建触发崩溃或已完成');
  await new Promise((r) => setTimeout(r, 500));

  p2 = await restart(p2, d2);
  st = (await getState(p2)).json;
  const plans = st.plans.filter((x) => x.state === 'ACTIVE');
  assertEq(plans.length, 1, '恰好一个活动迁移方案（无重复）');
  assertEq(st.circuits.flatMap((c) => c.sockets).filter((s) => s.status === 'CLAIMED').length, 1,
    '恰好一个 CLAIMED 目标');
  // 计划可继续执行
  const planId = plans[0].id;
  await post(p2, '/api/migrations/steps', { planId, index: 0, action: 'unplug' });
  await post(p2, '/api/migrations/steps', { planId, index: 0, action: 'occupy' });

  // 7c. 送电（repower，换插座原子边界）崩溃
  await post(p2, '/test/faults/arm', { factType: 'stepAdvanced' });
  await post(p2, '/api/migrations/steps', { planId, index: 0, action: 'repower' }, { tolerateCrash: true });
  await new Promise((r) => setTimeout(r, 400));
  p2 = await restart(p2, d2);

  st = (await getState(p2)).json;
  const plan = st.plans.find((x) => x.id === planId);
  assertEq(plan.state, 'COMPLETED', 'repower 已落盘 -> 重启后方案 COMPLETED');
  assertEq(plan.steps[0].state, 'DONE', '步骤 DONE');
  const w1 = st.reefers.find((r) => r.id === 'W1');
  assertEq(w1.status, 'POWERED', '迁移已完成且箱恢复供电（不会停留在断电标记）');
  assertEq(w1.socketId, 'V2S', 'W1 在新插座');
  const v1s = st.circuits.flatMap((c) => c.sockets).find((s) => s.id === 'V1S');
  assertEq(v1s.status, 'AVAILABLE', '原插座已释放');
  // 负载统计不失真
  const v2 = st.circuits.find((c) => c.id === 'V2');
  assert(v2.loadA > 0 && v2.loadA <= v2.ratedCurrentA, `V2 负载合理: ${v2.loadA}A`);
  const v1 = st.circuits.find((c) => c.id === 'V1');
  assertEq(v1.loadA, 0, '跳闸回路负载为 0，统计无失真');
  await stopServer(p2);

  // 7d. 跨越快照周期后的重启归并
  await stopServer(p);
  p = await startServer(d);
  st = (await getState(p)).json;
  assertEq(st.reefers.find((r) => r.id === 'Q1').status, 'POWERED', '再次重启状态一致');
  await stopServer(p);
});
