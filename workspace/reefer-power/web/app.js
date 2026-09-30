'use strict';

let state = null;

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (x) => String(x ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const fmtTime = (t) => (t == null ? '—' : new Date(t).toLocaleTimeString('zh-CN', { hour12: false }));
const fmtDateTime = (t) => (t == null ? '—' : new Date(t).toLocaleString('zh-CN', { hour12: false }));

async function api(path, body, opts = {}) {
  const headers = { 'content-type': 'application/json' };
  if (opts.commandId) headers['x-command-id'] = opts.commandId;
  const res = await fetch(path, {
    method: 'POST',
    headers,
    body: JSON.stringify(body ?? {}),
  });
  const json = await res.json();
  if (!res.ok || json.ok === false) {
    const e = new Error(json.error?.message || `HTTP ${res.status}`);
    e.payload = json;
    e.status = res.status;
    throw e;
  }
  return json;
}

function toast(msg, kind = 'ok') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $('#toast').appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

// ---------------- 渲染 ----------------
function render() {
  if (!state) return;
  $('#clock').textContent = new Date(state.at).toLocaleString('zh-CN', { hour12: false });
  renderSummary();
  renderTopology();
  renderReefers();
  renderPeak();
  renderPrecool();
  renderTrips();
  renderSamples();
}

function renderSummary() {
  const reefers = state.reefers;
  const powered = reefers.filter((r) => r.status === 'POWERED').length;
  const unpowered = reefers.filter((r) => r.status === 'UNPOWERED').length;
  const reserved = reefers.filter((r) => r.status === 'RESERVED').length;
  const loss = reefers.filter((r) => r.status === 'POWER_LOSS').length;
  const tripped = state.circuits.filter((c) => c.state === 'TRIPPED').length;
  const q = state.samples.quarantined.length;
  const activePlans = state.plans.filter((p) => p.state === 'ACTIVE').length;
  const overloaded = state.circuits.filter((c) => c.overloaded).length;
  const peak = state.peak;
  const budgetActive = peak?.budget?.active;
  const overBudget = budgetActive && peak.load.shipNetA > (peak.budget.shipLimitA ?? Infinity);
  const effectiveCuts = peak ? peak.actions.filter((a) => a.status === 'EFFECTIVE' && a.kind === 'CUT').length : 0;
  const effectiveLimits = peak ? peak.actions.filter((a) => a.status === 'EFFECTIVE' && a.kind === 'LIMIT').length : 0;
  const recoveryPaused = peak?.recovery?.paused;

  $('#summary').innerHTML = [
    ['总箱数', reefers.length, ''],
    ['已供电', powered, 'ok'],
    ['预留中', reserved, 'warn'],
    ['未接电', unpowered, 'warn'],
    ['掉电', loss, loss ? 'alert' : ''],
    ['跳闸回路', tripped, tripped ? 'alert' : ''],
    ['过载回路', overloaded, overloaded ? 'alert' : ''],
    ['迁移中方案', activePlans, activePlans ? 'warn' : ''],
    ['生效预算', budgetActive ? `${peak.budget.shipLimitA ?? '—'}A` : '无', budgetActive ? (overBudget ? 'alert' : 'ok') : ''],
    ['越限', overBudget ? '是' : '否', overBudget ? 'alert' : ''],
    ['限功率中', effectiveLimits, effectiveLimits ? 'warn' : ''],
    ['断电中', effectiveCuts, effectiveCuts ? 'alert' : ''],
    ['当前节省', peak ? `${peak.savings.currentSavingA}A` : '0A', 'ok'],
    ['恢复暂停', recoveryPaused ? '是' : '否', recoveryPaused ? 'alert' : ''],
    ...(peak?.stats ? [
      ['有效合格证', state.precool?.stats.validCerts ?? 0, 'ok'],
      ['失效合格证', state.precool?.stats.invalidCerts ?? 0, (state.precool?.stats.invalidCerts ?? 0) ? 'warn' : ''],
      ['有效豁免', state.precool?.stats.activeWaivers ?? 0, (state.precool?.stats.activeWaivers ?? 0) ? 'warn' : ''],
      ['温控待处理', state.precool?.stats.openPending ?? 0, (state.precool?.stats.openPending ?? 0) ? 'alert' : ''],
    ] : []),
    ['待审样本', q, q ? 'warn' : ''],
  ].map(([l, n, cls]) => `<div class="card ${cls}"><div class="n">${n}</div><div class="l">${l}</div></div>`).join('');
}

function meter(c) {
  const pct = Math.min(100, Math.round((c.loadA / c.ratedCurrentA) * 100));
  return `<div class="meter">
    <div class="track"><div class="fill ${c.overloaded ? 'over' : ''}" style="width:${pct}%"></div></div>
    <div class="nums">${c.loadA} / ${c.ratedCurrentA} A · 余量 ${c.headroomA} A${c.overloaded ? ' · <span class="danger">过载</span>' : ''}</div>
  </div>`;
}

function socketHtml(s) {
  const who = s.reeferId
    ? `<div class="who">${esc(s.reeferId)}${s.measuredCurrentA != null ? ` · ${s.measuredCurrentA}A` : ''}</div>`
    : '<div class="who muted">空闲</div>';
  return `<div class="socket">
    <div class="sid"><span>${esc(s.id)}</span><span class="tag ${s.status}">${s.status}</span></div>
    <div class="meta">${esc(s.phase)} · ${s.ratedCurrentA}A · ${esc(s.allowedBoxTypes.join('/'))} · ${esc(s.reachAreas.join('/'))}</div>
    ${who}
  </div>`;
}

function renderTopology() {
  const boards = state.boards.map((b) => {
    const circuits = state.circuits.filter((c) => c.boardId === b.id);
    return `<div class="board">
      <h3>配电板 ${esc(b.id)} · ${esc(b.name)}</h3>
      ${circuits.map((c) => `<div class="circuit ${c.state === 'TRIPPED' ? 'tripped' : ''}">
        <div class="circuit-head">
          <span class="name">${esc(c.id)} ${esc(c.name)}</span>
          <span class="tag p${c.phase === '3P' ? 3 : 1}">${c.phase}</span>
          <span class="tag ${c.state}">${c.state === 'TRIPPED' ? `跳闸 ${fmtTime(c.trippedAt)}` : '正常'}</span>
          ${meter(c)}
          ${c.state === 'TRIPPED'
            ? `<button class="warn" onclick="planMigration('${c.id}')">迁移方案</button> <button class="ghost" onclick="resetCircuit('${c.id}')">复位</button>`
            : ''}
        </div>
        <div class="sockets">${c.sockets.map(socketHtml).join('')}</div>
      </div>`).join('')}
    </div>`;
  }).join('');
  $('#tab-topology').innerHTML = boards || '<p class="muted">尚未录入配电拓扑。请到「录入/导入」页提交。</p>';
}

function renderReefers() {
  const rows = state.reefers.map((r) => {
    const ins = state.insulation[r.id];
    const statusTxt = {
      POWERED: '<span class="good">供电</span>',
      RESERVED: '<span class="warn">已预留</span>',
      UNPOWERED: '<span class="muted">未接电</span>',
      POWER_LOSS: '<span class="danger">掉电</span>',
    }[r.status];
    return `<tr>
      <td><b>${esc(r.id)}</b></td>
      <td>${esc(r.boxType)}</td>
      <td>${r.phase}</td>
      <td>${r.ratedCurrentA}A${r.powerKw != null ? `<div class="muted">${r.powerKw}kW</div>` : ''}</td>
      <td>${esc(r.area)}</td>
      <td>${r.tempPriority}</td>
      <td>${r.insulationHours}h${ins ? `<div class="muted ${ins.expired ? 'danger' : ''}">剩余 ${ins.remainingMinutes} 分钟</div>` : ''}</td>
      <td>${esc(r.socketId) ?? '—'}</td>
      <td>${statusTxt}</td>
    </tr>`;
  }).join('');
  $('#tab-reefers').innerHTML = `<div class="panel">
    <h3>冷藏箱（${state.reefers.length}）</h3>
    <table><thead><tr><th>箱号</th><th>箱型</th><th>相制</th><th>额定</th><th>区域</th><th>温控优先级</th><th>保温时限</th><th>插座</th><th>状态</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

// ---------------- 功率预算 / 削峰 ----------------
function renderPeak() {
  const p = state.peak;
  if (!p) { $('#tab-peak').innerHTML = '<p class="muted">无削峰数据。</p>'; return; }
  const b = p.budget;

  const budgetHtml = `<div class="panel">
    <h3>当前生效预算 ${b.active ? `<span class="tag">生效中 · 剩余 ${b.secondsRemaining}s</span>` : '<span class="muted">无</span>'}</h3>
    ${b.active ? `<p>全船上限 <b>${b.shipLimitA ?? '—'}A</b>；净载 <b>${p.load.shipNetA}A</b>（毛载 ${p.load.shipGrossA}A）
        <span class="${p.load.shipWithinLimit ? 'good' : 'danger'}">${p.load.shipWithinLimit ? '✓ 在上限内' : '✗ 越限'}</span></p>
      <table><thead><tr><th>配电板</th><th>毛载A</th><th>净载A</th><th>上限A</th><th>已释放A</th><th>状态</th></tr></thead><tbody>
        ${p.load.boards.map((x) => `<tr><td>${esc(x.boardId)}</td><td>${x.grossA}</td><td>${x.netA}</td><td>${x.limitA ?? '—'}</td><td>${x.releasedA}</td>
          <td>${x.withinLimit ? '<span class="good">✓</span>' : '<span class="danger">✗ 越限</span>'}</td></tr>`).join('')}
      </tbody></table>
      <p class="muted">合并自预算：${(b.sourceBudgetIds || []).join(', ')} · 区间 ${fmtDateTime(b.startAt)} ~ ${fmtDateTime(b.endAt)}</p>`
      : '<p class="muted">当前时刻没有生效中的预算区间。</p>'}
    <h3>预算曲线</h3>
    ${p.budgetCurve.length ? `<table><thead><tr><th>开始</th><th>结束</th><th>全船上限A</th><th>配电板上限</th></tr></thead><tbody>
      ${p.budgetCurve.map((g) => `<tr><td>${fmtDateTime(g.startAt)}</td><td>${fmtDateTime(g.endAt)}</td><td>${g.shipLimitA ?? '—'}</td>
        <td>${Object.entries(g.boardLimitsA || {}).map(([k, v]) => `${k}:${v}`).join('，') || '—'}</td></tr>`).join('')}
      </tbody></table>` : '<p class="muted">无预算区间。</p>'}
  </div>`;

  const rev = p.currentRevision;
  const revHtml = `<div class="panel">
    <h3>方案修订 ${rev ? `<span class="tag">R${rev.rev} · ${rev.status}${rev.stale ? ' · 已失效需重算' : ''}</span>` : ''}</h3>
    ${rev ? `<p>${esc(rev.summary)} ${rev.stale ? '<span class="danger">电流/温度变化使原计算失效</span>' : ''}</p>
      <p class="muted">值班员 ${esc(rev.operatorId)} · ${fmtDateTime(rev.at)}</p>
      ${rev.feasible ? '' : `<p class="danger">无法满足预算：${esc((rev.infeasibleReasons || []).map((r) => r.reason).join('；'))}</p>`}
      <p>
        <button class="warn" onclick="peakPublish()">发布/重算修订</button>
        <button class="ghost" onclick="peakCancel()">撤销未执行动作</button>
      </p>` : '<p class="muted">尚未发布削峰修订。</p>'}
    <h3>修订历史</h3>
    <table><thead><tr><th>修订</th><th>时间</th><th>值班员</th><th>状态</th><th>动作数</th><th>摘要</th></tr></thead><tbody>
      ${[...p.revisions].reverse().map((r) => `<tr><td>R${r.rev}</td><td>${fmtTime(r.at)}</td><td>${esc(r.operatorId)}</td>
        <td>${r.feasible ? '<span class="good">可行</span>' : '<span class="danger">不可行</span>'}</td><td>${r.actions}</td><td>${esc(r.summary)}</td></tr>`).join('')}
    </tbody></table>
  </div>`;

  const statusTag = (s) => ({
    PENDING: '<span class="warn">待执行</span>', EFFECTIVE: '<span class="good">已生效</span>',
    SUPERSEDED: '<span class="muted">已被断电取代</span>', RESTORED: '<span class="muted">已恢复</span>',
    INVALIDATED: '<span class="danger">已作废</span>', CANCELLED: '<span class="muted">已撤销</span>',
  }[s] || s);
  const kindTag = (k) => (k === 'CUT' ? '<span class="danger">短时断电</span>' : '<span class="warn">限功率</span>');

  const actHtml = `<div class="panel">
    <h3>削峰动作（每步释放电流/持续/恢复最迟时刻）</h3>
    ${p.actions.length ? `<table><thead><tr><th>动作</th><th>修订</th><th>箱</th><th>类型</th><th>状态</th><th>释放A</th><th>持续(min)</th><th>恢复最迟</th><th>操作</th></tr></thead><tbody>
      ${p.actions.map((a) => `<tr>
        <td>${esc(a.id)}</td><td>R${a.rev}</td><td><b>${esc(a.reeferId)}</b></td><td>${kindTag(a.kind)}${a.kind === 'LIMIT' && a.gearFraction != null ? ` <span class="muted">${Math.round(a.gearFraction * 100)}%</span>` : ''}</td>
        <td>${statusTag(a.status)}${a.due ? ' <span class="danger">到期!</span>' : ''}</td>
        <td>${a.releaseA ?? '—'}${a.incrementalReleaseA != null && a.kind === 'CUT' ? ` <span class="muted">(+${a.incrementalReleaseA})</span>` : ''}</td>
        <td>${a.durationMin ?? '—'}</td><td>${a.restoreBy ? fmtDateTime(a.restoreBy) : '—'}</td>
        <td>${a.status === 'PENDING' ? `<button onclick="peakExecute(['${a.id}'])">执行</button>` : ''}</td>
      </tr>`).join('')}
    </tbody></table>
    <p><button onclick="peakExecuteAll()">执行全部待执行</button></p>`
      : '<p class="muted">尚无削峰动作。</p>'}
  </div>`;

  const riskHtml = `<div class="panel">
    <h3>温度风险与传感器新鲜度</h3>
    <table><thead><tr><th>箱</th><th>板</th><th>风险等级</th><th>风险分</th><th>箱温℃</th><th>设定℃</th><th>偏离℃</th><th>回温℃/h</th><th>保冷余量</th><th>电流口径</th><th>温度</th><th>当前控制</th></tr></thead><tbody>
      ${p.reefers.map((r) => `<tr>
        <td><b>${esc(r.reeferId)}</b></td><td>${esc(r.boardId)}</td><td>${esc(r.tierLabel)}</td><td>${r.riskScore}</td>
        <td>${r.currentC ?? '—'}</td><td>${r.setpointC ?? '—'}</td><td>${r.excursionC ?? '—'}</td><td>${r.trendRateCph}</td>
        <td>${r.spareRatio != null ? Math.round(r.spareRatio * 100) + '%' : '—'}</td>
        <td>${r.currentBasis === 'MEASURED' ? '<span class="good">实测</span>' : '<span class="warn">铭牌</span>'}</td>
        <td>${r.tempFresh ? '<span class="good">新鲜</span>' : '<span class="warn">过期</span>'}</td>
        <td>${r.activeKind === 'CUT' ? '<span class="danger">断电中</span>' : r.activeKind === 'LIMIT' ? '<span class="warn">限功率</span>' : '<span class="muted">正常</span>'}</td>
      </tr>`).join('')}
    </tbody></table>
  </div>`;

  const rec = p.recovery;
  const saveHtml = `<div class="panel">
    <h3>节省量与分批恢复</h3>
    <p>当前节省 <b class="good">${p.savings.currentSavingA}A</b> · 累计节省电量 <b>${p.savings.cumulativeEnergyAmin} A·min</b></p>
    ${rec ? `<p>恢复会话 ${esc(rec.sessionId)}：已恢复 <b>${rec.recoveredCount}/${rec.total}</b>，剩余 ${rec.remaining}
        <span class="${rec.paused ? 'danger' : rec.state === 'COMPLETED' ? 'good' : 'warn'}">
        ${rec.state === 'COMPLETED' ? '· 已完成' : rec.paused ? `· 已暂停（${rec.pauseReason}）` : '· 进行中'}</span></p>` : '<p class="muted">暂无恢复会话。</p>'}
    <p>
      <button onclick="peakRestore(1)">恢复 1 台</button>
      <button onclick="peakRestore(2)">恢复 2 台</button>
      <button onclick="peakRestore(5)">恢复一批(≤5)</button>
    </p>
  </div>`;

  $('#tab-peak').innerHTML = budgetHtml + revHtml + saveHtml + actHtml + riskHtml;
}

window.peakPublish = async () => {
  try {
    const r = await api('/api/peak/plans', {});
    toast(`修订 R${r.rev}：${r.feasible ? '可行' : '不可行'}，${r.actions.length} 个动作`);
    await refresh();
  } catch (e) { toast(e.message, 'err'); }
};
window.peakCancel = async () => {
  try { const r = await api('/api/peak/cancel', {}); toast(`撤销 ${r.cancelled ?? 0} 个未执行动作`); await refresh(); }
  catch (e) { toast(e.message, 'err'); }
};
window.peakExecute = async (actionIds) => {
  try {
    const r = await api('/api/peak/actions/execute', { actionIds });
    toast(`执行 ${r.executed} 个，净载 ${r.shipNetA}A ${r.withinLimit ? '（达标）' : '（仍有余量待执行）'}`);
    await refresh();
  } catch (e) { toast(e.message, 'err'); }
};
window.peakExecuteAll = async () => {
  const ids = state.peak.actions.filter((a) => a.status === 'PENDING').map((a) => a.id);
  if (!ids.length) return toast('没有待执行动作');
  await window.peakExecute(ids);
};
window.peakRestore = async (maxBatchSize) => {
  try {
    const r = await api('/api/peak/restore', { maxBatchSize });
    if (r.paused) toast(`恢复暂停（${r.pauseReason}），已发布新修订 R${r.revisionPublished}`, 'err');
    else toast(`恢复 ${r.restored ?? 0} 台`);
    await refresh();
  } catch (e) { toast(e.message, 'err'); }
};

// ---------------- 装船前预冷合格证与放行互锁 ----------------
function renderPrecool() {
  const p = state.precool;
  if (!p) { $('#tab-precool').innerHTML = '<p class="muted">无预冷数据。</p>'; return; }

  const reasonText = (r) => ({
    POWER_LOSS: '期间掉电', SETPOINT_CHANGED: '设定点调整', SENSOR_BOOT_CHANGED: '传感器更换',
    READING_STALE: '读数过期', DEVIATION_EXCEEDED: '温度偏差超限', RATE_EXCEEDED: '变化速度超限',
    OVER_TEMPERATURE: '后续超温', WAIVER_EXPIRED: '豁免到期',
  }[r] || r);

  // 合格进度
  const obsRows = p.observations.map((o) => {
    const pr = o.progress || {};
    const blockers = (o.blockers || []).map((b) => `<span class="danger">${esc(b.code)}</span>`).join(' ');
    return `<tr>
      <td><b>${esc(o.reeferId)}</b><div class="muted">${esc(o.voyageId)}</div></td>
      <td>${esc(o.tierLabel)}</td>
      <td>${o.status === 'READY' ? '<span class="good">可签发</span>' : `<span class="warn">${esc(o.status)}</span>${blockers ? '<div>' + blockers + '</div>' : ''}`}</td>
      <td style="min-width:160px">
        <div class="meter"><div class="track"><div class="fill ${pr.pct >= 100 ? '' : 'warn'}" style="width:${pr.pct ?? 0}%"></div></div>
        <div class="nums">${pr.pct ?? 0}% · 合格 ${Math.round((pr.qualifiedMs ?? 0) / 60000)} / ${Math.round((pr.stableRequiredMs ?? 0) / 60000)} 分钟</div></div>
        <div class="muted">最新 ${pr.latestValueC ?? '—'}℃ @ ${fmtTime(pr.latestMeasuredAt)} · ${pr.readingFresh ? '<span class="good">新鲜实测</span>' : '<span class="warn">过期/无</span>'}</div>
      </td>
      <td>${pr.restartRequired ? `<span class="danger">需重新观测</span><div class="muted">起点 ${fmtDateTime(pr.restartFrom)}<br>剩余 ${Math.round((pr.remainingMs ?? 0) / 60000)} 分钟</div>` : '<span class="muted">—</span>'}</td>
      <td>
        <button onclick="pcObserve('${esc(o.voyageId)}','${esc(o.reeferId)}')">${o.observationStartedAt ? '重新观测' : '发起观测'}</button>
        <button onclick="pcIssue('${esc(o.voyageId)}','${esc(o.reeferId)}')">签发合格证</button>
      </td>
    </tr>`;
  }).join('');

  // 证书
  const certRows = p.certs.map((c) => `<tr>
    <td>${esc(c.id)}</td>
    <td><b>${esc(c.reeferId)}</b><div class="muted">${esc(c.voyageId)}</div></td>
    <td>${esc(c.tierLabel)}</td>
    <td>${c.live ? '<span class="good">有效</span>' : `<span class="danger">已失效·${esc(reasonText(c.liveReason || c.invalidReason))}</span>`}</td>
    <td>${esc(c.sensorBootId) ?? '—'}<div class="muted">设定 ${c.setpointC}℃</div></td>
    <td>${c.evidence ? `${fmtDateTime(c.evidence.from)}<br>~ ${fmtDateTime(c.evidence.to)}<div class="muted">${c.evidence.sampleCount} 点 · ${c.evidence.minC}~${c.evidence.maxC}℃ · ${c.evidence.maxAbsRateCph}℃/h</div>` : '—'}</td>
    <td>${c.live ? `${fmtDateTime(c.issuedAt)}` : `<div class="muted">${fmtDateTime(c.invalidatedAt)}</div>${c.restartFrom ? `<div class="danger">重新观测起点 ${fmtDateTime(c.restartFrom)}<br>剩余 ${Math.round((c.restartRemainingMs ?? 0) / 60000)} 分钟</div>` : ''}`}</td>
  </tr>`).join('');

  // 豁免（含倒计时）
  const wvRows = p.waivers.map((w) => {
    const live = w.live;
    const mins = Math.round((w.remainingMs ?? 0) / 60000);
    return `<tr>
      <td>${esc(w.id)}</td>
      <td><b>${esc(w.reeferId)}</b><div class="muted">${esc(w.voyageId)}</div></td>
      <td>${live ? `<span class="warn">生效中 · 剩 ${mins} 分钟</span>` : `<span class="danger">${esc(reasonText(w.revokeReason) || w.liveStatus)}</span>`}</td>
      <td>${esc(w.reason)}<div class="muted">货物负责人 ${esc(w.cargoOwner)} · ${esc(w.grantedBy)}</div></td>
      <td>${fmtDateTime(w.validFrom)}<br>~ ${fmtDateTime(w.validTo)}</td>
    </tr>`;
  }).join('');

  // 待处理队列
  const pendRows = p.pending.map((q) => `<tr class="${q.status === 'OPEN' ? '' : 'muted'}">
    <td>${esc(q.id)}</td>
    <td><b>${esc(q.reeferId)}</b><div class="muted">${esc(q.voyageId)}</div></td>
    <td>${q.status === 'OPEN' ? '<span class="danger">待处理</span>' : esc(q.resolution)}</td>
    <td>${(q.reasons || []).map((r) => `<span class="danger">${esc(r)}</span>`).join(' ')}</td>
    <td>${fmtDateTime(q.enteredAt)}<div class="muted">最近扫描 ${fmtDateTime(q.lastScanAt)} · ${esc(q.lastSlotCode || '')}</div></td>
    <td>${q.status === 'OPEN' ? `
      <button class="good" onclick="pcReobserve('${esc(q.voyageId)}','${esc(q.reeferId)}')">恢复后重新观测</button>
      <button class="warn" onclick="pcWaive('${esc(q.voyageId)}','${esc(q.reeferId)}')">临时豁免</button>` : esc(q.resolvedBy || '')}</td>
  </tr>`).join('');

  // 装船放行结果
  const scanRows = p.scans.slice(0, 60).map((g) => `<tr>
    <td>${fmtDateTime(g.at)}</td>
    <td><b>${esc(g.reeferId)}</b><div class="muted">${esc(g.voyageId)} · ${g.stage === 'LOAD_CONFIRM' ? '装船确认' : '岸桥扫描'}</div></td>
    <td>${g.scannerId || ''}</td>
    <td>${g.slotCode || ''}</td>
    <td>${g.effectiveOutcome === 'RELEASED' ? '<span class="good">放行</span>' : g.effectiveOutcome === 'RELEASE_NOW_VOID' ? '<span class="warn">已放行(依据后失效)</span>' : '<span class="danger">待处理</span>'}</td>
    <td class="muted">${g.certId || g.waiverId || (g.reasons || []).join(',')}</td>
  </tr>`).join('');

  $('#tab-precool').innerHTML = `
  <div class="panel">
    <h3>预冷观测合格进度（高风险更严：稳定 30 分钟 / 偏差 0.5℃）</h3>
    <table><thead><tr><th>箱/航次</th><th>风险</th><th>状态</th><th>合格进度/证据</th><th>重新观测</th><th>操作</th></tr></thead>
    <tbody>${obsRows || '<tr><td colspan="6" class="muted">尚未对任何箱发起观测。</td></tr>'}</tbody></table>
  </div>
  <div class="panel">
    <h3>预冷合格证（绑定箱号/航次/传感器启动代号/观测区间）</h3>
    <table><thead><tr><th>编号</th><th>箱/航次</th><th>风险</th><th>状态/失效原因</th><th>传感器/证据区间摘要</th><th>签发/失效与重测提示</th></tr></thead>
    <tbody>${certRows || '<tr><td colspan="6" class="muted">尚无合格证。</td></tr>'}</tbody></table>
  </div>
  <div class="panel">
    <h3>临时豁免（仅覆盖指定航次与箱号，超温/掉电自动撤销）</h3>
    <table><thead><tr><th>编号</th><th>箱/航次</th><th>倒计时/状态</th><th>原因/货物负责人</th><th>有效期</th></tr></thead>
    <tbody>${wvRows || '<tr><td colspan="5" class="muted">尚无豁免。</td></tr>'}</tbody></table>
  </div>
  <div class="panel">
    <h3>温控待处理队列（装船计划与插座事实保持不变）</h3>
    <table><thead><tr><th>条目</th><th>箱/航次</th><th>状态</th><th>原因</th><th>入队/扫描</th><th>处置</th></tr></thead>
    <tbody>${pendRows || '<tr><td colspan="6" class="muted">队列为空。</td></tr>'}</tbody></table>
  </div>
  <div class="panel">
    <h3>装船放行结果（装船确认 / 岸桥扫描）</h3>
    <div class="grid2">
      <label class="fld">航次<input id="pcVoyage" placeholder="VOY-1" value="VOY-1"></label>
      <label class="fld">箱号<input id="pcReefer" placeholder="R1"></label>
      <label class="fld">槽位<input id="pcSlot" placeholder="02-01-82"></label>
      <label class="fld">扫描员<input id="pcScanner" placeholder="CRANE-1"></label>
    </div>
    <p><button class="good" onclick="pcScan(false)">岸桥扫描查询放行</button>
       <button class="good" onclick="pcScan(true)">装船确认查询放行</button></p>
    <table><thead><tr><th>时间</th><th>箱/航次</th><th>扫描员</th><th>槽位</th><th>结果</th><th>依据</th></tr></thead>
    <tbody>${scanRows || '<tr><td colspan="6" class="muted">尚无扫描。</td></tr>'}</tbody></table>
  </div>`;
}

window.pcObserve = async (voyageId, reeferId) => {
  try { const r = await api('/api/precool/observe', { voyageId, reeferId, operatorId: 'DUTY' });
    toast(r.reset ? '已重新发起观测，计时重新开始' : '观测已开始'); await refresh();
  } catch (e) { toast(e.message, 'err'); }
};
window.pcIssue = async (voyageId, reeferId) => {
  try { const r = await api('/api/precool/certificates', { voyageId, reeferId, operatorId: 'DUTY' });
    toast(`合格证 ${r.certId} 已签发`); await refresh();
  } catch (e) { toast(e.message + (e.payload?.error?.restartFrom ? '（需重新观测）' : ''), 'err'); }
};
window.pcReobserve = async (voyageId, reeferId) => {
  try { await api('/api/precool/pending/resolve', { voyageId, reeferId, action: 'reobserve', operatorId: 'DUTY' });
    toast('已在恢复供电后发起重新观测'); await refresh();
  } catch (e) {
    // 无开放待处理条目时直接发起观测
    try { await api('/api/precool/observe', { voyageId, reeferId, operatorId: 'DUTY' }); toast('已发起重新观测'); await refresh(); }
    catch (e2) { toast(e2.message, 'err'); }
  }
};
window.pcWaive = async (voyageId, reeferId) => {
  const reason = prompt('豁免原因（必填）');
  if (reason == null) return;
  const cargoOwner = prompt('货物负责人身份（必填）');
  if (cargoOwner == null) return;
  const mins = Number(prompt('有效分钟数（≤60）', '30'));
  try {
    const r = await api('/api/precool/waivers', { voyageId, reeferId, reason, cargoOwner, validMinutes: mins, operatorId: 'DUTY' });
    toast(`豁免 ${r.waiverId} 已签发，剩 ${Math.round(r.expiresInMs / 60000)} 分钟`); await refresh();
  } catch (e) { toast(e.message, 'err'); }
};
window.pcScan = async (isConfirm) => {
  const voyageId = $('#pcVoyage').value.trim() || 'VOY-1';
  const reeferId = $('#pcReefer').value.trim();
  if (!reeferId) return toast('请输入箱号', 'err');
  const body = { voyageId, reeferId, scannerId: $('#pcScanner').value.trim() || 'CRANE-1', slotCode: $('#pcSlot').value.trim() || null };
  if (isConfirm) body.stage = 'LOAD_CONFIRM';
  try {
    const r = await api('/api/precool/gate-scan', body);
    toast(r.outcome === 'RELEASED' ? `放行（${r.basis === 'CERT' ? '合格证' : '豁免'}）` : '缺失/失效：进入温控待处理队列', r.outcome === 'RELEASED' ? 'ok' : 'err');
    await refresh();
  } catch (e) { toast(e.message, 'err'); }
};

function renderTrips() {
  let html = `<div class="panel"><h3>跳闸影响</h3>`;
  if (!trippedCircuits.length) {
    html += '<p class="muted">当前没有跳闸回路。</p>';
  } else {
    for (const c of trippedCircuits) {
      const affected = [];
      for (const sock of c.sockets) {
        if (sock.status === 'FROZEN' && sock.reeferId) affected.push(sock.reeferId);
      }
      const insRows = affected.map((rid) => {
        const r = state.reefers.find((x) => x.id === rid);
        const i = state.insulation[rid];
        return `<tr><td><b>${esc(rid)}</b></td><td>${r?.tempPriority}</td>
          <td>${i ? `${i.remainingMinutes} 分钟` : '—'}</td>
          <td>${i ? fmtDateTime(i.powerLossAt) : '—'}</td></tr>`;
      }).join('');
      html += `<p>回路 <b>${esc(c.id)}</b> 于 ${fmtDateTime(c.trippedAt)} 跳闸，新接电已冻结。</p>
        <table><thead><tr><th>箱号</th><th>温控优先级</th><th>剩余保温</th><th>掉电时刻</th></tr></thead>
        <tbody>${insRows}</tbody></table>
        <p style="margin-top:10px">
          <button onclick="planMigration('${c.id}')">生成迁移方案</button>
          <button class="ghost" onclick="resetCircuit('${c.id}')">回路复位</button>
        </p>`;
    }
  }
  html += '</div>';

  html += `<div class="panel"><h3>迁移执行状态</h3>`;
  if (!state.plans.length) {
    html += '<p class="muted">尚无迁移方案。</p>';
  } else {
    for (const p of [...state.plans].reverse()) {
      const stateTag = { ACTIVE: '<span class="warn">进行中</span>', COMPLETED: '<span class="good">已完成</span>', CANCELLED: '<span class="muted">已取消</span>' }[p.state];
      html += `<div class="circuit"><div class="circuit-head">
        <span class="name">方案 ${esc(p.id)} · 回路 ${esc(p.circuitId)}</span>${stateTag}
        <span class="muted">创建 ${fmtTime(p.createdAt)}${p.completedAt ? ` · 完成 ${fmtTime(p.completedAt)}` : ''}${p.cancelledAt ? ` · 取消 ${fmtTime(p.cancelledAt)}` : ''}</span>
        ${p.state === 'ACTIVE' ? `<button class="bad" onclick="cancelPlan('${p.id}')">取消方案</button>` : ''}
      </div><div class="steps">`;
      for (const st of p.steps) {
        const stTag = { PENDING: '待执行', UNPLUGGED: '已拔除', OCCUPIED: '已占用', DONE: '已送电', CANCELLED: '已取消' }[st.state];
        const arrow = st.sameSite
          ? `${esc(st.fromSocketId)} 原位复位`
          : `${esc(st.fromSocketId)} → ${esc(st.targetSocketId)}`;
        html += `<div class="step ${st.state.toLowerCase()}">
          <span>#${st.index}</span><b>${esc(st.reeferId)}</b><span>${arrow}</span>
          <span class="muted">${stTag}</span>
          <span style="flex:1"></span>${stepButtons(p, st)}
        </div>`;
      }
      html += '</div></div>';
    }
  }
  html += '</div>';
  $('#tab-trips').innerHTML = html;
}

function stepButtons(p, st) {
  if (p.state !== 'ACTIVE') return '';
  const btns = [];
  if (st.sameSocket) {
    if (st.state !== 'DONE') btns.push(`<button onclick="step('${p.id}',${st.index},'repower')">重新送电</button>`);
  } else {
    if (st.state === 'PENDING') {
      btns.push(`<button class="warn" onclick="step('${p.id}',${st.index},'unplug')">确认拔除</button>`);
      if (!st.occupyCommitted) btns.push(`<button disabled>占用目标</button>`);
    }
    if (st.state === 'UNPLUGGED') {
      btns.push(`<button class="ghost" onclick="step('${p.id}',${st.index},'occupy')">确认占用目标</button>`);
      btns.push(`<button disabled>送电</button>`);
    }
    if (st.state === 'OCCUPIED') btns.push(`<button onclick="step('${p.id}',${st.index},'repower')">重新送电</button>`);
  }
  return btns.join(' ');
}

function renderSamples() {
  const q = state.samples.quarantined;
  let html = `<div class="panel"><h3>待审（倒序/超时钟偏差）样本 ${q.length}</h3>`;
  html += q.length
    ? `<table><thead><tr><th>种类</th><th>传感器启动代号</th><th>对象</th><th>测量时刻</th><th>接收时刻</th><th>数值</th><th>原因</th></tr></thead><tbody>
      ${q.map((s) => `<tr><td>${s.kind}</td><td>${esc(s.sensorBootId)}</td>
        <td>${esc(s.reeferId || s.socketId || s.circuitId)}</td>
        <td>${fmtDateTime(s.measuredAt)}</td><td>${fmtDateTime(s.receivedAt)}</td>
        <td>${s.value}</td><td class="danger">${esc(s.reason)}</td></tr>`).join('')}
      </tbody></table>`
    : '<p class="muted">没有隔离样本。</p>';
  html += '</div>';

  const a = state.samples.accepted;
  html += `<div class="panel"><h3>最新接受样本（${a.length}，最多 200 条）</h3>
    <table><thead><tr><th>种类</th><th>传感器</th><th>对象</th><th>测量时刻</th><th>数值</th></tr></thead><tbody>
    ${a.slice(0, 50).map((s) => `<tr><td>${s.kind}</td><td>${esc(s.sensorBootId)}</td>
      <td>${esc(s.reeferId || s.socketId || s.circuitId)}</td>
      <td>${fmtDateTime(s.measuredAt)}</td><td>${s.value}</td></tr>`).join('')}
    </tbody></table></div>`;
  $('#tab-samples').innerHTML = html;
}

// ---------------- 操作 ----------------
window.planMigration = async (circuitId) => {
  const recompute = false;
  try {
    const dry = await api('/api/migrations', { circuitId, commit: false });
    const msg = dry.steps.map((s) =>
      `#${s.index} ${s.reeferId}: ${s.fromSocketId} → ${s.sameSocket ? '原位复位' : s.targetSocketId}`
    ).join('\n');
    if (!confirm(`迁移步骤（按温控优先级排序）：\n\n${msg || '（无）'}\n\n确认提交并锁定首个目标？`)) return;
    const created = await api('/api/migrations', { circuitId, commit: true });
    toast(`方案 ${created.planId} 已创建`);
    await refresh();
  } catch (e) {
    if (e.payload?.error?.code === 'TARGET_CONTENDED') {
      const c = e.payload.error;
      const alt = c.alternative.steps.map((s) =>
        `#${s.index} ${s.reeferId}: ${s.fromSocketId} → ${s.sameSocket ? '原位复位' : s.targetSocketId}`
      ).join('\n');
      if (confirm(`目标被争用：\n${c.conflicts.map((x) => `${x.targetSocketId} 已被 ${x.heldByReeferId} (${x.leaseState}) 锁定`).join('\n')}\n\n按最新占用重算的备选：\n${alt || '（无可行目标）'}\n\n是否采用备选提交？`)) {
        try {
          const created = await api('/api/migrations', { circuitId, recompute: true });
          toast(`备选方案 ${created.planId} 已创建`);
          await refresh();
        } catch (e2) { toast(e2.message, 'err'); }
      }
    } else {
      toast(e.message, 'err');
    }
  }
};

window.resetCircuit = async (circuitId) => {
  if (!confirm(`确认复位回路 ${circuitId}？该回路 FROZEN 箱将就地恢复供电。`)) return;
  try {
    await api('/api/circuits/reset', { circuitId });
    toast('回路已复位');
    await refresh();
  } catch (e) { toast(e.message, 'err'); }
};

window.cancelPlan = async (planId) => {
  if (!confirm(`确认取消方案 ${planId}？未执行步骤的目标插座将被释放，已完成步骤保留。`)) return;
  try {
    await api('/api/migrations/cancel', { planId });
    toast('方案已取消');
    await refresh();
  } catch (e) { toast(e.message, 'err'); }
};

window.step = async (planId, index, action) => {
  const labels = { unplug: '拔除', occupy: '占用目标', repower: '送电' };
  if (!confirm(`确认步骤 #${index} 执行「${labels[action]}」？`)) return;
  try {
    await api('/api/migrations/steps', { planId, index, action });
    toast(`步骤 #${index} ${labels[action]}完成`);
    await refresh();
  } catch (e) { toast(e.message, 'err'); }
};

// ---------------- 录入/导入页 ----------------
const DEFAULT_TOPO = {
  boards: [{ id: 'B1', name: '主配电板' }],
  circuits: [
    { id: 'C1', boardId: 'B1', name: '回路1', phase: '3P', ratedCurrentA: 63 },
    { id: 'C2', boardId: 'B1', name: '回路2', phase: '3P', ratedCurrentA: 63 },
  ],
  sockets: [
    { id: 'S1', circuitId: 'C1', allowedBoxTypes: ['A1'], reachAreas: ['BAY-A'] },
    { id: 'S2', circuitId: 'C1', allowedBoxTypes: ['A1'], reachAreas: ['BAY-A'] },
    { id: 'S3', circuitId: 'C2', allowedBoxTypes: ['A1'], reachAreas: ['BAY-A'] },
  ],
};
const DEFAULT_REEFERS = {
  reefers: [
    { id: 'R1', boxType: 'A1', phase: '3P', powerKw: 5, area: 'BAY-A', tempPriority: 5, insulationHours: 4 },
    { id: 'R2', boxType: 'A1', phase: '3P', powerKw: 5, area: 'BAY-A', tempPriority: 2, insulationHours: 4 },
  ],
};

function adminHtml() {
  return `
  <div class="grid2">
    <div class="panel">
      <h3>1. 配电拓扑（配电板/回路/插座/相制/额定/箱型/可达区域）</h3>
      <textarea id="topoJson">${esc(JSON.stringify(DEFAULT_TOPO, null, 2))}</textarea>
      <p><button onclick="submitTopo()">录入拓扑</button></p>
    </div>
    <div class="panel">
      <h3>2. 本航次冷藏箱功率需求与温控优先级</h3>
      <textarea id="reeferJson">${esc(JSON.stringify(DEFAULT_REEFERS, null, 2))}</textarea>
      <p><button onclick="submitReefers()">导入冷藏箱</button></p>
    </div>
  </div>
  <div class="panel">
    <h3>3. 自动生成插座预留 & 接电终端</h3>
    <p><button onclick="autoReserve()">为所有未接电箱生成预留</button></p>
    <div class="grid2">
      <label class="fld">箱号<input id="connReefer" placeholder="R1" /></label>
      <label class="fld">插座号<input id="connSocket" placeholder="S1" /></label>
    </div>
    <p><button onclick="connectOne()">预留转供电（提交命令标识由服务端幂等处理）</button></p>
  </div>
  <div class="grid2">
    <div class="panel">
      <h3>4. 传感器采样</h3>
      <div class="grid2">
        <label class="fld">种类
          <select id="sKind"><option value="current">current 电流</option><option value="temperature">temperature 温度</option><option value="powerLoss">powerLoss 掉电</option></select>
        </label>
        <label class="fld">传感器启动代号<input id="sBoot" value="BOOT-1" /></label>
        <label class="fld">对象（电流填插座 / 其余填箱号）<input id="sTarget" placeholder="S1" /></label>
        <label class="fld">数值<input id="sValue" placeholder="17.2" /></label>
      </div>
      <p><button onclick="submitSample()">提交样本</button></p>
    </div>
    <div class="panel">
      <h3>5. 回路操作</h3>
      <label class="fld">回路号<input id="tripCircuit" placeholder="C1" /></label>
      <p>
        <button class="bad" onclick="tripCircuit()">登记跳闸</button>
        <button class="ghost" onclick="resetCircuit2()">复位回路</button>
      </p>
    </div>
  </div>
  <div class="panel">
    <h3>6. 分时功率预算（全船 + 配电板两级安培上限）</h3>
    <div class="grid2">
      <label class="fld">生效时长（分钟，从现在起）<input id="bgMins" placeholder="30" value="30" /></label>
      <label class="fld">全船上限 shipLimitA（可空）<input id="bgShip" placeholder="40" /></label>
      <label class="fld">配电板上限 JSON（可空）<input id="bgBoards" placeholder='{"B1":30}' /></label>
      <label class="fld">值班员<input id="bgOp" value="OPS" /></label>
    </div>
    <p>
      <button class="warn" onclick="registerBudget()">登记预算并去削峰页</button>
      <button onclick="peakPublish()">立即发布削峰修订</button>
    </p>
  </div>
  <div class="panel">
    <h3>7. 预冷合格证（观测/设定点调整）</h3>
    <div class="grid2">
      <label class="fld">航次<input id="pcVoy" value="VOY-1"></label>
      <label class="fld">箱号<input id="pcBox" placeholder="R1"></label>
      <label class="fld">新设定点℃（调整即令旧证失效）<input id="pcSetpoint" placeholder="-22"></label>
    </div>
    <p>
      <button onclick="pcAdminObserve()">发起/重新观测</button>
      <button class="warn" onclick="pcAdminSetpoint()">调整设定点</button>
    </p>
  </div>`;
}

window.pcAdminObserve = async () => {
  const voyageId = $('#pcVoy').value.trim();
  const reeferId = $('#pcBox').value.trim();
  if (!reeferId) return toast('请输入箱号', 'err');
  try { await api('/api/precool/observe', { voyageId, reeferId, operatorId: 'OPS' });
    toast('观测已发起/重置'); switchTab('precool'); await refresh();
  } catch (e) { toast(e.message, 'err'); }
};
window.pcAdminSetpoint = async () => {
  const reeferId = $('#pcBox').value.trim();
  const toC = Number($('#pcSetpoint').value);
  if (!reeferId || !Number.isFinite(toC)) return toast('请输入箱号与新设定点', 'err');
  try { const r = await api('/api/precool/setpoint', { reeferId, toC, operatorId: 'OPS' });
    toast(`设定点已调整为 ${toC}℃，${(r.invalidations || []).length} 项失效`);
    switchTab('precool'); await refresh();
  } catch (e) { toast(e.message, 'err'); }
};

function readJson(id) {
  try { return JSON.parse($('#' + id).value); }
  catch (e) { toast('JSON 解析失败: ' + e.message, 'err'); return null; }
}
window.submitTopo = async () => {
  const body = readJson('topoJson'); if (!body) return;
  try { await api('/api/topology', body); toast('拓扑已录入'); await refresh(); } catch (e) { toast(e.message, 'err'); }
};
window.submitReefers = async () => {
  const body = readJson('reeferJson'); if (!body) return;
  try { await api('/api/reefers', body); toast('冷藏箱已导入'); await refresh(); } catch (e) { toast(e.message, 'err'); }
};
window.autoReserve = async () => {
  try { const r = await api('/api/reservations', {});
    toast(`预留 ${r.reserved.length} 个，拒绝 ${r.rejected.length} 个`); await refresh();
  } catch (e) { toast(e.message, 'err'); }
};
window.connectOne = async () => {
  const reeferId = $('#connReefer').value.trim();
  const socketId = $('#connSocket').value.trim();
  try { const r = await api('/api/connect', { reeferId, socketId }); toast(`${r.reeferId} 已在 ${r.socketId} 送电`); await refresh(); }
  catch (e) { toast(e.message, 'err'); }
};
window.submitSample = async () => {
  const kind = $('#sKind').value;
  const boot = $('#sBoot').value.trim();
  const target = $('#sTarget').value.trim();
  const value = Number($('#sValue').value);
  const body = { kind, sensorBootId: boot, measuredAt: Date.now(), value };
  if (kind === 'current') body.socketId = target; else body.reeferId = target;
  try { const r = await api('/api/samples', body);
    toast(r.accepted ? '样本已接受' : `样本隔离待审: ${r.reason}`, r.accepted ? 'ok' : 'err');
    await refresh();
  } catch (e) { toast(e.message, 'err'); }
};
window.tripCircuit = async () => {
  const circuitId = $('#tripCircuit').value.trim();
  try { const r = await api('/api/circuits/trip', { circuitId });
    if (r.alreadyTripped) toast('回路此前已跳闸');
    else toast(`已跳闸，受影响 ${r.impact.affected.length} 箱`, 'err');
    switchTab('trips'); await refresh();
  } catch (e) { toast(e.message, 'err'); }
};
window.resetCircuit2 = async () => {
  const circuitId = $('#tripCircuit').value.trim();
  await window.resetCircuit(circuitId);
};

window.registerBudget = async () => {
  const mins = Number($('#bgMins').value) || 30;
  const startAt = Date.now() - 1000;
  const endAt = Date.now() + mins * 60000;
  const body = { operatorId: $('#bgOp').value.trim() || 'OPS', startAt, endAt };
  const ship = $('#bgShip').value.trim();
  if (ship) body.shipLimitA = Number(ship);
  const boards = $('#bgBoards').value.trim();
  if (boards) { try { body.boardLimitsA = JSON.parse(boards); } catch (e) { return toast('配电板上限 JSON 解析失败', 'err'); } }
  try {
    const r = await api('/api/budgets', body);
    toast(`预算 ${r.budgetId} 已登记（${fmtDateTime(startAt)} ~ ${fmtDateTime(endAt)}）`);
    switchTab('peak');
    await refresh();
  } catch (e) { toast(e.message, 'err'); }
};

// ---------------- tabs / refresh ----------------
function switchTab(name) {
  $$('.tabs button').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
  $$('.tab').forEach((t) => t.classList.toggle('active', t.id === `tab-${name}`));
}
$$('.tabs button').forEach((b) => b.addEventListener('click', () => switchTab(b.dataset.tab)));

async function refresh() {
  const res = await fetch('/api/state');
  state = await res.json();
  render();
}
$('#refresh').addEventListener('click', refresh);
setInterval(() => { if ($('#auto').checked) refresh(); }, 2000);

$('#tab-admin').innerHTML = adminHtml();
refresh().catch((e) => toast(e.message, 'err'));
