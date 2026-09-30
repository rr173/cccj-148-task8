#!/usr/bin/env python3
"""Start the stowage planner:  python3 run.py [port]"""
import sys

from stowage.server import main

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    main(port=port)
