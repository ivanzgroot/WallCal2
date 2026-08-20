/* WallCal settings — the service surface.
 *
 * Every control is declarative: data-setting names the key, and one generic
 * binder handles reading, debouncing, writing and confirming. Adding a
 * setting means adding a row to the template, not another event listener.
 */
(function () {
'use strict';

var state = { settings: {}, saved: {}, calendars: [], presence: null,
              pendingStation: null };
var SAVE_DEBOUNCE = 400;

function $(id) { return document.getElementById(id); }
function qsa(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }

function api(method, url, body, cb) {
    var xhr = new XMLHttpRequest();
    xhr.open(method, url, true);
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.timeout = 15000;
    xhr.onload = function () {
        var data = null;
        try { data = JSON.parse(xhr.responseText); } catch (e) {}
        cb(xhr.status >= 400 ? (data && data.error) || ('HTTP ' + xhr.status) : null, data);
    };
    xhr.onerror = function () { cb('network', null); };
    xhr.ontimeout = function () { cb('timeout', null); };
    xhr.send(body ? JSON.stringify(body) : null);
}
function get(url, cb) { api('GET', url, null, cb); }
function post(url, body, cb) { api('POST', url, body, cb); }

// ---------------------------------------------------------------
// Saving
//
// Changes apply live within a couple of seconds. Without feedback you
// change a value, see nothing, and change it again — so the displayed
// value flashes once and fades back. Debounced, not one write per
// keystroke.
// ---------------------------------------------------------------
var saveTimers = {};

function save(key, value) {
    var one = {};
    one[key] = value;
    saveMany(one);
}

/** Write several keys as one change.
 *
 *  Picking a station sets an id and a name; dragging one density threshold
 *  can push the other. Sending those separately meant two requests, two
 *  debounces and a moment where the wall had half the answer — an id with no
 *  name, or thresholds briefly crossed.
 */
function saveMany(changes) {
    var keys = Object.keys(changes);
    if (!keys.length) return;

    keys.forEach(function (key) {
        state.settings[key] = String(changes[key]);
        showValue(key);
    });
    // Every write goes through here, so this is the one place that has to
    // ask whether anything just became relevant — or stopped being.
    applyConditions();

    // One timer per set of keys, so unrelated controls never cancel each
    // other and the same control still coalesces while you drag it.
    var batch = keys.slice().sort().join(',');
    clearTimeout(saveTimers[batch]);
    saveTimers[batch] = setTimeout(function () {
        var body = {};
        keys.forEach(function (key) { body[key] = String(changes[key]); });
        post('/api/settings', body, function (err) {
            if (err) { revert(keys); flashError(keys, err); return; }
            keys.forEach(function (key) { state.saved[key] = state.settings[key]; });
            confirmSaved(keys);
        });
    }, SAVE_DEBOUNCE);
}

/** Put the page back to what the wall actually holds.
 *
 *  Without this a rejected write left the control showing the value it failed
 *  to store, and everything computed from it — the density explanation, the
 *  setup card — went on reasoning from a number the wall had never seen.
 */
function revert(keys) {
    keys.forEach(function (key) {
        if (state.saved[key] === undefined) return;
        state.settings[key] = state.saved[key];
        applyOne(key);
    });
    updateDensityExplanation();
    renderSetup();
}

function confirmSaved(keys) {
    var box = $('saveError');
    if (box) box.innerHTML = '';
    keys.forEach(function (key) {
        qsa('[data-value-for="' + key + '"]').forEach(function (el) {
            el.classList.remove('failed');
            el.classList.add('saved');
            setTimeout(function () { el.classList.remove('saved'); }, 60);
        });
    });
}

/** Say what happened, where it happened, in the interface's voice.
 *  Errors explain what failed and what to try; they do not apologise. */
function notify(hostId, kind, title, detail) {
    var host = $(hostId);
    if (!host) return;
    host.innerHTML = '';
    var box = document.createElement('div');
    box.className = (kind === 'ok') ? 'ok-note' : 'err';
    var b = document.createElement('b');
    b.textContent = title;
    box.appendChild(b);
    if (detail) box.appendChild(document.createTextNode(detail));
    host.appendChild(box);
    if (kind === 'ok') {
        setTimeout(function () { if (host.firstChild === box) host.innerHTML = ''; }, 6000);
    }
}

/** Turn a transport-level failure into something a person can act on. */
function reason(err) {
    if (err === 'network') return 'Die Wand ist nicht erreichbar. WLAN prüfen.';
    if (err === 'timeout') return 'Zeitüberschreitung — die Wand antwortet nicht.';
    return String(err || 'Unbekannter Fehler');
}

function flashError(keys, err) {
    keys.forEach(function (key) {
        qsa('[data-value-for="' + key + '"]').forEach(function (el) {
            el.textContent = 'nicht gespeichert';
            el.classList.add('failed');
        });
    });
    notify('saveError', 'err', 'Änderung nicht gespeichert',
           reason(err) + ' Der angezeigte Wert ist wieder der der Wand.');
}

/** The current value, in the unit the user thinks in.
 *
 *  Always the label a person chose, never the token stored behind it: a
 *  segment shows its button's text, and a dropdown shows the option's — the
 *  Abfall picker used to report the calendar's database id.
 */
function showValue(key) {
    var raw = state.settings[key];
    qsa('[data-value-for="' + key + '"]').forEach(function (el) {
        var input = document.querySelector('[data-setting="' + key + '"]');
        var unit = input ? (input.getAttribute('data-unit') || '') : '';
        var seg = document.querySelector('[data-seg="' + key + '"]');
        if (seg) {
            var btn = seg.querySelector('button[data-val="' + raw + '"]');
            el.textContent = btn ? btn.textContent : (raw || '—');
            return;
        }
        if (input && input.tagName === 'SELECT') {
            var opt = input.options[input.selectedIndex];
            el.textContent = (opt && opt.textContent) || '—';
            return;
        }
        el.textContent = (raw === '' || raw === undefined || raw === null)
            ? '—' : raw + unit;
    });
}

/** Push one setting back into whatever control owns it. */
function applyOne(key) {
    var value = state.settings[key];
    qsa('[data-setting="' + key + '"]').forEach(function (el) {
        if (el.getAttribute('data-bool')) el.checked = String(value) === 'true';
        else el.value = value;
    });
    if (document.querySelector('[data-seg="' + key + '"]')) paintSegment(key);
    else showValue(key);
    if (EDITORS[key]) EDITORS[key].render();
}

//: Structured editors that own a whole settings string, keyed by that
//: setting. Registered by the editors themselves further down.
var EDITORS = {};

// ---------------------------------------------------------------
// Generic binding
// ---------------------------------------------------------------
function bindInputs() {
    qsa('[data-setting]').forEach(function (el) {
        // Controls that move together own their own handler: writing one
        // half of a linked pair on its own is the bug, not the feature.
        if (el.getAttribute('data-linked')) return;
        var key = el.getAttribute('data-setting');
        var evt = (el.type === 'range' || el.tagName === 'TEXTAREA'
                   || el.type === 'text' || el.type === 'number'
                   || el.type === 'search') ? 'input' : 'change';
        el.addEventListener(evt, function () {
            var value = el.getAttribute('data-bool') ? (el.checked ? 'true' : 'false')
                                                     : el.value;
            save(key, value);
        });
    });
}

function bindSegments() {
    qsa('[data-seg]').forEach(function (host) {
        var key = host.getAttribute('data-seg');
        var spec = host.getAttribute('data-options')
                || 'always:Immer|dynamic:Dynamisch|off:Aus';
        host.innerHTML = '';
        // These are radio buttons wearing a different coat, and until now the
        // only thing carrying the choice was a CSS class — invisible to a
        // screen reader on a page whose whole job is being opened on a phone.
        host.setAttribute('role', 'radiogroup');
        spec.split('|').forEach(function (pair) {
            var bits = pair.split(':');
            var b = document.createElement('button');
            b.type = 'button';
            b.setAttribute('role', 'radio');
            b.setAttribute('aria-checked', 'false');
            b.setAttribute('data-val', bits[0]);
            b.textContent = bits[1] || bits[0];
            b.addEventListener('click', function () {
                save(key, bits[0]);
                paintSegment(key);
                applyConditions();
                renderSetup();
            });
            host.appendChild(b);
        });
    });
}

function paintSegment(key) {
    var host = document.querySelector('[data-seg="' + key + '"]');
    if (!host) return;
    qsa('[data-seg="' + key + '"] button').forEach(function (b) {
        var on = b.getAttribute('data-val') === state.settings[key];
        b.classList.toggle('on', on);
        b.setAttribute('aria-checked', on ? 'true' : 'false');
    });
    showValue(key);
}

function applySettings(data) {
    state.settings = data;
    // What the wall actually holds, so a rejected write has something to
    // fall back to rather than leaving the page believing its own guess.
    state.saved = {};
    Object.keys(data).forEach(function (k) { state.saved[k] = String(data[k]); });
    qsa('[data-setting]').forEach(function (el) {
        var key = el.getAttribute('data-setting');
        var value = data[key];
        if (value === undefined) return;
        if (el.getAttribute('data-bool')) el.checked = String(value) === 'true';
        else el.value = value;
        showValue(key);
    });
    qsa('[data-seg]').forEach(function (h) { paintSegment(h.getAttribute('data-seg')); });
    Object.keys(EDITORS).forEach(function (k) { EDITORS[k].render(); });
    syncDensityPair();
    updateTimezoneHint();
    updateDensityExplanation();
    updateOverlayHint();
    applyConditions();
    renderSetup();
}

// ---------------------------------------------------------------
// Show a control only where changing it would do something
//
// A settings page that lists everything it can store makes the reader do
// the work of knowing which parts are live. The ÖPNV time windows mean
// nothing once the board is set to "Immer"; the night brightness means
// nothing unless night mode dims; half the PWM block means nothing unless
// the backlight is wired. Each of those is a fact the page already knows.
//
//   data-when="key=value"      one of, with | between alternatives
//   data-when="key!=value"     anything but
//   data-when="key~token"      a comma-separated list contains the token
//
// Deliberately no numeric comparison: it would need a > inside an attribute
// value, which is legal HTML and quietly truncates every naive parser that
// reads this file — including the one the tests use. "!=0" says the same
// thing about a slider that bottoms out at zero.
//
// Several conditions separated by spaces must all hold. One attribute, one
// evaluator — the same bargain as data-setting.
// ---------------------------------------------------------------
var CONDITION = /^([a-z0-9_]+)(!=|=|~)(.*)$/;

function conditionMet(expr) {
    return String(expr || '').trim().split(/\s+/).every(function (term) {
        var parts = CONDITION.exec(term);
        if (!parts) return true;
        var actual = state.settings[parts[1]];
        actual = String(actual === undefined || actual === null ? '' : actual);
        var wanted = parts[3];
        switch (parts[2]) {
            case '=':  return wanted.split('|').indexOf(actual) >= 0;
            case '!=': return wanted.split('|').indexOf(actual) < 0;
            case '~':  return csvList(actual).indexOf(wanted) >= 0;
        }
        return true;
    });
}

function applyConditions() {
    qsa('[data-when]').forEach(function (el) {
        el.hidden = !conditionMet(el.getAttribute('data-when'));
    });
}

// ---------------------------------------------------------------
// Setup status
//
// Someone opening this for the first time needs to know what already
// works and what still wants them. Without that, sixty controls all
// look equally urgent — and the only one that actually matters on day
// one is adding a calendar.
// ---------------------------------------------------------------
function renderSetup() {
    var host = $('setupCard');
    if (!host) return;
    var s = state.settings;

    var steps = [
        { id: 'calendars', label: 'Kalender',
          done: state.calendars.length > 0,
          stat: state.calendars.length
              ? state.calendars.length + (state.calendars.length === 1 ? ' Quelle' : ' Quellen')
              : 'noch keine' },
        { id: 'presence', label: 'Bewegungssensor',
          done: !!(state.presence && state.presence.daemon_running),
          // Whether distance is available decides if the wall can switch
          // between near and far at all, so it is worth saying here.
          stat: !(state.presence && state.presence.daemon_running) ? 'offline'
              : ((state.presence.distance_cm === null || state.presence.distance_cm === undefined)
                   ? 'ohne Entfernung' : 'mit Entfernung') },
        { id: 'widgets', label: 'Wetter',
          done: !!s.weather_lat,
          optional: true,
          stat: s.weather_lat ? (s.weather_place || 'eingerichtet') : 'optional' },
        { id: 'widgets', label: 'ÖPNV',
          done: !!s.transit_station_id,
          optional: true,
          stat: s.transit_station_id ? (s.transit_station_name || 'eingerichtet') : 'optional' }
    ];

    var required = steps.filter(function (x) { return !x.optional; });
    var firstRun = required.some(function (x) { return !x.done; });

    host.hidden = false;
    host.className = 'setup' + (firstRun ? ' first-run' : '');
    host.innerHTML = '';

    if (firstRun) {
        var intro = document.createElement('div');
        intro.className = 'intro';
        intro.innerHTML = '<b>Erst einmal einrichten</b><span></span>';
        intro.querySelector('span').textContent = state.calendars.length
            ? 'Fast fertig — der Rest unten ist Feinschliff.'
            : 'Ohne Kalender bleibt die Wand leer. Alles andere ist optional '
            + 'und kann warten.';
        host.appendChild(intro);
    }

    steps.forEach(function (step) {
        // Once everything essential is done the card shrinks to a status
        // line: optional items stop nagging about things nobody asked for.
        if (!firstRun && step.optional && !step.done) return;
        var a = document.createElement('a');
        a.className = 'step' + (step.done ? ' done' : '');
        a.href = '#' + step.id;
        a.innerHTML = '<span class="tick"></span><span class="label"></span>' +
                      '<span class="stat"></span><span class="chev">›</span>';
        a.querySelector('.label').textContent = step.label;
        a.querySelector('.stat').textContent = step.stat;
        host.appendChild(a);
    });

    // Adding a calendar is the one thing a new install cannot do without,
    // so open that panel rather than making them find it.
    var add = $('addCalDetails');
    if (add && !state.calendars.length && !add.dataset.nudged) {
        add.dataset.nudged = '1';
        add.open = true;
    }
}

// ---------------------------------------------------------------
// The two density thresholds are one control
//
// They were independent sliders, and the description under the second
// asked politely that it stay above the first. Nothing enforced it. Cross
// them and the dead band disappears: every distance resolves on one
// threshold, so somebody standing still at it with a few cm of radar
// jitter flips the layout back and forth. Measured, a drift of 10 cm went
// from zero switches to eleven in thirty seconds.
//
// So they push each other instead. You cannot express the broken state,
// and no dialog has to explain why it was refused.
// ---------------------------------------------------------------
var DENSITY_GAP_CM = 20;      // two slider steps: the narrowest useful band

/** A numeric attribute, or ``fallback`` when the control does not carry one.
 *  An absent bound is no bound — never a NaN that propagates into the value
 *  written to the wall. */
function attrNum(el, attr, fallback) {
    var parsed = parseFloat(el.getAttribute(attr));
    return isFinite(parsed) ? parsed : fallback;
}
function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

function bindDensityPair() {
    var nearEl = $('setNearCm'), farEl = $('setFarCm');
    if (!nearEl || !farEl) return;

    function settle(moved) {
        var near = parseFloat(nearEl.value);
        var far = parseFloat(farEl.value);
        var nLo = attrNum(nearEl, 'min', -Infinity);
        var nHi = attrNum(nearEl, 'max', Infinity);
        var fLo = attrNum(farEl, 'min', -Infinity);
        var fHi = attrNum(farEl, 'max', Infinity);

        // Move the one that was dragged first, then let the other give way
        // as far as its own range allows, then re-seat the first inside what
        // is left. Two passes always terminate.
        if (moved === 'near') {
            near = clamp(near, nLo, nHi);
            far = clamp(Math.max(far, near + DENSITY_GAP_CM), fLo, fHi);
            near = clamp(Math.min(near, far - DENSITY_GAP_CM), nLo, nHi);
        } else {
            far = clamp(far, fLo, fHi);
            near = clamp(Math.min(near, far - DENSITY_GAP_CM), nLo, nHi);
            far = clamp(Math.max(far, near + DENSITY_GAP_CM), fLo, fHi);
        }

        nearEl.value = near;
        farEl.value = far;
        saveMany({ density_near_cm: near, density_far_cm: far });
        updateDensityExplanation();
    }

    nearEl.addEventListener('input', function () { settle('near'); });
    farEl.addEventListener('input', function () { settle('far'); });
}

/** Bring a pair loaded from the wall into the same shape, without writing. */
function syncDensityPair() {
    var nearEl = $('setNearCm'), farEl = $('setFarCm');
    if (!nearEl || !farEl) return;
    var near = parseFloat(state.settings.density_near_cm);
    var far = parseFloat(state.settings.density_far_cm);
    if (isFinite(near) && isFinite(far) && far - near < DENSITY_GAP_CM) {
        // An installation that crossed them before this existed, or a
        // `wallcal.sh config set` that still can. Show the truth; the next
        // touch of either slider corrects it.
        nearEl.classList.add('conflict');
        farEl.classList.add('conflict');
    } else {
        nearEl.classList.remove('conflict');
        farEl.classList.remove('conflict');
    }
}

// ---------------------------------------------------------------
// Density: show the computed state and the band, so it is obvious why
// the feature is on or off.
// ---------------------------------------------------------------
function updateDensityExplanation() {
    var el = $('densityWhy');
    if (!el) return;
    var mode = state.settings.density_mode || 'auto';
    var wake = parseFloat(state.settings.sensor_distance_max_cm) || 300;
    var near = parseFloat(state.settings.density_near_cm) || 100;
    var minBand = parseFloat(state.settings.density_min_band_cm) || 80;
    var strategy = String(state.settings.display_off_strategy || 'hdmi');
    var never = strategy.indexOf('none') >= 0;
    var band = never ? (600 - near) : (wake - near);

    var hasDistance = state.presence
        && state.presence.distance_cm !== null
        && state.presence.distance_cm !== undefined;

    // Say which layout you end up with, always. The old wording described
    // the mechanism ("ausgeschaltet") and left the outcome — permanently the
    // near layout — for the user to discover on the wall.
    var text;
    if (mode === 'near' || mode === 'off') {
        text = 'Immer die Nahansicht: die Detailansicht, unabhängig vom Abstand. '
             + 'Der Sensor schaltet weiterhin den Bildschirm, nur nicht das Layout.';
    } else if (mode === 'far') {
        text = 'Immer die Fernansicht: große Uhr und der nächste Termin, '
             + 'unabhängig vom Abstand.';
    } else if (!hasDistance) {
        text = 'Wechselt je nach Abstand — zurzeit inaktiv: der Sensor liefert keine '
             + 'Entfernung (nur im UART-Modus verfügbar). Die Wand bleibt in der Nahansicht.';
    } else if (mode === 'on') {
        text = 'Wechselt je nach Abstand, unabhängig vom nutzbaren Bereich.';
    } else {
        text = 'Wechselt je nach Abstand. Nutzbarer Fernbereich: '
             + Math.round(band) + ' cm (nötig: ' + minBand + ' cm). '
             + (band >= minBand
                  ? 'Aktiv.'
                  : 'Zu schmal, daher inaktiv — die Wand bleibt in der Nahansicht.');
    }
    el.textContent = text;
}

/** What time it actually is in the chosen zone.
 *
 *  A timezone is the one setting whose effect you cannot see from its own
 *  name: "Europe/Berlin" and "Europe/London" look equally plausible, and the
 *  wall is in another room. Showing the clock closes that loop here.
 */
function updateTimezoneHint() {
    var el = $('tzNow');
    if (!el) return;
    var chosen = String(state.settings.timezone || 'auto');
    var explicit = chosen && chosen.toLowerCase() !== 'auto';
    try {
        var opts = { hour: '2-digit', minute: '2-digit' };
        if (explicit) opts.timeZone = chosen;
        var clock = new Intl.DateTimeFormat(state.settings.locale || 'de-DE', opts)
            .format(new Date());
        var named = chosen;
        if (!explicit) {
            named = 'Gerät';
            try {
                named = new Intl.DateTimeFormat().resolvedOptions().timeZone || 'Gerät';
            } catch (e) {}
        }
        el.textContent = 'Dort ist es jetzt ' + clock + ' (' + named + ').';
    } catch (e) {
        // Intl throws on a zone it does not know, which is the clearest
        // possible signal that the name is wrong.
        el.textContent = 'Unbekannte Zeitzone — die Wand nutzt die des Geräts.';
    }
}

function updateOverlayHint() {
    var el = $('pwmOverlay');
    if (!el) return;
    var pin = parseInt(state.settings.pwm_gpio, 10);
    var func = { 12: 4, 13: 4, 18: 2, 19: 2 }[pin];
    el.textContent = func
        ? 'Benötigt in config.txt: dtoverlay=pwm,pin=' + pin + ',func=' + func
        : 'GPIO ' + pin + ' hat kein Hardware-PWM. Möglich: 12, 13, 18, 19.';

    var conflict = $('pinConflict');
    if (!conflict) return;
    var sensorPin = parseInt(state.settings.sensor_gpio_pin, 10);
    var usesPwm = String(state.settings.display_off_strategy || '').indexOf('pwm') >= 0;
    conflict.textContent = (usesPwm && sensorPin === pin)
        ? 'Konflikt: Sensor und Hintergrundbeleuchtung liegen beide auf GPIO ' + pin + '.'
        : '';
}

// ---------------------------------------------------------------
// The radar meter — the signature element
// ---------------------------------------------------------------
var METER_MAX_CM = 600;   // the LD2410's own ceiling: 8 gates x 75 cm

var meterTicks = 0;

function renderMeter(s) {
    var wasRunning = state.presence && state.presence.daemon_running;
    state.presence = s;
    // The setup card carries sensor status; refresh it when that flips,
    // rather than rebuilding the card twice a second.
    if (!!wasRunning !== !!(s && s.daemon_running)) renderSetup();
    var dist = s && s.distance_cm;
    $('meterDist').textContent = (dist === null || dist === undefined) ? '—' : Math.round(dist);

    var badge = $('meterState');
    if (!s || !s.daemon_running) {
        badge.textContent = 'offline'; badge.classList.remove('live');
    } else if (s.present) {
        badge.textContent = 'erkannt'; badge.classList.add('live');
    } else {
        badge.textContent = 'niemand da'; badge.classList.remove('live');
    }

    $('meterFill').style.width =
        Math.max(0, Math.min(100, ((dist || 0) / METER_MAX_CM) * 100)) + '%';

    mark('markWake', state.settings.sensor_distance_max_cm);
    mark('markNear', state.settings.density_near_cm);
    mark('markFar', state.settings.density_far_cm);
    paintOverride(s && s.override);
    updateDensityExplanation();
}

// ---------------------------------------------------------------
// Manual override
//
// It was write-only: the label said "automatisch" because the markup said
// so, and no button was ever lit from the wall's own state. Force the panel
// on, come back later, and the page claimed the sensor was in charge while
// the wall burned all night. It is a sticky mode, so it is shown from
// anywhere and can be left from anywhere.
// ---------------------------------------------------------------
var OVERRIDE_WORDS = { auto: 'automatisch', on: 'immer an', off: 'aus' };

function paintOverride(mode) {
    mode = OVERRIDE_WORDS[mode] ? mode : 'auto';
    qsa('#overrideSeg button').forEach(function (b) {
        var on = b.getAttribute('data-mode') === mode;
        b.classList.toggle('on', on);
        b.setAttribute('aria-checked', on ? 'true' : 'false');
    });
    var value = $('overrideValue');
    if (value) value.textContent = OVERRIDE_WORDS[mode];

    var note = $('overrideNote');
    if (note) {
        note.textContent = mode === 'auto' ? ''
            : 'Bleibt so, bis du auf „Automatisch“ zurückstellst — der Sensor '
              + 'schaltet die Anzeige solange nicht.';
    }

    // The buttons live inside a collapsed <details>. Somebody who set this
    // last week is not going to open it to find out why the wall never
    // sleeps, so the banner sits above everything with the way out on it.
    var banner = $('overrideBanner');
    if (!banner) return;
    if (mode === 'auto') { banner.hidden = true; banner.innerHTML = ''; return; }
    if (banner.dataset.mode === mode) return;
    banner.dataset.mode = mode;
    banner.hidden = false;
    banner.innerHTML = '<b></b><span></span>';
    banner.querySelector('b').textContent =
        mode === 'on' ? 'Anzeige ist auf „immer an“' : 'Anzeige ist auf „aus“';
    banner.querySelector('span').textContent =
        'Die Bewegungserkennung ist übersteuert.';
    var back = document.createElement('button');
    back.type = 'button';
    back.className = 'btn ghost';
    back.textContent = 'Automatik zurück';
    back.addEventListener('click', function () {
        back.disabled = true;
        post('/api/presence/override', { mode: 'auto' }, function (err) {
            back.disabled = false;
            if (err) { notify('saveError', 'err', 'Nicht zurückgestellt', reason(err)); return; }
            paintOverride('auto');
        });
    });
    banner.appendChild(back);
}

function mark(id, cm) {
    var el = $(id);
    if (!el) return;
    var pct = Math.max(0, Math.min(100, (parseFloat(cm) || 0) / METER_MAX_CM * 100));
    el.style.left = pct + '%';
}

// ---------------------------------------------------------------
// How often to ask
//
// This page polled the wall every 700 ms for as long as it stayed open —
// on a phone left on a bench, forever. The meter genuinely wants that
// cadence while somebody is standing in front of the wall watching it
// move, and nothing at all when the page is in a background tab.
// ---------------------------------------------------------------
var METER_FAST_MS = 700;      // the meter is on screen and being watched
var METER_SLOW_MS = 5000;     // page open, meter scrolled away
var presenceTimer = null;
var presenceEvery = -1;
var meterOnScreen = true;

function pollPresence() {
    get('/api/presence/live', function (err, data) { if (!err) renderMeter(data); });
}

function presenceCadence() {
    if (document.hidden) return 0;               // nobody is looking at all
    return meterOnScreen ? METER_FAST_MS : METER_SLOW_MS;
}

function schedulePresence() {
    var every = presenceCadence();
    if (every === presenceEvery) return;
    presenceEvery = every;
    if (presenceTimer) { clearInterval(presenceTimer); presenceTimer = null; }
    if (!every) return;
    pollPresence();                              // do not wait out the gap
    presenceTimer = setInterval(pollPresence, every);
}

function watchAttention() {
    document.addEventListener('visibilitychange', schedulePresence);

    var track = $('meterTrack');
    if (track && typeof IntersectionObserver === 'function') {
        new IntersectionObserver(function (entries) {
            meterOnScreen = entries[0].isIntersecting;
            schedulePresence();
        }, { rootMargin: '80px' }).observe(track);
    }
    schedulePresence();
}

// ---------------------------------------------------------------
// Calibration — the moment worth polishing most
// ---------------------------------------------------------------
var calibTimer = null;

function showCalib(which) {
    ['calibIdle', 'calibRun', 'calibDone'].forEach(function (id) {
        $(id).hidden = (id !== which);
    });
}

function renderCalib(s) {
    if (!s) return;
    if (s.state === 'countdown') {
        showCalib('calibRun');
        $('calibCount').textContent = s.remaining;
        $('calibMsg').textContent = 'Stell dich dorthin, wo die Wand aufwachen soll';
    } else if (s.state === 'sampling') {
        showCalib('calibRun');
        $('calibCount').textContent = s.remaining;
        $('calibMsg').textContent = 'Messung läuft — stillhalten';
    } else if (s.state === 'done' && s.result) {
        showCalib('calibDone');
        $('calibResult').textContent = s.result.suggested_distance_max_cm + ' cm';
        stopCalib();
    } else if (s.state === 'failed') {
        showCalib('calibIdle');
        $('calibError').innerHTML = '';
        var box = document.createElement('div');
        box.className = 'err';
        box.innerHTML = '<b>Zu wenige Erkennungen</b>';
        box.appendChild(document.createTextNode(s.error || ''));
        $('calibError').appendChild(box);
        stopCalib();
    } else if (s.state === 'cancelled') {
        showCalib('calibIdle');
        stopCalib();
    }
    $('calibNow').textContent = s.current_distance_cm || '—';
    $('calibMax').textContent = s.furthest_seen_cm || '—';
    $('calibFrames').textContent = s.samples || 0;
}

function stopCalib() { if (calibTimer) { clearInterval(calibTimer); calibTimer = null; } }

/** Watch a run that is already going. Gives up rather than polling a wall
 *  that has stopped answering, which the old loop would have done forever. */
function startCalibPoll() {
    stopCalib();
    var misses = 0;
    calibTimer = setInterval(function () {
        get('/api/sensor/calibrate', function (err, s) {
            if (err) {
                if (++misses >= 10) {
                    stopCalib();
                    showCalib('calibIdle');
                    notify('calibError', 'err', 'Verbindung zur Messung verloren', reason(err));
                }
                return;
            }
            misses = 0;
            renderCalib(s);
        });
    }, 500);
}

/** A calibration started before a reload is still running on the wall.
 *  The page used to come back showing an idle panel while the daemon was
 *  mid-countdown, and starting a second run was the obvious next move. */
function resumeCalibration() {
    get('/api/sensor/calibrate', function (err, s) {
        if (err || !s) return;
        if (s.state === 'countdown' || s.state === 'sampling') {
            renderCalib(s);
            startCalibPoll();
        } else if (s.state === 'done' && s.result) {
            renderCalib(s);
        }
    });
}


// ---------------------------------------------------------------
// Structured editors
//
// Two settings used to be textareas holding a private format —
// "mon=06:30-09:00,16:00-18:30|sat=" and "rest=Restmüll:#8A8F9C|bio=…".
// Both parse server-side and both failed silently: a typo meant the widget
// simply never appeared, with nothing anywhere saying why. You cannot type
// a syntax error into a time picker.
//
// The stored format is unchanged, so `wallcal.sh config set` and every
// existing installation still work.
// ---------------------------------------------------------------

var DAYS = [['mon', 'Montag'], ['tue', 'Dienstag'], ['wed', 'Mittwoch'],
            ['thu', 'Donnerstag'], ['fri', 'Freitag'], ['sat', 'Samstag'],
            ['sun', 'Sonntag']];

function minutesOf(hhmm) {
    var bits = String(hhmm || '').split(':');
    var h = parseInt(bits[0], 10), m = parseInt(bits[1], 10);
    return (isFinite(h) && isFinite(m)) ? h * 60 + m : null;
}

function hhmmOf(minutes) {
    minutes = Math.max(0, Math.min(23 * 60 + 59, minutes));
    var h = Math.floor(minutes / 60), m = minutes % 60;
    return (h < 10 ? '0' : '') + h + ':' + (m < 10 ? '0' : '') + m;
}

/** Delimiters of the stored format. A label carrying one would corrupt it,
 *  which the textarea let you do and had no way to report. */
function clean(text) { return String(text || '').replace(/[|:=,]/g, '').trim(); }

function button(className, label, ariaLabel, onClick) {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = className;
    b.textContent = label;
    b.setAttribute('aria-label', ariaLabel);
    b.addEventListener('click', onClick);
    return b;
}

// --- ÖPNV: per-weekday time windows -----------------------------
var windowEditor = (function () {
    var model = {};        // day -> [[fromMin, toMin], …]
    var host = null;

    function parse(spec) {
        var out = {};
        DAYS.forEach(function (d) { out[d[0]] = []; });
        String(spec || '').split('|').forEach(function (part) {
            var bits = part.split('=');
            var day = String(bits[0] || '').trim().toLowerCase();
            if (!(day in out)) return;
            String(bits[1] || '').split(',').forEach(function (range) {
                var ends = range.split('-');
                var a = minutesOf(ends[0]), b = minutesOf(ends[1]);
                // b < a runs past midnight — a night bus, not a mistake.
                // Dropping those here meant opening this page and touching
                // an unrelated day silently deleted them from the config.
                if (a === null || b === null || a === b) return;
                out[day].push([a, b]);
            });
        });
        return out;
    }

    function serialize() {
        return DAYS.map(function (d) {
            var ranges = (model[d[0]] || []).slice().sort(function (x, y) { return x[0] - y[0]; });
            return d[0] + '=' + ranges.map(function (r) {
                return hhmmOf(r[0]) + '-' + hhmmOf(r[1]);
            }).join(',');
        }).join('|');
    }

    function commit() { save('transit_windows', serialize()); summarise(); }

    /** The chip beside the heading: how many days are covered at all. */
    function summarise() {
        var days = DAYS.filter(function (d) { return (model[d[0]] || []).length; }).length;
        state.settings.transit_windows = serialize();
        qsa('[data-value-for="transit_windows"]').forEach(function (el) {
            el.textContent = days === 0 ? 'nie' : days + ' von 7 Tagen';
        });
    }

    function drawRange(day, index) {
        var range = model[day][index];
        var row = document.createElement('div');
        row.className = 'win-range';

        function timeInput(which) {
            var input = document.createElement('input');
            input.type = 'time';
            input.value = hhmmOf(range[which]);
            input.setAttribute('aria-label', which === 0 ? 'von' : 'bis');
            input.addEventListener('change', function () {
                var value = minutesOf(input.value);
                if (value === null) { input.value = hhmmOf(range[which]); return; }
                range[which] = value;
                // An end before the start is allowed and labelled: that is
                // how you say "until 02:00 the next day". Only a zero-length
                // window means nothing at all, so that one gives way.
                if (range[0] === range[1]) {
                    if (which === 0) range[1] = (range[0] + 30) % 1440;
                    else range[0] = (range[1] - 30 + 1440) % 1440;
                }
                draw();
                commit();
            });
            return input;
        }

        row.appendChild(timeInput(0));
        var dash = document.createElement('span');
        dash.className = 'dash';
        dash.textContent = '–';
        row.appendChild(dash);
        row.appendChild(timeInput(1));
        if (range[1] < range[0]) {
            var over = document.createElement('span');
            over.className = 'win-overnight';
            over.textContent = 'Folgetag';
            over.title = 'Dieses Fenster läuft über Mitternacht';
            row.appendChild(over);
        }
        row.appendChild(button('icon-btn', '×', 'Zeitfenster entfernen', function () {
            model[day].splice(index, 1);
            draw();
            commit();
        }));
        return row;
    }

    function draw() {
        if (!host) return;
        host.innerHTML = '';
        DAYS.forEach(function (d) {
            var day = d[0];
            var block = document.createElement('div');
            block.className = 'win-day' + (model[day].length ? '' : ' empty');

            var head = document.createElement('div');
            head.className = 'win-day-head';
            var name = document.createElement('span');
            name.className = 'win-day-name';
            name.textContent = d[1];
            head.appendChild(name);
            head.appendChild(button('icon-btn add', '+',
                                    d[1] + ': Zeitfenster hinzufügen', function () {
                var last = model[day][model[day].length - 1];
                var from = last ? Math.min(22 * 60, last[1] + 60) : 7 * 60;
                model[day].push([from, Math.min(23 * 60 + 59, from + 120)]);
                draw();
                commit();
            }));
            block.appendChild(head);

            if (!model[day].length) {
                var none = document.createElement('p');
                none.className = 'win-none';
                none.textContent = 'wird nicht angezeigt';
                block.appendChild(none);
            } else {
                model[day].forEach(function (_, i) { block.appendChild(drawRange(day, i)); });
            }
            host.appendChild(block);
        });
    }

    return {
        mount: function () {
            host = $('windowEditor');
            var copy = $('windowCopy');
            if (copy) copy.addEventListener('click', function () {
                // Five identical weekday windows typed on a phone is where
                // people give up and leave the default in place.
                ['tue', 'wed', 'thu', 'fri'].forEach(function (day) {
                    model[day] = model.mon.map(function (r) { return r.slice(); });
                });
                draw();
                commit();
            });
        },
        render: function () {
            model = parse(state.settings.transit_windows);
            draw();
            summarise();
        }
    };
})();

// --- Abfall: match term -> name and colour -----------------------
var fractionEditor = (function () {
    var model = [];        // [{needle, label, color}, …]
    var host = null;

    function parse(spec) {
        var out = [];
        String(spec || '').split('|').forEach(function (part) {
            var bits = part.split('=');
            var needle = clean(bits[0]);
            var rest = String(bits[1] || '');
            var cut = rest.lastIndexOf(':');
            var label = clean(cut >= 0 ? rest.slice(0, cut) : rest);
            var color = (cut >= 0 ? rest.slice(cut + 1) : '').trim() || '#8A8F9C';
            if (!/^#[0-9a-fA-F]{6}$/.test(color)) color = '#8A8F9C';
            if (needle && label) out.push({ needle: needle, label: label, color: color });
        });
        return out;
    }

    function serialize() {
        return model.filter(function (f) { return f.needle && f.label; })
                    .map(function (f) { return f.needle + '=' + f.label + ':' + f.color; })
                    .join('|');
    }

    function commit() {
        save('abfall_fractions', serialize());
        summarise();
    }

    function summarise() {
        var complete = model.filter(function (f) { return f.needle && f.label; }).length;
        state.settings.abfall_fractions = serialize();
        qsa('[data-value-for="abfall_fractions"]').forEach(function (el) {
            el.textContent = complete
                ? complete + (complete === 1 ? ' Tonne' : ' Tonnen')
                : 'keine';
        });
    }

    function drawRow(index) {
        var f = model[index];
        var row = document.createElement('div');
        row.className = 'frac-row' + ((f.needle && f.label) ? '' : ' incomplete');

        var color = document.createElement('input');
        color.type = 'color';
        color.value = f.color;
        color.className = 'frac-color';
        color.setAttribute('aria-label', 'Farbe');
        color.addEventListener('change', function () { f.color = color.value; commit(); });
        row.appendChild(color);

        function textField(field, placeholder, label) {
            var input = document.createElement('input');
            input.type = 'text';
            input.value = f[field];
            input.placeholder = placeholder;
            input.setAttribute('aria-label', label);
            input.className = 'frac-' + field;
            input.addEventListener('input', function () {
                var value = clean(input.value);
                if (value !== input.value) input.value = value;   // delimiters
                f[field] = value;
                row.classList.toggle('incomplete', !(f.needle && f.label));
                commit();
            });
            return input;
        }

        row.appendChild(textField('needle', 'rest', 'Suchbegriff im Termintitel'));
        row.appendChild(textField('label', 'Restmüll', 'Anzeigename'));
        row.appendChild(button('icon-btn', '×', 'Fraktion entfernen', function () {
            model.splice(index, 1);
            draw();
            commit();
        }));
        return row;
    }

    function draw() {
        if (!host) return;
        host.innerHTML = '';
        if (!model.length) {
            var empty = document.createElement('p');
            empty.className = 'win-none';
            empty.textContent = 'Keine Fraktionen — das Banner bleibt grau.';
            host.appendChild(empty);
        }
        model.forEach(function (_, i) { host.appendChild(drawRow(i)); });
    }

    return {
        mount: function () {
            host = $('fractionEditor');
            var add = $('fractionAdd');
            if (add) add.addEventListener('click', function () {
                model.push({ needle: '', label: '', color: '#8A8F9C' });
                draw();
                var last = host.querySelector('.frac-row:last-child .frac-needle');
                if (last) last.focus();
            });
        },
        render: function () {
            model = parse(state.settings.abfall_fractions);
            draw();
            summarise();
        }
    };
})();

EDITORS.transit_windows = windowEditor;
EDITORS.abfall_fractions = fractionEditor;


// ---------------------------------------------------------------
// Display: pick from what exists, do not spell it
//
// These were two free-text fields holding comma-separated enums. Typing
// "hmdi" left the wall unable to switch its panel off and reported nothing:
// parse_strategy drops unknown names with a log line nobody reads, and the
// value chip echoed the typo back as though it had been understood.
//
// The strategies are four known values, so they are four checkboxes. The
// backends are whatever this particular Pi has, and there is an endpoint
// that goes and looks — so we ask it rather than making the user guess.
// ---------------------------------------------------------------

function csvList(value) {
    return String(value || '').split(',')
        .map(function (s) { return s.trim().toLowerCase(); })
        .filter(Boolean);
}

/** One checkbox row: box, name, and a line on what it actually does. */
function pickerRow(value, label, desc, checked, disabled, onToggle) {
    var row = document.createElement('label');
    row.className = 'pick' + (disabled ? ' off' : '');

    var box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = !!checked;
    box.disabled = !!disabled;
    box.value = value;
    box.addEventListener('change', function () { onToggle(box.checked); });
    row.appendChild(box);

    var text = document.createElement('span');
    var name = document.createElement('b');
    name.textContent = label;
    text.appendChild(name);
    if (desc) {
        var small = document.createElement('small');
        small.textContent = desc;
        text.appendChild(small);
    }
    row.appendChild(text);
    return row;
}

var STRATEGIES = [
    ['hdmi', 'Ausgang abschalten', 'DPMS oder CEC — für Monitore und Fernseher.'],
    ['pwm', 'Hintergrundbeleuchtung', 'Hardware-PWM auf der BL_PWM-Leitung.'],
    ['css', 'Im Browser abdunkeln', 'Ohne Verkabelung; der Bildschirm bleibt an.'],
    ['none', 'Nie abschalten', 'Stattdessen übernimmt der Bildschirmschoner.']
];

var strategyPicker = (function () {
    var host = null;

    function draw() {
        if (!host) return;
        var chosen = csvList(state.settings.display_off_strategy);
        var never = chosen.indexOf('none') >= 0;
        host.innerHTML = '';

        STRATEGIES.forEach(function (spec) {
            var value = spec[0];
            var isNone = value === 'none';
            // "none" wins over everything in the daemon, so the UI cannot
            // offer it as one ingredient among others without lying.
            var disabled = never && !isNone;
            host.appendChild(pickerRow(
                value, spec[1], spec[2], chosen.indexOf(value) >= 0, disabled,
                function (on) { toggle(value, on); }));
        });

        var note = $('strategyNote');
        if (note) {
            note.textContent = never
                ? 'Der Bildschirm bleibt an; die anderen Methoden sind damit gegenstandslos.'
                : '';
        }
    }

    function toggle(value, on) {
        var chosen = csvList(state.settings.display_off_strategy);

        function without(name) {
            return chosen.filter(function (x) { return x !== name; });
        }

        if (value === 'none') {
            chosen = on ? ['none'] : ['hdmi'];
        } else if (on) {
            chosen = without('none').concat([value]);
        } else {
            chosen = without(value);
            // Nothing checked reads as "never off" to a person and as "hdmi"
            // to parse_strategy. Neither guess is worth making.
            if (!chosen.length) {
                notify('saveError', 'err', 'Mindestens eine Methode',
                       'Ohne Methode wüsste die Wand nicht, wie sie dunkel werden soll.');
                draw();
                return;
            }
        }
        // Keep the daemon's own order so the stored value stays comparable.
        var ordered = STRATEGIES.map(function (s) { return s[0]; })
            .filter(function (name) { return chosen.indexOf(name) >= 0; });
        save('display_off_strategy', ordered.join(','));
        draw();
        updateDensityExplanation();     // "none" changes the usable band
    }

    return {
        mount: function () { host = $('strategyPicker'); },
        render: draw
    };
})();

var backendPicker = (function () {
    var host = null;
    var surveyed = null;      // null until this Pi has actually been asked

    function draw() {
        if (!host) return;
        var chosen = csvList(state.settings.display_backend);
        var auto = !chosen.length || chosen.indexOf('auto') >= 0;
        host.innerHTML = '';

        host.appendChild(pickerRow('auto', 'Automatisch',
            'Nimmt das beste vorhandene und rüstet nach, sobald eine Sitzung da ist.',
            auto, false, function (on) { save('display_backend', on ? 'auto' : ''); draw(); }));

        // Before the probe we only know what is configured. Inventing a list
        // of backend names here would be a second copy of the daemon's.
        var names = surveyed
            ? surveyed.map(function (b) { return b.name; })
            : chosen.filter(function (x) { return x !== 'auto'; });

        names.forEach(function (name) {
            var info = surveyed && surveyed.filter(function (b) { return b.name === name; })[0];
            var desc = info
                ? (info.description || '') + (info.available ? '' : ' — hier nicht gefunden')
                : '';
            host.appendChild(pickerRow(name, name, desc,
                chosen.indexOf(name) >= 0, auto,
                function (on) { toggle(name, on); }));
        });

        if (surveyed && !names.length) {
            var empty = document.createElement('p');
            empty.className = 'win-none';
            empty.textContent = 'Kein Backend gefunden — läuft schon eine Sitzung?';
            host.appendChild(empty);
        }
    }

    function toggle(name, on) {
        var chosen = csvList(state.settings.display_backend)
            .filter(function (x) { return x !== 'auto' && x !== name; });
        if (on) chosen.push(name);
        save('display_backend', chosen.length ? chosen.join(',') : 'auto');
        draw();
    }

    return {
        mount: function () {
            host = $('backendPicker');
            var probe = $('backendDetect');
            if (probe) probe.addEventListener('click', function () {
                probe.disabled = true;
                probe.textContent = 'Wird gesucht…';
                // The survey opens every backend in turn; it is genuinely
                // slow, which is why it happens on request and not on load.
                get('/api/display/backends', function (err, data) {
                    probe.disabled = false;
                    probe.textContent = 'Erneut suchen';
                    if (err || !data || !data.backends) {
                        notify('saveError', 'err', 'Suche fehlgeschlagen', reason(err));
                        return;
                    }
                    surveyed = data.backends;
                    draw();
                });
            });
        },
        render: draw,
        /** What the daemon actually ended up using, which is not always what
         *  was asked for — the fallback backends answer before a compositor
         *  exists and get upgraded later. */
        reportActive: function (info) {
            var note = $('backendNote');
            if (!note) return;
            var live = (info && info.backends) || [];
            note.textContent = live.length
                ? 'Aktiv laut Dienst: ' + live.join(', ')
                : '';
        }
    };
})();

EDITORS.display_off_strategy = strategyPicker;
EDITORS.display_backend = backendPicker;

// ---------------------------------------------------------------
// Calendars
// ---------------------------------------------------------------
function renderCalendars() {
    var host = $('calList');
    var picker = $('setAbfallCal');
    host.innerHTML = '';
    picker.innerHTML = '<option value="">— keiner —</option>';

    if (!state.calendars.length) {
        host.innerHTML = '<div class="empty-state"><b>Noch keine Kalender</b>' +
            'Füge eine CalDAV-Quelle hinzu, damit auf der Wand etwas steht.</div>';
        return;
    }

    state.calendars.forEach(function (cal) {
        var item = document.createElement('div');
        item.className = 'cal-item';

        var dot = document.createElement('input');
        dot.type = 'color'; dot.value = cal.color; dot.className = 'dot';
        dot.style.width = '32px'; dot.style.height = '32px'; dot.style.padding = '0';
        dot.style.border = 'none'; dot.style.background = 'none';
        dot.addEventListener('change', function () {
            api('PUT', '/api/calendars/' + cal.id, { color: dot.value }, function (err) {
                if (err) notify('calFeedback', 'err', 'Farbe nicht gespeichert', reason(err));
            });
        });
        item.appendChild(dot);

        var who = document.createElement('div');
        who.className = 'who';
        who.innerHTML = '<b></b><span></span>';
        who.querySelector('b').textContent = cal.name;
        who.querySelector('span').textContent = cal.caldav_url;
        item.appendChild(who);

        var del = document.createElement('button');
        del.className = 'btn ghost';
        del.textContent = 'Entfernen';
        del.addEventListener('click', function () {
            if (!confirm('„' + cal.name + '“ entfernen? Die zwischengespeicherten Termine gehen mit.')) return;
            api('DELETE', '/api/calendars/' + cal.id, null, loadCalendars);
        });
        item.appendChild(del);
        host.appendChild(item);

        var opt = document.createElement('option');
        opt.value = String(cal.id);
        opt.textContent = cal.name;
        picker.appendChild(opt);
    });
    picker.value = state.settings.abfall_calendar_id || '';
}

function loadCalendars() {
    get('/api/calendars', function (err, data) {
        if (err) return;
        state.calendars = data.calendars || [];
        renderCalendars();
        renderSetup();
    });
}

// ---------------------------------------------------------------
// Search pickers
// ---------------------------------------------------------------
function wireSearch(inputId, resultsId, url, onPick, render) {
    var timer = null;
    $(inputId).addEventListener('input', function () {
        var q = this.value.trim();
        clearTimeout(timer);
        if (q.length < 2) { $(resultsId).innerHTML = ''; return; }
        timer = setTimeout(function () {
            get(url + encodeURIComponent(q), function (err, data) {
                var host = $(resultsId);
                host.innerHTML = '';
                if (err || (data && data.error)) {
                    var box = document.createElement('div');
                    box.className = 'err';
                    box.innerHTML = '<b>Suche nicht erreichbar</b>';
                    box.appendChild(document.createTextNode(
                        (data && data.error) || 'Prüfe die Netzwerkverbindung der Wand.'));
                    host.appendChild(box);
                    return;
                }
                render(host, data, onPick);
            });
        }, 350);
    });
}

function renderPicks(host, rows, onPick) {
    if (!rows.length) {
        host.innerHTML = '<div class="empty-state">Nichts gefunden. Anders schreiben?</div>';
        return;
    }
    rows.slice(0, 8).forEach(function (row) {
        var b = document.createElement('button');
        b.type = 'button';
        b.textContent = row.name;
        if (row.area) {
            var small = document.createElement('small');
            small.textContent = row.area;
            b.appendChild(small);
        }
        b.addEventListener('click', function () { onPick(row); host.innerHTML = ''; });
        host.appendChild(b);
    });
}

// ---------------------------------------------------------------
// System
// ---------------------------------------------------------------
function loadSystem() {
    get('/api/system', function (err, info) {
        if (err || !info) return;
        var host = $('diag');
        host.innerHTML = '';
        function tile(label, value, cls) {
            var d = document.createElement('div');
            if (cls) d.className = cls;
            d.innerHTML = '<b></b><span></span>';
            d.querySelector('b').textContent = value;
            d.querySelector('span').textContent = label;
            host.appendChild(d);
        }
        if (info.cpu_temp_c !== undefined)
            tile('CPU-Temperatur', info.cpu_temp_c + ' °C',
                 info.cpu_temp_c > 75 ? 'warn' : '');
        if (info.under_voltage_since_boot !== undefined)
            tile('Spannung', info.under_voltage_now ? 'zu niedrig'
                 : (info.under_voltage_since_boot ? 'war zu niedrig' : 'in Ordnung'),
                 info.under_voltage_now ? 'bad' : (info.under_voltage_since_boot ? 'warn' : ''));
        if (info.disk_free_mb !== undefined)
            tile('Speicher frei', Math.round(info.disk_free_mb / 1024 * 10) / 10 + ' GB',
                 info.disk_percent_used > 90 ? 'warn' : '');
        if (info.memory_percent_used !== undefined)
            tile('RAM benutzt', info.memory_percent_used + ' %', '');
        if (info.uptime_seconds !== undefined)
            tile('Laufzeit', Math.round(info.uptime_seconds / 3600) + ' h', '');
    });

    get('/api/status', function (err, data) {
        if (err || !data) return;
        var host = $('feedHealth');
        host.innerHTML = '';
        var feeds = data.feeds || [];
        if (!feeds.length) {
            host.innerHTML = '<div class="empty-state">Noch keine externen Datenquellen aktiv.</div>';
            return;
        }
        feeds.forEach(function (f) {
            var row = document.createElement('div');
            row.className = 'row';
            var age = f.age_seconds === null ? '—'
                : (f.age_seconds < 90 ? Math.round(f.age_seconds) + ' s'
                                      : Math.round(f.age_seconds / 60) + ' min');
            row.innerHTML = '<div class="row-head"><span class="name"></span>' +
                            '<span class="value"></span></div><p class="desc"></p>';
            row.querySelector('.name').textContent = f.feed;
            row.querySelector('.value').textContent = age;
            row.querySelector('.desc').textContent = f.ok
                ? (f.stale ? 'Älter als erwartet — die Wand zeigt weiter die letzten Daten.'
                           : 'Aktuell.')
                : ('Letzter Abruf fehlgeschlagen: ' + (f.error || 'unbekannt'));
            host.appendChild(row);
        });
    });

    get('/api/display', function (err, info) {
        if (!err) backendPicker.reportActive(info);
    });

    get('/api/prewake', function (err, data) {
        var el = $('prewakeNext');
        if (err || !el) return;
        el.textContent = (data && data.next)
            ? 'Nächstes Wecken: ' + data.next.from.replace('T', ' ') + ' — ' + data.next.label
            : '';
    });
}

// ---------------------------------------------------------------
// Init
// ---------------------------------------------------------------
function init() {
    $('hostLine').textContent = window.location.host;
    bindSegments();
    bindInputs();
    bindDensityPair();
    windowEditor.mount();
    fractionEditor.mount();
    strategyPicker.mount();
    backendPicker.mount();

    get('/api/settings', function (err, data) {
        if (!err) { applySettings(data); loadCalendars(); }
    });
    loadSystem();
    watchAttention();
    resumeCalibration();
    setInterval(loadSystem, 30000);

    // Scroll-spy on the section jumper.
    var links = qsa('.jumper a');
    var sections = links.map(function (a) { return $(a.getAttribute('href').slice(1)); });
    function spy() {
        var best = 0;
        sections.forEach(function (s, i) {
            if (s && s.getBoundingClientRect().top <= 90) best = i;
        });
        links.forEach(function (a, i) { a.classList.toggle('on', i === best); });
    }
    // getBoundingClientRect on every section, on every scroll event, is how
    // a settings page comes to feel heavier than the wall it configures.
    var raf = (typeof requestAnimationFrame === 'function')
        ? requestAnimationFrame : function (fn) { return setTimeout(fn, 16); };
    var spyQueued = false;
    window.addEventListener('scroll', function () {
        if (spyQueued) return;
        spyQueued = true;
        raf(function () { spyQueued = false; spy(); });
    }, { passive: true });
    spy();

    // --- Calendars ---
    $('calTest').addEventListener('click', function () {
        var box = $('calFeedback');
        box.innerHTML = '';
        post('/api/test-connection', {
            caldav_url: $('calUrl').value, username: $('calUser').value,
            password: $('calPass').value
        }, function (err, data) {
            var div = document.createElement('div');
            if (!err && data && data.ok) {
                div.className = 'ok-note';
                div.textContent = data.message;
            } else {
                div.className = 'err';
                div.innerHTML = '<b>Verbindung fehlgeschlagen</b>';
                div.appendChild(document.createTextNode(
                    ((data && data.error) || err || '') +
                    ' — prüfe die Adresse und ob der Benutzername ein App-Passwort braucht.'));
            }
            box.appendChild(div);
        });
    });

    $('calDiscover').addEventListener('click', function () {
        var host = $('calResults');
        host.innerHTML = '';
        post('/api/discover', {
            caldav_url: $('calUrl').value, username: $('calUser').value,
            password: $('calPass').value
        }, function (err, data) {
            if (err || !data || !data.calendars) {
                var div = document.createElement('div');
                div.className = 'err';
                div.innerHTML = '<b>Suche fehlgeschlagen</b>';
                div.appendChild(document.createTextNode(
                    (err || '') + ' — stimmt die CalDAV-Adresse?'));
                host.appendChild(div);
                return;
            }
            renderPicks(host, data.calendars.map(function (c) {
                return { name: c.name, area: c.url || '', raw: c };
            }), function (pick) {
                post('/api/calendars', {
                    name: pick.raw.name, caldav_url: pick.raw.url,
                    username: $('calUser').value, password: $('calPass').value,
                    color: pick.raw.color || '#00d4aa', cal_path: pick.raw.url || ''
                }, loadCalendars);
            });
        });
    });

    // --- Pickers ---
    wireSearch('stationSearch', 'stationResults', '/api/transit/search?q=',
        function (pick) {
            saveMany({ transit_station_id: pick.id, transit_station_name: pick.name });
        },
        function (host, data, onPick) { renderPicks(host, data.stations || [], onPick); });

    wireSearch('weatherSearch', 'weatherResults', '/api/geocode?q=',
        function (pick) {
            saveMany({ weather_lat: pick.lat, weather_lon: pick.lon,
                       weather_place: pick.name });
        },
        function (host, data, onPick) { renderPicks(host, data.places || [], onPick); });

    wireSearch('homeSearch', 'homeResults', '/api/geocode?q=',
        function (pick) { saveMany({ home_lat: pick.lat, home_lon: pick.lon }); },
        function (host, data, onPick) { renderPicks(host, data.places || [], onPick); });

    // Six hundred zones is a search, not a dropdown. Picking one shows the
    // clock in it straight away, so a wrong guess is obvious here rather
    // than tomorrow morning on the wall.
    wireSearch('tzSearch', 'tzResults', '/api/timezones?q=',
        function (pick) {
            save('timezone', pick.id);
            $('tzSearch').value = '';
            updateTimezoneHint();
        },
        function (host, data, onPick) { renderPicks(host, data.zones || [], onPick); });

    var tzAuto = $('tzAuto');
    if (tzAuto) tzAuto.addEventListener('click', function () {
        save('timezone', 'auto');
        $('tzSearch').value = '';
        $('tzResults').innerHTML = '';
        updateTimezoneHint();
    });

    // --- Manual override ---
    qsa('#overrideSeg button').forEach(function (b) {
        b.setAttribute('role', 'radio');
        b.addEventListener('click', function () {
            var mode = b.getAttribute('data-mode');
            post('/api/presence/override', { mode: mode }, function (err) {
                if (err) {
                    notify('saveError', 'err', 'Umschalten fehlgeschlagen', reason(err));
                    return;
                }
                // Paint it now for the tap, and let the next poll confirm it
                // from the daemon rather than trusting the click.
                paintOverride(mode);
            });
        });
    });
    var overrideSeg = $('overrideSeg');
    if (overrideSeg) overrideSeg.setAttribute('role', 'radiogroup');
    // Paint once the buttons are wired, from whatever the wall has already
    // told us. Blanking them here instead would undo the first poll, which
    // can land before this line on a fast local request.
    paintOverride(state.presence && state.presence.override);

    // --- Calibration ---
    $('calibStart').addEventListener('click', function () {
        $('calibError').innerHTML = '';
        post('/api/sensor/calibrate', { seconds: 20, delay: 8 }, function (err) {
            if (err) {
                notify('calibError', 'err', 'Messung konnte nicht starten',
                       reason(err) + ' Läuft der Präsenz-Dienst? '
                       + 'Prüfen mit: ./wallcal.sh presence status');
                return;
            }
            showCalib('calibRun');
            startCalibPoll();
        });
    });
    $('calibCancel').addEventListener('click', function () {
        post('/api/sensor/calibrate/cancel', {}, function () { showCalib('calibIdle'); stopCalib(); });
    });
    $('calibApply').addEventListener('click', function () {
        var button = $('calibApply');
        button.disabled = true;
        button.textContent = 'Wird übernommen…';
        post('/api/sensor/calibrate/apply', {}, function (err, data) {
            button.disabled = false;
            button.textContent = 'Übernehmen';
            if (err) {
                // Silence here was the whole bug: the button did nothing
                // visible whether it worked or not.
                notify('calibError', 'err', 'Konnte nicht übernommen werden',
                       reason(err));
                return;
            }
            var applied = (data && data.updated) || {};
            showCalib('calibIdle');
            notify('calibError', 'ok', 'Übernommen',
                   applied.sensor_distance_max_cm
                       ? 'Weckdistanz steht jetzt auf ' + applied.sensor_distance_max_cm + ' cm.'
                       : 'Die gemessenen Werte sind gespeichert.');
            get('/api/settings', function (e, d) { if (!e) applySettings(d); });
        });
    });

    // --- System actions ---
    $('syncNow').addEventListener('click', function () {
        post('/api/poll', {}, function () { $('syncNow').textContent = 'Läuft…';
            setTimeout(function () { $('syncNow').textContent = 'Synchronisieren'; }, 2500); });
    });
    $('restartKiosk').addEventListener('click', function () {
        post('/api/kiosk/restart', {}, function (err) {
            notify('feedHealth', err ? 'err' : 'ok',
                   err ? 'Neustart fehlgeschlagen' : 'Browser startet neu',
                   err ? reason(err) : 'Die Wand ist gleich wieder da.');
        });
    });

    // --- Destructive, confirmed ---
    $('radarReset').addEventListener('click', function () {
        if (!confirm('Radar wirklich auf Werkseinstellungen zurücksetzen? ' +
                     'Alle Gates und Empfindlichkeiten gehen verloren.')) return;
        post('/api/sensor/reset', {}, function (err) {
            notify('calibError', err ? 'err' : 'ok',
                   err ? 'Zurücksetzen fehlgeschlagen' : 'Radar zurückgesetzt',
                   err ? reason(err) : 'Bitte jetzt neu kalibrieren.');
        });
    });
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else { init(); }
})();
