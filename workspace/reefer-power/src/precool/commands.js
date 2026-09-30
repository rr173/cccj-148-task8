'use strict';

// 装船前预冷合格证与放行互锁：命令处理器。
// 所有多步动作都与 monitor.sweep 产生的自动失效事实同批次落盘（原子批次），
// 因此“失效 + 队列/扫描 + 判定”要么整体可见，要么整体不存在。

import { bad, conflict, notFound, checkId, num } from '../util.js';
import { applyFact } from '../domain.js';
import { applyPrecoolFact, pcKey, nextPrecoolId } from './projection.js';
import { evaluateObservation, sweep, validCerts, activeWaivers } from './monitor.js';
import { cargoTier, tierLabel } from '../peak/risk.js';
import { policyForTier, maxWaiverMs } from './policy.js';
import { cloneState } from '../store.js';

const OPERATOR_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
const VOYAGE_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const OWNER_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;

function actor(body) {
  const id = String(body.operatorId ?? body.actor ?? 'DUTY').trim();
  if (!OPERATOR_RE.test(id)) throw bad('operatorId 非法');
  return id;
}
function voyage(body, required = true) {
  const id = body.voyageId != null ? String(body.voyageId) : null;
  if (required && (id == null || !VOYAGE_RE.test(id))) throw bad('voyageId 非法');
  return id;
}
function requireReefer(s, reeferId) {
  const r = s.reefers.get(reeferId);
  if (!r) throw notFound(`冷藏箱不存在: ${reeferId}`);
  return r;
}

// 在克隆状态上执行失效扫描，并把失效事实归并进克隆状态，使后续判定基于“本批次失效后”的最新状态。
function runSweep(s, at, opts) {
  const scratch = cloneState(s);
  const out = sweep(scratch, at, { ...opts, apply: applyPrecoolFact });
  return { scratch, sweepFacts: out.facts, invalidations: out.invalidations };
}

// 竞争收敛由两层保证：
//  1) 服务器用串行链把所有写命令排序，第二个处理器在第一个提交后才运行；
//  2) 处理器支持 expectedVersion 前置条件，并以 CERT_ALREADY_VALID / WAIVER_ALREADY_ACTIVE 兜底，
//     因此并发签发只可能产生单一证书或单一豁免，旧页面（旧 expectedVersion）不能覆盖后来的失效态。

function obsOf(s, voyageId, reeferId) {
  return s.precoolObs.get(pcKey(voyageId, reeferId)) || null;
}

// ---------- 发起 / 重新发起观测 ----------
export function cmdPrecoolObserve(s, body, at) {
  const voyageId = voyage(body);
  const reeferId = checkId(body.reeferId, '冷藏箱');
  const operatorId = actor(body);
  requireReefer(s, reeferId);

  const { scratch, sweepFacts, invalidations } = runSweep(s, at, { reeferIds: [reeferId] });

  const powered = scratch.leases.some((l) => l.reeferId === reeferId && l.state === 'POWER');
  if (!powered) {
    // 掉电期间不能开始计时；返回需要重新观测的起点（恢复供电后再发起）
    const r = requireReefer(scratch, reeferId);
    throw conflict(`箱 ${reeferId} 当前未由岸电供电，恢复供电后才能发起预冷观测`, {
      code: 'REEFER_NOT_POWERED',
      voyageId,
      reeferId,
      restartFrom: scratch.powerRestoredAt.get(reeferId) ?? null,
      facts: sweepFacts, // 供 server 同批次落盘（见 executePrecool）
      invalidations,
    });
  }

  const reefer = scratch.reefers.get(reeferId);
  const tier = cargoTier(reefer.cargoGrade);
  const policy = policyForTier(tier);
  const agg = obsOf(scratch, voyageId, reeferId);
  const prev = agg?.observation || null;
  const version = (agg?.version ?? 0) + 1;

  // 起点箱温读数若有，则锁定当前传感器启动代号；期间换传感器即判传感器更换
  const temps = [...scratch.samples.values()]
    .filter((x) => x.accepted && x.kind === 'temperature' && x.reeferId === reeferId)
    .sort((a, b) => b.measuredAt - a.measuredAt);
  const bootId = temps[0]?.sensorBootId ?? body.bootId ?? null;
  if (body.bootId && bootId && body.bootId !== bootId) {
    throw conflict(`请求中的 bootId=${body.bootId} 与最新实测温度的传感器启动代号 ${bootId} 不一致`, {
      code: 'SENSOR_BOOT_MISMATCH',
    });
  }

  const fact = {
    type: 'precoolObservationStarted',
    at,
    voyageId,
    reeferId,
    version,
    startedAt: at,
    samplesAfter: at,
    bootId,
    baselineLossCount: scratch.powerLossCount.get(reeferId) ?? 0,
    baselineSetpointC: reefer.setpointC,
    operatorId,
    reset: !!prev,
  };

  const facts = [...sweepFacts, fact];
  return {
    facts,
    enrich: (state) => {
      const ev = evaluateObservation(state, voyageId, reeferId, at);
      return {
        observing: true,
        reset: !!prev,
        voyageId,
        reeferId,
        startedAt: at,
        bootId,
        sensorBootId: bootId,
        tier,
        tierLabel: tierLabel(tier),
        stableRequiredMs: policy.stableMs,
        remainingMs: policy.stableMs,
        invalidations,
        progress: ev.progress,
        note: prev
          ? '已重新发起观测，合格计时重新开始'
          : '观测已开始，箱温持续合格达到稳定时间后方可签发合格证',
      };
    },
  };
}

// ---------- 签发预冷合格证 ----------
export function cmdCertIssue(s, body, at) {
  const voyageId = voyage(body);
  const reeferId = checkId(body.reeferId, '冷藏箱');
  const operatorId = actor(body);
  const reefer = requireReefer(s, reeferId);

  const { scratch, sweepFacts, invalidations } = runSweep(s, at, { reeferIds: [reeferId] });

  const agg = obsOf(scratch, voyageId, reeferId);
  if (body.expectedVersion != null && agg && Number(body.expectedVersion) !== agg.version) {
    throw conflict(`聚合版本已前进：期望 ${body.expectedVersion}，当前 ${agg.version}`, {
      code: 'VERSION_CONFLICT',
      currentVersion: agg.version,
      facts: sweepFacts,
      invalidations,
    });
  }

  const ev = evaluateObservation(scratch, voyageId, reeferId, at);
  const live = validCerts(scratch, voyageId, reeferId);
  if (live.length) {
    throw conflict(`箱 ${reeferId} 在航次 ${voyageId} 已有有效合格证 ${live[0].id}`, {
      code: 'CERT_ALREADY_VALID',
      certId: live[0].id,
      currentVersion: agg?.version ?? 0,
      facts: sweepFacts,
      invalidations,
    });
  }
  if (!agg?.observation) {
    throw conflict('尚未发起预冷观测，不能签发合格证', {
      code: 'OBSERVATION_NOT_STARTED',
      facts: sweepFacts,
      invalidations,
    });
  }
  if (!ev.ready) {
    throw conflict(`观测数据尚未持续合格，不能签发合格证: ${ev.blockers.map((b) => b.code).join(',')}`, {
      code: 'NOT_STABLE',
      blockers: ev.blockers,
      progress: ev.progress,
      restartFrom: ev.progress?.restartFrom ?? null,
      remainingMs: ev.progress?.remainingMs ?? null,
      facts: sweepFacts,
      invalidations,
    });
  }

  const version = agg.version + 1;
  const certId = nextPrecoolId(scratch, 'cert');
  const fact = {
    type: 'precoolCertificateIssued',
    at,
    certId,
    voyageId,
    reeferId,
    version,
    operatorId,
    bootId: ev.evidence?.bootId ?? agg.observation.bootId,
    setpointC: ev.setpointC,
    tier: ev.tier,
    cargoGrade: ev.cargoGrade,
    policy: ev.policy,
    evidence: ev.evidence,
  };
  const facts = [...sweepFacts, fact];
  return {
    facts,
    enrich: (state) => ({
      issued: true,
      certId,
      voyageId,
      reeferId,
      version,
      status: 'VALID',
      issuedAt: at,
      bootId: fact.bootId,
      sensorBootId: fact.bootId,
      tier: ev.tier,
      tierLabel: ev.tierLabel,
      policy: ev.policy,
      evidence: ev.evidence,
      observationSummary: summarizeEvidence(ev),
      invalidations,
    }),
  };
}

function summarizeEvidence(ev) {
  const e = ev.evidence;
  if (!e) return null;
  return `实测 ${e.sampleCount} 点 [${new Date(e.from).toISOString()} ~ ${new Date(e.to).toISOString()}]，` +
    `箱温 ${e.minC}~${e.maxC}℃ / 设定 ${e.setpointC}℃，最大 |变化| ${e.maxAbsRateCph}℃/h，传感器 ${e.bootId}`;
}

// ---------- 临时豁免 ----------
export function cmdWaiverGrant(s, body, at) {
  const voyageId = voyage(body);
  const reeferId = checkId(body.reeferId, '冷藏箱');
  const operatorId = actor(body);
  requireReefer(s, reeferId);
  const reason = String(body.reason ?? '').trim();
  if (reason.length < 2 || reason.length > 500) throw bad('豁免原因 reason 必须为 2~500 字');
  const cargoOwner = String(body.cargoOwner ?? body.cargoOwnerId ?? '').trim();
  if (!OWNER_RE.test(cargoOwner)) throw bad('货物负责人身份 cargoOwner 非法');

  const validMinutes = num(body.validMinutes ?? body.durationMinutes, '豁免有效分钟数', { min: 1, max: 24 * 60 });
  const validFrom = body.validFrom != null ? num(body.validFrom, 'validFrom', { integer: true }) : at;
  const validTo = body.validTo != null
    ? num(body.validTo, 'validTo', { integer: true })
    : Math.min(validFrom + validMinutes * 60000, at + maxWaiverMs());
  if (validTo <= at) throw bad('豁免有效期已结束（validTo 必须晚于当前时刻）');
  if (validTo - validFrom > maxWaiverMs()) throw bad(`豁免有效期不得超过 ${maxWaiverMs() / 60000} 分钟`);

  const { scratch, sweepFacts, invalidations } = runSweep(s, at, { reeferIds: [reeferId] });
  const agg = obsOf(scratch, voyageId, reeferId);
  if (body.expectedVersion != null && agg && Number(body.expectedVersion) !== agg.version) {
    throw conflict(`聚合版本已前进：期望 ${body.expectedVersion}，当前 ${agg.version}`, {
      code: 'VERSION_CONFLICT',
      currentVersion: agg.version,
      facts: sweepFacts,
      invalidations,
    });
  }

  const live = validCerts(scratch, voyageId, reeferId);
  if (live.length) {
    throw conflict(`箱 ${reeferId} 在航次 ${voyageId} 已有有效合格证 ${live[0].id}，无需豁免`, {
      code: 'CERT_ALREADY_VALID',
      certId: live[0].id,
      facts: sweepFacts,
      invalidations,
    });
  }
  const liveW = activeWaivers(scratch, voyageId, reeferId, at);
  if (liveW.length) {
    throw conflict(`箱 ${reeferId} 在航次 ${voyageId} 已有有效豁免 ${liveW[0].id}`, {
      code: 'WAIVER_ALREADY_ACTIVE',
      waiverId: liveW[0].id,
      facts: sweepFacts,
      invalidations,
    });
  }

  const version = (agg?.version ?? 0) + 1;
  const waiverId = nextPrecoolId(scratch, 'waiver');
  const fact = {
    type: 'precoolWaiverGranted',
    at,
    waiverId,
    voyageId,
    reeferId,
    version,
    operatorId,
    reason,
    cargoOwner,
    validFrom,
    validTo,
    baselineLossCount: scratch.powerLossCount.get(reeferId) ?? 0,
  };
  const facts = [...sweepFacts, fact];
  return {
    facts,
    enrich: () => ({
      granted: true,
      waiverId,
      voyageId,
      reeferId,
      version,
      status: 'ACTIVE',
      reason,
      cargoOwner,
      validFrom,
      validTo,
      expiresInMs: Math.max(0, validTo - at),
      invalidations,
      scope: `仅覆盖航次 ${voyageId} / 箱 ${reeferId}；后续超温或掉电即自动撤销`,
    }),
  };
}

// ---------- 装船确认 / 岸桥扫描（查询当前合格证并互锁） ----------
export function cmdGateScan(s, body, at) {
  const voyageId = voyage(body);
  const reeferId = checkId(body.reeferId, '冷藏箱');
  const scannerId = String(body.scannerId ?? body.operatorId ?? 'GATE').trim() || 'GATE';
  const stage = String(body.stage ?? 'GATE').toUpperCase() === 'LOAD_CONFIRM' ? 'LOAD_CONFIRM' : 'GATE';
  const slotCode = body.slotCode != null ? String(body.slotCode) : null;
  requireReefer(s, reeferId);

  const { scratch, sweepFacts, invalidations } = runSweep(s, at, { reeferIds: [reeferId] });
  const agg = obsOf(scratch, voyageId, reeferId);

  const certs = validCerts(scratch, voyageId, reeferId);
  const waivers = activeWaivers(scratch, voyageId, reeferId, at);
  const openQ = scratch.precoolPending.find(
    (q) => q.voyageId === voyageId && q.reeferId === reeferId && q.status === 'OPEN'
  );

  const facts = [...sweepFacts];
  let version = agg?.version ?? 0;
  let pendingId = openQ?.id ?? null;
  let queuedAt = at;
  const reasons = [];

  let outcome;
  let certId = null;
  let waiverId = null;

  if (certs.length) {
    outcome = 'RELEASED';
    certId = certs[0].id;
  } else if (waivers.length) {
    outcome = 'RELEASED';
    waiverId = waivers[0].id;
  } else {
    outcome = 'PENDING';
    const ev = evaluateObservation(scratch, voyageId, reeferId, at);
    reasons.push(...ev.blockers.map((b) => b.code));
    if (!reasons.length) reasons.push('NO_VALID_CERT');
    if (!openQ) {
      // 新箱入温控待处理队列；原装船计划与插座事实保持不变
      version += 1;
      pendingId = nextPrecoolId(scratch, 'pending');
      queuedAt = at;
      facts.push({
        type: 'precoolPendingQueued',
        at,
        pendingId,
        voyageId,
        reeferId,
        version,
        reasons,
        scanId: null,
        slotCode,
      });
    }
  }

  const scanId = nextPrecoolId(scratch, 'scan'); // 扫描事实不占聚合版本
  facts.push({
    type: 'precoolGateScanned',
    at,
    scanId,
    voyageId,
    reeferId,
    scannerId,
    stage,
    outcome,
    certId,
    waiverId,
    pendingId,
    queuedAt,
    reasons: outcome === 'PENDING' ? reasons : [],
    operationId: body.operationId ?? null,
    slotCode,
  });

  return {
    facts,
    enrich: () => ({
      outcome,
      released: outcome === 'RELEASED',
      scanId,
      voyageId,
      reeferId,
      stage,
      slotCode,
      certId,
      waiverId,
      pendingId,
      reasons: outcome === 'PENDING' ? reasons : [],
      basis: certId ? 'CERT' : waiverId ? 'WAIVER' : null,
      loadingPlanUntouched: true,
      socketFactUntouched: true,
      invalidations,
      note: outcome === 'RELEASED'
        ? (certId ? '有效合格证允许继续装船' : '有效豁免允许继续装船（仅本航次本箱）')
        : '缺失或失效：箱进入温控待处理队列；装船计划与插座事实保持不变',
    }),
  };
}

// ---------- 待处理队列处置：重新观测 / 豁免放行 / 关闭 ----------
export function cmdPendingResolve(s, body, at) {
  const voyageId = voyage(body);
  const reeferId = checkId(body.reeferId, '冷藏箱');
  const operatorId = actor(body);
  const action = String(body.action ?? '').toLowerCase();
  if (!['reobserve', 'waive', 'dismiss'].includes(action)) throw bad('action 必须是 reobserve / waive / dismiss');

  const { scratch, sweepFacts, invalidations } = runSweep(s, at, { reeferIds: [reeferId] });
  const q = scratch.precoolPending.find(
    (x) => x.voyageId === voyageId && x.reeferId === reeferId && x.status === 'OPEN'
  );
  if (!q) throw notFound(`箱 ${reeferId} 在航次 ${voyageId} 没有 OPEN 的待处理条目`);

  const facts = [...sweepFacts];
  const agg = obsOf(scratch, voyageId, reeferId);
  let version = agg.version;
  let waiverId = null;

  if (action === 'reobserve') {
    const powered = scratch.leases.some((l) => l.reeferId === reeferId && l.state === 'POWER');
    if (!powered) {
      throw conflict('箱尚未恢复供电，不能重新观测', {
        code: 'REEFER_NOT_POWERED',
        facts: sweepFacts,
        invalidations,
      });
    }
    const reefer = scratch.reefers.get(reeferId);
    version += 1;
    const temps = [...scratch.samples.values()]
      .filter((x) => x.accepted && x.kind === 'temperature' && x.reeferId === reeferId)
      .sort((a, b) => b.measuredAt - a.measuredAt);
    facts.push({
      type: 'precoolObservationStarted',
      at,
      voyageId,
      reeferId,
      version,
      startedAt: at,
      samplesAfter: at,
      bootId: temps[0]?.sensorBootId ?? null,
      baselineLossCount: scratch.powerLossCount.get(reeferId) ?? 0,
      baselineSetpointC: reefer.setpointC,
      operatorId,
      reset: true,
    });
  } else if (action === 'waive') {
    const reason = String(body.reason ?? '').trim();
    if (reason.length < 2) throw bad('waive 必须提供 reason');
    const cargoOwner = String(body.cargoOwner ?? '').trim();
    if (!OWNER_RE.test(cargoOwner)) throw bad('waive 必须提供货物负责人 cargoOwner');
    const validMinutes = num(body.validMinutes ?? 60, '豁免有效分钟数', { min: 1, max: 24 * 60 });
    const validTo = Math.min(at + validMinutes * 60000, at + maxWaiverMs());
    if (validCerts(scratch, voyageId, reeferId).length) {
      throw conflict('箱已有有效合格证，无需豁免', { code: 'CERT_ALREADY_VALID' });
    }
    version += 1;
    waiverId = nextPrecoolId(scratch, 'waiver');
    facts.push({
      type: 'precoolWaiverGranted',
      at,
      waiverId,
      voyageId,
      reeferId,
      version,
      operatorId,
      reason,
      cargoOwner,
      validFrom: at,
      validTo,
      baselineLossCount: scratch.powerLossCount.get(reeferId) ?? 0,
    });
  }

  version += 1;
  facts.push({
    type: 'precoolPendingResolved',
    at,
    pendingId: q.id,
    voyageId,
    reeferId,
    version,
    resolution: action === 'waive' ? 'WAIVED' : action === 'reobserve' ? 'REOBSERVING' : 'DISMISSED',
    operatorId,
  });

  return {
    facts,
    enrich: (state) => {
      const ev = evaluateObservation(state, voyageId, reeferId, at);
      return {
        resolved: true,
        pendingId: q.id,
        resolution: action === 'waive' ? 'WAIVED' : action === 'reobserve' ? 'REOBSERVING' : 'DISMISSED',
        waiverId,
        invalidations,
        progress: ev.progress,
      };
    },
  };
}

// ---------- 设定点调整（使既有证书立即失效、观测重新计时） ----------
export function cmdSetpointAdjust(s, body, at) {
  const reeferId = checkId(body.reeferId, '冷藏箱');
  const operatorId = actor(body);
  const reefer = requireReefer(s, reeferId);
  const toC = num(body.toC ?? body.setpointC, '新设定点(℃)', { min: -100, max: 100 });
  if (reefer.setpointC != null && Math.abs(reefer.setpointC - toC) < 1e-9) {
    return { facts: [], reply: { changed: false, alreadySet: true, setpointC: toC } };
  }

  const reeferIds = [reeferId];
  const { scratch, sweepFacts, invalidations } = runSweep(s, at, { reeferIds });

  const fact = { type: 'setpointAdjusted', at, reeferId, fromC: reefer.setpointC, toC, operatorId };
  // 设定点事实在 scratch 上归并后，再扫描受影响航次的证书/观测失效
  applyFact(scratch, fact);
  const after = sweep(scratch, at, { reeferIds, apply: applyPrecoolFact });

  return {
    facts: [...sweepFacts, fact, ...after.facts],
    enrich: () => ({
      changed: true,
      reeferId,
      fromC: reefer.setpointC,
      toC,
      invalidations: [...invalidations, ...after.invalidations],
      note: '设定点已调整：相关合格证立即失效，观测需从调整时刻重新计时',
    }),
  };
}
