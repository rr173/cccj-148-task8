'use strict';

import { round2 } from '../util.js';

// 货物等级 -> 保护层级
//   3 HIGH    医药/高价值温控：必须保护，不允许任何削峰
//   2 MEDIUM  生鲜/冷链：允许限功率，不允许断电
//   1 LOW     普通冻货/干货：先限功率，仍超限可短时断电
export const GRADES = {
  PHARMA: { tier: 3, label: '医药' },
  HIGH: { tier: 3, label: '高价值温控' },
  FRESH: { tier: 2, label: '生鲜' },
  CHILLED: { tier: 2, label: '冷藏' },
  FROZEN: { tier: 1, label: '冻货' },
  GENERAL: { tier: 1, label: '普通' },
};

const TEMP_FRESHNESS_MS = 10 * 60 * 1000;
const TREND_WINDOW_MS = 30 * 60 * 1000;

export function cargoTier(grade) {
  const g = GRADES[String(grade || 'GENERAL').toUpperCase()];
  return g ? g.tier : 1;
}

export function tierLabel(tier) {
  return { 3: '高风险', 2: '中风险', 1: '低风险' }[tier] || '低风险';
}

// 从已接受的温度样本中取该箱按测量时刻升序的序列
export function temperatureSeries(s, reeferId) {
  const out = [];
  for (const smp of s.samples.values()) {
    if (!smp.accepted || smp.kind !== 'temperature' || smp.reeferId !== reeferId) continue;
    out.push({ measuredAt: smp.measuredAt, value: smp.value });
  }
  return out.sort((a, b) => a.measuredAt - b.measuredAt);
}

function clamp01(x) {
  return Math.max(0, Math.min(1, x));
}

// 计算单个冷藏箱当前的温度风险画像（确定性纯函数）。
// 评分越高温度风险越大，恢复时越优先恢复；削峰时越后被选中。
//   trendRateCph    最近窗口内温度趋势（℃/h，>0 表示回温）
//   excursionC      最新箱温相对设定温度的偏离（℃，高于设定为正）
//   spareRatio      保冷余量：允许温升中尚未被消耗的比例
//   uncertainty     缺少新鲜温度读数带来的不确定惩罚
export function assessReefer(s, reefer, at = Date.now()) {
  const series = temperatureSeries(s, reefer.id);
  const latest = series.length ? series[series.length - 1] : null;
  const tempFresh = !!latest && at - latest.measuredAt <= TEMP_FRESHNESS_MS;

  const setpoint = reefer.setpointC != null ? Number(reefer.setpointC) : -18;
  const allowable = reefer.allowableRiseC != null ? Number(reefer.allowableRiseC) : 5;

  let currentC = latest ? latest.value : setpoint;
  let excursionC = round2(currentC - setpoint);

  // 趋势：取最近窗口首末两点做线性斜率（℃/h）
  let trendRateCph = 0;
  if (series.length >= 2) {
    const last = series[series.length - 1];
    let first = series[0];
    for (const p of series) {
      if (last.measuredAt - p.measuredAt <= TREND_WINDOW_MS) {
        first = p;
        break;
      }
    }
    const dtH = (last.measuredAt - first.measuredAt) / 3600000;
    if (dtH > 0) trendRateCph = round2((last.value - first.value) / dtH);
  }

  const usedRise = Math.max(0, excursionC);
  const spareRatio = allowable > 0 ? clamp01((allowable - usedRise) / allowable) : 0;

  // 风险分量（0..1）
  const trendComp = clamp01(trendRateCph / 2);                 // 2℃/h 视为满分回温风险
  const excursionComp = clamp01(usedRise / Math.max(0.5, allowable));
  const spareComp = 1 - spareRatio;                            // 余量越小风险越高
  const uncertainty = tempFresh ? 0 : 0.5;

  const tier = cargoTier(reefer.cargoGrade);
  const score = round2(
    3 * tier + 1.5 * trendComp + excursionComp + spareComp + uncertainty
  );

  return {
    reeferId: reefer.id,
    tier,
    tierLabel: tierLabel(tier),
    cargoGrade: reefer.cargoGrade || 'GENERAL',
    setpointC: setpoint,
    currentC: tempFresh ? round2(currentC) : null,
    excursionC: tempFresh ? excursionC : null,
    trendRateCph,
    allowableRiseC: allowable,
    spareRatio: round2(spareRatio),
    tempFresh,
    tempMeasuredAt: latest ? latest.measuredAt : null,
    score,
  };
}

export function assessAll(s, at) {
  const m = new Map();
  for (const r of s.reefers.values()) m.set(r.id, assessReefer(s, r, at));
  return m;
}

// 估算在不突破「允许温升」的前提下可承受的断电时长（分钟）。
// 已回温部分先扣减，回温速率按温度趋势（无趋势时按保温时限保守折算）估算。
export function safeCutMinutes(s, reefer, risk, at) {
  const allowable = reefer.allowableRiseC != null ? Number(reefer.allowableRiseC) : 5;
  const usedRise = Math.max(0, (risk.excursionC ?? 0));
  const remainingRise = Math.max(0, allowable - usedRise);

  let risePerHour;
  if (risk.trendRateCph > 0.01) {
    risePerHour = risk.trendRateCph;
  } else {
    // 无正向趋势时按保温时限保守估算：整段保温对应允许温升
    risePerHour = allowable / Math.max(0.01, reefer.insulationHours);
  }
  const hours = remainingRise / Math.max(0.01, risePerHour);
  return Math.max(0, round2(hours * 60));
}
