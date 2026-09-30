'use strict';

import {
  bad,
  conflict,
  notFound,
  checkId,
  num,
  round2,
} from '../util.js';
import { poweredLease } from '../domain.js';
import { effectiveCurrentA } from '../views.js';
import { mergedBudget } from './budget.js';
import { assessAll } from './risk.js';
import { planShaving } from './planner.js';
import { planRestoreBatch } from './recovery.js';
import { projectedLoads, basisSignature, activeActionOf } from './load.js';
import { cloneState } from '../store.js';
import { sweepAfterCoreFacts } from '../precool/monitor.js';

const OPERATOR_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$/;

function operator(body) {
  const id = String(body.operatorId ?? body.operator ?? 'OPS').trim();
  if (!OPERATOR_RE.test(id)) throw bad('operatorId 非法');
  return id;
}

function latestRevision(s) {
  return s.revisions.length ? s.revisions[s.revisions.length - 1] : null;
}

function actionView(a) {
  return {
    actionId: a.id,
    rev: a.rev,
    reeferId: a.reeferId,
    boardId: a.boardId,
    socketId: a.socketId,
    kind: a.kind,
    status: a.status,
    releaseA: a.releaseA,
    heldA: a.heldA ?? a.plannedHeldA ?? null,
    gearFraction: a.gearFraction,
    durationMin: a.durationMin,
    restoreBy: a.restoreBy,
    safeCutMinutes: a.safeCutMinutes ?? null,
    basisCurrentA: a.basisCurrentA,
    currentA: a.currentA ?? null,
    incrementalReleaseA: a.incrementalReleaseA ?? null,
  };
}

function withinBudget(s, at) {
  const budget = mergedBudget(s, at);
  if (!budget.active) return { ok: true, budget };
  const loads = projectedLoads(s, at);
  const violations = [];
  if (budget.shipLimitA != null && loads.shipNetA > budget.shipLimitA + 1e-6) {
    violations.push({ scope: 'SHIP', netA: loads.shipNetA, limitA: budget.shipLimitA });
  }
  for (const b of loads.boards) {
    const lim = budget.boardLimitsA?.[b.boardId];
    if (lim != null && b.netA > lim + 1e-6) {
      violations.push({ scope: 'BOARD', boardId: b.boardId, netA: b.netA, limitA: lim });
    }
  }
  return { ok: violations.length === 0, violations, budget, loads };
}

// ---------- 登记分时功率预算 ----------
export function cmdBudgetRegister(s, body, at) {
  const operatorId = operator(body);
  const startAt = num(body.startAt ?? at, 'startAt(epoch ms)', { integer: true });
  const endAt = num(body.endAt, 'endAt(epoch ms)', { integer: true });
  if (endAt <= startAt) throw bad('预算结束时刻必须晚于开始时刻');

  let shipLimitA = null;
  if (body.shipLimitA != null) shipLimitA = num(body.shipLimitA, '全船安培上限', { min: 0.01 });
  const boardLimitsA = {};
  for (const [bid, lim] of Object.entries(body.boardLimitsA || {})) {
    checkId(bid, '配电板');
    if (!s.boards.has(bid)) throw bad(`配电板 ${bid} 尚未录入`);
    boardLimitsA[bid] = num(lim, `配电板 ${bid} 安培上限`, { min: 0.01 });
  }
  if (shipLimitA == null && !Object.keys(boardLimitsA).length) {
    throw bad('至少需要提供全船上限 shipLimitA 或配电板上限 boardLimitsA');
  }

  const budgetId = `BG${String(s.budgetSeq + 1).padStart(4, '0')}`;
  const fact = {
    type: 'budgetRegistered',
    at, budgetId, operatorId, startAt, endAt, shipLimitA, boardLimitsA,
  };

  return {
    facts: [fact],
    enrich: (state) => {
      const merged = mergedBudget(state, at);
      return {
        registered: true,
        budgetId,
        startAt,
        endAt,
        shipLimitA,
        boardLimitsA,
        mergedAtRegistration: {
          active: merged.active,
          shipLimitA: merged.shipLimitA,
          boardLimitsA: merged.boardLimitsA,
          sourceBudgetIds: merged.sourceIds,
        },
      };
    },
  };
}

export function cmdBudgetRevoke(s, body, at) {
  const budgetId = checkId(body.budgetId, '预算');
  const b = s.budgets.find((x) => x.id === budgetId);
  if (!b) throw notFound(`预算不存在: ${budgetId}`);
  if (b.revokedAt) return { facts: [], reply: { revoked: false, alreadyRevoked: true, budgetId } };
  return {
    facts: [{ type: 'budgetRevoked', at, budgetId, operatorId: operator(body) }],
    reply: { revoked: true, budgetId },
  };
}

// ---------- 发布削峰修订 ----------
// 可选 expectedRev：旧修订操作（expectedRev 非当前）返回 409 + 最新版本信息。
export function cmdPeakPublish(s, body, at) {
  const operatorId = operator(body);
  const budget = mergedBudget(s, at);
  if (!budget.active) throw conflict('当前时刻没有生效中的功率预算，无法发布削峰方案', { code: 'NO_ACTIVE_BUDGET' });

  const last = latestRevision(s);
  const expectedRev = body.expectedRev != null ? Number(body.expectedRev) : null;
  if (last && expectedRev != null && expectedRev !== last.rev) {
    throw conflict(`旧修订 ${expectedRev} 已被修订 ${last.rev} 取代，请基于最新版本重算`, {
      code: 'REVISION_STALE',
      currentRev: last.rev,
      currentSummary: last.summary,
      currentStatus: last.status,
      recalcHint: '读取 GET /api/state 后重新发布',
    });
  }

  const risks = assessAll(s, at);
  const plan = planShaving(s, budget, risks, at);
  const newRev = (last?.rev ?? 0) + 1;
  const supersedesRev = last?.rev ?? 0;
  const sig = basisSignature(s, at, risks);

  const summary = summarize(plan, budget);
  const actions = plan.actions;
  const fact = {
    type: 'peakPlanPublished',
    at,
    rev: newRev,
    supersedesRev,
    operatorId,
    summary,
    feasible: plan.feasible,
    basisSignature: sig,
    budget: {
      shipLimitA: budget.shipLimitA,
      boardLimitsA: budget.boardLimitsA,
      endAt: budget.endAt,
      sourceBudgetIds: budget.sourceIds,
    },
    actions,
    deficitsAfter: plan.deficitsAfter,
    infeasibleReasons: plan.infeasibleReasons,
    skipReasons: plan.skipReasons,
  };

  return {
    facts: [fact],
    enrich: (state) => {
      const created = state.shavingActions.filter((a) => a.rev === newRev).map(actionView);
      const check = withinBudget(state, at);
      return {
        rev: newRev,
        supersedesRev,
        operatorId,
        summary,
        feasible: plan.feasible,
        stale: false,
        budget: fact.budget,
        before: { shipGrossA: plan.loadsBefore.shipGrossA, shipNetA: plan.loadsBefore.shipNetA, deficits: plan.deficitsBefore },
        after: { shipNetA: plan.loadsAfter.shipNetA, deficits: plan.deficitsAfter, withinLimit: check.ok },
        actions: created,
        infeasibleReasons: plan.infeasibleReasons,
        skipReasons: plan.skipReasons,
      };
    },
  };
}

function summarize(plan, budget) {
  const limits = plan.actions.filter((a) => a.kind === 'LIMIT').length;
  const cuts = plan.actions.filter((a) => a.kind === 'CUT').length;
  const released = plan.actions.reduce((sum, a) => {
    // 升级断电的增量释放要避免与既有 LIMIT 重复扣减
    return sum + (a.kind === 'CUT' ? (a.incrementalReleaseA ?? a.releaseA) : a.releaseA);
  }, 0);
  const lvl = [];
  if (budget.shipLimitA != null) lvl.push(`全船≤${budget.shipLimitA}A`);
  for (const [b, v] of Object.entries(budget.boardLimitsA || {})) lvl.push(`${b}≤${v}A`);
  const tail = plan.feasible
    ? `限功率${limits}箱/断电${cuts}箱，预计释放${round2(released)}A`
    : `无法满足预算（限功率${limits}/断电${cuts}，仍有缺口）`;
  return `${lvl.join('，')}；${tail}`;
}

// ---------- 执行限功率 / 断电 ----------
export function cmdShavingExecute(s, body, at) {
  const operatorId = operator(body);
  if (!Array.isArray(body.actionIds) || !body.actionIds.length) throw bad('actionIds 必须是非空数组');

  const ids = body.actionIds.map((x) => checkId(String(x), '动作'));
  const found = ids.map((id) => {
    const a = s.shavingActions.find((x) => x.id === id);
    if (!a) throw notFound(`削峰动作不存在: ${id}`);
    return a;
  });

  const facts = [];
  const replies = [];

  for (const a of found) {
    if (a.status !== 'PENDING') {
      throw conflict(`动作 ${a.id} 当前为 ${a.status}，只能执行待执行动作`, {
        code: `ACTION_${a.status}`,
        actionId: a.id,
      });
    }
    const lease = poweredLease(s, a.reeferId);
    if (!lease) throw conflict(`箱 ${a.reeferId} 当前未供电，无法执行削峰动作`, { code: 'REEFER_NOT_POWERED' });

    const currentA = round2(effectiveCurrentA(s, lease.socketId, at));
    if (a.kind === 'LIMIT') {
      // 以执行瞬间的新鲜电流为准重算实际释放量（无新鲜读数则回退铭牌，与基线一致）
      const gear = a.gearFraction;
      const held = round2(currentA * gear);
      facts.push({
        type: 'shavingActionExecuted',
        at, actionId: a.id, operatorId,
        kind: 'LIMIT',
        gearFraction: gear,
        currentA,
        heldA: held,
        releaseA: held,
      });
      replies.push({ actionId: a.id, reeferId: a.reeferId, kind: 'LIMIT', currentA, heldA: held, releaseA: held, durationMin: a.durationMin, restoreBy: a.restoreBy });
      // 投影到本次调用的后续状态由 store.enrich 在归并后统一校验，此处不直接改 s
    } else {
      let incremental = currentA;
      if (a.replacesActionId) {
        const old = s.shavingActions.find((x) => x.id === a.replacesActionId);
        if (!old || old.status !== 'EFFECTIVE') {
          throw conflict(`断电动作 ${a.id} 要升级的限功率 ${a.replacesActionId} 尚未生效`, { code: 'LIMIT_NOT_EFFECTIVE' });
        }
        incremental = round2(currentA);
      }
      facts.push({
        type: 'shavingActionExecuted',
        at, actionId: a.id, operatorId,
        kind: 'CUT',
        gearFraction: null,
        currentA,
        heldA: currentA,
        releaseA: currentA,
        incrementalReleaseA: incremental,
      });
      replies.push({ actionId: a.id, reeferId: a.reeferId, kind: 'CUT', currentA, heldA: currentA, releaseA: currentA, incrementalReleaseA: incremental, durationMin: a.durationMin, restoreBy: a.restoreBy });
    }
  }

  // 预冷互锁：短时断电(CUT)使相关箱既有合格证立即失效、豁免自动撤销。
  // 限功率(LIMIT)不中断供电，不影响合格证。
  const cutReeferIds = [...new Set(found.filter((a) => a.kind === 'CUT').map((a) => a.reeferId))];
  const swept = cutReeferIds.length ? sweepAfterCoreFacts(s, at, facts, cutReeferIds) : { facts: [], invalidations: [] };

  return {
    facts: [...facts, ...swept.facts],
    enrich: (state) => {
      // 削峰动作只会降低负载。允许值班员逐步执行（途中可能仍未达标，待剩余动作执行完达标），
      // 但绝不允许执行后净载反弹升高（防止瞬间越限扩大）。
      const before = projectedLoads(s, at);
      const after = projectedLoads(state, at);
      const rebounded = [];
      if (after.shipNetA > before.shipNetA + 1e-6) {
        rebounded.push({ scope: 'SHIP', beforeA: before.shipNetA, afterA: after.shipNetA });
      }
      for (const bAfter of after.boards) {
        const bBefore = before.boards.find((b) => b.boardId === bAfter.boardId);
        if (bBefore && bAfter.netA > bBefore.netA + 1e-6) {
          rebounded.push({ scope: 'BOARD', boardId: bAfter.boardId, beforeA: bBefore.netA, afterA: bAfter.netA });
        }
      }
      if (rebounded.length) {
        throw conflict(`执行削峰动作不应使负载反弹升高: ${JSON.stringify(rebounded)}`, {
          code: 'LOAD_REBOUND_ON_EXECUTE',
          rebounded,
        });
      }
      const check = withinBudget(state, at);
      return {
        executed: replies.length,
        actions: state.shavingActions.filter((x) => ids.includes(x.id)).map(actionView),
        releasedTotalA: round2(replies.reduce((sum, r) => sum + (r.incrementalReleaseA ?? r.releaseA), 0)),
        shipNetA: after.shipNetA,
        withinLimit: check.ok,
        violations: check.violations,
        invalidations: swept.invalidations,
      };
    },
  };
}

// ---------- 计划失效重算：撤销未执行动作并生成新建议（已生效事实不消失） ----------
export function cmdPeakRecompute(s, body, at) {
  const operatorId = operator(body);
  const budget = mergedBudget(s, at);
  if (!budget.active) throw conflict('当前没有生效中的功率预算', { code: 'NO_ACTIVE_BUDGET' });
  const last = latestRevision(s);
  if (!last) throw conflict('尚无已发布修订，无法重算', { code: 'NO_REVISION' });

  // 只撤销“当前最新修订”尚未执行的动作；更早修订的待执行动作在新修订发布时已作废
  const pending = s.shavingActions.filter((a) => a.status === 'PENDING' && a.rev === last.rev);
  const risks = assessAll(s, at);
  const plan = planShaving(s, budget, risks, at);
  const newRev = last.rev + 1;
  const sig = basisSignature(s, at, risks);
  const summary = summarize(plan, budget);

  const facts = [];
  if (pending.length) {
    facts.push({
      type: 'shavingActionsInvalidated',
      at,
      operatorId,
      actionIds: pending.map((a) => a.id),
      reason: body.reason ?? 'CURRENT_OR_TEMP_CHANGED',
      byRev: newRev,
    });
  }
  facts.push({
    type: 'peakPlanPublished',
    at,
    rev: newRev,
    supersedesRev: last.rev,
    operatorId,
    summary,
    feasible: plan.feasible,
    basisSignature: sig,
    budget: {
      shipLimitA: budget.shipLimitA,
      boardLimitsA: budget.boardLimitsA,
      endAt: budget.endAt,
      sourceBudgetIds: budget.sourceIds,
    },
    actions: plan.actions,
    deficitsAfter: plan.deficitsAfter,
    infeasibleReasons: plan.infeasibleReasons,
    skipReasons: plan.skipReasons,
  });

  return {
    facts,
    enrich: (state) => ({
      rev: newRev,
      recomputedFromRev: last.rev,
      invalidated: pending.map((a) => ({ actionId: a.id, reeferId: a.reeferId, kind: a.kind })),
      preservedEffective: state.shavingActions.filter((a) => a.status === 'EFFECTIVE').map((a) => a.id),
      summary,
      feasible: plan.feasible,
      actions: state.shavingActions.filter((a) => a.rev === newRev).map(actionView),
      infeasibleReasons: plan.infeasibleReasons,
    }),
  };
}

// ---------- 手动撤销方案（只撤销尚未执行动作） ----------
export function cmdPeakCancel(s, body, at) {
  const operatorId = operator(body);
  const last = latestRevision(s);
  if (!last) throw conflict('尚无方案可撤销', { code: 'NO_REVISION' });
  const rev = body.rev != null ? Number(body.rev) : last.rev;
  const target = s.revisions.find((r) => r.rev === rev);
  if (!target) throw notFound(`修订不存在: ${rev}`);
  const pending = s.shavingActions.filter((a) => a.rev === rev && a.status === 'PENDING');
  if (!pending.length) {
    return { facts: [], reply: { cancelled: 0, rev, note: '没有尚未执行的动作（已生效事实保留）' } };
  }
  return {
    facts: [{
      type: 'shavingActionsCancelled',
      at, operatorId, rev, actionIds: pending.map((a) => a.id),
    }],
    reply: { cancelled: pending.length, rev, actionIds: pending.map((a) => a.id) },
  };
}

// ---------- 预算结束后分批恢复（风险优先，途中遇更低上限暂停重算） ----------
export function cmdRestore(s, body, at) {
  const operatorId = operator(body);
  const batch = planRestoreBatch(s, at, { maxBatchSize: body.maxBatchSize });

  if (!batch.restored.length) {
    if (batch.paused) {
      // 整批一个都还不进去且需暂停：在当前状态上重算，发布新修订，不恢复任何箱
      const pause = buildPauseFact(s, at, operatorId, batch, s);
      return {
        facts: [pause.fact],
        enrich: (state2) => ({
          restored: 0,
          paused: true,
          pauseReason: batch.pauseReason,
          revisionPublished: pause.fact.rev,
          feasible: pause.plan.feasible,
          actions: state2.shavingActions.filter((a) => a.rev === pause.fact.rev).map(actionView),
          infeasibleReasons: pause.plan.infeasibleReasons,
          note: '可用余量不足，恢复暂停并生成新建议',
        }),
      };
    }
    return {
      facts: [],
      reply: {
        restored: 0,
        complete: batch.complete,
        budgetActive: batch.budgetActive,
        note: batch.complete ? '没有待恢复的削峰动作' : '当前无待恢复项',
      },
    };
  }

  const session = resolveSession(s, at);
  const batchId = `RB${String(s.peakSeq + 1).padStart(5, '0')}`;
  const items = batch.restored.map((x) => ({
    actionId: x.actionId,
    reeferId: x.reeferId,
    boardId: x.boardId,
    kind: x.kind,
    restoreA: x.restoreA,
    riskScore: x.riskScore,
  }));

  // 构造“已归还本批”的临时状态：把本批动作视为 RESTORED，再据此重算暂停建议
  let pause = null;
  if (batch.paused) {
    const scratch = cloneState(s);
    for (const id of batch.restored.map((x) => x.actionId)) {
      const a = scratch.shavingActions.find((x) => x.id === id);
      if (a) a.status = 'RESTORED';
    }
    pause = buildPauseFact(scratch, at, operatorId, batch, s);
  }

  const restoreFact = {
    type: 'restoreBatchRecorded',
    at,
    batchId,
    sessionId: session.sessionId,
    items,
    total: session.total,
    recoveredCount: items.length,
    paused: batch.paused,
    pauseReason: batch.pauseReason,
    budgetActive: batch.budgetActive,
    revisionPublished: pause ? pause.fact.rev : null,
    complete: batch.complete && !batch.paused,
  };
  const facts = [restoreFact];
  if (pause) facts.push(pause.fact);

  return {
    facts,
    enrich: (state2) => {
      const check = withinBudget(state2, at);
      return {
        batchId,
        sessionId: session.sessionId,
        restored: items.length,
        items: batch.restored,
        paused: batch.paused,
        pauseReason: batch.pauseReason,
        budgetActive: batch.budgetActive,
        revisionPublished: pause ? pause.fact.rev : null,
        withinLimitAfterRestore: check.ok,
        violations: check.violations,
        cumulativeEnergyAmin: state2.energySavedAmin,
      };
    },
  };
}

function resolveSession(s, at) {
  const lastBatch = [...s.restoreBatches].sort((a, b) => b.at - a.at || b.id.localeCompare(a.id))[0];
  const activeCount = s.shavingActions.filter((a) => a.status === 'EFFECTIVE').length;
  // 无上一会话、或上一会话已完成 -> 新建；暂停(PAUSED)/进行中(IN_PROGRESS) 沿用同一会话，
  // 保证恢复进度（已恢复/总数）跨批次连续，不因中途插入新削峰而清零。
  const needNew = !lastBatch || lastBatch.state === 'COMPLETED';
  if (needNew) {
    const id = `RS${String(s.peakSeq + 1).padStart(5, '0')}`;
    return { sessionId: id, total: activeCount, newSession: true };
  }
  return { sessionId: lastBatch.sessionId, total: lastBatch.total, newSession: false };
}

// 在给定基准状态 baseS（暂停时为“已归还本批”的临时状态）上重新计算削峰，
// 生成暂停恢复用的发布事实。修订号相对真实状态 s 递增。
function buildPauseFact(baseS, at, operatorId, batch, realS) {
  const last = realS.revisions.length ? realS.revisions[realS.revisions.length - 1] : null;
  const newRev = (last?.rev ?? 0) + 1;
  const risks = assessAll(baseS, at);
  const plan = planShaving(baseS, batch.budget, risks, at);
  const summary = summarize(plan, batch.budget);
  return {
    plan,
    fact: {
      type: 'peakPlanPublished',
      at, rev: newRev, supersedesRev: last?.rev ?? 0, operatorId: `RESTORE/${operatorId}`,
      summary, feasible: plan.feasible, basisSignature: basisSignature(baseS, at, risks),
      budget: {
        shipLimitA: batch.budget.shipLimitA,
        boardLimitsA: batch.budget.boardLimitsA,
        endAt: batch.budget.endAt,
        sourceBudgetIds: batch.budget.sourceIds,
      },
      actions: plan.actions,
      deficitsAfter: plan.deficitsAfter,
      infeasibleReasons: plan.infeasibleReasons,
      skipReasons: plan.skipReasons,
    },
  };
}

export { activeActionOf };
