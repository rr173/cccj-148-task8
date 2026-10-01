#!/usr/bin/env python3
"""启动压载水联动执行模块：  python3 run_ballast.py [port] [db_path]

与配载系统共用数据库（默认 stowage.db，或 STOWAGE_DB 环境变量），
启动时先从事件账本与最新液位恢复执行状态。
"""
import os
import sys

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8060
    if len(sys.argv) > 2:
        os.environ["STOWAGE_DB"] = os.path.abspath(sys.argv[2])
    from ballast.server import main
    main(port=port)
