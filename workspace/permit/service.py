"""岸桥作业许可 / 装船波次互锁 —— 业务服务层。

约定：
  - 所有多步写操作在单个 BEGIN IMMEDIATE 事务内提交；
  - PERMIT_CRASH=C1..C4 仅供测试：在对应提交边界前 os._exit(91) 模拟掉电；
  - 时间为带时区 ISO8601；测试可用 now= 注入确定时间；
  - 乐观锁：修改许可必须带 expected_revision，旧修订返回 409 + 最新占用快照。
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime

from . import store
from .store import PermitError, connect, init_db  # re-export


# ---------------------------------------------------------------- 基础工具

def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_ts(value, field="time") -> datetime:
    if not value:
        raise PermitError(400, "BAD_TIME", f"{field} 不能为空")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise PermitError(400, "BAD_TIME", f"{field} 不是合法 ISO8601 时间: {value}")
    if dt.tzinfo is None:
        raise PermitError(400, "BAD_TIME", f"{field} 必须带时区")
    return dt


def _db_path() -> str:
    return os.environ.get("PERMIT_DB", store.DB_PATH)


def _rows(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _one(conn, sql, args=()):
    row = conn.execute(sql, args).fetchone()
    return dict(row) if row else None


def log_event(conn, actor, action, detail=None):
    conn.execute(
        "INSERT INTO events (actor, action, detail, created_at) VALUES (?,?,?,?)",
        (actor, action,
         json.dumps(detail, ensure_ascii=False) if detail is not None else None,
         now_iso()))


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------- 崩溃注入

_CRASH_USED = set()


def crash_point(point):
    """测试钩子：PERMIT_CRASH 含该点且本进程尚未触发过时，掉电退出。"""
    flag = os.environ.get("PERMIT_CRASH", "")
    if point in flag.split(",") and point not in _CRASH_USED:
        _CRASH_USED.add(point)
        os._exit(91)


# ---------------------------------------------------------------- 幂等

def _idem_begin(conn, idem_key, scope, payload):
    """返回事务内已存响应 dict（重放）或 None（首次）。调用方提交后无需再写。"""
    if not idem_key:
        return None
    req_hash = _canon_hash({"scope": scope, "payload": payload})
    row = conn.execute(
        "SELECT request_hash, status_code, response FROM idem_records WHERE idem_key=?",
        (idem_key,)).fetchone()
    if row:
        if row["request_hash"] != req_hash:
            raise PermitError(409, "IDEM_KEY_REUSE",
                              "幂等键已用于不同请求体")
        return {"status": row["status_code"],
                "body": json.loads(row["response"]), "replay": True}
    conn.execute(
        "INSERT INTO idem_records (idem_key, scope, request_hash, status_code, "
        "response, created_at) VALUES (?,?,?,?,?,?)",
        (idem_key, scope, req_hash, 0, "", now_iso()))
    return None


def _idem_finish(conn, idem_key, status, body):
    if not idem_key:
        return
    conn.execute(
        "UPDATE idem_records SET status_code=?, response=? WHERE idem_key=?",
        (status, json.dumps(body, ensure_ascii=False), idem_key))


def _canon_hash(obj) -> str:
    import hashlib
    raw = json.dumps(obj, sort_keys=True, ensure_ascii=False,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------- 配载底账

def create_plan(data, actor="dispatcher"):
    pid = data.get("id") or new_id("plan")
    name = data.get("name") or pid
    hatches = data.get("hatches") or []
    if not hatches:
        raise PermitError(400, "NO_HATCHES", "配载方案至少需要一个舱区")
    containers = data.get("containers") or []
    entries = data.get("entries")
    conn = connect(_db_path())
    conn.execute("BEGIN IMMEDIATE")
    try:
        if _one(conn, "SELECT id FROM plans WHERE id=?", (pid,)):
            raise PermitError(409, "PLAN_EXISTS", f"方案已存在: {pid}")
        for h in hatches:
            conn.execute(
                "INSERT INTO hatches (code, bay_from, bay_to, name) VALUES (?,?,?,?)",
                (h["code"], int(h["bay_from"]), int(h["bay_to"]), h.get("name")))
        for a, b in (data.get("conflicts") or []):
            if a == b:
                raise PermitError(400, "BAD_CONFLICT", f"舱区不能与自身冲突: {a}")
            for x, y in ((a, b), (b, a)):
                conn.execute(
                    "INSERT OR IGNORE INTO hatch_conflicts (hatch_a, hatch_b) "
                    "VALUES (?,?)", (x, y))
        valid_hatches = {h["code"] for h in hatches}
        for c in containers:
            if c["hatch_code"] not in valid_hatches:
                raise PermitError(400, "BAD_CONTAINER",
                                  f"箱 {c['id']} 引用了未知舱区 {c['hatch_code']}")
            conn.execute(
                "INSERT INTO containers (id, hatch_code, stack_key, stack_seq, "
                "tier_code) VALUES (?,?,?,?,?)",
                (c["id"], c["hatch_code"], str(c["stack_key"]),
                 int(c["stack_seq"]), c.get("tier_code")))
        entry_ids = entries if entries is not None else [c["id"] for c in containers]
        unknown = [cid for cid in entry_ids
                   if not _one(conn, "SELECT 1 FROM containers WHERE id=?", (cid,))]
        if unknown:
            raise PermitError(400, "BAD_ENTRY", f"箱号不存在: {unknown[:5]}")
        conn.execute(
            "INSERT INTO plans (id, name, status, created_at) VALUES (?,?,?,?)",
            (pid, name, "draft", now_iso()))
        for cid in entry_ids:
            h = conn.execute("SELECT hatch_code FROM containers WHERE id=?",
                             (cid,)).fetchone()["hatch_code"]
            conn.execute(
                "INSERT INTO plan_entries (plan_id, container_id, hatch_code) "
                "VALUES (?,?,?)", (pid, cid, h))
        log_event(conn, actor, "plan.create", {"plan_id": pid, "boxes": len(entry_ids)})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return plan_detail(pid), 201


def publish_plan(plan_id, actor="dispatcher"):
    conn = connect(_db_path())
    conn.execute("BEGIN IMMEDIATE")
    try:
        plan = _one(conn, "SELECT * FROM plans WHERE id=?", (plan_id,))
        if not plan:
            raise PermitError(404, "PLAN_NOT_FOUND", f"方案不存在: {plan_id}")
        if not _one(conn, "SELECT 1 FROM plan_entries WHERE plan_id=? LIMIT 1",
                    (plan_id,)):
            raise PermitError(409, "EMPTY_PLAN", "空方案不能发布")
        if plan["status"] != "published":
            conn.execute(
                "UPDATE plans SET status='published', published_at=? WHERE id=?",
                (now_iso(), plan_id))
            log_event(conn, actor, "plan.publish", {"plan_id": plan_id})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return plan_detail(plan_id), 200


def plan_detail(plan_id):
    conn = connect(_db_path())
    try:
        plan = _one(conn, "SELECT * FROM plans WHERE id=?", (plan_id,))
        if not plan:
            raise PermitError(404, "PLAN_NOT_FOUND", f"方案不存在: {plan_id}")
        plan["entries"] = _rows(
            conn, "SELECT container_id, hatch_code FROM plan_entries "
                  "WHERE plan_id=? ORDER BY container_id", (plan_id,))
        return plan
    finally:
        conn.close()


def list_plans():
    conn = connect(_db_path())
    try:
        plans = _rows(conn,
                      "SELECT p.*, (SELECT COUNT(*) FROM plan_entries e "
                      "WHERE e.plan_id=p.id) n FROM plans p ORDER BY p.created_at")
        return {"plans": plans}
    finally:
        conn.close()


# ---------------------------------------------------------------- 波次

def publish_wave(plan_id, data, actor="dispatcher", idem_key=None, now=None):
    """把已发布配载方案拆成波次；发布时由堆叠顺序推导前置关系。"""
    now = now or now_iso()
    name = data.get("name") or f"wave-{now[:16]}"
    subset = data.get("container_ids")
    wave_id = data.get("id")  # 仅测试/导入使用
    conn = connect(_db_path())
    conn.execute("BEGIN IMMEDIATE")
    try:
        replay = _idem_begin(conn, idem_key, "wave_publish",
                             {"plan_id": plan_id, "name": name, "subset": subset})
        if replay:
            conn.commit()
            return replay["body"], replay["status"]
        plan = _one(conn, "SELECT * FROM plans WHERE id=?", (plan_id,))
        if not plan:
            raise PermitError(404, "PLAN_NOT_FOUND", f"方案不存在: {plan_id}")
        if plan["status"] != "published":
            raise PermitError(409, "PLAN_NOT_PUBLISHED",
                              "只能拆分已发布的配载方案")
        entries = _rows(conn,
            "SELECT pe.container_id cid, c.hatch_code, c.stack_key, c.stack_seq "
            "FROM plan_entries pe JOIN containers c ON c.id=pe.container_id "
            "WHERE pe.plan_id=? ORDER BY c.stack_key, c.stack_seq", (plan_id,))
        if subset is not None:
            chosen = set(subset)
            entries = [e for e in entries if e["cid"] in chosen]
            missing = chosen - {e["cid"] for e in entries}
            if missing:
                raise PermitError(400, "BOX_NOT_IN_PLAN",
                                  f"箱不属于该方案: {sorted(missing)[:5]}")
        if not entries:
            raise PermitError(400, "EMPTY_WAVE", "波次箱集合为空")
        wave_id = wave_id or new_id("wave")
        if _one(conn, "SELECT 1 FROM waves WHERE id=?", (wave_id,)):
            raise PermitError(409, "WAVE_EXISTS", f"波次已存在: {wave_id}")
        conn.execute(
            "INSERT INTO waves (id, plan_id, name, status, total, executed, "
            "created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (wave_id, plan_id, name, "released", len(entries), 0, actor, now))
        by_stack = {}
        for e in entries:
            by_stack.setdefault(e["stack_key"], []).append(e)
        for stack_items in by_stack.values():
            stack_items.sort(key=lambda e: e["stack_seq"])
            lower = []
            for e in stack_items:
                # 同堆自底向上：所有下层箱（无论是否已在其他波次执行）构成前置；
                # 其中属于本波次的箱作为波次内可跟踪前置边。
                in_wave = [x["cid"] for x in lower]
                conn.execute(
                    "INSERT INTO wave_containers (wave_id, container_id, hatch_code, "
                    "stack_key, stack_seq, prereq_ids) VALUES (?,?,?,?,?,?)",
                    (wave_id, e["cid"], e["hatch_code"], e["stack_key"],
                     e["stack_seq"], json.dumps(in_wave)))
                lower.append(e)
        log_event(conn, actor, "wave.publish",
                  {"wave_id": wave_id, "plan_id": plan_id, "boxes": len(entries)})
        body = wave_detail_conn(conn, wave_id)
        _idem_finish(conn, idem_key, 201, body)
        conn.commit()
        return body, 201
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def wave_detail_conn(conn, wave_id):
    wave = _one(conn, "SELECT * FROM waves WHERE id=?", (wave_id,))
    if not wave:
        raise PermitError(404, "WAVE_NOT_FOUND", f"波次不存在: {wave_id}")
    boxes = _rows(conn,
        "SELECT wc.*, pc.permit_id AS owner_permit_id, pc.state AS permit_state "
        "FROM wave_containers wc LEFT JOIN permit_containers pc "
        "ON pc.wave_id=wc.wave_id AND pc.container_id=wc.container_id "
        "AND pc.state='active' WHERE wc.wave_id=? "
        "ORDER BY wc.stack_key, wc.stack_seq", (wave_id,))
    status = {b["container_id"]: b["executed"] for b in boxes}
    workable, blocked = [], []
    for b in boxes:
        b["prereq_ids"] = json.loads(b["prereq_ids"] or "[]")
        blockers = [p for p in b["prereq_ids"] if not status.get(p)]
        b["blocked_by"] = blockers
        if b["executed"]:
            pass
        elif blockers:
            blocked.append(b["container_id"])
        else:
            workable.append(b["container_id"])
    wave["boxes"] = boxes
    wave["workable"] = workable
    wave["blocked"] = blocked
    wave["permits"] = _rows(conn,
        "SELECT * FROM permits WHERE wave_id=? ORDER BY created_at", (wave_id,))
    return wave


def wave_detail(wave_id):
    conn = connect(_db_path())
    try:
        return wave_detail_conn(conn, wave_id)
    finally:
        conn.close()


def list_waves():
    conn = connect(_db_path())
    try:
        return {"waves": _rows(conn,
            "SELECT w.*, (SELECT COUNT(*) FROM permits p WHERE p.wave_id=w.id) "
            "permit_count FROM waves w ORDER BY w.created_at")}
    finally:
        conn.close()


# ---------------------------------------------------------------- 舱区冲突

def _windows_overlap(a_from, a_to, b_from, b_to) -> bool:
    return a_from < b_to and b_from < a_to


def hatch_conflicts_conn(conn, hatch_codes):
    """返回舱区集合内部的相邻冲突对。"""
    codes = list(hatch_codes)
    rel = set()
    for r in conn.execute("SELECT hatch_a, hatch_b FROM hatch_conflicts"):
        rel.add((r["hatch_a"], r["hatch_b"]))
    pairs = []
    for i, a in enumerate(codes):
        for b in codes[i + 1:]:
            if (a, b) in rel:
                pairs.append([a, b])
    return pairs


def find_grant_conflicts(conn, hatches, valid_from, valid_to, crane_id,
                         wave_id=None, exclude_permit=None):
    """同一时段：同舱区或相邻冲突舱区不得授予不同岸桥（跨波次全局检查）。

    active 与 suspended 许可都占用舱区；revoked 已释放；过期与否由时段重叠
    自然判定。
    """
    vf, vt = parse_ts(valid_from, "valid_from"), parse_ts(valid_to, "valid_to")
    if not (vf < vt):
        raise PermitError(400, "BAD_WINDOW", "valid_from 必须早于 valid_to")
    rel = set()
    for r in conn.execute("SELECT hatch_a, hatch_b FROM hatch_conflicts"):
        rel.add((r["hatch_a"], r["hatch_b"]))
    clashes = []
    others = _rows(conn,
        "SELECT p.*, GROUP_CONCAT(ph.hatch_code) hatch_csv FROM permits p "
        "JOIN permit_hatches ph ON ph.permit_id=p.id "
        "WHERE p.status IN ('active','suspended') AND p.id != ? "
        "GROUP BY p.id", (exclude_permit or "",))
    for o in others:
        if o["crane_id"] == crane_id:
            continue  # 同一岸桥的多张许可不互斥
        of, ot = parse_ts(o["valid_from"]), parse_ts(o["valid_to"])
        if not _windows_overlap(vf, vt, of, ot):
            continue
        for h in hatches:
            for oh in o["hatch_csv"].split(","):
                if h == oh:
                    clashes.append({"hatch": h, "other_hatch": oh,
                                    "relation": "same", "permit_id": o["id"],
                                    "crane_id": o["crane_id"]})
                elif (h, oh) in rel:
                    clashes.append({"hatch": h, "other_hatch": oh,
                                    "relation": "adjacent", "permit_id": o["id"],
                                    "crane_id": o["crane_id"]})
    return clashes


def latest_occupancy(conn, wave_id=None):
    """最新舱区占用快照（409 时随响应返回，供调度页重绘）。"""
    sql = ("SELECT p.id permit_id, p.wave_id, p.crane_id, p.revision, p.status, "
           "p.valid_from, p.valid_to, ph.hatch_code "
           "FROM permits p JOIN permit_hatches ph ON ph.permit_id=p.id "
           "WHERE p.status IN ('active','suspended')")
    args = ()
    if wave_id:
        sql += " AND p.wave_id=?"
        args = (wave_id,)
    sql += " ORDER BY p.valid_from"
    return _rows(conn, sql, args)


# ---------------------------------------------------------------- 许可

def _seq_permit_no(conn):
    n = conn.execute(
        "SELECT COUNT(*) c FROM permits").fetchone()["c"] + 1
    return f"PMT-{n:04d}"


def _load_active_permit(conn, permit_id):
    p = _one(conn, "SELECT * FROM permits WHERE id=?", (permit_id,))
    if not p:
        raise PermitError(404, "PERMIT_NOT_FOUND", f"许可不存在: {permit_id}")
    return p


def create_permit(wave_id, data, actor="dispatcher", now=None):
    now = now or now_iso()
    crane = data.get("crane_id")
    hatches = data.get("hatches") or []
    vf, vt = data.get("valid_from"), data.get("valid_to")
    box_ids = data.get("container_ids")
    if not crane:
        raise PermitError(400, "NO_CRANE", "必须指定岸桥 crane_id")
    if not hatches:
        raise PermitError(400, "NO_HATCHES", "必须至少分配一个舱区")
    parse_ts(vf, "valid_from")
    parse_ts(vt, "valid_to")
    conn = connect(_db_path())
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not _one(conn, "SELECT 1 FROM waves WHERE id=?", (wave_id,)):
            raise PermitError(404, "WAVE_NOT_FOUND", f"波次不存在: {wave_id}")
        unknown = [h for h in hatches if not _one(
            conn, "SELECT 1 FROM hatches WHERE code=?", (h,))]
        if unknown:
            raise PermitError(400, "BAD_HATCH", f"舱区不存在: {unknown}")
        clashes = find_grant_conflicts(conn, hatches, vf, vt, crane)
        if clashes:
            raise PermitError(409, "HATCH_CONFLICT",
                              "同一时段相邻/相同舱区已授予其他岸桥",
                              {"conflicts": clashes,
                               "occupancy": latest_occupancy(conn, wave_id)})
        # 箱集合：显式给出或默认该波次在这些舱区里尚未归属任何许可的箱
        wave_boxes = {r["container_id"]: r for r in _rows(conn,
            "SELECT container_id, hatch_code, executed FROM wave_containers "
            "WHERE wave_id=?", (wave_id,))}
        if box_ids is None:
            box_ids = [cid for cid, r in wave_boxes.items()
                       if r["hatch_code"] in hatches and not r["executed"]
                       and not _one(conn,
                           "SELECT 1 FROM permit_containers WHERE wave_id=? "
                           "AND container_id=? AND state='active'",
                           (wave_id, cid))]
        for cid in box_ids:
            r = wave_boxes.get(cid)
            if not r:
                raise PermitError(400, "BOX_NOT_IN_WAVE",
                                  f"箱不属于该波次: {cid}")
            if r["hatch_code"] not in hatches:
                raise PermitError(409, "BOX_OUT_OF_HATCH",
                                  f"箱 {cid} 的舱区 {r['hatch_code']} 不在许可舱区内",
                                  {"container_id": cid, "hatch_code": r["hatch_code"]})
            owner = _one(conn,
                "SELECT permit_id FROM permit_containers WHERE wave_id=? "
                "AND container_id=? AND state='active'", (wave_id, cid))
            if owner:
                raise PermitError(409, "BOX_OWNED_ELSEWHERE",
                                  f"箱 {cid} 已归属有效许可 {owner['permit_id']}",
                                  {"container_id": cid,
                                   "owner_permit": owner["permit_id"]})
        pid = data.get("id") or _seq_permit_no(conn)
        if _one(conn, "SELECT 1 FROM permits WHERE id=?", (pid,)):
            raise PermitError(409, "PERMIT_EXISTS", f"许可编号已存在: {pid}")
        conn.execute(
            "INSERT INTO permits (id, wave_id, crane_id, revision, status, "
            "valid_from, valid_to, created_by, created_at, updated_at) "
            "VALUES (?,?,?,1,'active',?,?,?,?,?)",
            (pid, wave_id, crane, vf, vt, actor, now, now))
        for h in hatches:
            conn.execute(
                "INSERT INTO permit_hatches (permit_id, hatch_code) VALUES (?,?)",
                (pid, h))
        for cid in box_ids:
            conn.execute(
                "INSERT INTO permit_containers (permit_id, wave_id, container_id, "
                "state) VALUES (?,?,?,'active')", (pid, wave_id, cid))
            conn.execute(
                "UPDATE wave_containers SET owner_permit=? "
                "WHERE wave_id=? AND container_id=?", (pid, wave_id, cid))
        log_event(conn, actor, "permit.create",
                  {"permit_id": pid, "crane": crane, "hatches": hatches,
                   "boxes": len(box_ids), "window": [vf, vt]})
        crash_point("C1")  # 保存许可事务提交前掉电 -> 整体回滚
        conn.commit()
        return permit_detail_conn(conn, pid), 201
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def permit_detail_conn(conn, permit_id):
    p = _one(conn, "SELECT * FROM permits WHERE id=?", (permit_id,))
    if not p:
        raise PermitError(404, "PERMIT_NOT_FOUND", f"许可不存在: {permit_id}")
    p["hatches"] = [r["hatch_code"] for r in conn.execute(
        "SELECT hatch_code FROM permit_hatches WHERE permit_id=? ORDER BY hatch_code",
        (permit_id,))]
    p["containers"] = _rows(conn,
        "SELECT container_id, state FROM permit_containers "
        "WHERE permit_id=? ORDER BY container_id", (permit_id,))
    p["active_count"] = sum(1 for c in p["containers"] if c["state"] == "active")
    p["executed_count"] = sum(1 for c in p["containers"] if c["state"] == "executed")
    # 可作业清单：归属本许可、未执行、前置已完成
    boxes = _rows(conn,
        "SELECT wc.container_id, wc.prereq_ids, wc.executed "
        "FROM permit_containers pc JOIN wave_containers wc "
        "ON wc.wave_id=pc.wave_id AND wc.container_id=pc.container_id "
        "WHERE pc.permit_id=? AND pc.state='active'", (permit_id,))
    exec_set = {r["container_id"] for r in conn.execute(
        "SELECT container_id FROM wave_containers WHERE wave_id=? AND executed=1",
        (p["wave_id"],))}
    workable, blocked = [], []
    for b in boxes:
        prereq = json.loads(b["prereq_ids"] or "[]")
        pending = [x for x in prereq if x not in exec_set]
        (blocked if pending else workable).append(
            {"container_id": b["container_id"], "blocked_by": pending})
    p["workable"] = workable
    p["blocked"] = blocked
    return p


def permit_detail(permit_id):
    conn = connect(_db_path())
    try:
        return permit_detail_conn(conn, permit_id)
    finally:
        conn.close()


def list_permits(wave_id=None):
    conn = connect(_db_path())
    try:
        sql = "SELECT * FROM permits"
        args = ()
        if wave_id:
            sql += " WHERE wave_id=?"
            args = (wave_id,)
        sql += " ORDER BY created_at"
        out = []
        for r in _rows(conn, sql, args):
            r["hatches"] = [x["hatch_code"] for x in conn.execute(
                "SELECT hatch_code FROM permit_hatches WHERE permit_id=?",
                (r["id"],))]
            out.append(r)
        return {"permits": out}
    finally:
        conn.close()


def _check_revision(p, expected):
    if expected is None:
        raise PermitError(400, "REVISION_REQUIRED",
                          "必须携带 expected_revision")
    if int(expected) != p["revision"]:
        raise PermitError(409, "STALE_REVISION",
                          f"修订号过期：客户端 r{expected}，最新 r{p['revision']}",
                          {"latest_revision": p["revision"]})


def update_permit(permit_id, data, actor="dispatcher", now=None):
    """两名调度员并发调整：旧修订不得覆盖新修订（乐观锁 + IMMEDIATE 串行）。"""
    now = now or now_iso()
    conn = connect(_db_path())
    conn.execute("BEGIN IMMEDIATE")
    try:
        p = _load_active_permit(conn, permit_id)
        _check_revision(p, data.get("expected_revision"))
        new_hatches = data.get("hatches")
        new_boxes = data.get("container_ids")
        vf = data.get("valid_from", p["valid_from"])
        vt = data.get("valid_to", p["valid_to"])
        crane = data.get("crane_id", p["crane_id"])
        if new_hatches is not None:
            unknown = [h for h in new_hatches
                       if not _one(conn, "SELECT 1 FROM hatches WHERE code=?", (h,))]
            if unknown:
                raise PermitError(400, "BAD_HATCH", f"舱区不存在: {unknown}")
            check_hatches = new_hatches
        else:
            check_hatches = [r["hatch_code"] for r in conn.execute(
                "SELECT hatch_code FROM permit_hatches WHERE permit_id=?",
                (permit_id,))]
        clashes = find_grant_conflicts(conn, check_hatches, vf, vt, crane,
                                       exclude_permit=permit_id)
        if clashes:
            raise PermitError(409, "HATCH_CONFLICT",
                              "调整后与其他岸桥在同一时段冲突",
                              {"conflicts": clashes,
                               "latest_revision": p["revision"],
                               "occupancy": latest_occupancy(conn, p["wave_id"])})
        if new_hatches is not None:
            conn.execute("DELETE FROM permit_hatches WHERE permit_id=?", (permit_id,))
            for h in new_hatches:
                conn.execute(
                    "INSERT INTO permit_hatches (permit_id, hatch_code) "
                    "VALUES (?,?)", (permit_id, h))
            # 已授权但不在新舱区集合内的未执行箱 -> 释放
            placeholders = ",".join("?" for _ in new_hatches)
            if new_hatches:
                conn.execute(
                    f"UPDATE permit_containers SET state='released' "
                    f"WHERE permit_id=? AND state='active' AND container_id IN "
                    f"(SELECT container_id FROM wave_containers WHERE wave_id=? "
                    f"AND hatch_code NOT IN ({placeholders}))",
                    [permit_id, p["wave_id"], *new_hatches])
            else:
                conn.execute(
                    "UPDATE permit_containers SET state='released' "
                    "WHERE permit_id=? AND state='active'", (permit_id,))
        if new_boxes is not None:
            wave_boxes = {r["container_id"]: r for r in _rows(conn,
                "SELECT container_id, hatch_code, executed FROM wave_containers "
                "WHERE wave_id=?", (p["wave_id"],))}
            for cid in new_boxes:
                r = wave_boxes.get(cid)
                if not r:
                    raise PermitError(400, "BOX_NOT_IN_WAVE",
                                      f"箱不属于该波次: {cid}")
                if r["hatch_code"] not in check_hatches:
                    raise PermitError(409, "BOX_OUT_OF_HATCH",
                                      f"箱 {cid} 舱区不在许可范围内")
                owner = _one(conn,
                    "SELECT permit_id FROM permit_containers WHERE wave_id=? "
                    "AND container_id=? AND state='active'",
                    (p["wave_id"], cid))
                if owner and owner["permit_id"] != permit_id:
                    raise PermitError(409, "BOX_OWNED_ELSEWHERE",
                                      f"箱 {cid} 已归属 {owner['permit_id']}")
            keep = set(new_boxes)
            conn.execute(
                "UPDATE permit_containers SET state='released' "
                "WHERE permit_id=? AND state='active' "
                + ("AND container_id NOT IN (%s)" % ",".join("?" * len(keep))
                   if keep else ""),
                [permit_id, *keep] if keep else (permit_id,))
            for cid in new_boxes:
                conn.execute(
                    "INSERT INTO permit_containers (permit_id, wave_id, "
                    "container_id, state) VALUES (?,?,?,'active') "
                    "ON CONFLICT(permit_id, wave_id, container_id) "
                    "DO UPDATE SET state='active'",
                    (permit_id, p["wave_id"], cid))
                conn.execute(
                    "UPDATE wave_containers SET owner_permit=? WHERE wave_id=? "
                    "AND container_id=?", (permit_id, p["wave_id"], cid))
        conn.execute(
            "UPDATE permits SET revision=revision+1, crane_id=?, valid_from=?, "
            "valid_to=?, updated_at=? WHERE id=?",
            (crane, vf, vt, now, permit_id))
        conn.execute(
            "UPDATE wave_containers SET owner_permit=NULL WHERE wave_id=? "
            "AND container_id IN (SELECT container_id FROM permit_containers "
            "WHERE permit_id=? AND state='released')",
            (p["wave_id"], permit_id))
        log_event(conn, actor, "permit.update",
                  {"permit_id": permit_id, "hatches": check_hatches,
                   "window": [vf, vt]})
        conn.commit()
        return permit_detail_conn(conn, permit_id), 200
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _set_status(permit_id, status, actor, reason=None, now=None):
    now = now or now_iso()
    conn = connect(_db_path())
    conn.execute("BEGIN IMMEDIATE")
    try:
        p = _load_active_permit(conn, permit_id)
        if status == "suspended" and p["status"] not in ("active",):
            raise PermitError(409, "BAD_PERMIT_STATE",
                              f"许可状态为 {p['status']}，不能暂停")
        if status == "active" and p["status"] != "suspended":
            raise PermitError(409, "BAD_PERMIT_STATE",
                              f"许可状态为 {p['status']}，只有暂停态可恢复")
        if status == "revoked" and p["status"] == "revoked":
            raise PermitError(409, "BAD_PERMIT_STATE", "许可已撤销")
        conn.execute("UPDATE permits SET status=?, revision=revision+1, "
                     "updated_at=?, suspend_reason=? WHERE id=?",
                     (status, now, reason, permit_id))
        if status == "revoked":
            # 撤销释放全部未执行箱
            conn.execute(
                "UPDATE permit_containers SET state='released' "
                "WHERE permit_id=? AND state='active'", (permit_id,))
            conn.execute(
                "UPDATE wave_containers SET owner_permit=NULL WHERE wave_id=? "
                "AND container_id IN (SELECT container_id FROM permit_containers "
                "WHERE permit_id=? AND state='released')",
                (p["wave_id"], permit_id))
        log_event(conn, actor, f"permit.{status}",
                  {"permit_id": permit_id, "reason": reason})
        conn.commit()
        return permit_detail_conn(conn, permit_id), 200
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def suspend_permit(permit_id, data, actor="dispatcher"):
    return _set_status(permit_id, "suspended", actor,
                       reason=(data or {}).get("reason"))


def resume_permit(permit_id, data, actor="dispatcher"):
    return _set_status(permit_id, "active", actor)


def revoke_permit(permit_id, data, actor="dispatcher"):
    return _set_status(permit_id, "revoked", actor,
                       reason=(data or {}).get("reason"))


# ---------------------------------------------------------------- 许可转交

def transfer_permit(source_id, data, actor="dispatcher", idem_key=None, now=None):
    """整许可转交：已执行箱保持原归属，未执行箱一次性全部迁移。"""
    now = now or now_iso()
    target_id = data.get("target_permit_id")
    if not target_id:
        raise PermitError(400, "NO_TARGET", "必须指定 target_permit_id")
    conn = connect(_db_path())
    conn.execute("BEGIN IMMEDIATE")
    try:
        replay = _idem_begin(conn, idem_key, "permit_transfer",
                             {"source": source_id, "target": target_id})
        if replay:
            conn.commit()
            return replay["body"], replay["status"]
        src = _load_active_permit(conn, source_id)
        tgt = _load_active_permit(conn, target_id)
        if data.get("expected_source_revision") is not None:
            _check_revision(src, data["expected_source_revision"])
        if data.get("expected_target_revision") is not None:
            _check_revision(tgt, data["expected_target_revision"])
        if src["wave_id"] != tgt["wave_id"]:
            raise PermitError(409, "WAVE_MISMATCH",
                              "只能转交到同一波次的许可")
        if tgt["status"] != "active":
            raise PermitError(409, "TARGET_NOT_ACTIVE",
                              f"目标许可状态为 {tgt['status']}，必须有效")
        cur = parse_ts(now)
        if not (parse_ts(tgt["valid_from"]) <= cur < parse_ts(tgt["valid_to"])):
            raise PermitError(409, "TARGET_NOT_VALID_NOW",
                              "目标许可当前不在有效时段内")
        migrating = _rows(conn,
            "SELECT pc.container_id, wc.hatch_code FROM permit_containers pc "
            "JOIN wave_containers wc ON wc.wave_id=pc.wave_id "
            "AND wc.container_id=pc.container_id "
            "WHERE pc.permit_id=? AND pc.state='active'", (source_id,))
        tgt_hatches = {r["hatch_code"] for r in conn.execute(
            "SELECT hatch_code FROM permit_hatches WHERE permit_id=?",
            (target_id,))}
        outside = sorted({m["hatch_code"] for m in migrating
                          if m["hatch_code"] not in tgt_hatches})
        if outside:
            raise PermitError(409, "TRANSFER_HATCH_MISMATCH",
                              f"目标许可不覆盖舱区 {outside}",
                              {"missing_hatches": outside,
                               "occupancy": latest_occupancy(conn, src["wave_id"])})
        for m in migrating:
            cid = m["container_id"]
            # 先删源归属再插目标归属；部分唯一索引保证中途不可能有两个 active 属主
            conn.execute(
                "DELETE FROM permit_containers WHERE permit_id=? AND wave_id=? "
                "AND container_id=? AND state='active'",
                (source_id, src["wave_id"], cid))
            conn.execute(
                "INSERT INTO permit_containers (permit_id, wave_id, "
                "container_id, state) VALUES (?,?,?,'active') "
                "ON CONFLICT(permit_id, wave_id, container_id) "
                "DO UPDATE SET state='active'",
                (target_id, src["wave_id"], cid))
            conn.execute(
                "UPDATE wave_containers SET owner_permit=? WHERE wave_id=? "
                "AND container_id=?", (target_id, src["wave_id"], cid))
        conn.execute(
            "UPDATE permits SET revision=revision+1, updated_at=? WHERE id IN (?,?)",
            (now, source_id, target_id))
        log_event(conn, actor, "permit.transfer",
                  {"source": source_id, "target": target_id,
                   "migrated": len(migrating)})
        crash_point("C2")  # 迁移未执行箱事务提交前掉电 -> 全部回滚
        body = {"source": permit_detail_conn(conn, source_id),
                "target": permit_detail_conn(conn, target_id),
                "migrated": [m["container_id"] for m in migrating]}
        _idem_finish(conn, idem_key, 200, body)
        conn.commit()
        return body, 200
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------- 扫描授权

EXCEPTION_REASONS = {
    "UNKNOWN_PERMIT", "STALE_REVISION", "PERMIT_SUSPENDED", "PERMIT_REVOKED",
    "PERMIT_EXPIRED", "NOT_IN_WAVE", "OUT_OF_HATCH", "PERMIT_RELEASED",
}


def _register_exception(conn, data):
    eid = new_id("exc")
    conn.execute(
        "INSERT INTO auth_exceptions (id, wave_id, permit_id, permit_rev, "
        "container_id, scanner_id, reason, detail, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,'open',?)",
        (eid, data.get("wave_id"), data.get("permit_id"), data.get("permit_rev"),
         data.get("container_id"), data.get("scanner_id"), data["reason"],
         data.get("detail"), now_iso()))
    log_event(conn, data.get("scanner_id"), "scan.exception",
              {"exception_id": eid, "reason": data["reason"],
               "permit_id": data.get("permit_id"),
               "container_id": data.get("container_id")})
    return eid


def confirm_scan(data, actor=None, idem_key=None, now=None):
    """码头扫描：必须携带许可编号 + 修订号。

    成功（许可有效 / 箱属该波次该许可 / 前置完成）-> 登记装船记录。
    授权失败 -> 写入 auth_exceptions，返回 200 以便手持终端继续作业流。
    前置未完成 -> 409 PREREQ_BLOCKED，不产生装船记录也不算授权异常。
    """
    now = now or now_iso()
    permit_no = data.get("permit_id")
    revision = data.get("revision")
    cid = data.get("container_id")
    scanner = data.get("scanner_id") or actor or "scanner"
    slot = data.get("slot_code")
    if not permit_no or revision is None or not cid:
        raise PermitError(400, "BAD_SCAN",
                          "扫描必须携带 permit_id、revision、container_id")
    try:
        revision = int(revision)
    except (TypeError, ValueError):
        raise PermitError(400, "BAD_SCAN", "revision 必须是整数")

    conn = connect(_db_path())
    conn.execute("BEGIN IMMEDIATE")
    try:
        replay = _idem_begin(conn, idem_key, "scan",
                             {"permit_id": permit_no, "revision": revision,
                              "container_id": cid, "scanner_id": scanner,
                              "slot_code": slot, "scanned_at": data.get("scanned_at")})
        if replay:
            conn.commit()
            return replay["body"], replay["status"]

        p = _one(conn, "SELECT * FROM permits WHERE id=?", (permit_no,))

        def deny(reason, detail=None, wave_id=None, rev=None):
            eid = _register_exception(conn, {
                "wave_id": wave_id or (p["wave_id"] if p else None),
                "permit_id": permit_no,
                "permit_rev": rev if rev is not None else revision,
                "container_id": cid, "scanner_id": scanner,
                "reason": reason, "detail": detail})
            body = {"outcome": "exception", "exception_id": eid,
                    "reason": reason, "detail": detail}
            _idem_finish(conn, idem_key, 200, body)
            conn.commit()
            return body, 200

        if not p:
            return deny("UNKNOWN_PERMIT", "许可编号不存在")
        wave_id = p["wave_id"]
        # 已装船：天然幂等，直接回放原记录（即使手持端引用了旧修订）
        wc = _one(conn, "SELECT * FROM wave_containers WHERE wave_id=? AND "
                        "container_id=?", (wave_id, cid))
        prev = _one(conn, "SELECT * FROM loading_scans WHERE wave_id=? AND "
                          "container_id=?", (wave_id, cid))
        if prev:
            body = {"outcome": "loaded", "duplicate": True, "scan": prev}
            _idem_finish(conn, idem_key, 200, body)
            conn.commit()
            return body, 200
        if revision != p["revision"]:
            return deny("STALE_REVISION",
                        f"扫描引用 r{revision}，当前为 r{p['revision']}",
                        rev=revision)
        if p["status"] == "suspended":
            return deny("PERMIT_SUSPENDED", "许可已暂停")
        if p["status"] == "revoked":
            return deny("PERMIT_REVOKED", "许可已撤销")
        cur = parse_ts(now)
        if cur >= parse_ts(p["valid_to"]):
            return deny("PERMIT_EXPIRED",
                        f"许可已于 {p['valid_to']} 到期")
        if cur < parse_ts(p["valid_from"]):
            return deny("PERMIT_EXPIRED",
                        f"许可尚未生效（{p['valid_from']} 起）")
        if not wc:
            return deny("NOT_IN_WAVE",
                        f"箱 {cid} 不属于该波次", wave_id=wave_id)
        owner = _one(conn,
            "SELECT state FROM permit_containers WHERE permit_id=? AND wave_id=? "
            "AND container_id=?", (permit_no, wave_id, cid))
        active_owner = _one(conn,
            "SELECT permit_id FROM permit_containers WHERE wave_id=? "
            "AND container_id=? AND state='active'", (wave_id, cid))
        if not owner:
            return deny("OUT_OF_HATCH",
                        f"箱 {cid} 不在许可 {permit_no} 的箱集合内")
        if owner["state"] == "released" or (
                active_owner and active_owner["permit_id"] != permit_no):
            return deny(
                "PERMIT_RELEASED" if owner["state"] == "released"
                else "OUT_OF_HATCH",
                f"箱当前归属许可 {active_owner['permit_id'] if active_owner else '（空）'}")
        prereq = json.loads(wc["prereq_ids"] or "[]")
        blockers = [x for x in prereq if not _one(conn,
            "SELECT 1 FROM wave_containers WHERE wave_id=? AND container_id=? "
            "AND executed=1", (wave_id, x))]
        if blockers:
            raise PermitError(409, "PREREQ_BLOCKED",
                              f"箱 {cid} 的前置箱尚未装船",
                              {"container_id": cid, "blocked_by": blockers})
        scanned_at = data.get("scanned_at") or now
        parse_ts(scanned_at, "scanned_at")
        scan_id = new_id("scn")
        conn.execute(
            "INSERT INTO loading_scans (id, wave_id, permit_id, permit_rev, "
            "container_id, slot_code, scanner_id, scanned_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (scan_id, wave_id, permit_no, revision, cid, slot, scanner,
             scanned_at, now))
        if os.environ.get("PERMIT_CRASH_SCAN_SPLIT"):
            # C4 专用：只让装船记录落库（进度/箱归属尚未推进）即掉电，
            # 制造“装船记录已存在但波次仍未开始”的撕裂，交给 reconcile 修复。
            conn.commit()
            conn.close()
            crash_point("C4")
            return {"outcome": "loaded", "scan_id": scan_id}, 200
        conn.execute(
            "UPDATE wave_containers SET executed=1, executed_at=?, scan_id=? "
            "WHERE wave_id=? AND container_id=?", (now, scan_id, wave_id, cid))
        conn.execute(
            "UPDATE permit_containers SET state='executed' WHERE permit_id=? "
            "AND wave_id=? AND container_id=?", (permit_no, wave_id, cid))
        total = conn.execute(
            "SELECT COUNT(*) c FROM wave_containers WHERE wave_id=?",
            (wave_id,)).fetchone()["c"]
        done = conn.execute(
            "SELECT COUNT(*) c FROM wave_containers WHERE wave_id=? AND executed=1",
            (wave_id,)).fetchone()["c"]
        status = "completed" if done == total else "in_progress"
        conn.execute(
            "UPDATE waves SET executed=?, status=?, started_at=COALESCE(started_at,?), "
            "completed_at=? WHERE id=?",
            (done, status, now, now if status == "completed" else None, wave_id))
        log_event(conn, scanner, "scan.loaded",
                  {"scan_id": scan_id, "wave_id": wave_id,
                   "permit_id": permit_no, "revision": revision,
                   "container_id": cid, "progress": f"{done}/{total}"})
        crash_point("C3")  # 登记扫描结果事务提交前掉电 -> 整体回滚
        body = {"outcome": "loaded", "scan_id": scan_id,
                "progress": {"executed": done, "total": total, "wave_status": status}}
        _idem_finish(conn, idem_key, 200, body)
        conn.commit()
        return body, 200
    except PermitError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            conn.close()
        except sqlite3.ProgrammingError:
            pass


def list_scans(wave_id=None):
    conn = connect(_db_path())
    try:
        sql = "SELECT * FROM loading_scans"
        args = ()
        if wave_id:
            sql += " WHERE wave_id=?"
            args = (wave_id,)
        sql += " ORDER BY created_at"
        return {"scans": _rows(conn, sql, args)}
    finally:
        conn.close()


# ---------------------------------------------------------------- 授权异常处置

def list_exceptions(status=None):
    conn = connect(_db_path())
    try:
        sql = "SELECT * FROM auth_exceptions"
        args = ()
        if status:
            sql += " WHERE status=?"
            args = (status,)
        sql += " ORDER BY created_at"
        return {"exceptions": _rows(conn, sql, args)}
    finally:
        conn.close()


def _move_single_box(conn, wave_id, cid, target_id, now):
    """把单个未执行箱的有效归属迁到目标许可（异常转交）。"""
    tgt = _load_active_permit(conn, target_id)
    if tgt["wave_id"] != wave_id:
        raise PermitError(409, "WAVE_MISMATCH", "目标许可属于其他波次")
    if tgt["status"] != "active":
        raise PermitError(409, "TARGET_NOT_ACTIVE",
                          f"目标许可状态为 {tgt['status']}")
    wc = _one(conn, "SELECT hatch_code, executed FROM wave_containers "
                    "WHERE wave_id=? AND container_id=?", (wave_id, cid))
    if not wc:
        raise PermitError(409, "BOX_NOT_IN_WAVE", f"箱 {cid} 不属于该波次")
    if wc["executed"]:
        raise PermitError(409, "BOX_EXECUTED", f"箱 {cid} 已装船，不能转交")
    tgt_hatches = {r["hatch_code"] for r in conn.execute(
        "SELECT hatch_code FROM permit_hatches WHERE permit_id=?", (target_id,))}
    if wc["hatch_code"] not in tgt_hatches:
        raise PermitError(409, "TRANSFER_HATCH_MISMATCH",
                          f"目标许可不覆盖舱区 {wc['hatch_code']}")
    conn.execute(
        "DELETE FROM permit_containers WHERE wave_id=? AND container_id=? "
        "AND state='active'", (wave_id, cid))
    conn.execute(
        "INSERT INTO permit_containers (permit_id, wave_id, container_id, state) "
        "VALUES (?,?,?,'active') ON CONFLICT(permit_id, wave_id, container_id) "
        "DO UPDATE SET state='active'", (target_id, wave_id, cid))
    conn.execute(
        "UPDATE wave_containers SET owner_permit=? WHERE wave_id=? AND container_id=?",
        (target_id, wave_id, cid))
    conn.execute("UPDATE permits SET revision=revision+1, updated_at=? WHERE id=?",
                 (now, target_id))
    return tgt


def resolve_exception(exc_id, data, actor="dispatcher", idem_key=None, now=None):
    """调度员处置授权异常：transfer | restore | void（幂等）。"""
    now = now or now_iso()
    action = data.get("action")
    if action not in ("transfer", "restore", "void"):
        raise PermitError(400, "BAD_ACTION",
                          "action 必须是 transfer / restore / void")
    conn = connect(_db_path())
    conn.execute("BEGIN IMMEDIATE")
    try:
        replay = _idem_begin(conn, idem_key, "exception",
                             {"exc_id": exc_id, "action": action,
                              "target": data.get("target_permit_id"),
                              "valid_to": data.get("valid_to")})
        if replay:
            conn.commit()
            return replay["body"], replay["status"]
        exc = _one(conn, "SELECT * FROM auth_exceptions WHERE id=?", (exc_id,))
        if not exc:
            raise PermitError(404, "EXCEPTION_NOT_FOUND",
                              f"授权异常不存在: {exc_id}")
        if exc["status"] != "open":
            raise PermitError(409, "EXCEPTION_CLOSED",
                              f"异常已处置为 {exc['status']}")
        resolved_permit = None
        if action == "transfer":
            target_id = data.get("target_permit_id")
            if not target_id:
                raise PermitError(400, "NO_TARGET", "转交必须指定 target_permit_id")
            if not exc["wave_id"]:
                raise PermitError(409, "NO_WAVE_CONTEXT",
                                  "未知许可的扫描缺少波次上下文，不能转交")
            _move_single_box(conn, exc["wave_id"], exc["container_id"],
                             target_id, now)
            resolved_permit = target_id
        elif action == "restore":
            pid = exc["permit_id"]
            p = _one(conn, "SELECT * FROM permits WHERE id=?", (pid,))
            if not p:
                raise PermitError(409, "PERMIT_GONE",
                                  "原许可已不存在，只能转交或作废")
            if p["status"] == "revoked":
                raise PermitError(409, "PERMIT_REVOKED",
                                  "已撤销许可不能恢复，请转交或作废")
            if p["status"] == "suspended":
                conn.execute(
                    "UPDATE permits SET status='active', revision=revision+1, "
                    "updated_at=?, suspend_reason=NULL WHERE id=?", (now, pid))
            elif exc["reason"] == "PERMIT_EXPIRED":
                new_to = data.get("valid_to")
                if not new_to:
                    raise PermitError(400, "VALID_TO_REQUIRED",
                                      "恢复过期许可需给出新的 valid_to")
                vt = parse_ts(new_to, "valid_to")
                if vt <= parse_ts(now):
                    raise PermitError(400, "BAD_WINDOW", "新到期时间仍在过去")
                conn.execute(
                    "UPDATE permits SET valid_to=?, revision=revision+1, "
                    "updated_at=? WHERE id=?", (new_to, now, pid))
            # STALE_REVISION / OUT_OF_HATCH / NOT_IN_WAVE：无需改许可，
            # 关闭异常并把最新修订号交回手持端重新扫描
            p = _one(conn, "SELECT * FROM permits WHERE id=?", (pid,))
            resolved_permit = pid
        else:  # void
            # 作废本次扫描：释放（若有）该箱在异常引用许可上的未执行归属
            if exc["wave_id"]:
                conn.execute(
                    "UPDATE permit_containers SET state='released' WHERE wave_id=? "
                    "AND container_id=? AND state='active' AND permit_id=?",
                    (exc["wave_id"], exc["container_id"], exc["permit_id"] or ""))
                conn.execute(
                    "UPDATE wave_containers SET owner_permit=NULL WHERE wave_id=? "
                    "AND container_id=?",
                    (exc["wave_id"], exc["container_id"]))
        conn.execute(
            "UPDATE auth_exceptions SET status=?, resolved_permit_id=?, "
            "resolved_by=?, resolved_at=? WHERE id=?",
            ({"transfer": "transferred", "restore": "restored",
              "void": "voided"}[action],
             resolved_permit, actor, now, exc_id))
        log_event(conn, actor, "exception.resolve",
                  {"exception_id": exc_id, "action": action,
                   "resolved_permit": resolved_permit})
        body = {"exception_id": exc_id, "outcome": action,
                "resolved_permit_id": resolved_permit}
        if action == "restore" and resolved_permit:
            body["latest_revision"] = _one(conn,
                "SELECT revision FROM permits WHERE id=?",
                (resolved_permit,))["revision"]
        _idem_finish(conn, idem_key, 200, body)
        conn.commit()
        return body, 200
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------- 调度页视图

def schedule(now=None):
    now = now or now_iso()
    conn = connect(_db_path())
    try:
        permits = []
        for r in _rows(conn,
                "SELECT p.*, w.name wave_name FROM permits p "
                "JOIN waves w ON w.id=p.wave_id ORDER BY p.valid_from"):
            r["hatches"] = [x["hatch_code"] for x in conn.execute(
                "SELECT hatch_code FROM permit_hatches WHERE permit_id=? "
                "ORDER BY hatch_code", (r["id"],))]
            r["active_boxes"] = conn.execute(
                "SELECT COUNT(*) c FROM permit_containers WHERE permit_id=? "
                "AND state='active'", (r["id"],)).fetchone()["c"]
            r["executed_boxes"] = conn.execute(
                "SELECT COUNT(*) c FROM permit_containers WHERE permit_id=? "
                "AND state='executed'", (r["id"],)).fetchone()["c"]
            cur = parse_ts(now)
            r["time_state"] = (
                "future" if cur < parse_ts(r["valid_from"])
                else "expired" if cur >= parse_ts(r["valid_to"])
                else "current")
            permits.append(r)
        # 检测现存的舱区冲突（任何时刻窗口重叠 + 相邻/同舱 + 不同岸桥）
        rel = {(x["hatch_a"], x["hatch_b"]) for x in _rows(
            conn, "SELECT hatch_a, hatch_b FROM hatch_conflicts")}
        occupancy, conflicts = [], []
        for p in permits:
            for h in p["hatches"]:
                occupancy.append({"hatch": h, "permit_id": p["id"],
                                  "crane_id": p["crane_id"], "wave_id": p["wave_id"],
                                  "status": p["status"],
                                  "valid_from": p["valid_from"],
                                  "valid_to": p["valid_to"]})
        active_like = [p for p in permits if p["status"] in ("active", "suspended")]
        for i, a in enumerate(active_like):
            for b in active_like[i + 1:]:
                if a["crane_id"] == b["crane_id"]:
                    continue
                if not _windows_overlap(parse_ts(a["valid_from"]),
                                        parse_ts(a["valid_to"]),
                                        parse_ts(b["valid_from"]),
                                        parse_ts(b["valid_to"])):
                    continue
                for ha in a["hatches"]:
                    for hb in b["hatches"]:
                        if ha == hb or (ha, hb) in rel:
                            conflicts.append({
                                "hatch_a": ha, "hatch_b": hb,
                                "permit_a": a["id"], "permit_b": b["id"],
                                "crane_a": a["crane_id"], "crane_b": b["crane_id"],
                                "relation": "same" if ha == hb else "adjacent"})
        return {"now": now, "permits": permits, "occupancy": occupancy,
                "conflicts": conflicts}
    finally:
        conn.close()


def board(now=None):
    now = now or now_iso()
    conn = connect(_db_path())
    try:
        plans = _rows(conn, "SELECT * FROM plans ORDER BY created_at")
        waves = [wave_detail_conn(conn, w["id"])
                 for w in _rows(conn, "SELECT id FROM waves ORDER BY created_at")]
        exceptions = _rows(conn,
            "SELECT * FROM auth_exceptions ORDER BY created_at DESC LIMIT 100")
        scans = _rows(conn,
            "SELECT * FROM loading_scans ORDER BY created_at DESC LIMIT 100")
        return {"now": now, "plans": plans, "waves": waves,
                "exceptions": exceptions, "scans": scans,
                "schedule": schedule(now)}
    finally:
        conn.close()


# ---------------------------------------------------------------- 对账与自检

def reconcile(path=None, now=None):
    """启动恢复：以装船记录为事实源，修复进度/状态/归属三类撕裂。"""
    now = now or now_iso()
    conn = connect(path or _db_path())
    actions = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        scans = _rows(conn, "SELECT * FROM loading_scans")
        for s in scans:
            wc = _one(conn, "SELECT * FROM wave_containers WHERE wave_id=? "
                            "AND container_id=?", (s["wave_id"], s["container_id"]))
            if not wc:
                # 装船记录没有对应波次行：理论上不可能，保留记录并报告
                actions.append({"type": "orphan_scan", "scan_id": s["id"]})
                continue
            if not wc["executed"]:
                conn.execute(
                    "UPDATE wave_containers SET executed=1, executed_at=COALESCE("
                    "executed_at,?), scan_id=COALESCE(scan_id,?) WHERE wave_id=? "
                    "AND container_id=?",
                    (s["created_at"], s["id"], s["wave_id"], s["container_id"]))
                actions.append({"type": "mark_executed",
                                "container_id": s["container_id"]})
            # 许可侧归属行必须是 executed
            pc = _one(conn,
                "SELECT state FROM permit_containers WHERE permit_id=? AND "
                "wave_id=? AND container_id=?",
                (s["permit_id"], s["wave_id"], s["container_id"]))
            if pc is None:
                conn.execute(
                    "INSERT INTO permit_containers (permit_id, wave_id, "
                    "container_id, state) VALUES (?,?,?,'executed')",
                    (s["permit_id"], s["wave_id"], s["container_id"]))
                actions.append({"type": "restore_executed_owner",
                                "container_id": s["container_id"],
                                "permit_id": s["permit_id"]})
            elif pc["state"] != "executed":
                conn.execute(
                    "UPDATE permit_containers SET state='executed' WHERE "
                    "permit_id=? AND wave_id=? AND container_id=?",
                    (s["permit_id"], s["wave_id"], s["container_id"]))
                actions.append({"type": "fix_owner_state",
                                "container_id": s["container_id"]})
        for w in _rows(conn, "SELECT id FROM waves"):
            total = conn.execute(
                "SELECT COUNT(*) c FROM wave_containers WHERE wave_id=?",
                (w["id"],)).fetchone()["c"]
            done = conn.execute(
                "SELECT COUNT(*) c FROM wave_containers WHERE wave_id=? "
                "AND executed=1", (w["id"],)).fetchone()["c"]
            first = conn.execute(
                "SELECT MIN(scanned_at) m FROM loading_scans WHERE wave_id=?",
                (w["id"],)).fetchone()["m"]
            last = conn.execute(
                "SELECT MAX(scanned_at) m FROM loading_scans WHERE wave_id=?",
                (w["id"],)).fetchone()["m"]
            status = "completed" if done == total and total else (
                "in_progress" if done else "released")
            cur = _one(conn, "SELECT * FROM waves WHERE id=?", (w["id"],))
            if (cur["total"], cur["executed"], cur["status"],
                    cur["started_at"], cur["completed_at"]) != (
                    total, done, status,
                    first if done else None,
                    last if status == "completed" else None):
                conn.execute(
                    "UPDATE waves SET total=?, executed=?, status=?, "
                    "started_at=?, completed_at=? WHERE id=?",
                    (total, done, status,
                     first if done else None,
                     last if status == "completed" else None, w["id"]))
                actions.append({"type": "wave_progress_fixed", "wave_id": w["id"],
                                "executed": done, "total": total, "status": status})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"actions": actions, "verify": verify(path)}


def verify(path=None):
    """不变量自检：箱唯一归属 / 舱区不重叠 / 装船记录与波次进度一致。"""
    conn = connect(path or _db_path())
    violations = []
    try:
        # 1) 同一未执行箱同时属于两张有效许可（索引已兜底，再显式查一次）
        for r in _rows(conn,
                "SELECT wave_id, container_id, COUNT(*) c FROM permit_containers "
                "WHERE state='active' GROUP BY wave_id, container_id HAVING c>1"):
            violations.append({"code": "DUPLICATE_ACTIVE_OWNER", **r})
        # 2) 相邻/同舱在重叠时段授予不同岸桥
        rel = {(x["hatch_a"], x["hatch_b"]) for x in _rows(
            conn, "SELECT hatch_a, hatch_b FROM hatch_conflicts")}
        grants = _rows(conn,
            "SELECT p.id permit_id, p.crane_id, p.status, p.valid_from, p.valid_to, "
            "ph.hatch_code FROM permits p JOIN permit_hatches ph "
            "ON ph.permit_id=p.id WHERE p.status IN ('active','suspended')")
        for i, a in enumerate(grants):
            for b in grants[i + 1:]:
                if a["crane_id"] == b["crane_id"]:
                    continue
                if not _windows_overlap(parse_ts(a["valid_from"]),
                                        parse_ts(a["valid_to"]),
                                        parse_ts(b["valid_from"]),
                                        parse_ts(b["valid_to"])):
                    continue
                if a["hatch_code"] == b["hatch_code"] or \
                        (a["hatch_code"], b["hatch_code"]) in rel:
                    violations.append({
                        "code": "OVERLAPPING_HATCH_GRANT",
                        "permit_a": a["permit_id"], "permit_b": b["permit_id"],
                        "hatch_a": a["hatch_code"], "hatch_b": b["hatch_code"]})
        # 3) 装船记录存在但波次仍显示未开始 / 进度不一致
        for w in _rows(conn, "SELECT id, status, executed, started_at FROM waves"):
            done = conn.execute(
                "SELECT COUNT(*) c FROM loading_scans WHERE wave_id=?",
                (w["id"],)).fetchone()["c"]
            if done and (w["status"] == "released" or not w["started_at"]):
                violations.append({"code": "WAVE_NOT_STARTED_WITH_SCANS",
                                   "wave_id": w["id"], "scans": done})
            if done != w["executed"]:
                violations.append({"code": "WAVE_PROGRESS_MISMATCH",
                                   "wave_id": w["id"], "scans": done,
                                   "executed": w["executed"]})
        # 4) 装船记录与 executed 标记一致
        for r in _rows(conn,
                "SELECT s.id scan_id, s.container_id, wc.executed FROM loading_scans s "
                "LEFT JOIN wave_containers wc ON wc.wave_id=s.wave_id "
                "AND wc.container_id=s.container_id WHERE COALESCE(wc.executed,0)=0"):
            violations.append({"code": "SCAN_WITHOUT_EXECUTED_MARK", **r})
        return {"ok": not violations, "violations": violations}
    finally:
        conn.close()
