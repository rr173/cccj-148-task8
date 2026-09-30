"""端到端测试夹具：准备配载系统航次（导入→求解→发布），构造合法箱号。"""
import json
import os
import tempfile
import time
import urllib.request
import urllib.error

from stowage import service
from stowage.db import connect, init_db

from relay.protocol import iso6346_check_digit
from relay.receiver import Receiver
from relay.segment import DeviceSpool

# 4 堆 x 4 层 = 16 槽，配 8 个箱，留出空闲槽位供后续“重映射/换槽”演练
STACKS = [
    {"bay": 2, "row": 1, "max_tiers": 4, "max_weight": 200},
    {"bay": 2, "row": 2, "max_tiers": 4, "max_weight": 200},
    {"bay": 4, "row": 1, "max_tiers": 4, "max_weight": 200},
    {"bay": 4, "row": 2, "max_tiers": 4, "max_weight": 200},
]


def make_container_id(prefix4: str, n: int) -> str:
    base = f"{prefix4}{n:06d}"
    return base + str(iso6346_check_digit(base))


def make_containers(prefix="MSKU", count=10, pods=None):
    pods = pods or ["HAM", "SIN", "HAM", "HAM", "SIN", "HAM", "HAM", "SIN",
                    "HAM", "HAM"]
    out = []
    for i in range(count):
        out.append({"id": make_container_id(prefix, i + 1),
                    "pod": pods[i % len(pods)], "weight": 10 + i})
    return out


def voyage_payload(containers=None, stacks=None,
                   port_sequence=("SHA", "SIN", "HAM")):
    return {
        "voyage": {"name": "RELAY-E2E",
                   "port_sequence": list(port_sequence)},
        "ship": {"lightship_weight": 9000, "lightship_lcg": 30,
                 "lightship_vcg": 8.5, "min_gm": 1.0, "max_gm": 5.5,
                 "max_trim": 2.0},
        "stacks": stacks or STACKS,
        "containers": containers if containers is not None
        else make_containers(),
    }


def new_paths():
    d = tempfile.mkdtemp(prefix="relaytest_")
    return (os.path.join(d, "relay.db"), os.path.join(d, "stowage.db"),
            os.path.join(d, "spool"))


def seed_voyage(stowage_db, payload=None, publish=True):
    """返回 (voyage_id, {container_id: slot_code})。"""
    init_db(stowage_db)
    vid = service.import_voyage(payload or voyage_payload(),
                                db_path=stowage_db)["voyage_id"]
    plan_id = None
    if publish:
        job = service.create_solve_job(vid, run_async=False,
                                       db_path=stowage_db)
        j = service.get_job(job, db_path=stowage_db)
        assert j["status"] == "done", j
        plan_id = j["plan_id"]
        token = service.request_approval(plan_id, actor="planner",
                                         db_path=stowage_db)["token"]
        service.publish_plan(plan_id, token=token, actor="planner",
                             db_path=stowage_db)
    mapping = plan_slot_mapping(stowage_db, vid, plan_id) if publish else {}
    return vid, mapping


def plan_slot_mapping(stowage_db, voyage_id, plan_id=None):
    conn = connect(stowage_db)
    try:
        if plan_id is None:
            p = service._latest_published(conn, voyage_id)
            plan_id = p["id"] if p else None
        if not plan_id:
            return {}
        rows = conn.execute(
            "SELECT pe.container_id cid, s.code code FROM plan_entries pe "
            "JOIN slots s ON s.id=pe.slot_id WHERE pe.plan_id=?",
            (plan_id,)).fetchall()
        return {r["cid"]: r["code"] for r in rows}
    finally:
        conn.close()


def republish_relocate(stowage_db, voyage_id, container, dest_slot_code):
    """确定性的“计划更替”：把 container 改配到指定槽位（需为空且合法），
    其余条目不变，走草稿→审批→发布正规流程，返回新 plan_id。"""
    conn = connect(stowage_db)
    try:
        plan = service._latest_published(conn, voyage_id)
        assignment = {}
        for pe in conn.execute(
                "SELECT container_id, slot_id FROM plan_entries WHERE plan_id=?",
                (plan["id"],)):
            assignment[pe["container_id"]] = pe["slot_id"]
        dest = conn.execute(
            "SELECT id FROM slots WHERE voyage_id=? AND code=?",
            (voyage_id, dest_slot_code)).fetchone()
        assert dest, f"槽位 {dest_slot_code} 不存在"
        # 目标槽位若被占，把占用者移到 container 的原槽位（对调）
        occupant = next((cid for cid, sid in assignment.items()
                         if sid == dest["id"] and cid != container), None)
        old_slot = assignment[container]
        assignment[container] = dest["id"]
        if occupant:
            assignment[occupant] = old_slot
    finally:
        conn.close()

    conn = connect(stowage_db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        new_id = service._create_plan(
            conn, voyage_id, assignment, True, "relay plan change", "planner")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    token = service.request_approval(new_id, actor="planner",
                                     db_path=stowage_db)["token"]
    service.publish_plan(new_id, token=token, actor="planner",
                         db_path=stowage_db)
    return new_id


def republish_move_container_to_valid_slot(stowage_db, voyage_id, container):
    """以合法方式让 container 的方案槽位发生变化并发布新版本。

    选同一 stack、同一卸港的相邻箱对调（重量/悬空/港序均不受影响）。
    返回 (new_plan_id, new_slot_code)。
    """
    conn = connect(stowage_db)
    try:
        plan = service._latest_published(conn, voyage_id)
        # 目标箱所在 (bay,row,pod)
        me = conn.execute(
            "SELECT c.pod AS pod, st.bay AS rbay, st.row AS rrn, s.code AS code "
            "FROM plan_entries pe JOIN slots s ON s.id=pe.slot_id "
            "JOIN stacks st ON st.id=s.stack_id JOIN containers c "
            "ON c.id=pe.container_id WHERE pe.plan_id=? AND pe.container_id=?",
            (plan["id"], container)).fetchone()
        # 同堆同卸港的另一个箱（对调不改变重量/悬空/港序）
        mate = conn.execute(
            "SELECT pe.container_id AS cid, s.code AS code FROM plan_entries pe "
            "JOIN slots s ON s.id=pe.slot_id JOIN stacks st ON st.id=s.stack_id "
            "JOIN containers c ON c.id=pe.container_id WHERE pe.plan_id=? "
            "AND st.bay=? AND st.row=? AND c.pod=? AND pe.container_id<>?",
            (plan["id"], me["rbay"], me["rrn"], me["pod"],
             container)).fetchone()
        assert mate, "找不到同堆同卸港的交换对象"
        dest_code = mate["code"]
    finally:
        conn.close()
    return republish_relocate(stowage_db, voyage_id, container,
                              dest_code), dest_code


# ---------------------------- HTTP 辅助 ------------------------------------ #
def wait_server(url, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url + "/healthz", timeout=1) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError(f"server not up at {url}")


def post_json(url, path, body_text):
    data = body_text if isinstance(body_text, (bytes, str)) else \
        json.dumps(body, ensure_ascii=False)
    req = urllib.request.Request(
        url.rstrip("/") + path,
        data=data.encode() if isinstance(data, str) else data,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def get_json(url, path):
    with urllib.request.urlopen(url.rstrip("/") + path, timeout=10) as r:
        return r.status, json.loads(r.read().decode())


def fresh_receiver(relay_db, stowage_db, voyage_id=None, crash=None):
    os.environ["STOWAGE_DB"] = stowage_db
    recv = Receiver(relay_db, stowage_db, crash=crash)
    if voyage_id:
        conn = connect(relay_db)
        try:
            from relay import store
            store.set_config(conn, "active_voyage", voyage_id)
            conn.commit()
        finally:
            conn.close()
    return recv


def make_spool(root, device_id):
    return DeviceSpool(root, device_id)
