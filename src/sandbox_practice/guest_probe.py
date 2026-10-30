from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path("/tmp/sandbox-practice")
ROOT.mkdir(parents=True, exist_ok=True)
BOOT_ID = f"{os.getpid()}-{time.time_ns()}"
TICKS = 0


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps(
            {
                "pid": os.getpid(),
                "boot_id": BOOT_ID,
                "ticks_in_memory": TICKS,
                "heartbeat": json.loads((ROOT / "heartbeat.json").read_text()),
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        return


def tick() -> None:
    global TICKS
    while True:
        TICKS += 1
        (ROOT / "heartbeat.json").write_text(
            json.dumps({"ticks_on_disk": TICKS, "updated_at": time.time()})
        )
        time.sleep(1)


if __name__ == "__main__":
    (ROOT / "fixture.bin").write_bytes(os.urandom(1024 * 1024))
    threading.Thread(target=tick, daemon=True).start()
    print(json.dumps({"ready": True, "pid": os.getpid(), "boot_id": BOOT_ID}), flush=True)
    HTTPServer(("0.0.0.0", 8765), Handler).serve_forever()
