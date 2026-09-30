"""段包线格式与记录合法性校验。

终端侧把扫描写入只追加的段文件；封包时一个段 = 一个上传包：

    {
      "device_id":  "HH-01",
      "session":    "HH-01:2026-09-28T08:00:00+08:00",
      "seq":        7,
      "voyage_id":  "voy_...",
      "records": [
        {"record_id": "rec_uuid", "container": "MSKU1234567",
         "slot": "02-01-82", "collected_at": "2026-09-28T09:10:11+08:00"}
      ],
      "checksum": "<sha256-hex>"
    }

校验和只覆盖除 checksum 外的字段，采用 sort_keys 的 UTF-8 JSON，
接收端用同一份 canonical_bytes 复算，杜绝“只改线格式”绕过。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

# ISO 6346 箱号：4 个大写字母 + 7 位数字。校验位规则（第 11 位）也一并核对。
CONTAINER_RE = re.compile(r"^[A-Z]{4}\d{7}$")
# 槽位 bay-row-tier：如 02-01-82（题目域：舱内 01..n，甲板 82/84…）
SLOT_RE = re.compile(r"^\d{2}-\d{2}-\d{2}$")

# ISO 6346 字母数值表（11 的倍数 11/22/33 在序列中跳过）
ISO6346_VALUES = dict(zip(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    [10, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 23, 24, 25, 26, 27,
     28, 29, 30, 31, 32, 34, 35, 36, 37, 38]))
for _i in range(10):
    ISO6346_VALUES[str(_i)] = _i


def iso6346_check_digit(code10: str) -> int:
    total = 0
    for i, ch in enumerate(code10):
        total += ISO6346_VALUES[ch] * (2 ** i)
    return total % 11 % 10


def valid_container(container) -> bool:
    if not isinstance(container, str) or not CONTAINER_RE.match(container):
        return False
    return iso6346_check_digit(container[:10]) == int(container[10])


def valid_slot(slot) -> bool:
    return isinstance(slot, str) and bool(SLOT_RE.match(slot))


def valid_ts(ts) -> bool:
    if not isinstance(ts, str):
        return False
    try:
        datetime.fromisoformat(ts)
        return True
    except ValueError:
        return False


def canonical_bytes(envelope: dict) -> bytes:
    """除 checksum 外全部字段的确定性序列化（嵌套 records 原样参与）。"""
    body = {k: v for k, v in envelope.items() if k != "checksum"}
    return json.dumps(body, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def checksum_of(envelope: dict) -> str:
    return hashlib.sha256(canonical_bytes(envelope)).hexdigest()


def seal(envelope: dict) -> dict:
    out = dict(envelope)
    out["checksum"] = checksum_of(envelope)
    return out


def verify_checksum(envelope: dict) -> bool:
    got = envelope.get("checksum")
    return isinstance(got, str) and got == checksum_of(envelope)


class PacketError(ValueError):
    """结构/校验和级别问题（400）。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class RecordError(ValueError):
    """包内某条记录非法（422，整包拒绝）。"""

    def __init__(self, message, index=None):
        super().__init__(message)
        self.index = index


def validate_shape(envelope) -> dict:
    """只做 JSON 结构层面校验，返回规范化后的包；不碰业务、不碰校验和策略。"""
    if not isinstance(envelope, dict):
        raise PacketError("BAD_JSON", "请求体必须是 JSON 对象")
    for field in ("device_id", "session", "seq", "records", "checksum"):
        if field not in envelope:
            raise PacketError("MISSING_FIELD", f"缺少字段 {field}")
    if not isinstance(envelope["device_id"], str) or not envelope["device_id"]:
        raise PacketError("BAD_FIELD", "device_id 必须是非空字符串")
    if not isinstance(envelope["session"], str) or not envelope["session"]:
        raise PacketError("BAD_FIELD", "session 必须是非空字符串")
    if isinstance(envelope["seq"], bool) or not isinstance(envelope["seq"], int) \
            or envelope["seq"] < 0:
        raise PacketError("BAD_FIELD", "seq 必须是非负整数")
    if not isinstance(envelope["records"], list) or not envelope["records"]:
        raise PacketError("BAD_FIELD", "records 必须是非空数组")
    if not isinstance(envelope.get("checksum"), str):
        raise PacketError("BAD_FIELD", "checksum 必须是字符串")
    return envelope


def validate_records(envelope: dict) -> None:
    """逐记录合法性；任一条非法即抛 RecordError —— 调用方必须整包拒绝。"""
    seen = set()
    for i, r in enumerate(envelope["records"]):
        if not isinstance(r, dict):
            raise RecordError("记录不是对象", i)
        rid = r.get("record_id")
        if not isinstance(rid, str) or not rid:
            raise RecordError("record_id 缺失", i)
        if rid in seen:
            raise RecordError(f"包内 record_id 重复: {rid}", i)
        seen.add(rid)
        if not valid_container(r.get("container")):
            raise RecordError(f"非法箱号（ISO6346）: {r.get('container')!r}", i)
        if not valid_slot(r.get("slot")):
            raise RecordError(f"非法槽位号: {r.get('slot')!r}", i)
        if not valid_ts(r.get("collected_at")):
            raise RecordError(f"非法采集时间: {r.get('collected_at')!r}", i)
