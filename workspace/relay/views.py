"""操作台只读视图：待传 / 断层 / 隔离 / 已应用，以及扫描到当前航次的映射。"""
from __future__ import annotations

import json

from . import loading
from .store import (APPLIED, IGNORED, PENDING, PK_QUARANTINE, PK_STAGED,
                    connect)


def sessions_overview(db_path: str) -> list[dict]:
    conn = connect(db_path)
    try:
        out = []
        for s in conn.execute("SELECT * FROM sessions ORDER BY session"):
            staged = conn.execute(
                "SELECT seq,message FROM raw_packets WHERE session=? "
                "AND status=? ORDER BY seq",
                (s["session"], PK_STAGED)).fetchall()
            quar = conn.execute(
                "SELECT seq,sha256,error_code,message FROM raw_packets "
                "WHERE session=? AND status=? ORDER BY seq",
                (s["session"], PK_QUARANTINE)).fetchall()
            pending = conn.execute(
                "SELECT COUNT(*) c FROM scan_records WHERE session=? "
                "AND status=?", (s["session"], PENDING)).fetchone()["c"]
            applied = conn.execute(
                "SELECT COUNT(*) c FROM scan_records WHERE session=? "
                "AND status=?", (s["session"], APPLIED)).fetchone()["c"]
            out.append({
                "session": s["session"], "device_id": s["device_id"],
                "cursor": s["cursor"],
                "next_expected": s["cursor"] + 1,
                "staged": [dict(x) for x in staged],
                "quarantine": [dict(x) for x in quar],
                "pending_count": pending, "applied_count": applied,
            })
        return out
    finally:
        conn.close()


def pending(db_path: str, voyage_id=None) -> list[dict]:
    """待处理：争用/计划更替/别处已确认，等操作员处置。"""
    conn = connect(db_path)
    try:
        sql = ("SELECT r.*, d.request_id AS disposition_request "
               "FROM scan_records r LEFT JOIN dispositions d "
               "ON d.record_id=r.record_id AND d.action='ignore' "
               "WHERE r.status=?")
        args = [PENDING]
        if voyage_id:
            sql += " AND r.voyage_id=?"
            args.append(voyage_id)
        sql += " ORDER BY r.rowid"
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def gaps(db_path: str) -> list[dict]:
    """断层：已暂存但游标不能越过的包，以及缺失序号清单。"""
    out = []
    for s in sessions_overview(db_path):
        if s["staged"]:
            out.append({"session": s["session"], "cursor": s["cursor"],
                        "missing": list(range(s["cursor"] + 1,
                                              s["staged"][0]["seq"])),
                        "staged": s["staged"]})
    return out


def quarantine(db_path: str) -> list[dict]:
    conn = connect(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM raw_packets WHERE status=? ORDER BY session,seq",
            (PK_QUARANTINE,))]
    finally:
        conn.close()


def applied(db_path: str, voyage_id=None) -> list[dict]:
    """已应用（装船确认成功），附被忽略记录数提示。"""
    conn = connect(db_path)
    try:
        sql = "SELECT * FROM scan_records WHERE status=?"
        args = [APPLIED]
        if voyage_id:
            sql += " AND voyage_id=?"
            args.append(voyage_id)
        sql += " ORDER BY applied_at,rowid"
        rows = [dict(r) for r in conn.execute(sql, args)]
        ignored = conn.execute(
            "SELECT COUNT(*) c FROM scan_records WHERE status=?"
            + (" AND voyage_id=?" if voyage_id else ""),
            [IGNORED] + ([voyage_id] if voyage_id else [])).fetchone()["c"]
        return rows, ignored
    finally:
        conn.close()


def voyage_mapping(db_path: str, stowage_db: str, voyage_id: str) -> dict:
    """扫描记录如何映射到*当前*航次：当前方案槽位 vs 上报槽位 vs 最终态。"""
    conn = connect(db_path)
    biz = connect(stowage_db)
    try:
        plan = loading.published_plan(biz, voyage_id)
        plan_info = None
        plan_map = {}
        if plan:
            plan_info = {"plan_id": plan["id"], "version": plan["version"]}
            pe = biz.execute(
                "SELECT pe.container_id, s.code FROM plan_entries pe "
                "JOIN slots s ON s.id=pe.slot_id WHERE pe.plan_id=?",
                (plan["id"],)).fetchall()
            plan_map = {r["container_id"]: r["code"] for r in pe}

        rows = conn.execute(
            "SELECT * FROM scan_records WHERE voyage_id=? ORDER BY rowid",
            (voyage_id,)).fetchall()
        records = []
        for r in rows:
            expected = plan_map.get(r["container"])
            records.append({
                "record_id": r["record_id"], "device_id": r["device_id"],
                "session": r["session"], "seq": r["seq"],
                "container": r["container"],
                "scanned_slot": r["slot"],
                "current_plan_slot": expected,
                "slot_matches_plan": expected == r["slot"] if expected else False,
                "status": r["status"], "reason": r["reason"],
                "resolved_slot": r["resolved_slot"],
                "confirmation_id": r["confirmation_id"],
                "collected_at": r["collected_at"], "applied_at": r["applied_at"],
            })
        return {"voyage_id": voyage_id, "current_plan": plan_info,
                "records": records}
    finally:
        conn.close()
        biz.close()


def rejected(db_path: str) -> list[dict]:
    conn = connect(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT session,seq,sha256,error_code,message,received_at "
            "FROM raw_packets WHERE status='rejected' ORDER BY id")]
    finally:
        conn.close()


def events(db_path: str, session=None, limit=200) -> list[dict]:
    conn = connect(db_path)
    try:
        sql = "SELECT * FROM relay_events"
        args = []
        if session:
            sql += " WHERE session=?"
            args.append(session)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        out = []
        for e in conn.execute(sql, args):
            d = dict(e)
            try:
                d["detail"] = json.loads(d["detail"]) if d["detail"] else None
            except ValueError:
                pass
            out.append(d)
        return out
    finally:
        conn.close()


def verify_invariants(db_path: str, stowage_db: str) -> dict:
    """一致性自检：收件状态 / 装船确认 / 游标不得互相矛盾。"""
    conn = connect(db_path)
    biz = connect(stowage_db)
    violations = []
    try:
        # 1. 游标 = 已连续接受的最大序号；游标之后不得有 accepted 断档
        for s in conn.execute("SELECT * FROM sessions"):
            cursor = s["cursor"]
            for seq in range(1, cursor + 1):
                n = conn.execute(
                    "SELECT COUNT(*) c FROM raw_packets WHERE session=? "
                    "AND seq=? AND status='accepted'",
                    (s["session"], seq)).fetchone()["c"]
                if n != 1:
                    violations.append(f"{s['session']}: 游标={cursor} 但序号 "
                                      f"{seq} 没有恰好一个 accepted 包 ({n})")
            # 2. 游标内每个 accepted 包的明细不得残留 received
            stuck = conn.execute(
                "SELECT COUNT(*) c FROM scan_records WHERE session=? "
                "AND seq<=? AND status='received'",
                (s["session"], cursor)).fetchone()["c"]
            if stuck:
                violations.append(f"{s['session']}: {stuck} 条游标内记录仍为"
                                  " received（业务入口未补偿完）")
            # 3. 游标内包不得有 pending 状态？——允许：pending 是已应用包内
            #    争用结果，不阻挡游标。只检查明细完整性。
            miss = conn.execute(
                "SELECT rp.seq FROM raw_packets rp WHERE rp.session=? "
                "AND rp.status='accepted' AND rp.seq<=? AND NOT EXISTS "
                "(SELECT 1 FROM scan_records sr WHERE sr.session=rp.session "
                "AND sr.seq=rp.seq LIMIT 1)",
                (s["session"], cursor)).fetchall()
            for m in miss:
                violations.append(f"{s['session']}: accepted 包 #{m['seq']} "
                                  "缺少扫描明细")

        # 4. applied 记录必须能在现有装船确认表中找到确认
        for r in conn.execute(
                "SELECT * FROM scan_records WHERE status='applied'"):
            cnf = biz.execute(
                "SELECT f.id FROM confirmations f JOIN containers c "
                "ON c.id=f.container_id WHERE f.voyage_id=? AND c.id=? ",
                (r["voyage_id"], r["container"])).fetchone()
            if not cnf:
                violations.append(f"记录 {r['record_id']} 标记 applied 但"
                                  "现有装船确认表中无确认")
            elif r["confirmation_id"] and cnf["id"] != r["confirmation_id"]:
                violations.append(
                    f"记录 {r['record_id']} 的 confirmation_id 与业务表不一致")

        # 5. 同一航次同一箱最多一项 applied（其余应 pending/ignored）
        for row in conn.execute(
                "SELECT voyage_id,container,COUNT(*) c FROM scan_records "
                "WHERE status='applied' GROUP BY voyage_id,container HAVING c>1"):
            violations.append(f"{row['voyage_id']} {row['container']} 有 "
                              f"{row['c']} 项 applied（争用规则被破坏）")
        return {"ok": not violations, "violations": violations}
    finally:
        conn.close()
        biz.close()
