'use strict';

import { pcKey, applyPrecoolFact } from './projection.js';
import { cargoTier, tierLabel } from '../peak/risk.js';
import { policyForTier } from './policy.js';
import { round2 } from '../util.js';
import { cloneState } from '../store.js';
import { applyFact } from '../domain.js';

// 观测/合格证失效原因（与接口和页面展示一致）
export const REASONS = {
  POWER_LOSS: '期间发生掉电，供电连续性被打破',
  SETPOINT_CHANGED: '观测期间设定点被调整',
  SENSOR_BOOT_CHANGED: '传感器更换（启动代号变化）',
  READING_STALE: '温度读数已过期',
  DEVIATION_EXCEEDED: '箱温偏差超出阈值',
  RATE_EXCEEDED: '温度变化速度超出阈值',
};

// 取得该箱按测量时刻升序的真实温度读数（含传感器启动代号），只用已接受的实测样本
function tempSeriesWithBoot(s, reeferId) {
  const out = [];
  for (const smp of s.samples.values()) {
    if (!smp.accepted || smp.kind !== 'temperature' || smp.reeferId !== reeferId) continue;
    out.push({ measuredAt: smp.measuredAt, value: smp.value, bootId: smp.sensorBootId });
  }
  return out.sort((a, b) => a.measuredAt - b.measuredAt);
}

function blockerOf(code, detail = null) {
  return { code, detail, message: blockerMessage(code, detail) };
}

// 实时热状态（不依赖是否发起过观测）：用于豁免的“后续超温/掉电”自动撤销。
// 只用真实温度读数，绝不回退铭牌。
export function liveCondition(s, reefer, at) {
  const tier = cargoTier(reefer.cargoGrade);
  const policy = policyForTier(tier);
  const setpoint = reefer.setpointC != null ? Number(reefer.setpointC) : -18;
  const powered = s.leases.some((l) => l.reeferId === reefer.id && l.state === 'POWER');
  const temps = tempSeriesWithBoot(s, reefer.id);
  const latest = temps.length ? temps[temps.length - 1] : null;
  const stale = latest ? at - latest.measuredAt > policy.maxReadingAgeMs : false;
  const overTemperature = latest
    ? Math.abs(latest.value - setpoint) > policy.maxDeviationC + 1e-9
    : false;
  // 温度变化速度（最近两点，任意传感器）
  let rateViolated = false;
  if (temps.length >= 2) {
    const a = temps[temps.length - 2];
    const b = temps[temps.length - 1];
    const dtH = (b.measuredAt - a.measuredAt) / 3600000;
    if (dtH > 0 && Math.abs(b.value - a.value) / dtH > policy.maxRateCph + 1e-9) rateViolated = true;
  }
  return {
    tier,
    policy,
    setpoint,
    powered,
    latestMeasuredAt: latest?.measuredAt ?? null,
    latestValueC: latest ? round2(latest.value) : null,
    latestBootId: latest?.bootId ?? null,
    hasReading: !!latest,
    stale,
    overTemperature,
    rateViolated,
    lossCount: s.powerLossCount.get(reefer.id) ?? 0,
  };
}

function blockerMessage(code, detail) {
  switch (code) {
    case 'NOT_STARTED': return '尚未发起重新观测';
    case 'REEFER_NOT_POWERED': return '箱当前未接岸电，无法观测';
    case 'POWER_LOSS_AFTER_START': return REASONS.POWER_LOSS;
    case 'SETPOINT_CHANGED_AFTER_START': return REASONS.SETPOINT_CHANGED;
    case 'SENSOR_BOOT_CHANGED': return REASONS.SENSOR_BOOT_CHANGED;
    case 'NO_TEMP_READING': return '没有真实温度读数，不能用铭牌估算替代';
    case 'READING_STALE': return REASONS.READING_STALE;
    case 'WINDOW_TOO_SHORT': return '合格连续区间尚不足规定稳定时间';
    default: return code;
  }
}

// 评估一个 (航次,箱号) 的观测状态（确定性纯函数）。
// opts.extraSample：尚未归并的新温度样本（采样接口在同批次内预判传感器更换/偏差）。
// opts.policyOverride：按证书签发时冻结的阈值复核（而非当前货物等级阈值）。
export function evaluateObservation(s, voyageId, reeferId, at, opts = {}) {
  const reefer = s.reefers.get(reeferId);
  if (!reefer) return { status: 'UNKNOWN_REEFER', blockers: [blockerOf('UNKNOWN_REEFER')], progress: progressZero() };
  const tier = cargoTier(reefer.cargoGrade);
  const policy = opts.policyOverride || policyForTier(tier);
  const k = pcKey(voyageId, reeferId);
  const agg = s.precoolObs.get(k);
  const obs = agg?.observation;

  const base = {
    voyageId,
    reeferId,
    tier,
    tierLabel: tierLabel(tier),
    cargoGrade: reefer.cargoGrade,
    setpointC: reefer.setpointC,
    policy,
    bootId: obs?.bootId ?? null,
  };

  if (!obs) return { ...base, status: 'NOT_STARTED', started: false, blockers: [blockerOf('NOT_STARTED')], progress: progressZero() };

  const blockers = [];

  // 1) 当前必须由岸电供电
  // （domain.activeLeaseOfReefer 经 views 间接使用；此处直接查租约避免循环依赖）
  const powered = s.leases.some((l) => l.reeferId === reeferId && l.state === 'POWER');
  if (!powered) blockers.push(blockerOf('REEFER_NOT_POWERED'));

  // 2) 供电连续性：观测起点之后发生过新的掉电分组 -> 重新计时
  const lossCount = s.powerLossCount.get(reeferId) ?? 0;
  let restartFrom = null;
  if (lossCount > (obs.baselineLossCount ?? 0)) {
    blockers.push(blockerOf('POWER_LOSS_AFTER_START'));
    restartFrom = s.powerRestoredAt.get(reeferId) ?? s.powerLossAt.get(reeferId) ?? obs.startedAt;
  }

  // 3) 设定点连续性：观测起点之后设定点被调整 -> 重新计时
  const spHist = (s.setpointHistory.get(reeferId) || []).filter((e) => e.at > obs.startedAt);
  if (spHist.length) blockers.push(blockerOf('SETPOINT_CHANGED_AFTER_START'));

  // 4) 真实温度读数（采样来源必须为实测，绝不回退铭牌）。
  // 掉电后重新观测时时钟可能已推进，旧区间存在时刻晚于重观测发起时刻的样本：
  // 以观测事实显式记录的 samplesAfter 为下界，严格晚于它的样本才属于本次观测区间。
  const lowerBound = obs.samplesAfter ?? obs.startedAt;
  let series = tempSeriesWithBoot(s, reeferId).filter((p) => p.measuredAt > lowerBound);
  if (opts.extraSample) series = [...series, opts.extraSample].sort((a, b) => a.measuredAt - b.measuredAt);
  // 原始最新读数（不按启动代号过滤）：用于豁免的“后续新超温”自动撤销判定。
  // 换了传感器后旧证书/观测按 SENSOR_BOOT_CHANGED 处理，但新传感器若报超温仍必须撤销豁免。
  const rawLatest = series.length ? series[series.length - 1] : null;
  // 观测发起时尚无温度读数时 bootId 可能为空：以区间内第一个读数的启动代号作为锚点，
  // 证书签发时也绑定该代号，之后出现不同代号即判传感器更换。
  const anchorBootId = obs.bootId || (series[0]?.bootId ?? null);
  if (anchorBootId) {
    const otherBoot = series.some((p) => p.bootId && p.bootId !== anchorBootId);
    if (otherBoot) blockers.push(blockerOf('SENSOR_BOOT_CHANGED'));
    series = series.filter((p) => !p.bootId || p.bootId === anchorBootId);
  }
  const latest = series.length ? series[series.length - 1] : null;
  if (!latest) blockers.push(blockerOf('NO_TEMP_READING'));

  // 5) 读数新鲜度
  let stale = false;
  if (latest && at - latest.measuredAt > policy.maxReadingAgeMs) {
    stale = true;
    blockers.push(blockerOf('READING_STALE', { ageMs: at - latest.measuredAt, maxMs: policy.maxReadingAgeMs }));
  }

  // 6) 从最新读数向回取「连续合格后缀」：间隔、变化速度、偏差全部满足
  const setpoint = reefer.setpointC != null ? Number(reefer.setpointC) : -18;
  let windowStart = latest?.measuredAt ?? at;
  let suffixPoints = 0;
  let rateViolations = 0;
  let deviationViolations = 0;
  if (latest) {
    if (Math.abs(latest.value - setpoint) > policy.maxDeviationC + 1e-9) {
      blockers.push(blockerOf('DEVIATION_EXCEEDED'));
    }
    windowStart = latest.measuredAt;
    for (let i = series.length - 1; i >= 0; i--) {
      const p = series[i];
      if (Math.abs(p.value - setpoint) > policy.maxDeviationC + 1e-9) break;
      if (i < series.length - 1) {
        const next = series[i + 1];
        const gap = next.measuredAt - p.measuredAt;
        if (gap > policy.maxGapMs) break;
        const dtH = gap / 3600000;
        if (dtH > 0 && Math.abs(next.value - p.value) / dtH > policy.maxRateCph + 1e-9) {
          rateViolations++;
          break;
        }
      }
      windowStart = p.measuredAt;
      suffixPoints++;
    }
    if (rateViolations) blockers.push(blockerOf('RATE_EXCEEDED'));
  }

  const windowEnd = latest?.measuredAt ?? null;
  const qualifiedMs = windowEnd != null ? Math.max(0, windowEnd - windowStart) : 0;
  const enough = qualifiedMs >= policy.stableMs && suffixPoints >= 2 && !stale;
  if (!stale && latest && !enough && !blockers.some((b) => ['DEVIATION_EXCEEDED', 'RATE_EXCEEDED', 'SENSOR_BOOT_CHANGED', 'POWER_LOSS_AFTER_START', 'SETPOINT_CHANGED_AFTER_START', 'NO_TEMP_READING', 'REEFER_NOT_POWERED'].includes(b.code))) {
    blockers.push(blockerOf('WINDOW_TOO_SHORT'));
  }

  const hardReset = blockers.find((b) =>
    ['POWER_LOSS_AFTER_START', 'SETPOINT_CHANGED_AFTER_START', 'SENSOR_BOOT_CHANGED'].includes(b.code));

  const progress = {
    observing: true,
    startedAt: obs.startedAt,
    stableRequiredMs: policy.stableMs,
    qualifiedMs,
    qualifiedFrom: windowEnd != null ? windowStart : null,
    qualifiedTo: windowEnd,
    remainingMs: Math.max(0, policy.stableMs - qualifiedMs),
    pct: Math.min(100, Math.round((qualifiedMs / policy.stableMs) * 100)),
    sampleCount: suffixPoints,
    latestMeasuredAt: latest?.measuredAt ?? null,
    latestValueC: latest ? round2(latest.value) : null,
    readingAgeMs: latest ? at - latest.measuredAt : null,
    maxReadingAgeMs: policy.maxReadingAgeMs,
    readingFresh: latest ? at - latest.measuredAt <= policy.maxReadingAgeMs : false,
    basis: latest ? 'MEASURED' : 'NONE',
    restartRequired: !!hardReset,
    restartFrom: hardReset?.code === 'POWER_LOSS_AFTER_START' ? restartFrom
      : hardReset?.code === 'SETPOINT_CHANGED_AFTER_START' ? (spHist[spHist.length - 1]?.at ?? at)
      : hardReset?.code === 'SENSOR_BOOT_CHANGED' ? latest?.measuredAt ?? at
      : null,
  };

  const status = blockers.length ? 'ACCUMULATING' : 'READY';
  // 原始最新读数（任意传感器）层面的超温/过期/速度信号，独立于观测启动代号的过滤
  const rawSignals = rawLatest
    ? {
        measuredAt: rawLatest.measuredAt,
        valueC: round2(rawLatest.value),
        bootId: rawLatest.bootId ?? null,
        deviationC: round2(rawLatest.value - setpoint),
        overTemperature: Math.abs(rawLatest.value - setpoint) > policy.maxDeviationC + 1e-9,
        stale: at - rawLatest.measuredAt > policy.maxReadingAgeMs,
      }
    : null;
  return {
    ...base,
    status,
    started: true,
    blockers,
    ready: blockers.length === 0,
    progress,
    rawSignals,
    evidence: windowEnd != null ? evidenceOf(series, windowStart, windowEnd, setpoint, obs) : null,
  };
}

function evidenceOf(series, from, to, setpoint, obs) {
  const pts = series.filter((p) => p.measuredAt >= from && p.measuredAt <= to);
  const values = pts.map((p) => p.value);
  const minC = values.length ? round2(Math.min(...values)) : null;
  const maxC = values.length ? round2(Math.max(...values)) : null;
  const maxAbsRate = maxRate(pts);
  return {
    from,
    to,
    sampleCount: pts.length,
    setpointC: setpoint,
    minC,
    maxC,
    maxAbsRateCph: maxAbsRate,
    bootId: obs.bootId ?? pts[0]?.bootId ?? null,
    source: 'SHORE_POWER_MEASURED',
  };
}

function maxRate(pts) {
  let m = 0;
  for (let i = 1; i < pts.length; i++) {
    const dtH = (pts[i].measuredAt - pts[i - 1].measuredAt) / 3600000;
    if (dtH > 0) m = Math.max(m, Math.abs(pts[i].value - pts[i - 1].value) / dtH);
  }
  return round2(m);
}

function progressZero() {
  return {
    observing: false,
    qualifiedMs: 0,
    remainingMs: null,
    pct: 0,
    sampleCount: 0,
    readingFresh: false,
    basis: 'NONE',
  };
}

// 当前生效的证书/豁免（读模型口径，供视图与扫描判定）
export function validCerts(s, voyageId, reeferId) {
  return s.precoolCerts.filter((c) => c.voyageId === voyageId && c.reeferId === reeferId && c.status === 'VALID');
}
export function activeWaivers(s, voyageId, reeferId, at) {
  return s.precoolWaivers.filter(
    (w) => w.voyageId === voyageId && w.reeferId === reeferId && w.status === 'ACTIVE' && at < w.validTo
  );
}

// 失效扫描：在状态 s 上找出应当立即失效的证书与豁免，返回 {facts, invalidations}。
// 事实通过 applyPrecoolFact 在（克隆的）状态上试算版本号，保证同批次多聚合版本确定。
// reeferIds 限定范围；extraSample 透传新温度样本（同批次采样预判）。
export function sweep(s, at, { reeferIds = null, extraSampleByReefer = null, apply = null } = {}) {
  const facts = [];
  const invalidations = [];
  const aggs = [...s.precoolObs.values()].filter(
    (a) => !reeferIds || reeferIds.includes(a.reeferId)
  );

  for (const agg of aggs) {
    const { voyageId, reeferId } = agg;
    const certs = validCerts(s, voyageId, reeferId);
    const waivers = s.precoolWaivers.filter(
      (w) => w.voyageId === voyageId && w.reeferId === reeferId && w.status === 'ACTIVE'
    );
    if (!certs.length && !waivers.length) continue;

    const extra = extraSampleByReefer?.get(reeferId) || null;
    const ev = evaluateObservation(s, voyageId, reeferId, at, { extraSample: extra });
    const primary = ev.blockers[0]?.code ?? null;

    const certReason = certInvalidReason(ev, primary);
    for (const cert of certs) {
      if (!certReason) continue;
      const v = agg0Version(s, voyageId, reeferId) + 1;
      const restart = restartHint(certReason, ev);
      const fact = {
        type: 'precoolCertificateInvalidated',
        at,
        certId: cert.id,
        voyageId,
        reeferId,
        version: v,
        reason: certReason,
        restartFrom: restart.from,
        restartRemainingMs: restart.remainingMs,
      };
      facts.push(fact);
      invalidations.push({ kind: 'cert', id: cert.id, voyageId, reeferId, reason: certReason, ...restart });
      if (apply) apply(s, fact);
    }

    for (const w of waivers) {
      // 豁免撤销不依赖观测：直接看实时热状态（任意真实传感器的最新读数）
      const reefer = s.reefers.get(reeferId);
      const cond = reefer ? liveCondition(s, reefer, at) : null;
      const reason = waiverRevokeReason(w, ev, primary, at, cond);
      if (!reason) continue;
      const v = agg0Version(s, voyageId, reeferId) + 1;
      const fact = {
        type: 'precoolWaiverRevoked',
        at,
        waiverId: w.id,
        voyageId,
        reeferId,
        version: v,
        reason,
      };
      facts.push(fact);
      invalidations.push({ kind: 'waiver', id: w.id, voyageId, reeferId, reason });
      if (apply) apply(s, fact);
    }
  }
  return { facts, invalidations };
}

function agg0Version(s, voyageId, reeferId) {
  return s.precoolObs.get(pcKey(voyageId, reeferId))?.version ?? 0;
}

// 证书的失效原因（任何硬失效或读数过期/偏差/速度都立即失效）
function certInvalidReason(ev) {
  const codes = new Set(ev.blockers.map((b) => b.code));
  if (codes.has('POWER_LOSS_AFTER_START')) return 'POWER_LOSS';
  if (codes.has('SETPOINT_CHANGED_AFTER_START')) return 'SETPOINT_CHANGED';
  if (codes.has('SENSOR_BOOT_CHANGED')) return 'SENSOR_BOOT_CHANGED';
  if (codes.has('READING_STALE')) return 'READING_STALE';
  if (codes.has('DEVIATION_EXCEEDED')) return 'DEVIATION_EXCEEDED';
  if (codes.has('RATE_EXCEEDED')) return 'RATE_EXCEEDED';
  return null;
}

// 豁免的自动撤销原因：后续新的超温（任意传感器的最新真实读数）或掉电即撤销；到期也撤销。
// 传感器更换、设定点调整本身不撤销豁免（它们只令证书失效/观测重计时）。
function waiverRevokeReason(w, ev, primary, at, cond) {
  if (at >= w.validTo) return 'WAIVER_EXPIRED';
  // 掉电：优先看观测信号，再看实时热状态（豁免可能在未发起观测时签发）
  const obsCodes = new Set((ev?.blockers || []).map((b) => b.code));
  const lossAfterGrant = cond && cond.lossCount > (w.baselineLossCount ?? 0);
  if (obsCodes.has('POWER_LOSS_AFTER_START') || (!cond?.powered && lossAfterGrant)) return 'POWER_LOSS';
  if (cond) {
    if (cond.overTemperature || cond.rateViolated) return 'OVER_TEMPERATURE';
  }
  if (obsCodes.has('DEVIATION_EXCEEDED') || obsCodes.has('RATE_EXCEEDED') || obsCodes.has('READING_STALE')) {
    return 'OVER_TEMPERATURE';
  }
  return null;
}

function restartHint(reason, ev) {
  if (reason === 'POWER_LOSS' || reason === 'SETPOINT_CHANGED' || reason === 'SENSOR_BOOT_CHANGED') {
    return { from: ev.progress?.restartFrom ?? null, remainingMs: ev.policy?.stableMs ?? null };
  }
  // 读数过期/偏差/速度：从当前时刻起继续观测，剩余稳定时长按阈值重计
  if (reason === 'READING_STALE' || reason === 'DEVIATION_EXCEEDED' || reason === 'RATE_EXCEEDED') {
    return { from: ev.progress?.qualifiedTo ?? null, remainingMs: ev.policy?.stableMs ?? null };
  }
  return { from: null, remainingMs: null };
}

// 供核心命令（采样/跳闸/拔除/削峰断电）使用：把它们的核心事实先在克隆状态上归并，
// 再对受影响箱做失效扫描。返回可与核心事实同批次提交的预冷失效事实。
// extraSample：尚未归并的新温度样本（采样接口同批次预判传感器更换/超温）。
export function sweepAfterCoreFacts(s, at, coreFacts, reeferIds, { extraSample = null } = {}) {
  const scratch = cloneState(s);
  for (const f of coreFacts) applyFact(scratch, f);
  const extraMap = extraSample ? new Map([[extraSample.reeferId, extraSample]]) : null;
  const out = sweep(scratch, at, { reeferIds, extraSampleByReefer: extraMap, apply: applyPrecoolFact });
  return { facts: out.facts, invalidations: out.invalidations };
}
