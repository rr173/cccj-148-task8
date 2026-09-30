#!/usr/bin/env python3
"""启动岸桥许可/波次互锁服务：  python3 run_permit.py [port] [db_path]"""
import os
import sys

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8070
    if len(sys.argv) > 2:
        os.environ["PERMIT_DB"] = os.path.abspath(sys.argv[2])
    from permit.server import main
    main(port=port)
