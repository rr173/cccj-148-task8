'use strict';

import { round2 } from '../util.js';
import { poweredLease } from '../domain.js';
import { mergedBudget, budgetCurve } from './budget.js';
import { assessAll } from './risk.js';
import { activeActions, activeActionOf, projectedLoads, basisSignature } from './load.js';
import { CURRENT_FRESHNESS_MS } from '../util.js';

function currentFreshness(s, reeferId, at) {
  const lease = poweredLease(s, reeferId);
  if (!lease) return null;
  const m = s.latestCurrent.get(lease.socketId);
  const fresh = !!m && at - m.measuredAt <= CURRENT_FRESHNESS_MS;
  return {
    socketId: lease.socketId,
    measuredAt: m?.measuredAt ?? null,
    ageMs: m ? at - m.measuredAt : null,
    fresh,
    basis: fresh ? 'MEASURED' : 'NAMEPLATE',
  };
}

// 当前恢复进度：以最近一次会话的总数为分母，累计已恢复数量为分子
function recoveryProgress(s) {
  const batches = [...s.restoreBatches].sort((a, b) => a.at - b.at || a.id.localeCompare(b.id));
  if (!batches.length) return null;
  const sessionId = batches[batches.length - 1].sessionId;
  const sessionBatches = batches.filter((b) => b.sessionId === sessionId);
  const first = sessionBatches[0];
  const total = first.total ?? 0;
  let recovered = 0;
  for (const b of sessionBatches) recovered += (b.items?.length || 0);
  const last = sessionBatches[sessionBatches.length - 1];
  return {
    sessionId,
    total,
    recoveredCount: recovered,
    remaining: Math.max(0, total - recovered),
    state: last.state,
    paused: last.paused,
    pauseReason: last.pauseReason,
  };
}

export function buildPeakView(s, at) {
  const risks = assessAll(s, at);
  const budget = mergedBudget(s, at);
  const curve = budgetCurve(s);
  const loads = projectedLoads(s, at);

  // 削峰动作视图
  const actions = [...s.shavingActions]
    .sort((a, b) => a.rev - b.rev || a.id.localeCompare(b.id))
    .map((a) => {
      const rk = risks.get(a.reeferId);
      return {
        id: a.id,
        rev: a.rev,
        reeferId: a.reeferId,
        boardId: a.boardId,
        socketId: a.socketId,
        kind: a.kind,
        status: a.status,
        gearFraction: a.gearFraction,
        heldA: a.heldA ?? a.plannedHeldA ?? null,
        releaseA: a.releaseA,
        basisCurrentA: a.basisCurrentA,
        currentA: a.currentA ?? null,
        durationMin: a.durationMin,
        restoreBy: a.restoreBy,
        replacesActionId: a.replacesActionId,
        executedAt: a.executedAt,
        restoredAt: a.restoredAt,
        invalidatedAt: a.invalidatedAt,
        invalidateReason: a.invalidateReason ?? null,
        due: a.status === 'EFFECTIVE' && a.kind === 'CUT' && a.restoreBy != null && at >= a.restoreBy,
        riskTier: rk?.tier ?? null,
        riskScore: rk?.score ?? null,
        tierLabel: rk?.tierLabel ?? null,
      };
    });

  // 传感器新鲜度与风险
  const reefers = [...s.reefers.values()]
    .filter((r) => poweredLease(s, r.id))
    .map((r) => {
      const rk = risks.get(r.id);
      const cur = currentFreshness(s, r.id, at);
      const action = activeActionOf(s, r.id);
      return {
        reeferId: r.id,
        boardId: cur ? socketBoard(s, cur.socketId) : null,
        socketId: cur?.socketId ?? null,
        currentBasis: cur?.basis ?? 'NAMEPLATE',
        currentFresh: cur?.fresh ?? false,
        currentAgeMs: cur?.ageMs ?? null,
        currentMeasuredAt: cur?.measuredAt ?? null,
        tempFresh: rk?.tempFresh ?? false,
        tempMeasuredAt: rk?.tempMeasuredAt ?? null,
        currentC: rk?.currentC ?? null,
        setpointC: rk?.setpointC ?? null,
        excursionC: rk?.excursionC ?? null,
        trendRateCph: rk?.trendRateCph ?? 0,
        spareRatio: rk?.spareRatio ?? null,
        tier: rk?.tier ?? 1,
        tierLabel: rk?.tierLabel ?? '低风险',
        riskScore: rk?.score ?? 0,
        activeActionId: action?.id ?? null,
        activeKind: action?.kind ?? null,
      };
    })
    .sort((a, b) => a.reeferId.localeCompare(b.reeferId));

  const currentRevision = s.revisions.length ? s.revisions[s.revisions.length - 1] : null;
  let stale = false;
  let staleReasons = [];
  if (currentRevision && currentRevision.status === 'FEASIBLE') {
    const pending = s.shavingActions.some((a) => a.rev === currentRevision.rev && a.status === 'PENDING');
    if (pending) {
      // 基线签名变化即代表电流/温度变化使原计算失效
      if (basisSignature(s, at, risks) !== currentRevision.basisSignature) {
        stale = true;
        staleReasons.push('BASIS_CHANGED');
      }
    }
  }

  // 当前即时节省：所有生效动作的实际释放电流之和
  const currentSavingA = round2(activeActions(s).reduce((sum, a) => {
    if (a.kind === 'LIMIT') return sum + (a.heldA ?? a.plannedHeldA ?? 0);
    return sum + (a.releaseA ?? 0);
  }, 0));

  return {
    at,
    budget: budget.active
      ? {
          active: true,
          startAt: budget.startAt,
          endAt: budget.endAt,
          shipLimitA: budget.shipLimitA,
          boardLimitsA: budget.boardLimitsA,
          sourceBudgetIds: budget.sourceIds,
          secondsRemaining: Math.max(0, Math.round((budget.endAt - at) / 1000)),
        }
      : { active: false },
    budgetCurve: curve.segments.map((g) => ({
      startAt: g.startAt, endAt: g.endAt, shipLimitA: g.shipLimitA, boardLimitsA: g.boardLimitsA,
    })),
    registrations: curve.registrations,
    load: {
      shipGrossA: loads.shipGrossA,
      shipNetA: loads.shipNetA,
      currentSavingA,
      shipLimitA: budget.shipLimitA,
      shipWithinLimit: !(budget.shipLimitA != null && loads.shipNetA > budget.shipLimitA),
      boards: loads.boards.map((b) => ({
        boardId: b.boardId,
        grossA: b.grossA,
        netA: b.netA,
        releasedA: b.releasedA,
        limitA: budget.boardLimitsA?.[b.boardId] ?? null,
        withinLimit: !(budget.boardLimitsA?.[b.boardId] != null && b.netA > budget.boardLimitsA[b.boardId]),
      })),
    },
    currentRevision: currentRevision
      ? {
          rev: currentRevision.rev,
          status: currentRevision.status,
          feasible: currentRevision.feasible,
          operatorId: currentRevision.operatorId,
          at: currentRevision.at,
          summary: currentRevision.summary,
          stale,
          staleReasons,
          deficitsAfter: currentRevision.deficitsAfter,
          infeasibleReasons: currentRevision.infeasibleReasons,
          actionIds: currentRevision.actionIds,
        }
      : null,
    revisions: [...s.revisions].map((r) => ({
      rev: r.rev,
      at: r.at,
      operatorId: r.operatorId,
      status: r.status,
      feasible: r.feasible,
      summary: r.summary,
      actions: r.actionIds.length,
    })),
    actions,
    reefers,
    savings: {
      currentSavingA,
      cumulativeEnergyAmin: round2(s.energySavedAmin),
    },
    recovery: recoveryProgress(s),
  };
}

function socketBoard(s, socketId) {
  const sk = s.sockets.get(socketId);
  if (!sk) return null;
  return s.circuits.get(sk.circuitId)?.boardId ?? null;
}
