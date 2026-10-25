"""Disposable commissioning ref only: hold execution across identity rechecks."""
import json
import os
from pathlib import Path
import time

probe = {"probe": "manual-reader-loss-20261025", "pid": os.getpid(),
         "uid": os.getuid(), "startedAt": time.time(), "maximumSeconds": 240}
Path("janus-reader-loss-probe.json").write_text(json.dumps(probe) + "\n")
print("JANUS_READER_LOSS_PROBE_STARTED " + json.dumps(probe), flush=True)
for _ in range(8):
    time.sleep(30)
    print("JANUS_READER_LOSS_PROBE_STILL_RUNNING", flush=True)
raise SystemExit("Reader-loss probe expired without controller interruption")
