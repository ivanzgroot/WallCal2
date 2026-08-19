"""The SSE stream: it must open, frame correctly, and only speak on change."""
import json
import os
import pathlib
import sys
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
tmp = tempfile.mkdtemp()
os.environ["WALLCAL_DB_PATH"] = os.path.join(tmp, "t.db")
os.environ["WALLCAL_RUNTIME_DIR"] = os.path.join(tmp, "run")

import app                                   # noqa: E402
import database                              # noqa: E402
from presence import runtime                 # noqa: E402

database.init_db()
fails = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f"  (want {want!r})"))
    if not ok:
        fails.append(label)


runtime.write_state({"density": "far", "display_on": True, "display_mode": "normal",
                     "brightness": 100, "brightness_source": "css"})

client = app.app.test_client()
resp = client.get("/api/presence/stream", buffered=False)
check("content type", resp.headers["Content-Type"].split(";")[0], "text/event-stream")
check("not cached", resp.headers.get("Cache-Control"), "no-cache")

frames, stop = [], threading.Event()


def read():
    buf = b""
    for chunk in resp.response:
        if stop.is_set():
            break
        buf += chunk
        while b"\n\n" in buf:
            frame, buf = buf.split(b"\n\n", 1)
            frames.append(frame.decode())


reader = threading.Thread(target=read, daemon=True)
reader.start()
time.sleep(0.4)
check("stream opens with a comment", frames[0].startswith(":"), True)

data = [f for f in frames if f.startswith("data:")]
check("first state pushed", len(data) >= 1, True)
first = json.loads(data[0][5:])
check("carries density", first["density"], "far")

before = len([f for f in frames if f.startswith("data:")])
time.sleep(0.5)
check("silent while nothing changes",
      len([f for f in frames if f.startswith("data:")]), before)

# Silent on the wire too. A keepalive on every 0.1 s tick was 864,000 frames
# a day, each one waking the kiosk browser to parse a comment and throw it
# away. Only the opening ': connected' belongs in the first second.
check("no keepalive flood", len([f for f in frames if f.startswith(":")]), 1)

runtime.write_state({"density": "near", "display_on": True, "display_mode": "normal",
                     "brightness": 100, "brightness_source": "css"})
time.sleep(0.5)
pushed = [json.loads(f[5:]) for f in frames if f.startswith("data:")]
check("pushes the change", pushed[-1]["density"], "near")
check("push count", len(pushed), before + 1)

stop.set()
print("")
print("ALL PASS" if not fails else "%d FAILED: %s" % (len(fails), fails))
sys.exit(1 if fails else 0)
