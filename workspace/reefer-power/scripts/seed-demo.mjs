#!/usr/bin/env node
'use strict';

// 一键播种演示数据（通过 HTTP 接口调用，与真实录入路径一致）
const BASE = process.env.BASE || 'http://localhost:8080';
const cid = () => 'seed-' + Math.random().toString(36).slice(2, 10);

async function call(path, body) {
  const res = await fetch(BASE + path, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'x-command-id': cid() },
    body: JSON.stringify(body),
  });
  const json = await res.json();
  if (!res.ok || json.ok === false) {
    throw new Error(`${path} -> ${res.status} ${JSON.stringify(json.error ?? json)}`);
  }
  return json;
}

const topology = {
  boards: [{ id: 'B1', name: '主配电板 MSB' }],
  circuits: [
    { id: 'C1', boardId: 'B1', name: '冷藏馈线1', phase: '3P', ratedCurrentA: 63 },
    { id: 'C2', boardId: 'B1', name: '冷藏馈线2', phase: '3P', ratedCurrentA: 63 },
    { id: 'C3', boardId: 'B1', name: '备用馈线', phase: '3P', ratedCurrentA: 32 },
  ],
  sockets: [
    { id: 'S1', circuitId: 'C1', phase: '3P', ratedCurrentA: 32, allowedBoxTypes: ['RF40'], reachAreas: ['BAY-01', 'BAY-02'] },
    { id: 'S2', circuitId: 'C1', phase: '3P', ratedCurrentA: 32, allowedBoxTypes: ['RF40'], reachAreas: ['BAY-01', 'BAY-02'] },
    { id: 'S3', circuitId: 'C2', phase: '3P', ratedCurrentA: 32, allowedBoxTypes: ['RF40'], reachAreas: ['BAY-03', 'BAY-04'] },
    { id: 'S4', circuitId: 'C2', phase: '3P', ratedCurrentA: 32, allowedBoxTypes: ['RF40'], reachAreas: ['BAY-03', 'BAY-04'] },
    { id: 'S5', circuitId: 'C3', phase: '3P', ratedCurrentA: 32, allowedBoxTypes: ['RF40'], reachAreas: ['BAY-01', 'BAY-02', 'BAY-03', 'BAY-04'] },
    { id: 'S6', circuitId: 'C3', phase: '3P', ratedCurrentA: 32, allowedBoxTypes: ['RF40'], reachAreas: ['BAY-01', 'BAY-02', 'BAY-03', 'BAY-04'] },
  ],
};

const reefers = {
  reefers: [
    { id: 'MSKU-1001', boxType: 'RF40', phase: '3P', powerKw: 6.5, area: 'BAY-01', tempPriority: 9, insulationHours: 2, setpointC: -25 },
    { id: 'MSKU-1002', boxType: 'RF40', phase: '3P', powerKw: 5.0, area: 'BAY-02', tempPriority: 5, insulationHours: 4, setpointC: -18 },
    { id: 'MSKU-1003', boxType: 'RF40', phase: '3P', powerKw: 5.0, area: 'BAY-03', tempPriority: 2, insulationHours: 8, setpointC: 2 },
    { id: 'MSKU-1004', boxType: 'RF40', phase: '3P', powerKw: 6.5, area: 'BAY-04', tempPriority: 8, insulationHours: 3, setpointC: -25 },
  ],
};

const r = await call('/api/topology', topology);
console.log('拓扑:', r.imported);
const rr = await call('/api/reefers', reefers);
console.log('冷藏箱:', rr.imported, rr.reefers.join(', '));
const resv = await call('/api/reservations', {});
console.log('预留:', resv.reserved.map((x) => `${x.reeferId}->${x.socketId}`).join('  ') || '（无）');
for (const x of resv.reserved) {
  await call('/api/connect', { reeferId: x.reeferId, socketId: x.socketId });
}
console.log('已全部送电。可登记 POST /api/circuits/trip {"circuitId":"C1"} 演练跳闸迁移。');
