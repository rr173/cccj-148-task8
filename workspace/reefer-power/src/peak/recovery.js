'use strict';

import { round2 } from '../util.js';
import { activeActions, projectedLoads, boardOfReefer } from './load.js';
import { mergedBudget } from './budget.js';
import { assessAll } from './risk.js';

// 恢复一个动作所需归还的电流（保守取记录在案的释放量）
function restoreA(a) {
  if (a.kind === 'CUT') return round2(a.releaseA ?? 0);
  if (a.kind === 'LIMIT') return round2(a.heldA ?? a.plannedHeldA ?? 0);
  return 0;
}

// 待恢复动作排序：
//   1) 已到恢复最迟时刻的 CUT 最先（保冷安全最紧迫）
//   2) 温度风险分高者优先（恢复优先高风险箱）
//   3) CUT 优先于 LIMIT（全断电比限功率更严重）
//   4) 并列按恢复最迟时刻、箱号
function sortCandidates(list, riskOf) {
  return [...list].sort((a, b) => {
    const ra = riskOf(a.reeferId);
    const rb = riskOf(b.reeferId);
    const aDue = a.kind === 'CUT' ? a.restoreBy : Infinity;
    const bDue = b.kind === 'CUT' ? b.restoreBy : Infinity;
    const aOverdue = aDue;
    const bOverdue = bDue;
    if (aOverdue !== bOverdue) return aOverdue - bOverdue;
    if ((rb?.score ?? 0) !== (ra?.score ?? 0)) return (rb?.score ?? 0) - (ra?.score ?? 0);
    const kindRank = (k) => (k === 'CUT' ? 0 : 1);
    if (kindRank(a.kind) !== kindRank(b.kind)) return kindRank(a.kind) - kindRank(b.kind);
    if ((a.restoreBy ?? 0) !== (b.restoreBy ?? 0)) return (a.restoreBy ?? 0) - (b.restoreBy ?? 0);
    return a.reeferId.localeCompare(b.reeferId);
  });
}

// 选择一个恢复批次（纯函数）。
//   预算仍生效：在不越限前提下贪婪纳入；遇到期 CUT 无法纳入 -> 暂停并附带重算建议
//   预算已结束：无上限约束，按 maxBatchSize 分批，优先风险更高的箱
export function planRestoreBatch(s, at, opts = {}) {
  const maxBatchSize = Math.max(1, opts.maxBatchSize ?? Number(process.env.REEFER_RESTORE_BATCH ?? 2));
  const budget = mergedBudget(s, at);
  const risks = assessAll(s, at);
  const pending = activeActions(s);
  const riskOf = (id) => risks.get(id);

  if (!pending.length) {
    return {
      restored: [], paused: false, budgetActive: budget.active, complete: true,
      budget,
    };
  }

  const ordered = sortCandidates(pending, riskOf);
  const restored = [];
  let paused = false;
  let pauseReason = null;

  if (budget.active) {
    // 用 overrides 逐步“归还”电流，模拟恢复后的净负载
    const overrides = new Map();
    for (const a of pending) overrides.set(a.reeferId, a);

    for (const a of ordered) {
      if (restored.length >= maxBatchSize) break;
      const trial = new Map(overrides);
      trial.set(a.reeferId, null); // 恢复该箱 -> 显式取消其削峰（即便状态里仍有生效动作也按恢复投影）
      const loads = projectedLoads(s, at, trial);
      let fits = true;
      if (budget.shipLimitA != null && loads.shipNetA > budget.shipLimitA) fits = false;
      const bid = boardOfReefer(s, a.reeferId);
      const blim = budget.boardLimitsA?.[bid];
      const board = loads.boards.find((b) => b.boardId === bid);
      if (blim != null && board && board.netA > blim) fits = false;

      const due = a.kind === 'CUT' && at >= a.restoreBy;
      if (fits) {
        restored.push(toItem(a, riskOf, restoreA(a)));
        overrides.set(a.reeferId, null);
      } else {
        // 首次遇到无法纳入：暂停恢复；新建议由命令层在“已归还本批”的状态上重算
        paused = true;
        pauseReason = due ? 'DUE_CUT_NO_HEADROOM' : 'LOWER_LIMIT_HEADROOM';
        break;
      }
    }
  } else {
    // 预算结束：按风险优先分批归还，单批不超过 maxBatchSize
    for (const a of ordered.slice(0, maxBatchSize)) {
      restored.push(toItem(a, riskOf, restoreA(a)));
    }
  }

  return {
    restored,
    restoredReeferIds: restored.map((x) => x.reeferId),
    paused,
    pauseReason,
    budgetActive: budget.active,
    complete: !paused && restored.length === pending.length,
    remaining: pending.length - restored.length,
    budget,
  };
}

function toItem(a, riskOf, restoreAmp) {
  const rk = riskOf(a.reeferId);
  return {
    actionId: a.id,
    reeferId: a.reeferId,
    boardId: a.boardId,
    kind: a.kind,
    restoreA: round2(restoreAmp),
    restoreBy: a.restoreBy ?? null,
    riskScore: rk?.score ?? 0,
    tierLabel: rk?.tierLabel ?? '低风险',
  };
}
