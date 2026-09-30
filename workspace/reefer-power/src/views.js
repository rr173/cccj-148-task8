'use strict';

import {
  activeLeases,
  activeLeaseOnSocket,
  activeLeasesOfReefer,
  poweredLease,
} from './domain.js';
import { round2 } from './util.js';
import { CURRENT_FRESHNESS_MS } from './util.js';

export function socketPhase(s, socket) {
  return socket.phase ?? s.circuits.get(socket.circuitId)?.phase;
}

export function mismatchReasons(s, reefer, socket) {
  const reasons = [];
  const circuit = s.circuits.get(socket.circuitId);
  const sPhase = socket.phase ?? circuit?.phase;
  if (sPhase !== reefer.phase) reasons.push('PHASE_MISMATCH');
  if (round2(socket.ratedCurrentA) < round2(reefer.ratedCurrentA)) reasons.push('SOCKET_RATING_EXCEEDED');
  if (!socket.allowedBoxTypes.includes(reefer.boxType)) reasons.push('BOX_TYPE_NOT_ALLOWED');
  if (!socket.reachAreas.includes(reefer.area)) reasons.push('AREA_UNREACHABLE');
  if (circuit?.state === 'TRIPPED') reasons.push('CIRCUIT_TRIPPED');
  return reasons;
}

// 该插座当前实际取用电流：优先用新鲜窗口内的电流采样，否则回退到箱铭牌电流
export function effectiveCurrentA(s, socketId, at = Date.now()) {
  const lease = activeLeaseOnSocket(s, socketId);
  if (!lease || lease.state === 'RESERVE' || lease.state === 'CLAIM') return 0;
  if (lease.state === 'FROZEN' || lease.state === 'UNPLUGGED') return 0;
  const m = s.latestCurrent.get(socketId);
  if (m && at - m.measuredAt <= CURRENT_FRESHNESS_MS && m.value >= 0) return round2(m.value);
  const r = s.reefers.get(lease.reeferId);
  return r ? r.ratedCurrentA : 0;
}

// 容量校核口径：该回路上已承诺的需求电流（POWER 按实测/铭牌，RESERVE 按铭牌）
export function committedDemandA(s, circuitId, opts = {}, at = Date.now()) {
  let total = 0;
  for (const l of activeLeases(s)) {
    if (l.id === opts.excludeLeaseId) continue;
    const sk = s.sockets.get(l.socketId);
    if (!sk || sk.circuitId !== circuitId) continue;
    if (l.state === 'POWER') total += effectiveCurrentA(s, l.socketId, at);
    else if (l.state === 'RESERVE') {
      const r = s.reefers.get(l.reeferId);
      if (r) total += r.ratedCurrentA;
    }
    // FROZEN/UNPLUGGED/CLAIM 不计当前负载
  }
  return round2(total);
}

function socketStatus(s, socket, at) {
  const lease = activeLeaseOnSocket(s, socket.id);
  let status = 'AVAILABLE';
  let reeferId = null;
  let leaseState = null;
  if (lease) {
    reeferId = lease.reeferId;
    leaseState = lease.state;
    status = {
      POWER: 'POWERED',
      RESERVE: 'RESERVED',
      FROZEN: 'FROZEN',
      UNPLUGGED: 'FROZEN',
      CLAIM: 'CLAIMED',
    }[lease.state] || 'AVAILABLE';
  }
  const measured = s.latestCurrent.get(socket.id) || null;
  const circuit = s.circuits.get(socket.circuitId);
  return {
    id: socket.id,
    name: socket.name,
    circuitId: socket.circuitId,
    phase: socket.phase ?? circuit.phase,
    ratedCurrentA: socket.ratedCurrentA,
    allowedBoxTypes: socket.allowedBoxTypes,
    reachAreas: socket.reachAreas,
    status,
    reeferId,
    leaseState,
    measuredCurrentA: measured && at - measured.measuredAt <= CURRENT_FRESHNESS_MS ? measured.value : null,
    measuredAt: measured?.measuredAt ?? null,
    effectiveCurrentA: effectiveCurrentA(s, socket.id, at),
  };
}

export function buildView(s, at = Date.now()) {
  const circuits = [];
  const socketsByCircuit = new Map();
  for (const sk of s.sockets.values()) {
    if (!socketsByCircuit.has(sk.circuitId)) socketsByCircuit.set(sk.circuitId, []);
    socketsByCircuit.get(sk.circuitId).push(socketStatus(s, sk, at));
  }

  for (const c of s.circuits.values()) {
    const socks = (socketsByCircuit.get(c.id) || []).sort((a, b) => a.id.localeCompare(b.id));
    const loadA = round2(socks.reduce((sum, x) => sum + (x.leaseState === 'POWER' ? x.effectiveCurrentA : 0), 0));
    circuits.push({
      id: c.id,
      boardId: c.boardId,
      name: c.name,
      phase: c.phase,
      ratedCurrentA: c.ratedCurrentA,
      state: c.state,
      trippedAt: c.trippedAt,
      loadA,
      headroomA: round2(c.ratedCurrentA - loadA),
      overloaded: loadA > c.ratedCurrentA,
      sockets: socks,
    });
  }
  circuits.sort((a, b) => a.id.localeCompare(b.id));

  const boards = [...s.boards.values()]
    .map((b) => ({
      ...b,
      circuits: circuits.filter((c) => c.boardId === b.id).map((c) => c.id),
    }))
    .sort((a, b) => a.id.localeCompare(b.id));

  // 箱视图
  const reefers = [...s.reefers.values()].map((r) => {
    const leases = activeLeasesOfReefer(s, r.id);
    const powered = poweredLease(s, r.id);
    let status = 'UNPOWERED';
    let socketId = null;
    if (powered) {
      status = 'POWERED';
      socketId = powered.socketId;
    } else if (leases.some((l) => l.state === 'RESERVE')) {
      status = 'RESERVED';
      socketId = leases.find((l) => l.state === 'RESERVE').socketId;
    } else if (leases.length) {
      status = 'POWER_LOSS';
      socketId = leases.find((l) => l.state === 'FROZEN' || l.state === 'UNPLUGGED')?.socketId ?? null;
    }
    return {
      id: r.id,
      boxType: r.boxType,
      phase: r.phase,
      ratedCurrentA: r.ratedCurrentA,
      powerKw: r.powerKw,
      area: r.area,
      tempPriority: r.tempPriority,
      insulationHours: r.insulationHours,
      setpointC: r.setpointC,
      status,
      socketId,
    };
  }).sort((a, b) => a.id.localeCompare(b.id));

  // 保温剩余：对所有非 POWERED 的箱，若已有掉电时刻
  const insulation = {};
  for (const r of reefers) {
    if (r.status === 'POWERED') continue;
    const lossAt = s.powerLossAt.get(r.id);
    if (lossAt == null) continue;
    const totalMs = r.insulationHours * 3600 * 1000;
    insulation[r.id] = {
      powerLossAt: lossAt,
      remainingMinutes: round2((totalMs - (at - lossAt)) / 60000),
      expired: at - lossAt >= totalMs,
    };
  }

  const plans = [...s.plans.values()]
    .map((p) => ({
      id: p.id,
      circuitId: p.circuitId,
      state: p.state,
      createdAt: p.createdAt,
      completedAt: p.completedAt,
      cancelledAt: p.cancelledAt,
      reeferIds: p.reeferIds,
      steps: p.steps.map((st) => ({
        index: st.index,
        reeferId: st.reeferId,
        fromSocketId: st.fromSocketId,
        targetSocketId: st.targetSocketId,
        targetCircuitId: st.targetCircuitId,
        sameSocket: !!st.sameSocket,
        occupyCommitted: !!st.occupyCommitted,
        state: st.state,
        claimLeaseId: st.claimLeaseId,
        newLeaseId: st.newLeaseId,
      })),
    }))
    .sort((a, b) => a.createdAt - b.createdAt || a.id.localeCompare(b.id));

  const quarantinedSamples = [...s.samples.values()]
    .filter((x) => !x.accepted)
    .sort((a, b) => b.receivedAt - a.receivedAt);
  const acceptedSamples = [...s.samples.values()]
    .filter((x) => x.accepted && x.kind !== '__cursor__')
    .sort((a, b) => b.measuredAt - a.measuredAt)
    .slice(0, 200);

  return {
    at,
    boards,
    circuits,
    reefers,
    insulation,
    plans,
    samples: { accepted: acceptedSamples, quarantined: quarantinedSamples },
  };
}
