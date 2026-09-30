'use strict';

import {
  activeLeases,
  activeLeaseOfReefer,
  activeLeaseOnSocket,
} from './domain.js';
import { mismatchReasons, committedDemandA } from './views.js';
import { round2, bad, conflict, notFound } from './util.js';

// 硬性匹配（不含占用与容量）
function hardReasons(s, reefer, socket, at) {
  return mismatchReasons(s, reefer, socket, at).filter(
    (r) => r !== 'CIRCUIT_TRIPPED'
  );
}

// ---------- 插座选择：已有负载回路优先（装箱并保留空闲备用回路），并列按插座编号 ----------
function bestSocket(candidates, loadOf) {
  candidates.sort((a, b) => {
    const d = loadOf(b.circuitId) - loadOf(a.circuitId);
    return d || a.socketId.localeCompare(b.socketId);
  });
  return candidates[0];
}

export function planReservations(s, at = Date.now()) {
  const decisions = [];
  const facts = [];

  const projected = new Map();
  for (const c of s.circuits.values()) projected.set(c.id, committedDemandA(s, c.id, {}, at));

  const pending = [...s.reefers.values()]
    .filter((r) => !activeLeaseOfReefer(s, r.id))
    .sort((a, b) => b.tempPriority - a.tempPriority || a.id.localeCompare(b.id));

  // 同一批次内已选插座（事实尚未归并，activeLeaseOnSocket 看不到）
  const localTaken = new Set();

  for (const reefer of pending) {
    const candidates = [];
    const reasonSet = new Set();
    for (const socket of s.sockets.values()) {
      if (activeLeaseOnSocket(s, socket.id) || localTaken.has(socket.id)) {
        reasonSet.add('SOCKET_OCCUPIED');
        continue;
      }
      if (s.circuits.get(socket.circuitId).state !== 'NORMAL') {
        reasonSet.add('CIRCUIT_TRIPPED');
        continue;
      }
      const hard = hardReasons(s, reefer, socket, at);
      if (hard.length) {
        hard.forEach((r) => reasonSet.add(r));
        continue;
      }
      const after = round2((projected.get(socket.circuitId) ?? 0) + reefer.ratedCurrentA);
      if (after > s.circuits.get(socket.circuitId).ratedCurrentA) {
        reasonSet.add('CIRCUIT_CAPACITY');
        continue;
      }
      const rated = s.circuits.get(socket.circuitId).ratedCurrentA;
      candidates.push({
        socketId: socket.id,
        circuitId: socket.circuitId,
        circuitRated: rated,
        headroom: round2(rated - after),
      });
    }
    if (!candidates.length) {
      decisions.push({ reeferId: reefer.id, accepted: false, reasons: [...reasonSet].sort() });
      continue;
    }
    const pick = bestSocket(candidates, (cid) => projected.get(cid) ?? 0);
    localTaken.add(pick.socketId);
    projected.set(
      s.sockets.get(pick.socketId).circuitId,
      round2((projected.get(s.sockets.get(pick.socketId).circuitId) ?? 0) + reefer.ratedCurrentA)
    );
    facts.push({ type: 'reserved', reeferId: reefer.id, socketId: pick.socketId, at });
    decisions.push({
      reeferId: reefer.id,
      accepted: true,
      socketId: pick.socketId,
      circuitId: s.sockets.get(pick.socketId).circuitId,
    });
  }
  return { facts, decisions };
}

// ---------- 迁移规划 ----------
function affectedReefers(s, circuitId) {
  const out = [];
  for (const l of activeLeases(s)) {
    if (l.state !== 'FROZEN' && l.state !== 'UNPLUGGED') continue;
    const sk = s.sockets.get(l.socketId);
    if (sk?.circuitId === circuitId) {
      out.push({ reefer: s.reefers.get(l.reeferId), lease: l, socket: sk });
    }
  }
  return out;
}

export function trippedImpact(s, circuitId, at = Date.now()) {
  const c = s.circuits.get(circuitId);
  if (!c) throw notFound(`回路不存在: ${circuitId}`);
  const totalMs = (r) => r.insulationHours * 3600 * 1000;
  const affected = affectedReefers(s, circuitId)
    .map(({ reefer, lease, socket }) => {
      const lossAt = s.powerLossAt.get(reefer.id) ?? c.trippedAt ?? at;
      return {
        reeferId: reefer.id,
        tempPriority: reefer.tempPriority,
        fromSocketId: socket.id,
        powerLossAt: lossAt,
        remainingMinutes: round2((totalMs(reefer) - (at - lossAt)) / 60000),
      };
    })
    .sort((a, b) => b.tempPriority - a.tempPriority || a.remainingMinutes - b.remainingMinutes || a.reeferId.localeCompare(b.reeferId));
  return { circuitId, state: c.state, trippedAt: c.trippedAt, affected };
}

// ideal=true 时忽略其他活动计划的 CLAIM（用于呈现理想方案）；
// ideal=false 时把 CLAIM 视为占用（用于在当前最新占用下重新计算）。
function allocate(s, circuitId, { ideal, at }) {
  const projected = new Map();
  for (const c of s.circuits.values()) {
    if (c.state === 'NORMAL') projected.set(c.id, committedDemandA(s, c.id, {}, at));
  }
  const inActivePlan = new Set();
  for (const p of s.plans.values()) {
    if (p.state !== 'ACTIVE') continue;
    p.reeferIds.forEach((r) => inActivePlan.add(r));
  }

  const impact = trippedImpact(s, circuitId, at);
  const steps = [];
  const unassigned = [];
  let index = 0;
  const localTaken = new Set();

  for (const aff of impact.affected) {
    if (inActivePlan.has(aff.reeferId)) {
      unassigned.push({ reeferId: aff.reeferId, reasons: ['ALREADY_IN_MIGRATION'] });
      continue;
    }
    const reefer = s.reefers.get(aff.reeferId);
    const fromSocket = s.sockets.get(aff.fromSocketId);

    // 回路若已恢复正常，首选原插座（同插座重新送电，无需拔除/占位）
    const trippedCircuit = s.circuits.get(circuitId);
    if (trippedCircuit.state === 'NORMAL' && fromSocket.circuitId === circuitId) {
      const after = round2((projected.get(circuitId) ?? 0) + reefer.ratedCurrentA);
      if (after <= trippedCircuit.ratedCurrentA) {
        projected.set(circuitId, after);
        steps.push(step(index++, reefer, fromSocket, fromSocket, true));
        continue;
      }
    }

    const candidates = [];
    const reasonSet = new Set();
    for (const target of s.sockets.values()) {
      if (localTaken.has(target.id)) {
        reasonSet.add('SOCKET_OCCUPIED');
        continue;
      }
      const targetCircuit = s.circuits.get(target.circuitId);
      if (targetCircuit.state !== 'NORMAL') continue;
      const lease = activeLeaseOnSocket(s, target.id);
      if (lease) {
        // 理想模式下其他方案的 CLAIM 留待提交时争用裁决；其余占用（POWER/FROZEN/原回路插座）一律排除
        const ignore = ideal && lease.state === 'CLAIM';
        if (!ignore) {
          reasonSet.add(lease.state === 'CLAIM' ? 'TARGET_CLAIMED' : 'SOCKET_OCCUPIED');
          continue;
        }
      }
      const hard = hardReasons(s, reefer, target, at);
      if (hard.length) {
        hard.forEach((r) => reasonSet.add(r));
        continue;
      }
      const after = round2((projected.get(target.circuitId) ?? 0) + reefer.ratedCurrentA);
      if (after > targetCircuit.ratedCurrentA) {
        reasonSet.add('CIRCUIT_CAPACITY');
        continue;
      }
      candidates.push({
        socket: target,
        circuitRated: targetCircuit.ratedCurrentA,
        headroom: round2(targetCircuit.ratedCurrentA - after),
      });
    }
    if (!candidates.length) {
      unassigned.push({ reeferId: reefer.id, reasons: [...reasonSet].sort() });
      continue;
    }
    const pick = bestSocket(
      candidates.map((c) => ({
        socketId: c.socket.id,
        circuitId: c.socket.circuitId,
        circuitRated: c.circuitRated,
        headroom: c.headroom,
      })),
      (cid) => projected.get(cid) ?? 0
    );
    const target = s.sockets.get(pick.socketId);
    localTaken.add(target.id);
    projected.set(
      target.circuitId,
      round2((projected.get(target.circuitId) ?? 0) + reefer.ratedCurrentA)
    );
    steps.push(step(index++, reefer, fromSocket, target, false));
  }
  return { circuitId, steps, unassigned, affected: impact.affected };
}

function step(index, reefer, fromSocket, targetSocket, sameSocket) {
  return {
    index,
    reeferId: reefer.id,
    fromSocketId: fromSocket.id,
    fromCircuitId: fromSocket.circuitId,
    targetSocketId: targetSocket.id,
    targetCircuitId: targetSocket.circuitId,
    sameSocket,
    occupyCommitted: false,
  };
}

export function planMigration(s, circuitId, opts = {}) {
  const at = opts.at ?? Date.now();
  const c = s.circuits.get(circuitId);
  if (!c) throw notFound(`回路不存在: ${circuitId}`);
  if (!trippedImpact(s, circuitId, at).affected.length && c.state !== 'TRIPPED') {
    throw bad(`回路 ${circuitId} 上没有等待迁移的掉电箱`);
  }

  const proposal = allocate(s, circuitId, { ideal: true, at });
  if (!proposal.steps.length && c.state === 'TRIPPED') {
    throw conflict(`回路 ${circuitId} 没有可迁移目标`, {
      code: 'NO_FEASIBLE_TARGET',
      unassigned: proposal.unassigned,
    });
  }
  return proposal;
}

// 在最新占用下重新计算（其他活动方案的 CLAIM 视为占用）
export function allocateFresh(s, circuitId, at = Date.now()) {
  return allocate(s, circuitId, { ideal: false, at });
}

// 提交前争用检查：理想目标若已被其他活动计划 CLAIM/占用，则冲突，
// 同时返回在最新占用下重新计算的备选。
export function contentionOnCommit(s, proposal, at = Date.now()) {
  const conflicts = [];
  for (const st of proposal.steps) {
    if (st.sameSocket) continue;
    const lease = activeLeaseOnSocket(s, st.targetSocketId);
    if (lease) {
      conflicts.push({
        reeferId: st.reeferId,
        targetSocketId: st.targetSocketId,
        heldByReeferId: lease.reeferId,
        leaseState: lease.state,
      });
    }
  }
  if (!conflicts.length) return null;
  const alternative = allocate(s, proposal.circuitId, { ideal: false, at });
  return {
    code: 'TARGET_CONTENDED',
    conflicts,
    alternative: { steps: alternative.steps, unassigned: alternative.unassigned },
  };
}
