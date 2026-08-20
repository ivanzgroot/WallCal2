"""Widget feeds are fetched on a thread, never inside a request.

The fetch used to happen in the /api/widgets handler: an 8 s-per-feed
network timeout in front of a request the wall abandons after 10 s, so on a
slow WLAN the widgets stopped updating even though the cache was fine.
feeds.py already says a fetch is never in the path of a render — these are
the checks that keep it true.
"""
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
tmp = tempfile.mkdtemp()
os.environ["WALLCAL_DB_PATH"] = os.path.join(tmp, "t.db")
os.environ["WALLCAL_RUNTIME_DIR"] = os.path.join(tmp, "run")

import app                            # noqa: E402
import database                       # noqa: E402
import feeds                          # noqa: E402
import widgets                        # noqa: E402
from presence import runtime          # noqa: E402

database.init_db()
fails = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f"  (want {want!r})"))
    if not ok:
        fails.append(label)


# Every provider reaches the network through this one helper, so replacing it
# seals the whole module. Returning {} is a valid empty answer for all of them.
calls = []
# Shaped like Open-Meteo actually answers, including the hourly block. An
# empty one made this suite pass over a loop that never ran — and that loop
# compares its bare local stamps against a clock that carries an offset,
# which Python refuses to do rather than guess.
_FUTURE = "2099-08-19T"
WEATHER = {
    "current": {"temperature_2m": 18.2, "apparent_temperature": 17.0,
                "weather_code": 61, "is_day": 1, "precipitation": 0.4,
                "wind_speed_10m": 12},
    "hourly": {"time": [_FUTURE + "13:00", _FUTURE + "14:00", _FUTURE + "15:00"],
               "temperature_2m": [18, 19, 18],
               "precipitation_probability": [5, 20, 75],
               "weather_code": [2, 3, 63]},
    "daily": {"time": ["2026-08-19", "2026-08-20"],
              "temperature_2m_min": [12, 13], "temperature_2m_max": [21, 22],
              "precipitation_probability_max": [55, 40], "weather_code": [61, 0]},
}


def _stub(url, params=None):
    calls.append(url)
    return WEATHER if "open-meteo" in url else {}


feeds._get = _stub

database.set_many_settings({
    "widget_transit": "always", "transit_station_id": "de:09362:12345",
    "transit_station_name": "Regensburg Hbf",
    "widget_weather": "always", "weather_lat": "49.02", "weather_lon": "12.10",
    "widget_travel": "off", "widget_qr": "off",
})

client = app.app.test_client()
refresher = widgets.Refresher()


def awake(on):
    runtime.write_state({"display_on": on, "density": "far"})


print("The request path never reaches the network")
awake(True)
calls.clear()
resp = client.get("/api/widgets")
check("endpoint answers", resp.status_code, 200)
check("without fetching anything", calls, [])

print("")
print("The refresher does the fetching")
calls.clear()
check("it ran", refresher.refresh_once(), True)
check("and went to the network", len(calls) > 0, True)
warmed = {f["feed"] for f in database.feed_freshness()}
check("transit cached", any(f.startswith("transit:") for f in warmed), True)
check("weather cached", "weather" in warmed, True)

print("")
print("A warm cache is what the request path serves")
calls.clear()
body = client.get("/api/widgets").get_json()
check("still no fetching", calls, [])
check("transit slot present", body["transit"]["slot"], "transit")
# The count and the line filters live behind the cache, so a caller reading
# the feed row itself gets the provider's raw list and every one of those
# settings quietly stops working.
database.set_setting("transit_count", "2")
capped = client.get("/api/widgets").get_json()["transit"]
check("the departure count is honoured from cache too",
      len(capped["departures"]) <= 2, True)
check("station carried through", body["transit"].get("station"), "Regensburg Hbf")
# Every widget, not just the one whose fallback was written by hand. Weather
# and travel used to gate the whole provider call on allow_fetch, so moving
# the fetching to a thread left them permanently invisible on the wall.
check("weather rendered from the cache", body["weather"]["visible"], True)
check("with a real reading in it", body["weather"]["temperature"] is not None, True)
check("and the hourly strip survived the shaping",
      len(body["weather"]["hourly"]), 3)
check("with tomorrow alongside it",
      (body["weather"]["tomorrow"] or {}).get("max"), 22)

print("")
print("A dark panel costs nobody's rate limit")
awake(False)
calls.clear()
database.forget_feed("transit:")
database.forget_feed("weather")
check("the refresher stands down", refresher.refresh_once(), False)
check("nothing fetched", calls, [])

awake(True)
check("and picks up again when somebody walks up",
      refresher.refresh_once(), True)
check("fetching resumed", len(calls) > 0, True)

print("")
print("A dead network is invisible to the request path")
awake(True)


def explode(url, params=None):
    calls.append(url)
    raise OSError("network is down")


feeds._get = explode
database.forget_feed("transit:")
database.forget_feed("weather")
refresher.refresh_once()               # fails, records the error, keeps going
resp = client.get("/api/widgets")
check("the wall still gets an answer", resp.status_code, 200)
check("and it is a whole payload", sorted(resp.get_json().keys()),
      ["feeds", "generated_at", "qr", "transit", "travel", "weather"])

print("")
print("ALL PASS" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
