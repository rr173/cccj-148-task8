#!/usr/bin/env python3
"""启动压载联动执行服务：
   python3 run_ballast.py [port] [ballast.db] [stowage.db]
"""
import os
import sys

from ballast.executor import Executor
from ballast.server import serve

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8091
    db = sys.argv[2] if len(sys.argv) > 2 else "data/ballast.db"
    stowage = sys.argv[3] if len(sys.argv) > 3 else \
        os.environ.get("STOWAGE_DB", "stowage.db")
    if not os.path.exists(stowage):
        stowage = None
    ex = Executor(db)
    ex.recover()
    serve(ex, port, stowage)
