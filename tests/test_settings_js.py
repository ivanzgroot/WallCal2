"""Run the settings page's JavaScript for real.

Catches the class of bug that killed calibration: a handler that fires,
fails, and says nothing.
"""
import json
import pathlib
import re
import sys

from py_mini_racer import MiniRacer

ROOT = pathlib.Path(__file__).resolve().parent.parent
fails = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f"  (want {want!r})"))
    if not ok:
        fails.append(label)


TAG = re.compile(r"<(input|select|textarea|div|span|button|p|details)\b([^>]*)>", re.I)
ATTR = re.compile(r'([a-zA-Z0-9_:-]+)\s*=\s*"([^"]*)"')

#: Attributes the page reads back off its own controls. Anything else is
#: presentation the shim has no opinion about.
CARRIED = ("id", "type", "min", "max", "step", "value", "placeholder")


def template_nodes():
    """Every control the settings template declares, as (id, tagName, attrs).

    The shim has no HTML parser: it invents a node the first time something
    asks for an id, with no attributes on it. That meant a slider's own
    min/max, and every id-less value chip, simply did not exist — so tests
    passed against code paths that never ran. Reading the real template keeps
    the two from drifting, the same way test_import does.
    """
    html = (ROOT / "templates" / "settings.html").read_text(encoding="utf-8")
    out, synthetic = [], 0
    for tag in TAG.finditer(html):
        attrs = dict(ATTR.findall(tag.group(2)))
        interesting = ("id" in attrs or "data-setting" in attrs
                       or "data-seg" in attrs or "data-value-for" in attrs
                       or "data-when" in attrs)
        if not interesting:
            continue
        node_id = attrs.get("id")
        if not node_id:
            synthetic += 1
            node_id = "auto-%d" % synthetic
        keep = {k: v for k, v in attrs.items()
                if k.startswith("data-") or k in CARRIED}
        out.append((node_id, tag.group(1).upper(), keep))
    return out


def build(routes=None, status=None):
    ctx = MiniRacer()
    ctx.eval((ROOT / "tests" / "domshim.js").read_text(encoding="utf-8"))

    for node_id, tag_name, attrs in template_nodes():
        ctx.eval("(function(){var n=document.getElementById(%s);"
                 "n.tagName=%s;%s})()" % (
                     json.dumps(node_id), json.dumps(tag_name),
                     "".join("n.setAttribute(%s,%s);" % (json.dumps(k), json.dumps(v))
                             for k, v in attrs.items())))
        if "type" in attrs:
            ctx.eval("document.getElementById(%s).type=%s;"
                     % (json.dumps(node_id), json.dumps(attrs["type"])))
        if "value" in attrs:
            ctx.eval("document.getElementById(%s).value=%s;"
                     % (json.dumps(node_id), json.dumps(attrs["value"])))

    # The three override buttons are the one thing the template writes as
    # children rather than as addressable nodes.
    ctx.eval("""(function(){
        var seg = document.getElementById('overrideSeg');
        seg.children = [];
        ['auto','on','off'].forEach(function (mode) {
            var b = document.createElement('button');
            b.setAttribute('data-mode', mode);
            b.textContent = mode;
            seg.appendChild(b);
        });
    })()""")
    base = {
        "/api/settings": {"density_mode": "auto", "theme": "dark",
                          "sensor_distance_max_cm": "300", "density_near_cm": "100",
                          "density_far_cm": "140", "widget_transit": "dynamic"},
        "/api/calendars": {"calendars": []},
        "/api/system": {}, "/api/status": {"feeds": []},
        "/api/prewake": {"next": None},
        "/api/presence/live": {"daemon_running": True, "distance_cm": 120},
        "/api/presence/override": {"ok": True},
        "/api/sensor/calibrate/apply": {"ok": True,
            "updated": {"sensor_distance_max_cm": "180"}},
    }
    base.update(routes or {})
    ctx.eval("__routes = " + json.dumps(base) + ";")
    if status:
        ctx.eval("__status = " + json.dumps(status) + ";")
    ctx.eval((ROOT / "static" / "js" / "settings.js").read_text(encoding="utf-8"))
    return ctx


def click(ctx, node_id):
    """Fire the click handler registered on a node."""
    return ctx.eval(f"(function(){{ var h=__handlers['{node_id}']; "
                    f"if(!h) return 'NO HANDLER'; h.forEach(function(f){{f.call("
                    f"document.getElementById('{node_id}'), {{preventDefault:function(){{}}}});}}); "
                    f"return 'fired'; }})()")


def text(ctx, node_id):
    return ctx.eval(f"(function(){{var n=document.getElementById('{node_id}');"
                    f"return n.children.map(function(c){{"
                    f"return (c.children||[]).map(function(g){{return g.textContent||'';}}).join(' ')"
                    f" + ' ' + (c.textContent||'');}}).join(' | ');}})()")


print("Calibration apply")
ctx = build()
check("button has a handler", click(ctx, "calibApply"), "fired")
check("success is reported", "Übernommen" in text(ctx, "calibError"), True)
check("says what changed", "180" in text(ctx, "calibError"), True)

print("")
print("Calibration apply — server refuses (e.g. read-only database)")
ctx = build(status={"/api/sensor/calibrate/apply": 500},
            routes={"/api/sensor/calibrate/apply":
                    {"error": "Die Datenbank ist schreibgeschützt. Auf dem Pi: ./wallcal.sh doctor --fix"}})
click(ctx, "calibApply")
body = text(ctx, "calibError")
check("failure is reported", "Konnte nicht übernommen werden" in body, True)
check("says what to do", "doctor --fix" in body, True)

print("")
print("A control appears only where changing it would do something")


def shown(ctx, expr):
    """Is the element carrying this data-when currently visible?"""
    return ctx.eval(
        "(function(){var m=document.querySelectorAll('[data-when]').filter("
        "function(n){return n.getAttribute('data-when')===%s;});"
        "return m.length ? !m[0].hidden : 'NO SUCH CONDITION';})()" % json.dumps(expr))


ctx = build(routes={"/api/settings": {"widget_transit": "off", "theme": "dark"}})
check("a widget switched off hides its whole panel",
      shown(ctx, "widget_transit!=off"), False)

# The case that prompted this: windows describe when a dynamic board appears,
# so they say nothing at all once it is set to always.
ctx = build(routes={"/api/settings": {"widget_transit": "dynamic", "theme": "dark"}})
check("dynamic shows the time windows", shown(ctx, "widget_transit=dynamic"), True)
ctx = build(routes={"/api/settings": {"widget_transit": "always", "theme": "dark"}})
check("always hides them", shown(ctx, "widget_transit=dynamic"), False)
check("but keeps the panel", shown(ctx, "widget_transit!=off"), True)

print("")
print("Every operator the page relies on")
ctx = build(routes={"/api/settings": {
    "density_mode": "near", "night_mode": "dim_clock", "dim_seconds": "20",
    "display_off_strategy": "pwm,hdmi", "prewake_enabled": "false",
    "sensor_mode": "uart", "theme": "dark"}})
check("=  a pinned layout hides the thresholds",
      shown(ctx, "density_mode=auto|on"), False)
check("=  and the near view it will never draw",
      shown(ctx, "density_mode!=far"), True)
check("=  night brightness follows dim_clock",
      shown(ctx, "night_mode=dim_clock"), True)
check("!= quiet hours need a night mode", shown(ctx, "night_mode!=off"), True)
check("~  pwm is in the strategy, so its block shows",
      shown(ctx, "display_off_strategy~pwm"), True)
check("~  and none is not, so the screensaver hides",
      shown(ctx, "display_off_strategy~none"), False)
check("!= a dim level needs a dim to happen in",
      shown(ctx, "dim_seconds!=0"), True)
check("two terms: both must hold",
      shown(ctx, "prewake_enabled=true prewake_timed_only=false"), False)
check("uart mode hides the GPIO pin", shown(ctx, "sensor_mode=gpio|auto"), False)
check("and shows the port", shown(ctx, "sensor_mode=uart|auto"), True)

print("")
print("It keeps up as you change things")
ctx = build(routes={"/api/settings": {"prewake_enabled": "false", "theme": "dark"}})
check("nothing under a switch that is off",
      shown(ctx, "prewake_enabled=true"), False)
ctx.eval("(function(){var n=document.getElementById('setPrewake');"
         "n.checked=true; __fire(n,'change');})()")
check("turning it on reveals its settings, without a reload",
      shown(ctx, "prewake_enabled=true"), True)
check("and the day-time field stays hidden behind its own condition",
      shown(ctx, "prewake_enabled=true prewake_timed_only=false"), False)


def posts(ctx, url):
    """Every write to ``url``, decoded, in order."""
    return [json.loads(p["body"]) for p in json.loads(ctx.eval(
        "JSON.stringify(__posts.filter(function(p){return p.url==='" + url + "';}))"))
        if p["body"]]


def flush(ctx):
    """Let the debounced writes actually go out."""
    ctx.eval("__flush(400);")


def fire(ctx, root, label, evt="click"):
    return ctx.eval("__fire(__byLabel('%s', %s), '%s')" % (root, json.dumps(label), evt))


def setval(ctx, root, label, value):
    ctx.eval("(function(){var n=__byLabel('%s', %s); n.value=%s;"
             "__fire(n,'change'); __fire(n,'input');})()"
             % (root, json.dumps(label), json.dumps(value)))


print("")
print("Zeitfenster: a picker, not a syntax")
ctx = build(routes={"/api/settings": {
    "transit_windows": "mon=06:30-09:00,16:00-18:30|tue=|wed=|thu=|fri=|sat=|sun=",
    "widget_transit": "dynamic"}})
check("seven days, always", ctx.eval(
    "document.getElementById('windowEditor').children.length"), 7)
check("the empty days say so", ctx.eval(
    "__all('windowEditor').filter(function(n){return n.textContent==="
    "'wird nicht angezeigt';}).length"), 6)
check("the summary counts days, not characters", ctx.eval(
    "document.querySelector('[data-value-for=\"transit_windows\"]').textContent"),
    "1 von 7 Tagen")

fire(ctx, "windowEditor", "Dienstag: Zeitfenster hinzufügen")
flush(ctx)
written = posts(ctx, "/api/settings")[-1]["transit_windows"]
check("adding a window writes a parseable day", "tue=07:00-09:00" in written, True)
check("and leaves the other days alone", "mon=06:30-09:00,16:00-18:30" in written, True)

fire(ctx, "windowEditor", "Zeitfenster entfernen")
flush(ctx)
check("removing one takes it back out",
      "mon=16:30-18:30" in posts(ctx, "/api/settings")[-1]["transit_windows"] or
      "mon=16:00-18:30" in posts(ctx, "/api/settings")[-1]["transit_windows"], True)

print("")
print("A window running past midnight is kept, and says so")
# 22:00-02:00 is a night bus. Rejecting it as reversed dropped it on parse,
# so opening this page and touching an unrelated day silently deleted it.
ctx = build(routes={"/api/settings": {
    "transit_windows": "mon=22:00-02:00|tue=|wed=|thu=|fri=|sat=|sun=",
    "widget_transit": "dynamic"}})
check("it survives being loaded", ctx.eval(
    "__all('windowEditor').filter(function(n){return n.className==='win-range';}).length"), 1)
check("and is labelled as overnight", ctx.eval(
    "__all('windowEditor').filter(function(n){"
    "return n.className==='win-overnight';}).length"), 1)

fire(ctx, "windowEditor", "Dienstag: Zeitfenster hinzufügen")
flush(ctx)
written = posts(ctx, "/api/settings")[-1]["transit_windows"]
check("editing another day leaves it alone", "mon=22:00-02:00" in written, True)

print("")
print("A zero-length window is the only one corrected")
ctx = build(routes={"/api/settings": {
    "transit_windows": "mon=08:00-09:00|tue=|wed=|thu=|fri=|sat=|sun=",
    "widget_transit": "dynamic"}})
setval(ctx, "windowEditor", "bis", "08:00")
flush(ctx)
written = posts(ctx, "/api/settings")[-1]["transit_windows"]
monday = written.split("|")[0].split("=")[1]
check("it does not stay empty", monday.split("-")[0] != monday.split("-")[1], True)

print("")
print("Montag auf Di–Fr übertragen")
ctx = build(routes={"/api/settings": {
    "transit_windows": "mon=06:30-09:00|tue=|wed=|thu=|fri=|sat=|sun=",
    "widget_transit": "dynamic"}})
click(ctx, "windowCopy")
flush(ctx)
written = posts(ctx, "/api/settings")[-1]["transit_windows"]
check("weekdays match Monday",
      all(d + "=06:30-09:00" in written for d in ("mon", "tue", "wed", "thu", "fri")), True)
check("the weekend is left alone", "sat=|sun=" in written, True)

print("")
print("Fraktionen: a colour and two words")
ctx = build(routes={"/api/settings": {
    "abfall_fractions": "rest=Restmüll:#8A8F9C|bio=Bio:#6FAE3F"}})
check("one row per fraction", ctx.eval(
    "document.getElementById('fractionEditor').children.length"), 2)
check("counted in the summary", ctx.eval(
    "document.querySelector('[data-value-for=\"abfall_fractions\"]').textContent"),
    "2 Tonnen")

# The delimiters of the stored format cannot be typed into it any more.
setval(ctx, "fractionEditor", "Anzeigename", "Gelber:Sack|Rest")
flush(ctx)
written = posts(ctx, "/api/settings")[-1]["abfall_fractions"]
check("delimiters are stripped as you type", "GelberSackRest" in written, True)
check("so the format still parses", len(written.split("|")), 2)

click(ctx, "fractionAdd")
check("a new row appears", ctx.eval(
    "document.getElementById('fractionEditor').children.length"), 3)
before = len(posts(ctx, "/api/settings"))
flush(ctx)
check("but an empty one is not written yet",
      len(posts(ctx, "/api/settings")), before)

print("")
print("The density thresholds move together")
ctx = build(routes={"/api/settings": {
    "density_mode": "auto", "density_near_cm": "100", "density_far_cm": "140",
    "sensor_distance_max_cm": "300"}})
ctx.eval("(function(){var n=document.getElementById('setNearCm');"
         "n.value='200'; __fire(n,'input');})()")
flush(ctx)
written = posts(ctx, "/api/settings")[-1]
check("pushing near past far moves far too", written.get("density_far_cm"), "220")
check("near lands where it was dragged", written.get("density_near_cm"), "200")
check("and it is one write, not two", len(written), 2)

ctx.eval("(function(){var f=document.getElementById('setFarCm');"
         "f.value='40'; __fire(f,'input');})()")
flush(ctx)
written = posts(ctx, "/api/settings")[-1]
check("dragging far below near pushes near down",
      float(written["density_near_cm"]) <= float(written["density_far_cm"]) - 20, True)
check("and both stay inside their own range",
      float(written["density_near_cm"]) >= 30, True)

print("")
print("Abschaltmethode: four known values, not a spelling test")
ctx = build(routes={"/api/settings": {"display_off_strategy": "hdmi"}})
check("one row per strategy", ctx.eval(
    "document.getElementById('strategyPicker').children.length"), 4)
ctx.eval("(function(){var n=__all('strategyPicker').filter(function(x){"
         "return x.value==='pwm';})[0]; n.checked=true; __fire(n,'change');})()")
flush(ctx)
check("combining keeps the daemon's own order",
      posts(ctx, "/api/settings")[-1]["display_off_strategy"], "hdmi,pwm")

ctx.eval("(function(){var n=__all('strategyPicker').filter(function(x){"
         "return x.value==='none';})[0]; n.checked=true; __fire(n,'change');})()")
flush(ctx)
check("never-off displaces the rest",
      posts(ctx, "/api/settings")[-1]["display_off_strategy"], "none")
check("and says what that means", "Bildschirm bleibt an" in
      ctx.eval("document.getElementById('strategyNote').textContent"), True)

ctx = build(routes={"/api/settings": {"display_off_strategy": "hdmi"}})
before = len(posts(ctx, "/api/settings"))
ctx.eval("(function(){var n=__all('strategyPicker').filter(function(x){"
         "return x.value==='hdmi';})[0]; n.checked=false; __fire(n,'change');})()")
flush(ctx)
check("emptying it is refused rather than guessed at",
      len(posts(ctx, "/api/settings")), before)

print("")
print("The manual override is readable, not just settable")
ctx = build(routes={"/api/presence/live": {
    "daemon_running": True, "distance_cm": 120, "override": "on"}})
check("the wall's own state paints the buttons", ctx.eval(
    "__all('overrideSeg').filter(function(b){return b.getAttribute('aria-checked')"
    "==='true';}).map(function(b){return b.getAttribute('data-mode');}).join()"), "on")
check("stated in words too", ctx.eval(
    "document.getElementById('overrideValue').textContent"), "immer an")
check("and carried above the fold", ctx.eval(
    "document.getElementById('overrideBanner').hidden"), False)
check("with the way out on it",
      "Automatik" in text(ctx, "overrideBanner"), True)

ctx.eval("(function(){var b=__all('overrideBanner').filter(function(n){"
         "return n.textContent==='Automatik zurück';})[0]; __fire(b,'click');})()")
check("which posts auto", posts(ctx, "/api/presence/override")[-1], {"mode": "auto"})
check("and the banner goes", ctx.eval(
    "document.getElementById('overrideBanner').hidden"), True)

ctx = build()
check("nothing shouted when the sensor is in charge", ctx.eval(
    "document.getElementById('overrideBanner').hidden"), True)

print("")
print("A refused write puts the page back")
ctx = build(status={"POST /api/settings": 500},
            routes={"/api/settings": {"brightness": "100", "density_mode": "auto",
                                      "density_near_cm": "100", "density_far_cm": "140"},
                    "POST /api/settings": {"error": "Die Datenbank ist schreibgeschützt."}})
ctx.eval("(function(){var n=document.getElementById('setBrightness');"
         "n.value='35'; __fire(n,'input');})()")
flush(ctx)
check("the control returns to the wall's value", ctx.eval(
    "document.getElementById('setBrightness').value"), "100")
check("and says so", "nicht gespeichert" in text(ctx, "saveError"), True)

print("")
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
