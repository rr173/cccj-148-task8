'use strict';

import { round2 } from '../util.js';
import { poweredLease } from '../domain.js';
import { effectiveCurrentA } from '../views.js';
// ---------- 削峰动作索引 ----------
// 活动动作 = EFFECTIVE（已生效）。PENDING 是尚未执行的建议，不参与当前负载；
// 已被取代(SUPERSEDED)/撤销(CANCELLED)/恢复(RESTORED)/失效(INVALIDATED) 均不再活动。
export function activeActionOf(s, reeferId) {
  return s.shavingActions.find((a) => a.reeferId === reeferId && a.status === 'EFFECTIVE') || null;
}
export function pendingActionOf(s, reeferId) {
  return s.shavingActions.find((a) => a.reeferId === reeferId && a.status === 'PENDING') || null;
}
export function activeActions(s) {
  return s.shavingActions.filter((a) => a.status === 'EFFECTIVE');
}

export function boardOfReefer(s, reeferId) {
  const lease = poweredLease(s, reeferId);
  if (!lease) return null;
  const socket = s.sockets.get(lease.socketId);
  if (!socket) return null;
  const circuit = s.circuits.get(socket.circuitId);
  return circuit ? circuit.boardId : null;
}

function baseCurrentA(s, reefer, at, override = null) {
  const lease = poweredLease(s, reefer.id);
  if (!lease) return 0;
  // 受控箱的毛负载锁定为“受控前基线”（执行时记录），
  // 避免传感器反馈已降低的电流后再减一次释放量，造成重复扣减/负净载。
  if (override && override.basisA != null) return round2(override.basisA);
  const active = activeActionOf(s, reefer.id);
  if (active && active.basisCurrentA != null) return round2(active.basisCurrentA);
  return effectiveCurrentA(s, lease.socketId, at);
}

// 动作作用下箱的净取用电流（不低于 0）
function netFor(baseA, action) {
  if (!action) return baseA;
  if (action.kind === 'CUT') return 0;
  if (action.kind === 'LIMIT') {
    const held = action.heldA ?? (baseA * (action.gearFraction ?? 0.4));
    return round2(Math.max(0, baseA - held));
  }
  return baseA;
}

// 计算全船与各配电板的毛负载（未削峰）与净负载（削峰后）。
// overrides: Map reeferId -> 动作|null，用于在“假设动作已生效”的状态上做规划投影。
//   动作对象至少含 {kind, heldA?, gearFraction?, basisA?}；显式 null 表示“已恢复/无动作”。
export function projectedLoads(s, at, overrides = new Map()) {
  const boards = new Map();
  let shipGross = 0;
  let shipNet = 0;

  for (const r of s.reefers.values()) {
    const lease = poweredLease(s, r.id);
    if (!lease) continue;
    const boardId = boardOfReefer(s, r.id);
    const hasOverride = overrides.has(r.id);
    const action = hasOverride ? (overrides.get(r.id) || null) : activeActionOf(s, r.id);
    const baseA = baseCurrentA(s, r, at, hasOverride ? overrides.get(r.id) : null);
    const netA = netFor(baseA, action);
    const releasedA = round2(baseA - netA);
    shipGross += baseA;
    shipNet += netA;
    if (boardId) {
      const b = boards.get(boardId) || { boardId, grossA: 0, netA: 0, reefers: [] };
      b.grossA = round2(b.grossA + baseA);
      b.netA = round2(b.netA + netA);
      b.reefers.push({ reeferId: r.id, baseA, netA, releasedA, actionKind: action?.kind ?? null });
      boards.set(boardId, b);
    }
  }

  return {
    shipGrossA: round2(shipGross),
    shipNetA: round2(shipNet),
    shipReleasedA: round2(shipGross - shipNet),
    boards: [...boards.values()].sort((a, b) => a.boardId.localeCompare(b.boardId)),
  };
}

// 缺口：返回正值（A）表示仍需释放的电流；全船与每块配电板分别给出
export function deficits(loads, budget) {
  const ship = budget.shipLimitA != null ? round2(loads.shipNetA - budget.shipLimitA) : 0;
  const boards = {};
  for (const b of loads.boards) {
    const lim = budget.boardLimitsA?.[b.boardId];
    if (lim != null) boards[b.boardId] = round2(b.netA - lim);
  }
  return { shipDeficitA: Math.max(0, ship), boardDeficitA: boards };
}

export function anyDeficit(d) {
  return d.shipDeficitA > 0 || Object.values(d.boardDeficitA).some((x) => x > 0);
}

// 计划基线签名：刻画“当前读数 + 温度趋势”。发布方案时随修订保存，
// 之后若签名变化（电流或温度变化使原计算失效），值班屏可标 stale 并提示重算。
export function basisSignature(s, at, risks) {
  const items = [];
  for (const r of [...s.reefers.values()].sort((a, b) => a.id.localeCompare(b.id))) {
    const lease = poweredLease(s, r.id);
    if (!lease) continue;
    const rk = risks.get(r.id);
    items.push({
      id: r.id,
      // 用“现场当前取用电流”（新鲜实测优先，否则铭牌）刻画变化，
      // 而非受控基线：受控箱实测电流变化同样意味着原计算失效。
      a: Math.round(effectiveCurrentA(s, lease.socketId, at) * 10) / 10,
      t: rk?.currentC != null ? Math.round(rk.currentC * 10) / 10 : null,
      trend: Math.round((rk?.trendRateCph ?? 0) * 10) / 10,
    });
  }
  return JSON.stringify(items);
}
