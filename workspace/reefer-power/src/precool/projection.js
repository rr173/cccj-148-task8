'use strict';

// 装船前预冷合格证互锁：不可变流水事实的确定性归并。
// 证书与豁免按 (航次, 箱号) 聚合，每个事实携带聚合版本号；
// 状态只能单向流转 VALID -> INVALIDATED、ACTIVE -> REVOKED/EXPIRED，已失效永不复活。
// 所有对象只追加：同一观测区间签不出两张有效证书，靠归并期不变量兜底。

export function pcKey(voyageId, reeferId) {
  return `${voyageId}|${reeferId}`;
}

// 统一序号在“真实状态”上单调递增；命令处理器在克隆状态上预演时也调用本函数，
// 克隆不会写回，因此不能依赖可变计数器——这里按各列表中已分配的同前缀最大序号推导。
// 同一批次连续分配多个 ID 时传入 bump 偏移（0=本批次第 1 个）。
export function nextPrecoolId(s, kind, bump = 0) {
  const prefix = { cert: 'PC', waiver: 'WV', scan: 'GS', pending: 'TQ' }[kind] || 'PC';
  let max = 0;
  const consider = (id) => {
    if (typeof id === 'string' && id.startsWith(prefix)) {
      const n = Number(id.slice(prefix.length));
      if (Number.isInteger(n) && n > max) max = n;
    }
  };
  s.precoolCerts.forEach((c) => consider(c.id));
  s.precoolWaivers.forEach((w) => consider(w.id));
  s.precoolScans.forEach((g) => consider(g.id));
  s.precoolPending.forEach((q) => consider(q.id));
  const n = Math.max(max, s.precoolSeq) + 1 + bump;
  if (n > s.precoolSeq) s.precoolSeq = n;
  return `${prefix}${String(n).padStart(5, '0')}`;
}

function getAgg(s, voyageId, reeferId) {
  const k = pcKey(voyageId, reeferId);
  if (!s.precoolObs.has(k)) s.precoolObs.set(k, { key: k, voyageId, reeferId, version: 0 });
  return s.precoolObs.get(k);
}

function checkVersion(agg, factVersion, tag) {
  if (factVersion !== agg.version + 1) {
    throw new Error(`预冷聚合版本不前进 (${tag}): ${agg.key} 期望 ${agg.version + 1}，实际 ${factVersion}`);
  }
}

export function applyPrecoolFact(s, f) {
  switch (f.type) {
    case 'precoolObservationStarted': return applyObservationStarted(s, f);
    case 'precoolCertificateIssued': return applyCertificateIssued(s, f);
    case 'precoolCertificateInvalidated': return applyCertificateInvalidated(s, f);
    case 'precoolWaiverGranted': return applyWaiverGranted(s, f);
    case 'precoolWaiverRevoked': return applyWaiverRevoked(s, f);
    case 'precoolGateScanned': return applyGateScanned(s, f);
    case 'precoolPendingQueued': return applyPendingQueued(s, f);
    case 'precoolPendingResolved': return applyPendingResolved(s, f);
    default:
      throw new Error(`未知预冷事实类型: ${f.type}`);
  }
}

function applyObservationStarted(s, f) {
  const agg = getAgg(s, f.voyageId, f.reeferId);
  checkVersion(agg, f.version, 'observation');
  agg.version = f.version;
  agg.observation = {
    startedAt: f.startedAt,
    samplesAfter: f.samplesAfter ?? f.startedAt,
    bootId: f.bootId ?? null,
    baselineLossCount: f.baselineLossCount,
    baselineSetpointC: f.baselineSetpointC,
    operatorId: f.operatorId,
    reset: f.reset ?? false,
  };
  return s;
}

function applyCertificateIssued(s, f) {
  const agg = getAgg(s, f.voyageId, f.reeferId);
  checkVersion(agg, f.version, 'issue');
  agg.version = f.version;
  s.precoolCerts.push({
    id: f.certId,
    voyageId: f.voyageId,
    reeferId: f.reeferId,
    version: f.version,
    status: 'VALID',
    issuedAt: f.at,
    issuedBy: f.operatorId,
    bootId: f.bootId,
    setpointC: f.setpointC,
    tier: f.tier,
    cargoGrade: f.cargoGrade,
    policy: { ...f.policy },
    evidence: { ...f.evidence },
    invalidatedAt: null,
    invalidReason: null,
    restartFrom: null,
    restartRemainingMs: null,
  });
  return s;
}

function applyCertificateInvalidated(s, f) {
  const agg = getAgg(s, f.voyageId, f.reeferId);
  checkVersion(agg, f.version, 'invalidate');
  agg.version = f.version;
  const cert = s.precoolCerts.find((c) => c.id === f.certId);
  if (!cert) throw new Error(`失效事实引用了未知证书: ${f.certId}`);
  if (cert.status === 'VALID') {
    cert.status = 'INVALIDATED';
    cert.invalidatedAt = f.at;
    cert.invalidReason = f.reason;
    cert.restartFrom = f.restartFrom ?? null;
    cert.restartRemainingMs = f.restartRemainingMs ?? null;
  }
  // 重放幂等：同一失效事实再次归并不改写状态（已失效永不复活）
  return s;
}

function applyWaiverGranted(s, f) {
  const agg = getAgg(s, f.voyageId, f.reeferId);
  checkVersion(agg, f.version, 'waiver');
  agg.version = f.version;
  s.precoolWaivers.push({
    id: f.waiverId,
    voyageId: f.voyageId,
    reeferId: f.reeferId,
    version: f.version,
    status: 'ACTIVE',
    grantedAt: f.at,
    grantedBy: f.operatorId,
    reason: f.reason,
    cargoOwner: f.cargoOwner,
    validFrom: f.validFrom,
    validTo: f.validTo,
    baselineLossCount: f.baselineLossCount ?? 0,
    revokedAt: null,
    revokeReason: null,
  });
  return s;
}

function applyWaiverRevoked(s, f) {
  const agg = getAgg(s, f.voyageId, f.reeferId);
  checkVersion(agg, f.version, 'waiver-revoke');
  agg.version = f.version;
  const w = s.precoolWaivers.find((x) => x.id === f.waiverId);
  if (!w) throw new Error(`豁免撤销事实引用了未知豁免: ${f.waiverId}`);
  if (w.status === 'ACTIVE') {
    w.status = f.reason === 'WAIVER_EXPIRED' ? 'EXPIRED' : 'REVOKED';
    w.revokedAt = f.at;
    w.revokeReason = f.reason;
  }
  return s;
}

function applyGateScanned(s, f) {
  s.precoolScans.push({
    id: f.scanId,
    at: f.at,
    voyageId: f.voyageId,
    reeferId: f.reeferId,
    scannerId: f.scannerId,
    stage: f.stage ?? 'GATE',
    outcome: f.outcome,
    certId: f.certId ?? null,
    waiverId: f.waiverId ?? null,
    pendingId: f.pendingId ?? null,
    reasons: f.reasons ?? [],
    operationId: f.operationId ?? null,
    slotCode: f.slotCode ?? null,
  });
  return s;
}

function applyPendingQueued(s, f) {
  const agg = getAgg(s, f.voyageId, f.reeferId);
  checkVersion(agg, f.version, 'pending');
  agg.version = f.version;
  // 同一聚合至多一个 open 待处理条目：旧 open 条目先关闭（由解析事实关闭），此处只追加
  s.precoolPending.push({
    id: f.pendingId,
    voyageId: f.voyageId,
    reeferId: f.reeferId,
    version: f.version,
    status: 'OPEN',
    enteredAt: f.at,
    reasons: f.reasons ?? [],
    lastScanId: f.scanId ?? null,
    lastSlotCode: f.slotCode ?? null,
    resolvedAt: null,
    resolution: null,
    resolvedBy: null,
  });
  return s;
}

function applyPendingResolved(s, f) {
  const agg = getAgg(s, f.voyageId, f.reeferId);
  checkVersion(agg, f.version, 'pending-resolve');
  agg.version = f.version;
  const q = s.precoolPending.find((x) => x.id === f.pendingId);
  if (!q) throw new Error(`待处理解析事实引用了未知条目: ${f.pendingId}`);
  if (q.status === 'OPEN') {
    q.status = 'RESOLVED';
    q.resolvedAt = f.at;
    q.resolution = f.resolution;
    q.resolvedBy = f.operatorId;
  }
  return s;
}

// ---------- 不变量 ----------
export function precoolInvariants(s, tag) {
  // 每个 (航次,箱号) 至多一张有效证书、至多一个有效豁免
  const liveCerts = new Map();
  const liveWaivers = new Map();
  for (const c of s.precoolCerts) {
    if (c.status !== 'VALID') continue;
    const k = pcKey(c.voyageId, c.reeferId);
    const prev = liveCerts.get(k);
    if (prev) throw new Error(`不变量被破坏 (${tag}): ${k} 存在两张有效证书 ${prev} / ${c.id}`);
    liveCerts.set(k, c.id);
  }
  for (const w of s.precoolWaivers) {
    if (w.status !== 'ACTIVE') continue;
    const k = pcKey(w.voyageId, w.reeferId);
    const prev = liveWaivers.get(k);
    if (prev) throw new Error(`不变量被破坏 (${tag}): ${k} 存在两个有效豁免 ${prev} / ${w.id}`);
    liveWaivers.set(k, w.id);
  }
  // 每个聚合至多一个 open 待处理条目
  const openPending = new Map();
  for (const q of s.precoolPending) {
    if (q.status !== 'OPEN') continue;
    const k = pcKey(q.voyageId, q.reeferId);
    const prev = openPending.get(k);
    if (prev) throw new Error(`不变量被破坏 (${tag}): ${k} 存在两个 OPEN 待处理条目 ${prev} / ${q.id}`);
    openPending.set(k, q.id);
  }
  // 说明：放行扫描是不可变历史事实——放行时引用有效证/豁免，之后证/豁免失效属正常业务
  // （该箱随后重新进入待处理），因此不做“扫描引用必须仍有效”的状态不变量。
  // 放行与失效不会撕裂：扫描命令在克隆状态上先应用同批次失效事实再做判定（见 commands.js）。
  // 版本号严格单调（聚合内事实序号 = version+1）
  for (const agg of s.precoolObs.values()) {
    let maxV = 0;
    for (const c of s.precoolCerts) if (pcKey(c.voyageId, c.reeferId) === agg.key && c.version > maxV) maxV = c.version;
    for (const w of s.precoolWaivers) if (pcKey(w.voyageId, w.reeferId) === agg.key && w.version > maxV) maxV = w.version;
    for (const q of s.precoolPending) if (pcKey(q.voyageId, q.reeferId) === agg.key && q.version > maxV) maxV = q.version;
    if (agg.observation && agg.version < maxV) {
      throw new Error(`不变量被破坏 (${tag}): 聚合 ${agg.key} 版本 ${agg.version} 落后于事实版本 ${maxV}`);
    }
  }
}
