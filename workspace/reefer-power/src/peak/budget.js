'use strict';

import { round2 } from '../util.js';

// 生效中的预算登记（未撤销且覆盖时刻 t）
export function activeRegistrations(s, at) {
  return s.budgets.filter((b) => !b.revokedAt && b.startAt <= at && at < b.endAt);
}

// 多个预算区间重叠时按最严格上限合并。
// 返回 { active, startAt, endAt, shipLimitA, boardLimits:{boardId:A}, sourceIds:[] }
//   - endAt 取所有重叠区间中最早结束时刻（保守：之后必须重算）
//   - 全船上限取最小；配电板上限逐板取最小
export function mergedBudget(s, at) {
  const regs = activeRegistrations(s, at);
  if (!regs.length) return { active: false, sourceIds: [] };

  let shipLimitA = Infinity;
  const boardMin = new Map();
  let endAt = Infinity;
  const sourceIds = [];
  for (const b of regs) {
    sourceIds.push(b.id);
    endAt = Math.min(endAt, b.endAt);
    if (b.shipLimitA != null) shipLimitA = Math.min(shipLimitA, b.shipLimitA);
    for (const [bid, lim] of Object.entries(b.boardLimitsA || {})) {
      boardMin.set(bid, boardMin.has(bid) ? Math.min(boardMin.get(bid), lim) : lim);
    }
  }
  return {
    active: true,
    startAt: Math.max(...regs.map((b) => b.startAt)),
    endAt,
    shipLimitA: shipLimitA === Infinity ? null : round2(shipLimitA),
    boardLimitsA: Object.fromEntries([...boardMin.entries()].map(([k, v]) => [k, round2(v)])),
    sourceIds: sourceIds.sort(),
  };
}

// 预算曲线：把所有未撤销区间投影到离散时间轴，按重叠合并取最严上限。
// 供值班屏绘制。segments 已按开始时刻排序、相邻且上限相同的区间做了合并。
export function budgetCurve(s, { from, to } = {}) {
  const regs = s.budgets.filter((b) => !b.revokedAt);
  const points = new Set();
  for (const b of regs) {
    points.add(b.startAt);
    points.add(b.endAt);
  }
  const sorted = [...points].sort((a, b) => a - b);
  const raw = [];
  for (let i = 0; i < sorted.length - 1; i++) {
    const segStart = sorted[i];
    const segEnd = sorted[i + 1];
    const mid = (segStart + segEnd) / 2;
    const m = mergedBudget(s, mid);
    if (!m.active) continue;
    if (from != null && segEnd <= from) continue;
    if (to != null && segStart >= to) continue;
    raw.push({
      startAt: Math.max(segStart, from ?? segStart),
      endAt: Math.min(segEnd, to ?? segEnd),
      shipLimitA: m.shipLimitA,
      boardLimitsA: m.boardLimitsA,
      sourceIds: m.sourceIds,
    });
  }
  // 合并相邻且上限完全相同的段
  const segments = [];
  for (const seg of raw) {
    const prev = segments[segments.length - 1];
    if (
      prev && prev.endAt === seg.startAt &&
      prev.shipLimitA === seg.shipLimitA &&
      JSON.stringify(prev.boardLimitsA) === JSON.stringify(seg.boardLimitsA)
    ) {
      prev.endAt = seg.endAt;
      prev.sourceIds = [...new Set([...prev.sourceIds, ...seg.sourceIds])].sort();
    } else {
      segments.push({ ...seg });
    }
  }
  return { at: from ?? null, segments, registrations: regs.map((b) => ({
    id: b.id, operatorId: b.operatorId, startAt: b.startAt, endAt: b.endAt,
    shipLimitA: b.shipLimitA, boardLimitsA: b.boardLimitsA, revokedAt: b.revokedAt,
  })) };
}
