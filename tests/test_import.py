"""The wall must still boot: every route answers and every asset resolves."""
import os
import pathlib
import re
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
tmp = tempfile.mkdtemp()
os.environ["WALLCAL_DB_PATH"] = os.path.join(tmp, "t.db")
os.environ["WALLCAL_RUNTIME_DIR"] = os.path.join(tmp, "run")

import app          # noqa: E402
import database     # noqa: E402

database.init_db()
client = app.app.test_client()
fails = []

ROUTES = ["/", "/settings", "/events", "/api/settings", "/api/status",
          "/api/presence", "/api/presence/live", "/api/display", "/api/widgets",
          "/api/prewake", "/api/system", "/api/qr.svg", "/api/calendars"]
for route in ROUTES:
    code = client.get(route).status_code
    print(f"  {'PASS' if code == 200 else 'FAIL'}  {route} -> {code}")
    if code != 200:
        fails.append(route)

for page in ("/", "/settings"):
    html = client.get(page).get_data(as_text=True)
    for asset in re.findall(r'(?:href|src)="(/static/[^"]+)"', html):
        if client.get(asset).status_code != 200:
            fails.append(asset)
    # Every id the page's script reaches for must exist in its markup.
    js_name = "wall.js" if page == "/" else "settings.js"
    js = (ROOT / "static" / "js" / js_name).read_text(encoding="utf-8")
    ids = set(re.findall(r'id="([^"]+)"', html))
    missing = sorted({m.group(1) for m in re.finditer(r"\$\('([A-Za-z0-9_-]+)'\)", js)} - ids)
    print(f"  {'PASS' if not missing else 'FAIL'}  {page} dangling ids: {missing or 'none'}")
    if missing:
        fails.append(page + " ids")

html = client.get("/settings").get_data(as_text=True)
keys = set(re.findall(r'data-setting="([^"]+)"', html)) | \
       set(re.findall(r'data-seg="([^"]+)"', html))
bad = sorted(k for k in keys if k not in database.SETTABLE_KEYS)
print(f"  {'PASS' if not bad else 'FAIL'}  settings keys valid ({len(keys)} bound): {bad or 'none'}")
if bad:
    fails.append("settings keys")

print("\nALL PASS" if not fails else f"\nFAILED: {fails}")
sys.exit(1 if fails else 0)
