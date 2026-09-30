#!/usr/bin/env python3
"""启动接收服务： python3 run_relay.py [port] [relay.db] [stowage.db]"""
import os
import sys

from relay.receiver import Receiver
from relay.server import serve

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
    relay_db = sys.argv[2] if len(sys.argv) > 2 else "data/relay.db"
    stowage_db = sys.argv[3] if len(sys.argv) > 3 else \
        os.environ.get("STOWAGE_DB", "stowage.db")
    recv = Receiver(relay_db, stowage_db)
    recv.recover()  # 重启先从事件日志恢复
    serve(recv, port)
