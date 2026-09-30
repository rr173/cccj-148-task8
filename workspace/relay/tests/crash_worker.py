"""崩溃注入工作进程：执行到指定点时 os._exit(91)，模拟掉电。

用法（由 test_recovery 以子进程方式调用）：
  python3 -m relay.tests.crash_worker <relay_db> <stowage_db> <voyage_id> \
      <point:P1|P2|P3> [extra_packets]

固定载荷：一个含 2 条记录的 seq=0 包。
"""
import json
import os
import sys

from relay import protocol
from relay.receiver import Receiver
from relay.store import connect, set_config
from relay.tests import fixture as fx


def build_packet(voyage_id):
    # 取当前方案前两个箱槽
    m = fx.plan_slot_mapping(os.environ["STOWAGE_DB"], voyage_id)
    items = list(m.items())[:2]
    return protocol.seal({
        "device_id": "HH-CRASH", "session": "HH-CRASH:s1", "seq": 0,
        "voyage_id": voyage_id,
        "records": [
            {"record_id": "rec_crash_1", "container": items[0][0],
             "slot": items[0][1], "collected_at": "2026-09-28T11:00:00+08:00"},
            {"record_id": "rec_crash_2", "container": items[1][0],
             "slot": items[1][1], "collected_at": "2026-09-28T11:00:01+08:00"},
        ]})


def main():
    relay_db, stowage_db, voyage_id, point = sys.argv[1:5]
    os.environ["STOWAGE_DB"] = stowage_db
    recv = Receiver(relay_db, stowage_db, crash=point)
    conn = connect(relay_db)
    set_config(conn, "active_voyage", voyage_id)
    conn.commit()
    conn.close()

    pkt = build_packet(voyage_id)
    try:
        ack = recv.upload(json.dumps(pkt, ensure_ascii=False))
        print(json.dumps({"ack": ack}))
    except SystemExit:
        raise
    except BaseException as e:  # 崩溃点之外的真实错误才会到这
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
