import datetime
import json
import os
import signal
import sys
import time

EXPECTED_COMMAND_SECONDS = 21600
SAFETY_STOP_SECONDS = 22020
START = time.monotonic()

def event(name, **extra):
    print(json.dumps({"probe": "janus-full-command-budget-20261026", "event": name,
                      "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                      "elapsedSeconds": round(time.monotonic() - START, 3),
                      "pid": os.getpid(), **extra}), flush=True)

def stopped(sig, _frame):
    event("controller-signal", signal=sig)
    raise SystemExit(128 + sig)

for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(sig, stopped)

if len(sys.argv) != 2 or sys.argv[1] not in ("3.11", "3.12"):
    raise SystemExit(2)
if sys.argv[1] == "3.12":
    event("independent-lane-control")
    raise SystemExit(0)

event("command-start", commandBudgetSeconds=EXPECTED_COMMAND_SECONDS,
      safetyStopSeconds=SAFETY_STOP_SECONDS, lane=sys.argv[1])
next_heartbeat = START + 300
while time.monotonic() - START < SAFETY_STOP_SECONDS:
    time.sleep(1)
    if time.monotonic() >= next_heartbeat:
        event("heartbeat")
        next_heartbeat += 300
# A successful commissioning run is terminated by the protected 360-minute limit.
# Reaching this independent bound means that limit was not enforced.
event("unexpected-safety-stop")
raise SystemExit(99)
