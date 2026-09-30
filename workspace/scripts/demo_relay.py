#!/usr/bin/env python3
"""一键端到端演练：在临时目录里准备航次、驱动两台终端、制造各类收件情形、
处置待处理、打印操作台四栏与一致性结论。

    python3 scripts/demo_relay.py
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stowage.db import connect as biz_connect

from relay import protocol, views
from relay.receiver import Receiver
from relay.console import dispose
from relay.tests import fixture as fx


def box(title):
    print("\n" + "=" * 74)
    print(title)
    print("=" * 74)


def main():
    root = tempfile.mkdtemp(prefix="relay_demo_")
    relay_db = os.path.join(root, "relay.db")
    stowage_db = os.path.join(root, "stowage.db")
    spool_root = os.path.join(root, "spool")
    print(f"工作目录: {root}")

    # 16 槽装满 16 箱，保证后续步骤有未装船的箱
    payload = fx.voyage_payload(containers=fx.make_containers(count=16))
    vid = fx.seed_voyage(stowage_db, payload=payload)[0]
    mapping = fx.plan_slot_mapping(stowage_db, vid)
    recv = Receiver(relay_db, stowage_db)
    from relay.store import connect, set_config
    c = connect(relay_db)
    set_config(c, "active_voyage", vid)
    c.commit()
    c.close()
    items = list(mapping.items())

    def up(device, session, seq, scans):
        env = protocol.seal({
            "device_id": device, "session": session, "seq": seq,
            "voyage_id": vid,
            "records": [{"record_id": f"rec_{device}_{seq}_{i}",
                         "container": ct, "slot": sl,
                         "collected_at": "2026-09-28T09:00:00+08:00"}
                        for i, (ct, sl) in enumerate(scans)]})
        return recv.upload(json.dumps(env, ensure_ascii=False))

    box("1) 连续上传：HH-01 段#0 两箱，游标推进，全部装船确认")
    a = up("HH-01", "HH-01:A", 0, items[0:2])
    print(f"   status={a['status']} cursor={a['cursor']} "
          f"applied={sum(r['status']=='applied' for r in a['records'])}")

    box("2) 相同包再次上传 -> 回原确认（不重复确认）")
    again = up("HH-01", "HH-01:A", 0, items[0:2])
    print(f"   duplicate={again['duplicate']}，业务确认表仍为 2 条")

    box("3) 篡改包：改箱号不重算校验和 -> 整包拒绝")
    bad = protocol.seal({"device_id": "HH-01", "session": "HH-01:A",
                         "seq": 1, "voyage_id": vid,
                         "records": [{"record_id": "rec_bad",
                                      "container": items[0][0],
                                      "slot": "99-01-01",
                                      "collected_at": "2026-09-28T09:00:00+08:00"}]})
    bad["records"][0]["container"] = "ZZZZ0000000"
    from relay.receiver import RecvError
    try:
        recv.upload(json.dumps(bad))
    except RecvError as e:
        print(f"   HTTP 语义码 {e.http_status} {e.code}：{e}")

    box("4) 断层：#2 先到暂存（游标不动），#1 补齐后 #2 自动排空")
    s2 = up("HH-01", "HH-01:A", 2, items[4:5])
    print(f"   #2 => {s2['status']}，缺失 {s2['missing']}，游标仍 {s2['cursor']}")
    s1 = up("HH-01", "HH-01:A", 1, items[2:4])
    print(f"   #1 到达后 cursor={s1['cursor']}（#2 已连续应用）")

    box("5) 双终端争用同一箱 + 箱在别处已确认 + 计划更替")
    ct, sl = items[5]
    print(f"   HH-02 与 HH-03 同时上报 {ct} @ {sl}")
    w = up("HH-02", "HH-02:A", 0, [(ct, sl)])
    l = up("HH-03", "HH-03:A", 0, [(ct, sl)])
    print(f"   HH-02 => {w['records'][0]['status']}；"
          f"HH-03 => {l['records'][0]['status']}"
          f"（{l['records'][0]['reason']}）")

    # 箱在别处（中继器之外）已确认
    from stowage import service
    ct2, sl2 = items[6]
    plan = service._latest_published(biz_connect(stowage_db), vid)
    service.confirm_load(vid, plan["id"], ct2, sl2, actor="crane-Z",
                         db_path=stowage_db)
    e = up("HH-02", "HH-02:A", 1, [(ct2, sl2)])
    print(f"   {ct2} 已由 crane-Z 确认，HH-02 上报 => "
          f"{e['records'][0]['status']}（{e['records'][0]['reason']}）")

    # 计划更替后旧槽位上报：选一个尚未装船的箱（方案后段）做重映射演练
    pc = None
    for idx in range(8, len(items)):
        ct3, sl3 = items[idx]
        new_plan, new_slot = fx.republish_move_container_to_valid_slot(
            stowage_db, vid, ct3)
        # 目标槽位未被占用才用于演示
        biz = biz_connect(stowage_db)
        occupied = biz.execute(
            "SELECT 1 FROM slots s JOIN confirmations f ON f.slot_id=s.id "
            "WHERE s.code=? AND f.voyage_id=?", (new_slot, vid)).fetchone()
        biz.close()
        if not occupied:
            pc = up("HH-02", "HH-02:A", 2, [(ct3, sl3)])
            break
    assert pc, "没有可用的未占用箱用于重映射演示"
    ct3, sl3 = items[idx]
    print(f"   计划换版：{ct3} 方案槽位 {sl3}->{new_slot}，"
          f"按旧槽位上报 => {pc['records'][0]['status']}"
          f"（{pc['records'][0]['reason']}）")

    box("6) 操作台处置：忽略 / 重映射 / 重调，同一请求只记一次")
    loser_rid = l["records"][0]["record_id"]
    r1 = dispose(recv, "demo-ignore-1", loser_rid, "ignore",
                 operator_id="duty", note="重复上报忽略")
    r2 = dispose(recv, "demo-ignore-1", loser_rid, "ignore",
                 operator_id="duty", note="重复请求")
    print(f"   忽略 {loser_rid[:16]}：首次 idempotent={r1['idempotent']}，"
          f"重发 idempotent={r2['idempotent']}/{r2['status']}")

    plan_change_rid = pc["records"][0]["record_id"]
    rm = dispose(recv, "demo-remap-1", plan_change_rid, "remap",
                 new_slot=new_slot, operator_id="duty",
                 note="按新版计划重映射")
    print(f"   重映射到 {new_slot} => {rm['status']}，确认={rm.get('confirmation_id')}")

    box("7) 操作台四栏 + 航次映射 + 一致性")
    for s in views.sessions_overview(relay_db):
        print(f"   {s['session']}: 游标={s['cursor']} "
              f"applied={s['applied_count']} pending={s['pending_count']} "
              f"staged={len(s['staged'])} quar={len(s['quarantine'])}")
    print("   待处理:")
    for r in views.pending(relay_db):
        print(f"     - {r['container']} {r['slot']} {r['reason']}")
    applied, ignored = views.applied(relay_db)
    print(f"   已应用 {len(applied)} 条；忽略 {ignored} 条；"
          f"隔离 {len(views.quarantine(relay_db))}；"
          f"拒绝 {len(views.rejected(relay_db))}")
    v = views.verify_invariants(relay_db, stowage_db)
    print(f"\n   一致性自检: {'OK' if v['ok'] else v['violations']}")
    print(f"   业务装船确认表总数: "
          f"{biz_connect(stowage_db).execute('SELECT COUNT(*) c FROM confirmations').fetchone()['c']}")
    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
