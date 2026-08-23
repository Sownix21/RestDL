#!/usr/bin/env python3
"""Exit non-zero when the DLoader process heartbeat is missing or stale."""
import json
import os
import sys
import time


data_dir = os.getenv("DATA_DIR", "database")
path = os.path.join(data_dir, "health.json")
try:
    with open(path, "r", encoding="utf-8") as handle:
        heartbeat = json.load(handle)
    age = time.time() - os.path.getmtime(path)
    pid = int(heartbeat["pid"])
    os.kill(pid, 0)
    if age > int(os.getenv("HEALTH_MAX_AGE", "90")):
        raise RuntimeError(f"heartbeat is {age:.0f}s old")
    if not heartbeat.get("bot_connected"):
        raise RuntimeError("bot client is disconnected")
except Exception as exc:
    print(f"DLoader unhealthy: {exc}", file=sys.stderr)
    raise SystemExit(1)

print("DLoader healthy")
