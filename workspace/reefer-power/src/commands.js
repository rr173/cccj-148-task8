'use strict';

import {
  activeLeaseOfReefer,
  activeLeaseOnSocket,
  leaseById,
  poweredLease,
} from './domain.js';
import { planReservations, planMigration, contentionOnCommit, trippedImpact, allocateFresh } from './planner.js';
import { buildView, committedDemandA } from './views.js';
import { sweepAfterCoreFacts } from './precool/monitor.js';
import {
  bad,
  conflict,
  notFound,
  checkId,
  num,
  strList,
  normPhase,
  round2,
  config,
} from './util.js';

// 每个处理器返回 { facts, reply }

// ---------- 配电拓扑录入 ----------
export function cmdTopology(s, body, at) {
  const boards = [];
  const circuits = [];
  const sockets = [];
  for (const b of body.boards || []) {
    const id = checkId(b.id, '配电板');
    boards.push({ id, name: String(b.name ?? id) });
  }
  const boardIds = new Set(boards.map((b) => b.id));
  for (const c of body.circuits || []) {
    const id = checkId(c.id, '回路');
    const boardId = checkId(c.boardId, '回路配电板');
    if (!boardIds.has(boardId)) throw bad(`回路 ${id} 引用了未登记的配电板 ${boardId}`);
    circuits.push({
      id,
      boardId,
      name: String(c.name ?? id),
      phase: normPhase(c.phase),
      ratedCurrentA: num(c.ratedCurrentA, '回路额定电流', { min: 0.1 }),
    });
  }
  const circuitIds = new Set(circuits.map((c) => c.id));
  for (const sk of body.sockets || []) {
    const id = checkId(sk.id, '插座');
    const circuitId = checkId(sk.circuitId, '插座回路');
    if (!circuitIds.has(circuitId)) throw bad(`插座 ${id} 引用了未登记的回路 ${circuitId}`);
    sockets.push({
      id,
      circuitId,
      name: String(sk.name ?? id),
      phase: sk.phase ? normPhase(sk.phase) : null,
      ratedCurrentA: sk.ratedCurrentA != null ? num(sk.ratedCurrentA, '插座额定电流', { min: 0.1 }) : null,
      allowedBoxTypes: strList(sk.allowedBoxTypes, '允许箱型'),
      reachAreas: strList(sk.reachAreas, '电缆可达区域'),
    });
  }
  if (!circuits.length) throw bad('至少需要录入一条回路');
  if (!sockets.length) throw bad('至少需要录入一个插座');
  const fact = { type: 'topologyImported', at, boards, circuits, sockets };
  return {
    facts: [fact],
    reply: { imported: { boards: boards.length, circuits: circuits.length, sockets: sockets.length } },
  };
}

// ---------- 冷藏箱功率需求与温控优先级导入 ----------
export function cmdReefers(s, body, at) {
  if (!Array.isArray(body.reefers) || !body.reefers.length) throw bad('reefers 必须是非空数组');
  const reefers = [];
  for (const r of body.reefers) {
    const id = checkId(r.id, '冷藏箱');
    const boxType = checkId(r.boxType, '箱型');
    const phase = normPhase(r.phase);
    if (r.powerKw == null && r.ratedCurrentA == null) {
      throw bad(`箱 ${id} 必须提供 powerKw 或 ratedCurrentA`);
    }
    const area = checkId(r.area, '堆放区域');
    reefers.push({
      id,
      boxType,
      phase,
      powerKw: r.powerKw != null ? num(r.powerKw, '功率(kW)', { min: 0.01 }) : null,
      ratedCurrentA: r.ratedCurrentA != null ? num(r.ratedCurrentA, '额定电流(A)', { min: 0.1 }) : null,
      area,
      tempPriority: num(r.tempPriority ?? 3, '温控优先级', { min: 1, max: 99, integer: true }),
      insulationHours: num(r.insulationHours ?? 8, '保温时限(小时)', { min: 0.01 }),
      setpointC: r.setpointC != null ? Number(r.setpointC) : null,
      cargoGrade: r.cargoGrade != null ? String(r.cargoGrade) : 'GENERAL',
      allowableRiseC: r.allowableRiseC != null ? num(r.allowableRiseC, '允许温升(℃)', { min: 0.1 }) : null,
    });
  }
  const ids = reefers.map((r) => r.id);
  if (new Set(ids).size !== ids.length) throw bad('箱号重复');
  return {
    facts: [{ type: 'reefersImported', at, reefers }],
    reply: { imported: reefers.length, reefers: ids },
  };
}

// ---------- 自动生成插座预留 ----------
export function cmdReserve(s, body, at) {
  if (!s.sockets.size) throw bad('尚未录入配电拓扑');
  if (!s.reefers.size) throw bad('尚未导入冷藏箱');
  const { facts, decisions } = planReservations(s, at);
  return {
    facts,
    reply: {
      reserved: decisions.filter((d) => d.accepted),
      rejected: decisions.filter((d) => !d.accepted),
    },
  };
}

// ---------- 接电终端：预留 -> 供电 ----------
export function cmdConnect(s, body, at) {
  const reeferId = checkId(body.reeferId, '冷藏箱');
  const socketId = checkId(body.socketId, '插座');
  const reefer = s.reefers.get(reeferId);
  const socket = s.sockets.get(socketId);
  if (!reefer) throw notFound(`冷藏箱不存在: ${reeferId}`);
  if (!socket) throw notFound(`插座不存在: ${socketId}`);

  const myLease = activeLeaseOfReefer(s, reeferId);
  if (!myLease) throw conflict(`箱 ${reeferId} 没有插座预留`, { code: 'NO_RESERVATION' });
  if (myLease.reeferId === reeferId && myLease.socketId !== socketId) {
    throw conflict(`箱 ${reeferId} 的预留插座是 ${myLease.socketId}，不是 ${socketId}`, {
      code: 'SOCKET_MISMATCH',
      reservedSocketId: myLease.socketId,
    });
  }
  if (myLease.state !== 'RESERVE') {
    throw conflict(`箱 ${reeferId} 当前租约状态为 ${myLease.state}，预留已无法转供电`, {
      code: `LEASE_${myLease.state}`,
    });
  }
  const holder = activeLeaseOnSocket(s, socketId);
  if (holder && holder.id !== myLease.id) {
    throw conflict(`插座 ${socketId} 已被箱 ${holder.reeferId} 占用`, { code: 'SOCKET_OCCUPIED' });
  }

  const circuit = s.circuits.get(socket.circuitId);
  if (circuit.state === 'TRIPPED') {
    throw conflict(`回路 ${circuit.id} 已跳闸，冻结新接电`, { code: 'CIRCUIT_TRIPPED' });
  }

  // 送电前以实时承诺负载再次校核（排除自身预留，防止期间回路余量变化）
  const poweredPart = committedDemandA(s, circuit.id, { excludeLeaseId: myLease.id }, at);
  if (round2(poweredPart + reefer.ratedCurrentA) > circuit.ratedCurrentA) {
    throw conflict(
      `回路 ${circuit.id} 容量越限: 需要 ${reefer.ratedCurrentA}A，当前已用 ${poweredPart}A，额定 ${circuit.ratedCurrentA}A`,
      { code: 'CIRCUIT_CAPACITY', loadA: poweredPart, ratedA: circuit.ratedCurrentA }
    );
  }

  return {
    facts: [{ type: 'powered', at, leaseId: myLease.id, reeferId, socketId }],
    reply: {
      powered: true,
      reeferId,
      socketId,
      circuitId: circuit.id,
      loadA: round2(poweredPart + reefer.ratedCurrentA),
    },
  };
}

// ---------- 传感器样本 ----------
function sampleKey(f) {
  return `${f.sensorBootId}|${f.kind}|${f.reeferId ?? '-'}|${f.socketId ?? '-'}|${f.circuitId ?? '-'}|${f.measuredAt}`;
}

// 同一传感器启动代号下的单调游标：{lastMeasuredAt, lastKey}
function cursorId(f) {
  return `${f.sensorBootId}|${f.kind}|${f.reeferId ?? '-'}|${f.socketId ?? '-'}|${f.circuitId ?? '-'}`;
}

export function cmdSample(s, body, at) {
  const sensorBootId = checkId(body.sensorBootId, '传感器启动代号');
  const kindRaw = String(body.kind ?? '').toLowerCase();
  if (!['current', 'temperature', 'powerloss'].includes(kindRaw)) {
    throw bad('样本 kind 必须是 current / temperature / powerLoss');
  }
  // 规范化：UI/接口允许 powerLoss/powerloss，流水与归并统一使用 powerLoss
  const kind = kindRaw === 'powerloss' ? 'powerLoss' : kindRaw;
  const measuredAt = num(body.measuredAt, '测量时刻(epoch ms)', { integer: true });
  if (Math.abs(measuredAt - at) > 24 * 3600 * 1000) throw bad('测量时刻与服务器时间相差超过 24 小时');
  const value = num(body.value, '样本数值', { min: -1000, max: 100000 });

  const f = {
    type: 'sampleRecorded',
    kind,
    sensorBootId,
    reeferId: body.reeferId ? checkId(body.reeferId, '冷藏箱') : null,
    socketId: body.socketId ? checkId(body.socketId, '插座') : null,
    circuitId: body.circuitId ? checkId(body.circuitId, '回路') : null,
    measuredAt,
    receivedAt: at,
    value,
  };
  if (kind === 'current' && !f.socketId) throw bad('电流样本必须带 socketId');
  if (kind !== 'current' && !f.reeferId) throw bad(`${kind} 样本必须带 reeferId`);
  if (f.socketId && !s.sockets.has(f.socketId)) throw notFound(`插座不存在: ${f.socketId}`);
  if (f.reeferId && !s.reefers.has(f.reeferId)) throw notFound(`冷藏箱不存在: ${f.reeferId}`);
  if (f.circuitId && !s.circuits.has(f.circuitId)) throw notFound(`回路不存在: ${f.circuitId}`);

  const key = sampleKey(f);
  if (s.samples.has(key)) {
    const prev = s.samples.get(key);
    // 完全重复：直接回放首次答复（不产生新事实）
    if (prev.value === value) {
      return {
        facts: [],
        reply: { accepted: false, duplicate: true, key, firstReceivedAt: prev.receivedAt },
      };
    }
    // 同一键但数值不同：载荷冲突拒绝
    throw conflict('同一样本键再次提交但数值不同，拒绝改写', {
      code: 'SAMPLE_CONFLICT',
      key,
      firstValue: prev.value,
    });
  }

  // 倒序检测：同一传感器启动代号下测量时刻倒退且超过允许时钟偏差 -> 隔离待审
  const cid = cursorId(f);
  const last = s.samples.get(`__cursor__${cid}`);
  let accepted = true;
  let reason = null;
  if (last && last.measuredAt - measuredAt > config().maxClockSkewMs) {
    accepted = false;
    reason = 'OUT_OF_ORDER_CLOCK_SKEW';
  }
  f.key = key;
  f.accepted = accepted;
  f.reason = reason;

  const facts = [{ ...f }];
  // 游标本身也入流水（作为已接受的排序事实），保证重启后单调判断一致
  if (accepted) {
    if (!last || measuredAt > last.measuredAt) {
      facts.push({
        type: 'sampleRecorded',
        kind: '__cursor__',
        sensorBootId,
        reeferId: f.reeferId,
        socketId: f.socketId,
        circuitId: f.circuitId,
        measuredAt,
        receivedAt: at,
        value: 0,
        key: `__cursor__${cid}`,
        accepted: true,
        reason: null,
      });
    }
  }

  // 预冷互锁：已接受的温度/掉电样本可能使既有合格证立即失效
  // （传感器更换、超温、读数过期；powerLoss 打破供电连续性）。
  let invalidations = [];
  let sweepFacts = [];
  if (accepted && f.reeferId && (kind === 'temperature' || kind === 'powerLoss')) {
    const extra = kind === 'temperature'
      ? { measuredAt, value, bootId: sensorBootId }
      : null;
    const swept = sweepAfterCoreFacts(s, at, facts, [f.reeferId], { extraSample: extra });
    sweepFacts = swept.facts;
    invalidations = swept.invalidations;
  }

  return {
    facts: [...facts, ...sweepFacts],
    reply: {
      accepted,
      reason,
      key,
      kind,
      measuredAt,
      quarantined: !accepted,
      invalidations,
    },
  };
}

// ---------- 回路跳闸登记 ----------
export function cmdTrip(s, body, at) {
  const circuitId = checkId(body.circuitId, '回路');
  const c = s.circuits.get(circuitId);
  if (!c) throw notFound(`回路不存在: ${circuitId}`);
  if (c.state === 'TRIPPED') {
    // 跳闸登记幂等：返回当前影响，不重复登记
    return { facts: [], reply: { tripped: false, alreadyTripped: true, impact: trippedImpact(s, circuitId, at) } };
  }
  const coreFacts = [{ type: 'tripped', at, circuitId }];
  // 受影响箱 = 跳闸前该回路上的 POWER 租约箱（跳闸事实归并后它们变为 FROZEN）
  const affectedReeferIds = activeLeaseOfReeferOnCircuitBeforeTrip(s, circuitId);
  const swept = sweepAfterCoreFacts(s, at, coreFacts, affectedReeferIds);
  const facts = [...coreFacts, ...swept.facts];
  return {
    facts,
    // impact 必须在事实归并后计算，由 store 提交后通过 enrich 补入回执；
    // 预冷合格证/豁免的自动失效也在回执中明示
    enrich: (state) => ({
      tripped: true,
      circuitId,
      at,
      impact: trippedImpact(state, circuitId, at),
      invalidations: swept.invalidations,
    }),
  };
}

function activeLeaseOfReeferOnCircuitBeforeTrip(s, circuitId) {
  const ids = [];
  for (const l of s.leases) {
    if (l.state !== 'POWER') continue;
    const sk = s.sockets.get(l.socketId);
    if (sk && sk.circuitId === circuitId) ids.push(l.reeferId);
  }
  return ids;
}

// ---------- 迁移方案 ----------
export function cmdMigrationCreate(s, body, at) {
  const circuitId = checkId(body.circuitId, '回路');
  if (!s.circuits.has(circuitId)) throw notFound(`回路不存在: ${circuitId}`);
  const commit = body.commit !== false;
  const recompute = body.recompute === true;
  const ideal = planMigration(s, circuitId, { at });

  if (!commit) {
    return {
      facts: [],
      reply: {
        committed: false,
        circuitId,
        steps: ideal.steps,
        unassigned: ideal.unassigned,
        affected: ideal.affected,
      },
    };
  }

  let proposal = ideal;
  if (!recompute) {
    const clash = contentionOnCommit(s, proposal, at);
    if (clash) {
      throw conflict('迁移目标被其他方案争用，先提交者成功', {
        ...clash,
        circuitId,
      });
    }
  } else {
    // 在最新占用下重新计算（计入其他方案的 CLAIM）
    proposal = allocateFresh(s, circuitId, at);
  }
  if (!proposal.steps.length) {
    throw conflict('没有可执行的迁移步骤', { code: 'NO_FEASIBLE_STEP', unassigned: proposal.unassigned });
  }

  // 步骤 0 立即预占目标（拔除与占位按顺序确认；首个目标先锁定，
  // 防止计划刚生成就被他人抢走）。同插座步骤无需占位。
  const steps = proposal.steps.map((st) =>
    st.index === 0 && !st.sameSocket ? { ...st, occupyCommitted: true } : { ...st }
  );
  const planId = `P${String(s.planSeq + 1).padStart(5, '0')}`;
  return {
    facts: [{ type: 'migrationCreated', at, planId, circuitId, reeferIds: proposal.affected.map((a) => a.reeferId), steps }],
    reply: {
      committed: true,
      planId,
      circuitId,
      steps,
      unassigned: proposal.unassigned,
      affected: proposal.affected,
    },
  };
}

function getPlan(s, planId) {
  const plan = s.plans.get(planId);
  if (!plan) throw notFound(`迁移方案不存在: ${planId}`);
  if (plan.state !== 'ACTIVE') throw conflict(`方案 ${planId} 已${plan.state === 'COMPLETED' ? '完成' : '取消'}`, {
    code: `PLAN_${plan.state}`,
  });
  return plan;
}

// 必须按步骤序号逐步执行：前一步未完成，后一步不得开始
function enforceOrder(plan, index) {
  if (index > 0 && plan.steps[index - 1].state !== 'DONE') {
    throw conflict(`步骤 ${index - 1} 尚未完成，不能开始步骤 ${index}`, { code: 'STEP_ORDER' });
  }
}

export function cmdMigrationStep(s, body, at) {
  const planId = checkId(body.planId, '迁移方案');
  const index = num(body.index, '步骤序号', { min: 0, integer: true });
  const action = String(body.action ?? '').toLowerCase();
  const plan = getPlan(s, planId);
  const st = plan.steps[index];
  if (!st) throw notFound(`步骤 ${index} 不存在`);
  enforceOrder(plan, index);

  const expected = st.index === 0 && !st.sameSocket ? 'PENDING' : null;

  if (action === 'unplug') {
    if (st.sameSocket) throw conflict('同插座复位步骤无需拔除，直接 repower', { code: 'SAME_SOCKET_STEP' });
    if (st.state !== 'PENDING' && st.state !== 'OCCUPIED') {
      // 步骤 0 预占后状态仍记 PENDING（租约已建），允许直接拔除
      if (!(st.index === 0 && st.occupyCommitted && st.state === 'PENDING')) {
        throw conflict(`步骤 ${index} 状态为 ${st.state}，无法拔除`, { code: `STEP_${st.state}` });
      }
    }
    const orig = s.leases.find(
      (l) => l.reeferId === st.reeferId && l.socketId === st.fromSocketId && l.state === 'FROZEN'
    );
    if (!orig) throw conflict(`箱 ${st.reeferId} 在原插座上没有 FROZEN 租约`, { code: 'NO_FROZEN_LEASE' });
    const coreFacts = [{ type: 'stepAdvanced', at, planId, index, action: 'unplug', leaseId: orig.id }];
    // FROZEN 通常来自跳闸（掉电已计数、证书已失效）；对未登记掉电的拔除做兜底失效
    const swept = sweepAfterCoreFacts(s, at, coreFacts, [st.reeferId]);
    return {
      facts: [...coreFacts, ...swept.facts],
      reply: { planId, index, action, state: 'UNPLUGGED', invalidations: swept.invalidations },
    };
  }

  if (action === 'occupy') {
    if (st.sameSocket) throw conflict('同插座复位步骤无需占用目标', { code: 'SAME_SOCKET_STEP' });
    if (st.state !== 'PENDING' && st.state !== 'UNPLUGGED') {
      throw conflict(`步骤 ${index} 状态为 ${st.state}，目标已占用或已完成`, { code: `STEP_${st.state}` });
    }
    const holder = activeLeaseOnSocket(s, st.targetSocketId);
    if (holder && holder.reeferId !== st.reeferId) {
      throw conflict(`目标插座 ${st.targetSocketId} 已被箱 ${holder.reeferId} 抢占`, {
        code: 'SOCKET_OCCUPIED',
        holder: holder.reeferId,
      });
    }
    // 步骤 0 的目标在方案创建时已预占
    if (st.index === 0 && st.occupyCommitted) {
      return { facts: [], reply: { planId, index, action, state: 'OCCUPIED', preCommitted: true } };
    }
    return {
      facts: [{ type: 'stepAdvanced', at, planId, index, action: 'occupy' }],
      reply: { planId, index, action, state: 'OCCUPIED' },
    };
  }

  if (action === 'repower') {
    const claim = st.claimLeaseId ? leaseById(s, st.claimLeaseId) : null;
    let oldLeaseId = null;
    if (st.sameSocket) {
      const orig = s.leases.find(
        (l) => l.reeferId === st.reeferId && l.socketId === st.fromSocketId && (l.state === 'FROZEN' || l.state === 'UNPLUGGED')
      );
      if (!orig) throw conflict('未找到原 FROZEN/UNPLUGGED 租约', { code: 'NO_ORIGIN_LEASE' });
      oldLeaseId = orig.id;
    } else {
      if (!claim || claim.state !== 'CLAIM') {
        throw conflict(`步骤 ${index} 尚未占用目标插座，不能送电`, { code: 'TARGET_NOT_CLAIMED' });
      }
      const orig = s.leases.find(
        (l) => l.reeferId === st.reeferId && l.socketId === st.fromSocketId && ['FROZEN', 'UNPLUGGED'].includes(l.state)
      );
      if (!orig) throw conflict('原租约缺失或已释放，不能完成迁移', { code: 'NO_ORIGIN_LEASE' });
      oldLeaseId = orig.id;
    }
    // 容量复核（目标回路余量必须仍满足）
    const targetCircuit = s.circuits.get(st.targetCircuitId);
    if (targetCircuit.state !== 'NORMAL') {
      throw conflict(`目标回路 ${targetCircuit.id} 不可用（${targetCircuit.state}）`, { code: 'TARGET_CIRCUIT_DOWN' });
    }
    const reefer = s.reefers.get(st.reeferId);
    const poweredPart = committedDemandA(s, targetCircuit.id, {}, at);
    if (round2(poweredPart + reefer.ratedCurrentA) > targetCircuit.ratedCurrentA) {
      throw conflict(`目标回路 ${targetCircuit.id} 余量不足`, {
        code: 'CIRCUIT_CAPACITY',
        loadA: poweredPart,
        ratedA: targetCircuit.ratedCurrentA,
      });
    }
    return {
      facts: [{ type: 'stepAdvanced', at, planId, index, action: 'repower', oldLeaseId }],
      reply: { planId, index, action, state: 'DONE', targetSocketId: st.targetSocketId },
    };
  }

  throw bad(`未知迁移动作: ${action}（允许 unplug/occupy/repower）`);
}

export function cmdMigrationCancel(s, body, at) {
  const planId = checkId(body.planId, '迁移方案');
  getPlan(s, planId);
  return {
    facts: [{ type: 'migrationCancelled', at, planId }],
    reply: { cancelled: true, planId },
  };
}

export function cmdCircuitReset(s, body, at) {
  const circuitId = checkId(body.circuitId, '回路');
  const c = s.circuits.get(circuitId);
  if (!c) throw notFound(`回路不存在: ${circuitId}`);
  if (c.state !== 'TRIPPED') return { facts: [], reply: { reset: false, alreadyNormal: true } };
  return {
    facts: [{ type: 'circuitReset', at, circuitId }],
    reply: { reset: true, circuitId },
  };
}

export function cmdState(s, _body, at) {
  return { facts: [], reply: buildView(s, at) };
}
