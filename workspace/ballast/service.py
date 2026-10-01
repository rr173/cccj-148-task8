"""压载业务服务层：拓扑、方案修订、执行状态机、幂等令牌、抢占、账本与恢复。

事务与并发
----------
* 每个多步写都在单个 BEGIN IMMEDIATE 事务里完成；令牌结果与状态跃迁
  同事务提交 —— 进程在响应写出前崩溃，客户端凭同一令牌重试只会拿到
  首次结果的回放，不会重复下发。
* 泵组占用靠部分唯一索引 uq_ballast_seizure_active 保证：两名值班员
  同时抢占同一泵组，数据库只放行一个，另一个收到 409 PUMP_BUSY。
* 方案修订：装船变化/液位漂移使剩余步骤不再安全时，只冻结未开始
  （pending/paused）的步骤并派生新修订；已确认步骤与其转移量原样保留，
  新修订以当前实际液位为基线，水量守恒自然延续。

崩溃恢复
--------
所有状态都在库表里（账本 + 检查点 + 业务表），recover() 重启后据此
恢复唯一状态：运行中步骤不重发、已转移水量不丢、一台泵不会被两步共占。
测试用 simulate_crash_at() 在指定跃迁提交后注入 SimulatedCrash 模拟
进程终止；服务器可用环境变量 BALLAST_CRASH=<point> 真实 os._exit。
"""
import hashlib
import json
import os
import threading
from datetime import datetime, timezone

from stowage.db import ServiceError
from stowage.domain import new_id

from . import planner, stability
from .db import connect
from .domain import parse_topology, topo_to_dict

# --------------------------------------------------------------------------- #
# 崩溃注入（测试 / 演示）
# --------------------------------------------------------------------------- #

CRASH_POINTS = ("after_seize", "after_dispatch", "after_partial_flow",
                "after_confirm")
_armed = set()
_armed_lock = threading.Lock()


class SimulatedCrash(Exception):
    """模拟进程在提交后、响应前终止。状态已落库，可安全 recover。"""


def simulate_crash_at(point):
    assert point in CRASH_POINTS, point
    with _armed_lock:
        _armed.add(point)


def clear_crash_points():
    with _armed_lock:
        _armed.clear()


def _maybe_crash(point):
    fire = False
    with _armed_lock:
        if point in _armed:
            _armed.discard(point)
            fire = True
    if fire or os.environ.get("BALLAST_CRASH") == point:
        if os.environ.get("BALLAST_CRASH") == point:
            os._exit(91)  # 真实进程终止（服务器演示用）
        raise SimulatedCrash(f"simulated crash at {point}")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# 基础读取
# --------------------------------------------------------------------------- #

def _row(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()


def _rows(conn, sql, args=()):
    return conn.execute(sql, args).fetchall()


def _get_voyage(conn, vid):
    v = _row(conn, "SELECT * FROM voyages WHERE id=?", (vid,))
    if not v:
        raise ServiceError(404, "VOYAGE_NOT_FOUND", f"voyage {vid} not found")
    return v


def _get_config(conn, vid):
    r = _row(conn, "SELECT * FROM ballast_config WHERE voyage_id=?", (vid,))
    cfg = dict(stability.DEFAULT_CONFIG)
    if r:
        for k in cfg:
            cfg[k] = r[k]
    return cfg


def _ship(conn, vid):
    v = _get_voyage(conn, vid)
    r = _row(conn, "SELECT MAX(x) AS mx FROM stacks WHERE voyage_id=?", (vid,))
    ship_len = (r["mx"] or 100.0) + stability.BAY_FALLBACK
    return stability.Ship(v["lightship_weight"], v["lightship_lcg"],
                          v["lightship_vcg"], ship_len, v["min_gm"],
                          v["max_gm"], v["max_trim"], _get_config(conn, vid))


def _load_topology(conn, vid):
    from .domain import Pipe, Pump, Tank, Topology, Valve
    topo = Topology(voyage_id=vid)
    for r in _rows(conn, "SELECT * FROM ballast_tanks WHERE voyage_id=?", (vid,)):
        topo.tanks[r["name"]] = Tank(
            id=r["name"], name=r["name"], capacity=r["capacity"],
            min_level=r["min_level"], max_level=r["max_level"],
            level=r["level"], x=r["x"], y=r["y"], z=r["z"],
            density=r["density"])
    for r in _rows(conn, "SELECT * FROM ballast_pumps WHERE voyage_id=?", (vid,)):
        topo.pumps[r["name"]] = Pump(id=r["name"], name=r["name"],
                                     rate=r["rate"])
    for r in _rows(conn, "SELECT * FROM ballast_valves WHERE voyage_id=?",
                   (vid,)):
        topo.valves[r["name"]] = Valve(id=r["name"], name=r["name"])
    for r in _rows(conn, "SELECT * FROM ballast_pipes WHERE voyage_id=?", (vid,)):
        topo.pipes[r["name"]] = Pipe(id=r["name"], name=r["name"],
                                     a_node=r["a_node"], b_node=r["b_node"],
                                     mutex_group=r["mutex_group"])
    return topo


def _require_topology(conn, vid):
    topo = _load_topology(conn, vid)
    if not topo.tanks:
        raise ServiceError(409, "NO_TOPOLOGY",
                           "ballast topology not set; POST topology first")
    return topo


def _levels(conn, vid):
    return {r["name"]: r["level"] for r in _rows(
        conn, "SELECT name, level FROM ballast_tanks WHERE voyage_id=?", (vid,))}


def _cargo_now(conn, vid):
    """已确认装船（在船）的货载：(weight, x, y, z)。"""
    return [(r["weight"], r["x"], r["y"], r["z"]) for r in _rows(
        conn, "SELECT c.weight, s.x, s.y, sl.z FROM confirmations f "
              "JOIN containers c ON c.id=f.container_id "
              "JOIN slots sl ON sl.id=f.slot_id "
              "JOIN stacks s ON s.id=sl.stack_id "
              "WHERE f.voyage_id=? AND c.locked=1 AND c.discharged=0", (vid,))]


def _confirmed_weight(conn, vid):
    r = _row(conn, "SELECT COALESCE(SUM(c.weight),0) AS w FROM confirmations f "
                   "JOIN containers c ON c.id=f.container_id "
                   "WHERE f.voyage_id=? AND c.locked=1 AND c.discharged=0",
             (vid,))
    return r["w"]


def _latest_published(conn, vid):
    return _row(conn, "SELECT * FROM plans WHERE voyage_id=? AND "
                      "status='published' ORDER BY version DESC LIMIT 1", (vid,))


def _cargo_remaining(conn, vid):
    """已发布方案中尚未确认装船的货载（随晚到箱重/甩箱实时变化）。"""
    p = _latest_published(conn, vid)
    if not p:
        return [], None
    items = [dict(id=r["id"], weight=r["weight"], x=r["x"], y=r["y"], z=r["z"])
             for r in _rows(
                 conn, "SELECT c.id, c.weight, s.x, s.y, sl.z FROM plan_entries pe "
                       "JOIN containers c ON c.id=pe.container_id "
                       "JOIN slots sl ON sl.id=pe.slot_id "
                       "JOIN stacks s ON s.id=sl.stack_id "
                       "WHERE pe.plan_id=? AND c.arrived=1 AND c.discharged=0 "
                       "AND c.locked=0", (p["id"],))]
    return items, p["id"]


def _active_revision(conn, vid):
    return _row(conn, "SELECT * FROM ballast_plans WHERE voyage_id=? AND "
                      "status='active' ORDER BY revision DESC LIMIT 1", (vid,))


def _steps_of(conn, plan_id):
    return _rows(conn, "SELECT * FROM ballast_steps WHERE plan_id=? "
                       "ORDER BY seq", (plan_id,))


def _ledger(conn, vid, actor, action, detail=None):
    conn.execute("INSERT INTO ballast_ledger (voyage_id, actor, action, "
                 "detail, created_at) VALUES (?,?,?,?,?)",
                 (vid, actor, action, json.dumps(detail or {},
                                                 ensure_ascii=False), now()))


def _alarm(conn, vid, kind, message, detail=None):
    aid = new_id("alm")
    conn.execute("INSERT INTO ballast_alarms (id, voyage_id, kind, message, "
                 "detail, status, created_at) VALUES (?,?,?,?,?,'open',?)",
                 (aid, vid, kind, message,
                  json.dumps(detail or {}, ensure_ascii=False), now()))
    return aid


def _exec_state(conn, vid):
    plan = _active_revision(conn, vid)
    steps = []
    if plan:
        steps = [{"seq": s["seq"], "status": s["status"],
                  "transferred": s["transferred"],
                  "src_start": s["src_start"], "dst_start": s["dst_start"]}
                 for s in _steps_of(conn, plan["id"])]
    return {
        "revision": plan["revision"] if plan else None,
        "steps": steps,
        "levels": _levels(conn, vid),
        "seizures": [{"pump": r["pump_id"], "officer": r["officer"],
                      "step": r["step_id"]}
                     for r in _rows(conn, "SELECT * FROM ballast_seizures "
                                          "WHERE voyage_id=? AND released=0",
                                    (vid,))],
        "open_alarms": _row(conn, "SELECT COUNT(*) AS n FROM ballast_alarms "
                                  "WHERE voyage_id=? AND status='open'",
                            (vid,))["n"],
    }


def _checkpoint(conn, vid):
    conn.execute("INSERT INTO ballast_checkpoints (voyage_id, state, "
                 "created_at) VALUES (?,?,?)",
                 (vid, json.dumps(_exec_state(conn, vid), ensure_ascii=False),
                  now()))


# --------------------------------------------------------------------------- #
# 幂等令牌
# --------------------------------------------------------------------------- #

def _canonical(payload):
    return json.dumps(payload or {}, sort_keys=True, ensure_ascii=False)


def _payload_hash(payload):
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _token_replay(conn, token, action, payload):
    """命中已有令牌时：载荷一致 → 回放首次结果；载荷变化 → 拒绝。"""
    t = _row(conn, "SELECT * FROM ballast_tokens WHERE token=?", (token,))
    if not t:
        return None
    if t["action"] != action or t["payload_hash"] != _payload_hash(payload):
        raise ServiceError(409, "TOKEN_PAYLOAD_MISMATCH",
                           "执行令牌已被不同载荷使用，已拒绝",
                           {"first_action": t["action"],
                            "first_payload": json.loads(t["payload"])})
    result = json.loads(t["result"])
    result["idempotent"] = True
    return result


def _token_record(conn, vid, token, action, payload, actor, result):
    conn.execute("INSERT INTO ballast_tokens (token, voyage_id, action, "
                 "payload_hash, payload, result, actor, created_at) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 (token, vid, action, _payload_hash(payload),
                  _canonical(payload), json.dumps(result, ensure_ascii=False),
                  actor, now()))


# --------------------------------------------------------------------------- #
# 拓扑
# --------------------------------------------------------------------------- #

def save_topology(vid, payload, actor="chief", db_path=None):
    """大副录入/整体替换压载拓扑。执行中（有 running/paused 步骤）禁止。"""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _get_voyage(conn, vid)
        busy = _row(conn, "SELECT COUNT(*) AS n FROM ballast_steps s "
                          "JOIN ballast_plans p ON p.id=s.plan_id "
                          "WHERE s.voyage_id=? AND p.status='active' "
                          "AND s.status IN ('running','paused')", (vid,))["n"]
        if busy:
            raise ServiceError(409, "TOPOLOGY_LOCKED",
                               "有步骤正在执行/暂停，不能改拓扑")
        topo = parse_topology(vid, payload)
        for table in ("ballast_tanks", "ballast_pumps", "ballast_valves",
                      "ballast_pipes"):
            conn.execute(f"DELETE FROM {table} WHERE voyage_id=?", (vid,))
        for t in topo.tanks.values():
            conn.execute(
                "INSERT INTO ballast_tanks (id, voyage_id, name, capacity, "
                "min_level, max_level, level, x, y, z, density) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (new_id("btk"), vid, t.name, t.capacity, t.min_level,
                 t.max_level, t.level, t.x, t.y, t.z, t.density))
        for p in topo.pumps.values():
            conn.execute("INSERT INTO ballast_pumps (id, voyage_id, name, "
                         "rate) VALUES (?,?,?,?)",
                         (new_id("bpp"), vid, p.name, p.rate))
        for v in topo.valves.values():
            conn.execute("INSERT INTO ballast_valves (id, voyage_id, name) "
                         "VALUES (?,?,?)", (new_id("bvl"), vid, v.name))
        for p in topo.pipes.values():
            conn.execute("INSERT INTO ballast_pipes (id, voyage_id, name, "
                         "a_node, b_node, mutex_group) VALUES (?,?,?,?,?,?)",
                         (new_id("bpi"), vid, p.name, p.a_node, p.b_node,
                          p.mutex_group))
        cfg = payload.get("config") or {}
        if cfg:
            cur = _get_config(conn, vid)
            cur.update({k: float(v) for k, v in cfg.items()
                        if k in cur})
            conn.execute(
                "INSERT INTO ballast_config (voyage_id, max_heel_deg, "
                "max_draft, min_draft, max_bm, tpc, tolerance, drift_tol, "
                "low_flow_ratio) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(voyage_id) DO UPDATE SET "
                "max_heel_deg=excluded.max_heel_deg, "
                "max_draft=excluded.max_draft, min_draft=excluded.min_draft, "
                "max_bm=excluded.max_bm, tpc=excluded.tpc, "
                "tolerance=excluded.tolerance, drift_tol=excluded.drift_tol, "
                "low_flow_ratio=excluded.low_flow_ratio",
                (vid, cur["max_heel_deg"], cur["max_draft"], cur["min_draft"],
                 cur["max_bm"], cur["tpc"], cur["tolerance"], cur["drift_tol"],
                 cur["low_flow_ratio"]))
        # 拓扑变了，旧修订全部作废（已确认步骤的记录仍保留在账本里）
        conn.execute("UPDATE ballast_plans SET status='superseded' "
                     "WHERE voyage_id=? AND status='active'", (vid,))
        _ledger(conn, vid, actor, "TOPOLOGY_SET",
                {"tanks": len(topo.tanks), "pumps": len(topo.pumps),
                 "valves": len(topo.valves), "pipes": len(topo.pipes)})
        _checkpoint(conn, vid)
        conn.commit()
        return topo_to_dict(topo)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_topology(vid, db_path=None):
    conn = connect(db_path)
    try:
        _get_voyage(conn, vid)
        topo = _load_topology(conn, vid)
        out = topo_to_dict(topo)
        out["config"] = _get_config(conn, vid)
        out["total_ballast"] = round(sum(t.level for t in topo.tanks.values()),
                                     3)
        return out
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 方案生成 / 修订
# --------------------------------------------------------------------------- #

def _step_row_to_dict(s):
    return {
        "seq": s["seq"], "stage": s["stage"], "status": s["status"],
        "source_tank": s["source_tank"], "target_tank": s["target_tank"],
        "target_volume": s["target_volume"], "tolerance": s["tolerance"],
        "pump_id": s["pump_id"], "valves": json.loads(s["valves"]),
        "pipes": json.loads(s["pipes"]),
        "start_condition": json.loads(s["start_condition"]),
        "transferred": s["transferred"],
        "started_at": s["started_at"], "confirmed_at": s["confirmed_at"],
    }


def _insert_revision(conn, vid, steps, reason, actor, note=None):
    """冻结旧修订未开始的步骤，派生新修订。调用方持有事务。"""
    old = _active_revision(conn, vid)
    frozen = 0
    if old:
        cur = conn.execute(
            "UPDATE ballast_steps SET status='frozen' WHERE plan_id=? "
            "AND status IN ('pending','paused')", (old["id"],))
        frozen = cur.rowcount
        conn.execute("UPDATE ballast_plans SET status='superseded' "
                     "WHERE id=?", (old["id"],))
    rev = (_row(conn, "SELECT COALESCE(MAX(revision),0)+1 AS r FROM "
                "ballast_plans WHERE voyage_id=?", (vid,)))["r"]
    plan_id = new_id("bpl")
    base = _latest_published(conn, vid)
    conn.execute(
        "INSERT INTO ballast_plans (id, voyage_id, revision, status, "
        "base_plan_id, reason, note, created_by, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (plan_id, vid, rev, "active", base["id"] if base else None, reason,
         note, actor, now()))
    for s in steps:
        conn.execute(
            "INSERT INTO ballast_steps (id, plan_id, voyage_id, seq, stage, "
            "source_tank, target_tank, target_volume, tolerance, pump_id, "
            "valves, pipes, start_condition, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending')",
            (new_id("bst"), plan_id, vid, s["seq"], s["stage"],
             s["source_tank"], s["target_tank"], s["target_volume"],
             s["tolerance"], s["pump_id"], json.dumps(s["valves"]),
             json.dumps(s["pipes"]), json.dumps(s["start_condition"])))
    return plan_id, rev, old, frozen


def generate_plan(vid, actor="chief", stages=3, reason="initial", db_path=None):
    """读取已发布配载方案与装船进度，生成分阶段调水方案（新修订）。"""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        ship = _ship(conn, vid)
        topo = _require_topology(conn, vid)
        running = _row(conn, "SELECT COUNT(*) AS n FROM ballast_steps s "
                             "JOIN ballast_plans p ON p.id=s.plan_id "
                             "WHERE s.voyage_id=? AND p.status='active' "
                             "AND s.status='running'", (vid,))["n"]
        if running:
            raise ServiceError(409, "REPLAN_BLOCKED",
                               "有步骤正在调水，先暂停或完成后再重排")
        levels = _levels(conn, vid)
        cargo_now = _cargo_now(conn, vid)
        remaining, base_plan_id = _cargo_remaining(conn, vid)
        try:
            steps, targets = planner.generate_steps(
                ship, topo, levels, cargo_now, remaining,
                n_stages=int(stages), tolerance=ship.config["tolerance"])
        except planner.Infeasible as e:
            _ledger(conn, vid, actor, "PLAN_INFEASIBLE",
                    {"code": e.code, "message": e.message,
                     "details": e.details})
            _checkpoint(conn, vid)
            conn.commit()
            raise ServiceError(422, e.code, e.message, e.details)
        plan_id, rev, old, frozen = _insert_revision(
            conn, vid, steps, reason, actor,
            note=f"stages={stages}; base_plan={base_plan_id}")
        _ledger(conn, vid, actor, "PLAN_GENERATED",
                {"plan": plan_id, "revision": rev, "steps": len(steps),
                 "reason": reason, "frozen_from_previous": frozen,
                 "superseded": old["revision"] if old else None,
                 "total_ballast": round(sum(levels.values()), 3)})
        _checkpoint(conn, vid)
        conn.commit()
        return {"plan_id": plan_id, "revision": rev, "steps": steps,
                "frozen_from_previous": frozen,
                "total_ballast": round(sum(levels.values()), 3),
                "conserved": True}
    except ServiceError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_plan(vid, db_path=None):
    """当前修订 + 方案时间线。"""
    conn = connect(db_path)
    try:
        _get_voyage(conn, vid)
        plan = _active_revision(conn, vid)
        if not plan:
            return {"voyage_id": vid, "revision": None, "steps": [],
                    "timeline": []}
        steps = [_step_row_to_dict(s) for s in _steps_of(conn, plan["id"])]
        return {"voyage_id": vid, "plan_id": plan["id"],
                "revision": plan["revision"], "reason": plan["reason"],
                "note": plan["note"], "created_at": plan["created_at"],
                "steps": steps,
                "timeline": [{"seq": s["seq"], "stage": s["stage"],
                              "status": s["status"],
                              "label": f"{s['source_tank']}→{s['target_tank']} "
                                       f"{s['target_volume']}m³"}
                             for s in steps]}
    finally:
        conn.close()


def list_revisions(vid, db_path=None):
    conn = connect(db_path)
    try:
        _get_voyage(conn, vid)
        out = []
        for p in _rows(conn, "SELECT * FROM ballast_plans WHERE voyage_id=? "
                             "ORDER BY revision DESC", (vid,)):
            steps = _rows(conn, "SELECT status, COUNT(*) AS n, "
                                "COALESCE(SUM(transferred),0) AS t "
                                "FROM ballast_steps WHERE plan_id=? "
                                "GROUP BY status", (p["id"],))
            out.append({"plan_id": p["id"], "revision": p["revision"],
                        "status": p["status"], "reason": p["reason"],
                        "note": p["note"], "created_by": p["created_by"],
                        "created_at": p["created_at"],
                        "steps_by_status": {s["status"]: s["n"] for s in steps},
                        "confirmed_volume": round(sum(
                            s["t"] for s in steps if s["status"] == "confirmed"
                        ), 3)})
        return {"voyage_id": vid, "revisions": out}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 剩余方案安全性复核 + 重算
# --------------------------------------------------------------------------- #

def _remaining_safe(conn, vid):
    """剩余步骤在当前实际液位/装船进度下是否仍然安全。"""
    plan = _active_revision(conn, vid)
    if not plan:
        return True, []
    problems = []
    levels = _levels(conn, vid)
    topo = _load_topology(conn, vid)
    pending = [s for s in _steps_of(conn, plan["id"]) if s["status"] == "pending"]
    proj = dict(levels)
    for s in pending:  # 按序推演：不得抽空、不得越安全上限
        src, dst = s["source_tank"], s["target_tank"]
        proj[src] = proj.get(src, 0) - s["target_volume"]
        proj[dst] = proj.get(dst, 0) + s["target_volume"]
        if proj[src] < topo.tanks[src].min_level - 1e-6:
            problems.append(f"步骤{s['seq']} 将抽空 {src}")
        if proj[dst] > topo.tanks[dst].max_level + 1e-6:
            problems.append(f"步骤{s['seq']} 将使 {dst} 超过安全上限")
    ship = _ship(conn, vid)
    cargo_now = _cargo_now(conn, vid)
    remaining, _ = _cargo_remaining(conn, vid)
    final_cargo = list(cargo_now) + [(c["weight"], c["x"], c["y"], c["z"])
                                     for c in remaining]
    # 剩余步骤原样执行到底的终态，对当前最新货载必须仍然达标
    m = stability.metrics(ship, final_cargo,
                          planner.ballast_items(topo.tanks, proj))
    for v in stability.violations(ship, m):
        problems.append(f"终态 {v['code']}: {v['message']}")
    return (not problems), problems


def recheck(vid, actor="system", reason="loading_change", db_path=None):
    """装船变化/漂移后的复核：不安全则冻结未开始步骤并生成新修订。"""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _get_voyage(conn, vid)
        plan = _active_revision(conn, vid)
        if not plan:
            conn.rollback()
            return {"safe": True, "replanned": False, "reason": "no_plan"}
        running = _row(conn, "SELECT COUNT(*) AS n FROM ballast_steps "
                             "WHERE plan_id=? AND status='running'",
                             (plan["id"],))["n"]
        if running:
            raise ServiceError(409, "REPLAN_BLOCKED",
                               "有步骤正在调水，先暂停或完成后再复核")
        safe, problems = _remaining_safe(conn, vid)
        if safe:
            _ledger(conn, vid, actor, "RECHECK_OK",
                    {"revision": plan["revision"], "reason": reason})
            _checkpoint(conn, vid)
            conn.commit()
            return {"safe": True, "replanned": False,
                    "revision": plan["revision"]}
        _ledger(conn, vid, actor, "RECHECK_UNSAFE",
                {"revision": plan["revision"], "problems": problems,
                 "reason": reason})
        conn.commit()
    except ServiceError:
        conn.rollback()
        raise
    finally:
        conn.close()
    # 不安全 → 以当前实际液位重算（已确认步骤与转移量保留在旧修订里）
    result = generate_plan(vid, actor=actor, reason=reason, db_path=db_path)
    result.update({"safe": False, "replanned": True, "problems": problems})
    return result


# --------------------------------------------------------------------------- #
# 执行：启动 / 暂停 / 恢复 / 确认
# --------------------------------------------------------------------------- #

def _get_step(conn, vid, plan, seq):
    s = _row(conn, "SELECT * FROM ballast_steps WHERE plan_id=? AND seq=?",
             (plan["id"], int(seq)))
    if not s:
        raise ServiceError(404, "STEP_NOT_FOUND",
                           f"step {seq} not in revision {plan['revision']}")
    return s


def _seize(conn, vid, pump_id, officer, step_id=None):
    """占用泵组；部分唯一索引保证并发只有一方成功。"""
    try:
        sid = new_id("sez")
        conn.execute("INSERT INTO ballast_seizures (id, voyage_id, pump_id, "
                     "officer, step_id, released, created_at) "
                     "VALUES (?,?,?,?,?,0,?)",
                     (sid, vid, pump_id, officer, step_id, now()))
        return sid
    except Exception:
        holder = _row(conn, "SELECT officer, step_id FROM ballast_seizures "
                            "WHERE voyage_id=? AND pump_id=? AND released=0",
                      (vid, pump_id))
        raise ServiceError(409, "PUMP_BUSY",
                           f"泵组 {pump_id} 已被占用",
                           {"held_by": holder["officer"] if holder else None,
                            "held_by_step": holder["step_id"] if holder else None})


def _release_pump(conn, vid, pump_id):
    conn.execute("UPDATE ballast_seizures SET released=1, released_at=? "
                 "WHERE voyage_id=? AND pump_id=? AND released=0",
                 (now(), vid, pump_id))


def _running_step(conn, vid):
    return _row(conn, "SELECT s.* FROM ballast_steps s "
                      "JOIN ballast_plans p ON p.id=s.plan_id "
                      "WHERE s.voyage_id=? AND p.status='active' "
                      "AND s.status='running'", (vid,))


def start_step(vid, token, seq, revision, actor="officer", db_path=None):
    """下发启动一步调水。

    * 同一令牌重发 → 回放首次结果（不会重复下发）；
    * 同一令牌载荷变化 → 409 拒绝；
    * 旧修订的启动请求 → 返回最新方案（revision_moved），不报错。
    """
    payload = {"seq": int(seq), "revision": int(revision)}
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _get_voyage(conn, vid)
        replay = _token_replay(conn, token, "start", payload)
        if replay is not None:
            conn.rollback()
            return replay
        plan = _active_revision(conn, vid)
        if not plan:
            raise ServiceError(409, "NO_PLAN", "还没有调水方案，请先生成")
        if int(revision) != plan["revision"]:
            # 旧修订启动请求：返回最新方案
            steps = [_step_row_to_dict(s) for s in _steps_of(conn, plan["id"])]
            result = {"revision_moved": True, "requested_revision": int(revision),
                      "current_revision": plan["revision"],
                      "plan_id": plan["id"], "steps": steps}
            _token_record(conn, vid, token, "start", payload, actor, result)
            _ledger(conn, vid, actor, "START_STALE_REVISION",
                    {"requested": int(revision), "current": plan["revision"]})
            _checkpoint(conn, vid)
            conn.commit()
            return result
        step = _get_step(conn, vid, plan, seq)
        if step["status"] != "pending":
            raise ServiceError(409, "STEP_NOT_PENDING",
                               f"步骤 {seq} 当前为 {step['status']}，不能启动")
        if int(seq) > 1:
            prev = _get_step(conn, vid, plan, int(seq) - 1)
            if prev["status"] != "confirmed":
                raise ServiceError(409, "PREVIOUS_NOT_CONFIRMED",
                                   f"上一步（{int(seq)-1}）尚未确认")
        other = _running_step(conn, vid)
        if other:
            raise ServiceError(409, "STEP_BUSY",
                               f"步骤 {other['seq']} 正在调水，不能并行")
        cond = json.loads(step["start_condition"])
        gate = cond.get("confirmed_weight_gte") or 0
        have = _confirmed_weight(conn, vid)
        if have + 1e-9 < gate:
            raise ServiceError(
                409, "CONDITION_NOT_MET",
                f"开始条件未满足：已确认装船 {have:.1f}t < 要求 {gate:.1f}t",
                {"confirmed_weight": have, "required": gate})
        levels = _levels(conn, vid)
        topo = _load_topology(conn, vid)
        src, dst = step["source_tank"], step["target_tank"]
        if levels[src] - step["target_volume"] < topo.tanks[src].min_level - 1e-6:
            raise ServiceError(409, "SOURCE_TOO_LOW",
                               f"{src} 液位不足，启动将触及抽空保护")
        if levels[dst] + step["target_volume"] > topo.tanks[dst].max_level + 1e-6:
            raise ServiceError(409, "TARGET_TOO_HIGH",
                               f"{dst} 空间不足，启动将超过安全上限")
        _seize(conn, vid, step["pump_id"], actor, step_id=step["id"])
        conn.execute("UPDATE ballast_steps SET status='running', src_start=?, "
                     "dst_start=?, started_at=? WHERE id=?",
                     (levels[src], levels[dst], now(), step["id"]))
        result = {"dispatched": True, "seq": int(seq),
                  "revision": plan["revision"],
                  "pump_id": step["pump_id"],
                  "valves": json.loads(step["valves"]),
                  "pipes": json.loads(step["pipes"]),
                  "src_start": levels[src], "dst_start": levels[dst]}
        _token_record(conn, vid, token, "start", payload, actor, result)
        _ledger(conn, vid, actor, "STEP_STARTED",
                {"seq": int(seq), "revision": plan["revision"],
                 "pump": step["pump_id"], "valves": json.loads(step["valves"]),
                 "pipes": json.loads(step["pipes"]),
                 "src_start": levels[src], "dst_start": levels[dst]})
        _checkpoint(conn, vid)
        conn.commit()
        _maybe_crash("after_dispatch")
        return result
    except ServiceError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def pause_step(vid, token, actor="officer", db_path=None, reason="manual"):
    payload = {"action": "pause"}
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _get_voyage(conn, vid)
        replay = _token_replay(conn, token, "pause", payload)
        if replay is not None:
            conn.rollback()
            return replay
        step = _running_step(conn, vid)
        if not step:
            raise ServiceError(409, "NO_RUNNING_STEP", "没有正在调水的步骤")
        conn.execute("UPDATE ballast_steps SET status='paused' WHERE id=?",
                     (step["id"],))
        _release_pump(conn, vid, step["pump_id"])
        levels = _levels(conn, vid)
        transferred = round(step["src_start"] - levels[step["source_tank"]], 3)
        result = {"paused": True, "seq": step["seq"],
                  "transferred_so_far": transferred}
        _token_record(conn, vid, token, "pause", payload, actor, result)
        _ledger(conn, vid, actor, "STEP_PAUSED",
                {"seq": step["seq"], "reason": reason,
                 "transferred_so_far": transferred})
        _checkpoint(conn, vid)
        conn.commit()
        return result
    except ServiceError:
        conn.rollback()
        raise
    finally:
        conn.close()


def resume_step(vid, token, actor="officer", db_path=None):
    payload = {"action": "resume"}
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _get_voyage(conn, vid)
        replay = _token_replay(conn, token, "resume", payload)
        if replay is not None:
            conn.rollback()
            return replay
        plan = _active_revision(conn, vid)
        step = _row(conn, "SELECT * FROM ballast_steps WHERE plan_id=? AND "
                          "status='paused'", (plan["id"],)) if plan else None
        if not step:
            raise ServiceError(409, "NO_PAUSED_STEP", "没有已暂停的步骤")
        if _running_step(conn, vid):
            raise ServiceError(409, "STEP_BUSY", "已有步骤在调水")
        _seize(conn, vid, step["pump_id"], actor, step_id=step["id"])
        conn.execute("UPDATE ballast_steps SET status='running' WHERE id=?",
                     (step["id"],))
        result = {"resumed": True, "seq": step["seq"],
                  "pump_id": step["pump_id"]}
        _token_record(conn, vid, token, "resume", payload, actor, result)
        _ledger(conn, vid, actor, "STEP_RESUMED", {"seq": step["seq"]})
        _checkpoint(conn, vid)
        conn.commit()
        return result
    except ServiceError:
        conn.rollback()
        raise
    finally:
        conn.close()


def confirm_step(vid, token, seq, actor="officer", db_path=None):
    """确认一步：实际转移量须达容差，且稳性重算通过，才能推进。"""
    payload = {"seq": int(seq)}
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _get_voyage(conn, vid)
        replay = _token_replay(conn, token, "confirm", payload)
        if replay is not None:
            conn.rollback()
            return replay
        plan = _active_revision(conn, vid)
        if not plan:
            raise ServiceError(409, "NO_PLAN", "没有活动修订")
        step = _get_step(conn, vid, plan, seq)
        if step["status"] not in ("running", "paused"):
            raise ServiceError(409, "STEP_NOT_ACTIVE",
                               f"步骤 {seq} 为 {step['status']}，不能确认")
        levels = _levels(conn, vid)
        src, dst = step["source_tank"], step["target_tank"]
        moved_src = step["src_start"] - levels[src]
        moved_dst = levels[dst] - step["dst_start"]
        transferred = round((moved_src + moved_dst) / 2.0, 3)
        if abs(moved_src - moved_dst) > 2 * step["tolerance"]:
            _alarm(conn, vid, "DEVIATION",
                   f"步骤 {seq} 源/目标舱计量不一致：源侧 {moved_src:.2f} "
                   f"目标侧 {moved_dst:.2f}",
                   {"seq": int(seq), "src_side": moved_src,
                    "dst_side": moved_dst})
        if abs(transferred - step["target_volume"]) > step["tolerance"]:
            raise ServiceError(
                422, "TARGET_NOT_REACHED",
                f"实际转移 {transferred:.2f}m³ 未达目标 "
                f"{step['target_volume']:.2f}±{step['tolerance']:.2f}m³",
                {"transferred": transferred,
                 "target": step["target_volume"],
                 "tolerance": step["tolerance"]})
        # 稳性重算：当前实际液位 + 当前实际货载，全部限值必须通过
        ship = _ship(conn, vid)
        topo = _load_topology(conn, vid)
        m = stability.metrics(ship, _cargo_now(conn, vid),
                              planner.ballast_items(topo.tanks, levels))
        viol = stability.violations(ship, m)
        if viol:
            _alarm(conn, vid, "STABILITY",
                   f"步骤 {seq} 确认时稳性复核未通过", {"violations": viol})
            _ledger(conn, vid, actor, "CONFIRM_REJECTED",
                    {"seq": int(seq), "violations": viol})
            _checkpoint(conn, vid)
            conn.commit()
            raise ServiceError(422, "STABILITY_NOT_MET",
                               "稳性复核未通过，不能推进", {"violations": viol})
        conn.execute("UPDATE ballast_steps SET status='confirmed', "
                     "transferred=?, confirmed_at=? WHERE id=?",
                     (transferred, now(), step["id"]))
        _release_pump(conn, vid, step["pump_id"])
        result = {"confirmed": True, "seq": int(seq),
                  "transferred": transferred,
                  "next_seq": int(seq) + 1, "stability": m}
        _token_record(conn, vid, token, "confirm", payload, actor, result)
        _ledger(conn, vid, actor, "STEP_CONFIRMED",
                {"seq": int(seq), "transferred": transferred,
                 "target": step["target_volume"], "stability": m})
        _checkpoint(conn, vid)
        conn.commit()
        _maybe_crash("after_confirm")
        return result
    except ServiceError:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 泵组抢占（人工）
# --------------------------------------------------------------------------- #

def seize_pump(vid, token, pump_id, officer, db_path=None):
    payload = {"pump_id": pump_id, "officer": officer}
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _get_voyage(conn, vid)
        replay = _token_replay(conn, token, "seize", payload)
        if replay is not None:
            conn.rollback()
            return replay
        if not _row(conn, "SELECT id FROM ballast_pumps WHERE voyage_id=? "
                          "AND name=?", (vid, pump_id)):
            raise ServiceError(404, "PUMP_NOT_FOUND", f"unknown pump {pump_id}")
        _seize(conn, vid, pump_id, officer)
        result = {"seized": True, "pump_id": pump_id, "officer": officer}
        _token_record(conn, vid, token, "seize", payload, officer, result)
        _ledger(conn, vid, officer, "PUMP_SEIZED", {"pump": pump_id})
        _checkpoint(conn, vid)
        conn.commit()
        _maybe_crash("after_seize")
        return result
    except ServiceError:
        conn.rollback()
        raise
    finally:
        conn.close()


def release_pump(vid, token, pump_id, officer, db_path=None):
    payload = {"pump_id": pump_id, "officer": officer}
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _get_voyage(conn, vid)
        replay = _token_replay(conn, token, "release", payload)
        if replay is not None:
            conn.rollback()
            return replay
        conn.execute("UPDATE ballast_seizures SET released=1, released_at=? "
                     "WHERE voyage_id=? AND pump_id=? AND released=0",
                     (now(), vid, pump_id))
        result = {"released": True, "pump_id": pump_id}
        _token_record(conn, vid, token, "release", payload, officer, result)
        _ledger(conn, vid, officer, "PUMP_RELEASED", {"pump": pump_id})
        _checkpoint(conn, vid)
        conn.commit()
        return result
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 传感器反馈（液位 / 流量）
# --------------------------------------------------------------------------- #

def feedback(vid, levels=None, flows=None, actor="sensor", db_path=None):
    """液位与流量反馈。

    * 更新最新液位（水量不丢的唯一来源）；
    * 运行中步骤：核对实际转移量、流量不足自动暂停、计量偏差告警；
    * 无步骤运行：液位漂移告警，剩余方案不再安全时自动冻结重算。
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _get_voyage(conn, vid)
        topo = _require_topology(conn, vid)
        cfg = _get_config(conn, vid)
        before = _levels(conn, vid)
        levels = levels or {}
        flows = flows or {}
        unknown = [t for t in levels if t not in topo.tanks]
        if unknown:
            raise ServiceError(404, "TANK_NOT_FOUND",
                               f"unknown tanks: {sorted(unknown)}")
        for name, lv in levels.items():
            conn.execute("UPDATE ballast_tanks SET level=? "
                         "WHERE voyage_id=? AND name=?",
                         (float(lv), vid, name))
        after = dict(before)
        after.update({k: float(v) for k, v in levels.items()})
        alarms = []
        total_delta = sum(after.values()) - sum(before.values())
        if abs(total_delta) > cfg["drift_tol"]:
            alarms.append(_alarm(
                conn, vid, "WATER_UNBALANCED",
                f"总水量变化 {total_delta:+.2f}m³，疑似未登记的加注/排放",
                {"delta": round(total_delta, 3)}))

        step = _running_step(conn, vid)
        paused = False
        transferred = None
        if step:
            src, dst = step["source_tank"], step["target_tank"]
            moved_src = step["src_start"] - after[src]
            moved_dst = after[dst] - step["dst_start"]
            transferred = round((moved_src + moved_dst) / 2.0, 3)
            if abs(moved_src - moved_dst) > 2 * step["tolerance"]:
                alarms.append(_alarm(
                    conn, vid, "DEVIATION",
                    f"步骤 {step['seq']} 源/目标舱计量不一致",
                    {"seq": step["seq"], "src_side": round(moved_src, 3),
                     "dst_side": round(moved_dst, 3)}))
            for name in topo.tanks:  # 非本步舱位异动 = 串舱/泄漏
                if name in (src, dst):
                    continue
                if abs(after[name] - before[name]) > cfg["drift_tol"]:
                    alarms.append(_alarm(
                        conn, vid, "DEVIATION",
                        f"舱 {name} 在步骤 {step['seq']} 期间异动 "
                        f"{after[name]-before[name]:+.2f}m³",
                        {"tank": name, "seq": step["seq"]}))
            pump = topo.pumps.get(step["pump_id"])
            flow = flows.get(step["pump_id"])
            if pump and flow is not None \
                    and float(flow) < cfg["low_flow_ratio"] * pump.rate:
                # 流量不足：自动暂停，保持已转移量，释放泵组
                conn.execute("UPDATE ballast_steps SET status='paused' "
                             "WHERE id=?", (step["id"],))
                _release_pump(conn, vid, step["pump_id"])
                paused = True
                alarms.append(_alarm(
                    conn, vid, "FLOW_LOW",
                    f"泵 {step['pump_id']} 流量 {flow}m³/h 低于阈值 "
                    f"{cfg['low_flow_ratio']*pump.rate:.0f}m³/h，步骤 "
                    f"{step['seq']} 已自动暂停",
                    {"seq": step["seq"], "flow": flow}))
                _ledger(conn, vid, actor, "STEP_AUTO_PAUSED",
                        {"seq": step["seq"], "flow": flow,
                         "transferred_so_far": transferred})
        else:
            drifted = {t: round(after[t] - before[t], 3)
                       for t in after
                       if abs(after[t] - before[t]) > cfg["drift_tol"]}
            if drifted:
                alarms.append(_alarm(conn, vid, "DRIFT",
                                     f"液位漂移: {drifted}",
                                     {"drifted": drifted}))
        _ledger(conn, vid, actor, "FEEDBACK",
                {"levels": levels, "flows": flows,
                 "transferred": transferred})
        _checkpoint(conn, vid)
        conn.commit()
        _maybe_crash("after_partial_flow")
        replanned = None
        if not step and alarms:  # 漂移后复核剩余方案，不安全则冻结重算
            try:
                r = recheck(vid, actor="auto", reason="drift", db_path=db_path)
                replanned = r if r.get("replanned") else None
            except ServiceError:
                replanned = None
        return {"levels": after, "transferred": transferred,
                "paused": paused, "alarms": alarms, "replanned": replanned}
    except ServiceError:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 查询：稳性 / 资源 / 告警 / 账本
# --------------------------------------------------------------------------- #

def get_stability(vid, db_path=None):
    conn = connect(db_path)
    try:
        ship = _ship(conn, vid)
        topo = _load_topology(conn, vid)
        levels = _levels(conn, vid)
        cargo_now = _cargo_now(conn, vid)
        m = stability.metrics(ship, cargo_now,
                              planner.ballast_items(topo.tanks, levels))
        return {"voyage_id": vid, "metrics": m,
                "violations": stability.violations(ship, m),
                "limits": {"min_gm": ship.min_gm, "max_gm": ship.max_gm,
                           "max_trim": ship.max_trim, **ship.config},
                "confirmed_weight": round(_confirmed_weight(conn, vid), 3),
                "total_ballast": round(sum(levels.values()), 3)}
    finally:
        conn.close()


def get_resources(vid, db_path=None):
    conn = connect(db_path)
    try:
        _get_voyage(conn, vid)
        pumps = []
        for p in _rows(conn, "SELECT * FROM ballast_pumps WHERE voyage_id=? "
                             "ORDER BY name", (vid,)):
            hold = _row(conn, "SELECT officer, step_id, created_at FROM "
                              "ballast_seizures WHERE voyage_id=? AND pump_id=? "
                              "AND released=0", (vid, p["name"]))
            pumps.append({"pump_id": p["name"], "rate": p["rate"],
                          "occupied": hold is not None,
                          "held_by": hold["officer"] if hold else None,
                          "held_by_step": hold["step_id"] if hold else None})
        running = _running_step(conn, vid)
        valves_in_use, pipes_in_use = [], []
        if running:
            valves_in_use = json.loads(running["valves"])
            pipes_in_use = json.loads(running["pipes"])
        return {"voyage_id": vid, "pumps": pumps,
                "running_step": running["seq"] if running else None,
                "valves_in_use": valves_in_use,
                "pipes_in_use": pipes_in_use}
    finally:
        conn.close()


def list_alarms(vid, db_path=None):
    conn = connect(db_path)
    try:
        _get_voyage(conn, vid)
        return {"voyage_id": vid, "alarms": [
            {"id": r["id"], "kind": r["kind"], "message": r["message"],
             "detail": json.loads(r["detail"] or "{}"), "status": r["status"],
             "created_at": r["created_at"]}
            for r in _rows(conn, "SELECT * FROM ballast_alarms "
                                 "WHERE voyage_id=? ORDER BY created_at DESC "
                                 "LIMIT 100", (vid,))]}
    finally:
        conn.close()


def ack_alarm(vid, alarm_id, actor="officer", db_path=None):
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        r = _row(conn, "SELECT * FROM ballast_alarms WHERE id=? AND "
                       "voyage_id=?", (alarm_id, vid))
        if not r:
            raise ServiceError(404, "ALARM_NOT_FOUND", "alarm not found")
        conn.execute("UPDATE ballast_alarms SET status='acked' WHERE id=?",
                     (alarm_id,))
        _ledger(conn, vid, actor, "ALARM_ACKED", {"alarm": alarm_id})
        conn.commit()
        return {"id": alarm_id, "status": "acked"}
    finally:
        conn.close()


def list_events(vid, limit=200, db_path=None):
    conn = connect(db_path)
    try:
        _get_voyage(conn, vid)
        return {"voyage_id": vid, "events": [
            {"id": r["id"], "actor": r["actor"], "action": r["action"],
             "detail": json.loads(r["detail"] or "{}"),
             "created_at": r["created_at"]}
            for r in _rows(conn, "SELECT * FROM ballast_ledger "
                                 "WHERE voyage_id=? ORDER BY id DESC LIMIT ?",
                           (vid, limit))]}
    finally:
        conn.close()


def list_checkpoints(vid, limit=20, db_path=None):
    conn = connect(db_path)
    try:
        _get_voyage(conn, vid)
        return {"voyage_id": vid, "checkpoints": [
            {"id": r["id"], "state": json.loads(r["state"]),
             "created_at": r["created_at"]}
            for r in _rows(conn, "SELECT * FROM ballast_checkpoints "
                                 "WHERE voyage_id=? ORDER BY id DESC LIMIT ?",
                           (vid, limit))]}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 崩溃恢复
# --------------------------------------------------------------------------- #

def recover(vid=None, db_path=None):
    """重启后从账本 + 最新液位恢复唯一状态。

    规则（确定性）：
      1. 运行中步骤至多一个（取最小 seq），多余的降为 paused；
      2. 步骤持有的泵占用，其步骤不在 running 的一律释放；
      3. 运行中步骤的已转移量按最新液位重算 —— 水量不丢；
      4. 每个航次写 RECOVERY 账本事件与新检查点。
    """
    conn = connect(db_path)
    reports = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        if vid:
            vids = [vid]
        else:
            vids = [r["voyage_id"] for r in _rows(
                conn, "SELECT DISTINCT voyage_id FROM ballast_tanks")]
        for v in vids:
            fixed = []
            running = _rows(conn, "SELECT s.* FROM ballast_steps s "
                                  "JOIN ballast_plans p ON p.id=s.plan_id "
                                  "WHERE s.voyage_id=? AND p.status='active' "
                                  "AND s.status='running' ORDER BY s.seq", (v,))
            for extra in running[1:]:  # 不可能发生，但恢复必须全覆盖
                conn.execute("UPDATE ballast_steps SET status='paused' "
                             "WHERE id=?", (extra["id"],))
                fixed.append(f"step {extra['seq']} downgraded to paused")
            keep = running[0] if running else None
            for sz in _rows(conn, "SELECT * FROM ballast_seizures "
                                  "WHERE voyage_id=? AND released=0", (v,)):
                if sz["step_id"] and (not keep or sz["step_id"] != keep["id"]):
                    conn.execute("UPDATE ballast_seizures SET released=1, "
                                 "released_at=? WHERE id=?",
                                 (now(), sz["id"]))
                    fixed.append(f"seizure on {sz['pump_id']} released "
                                 f"(owner step not running)")
            transferred = None
            if keep:
                levels = _levels(conn, v)
                moved_src = keep["src_start"] - levels[keep["source_tank"]]
                moved_dst = levels[keep["target_tank"]] - keep["dst_start"]
                transferred = round((moved_src + moved_dst) / 2.0, 3)
            _ledger(conn, v, "system", "RECOVERY",
                    {"running_step": keep["seq"] if keep else None,
                     "transferred_so_far": transferred, "fixed": fixed})
            _checkpoint(conn, v)
            reports.append({"voyage_id": v,
                            "running_step": keep["seq"] if keep else None,
                            "transferred_so_far": transferred,
                            "fixed": fixed,
                            "state": _exec_state(conn, v)})
        conn.commit()
        return {"recovered": reports}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
