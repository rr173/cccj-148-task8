'use strict';

// 预冷合格证互锁的读模型：合格进度、证据区间、失效原因、待处理箱、豁免倒计时、放行结果。
// 证书的“当前是否有效”在读时动态复核（读数过期等时间相关失效在下次写命令时才落失效事实，
// 但页面/接口必须立刻呈现失效态），动态失效态以 `live:false / liveReason` 标明。

import { pcKey } from './projection.js';
import { evaluateObservation, activeWaivers } from './monitor.js';
import { cargoTier, tierLabel } from '../peak/risk.js';
import { policyForTier } from './policy.js';

function certLiveStatus(s, cert, at) {
  if (cert.status !== 'VALID') return { live: false, reason: cert.invalidReason, persisted: true };
  // 动态复核：以证书签发时冻结的策略阈值评估当前观测
  const ev = evaluateObservation(s, cert.voyageId, cert.reeferId, at, { policyOverride: cert.policy });
  const code = ev.blockers[0]?.code ?? null;
  if (!code) return { live: true, reason: null, persisted: false };
  const map = {
    POWER_LOSS_AFTER_START: 'POWER_LOSS',
    SETPOINT_CHANGED_AFTER_START: 'SETPOINT_CHANGED',
    SENSOR_BOOT_CHANGED: 'SENSOR_BOOT_CHANGED',
    READING_STALE: 'READING_STALE',
    DEVIATION_EXCEEDED: 'DEVIATION_EXCEEDED',
    RATE_EXCEEDED: 'RATE_EXCEEDED',
    REEFER_NOT_POWERED: 'POWER_LOSS',
  };
  return { live: false, reason: map[code] || code, persisted: false, detail: ev.blockers[0]?.detail ?? null };
}

export function buildPrecoolView(s, at) {
  // 观测进度（每个聚合）
  const observations = [];
  for (const agg of s.precoolObs.values()) {
    const ev = evaluateObservation(s, agg.voyageId, agg.reeferId, at);
    const reefer = s.reefers.get(agg.reeferId);
    observations.push({
      key: agg.key,
      voyageId: agg.voyageId,
      reeferId: agg.reeferId,
      version: agg.version,
      cargoGrade: reefer?.cargoGrade ?? null,
      tier: ev.tier,
      tierLabel: ev.tierLabel,
      status: ev.status,
      ready: !!ev.ready,
      blockers: ev.blockers.map((b) => ({ code: b.code, message: b.message, detail: b.detail })),
      progress: ev.progress,
      evidence: ev.evidence,
      observationStartedAt: agg.observation?.startedAt ?? null,
      observationBootId: agg.observation?.bootId ?? null,
    });
  }
  observations.sort((a, b) => a.voyageId.localeCompare(b.voyageId) || a.reeferId.localeCompare(b.reeferId));

  const certs = s.precoolCerts.map((c) => {
    const live = certLiveStatus(s, c, at);
    return {
      id: c.id,
      voyageId: c.voyageId,
      reeferId: c.reeferId,
      version: c.version,
      status: c.status,
      live: live.live,
      liveReason: live.reason,
      liveStatusPersisted: live.persisted,
      issuedAt: c.issuedAt,
      issuedBy: c.issuedBy,
      sensorBootId: c.bootId,
      setpointC: c.setpointC,
      tier: c.tier,
      tierLabel: tierLabel(c.tier),
      cargoGrade: c.cargoGrade,
      policy: c.policy,
      evidence: c.evidence,
      invalidatedAt: c.invalidatedAt,
      invalidReason: c.invalidReason,
      restartFrom: c.restartFrom,
      restartRemainingMs: c.restartRemainingMs,
    };
  }).sort((a, b) => b.issuedAt - a.issuedAt || a.id.localeCompare(b.id));

  const waivers = s.precoolWaivers.map((w) => {
    let liveStatus;
    if (w.status !== 'ACTIVE') liveStatus = w.status;
    else if (at >= w.validTo) liveStatus = 'EXPIRED_DYNAMIC';
    else liveStatus = 'ACTIVE';
    return {
      id: w.id,
      voyageId: w.voyageId,
      reeferId: w.reeferId,
      version: w.version,
      status: w.status,
      liveStatus,
      live: liveStatus === 'ACTIVE',
      grantedAt: w.grantedAt,
      grantedBy: w.grantedBy,
      reason: w.reason,
      cargoOwner: w.cargoOwner,
      validFrom: w.validFrom,
      validTo: w.validTo,
      remainingMs: Math.max(0, w.validTo - at),
      revokedAt: w.revokedAt,
      revokeReason: w.revokeReason,
    };
  }).sort((a, b) => b.grantedAt - a.grantedAt || a.id.localeCompare(b.id));

  // 待处理队列
  const pending = s.precoolPending.map((q) => {
    const scans = s.precoolScans.filter(
      (g) => g.voyageId === q.voyageId && g.reeferId === q.reeferId
    ).sort((a, b) => a.at - b.at);
    const lastScan = scans[scans.length - 1] || null;
    return {
      id: q.id,
      voyageId: q.voyageId,
      reeferId: q.reeferId,
      version: q.version,
      status: q.status,
      enteredAt: q.enteredAt,
      reasons: q.reasons,
      lastScanAt: lastScan?.at ?? null,
      lastSlotCode: lastScan?.slotCode ?? q.lastSlotCode ?? null,
      resolvedAt: q.resolvedAt,
      resolution: q.resolution,
      resolvedBy: q.resolvedBy,
      // 队列中的箱此刻是否已具备放行依据（证/豁免），供值班员判断
      releasableNow:
        q.status === 'OPEN' &&
        (s.precoolCerts.some((c) => c.id === lastScan?.certId && c.status === 'VALID') ||
          (!!activeWaivers(s, q.voyageId, q.reeferId, at).length)),
    };
  }).sort((a, b) => (a.status === 'OPEN' ? -1 : 1) || a.enteredAt - b.enteredAt);

  const scans = [...s.precoolScans]
    .sort((a, b) => b.at - a.at || a.id.localeCompare(b.id))
    .map((g) => {
      // 放行结果读时复核：引用的证/豁免已失效则呈现 SUPERSEDED（历史事实本身不可变）
      let effectiveOutcome = g.outcome;
      if (g.outcome === 'RELEASED') {
        if (g.certId) {
          const c = s.precoolCerts.find((x) => x.id === g.certId);
          const live = c ? certLiveStatus(s, c, at) : { live: false };
          if (!live.live) effectiveOutcome = 'RELEASE_NOW_VOID';
        } else if (g.waiverId) {
          const w = s.precoolWaivers.find((x) => x.id === g.waiverId);
          if (!w || w.status !== 'ACTIVE' || at >= w.validTo) effectiveOutcome = 'RELEASE_NOW_VOID';
        }
      }
      return { ...g, effectiveOutcome };
    });

  // 每个航次当前的放行状态汇总
  const releaseByVoyage = {};
  for (const g of scans) {
    const cur = releaseByVoyage[g.voyageId] || { released: new Set(), pending: new Set() };
    if (g.effectiveOutcome === 'RELEASED') {
      cur.pending.delete(g.reeferId);
      cur.released.add(g.reeferId);
    } else if (g.outcome === 'PENDING') {
      if (!cur.released.has(g.reeferId)) cur.pending.add(g.reeferId);
    }
    releaseByVoyage[g.voyageId] = cur;
  }
  const voyages = Object.entries(releaseByVoyage).map(([voyageId, v]) => ({
    voyageId,
    releasedReefers: [...v.released].sort(),
    pendingReefers: [...v.pending].sort(),
  }));

  return {
    at,
    observations,
    certs,
    waivers,
    pending,
    scans: scans.slice(0, 200),
    voyages,
    stats: {
      validCerts: certs.filter((c) => c.live).length,
      invalidCerts: certs.filter((c) => !c.live).length,
      activeWaivers: waivers.filter((w) => w.live).length,
      openPending: pending.filter((q) => q.status === 'OPEN').length,
      releasedScans: scans.filter((g) => g.effectiveOutcome === 'RELEASED').length,
      pendingScans: scans.filter((g) => g.outcome === 'PENDING').length,
    },
  };
}

// 单点放行判定：供装船确认/岸桥扫描与 GET 查询共用
export function releaseDecision(s, voyageId, reeferId, at) {
  const cert = s.precoolCerts
    .filter((c) => c.voyageId === voyageId && c.reeferId === reeferId && c.status === 'VALID')
    .map((c) => ({ cert: c, live: certLiveStatus(s, c, at) }))
    .find((x) => x.live.live) || null;
  if (cert) {
    return { released: true, basis: 'CERT', certId: cert.cert.id, waiverId: null, reasons: [] };
  }
  const w = activeWaivers(s, voyageId, reeferId, at)[0];
  if (w) {
    return { released: true, basis: 'WAIVER', certId: null, waiverId: w.id, reasons: [], waiverValidTo: w.validTo };
  }
  const ev = evaluateObservation(s, voyageId, reeferId, at);
  return {
    released: false,
    basis: null,
    certId: null,
    waiverId: null,
    reasons: ev.blockers.map((b) => b.code),
    progress: ev.progress,
  };
}

// 供其他模块（如页面汇总卡片）查询某航次所有箱的放行状态
export function voyageReleaseStatus(s, voyageId, at) {
  const out = [];
  for (const reeferId of s.reefers.keys()) {
    out.push({ reeferId, ...releaseDecision(s, voyageId, reeferId, at) });
  }
  return out;
}

// 仅供测试/页面需要时按货物等级取策略
export { policyForTier, cargoTier };
