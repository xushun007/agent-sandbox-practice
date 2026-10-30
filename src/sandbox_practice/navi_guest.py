from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

home = Path(os.environ.get("NAVI_HOME", "/root/.navi-agent"))
home.mkdir(parents=True, exist_ok=True)
config = home / "config.yaml"
config.write_text(
    "runtime:\n  max_iterations: 8\n"
    "telemetry:\n  langfuse:\n    enabled: false\n"
)
prompt = os.environ["NAVI_PRACTICE_PROMPT"]
session_id = os.environ.get("NAVI_PRACTICE_SESSION", "sandbox-practice-session")
(home / "experiment-start.json").write_text(
    json.dumps({"started_at": time.time(), "pid": os.getpid(), "session_id": session_id})
)
print("NAVI_AGENT_START", flush=True)
agent = subprocess.Popen(
    ["navi-agent", "--yolo", "--session-id", session_id, prompt],
    env=os.environ.copy(),
)
start_record = json.loads((home / "experiment-start.json").read_text())
start_record["agent_pid"] = agent.pid
(home / "experiment-start.json").write_text(json.dumps(start_record))
returncode = agent.wait()
(home / "experiment-exit.json").write_text(
    json.dumps({"finished_at": time.time(), "returncode": returncode})
)
print(f"NAVI_AGENT_EXIT {returncode}", flush=True)
hold_seconds = int(os.environ.get("NAVI_PRACTICE_POST_RUN_HOLD_SECONDS", "120"))
if hold_seconds > 0:
    time.sleep(hold_seconds)
sys.exit(returncode)
