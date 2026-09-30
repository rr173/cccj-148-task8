'use strict';

// ---------- 电气与采样常量 ----------
const POWER_FACTOR = 0.85;
const V_L1P = 230;
const V_L3P = 400;
const SQRT3 = Math.sqrt(3);

// 采样新鲜度窗口：窗口内的电流采样用于实时负载，否则回退到铭牌需求
export const CURRENT_FRESHNESS_MS = 5 * 60 * 1000;

// 默认允许时钟偏差（毫秒）。超过该阈值的倒序样本进入待审隔离区。
export const DEFAULT_MAX_CLOCK_SKEW_MS = 5000;

export function config() {
  return {
    maxClockSkewMs: Number(process.env.REEFER_MAX_CLOCK_SKEW_MS ?? DEFAULT_MAX_CLOCK_SKEW_MS),
  };
}

// ---------- 时间 ----------
export function now() {
  return Date.now();
}

// ---------- 相制 ----------
export function normPhase(p) {
  const s = String(p ?? '').trim().toUpperCase().replace(/[^A-Z0-9]/g, '');
  if (s === '1P' || s === '1') return '1P';
  if (s === '3P' || s === '3') return '3P';
  throw bad(`非法相制: ${p} (只允许 1P/3P)`);
}

// ---------- 电流/功率换算 ----------
// 单相: I = P / (U * cosφ)；三相: I = P / (sqrt(3) * U * cosφ)
export function nameplateCurrentA(powerKw, phase) {
  const p = normPhase(phase);
  const watts = Number(powerKw) * 1000;
  const denom = p === '1P' ? V_L1P * POWER_FACTOR : SQRT3 * V_L3P * POWER_FACTOR;
  return round2(watts / denom);
}

export function round2(x) {
  return Math.round((x + Number.EPSILON) * 100) / 100;
}

// ---------- 规范化 JSON 与哈希 ----------
export function stableStringify(value) {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(',')}]`;
  return `{${Object.keys(value)
    .sort()
    .map((k) => `${JSON.stringify(k)}:${stableStringify(value[k])}`)
    .join(',')}}`;
}

export function payloadHash(body) {
  const clone = { ...body };
  delete clone.commandId;
  return stableStringify(clone);
}

let cryptoFn = null;
// 幂等键 = 命令标识 + 载荷摘要：相同命令标识必须配相同摘要；
// 样本类天然去重（同传感器/时刻键）由业务层单独处理，不依赖此键
export async function commandHash(commandId, body) {
  if (!cryptoFn) cryptoFn = (await import('node:crypto')).createHash;
  return cryptoFn('sha256').update(`${commandId ?? '__nocmd__'}\0${payloadHash(body)}`).digest('hex');
}

// ---------- 标识校验 ----------
const ID_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
export function checkId(value, label) {
  if (typeof value !== 'string' || !ID_RE.test(value)) {
    throw bad(`${label} 标识非法: ${JSON.stringify(value)}`);
  }
  return value;
}

export function num(value, label, { min = 0, max = Infinity, integer = false } = {}) {
  const n = Number(value);
  if (!Number.isFinite(n) || n < min || n > max || (integer && !Number.isInteger(n))) {
    throw bad(`${label} 非法: ${JSON.stringify(value)}`);
  }
  return n;
}

export function strList(value, label) {
  if (!Array.isArray(value) || value.some((v) => typeof v !== 'string' || !v.trim())) {
    throw bad(`${label} 必须是非空字符串数组`);
  }
  return [...new Set(value.map((v) => v.trim()))];
}

// ---------- 错误 ----------
export class DomainError extends Error {
  constructor(code, message, extra = {}) {
    super(message);
    this.name = 'DomainError';
    this.code = code;
    this.extra = extra;
  }
}
export const bad = (msg, extra = {}) => new DomainError('BAD_REQUEST', msg, extra);
export const conflict = (msg, extra = {}) => new DomainError('CONFLICT', msg, extra);
export const notFound = (msg, extra = {}) => new DomainError('NOT_FOUND', msg, extra);

export function errBody(e) {
  return { ok: false, error: { code: e.code || 'INTERNAL', message: e.message, ...(e.extra || {}) } };
}
