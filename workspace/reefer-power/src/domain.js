'use strict';

import { normPhase, num, round2, nameplateCurrentA, bad, DomainError } from './util.js';
import { applyPrecoolFact, precoolInvariants } from './precool/projection.js';

// 状态形态：
// {
//   boards:{id:{id,name}}, circuits:{id:{...}}, sockets:{id:{...}},
//   reefers:{id:{...}},
//   reservations:{socketId: {reeferId, leaseId}},   // RESERVE 租约的映射（活动）
//   leases:[ {id, reeferId, socketId, state, fromLeaseId?} ],
//   plans:{id:{...steps}},
//   samples:{ [key]:{...} },                        // 去重后的样本（含隔离/重复）
//   latestCurrent:{socketId:{...}}, powerLossAt:{reeferId:ts},
//   seq: number,
//
//   // —— 分时功率预算与冷藏负荷削峰 ——
//   budgets:[ {id,operatorId,startAt,endAt,shipLimitA,boardLimitsA,registeredAt,revokedAt} ],
//   budgetSeq, peakSeq,
//   revisions:[ {rev,operatorId,at,summary,status,basisSignature,budget:{...},feasible,...} ],
//   shavingActions:[
//     {id,rev,reeferId,boardId,kind,status:PENDING/EFFECTIVE/INVALIDATED/CANCELLED/SUPERSEDED/RESTORED,
//      gearFraction,plannedHeldA,heldA,basisCurrentA,currentA,releaseA,incrementalReleaseA,
//      durationMin,restoreBy,replacesActionId,supersededActionIds:[],executedAt,supersededAt,cancelledAt,restoredAt}
//   ],
//   restoreBatches:[ {id,at,sessionId,state,items,total,recoveredCount,paused,revisionPublished,energySavedAmin} ],
//   energySavedAmin                                   // 累计节省电量（A·min），仅恢复时一次性记账
//
//   // —— 装船前预冷合格证与放行互锁 ——
//   precoolObs:{ [voyageId|reeferId]: observation },
//   precoolCerts:[ certificate ],                     // VALID -> INVALIDATED（只追加，永不复活）
//   precoolWaivers:[ exemption ],                      // ACTIVE -> REVOKED/EXPIRED（只追加）
//   precoolScans:[ gateScan ],                        // 装船确认/岸桥扫描记录（只追加）
//   precoolPending:[ pendingEntry ],                  // 温控待处理队列
//   powerLossCount:{reeferId:n},                      // 供电中断分组计数（每次 POWER->失电 +1）
//   powerRestoredAt:{reeferId:ts},                    // 最近一次恢复供电时刻
//   setpointHistory:{reeferId:[{fromC,toC,at,operatorId}]},
//   precoolSeq                                        // 预冷对象统一序号（证书/豁免/扫描/队列）
// }
export function initialState() {
  return {
    boards: new Map(),
    circuits: new Map(),
    sockets: new Map(),
    reefers: new Map(),
    reservations: new Map(),
    leases: [],
    plans: new Map(),
    samples: new Map(),
    latestCurrent: new Map(),
    powerLossAt: new Map(),
    seq: 0,
    planSeq: 0,
    budgets: [],
    budgetSeq: 0,
    peakSeq: 0,
    revisions: [],
    shavingActions: [],
    restoreBatches: [],
    energySavedAmin: 0,
    precoolObs: new Map(),
    precoolCerts: [],
    precoolWaivers: [],
    precoolScans: [],
    precoolPending: [],
    powerLossCount: new Map(),
    powerRestoredAt: new Map(),
    setpointHistory: new Map(),
    precoolSeq: 0,
  };
}

export function assertInvariants(s, tag) {
  // 任一时刻一个插座只能服务一个箱（活动租约唯一）
  const bySocket = new Map();
  const byReefer = new Map();
  for (const l of s.leases) {
    if (leaseClosed(l)) continue;
    const a = bySocket.get(l.socketId) || [];
    a.push(l);
    bySocket.set(l.socketId, a);
    const b = byReefer.get(l.reeferId) || [];
    b.push(l);
    byReefer.set(l.reeferId, b);
  }
  for (const [sid, ls] of bySocket) {
    if (ls.length > 1) {
      throw new Error(`不变量被破坏 (${tag}): 插座 ${sid} 双重占用 ${ls.map((l) => `${l.reeferId}:${l.state}`)}`);
    }
  }
  for (const [rid, ls] of byReefer) {
    // 迁移期间允许同一箱同时持有 FROZEN(原) 与 CLAIM(目标)，其余情况唯一
    const nonMigPair = ls.filter((l) => l.state !== 'FROZEN' && l.state !== 'CLAIM');
    if (nonMigPair.length > 1) {
      throw new Error(`不变量被破坏 (${tag}): 箱 ${rid} 占用多个插座`);
    }
    if (ls.length > 2) {
      throw new Error(`不变量被破坏 (${tag}): 箱 ${rid} 租约数 ${ls.length}`);
    }
    if (ls.length === 2) {
      const st = ls.map((l) => l.state).sort().join('+');
      // 迁移中间态：目标 CLAIM + 原 FROZEN（刚占位）或 UNPLUGGED（已拔除待送电）
      if (st !== 'CLAIM+FROZEN' && st !== 'CLAIM+UNPLUGGED') {
        throw new Error(`不变量被破坏 (${tag}): 箱 ${rid} 非法租约组合 ${st}`);
      }
    }
  }
  // 预留映射与租约一致
  for (const [sid, r] of s.reservations) {
    const l = leaseById(s, r.leaseId);
    if (!l || leaseClosed(l) || l.socketId !== sid) throw new Error(`不变量被破坏 (${tag}): 预留映射漂移 ${sid}`);
  }
  // 计划步骤状态一致性
  for (const plan of s.plans.values()) {
    for (const st of plan.steps) {
      if (st.state === 'PENDING') {
        // 步骤 0 在方案创建时即预占目标插座（CLAIM），属设计内状态
        if (st.claimLeaseId && st.occupyCommitted) continue;
        if (st.claimLeaseId) throw new Error(`不变量被破坏 (${tag}): PENDING 步骤持有租约`);
      }
      if (st.state === 'DONE') {
        const l = leaseById(s, st.newLeaseId);
        if (!l || l.state !== 'POWER' || l.socketId !== st.targetSocketId) {
          throw new Error(`不变量被破坏 (${tag}): 计划 ${plan.id} 步骤 ${st.index} 已完成但目标未送电`);
        }
      }
    }
    if (plan.state === 'COMPLETED') {
      for (const reeferId of plan.reeferIds) {
        const r = s.reefers.get(reeferId);
        if (r && !poweredLease(s, reeferId)) {
          throw new Error(`不变量被破坏 (${tag}): 计划 ${plan.id} 已完成但箱 ${reeferId} 仍断电`);
        }
      }
    }
  }

  // —— 削峰不变量 ——
  // 每个箱至多一个已生效动作、至多一个待执行动作；
  // 待执行“升级断电”允许与已生效限功率短暂并存（此时箱由已生效动作控制）。
  const effByReefer = new Map();
  const pendByReefer = new Map();
  for (const a of s.shavingActions) {
    if (a.status === 'EFFECTIVE') {
      const cur = effByReefer.get(a.reeferId);
      if (cur) throw new Error(`不变量被破坏 (${tag}): 箱 ${a.reeferId} 有两个生效削峰动作 ${cur} / ${a.id}`);
      effByReefer.set(a.reeferId, a.id);
    }
    if (a.status === 'PENDING') {
      const cur = pendByReefer.get(a.reeferId);
      if (cur) throw new Error(`不变量被破坏 (${tag}): 箱 ${a.reeferId} 有两个待执行动作 ${cur} / ${a.id}`);
      pendByReefer.set(a.reeferId, a.id);
    }
    if (a.replacesActionId) {
      const old = s.shavingActions.find((x) => x.id === a.replacesActionId);
      if (!old || old.reeferId !== a.reeferId) {
        throw new Error(`不变量被破坏 (${tag}): 动作 ${a.id} 的 replacesActionId 引用非法`);
      }
    }
    for (const sid of a.supersededActionIds || []) {
      const old = s.shavingActions.find((x) => x.id === sid);
      if (!old || old.reeferId !== a.reeferId || old.status !== 'SUPERSEDED') {
        throw new Error(`不变量被破坏 (${tag}): 动作 ${a.id} 标记的取代动作 ${sid} 不一致`);
      }
    }
  }

  precoolInvariants(s, tag);
}

export function leaseClosed(l) {
  return l.state === 'RELEASED' || l.state === 'SUPERSEDED';
}
export function activeLeases(s) {
  return s.leases.filter((l) => !leaseClosed(l));
}
export function leaseById(s, id) {
  return s.leases.find((l) => l.id === id) || null;
}
export function activeLeaseOnSocket(s, socketId) {
  return s.leases.find((l) => l.socketId === socketId && !leaseClosed(l)) || null;
}
export function activeLeaseOfReefer(s, reeferId) {
  return s.leases.find((l) => l.reeferId === reeferId && !leaseClosed(l)) || null;
}
export function activeLeasesOfReefer(s, reeferId) {
  return s.leases.filter((l) => l.reeferId === reeferId && !leaseClosed(l));
}
export function poweredLease(s, reeferId) {
  return s.leases.find((l) => l.reeferId === reeferId && l.state === 'POWER') || null;
}

// ---------- 事实归并（确定性纯函数） ----------
export function applyFact(s, fact) {
  switch (fact.type) {
    case 'topologyImported': return applyTopology(s, fact);
    case 'reefersImported': return applyReefers(s, fact);
    case 'reserved': return applyReserved(s, fact);
    case 'powered': return applyPowered(s, fact);
    case 'sampleRecorded': return applySample(s, fact);
    case 'tripped': return applyTripped(s, fact);
    case 'circuitReset': return applyReset(s, fact);
    case 'migrationCreated': return applyMigrationCreated(s, fact);
    case 'stepAdvanced': return applyStepAdvanced(s, fact);
    case 'migrationCancelled': return applyMigrationCancelled(s, fact);
    case 'clockAdvanced': return s; // 仅用于持久化测试虚拟时钟偏移（store 负责 setClockOffset）
    case 'budgetRegistered': return applyBudgetRegistered(s, fact);
    case 'budgetRevoked': return applyBudgetRevoked(s, fact);
    case 'peakPlanPublished': return applyPeakPlanPublished(s, fact);
    case 'shavingActionExecuted': return applyShavingActionExecuted(s, fact);
    case 'shavingActionsInvalidated': return applyShavingActionsInvalidated(s, fact);
    case 'shavingActionsCancelled': return applyShavingActionsCancelled(s, fact);
    case 'restoreBatchRecorded': return applyRestoreBatch(s, fact);
    case 'setpointAdjusted': return applySetpointAdjusted(s, fact);
    default:
      if (fact.type.startsWith('precool')) return applyPrecoolFact(s, fact);
      throw new Error(`未知事实类型: ${fact.type}`);
  }
}

// 设定点调整：更新箱设定点并追加只增的设定点变更历史（预冷证书/观测据此失效/重计时）
function applySetpointAdjusted(s, f) {
  const r = s.reefers.get(f.reeferId);
  if (!r) throw new Error('设定点调整事实引用了未知箱');
  const fromC = r.setpointC;
  r.setpointC = num(f.toC, '设定点(℃)', { min: -100, max: 100 });
  const hist = s.setpointHistory.get(f.reeferId) || [];
  hist.push({ at: f.at, fromC, toC: r.setpointC, operatorId: f.operatorId });
  s.setpointHistory.set(f.reeferId, hist);
  return s;
}

function nextLeaseId(s) {
  s.seq += 1;
  return `L${String(s.seq).padStart(5, '0')}`;
}

// 登记一次「供电中断分组」：仅当该箱当前不在已登记的断电中时计数 +1。
// 同一轮掉电（跳闸、powerLoss 样本、拔除）不会重复计数；重新送电后再次掉电算新一组。
export function registerPowerLoss(s, reeferId, at) {
  if (s.powerLossAt.get(reeferId) != null) return false;
  s.powerLossAt.set(reeferId, at);
  s.powerLossCount.set(reeferId, (s.powerLossCount.get(reeferId) ?? 0) + 1);
  return true;
}

// 登记恢复供电：清掉电时刻并记录最近恢复时刻（预冷观测据此重新计时）
export function registerPowerRestored(s, reeferId, at) {
  s.powerLossAt.delete(reeferId);
  s.powerRestoredAt.set(reeferId, at);
}

function applyTopology(s, f) {
  for (const b of f.boards) {
    s.boards.set(b.id, { id: b.id, name: b.name });
  }
  for (const c of f.circuits) {
    const prev = s.circuits.get(c.id);
    s.circuits.set(c.id, {
      id: c.id,
      boardId: c.boardId,
      name: c.name,
      phase: normPhase(c.phase),
      ratedCurrentA: num(c.ratedCurrentA, '回路额定电流', { min: 0.1 }),
      state: prev?.state === 'TRIPPED' ? 'TRIPPED' : 'NORMAL',
      trippedAt: prev?.trippedAt || null,
    });
  }
  for (const sk of f.sockets) {
    const circuit = s.circuits.get(sk.circuitId);
    if (!circuit) throw new Error(`插座 ${sk.id} 引用了未知回路 ${sk.circuitId}`);
    s.sockets.set(sk.id, {
      id: sk.id,
      circuitId: sk.circuitId,
      name: sk.name,
      phase: sk.phase ? normPhase(sk.phase) : circuit.phase,
      ratedCurrentA: sk.ratedCurrentA != null
        ? num(sk.ratedCurrentA, '插座额定电流', { min: 0.1 })
        : circuit.ratedCurrentA,
      allowedBoxTypes: sk.allowedBoxTypes,
      reachAreas: sk.reachAreas,
    });
  }
  return s;
}

function applyReefers(s, f) {
  for (const r of f.reefers) {
    const phase = normPhase(r.phase);
    s.reefers.set(r.id, {
      id: r.id,
      boxType: r.boxType,
      phase,
      ratedCurrentA: r.ratedCurrentA != null
        ? round2(num(r.ratedCurrentA, '箱额定电流', { min: 0.1 }))
        : nameplateCurrentA(r.powerKw, phase),
      powerKw: r.powerKw != null ? Number(r.powerKw) : null,
      area: r.area,
      tempPriority: num(r.tempPriority, '温控优先级', { min: 1, max: 99, integer: true }),
      insulationHours: num(r.insulationHours, '保温时限(小时)', { min: 0.01 }),
      setpointC: r.setpointC != null ? Number(r.setpointC) : null,
      cargoGrade: String(r.cargoGrade ?? 'GENERAL').toUpperCase(),
      allowableRiseC: r.allowableRiseC != null ? num(r.allowableRiseC, '允许温升(℃)', { min: 0.1 }) : null,
    });
  }
  return s;
}

function applyReserved(s, f) {
  const reefer = s.reefers.get(f.reeferId);
  const socket = s.sockets.get(f.socketId);
  if (!reefer || !socket) throw new Error('预留事实引用了未知箱或插座');
  if (activeLeaseOnSocket(s, f.socketId)) throw new Error('预留事实与既有插座租约冲突');
  if (activeLeaseOfReefer(s, f.reeferId)) throw new Error('预留事实与既有箱租约冲突');
  const leaseId = nextLeaseId(s);
  s.leases.push({ id: leaseId, reeferId: f.reeferId, socketId: f.socketId, state: 'RESERVE' });
  s.reservations.set(f.socketId, { reeferId: f.reeferId, leaseId });
  return s;
}

function applyPowered(s, f) {
  const lease = leaseById(s, f.leaseId);
  if (!lease || lease.state !== 'RESERVE') throw new Error('送电事实要求 RESERVE 租约');
  lease.state = 'POWER';
  s.reservations.delete(lease.socketId);
  return s;
}

function applySample(s, f) {
  s.samples.set(f.key, {
    key: f.key,
    kind: f.kind,
    sensorBootId: f.sensorBootId,
    reeferId: f.reeferId ?? null,
    socketId: f.socketId ?? null,
    circuitId: f.circuitId ?? null,
    measuredAt: f.measuredAt,
    receivedAt: f.receivedAt,
    value: f.value,
    accepted: f.accepted,
    reason: f.reason,
  });
  if (f.accepted && f.kind === 'current' && f.socketId) {
    s.latestCurrent.set(f.socketId, { value: f.value, measuredAt: f.measuredAt });
  }
  if (f.accepted && f.kind === 'powerLoss' && f.reeferId) {
    const prev = s.powerLossAt.get(f.reeferId);
    if (prev == null || f.measuredAt > prev) registerPowerLoss(s, f.reeferId, f.measuredAt);
  }
  return s;
}

function applyTripped(s, f) {
  const c = s.circuits.get(f.circuitId);
  if (!c) throw new Error('跳闸事实引用未知回路');
  c.state = 'TRIPPED';
  c.trippedAt = f.at;
  for (const l of activeLeases(s)) {
    if (l.state !== 'POWER') continue;
    const sk = s.sockets.get(l.socketId);
    if (sk && sk.circuitId === f.circuitId) {
      l.state = 'FROZEN';
      registerPowerLoss(s, l.reeferId, f.at);
    }
  }
  return s;
}

function applyReset(s, f) {
  const c = s.circuits.get(f.circuitId);
  if (!c) throw new Error('复位事实引用未知回路');
  c.state = 'NORMAL';
  c.trippedAt = null;
  for (const l of activeLeases(s)) {
    if (l.state !== 'FROZEN') continue;
    const sk = s.sockets.get(l.socketId);
    if (sk && sk.circuitId === f.circuitId) {
      // 该箱若正处于迁移中（目标已 CLAIM），不就地恢复，避免 POWER+CLAIM 双租约
      const inMigration = s.leases.some(
        (x) => x.reeferId === l.reeferId && x.state === 'CLAIM'
      );
      if (inMigration) continue;
      l.state = 'POWER';
      registerPowerRestored(s, l.reeferId, f.at);
    }
  }
  return s;
}

function applyMigrationCreated(s, f) {
  if (s.plans.has(f.planId)) throw new Error(`计划 ${f.planId} 已存在`);
  s.planSeq += 1;
  if (f.planId !== `P${String(s.planSeq).padStart(5, '0')}`) {
    throw new Error(`计划编号不确定: ${f.planId}`);
  }
  const steps = f.steps.map((st, i) => {
    if (st.occupyCommitted) {
      const claimId = nextLeaseId(s);
      s.leases.push({
        id: claimId,
        reeferId: st.reeferId,
        socketId: st.targetSocketId,
        state: 'CLAIM',
      });
      return { ...st, index: i, state: 'PENDING', claimLeaseId: claimId, newLeaseId: null };
    }
    return { ...st, index: i, state: 'PENDING', claimLeaseId: null, newLeaseId: null };
  });
  s.plans.set(f.planId, {
    id: f.planId,
    circuitId: f.circuitId,
    reeferIds: f.reeferIds,
    steps,
    state: 'ACTIVE',
    createdAt: f.at,
    completedAt: null,
    cancelledAt: null,
  });
  return s;
}

function applyStepAdvanced(s, f) {
  const plan = s.plans.get(f.planId);
  if (!plan) throw new Error('步骤事实引用未知计划');
  const st = plan.steps[f.index];
  if (!st) throw new Error('步骤事实索引越界');

  if (f.action === 'unplug') {
    const orig = leaseById(s, f.leaseId);
    if (!orig || orig.state !== 'FROZEN') throw new Error('拔除要求 FROZEN 租约');
    orig.state = 'UNPLUGGED';
    st.state = 'UNPLUGGED';
  } else if (f.action === 'occupy') {
    const claimId = nextLeaseId(s);
    s.leases.push({
      id: claimId,
      reeferId: st.reeferId,
      socketId: st.targetSocketId,
      state: 'CLAIM',
    });
    st.claimLeaseId = claimId;
    st.state = 'OCCUPIED';
  } else if (f.action === 'repower') {
    if (st.claimLeaseId) {
      const claim = leaseById(s, st.claimLeaseId);
      if (!claim || claim.state !== 'CLAIM') throw new Error('送电要求 CLAIM 租约');
      claim.state = 'POWER';
      st.newLeaseId = claim.id;
    } else {
      // 同一插座复位送电（步骤 0 或原=目标）
      const orig = leaseById(s, f.leaseId);
      if (!orig || (orig.state !== 'FROZEN' && orig.state !== 'UNPLUGGED')) {
        throw new Error('送电要求 FROZEN/UNPLUGGED 租约');
      }
      orig.state = 'POWER';
      st.newLeaseId = orig.id;
    }
    const oldLease = leaseById(s, f.oldLeaseId);
    if (oldLease && oldLease.id !== st.newLeaseId) {
      oldLease.state = 'SUPERSEDED';
      registerPowerRestored(s, st.reeferId, f.at);
    }
    if (oldLease && oldLease.id === st.newLeaseId) {
      registerPowerRestored(s, st.reeferId, f.at);
    }
    st.state = 'DONE';
  } else {
    throw new Error(`未知步骤动作: ${f.action}`);
  }

  if (plan.steps.every((x) => x.state === 'DONE')) {
    plan.state = 'COMPLETED';
    plan.completedAt = f.at;
  }
  return s;
}

function applyMigrationCancelled(s, f) {
  const plan = s.plans.get(f.planId);
  if (!plan) throw new Error('取消事实引用未知计划');
  for (const st of plan.steps) {
    if (st.state === 'DONE') continue;
    if (st.claimLeaseId) {
      const claim = leaseById(s, st.claimLeaseId);
      if (claim && claim.state === 'CLAIM') {
        claim.state = 'RELEASED';
        st.claimLeaseId = null;
      }
    }
    // 已拔除但未完成：回到 FROZEN，箱仍处于断电（原插座未释放）
    const orig = s.leases.find(
      (l) => l.reeferId === st.reeferId && l.socketId === st.fromSocketId && (l.state === 'UNPLUGGED' || l.state === 'FROZEN')
    );
    if (orig) {
      orig.state = 'FROZEN';
      st.state = 'CANCELLED';
    } else if (st.state !== 'CANCELLED') {
      st.state = 'CANCELLED';
    }
  }
  plan.state = 'CANCELLED';
  plan.cancelledAt = f.at;
  return s;
}

export { bad, DomainError };

// ============================================================================
// 分时功率预算与冷藏负荷削峰：事实归并（确定性纯函数）
// ============================================================================
function nextBudgetId(s) {
  s.budgetSeq += 1;
  return `BG${String(s.budgetSeq).padStart(4, '0')}`;
}
function nextActionId(s) {
  s.peakSeq += 1;
  return `A${String(s.peakSeq).padStart(5, '0')}`;
}
function nextBatchId(s) {
  s.peakSeq += 1;
  return `RB${String(s.peakSeq).padStart(5, '0')}`;
}

function applyBudgetRegistered(s, f) {
  if (s.budgets.some((b) => b.id === f.budgetId)) throw new Error(`预算 ${f.budgetId} 已存在`);
  s.budgetSeq += 1;
  if (f.budgetId !== `BG${String(s.budgetSeq).padStart(4, '0')}`) {
    throw new Error(`预算编号不确定: ${f.budgetId}`);
  }
  s.budgets.push({
    id: f.budgetId,
    operatorId: f.operatorId,
    startAt: f.startAt,
    endAt: f.endAt,
    shipLimitA: f.shipLimitA,
    boardLimitsA: f.boardLimitsA || {},
    registeredAt: f.at,
    revokedAt: null,
  });
  return s;
}

function applyBudgetRevoked(s, f) {
  const b = s.budgets.find((x) => x.id === f.budgetId);
  if (!b) throw new Error('撤销事实引用未知预算');
  if (b.revokedAt) return s; // 幂等
  b.revokedAt = f.at;
  return s;
}

// 发布削峰修订：把上一版尚未执行(PENDING)的动作整体作废旧版本号；
// 已经生效(EFFECTIVE)的限功率/断电事实保留。每个动作生成稳定标识。
function applyPeakPlanPublished(s, f) {
  if (s.revisions.some((r) => r.rev === f.rev)) throw new Error(`修订号 ${f.rev} 已存在`);
  const prevActive = f.supersedesRev || (s.revisions.length ? s.revisions[s.revisions.length - 1].rev : 0);

  // 旧修订中尚未执行的动作撤销（仅撤销未执行动作，已生效事实不消失）
  for (const a of s.shavingActions) {
    if (a.status === 'PENDING' && a.rev === prevActive) {
      a.status = 'INVALIDATED';
      a.invalidatedAt = f.at;
      a.invalidateReason = 'SUPERSEDED_BY_REVISION';
      a.invalidatedByRev = f.rev;
    }
  }

  for (const act of f.actions) {
    const id = nextActionId(s);
    s.shavingActions.push({
      id,
      rev: f.rev,
      reeferId: act.reeferId,
      boardId: act.boardId,
      socketId: act.socketId || null,
      circuitId: act.circuitId || null,
      kind: act.kind,
      status: 'PENDING',
      gearFraction: act.gearFraction ?? null,
      plannedHeldA: act.plannedHeldA ?? act.releaseA ?? null,
      basisCurrentA: act.basisCurrentA ?? null,
      releaseA: act.releaseA ?? null,
      incrementalReleaseA: act.incrementalReleaseA ?? null,
      durationMin: act.durationMin ?? null,
      restoreBy: act.restoreBy ?? null,
      safeCutMinutes: act.safeCutMinutes ?? null,
      replacesActionId: act.replacesActionId || null,
      supersededActionIds: [],
      executedAt: null,
      supersededAt: null,
      cancelledAt: null,
      invalidatedAt: null,
      restoredAt: null,
    });
  }

  s.revisions.push({
    rev: f.rev,
    operatorId: f.operatorId,
    at: f.at,
    summary: f.summary,
    status: f.feasible ? 'FEASIBLE' : 'INFEASIBLE',
    feasible: f.feasible,
    basisSignature: f.basisSignature,
    budget: f.budget,
    actionIds: [],
    actionDigests: f.actions.map((a) => `${a.kind}:${a.reeferId}`),
    deficitsAfter: f.deficitsAfter || null,
    infeasibleReasons: f.infeasibleReasons || [],
    skipReasons: f.skipReasons || [],
  });
  const rev = s.revisions[s.revisions.length - 1];
  rev.actionIds = s.shavingActions.filter((a) => a.rev === f.rev).map((a) => a.id);
  return s;
}

function getAction(s, actionId) {
  const a = s.shavingActions.find((x) => x.id === actionId);
  if (!a) throw new Error(`削峰动作不存在: ${actionId}`);
  return a;
}

// 执行限功率/断电：PENDING -> EFFECTIVE。断电升级时既有 LIMIT 事实标记为 SUPERSEDED 保留。
function applyShavingActionExecuted(s, f) {
  const a = getAction(s, f.actionId);
  if (a.status !== 'PENDING') throw new Error(`动作 ${a.id} 当前为 ${a.status}，不能执行`);

  if (a.kind === 'CUT' && a.replacesActionId) {
    const old = getAction(s, a.replacesActionId);
    if (old.status !== 'EFFECTIVE') throw new Error(`升级断电要求原限功率 ${old.id} 已生效，实际 ${old.status}`);
    // 被取代的限功率在退场时一次性结算已节省电量（与最终恢复结算互斥，不重复）
    const heldA = old.heldA ?? old.plannedHeldA ?? 0;
    const heldMin = Math.max(0, (f.at - (old.executedAt ?? f.at)) / 60000);
    s.energySavedAmin += heldA * heldMin;
    old.status = 'SUPERSEDED';
    old.supersededAt = f.at;
    old.supersededByActionId = a.id;
    old.energyAccruedAmin = round2(heldA * heldMin);
    a.supersededActionIds.push(old.id);
  }

  a.status = 'EFFECTIVE';
  a.executedAt = f.at;
  a.gearFraction = f.gearFraction ?? a.gearFraction;
  a.currentA = f.currentA;
  a.heldA = f.heldA;
  a.releaseA = f.releaseA;
  a.incrementalReleaseA = f.incrementalReleaseA ?? a.incrementalReleaseA;
  // 削峰短时断电也是真实掉电：登记一个断电分组，预冷合格证据此失效
  if (a.kind === 'CUT') registerPowerLoss(s, a.reeferId, f.at);
  return s;
}

// 计划失效重算：只撤销尚未执行的 PENDING 动作（按修订过滤），已生效事实不消失
function applyShavingActionsInvalidated(s, f) {
  for (const id of f.actionIds) {
    const a = getAction(s, id);
    if (a.status !== 'PENDING') continue;
    a.status = 'INVALIDATED';
    a.invalidatedAt = f.at;
    a.invalidateReason = f.reason || 'RECALC';
    a.invalidatedByRev = f.byRev ?? null;
  }
  return s;
}

// 操作员手动撤销方案：同样只撤销未执行动作
function applyShavingActionsCancelled(s, f) {
  for (const id of f.actionIds) {
    const a = getAction(s, id);
    if (a.status !== 'PENDING') continue;
    a.status = 'CANCELLED';
    a.cancelledAt = f.at;
  }
  return s;
}

// 恢复批次：把动作置为 RESTORED，按其实际持有的释放量一次性累计节省电量
function applyRestoreBatch(s, f) {
  for (const item of f.items) {
    const a = getAction(s, item.actionId);
    if (a.status !== 'EFFECTIVE') throw new Error(`恢复要求动作 ${a.id} 已生效，实际 ${a.status}`);
    a.status = 'RESTORED';
    a.restoredAt = f.at;
    a.restoredBatchId = f.batchId;
    // 削峰断电恢复：清掉电标记并记录恢复时刻（预冷观测需重新计时）
    if (a.kind === 'CUT') registerPowerRestored(s, a.reeferId, f.at);
    // 节省电量 = 实际释放电流(A) × 已执行时长(min)，仅在恢复时一次性记账，绝不重复
    const heldA = a.kind === 'LIMIT'
      ? (a.heldA ?? a.plannedHeldA ?? 0)
      : (a.releaseA ?? 0);
    const heldMin = Math.max(0, (f.at - (a.executedAt ?? f.at)) / 60000);
    s.energySavedAmin += heldA * heldMin;
  }
  s.restoreBatches.push({
    id: f.batchId,
    at: f.at,
    sessionId: f.sessionId,
    state: f.paused ? 'PAUSED' : (f.complete ? 'COMPLETED' : 'IN_PROGRESS'),
    items: f.items.map((x) => ({ ...x })),
    total: f.total,
    recoveredCount: f.recoveredCount,
    paused: f.paused,
    pauseReason: f.pauseReason || null,
    budgetActive: f.budgetActive,
    revisionPublished: f.revisionPublished || null,
  });
  return s;
}
