'use strict';

import fs from 'node:fs';
import path from 'node:path';
import { initialState, applyFact, assertInvariants } from './domain.js';
import { setClockOffset, clockOffset, advanceClock } from './clock.js';

// 供电事实、插座租约和迁移动作采用不可变流水（events.log JSONL）加快照（snapshot.json）。
// 每个命令是一个原子批次：要么整批落入流水，要么完全没有。重启后从快照重放剩余批次归并唯一结果。

const SNAPSHOT_EVERY = Number(process.env.REEFER_SNAPSHOT_EVERY ?? 20);

export class Store {
  constructor(dir) {
    this.dir = dir;
    this.logPath = path.join(dir, 'events.log');
    this.snapPath = path.join(dir, 'snapshot.json');
    this.tmpSnapPath = path.join(dir, 'snapshot.json.tmp');
    fs.mkdirSync(dir, { recursive: true });
    // 清理上次崩溃可能留下的临时快照
    if (fs.existsSync(this.tmpSnapPath)) fs.rmSync(this.tmpSnapPath, { force: true });

    this.receipts = new Map(); // hash -> {commandId, reply, seq, status}
    this.commandIndex = new Map(); // commandId -> hash
    this.state = initialState();
    this.batchSeq = 0;
    setClockOffset(0);
    this._load();

    // 故障注入：下次提交包含指定事实类型的批次时，落盘 fsync 后、内存归并前退出
    this.faultArm = null;
  }

  _load() {
    let state = initialState();
    let fromSeq = 0;
    if (fs.existsSync(this.snapPath)) {
      const snap = JSON.parse(fs.readFileSync(this.snapPath, 'utf8'));
      state = revive(snap.state);
      fromSeq = snap.batchSeq;
      for (const b of snap.receipts) {
        this.receipts.set(b.hash, b);
        this.commandIndex.set(b.commandId, b.hash);
      }
      this.batchSeq = fromSeq;
    }

    const lines = this._readDurableLines();
    for (const { line, offset } of lines) {
      let batch;
      try {
        batch = JSON.parse(line);
      } catch {
        // 末行撕裂：截掉未完成写入，该批次视为从未落盘
        fs.truncateSync(this.logPath, offset);
        break;
      }
      // 快照已覆盖的旧批次只校验连续性，不重复归并；但时钟偏移需始终随流水取最新值
      if (batch.seq <= fromSeq) {
        const cf = (batch.facts || []).find((f) => f.type === 'clockAdvanced');
        if (cf && typeof cf.offsetMs === 'number') setClockOffset(cf.offsetMs);
        continue;
      }
      if (batch.seq !== this.batchSeq + 1) {
        throw new Error(`流水序号不连续: 期望 ${this.batchSeq + 1}，实际 ${batch.seq}`);
      }
      this._foldBatch(state, batch);
      this.batchSeq = batch.seq;
    }
    this.state = state;
    assertInvariants(this.state, 'load');
  }

  _readDurableLines() {
    if (!fs.existsSync(this.logPath)) return [];
    const buf = fs.readFileSync(this.logPath);
    const out = [];
    let start = 0;
    for (let i = 0; i < buf.length; i++) {
      if (buf[i] === 10) {
        if (i > start) out.push({ line: buf.toString('utf8', start, i), offset: start });
        start = i + 1;
      }
    }
    if (start < buf.length) out.push({ line: buf.toString('utf8', start), offset: start });
    return out;
  }

  _foldBatch(state, batch) {
    for (const fact of batch.facts) {
      applyFact(state, fact);
      // 测试虚拟时钟偏移随流水回放，保证崩溃重启后“当前时刻”唯一
      if (fact.type === 'clockAdvanced' && typeof fact.offsetMs === 'number') {
        setClockOffset(fact.offsetMs);
      }
    }
    this.receipts.set(batch.hash, {
      hash: batch.hash,
      commandId: batch.commandId,
      seq: batch.seq,
      reply: batch.reply,
      status: batch.status,
    });
    if (batch.commandId) this.commandIndex.set(batch.commandId, batch.hash);
  }

  // keyMode: 'cmd' 按 (命令标识,摘要) 去重；'content' 按纯内容摘要去重（样本）
  lookup(commandId, hash, keyMode = 'cmd') {
    if (commandId) {
      const existing = this.commandIndex.get(commandId);
      if (existing !== undefined) {
        if (existing !== hash) return { kind: 'payload-conflict' };
        return { kind: 'replay', receipt: this.receipts.get(existing) };
      }
    }
    if (keyMode === 'content' || !commandId) {
      const byHash = this.receipts.get(hash);
      if (byHash) return { kind: 'replay', receipt: byHash };
    }
    return { kind: 'new' };
  }

  // 串行化由 server 保证。返回 {reused:false, reply}
  commit({ commandId, hash, facts, reply, enrich, keyMode = 'cmd', status = 200 }) {
    // enrich 在“假设已归并”的状态上预演，得到最终回执，再整体落盘
    if (enrich) {
      const scratch = cloneState(this.state);
      for (const fact of facts) applyFact(scratch, fact);
      reply = enrich(scratch);
    }
    const batch = {
      v: 1,
      seq: this.batchSeq + 1,
      ts: Date.now(),
      commandId: commandId ?? null,
      hash,
      facts,
      reply,
      status,
    };

    // 1) 持久化边界：整批追加并 fsync
    const fd = fs.openSync(this.logPath, 'a');
    try {
      fs.writeSync(fd, JSON.stringify(batch) + '\n');
      fs.fsyncSync(fd);
    } finally {
      fs.closeSync(fd);
    }

    // 故障注入点：落盘完成但尚未归并 -> 重启必须重放出唯一结果
    if (this.faultArm && facts.some((f) => f.type === this.faultArm.factType)) {
      const arm = this.faultArm;
      this.faultArm = null;
      console.error(`[fault] batch ${batch.seq} durable, injected exit before fold [${arm.factType}]`);
      process.exit(17);
    }

    // 2) 内存归并
    for (const fact of facts) applyFact(this.state, fact);
    this.receipts.set(hash, { hash, commandId: commandId ?? null, seq: batch.seq, reply, status });
    if (commandId) this.commandIndex.set(commandId, hash);
    this.batchSeq = batch.seq;
    assertInvariants(this.state, `batch ${batch.seq}`);

    // 3) 快照（临时文件 + 原子改名，失败不影响流水）
    if (this.batchSeq % SNAPSHOT_EVERY === 0) {
      this._writeSnapshot();
    }
    return { reused: false, reply, status };
  }

  _writeSnapshot() {
    const receipts = [...this.receipts.values()];
    const snap = { batchSeq: this.batchSeq, at: Date.now(), state: this.state, receipts };
    fs.writeFileSync(this.tmpSnapPath, JSON.stringify(snap, replacer));
    const fd = fs.openSync(this.tmpSnapPath, 'r');
    try {
      fs.fsyncSync(fd);
    } finally {
      fs.closeSync(fd);
    }
    fs.renameSync(this.tmpSnapPath, this.snapPath);
  }
}

// ---------- Map/Set 快照序列化 ----------
export function cloneState(state) {
  return JSON.parse(JSON.stringify(state, replacer), (_k, v) => {
    if (v && typeof v === 'object' && v.__type === 'Map') return new Map(v.entries);
    if (v && typeof v === 'object' && v.__type === 'Set') return new Set(v.values);
    return v;
  });
}

function replacer(_key, value) {
  if (value instanceof Map) return { __type: 'Map', entries: [...value.entries()] };
  if (value instanceof Set) return { __type: 'Set', values: [...value.values()] };
  return value;
}
function revive(value) {
  return JSON.parse(JSON.stringify(value), (_k, v) => {
    if (v && typeof v === 'object' && v.__type === 'Map') return new Map(v.entries);
    if (v && typeof v === 'object' && v.__type === 'Set') return new Set(v.values);
    return v;
  });
}
