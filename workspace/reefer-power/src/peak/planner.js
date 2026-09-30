'use strict';

import { round2 } from '../util.js';
import { activeActionOf } from './load.js';
import { projectedLoads, deficits, anyDeficit, boardOfReefer } from './load.js';
import { safeCutMinutes, tierLabel } from './risk.js';

// 档位与断电安全常量
const LOW_MAX_GEAR = 0.65;   // 低风险箱限功率最多压缩 65% 取用电流
const MED_MAX_GEAR = 0.4;    // 中风险箱最多压缩 40%（且永不升级为断电）
const MIN_GEAR = 0.1;
const MIN_USEFUL_A = 0.5;    // 小于该释放量的限功率档无意义
const MIN_CUT_MIN = 2;       // 短时断电至少 2 分钟才有操作意义
const SAFETY_RESERVE_MIN = 1; // 恢复最迟时刻相对保冷极限再留 1 分钟余量

function candidateSocket(s, reeferId) {
  const lease = s.leases.find((l) => l.reeferId === reeferId && l.state === 'POWER');
  const socket = s.sockets.get(lease.socketId);
  const circuit = s.circuits.get(socket.circuitId);
  return { socketId: socket.id, circuitId: circuit.id, boardId: circuit.boardId };
}

// 在不越限前提下，为满足缺口选择“最温和但够用”的限功率档（压缩比例）
function pickGear(baseA, needA, maxGear) {
  if (baseA <= 0) return null;
  let gear = Math.ceil((needA / baseA) * 100) / 100;
  gear = Math.max(MIN_GEAR, Math.min(maxGear, gear));
  const held = round2(baseA * gear);
  if (held < MIN_USEFUL_A) return null;
  return { gearFraction: round2(gear), heldA: held };
}

// 规划削峰方案（纯函数，只读取状态，不做任何修改）。
// 不变量：每个箱在同一版方案中至多出现一个最终动作（先 LIMIT 后 CUT 时只保留 CUT）。
export function planShaving(s, budget, risks, at) {
  // 先尝试“仅限功率”即可达标（最温和）；达不到再允许对低风险箱短时断电。
  const limitOnly = runGreedy(s, budget, risks, at, { allowCuts: false });
  if (limitOnly.feasible) return limitOnly;
  return runGreedy(s, budget, risks, at, { allowCuts: true });
}

function runGreedy(s, budget, risks, at, { allowCuts }) {
  // overrides：投影动作。键为箱号，值为 {kind, heldA, gearFraction, basisA, _effectiveId?}
  const overrides = new Map();
  for (const a of s.shavingActions.filter((x) => x.status === 'EFFECTIVE')) {
    overrides.set(a.reeferId, {
      kind: a.kind,
      heldA: a.kind === 'LIMIT' ? (a.heldA ?? a.plannedHeldA) : a.releaseA,
      gearFraction: a.gearFraction,
      basisA: a.basisCurrentA,
      _effectiveId: a.id,
    });
  }
  // byReefer：本版方案为每个箱给出的最终动作（草稿对象，尚未生成稳定 id）
  const byReefer = new Map();
  const order = [];
  const skipReasons = [];
  const windowEndAt = budget.endAt;
  const windowMin = () => Math.max(0, (windowEndAt - at) / 60000);
  const recordSkip = (reeferId, phase, reason, extra = {}) =>
    skipReasons.push({ reeferId, phase, reason, ...extra });

  const upsertAction = (reeferId, action) => {
    if (!byReefer.has(reeferId)) order.push(reeferId);
    byReefer.set(reeferId, action);
  };
  const removeAction = (reeferId) => {
    byReefer.delete(reeferId);
    const i = order.indexOf(reeferId);
    if (i >= 0) order.splice(i, 1);
  };
  const snapshot = () => ({ loads: projectedLoads(s, at, overrides), d: deficits(projectedLoads(s, at, overrides), budget) });

  // 判断某箱能否安全短时断电（低风险且保冷时长足够）
  const cutInfo = (r, rk) => {
    if (rk.tier !== 1) return { cuttable: false, safeMin: safeCutMinutes(s, r, rk, at) };
    const safeMin = safeCutMinutes(s, r, rk, at);
    return { cuttable: round2(safeMin - SAFETY_RESERVE_MIN) >= MIN_CUT_MIN, safeMin };
  };

  // 当前绑定约束信息
  const context = (loads, d) => {
    const bindingBoards = new Set(Object.entries(d.boardDeficitA).filter(([, v]) => v > 0).map(([b]) => b));
    return { bindingBoards, shipBinding: d.shipDeficitA > 0 };
  };
  const itemOf = (loads, boardId, reeferId) =>
    loads.boards.find((b) => b.boardId === boardId)?.reefers.find((x) => x.reeferId === reeferId) || null;

  function addLimit(c, baseA) {
    upsertAction(c.reefer.id, {
      kind: 'LIMIT',
      reeferId: c.reefer.id,
      boardId: c.boardId,
      gearFraction: c.gearFraction,
      plannedHeldA: c.heldA,
      releaseA: c.heldA,
      incrementalReleaseA: c.heldA,
      basisCurrentA: baseA,
      durationMin: round2(windowMin()),
      restoreBy: windowEndAt,
      replacesActionId: null,
      risk: { tier: c.rk.tier, tierLabel: c.rk.tierLabel, score: c.rk.score },
    });
    overrides.set(c.reefer.id, { kind: 'LIMIT', heldA: c.heldA, gearFraction: c.gearFraction, basisA: baseA });
  }

  // 阶段 1：只对“不可断电”的箱（中风险，或低风险但保冷不足）安排限功率。
  // 可安全断电的低风险箱先保留，避免用 65% 限功率占位后阶段 2 无箱可断。
  for (let guard = 0; guard < s.reefers.size + 2; guard++) {
    const { loads, d } = snapshot();
    if (!anyDeficit(d)) break;
    const { bindingBoards, shipBinding } = context(loads, d);
    const cands = [];
    for (const r of s.reefers.values()) {
      const rk = risks.get(r.id);
      if (!rk || rk.tier >= 3) { if (guard === 0 && rk) recordSkip(r.id, 'LIMIT', 'HIGH_PROTECTED', { tier: rk.tierLabel }); continue; }
      if (overrides.has(r.id) || byReefer.has(r.id)) continue;
      const boardId = boardOfReefer(s, r.id);
      if (!boardId || (bindingBoards.size && !bindingBoards.has(boardId))) continue;
      const item = itemOf(loads, boardId, r.id);
      const baseA = item ? item.baseA : 0;
      if (baseA <= 0) continue;
      const { cuttable } = cutInfo(r, rk);
      // 允许断电时，把可安全断电的低风险箱先留给阶段 2；仅限功率模式则不保留
      if (allowCuts && cuttable) continue;
      const needA = Math.max(shipBinding ? d.shipDeficitA : 0, d.boardDeficitA[boardId] || 0);
      const pick = pickGear(baseA, needA, rk.tier === 1 ? LOW_MAX_GEAR : MED_MAX_GEAR);
      if (!pick) { recordSkip(r.id, 'LIMIT', 'RELEASE_TOO_SMALL', { baseA }); continue; }
      cands.push({ reefer: r, rk, boardId, baseA, ...pick });
    }
    if (!cands.length) break;
    cands.sort((a, b) =>
      a.rk.tier - b.rk.tier || a.rk.score - b.rk.score || b.heldA - a.heldA || a.reefer.id.localeCompare(b.reefer.id));
    const c = cands[0];
    addLimit(c, c.baseA);
  }

  if (allowCuts) {
  // 阶段 2：仍有缺口时，对“可安全断电”的低风险箱按风险从低到高短时断电。
  // 升级已生效限功率时增量取其剩余净载；取代本方案限功率时整体释放基线。
  for (let guard = 0; guard < s.reefers.size + 2; guard++) {
    const { loads, d } = snapshot();
    if (!anyDeficit(d)) break;
    const { bindingBoards } = context(loads, d);
    const cands = [];
    for (const r of s.reefers.values()) {
      const rk = risks.get(r.id);
      if (!rk) continue;
      if (rk.tier >= 3) { if (guard === 0) recordSkip(r.id, 'CUT', 'HIGH_PROTECTED', { tier: rk.tierLabel }); continue; }
      if (rk.tier === 2) { if (guard === 0) recordSkip(r.id, 'CUT', 'MEDIUM_NO_CUT', { tier: rk.tierLabel }); continue; }
      const boardId = boardOfReefer(s, r.id);
      if (!boardId || (bindingBoards.size && !bindingBoards.has(boardId))) continue;
      if (overrides.get(r.id)?.kind === 'CUT') continue;
      const item = itemOf(loads, boardId, r.id);
      if (!item) continue;
      const ov = overrides.get(r.id) || null;
      const hasEffectiveLimit = !!ov?._effectiveId;
      const { cuttable, safeMin } = cutInfo(r, rk);
      if (!cuttable) { recordSkip(r.id, 'CUT', 'UNSAFE_CUT', { safeMinutes: safeMin, spareRatio: rk.spareRatio }); continue; }
      const valueA = hasEffectiveLimit ? item.netA : item.baseA;
      if (valueA < MIN_USEFUL_A) continue;
      cands.push({ reefer: r, rk, boardId, baseA: item.baseA, valueA, netA: item.netA, effectiveId: hasEffectiveLimit ? ov._effectiveId : null });
    }
    if (!cands.length) break;
    cands.sort((a, b) => a.rk.score - b.rk.score || b.valueA - a.valueA || a.reefer.id.localeCompare(b.reefer.id));
    const c = cands[0];
    const { safeMin } = cutInfo(c.reefer, c.rk);
    const usableMin = round2(safeMin - SAFETY_RESERVE_MIN);
    const durationMin = round2(Math.min(usableMin, Math.max(MIN_CUT_MIN, round2(windowMin()))));
    const restoreBy = at + Math.round(durationMin * 60000);
    if (byReefer.has(c.reefer.id)) removeAction(c.reefer.id);
    upsertAction(c.reefer.id, {
      kind: 'CUT',
      reeferId: c.reefer.id,
      boardId: c.boardId,
      replacesActionId: c.effectiveId,
      releaseA: c.baseA,
      incrementalReleaseA: c.effectiveId ? round2(c.netA) : c.baseA,
      basisCurrentA: c.baseA,
      durationMin,
      restoreBy,
      safeCutMinutes: safeMin,
      risk: { tier: c.rk.tier, tierLabel: c.rk.tierLabel, score: c.rk.score },
    });
    overrides.set(c.reefer.id, { kind: 'CUT', heldA: c.baseA, basisA: c.baseA, _effectiveId: c.effectiveId });
  }

  // 阶段 3：若仍有小缺口，对剩余可安全限功率的低风险箱温和补齐（尽量少断电）。
  for (let guard = 0; guard < s.reefers.size + 2; guard++) {
    const { loads, d } = snapshot();
    if (!anyDeficit(d)) break;
    const { bindingBoards, shipBinding } = context(loads, d);
    const cands = [];
    for (const r of s.reefers.values()) {
      const rk = risks.get(r.id);
      if (!rk || rk.tier >= 3) continue;
      if (overrides.has(r.id) || byReefer.has(r.id)) continue;
      const boardId = boardOfReefer(s, r.id);
      if (!boardId || (bindingBoards.size && !bindingBoards.has(boardId))) continue;
      const item = itemOf(loads, boardId, r.id);
      const baseA = item ? item.baseA : 0;
      if (baseA <= 0) continue;
      const needA = Math.max(shipBinding ? d.shipDeficitA : 0, d.boardDeficitA[boardId] || 0);
      const pick = pickGear(baseA, needA, rk.tier === 1 ? LOW_MAX_GEAR : MED_MAX_GEAR);
      if (!pick) continue;
      cands.push({ reefer: r, rk, boardId, baseA, ...pick });
    }
    if (!cands.length) break;
    cands.sort((a, b) =>
      a.rk.tier - b.rk.tier || a.rk.score - b.rk.score || b.heldA - a.heldA || a.reefer.id.localeCompare(b.reefer.id));
    const c = cands[0];
    addLimit(c, c.baseA);
  }
  } // end if (allowCuts)

  const actions = order.map((rid) => ({ ...byReefer.get(rid), ...candidateSocket(s, rid) }));
  const loadsBefore = (() => {
    const base = new Map();
    for (const a of s.shavingActions.filter((x) => x.status === 'EFFECTIVE')) {
      base.set(a.reeferId, { kind: a.kind, heldA: a.kind === 'LIMIT' ? (a.heldA ?? a.plannedHeldA) : a.releaseA, gearFraction: a.gearFraction });
    }
    return projectedLoads(s, at, base);
  })();
  const deficitsBefore = deficits(loadsBefore, budget);
  const loadsAfter = projectedLoads(s, at, overrides);
  const deficitsAfter = deficits(loadsAfter, budget);
  const feasible = !anyDeficit(deficitsAfter);

  return {
    feasible,
    actions,
    loadsBefore,
    loadsAfter,
    deficitsBefore,
    deficitsAfter,
    windowEndAt,
    infeasibleReasons: feasible ? [] : infeasibleReasons(s, risks, deficitsAfter, skipReasons),
    skipReasons,
  };
}

function infeasibleReasons(s, risks, d, skipReasons) {
  const reasons = [];
  if (d.shipDeficitA > 0) {
    reasons.push({
      scope: 'SHIP',
      deficitA: d.shipDeficitA,
      reason: '剩余缺口无法释放：高风险箱受保护、中风险箱不得断电、可断电低风险箱保冷时长不足',
      highProtected: [...s.reefers.values()].filter((r) => (risks.get(r.id)?.tier ?? 1) >= 3).map((r) => r.id),
      unsafeCuts: skipReasons.filter((x) => x.phase === 'CUT' && x.reason === 'UNSAFE_CUT').map((x) => ({
        reeferId: x.reeferId, safeMinutes: x.safeMinutes,
      })),
    });
  }
  for (const [boardId, deficitA] of Object.entries(d.boardDeficitA)) {
    if (deficitA <= 0) continue;
    reasons.push({
      scope: 'BOARD',
      boardId,
      deficitA,
      reason: `配电板 ${boardId} 即使安排全部可执行限功率/断电仍超最严上限`,
      highProtected: [...s.reefers.values()]
        .filter((r) => boardOfReefer(s, r.id) === boardId && (risks.get(r.id)?.tier ?? 1) >= 3)
        .map((r) => r.id),
    });
  }
  return reasons;
}

export const LIMIT_CONSTANTS = { LOW_MAX_GEAR, MED_MAX_GEAR, MIN_CUT_MIN, SAFETY_RESERVE_MIN };
export { tierLabel };
