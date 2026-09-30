'use strict';

// 全局时钟。生产环境即墙钟；测试环境可通过 /test/clock/advance 推进虚拟时间。
// 推进量会作为 clockAdvanced 事实写入流水，因此崩溃重启后虚拟时间仍然一致。
let offsetMs = 0;

export function now() {
  return Date.now() + offsetMs;
}

export function setClockOffset(ms) {
  offsetMs = Number(ms) || 0;
}

export function advanceClock(ms) {
  offsetMs += Number(ms) || 0;
  return offsetMs;
}

export function clockOffset() {
  return offsetMs;
}
