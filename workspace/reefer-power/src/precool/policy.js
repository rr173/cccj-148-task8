'use strict';

// 装船前预冷合格证策略参数。
// 高风险货物使用更严格的稳定时间与偏差阈值；阈值可用环境变量覆盖（演练/测试）。
//
//   stableMs         箱温必须持续合格的最短稳定时间
//   maxDeviationC    箱温相对设定点允许的最大偏差（℃，绝对值）
//   maxRateCph       温度变化速度上限（℃/h，绝对值；制冷下降与回温上升都受限）
//   maxGapMs         观测区间内相邻读数最大间隔（超过即视为区间不连续，重新计时）
//   maxReadingAgeMs  最新读数的最大年龄：超过即读数过期，不签发/立即失效
export const DEFAULT_POLICY = {
  HIGH: {
    stableMs: 30 * 60 * 1000,
    maxDeviationC: 0.5,
    maxRateCph: 1.0,
    maxGapMs: 5 * 60 * 1000,
    maxReadingAgeMs: 5 * 60 * 1000,
  },
  MEDIUM: {
    stableMs: 20 * 60 * 1000,
    maxDeviationC: 1.5,
    maxRateCph: 2.0,
    maxGapMs: 5 * 60 * 1000,
    maxReadingAgeMs: 10 * 60 * 1000,
  },
  LOW: {
    stableMs: 10 * 60 * 1000,
    maxDeviationC: 3.0,
    maxRateCph: 3.0,
    maxGapMs: 10 * 60 * 1000,
    maxReadingAgeMs: 15 * 60 * 1000,
  },
};

// 环境变量覆盖：PRECOOL_STABLE_MIN_HIGH=30 / PRECOOL_DEVIATION_C_HIGH 等
function envNum(name, fallback) {
  const v = process.env[name];
  if (v == null || v === '') return fallback;
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

function withOverrides(base, tag) {
  return {
    stableMs: envNum(`PRECOOL_STABLE_MIN_${tag}`, base.stableMs / 60000) * 60000,
    maxDeviationC: envNum(`PRECOOL_DEVIATION_C_${tag}`, base.maxDeviationC),
    maxRateCph: envNum(`PRECOOL_MAX_RATE_CPH_${tag}`, base.maxRateCph),
    maxGapMs: envNum(`PRECOOL_MAX_GAP_MIN_${tag}`, base.maxGapMs / 60000) * 60000,
    maxReadingAgeMs: envNum(`PRECOOL_MAX_AGE_MIN_${tag}`, base.maxReadingAgeMs / 60000) * 60000,
  };
}

export function policyForTier(tier) {
  if (tier === 3) return withOverrides(DEFAULT_POLICY.HIGH, 'HIGH');
  if (tier === 2) return withOverrides(DEFAULT_POLICY.MEDIUM, 'MEDIUM');
  return withOverrides(DEFAULT_POLICY.LOW, 'LOW');
}

// 豁免有效期上限（分钟），默认 60 分钟，可用 PRECOOL_WAIVER_MAX_MIN 覆盖
export function maxWaiverMs() {
  return envNum('PRECOOL_WAIVER_MAX_MIN', 60) * 60000;
}
