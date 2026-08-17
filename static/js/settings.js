/* WallCal settings — the service surface.
 *
 * Every control is declarative: data-setting names the key, and one generic
 * binder handles reading, debouncing, writing and confirming. Adding a
 * setting means adding a row to the template, not another event listener.
 */
(function () {
'use strict';

var state = { settings: {}, calendars: [], presence: null, pendingStation: null };
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
    state.settings[key] = String(value);
    showValue(key);
    clearTimeout(saveTimers[key]);
    saveTimers[key] = setTimeout(function () {
        var body = {};
        body[key] = String(value);
        post('/api/settings', body, function (err) {
            if (err) { flashError(key); return; }
            confirmSaved(key);
        });
    }, SAVE_DEBOUNCE);
}

function confirmSaved(key) {
    qsa('[data-value-for="' + key + '"]').forEach(function (el) {
        el.classList.add('saved');
        setTimeout(function () { el.classList.remove('saved'); }, 60);
    });
}

function flashError(key) {
    qsa('[data-value-for="' + key + '"]').forEach(function (el) {
        el.textContent = 'nicht gespeichert';
    });
}

/** The current value, in the unit the user thinks in. */
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
        el.textContent = (raw === '' || raw === undefined || raw === null)
            ? '—' : raw + unit;
    });
}

// ---------------------------------------------------------------
// Generic binding
// ---------------------------------------------------------------
function bindInputs() {
    qsa('[data-setting]').forEach(function (el) {
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
        spec.split('|').forEach(function (pair) {
            var bits = pair.split(':');
            var b = document.createElement('button');
            b.type = 'button';
            b.setAttribute('data-val', bits[0]);
            b.textContent = bits[1] || bits[0];
            b.addEventListener('click', function () {
                save(key, bits[0]);
                paintSegment(key);
            });
            host.appendChild(b);
        });
    });
}

function paintSegment(key) {
    var host = document.querySelector('[data-seg="' + key + '"]');
    if (!host) return;
    qsa('[data-seg="' + key + '"] button').forEach(function (b) {
        b.classList.toggle('on', b.getAttribute('data-val') === state.settings[key]);
    });
    showValue(key);
}

function applySettings(data) {
    state.settings = data;
    qsa('[data-setting]').forEach(function (el) {
        var key = el.getAttribute('data-setting');
        var value = data[key];
        if (value === undefined) return;
        if (el.getAttribute('data-bool')) el.checked = String(value) === 'true';
        else el.value = value;
        showValue(key);
    });
    qsa('[data-seg]').forEach(function (h) { paintSegment(h.getAttribute('data-seg')); });
    updateDensityExplanation();
    updateOverlayHint();
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

    var text = 'Wechselt je nach Abstand zwischen Fern- und Nahansicht. ';
    if (!hasDistance) {
        text += 'Zurzeit inaktiv: der Sensor liefert keine Entfernung '
              + '(nur im UART-Modus verfügbar). Die Wand bleibt in der Nahansicht.';
    } else if (mode === 'off') {
        text += 'Ausgeschaltet.';
    } else if (mode === 'on') {
        text += 'Immer aktiv, unabhängig vom nutzbaren Bereich.';
    } else {
        text += 'Nutzbarer Fernbereich: ' + Math.round(band) + ' cm '
             + '(nötig: ' + minBand + ' cm). '
             + (band >= minBand ? 'Aktiv.' : 'Zu schmal, daher inaktiv.');
    }
    el.textContent = text;
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

function renderMeter(s) {
    state.presence = s;
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
    updateDensityExplanation();
}

function mark(id, cm) {
    var el = $(id);
    if (!el) return;
    var pct = Math.max(0, Math.min(100, (parseFloat(cm) || 0) / METER_MAX_CM * 100));
    el.style.left = pct + '%';
}

function pollPresence() {
    get('/api/presence/live', function (err, data) { if (!err) renderMeter(data); });
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
            api('PUT', '/api/calendars/' + cal.id, { color: dot.value }, function () {});
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

    get('/api/settings', function (err, data) {
        if (!err) { applySettings(data); loadCalendars(); }
    });
    loadSystem();
    pollPresence();
    setInterval(pollPresence, 700);
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
    window.addEventListener('scroll', spy, { passive: true });
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
            save('transit_station_id', pick.id);
            save('transit_station_name', pick.name);
        },
        function (host, data, onPick) { renderPicks(host, data.stations || [], onPick); });

    wireSearch('weatherSearch', 'weatherResults', '/api/geocode?q=',
        function (pick) {
            save('weather_lat', pick.lat); save('weather_lon', pick.lon);
            save('weather_place', pick.name);
        },
        function (host, data, onPick) { renderPicks(host, data.places || [], onPick); });

    wireSearch('homeSearch', 'homeResults', '/api/geocode?q=',
        function (pick) { save('home_lat', pick.lat); save('home_lon', pick.lon); },
        function (host, data, onPick) { renderPicks(host, data.places || [], onPick); });

    // --- Manual override ---
    qsa('#overrideSeg button').forEach(function (b) {
        b.addEventListener('click', function () {
            post('/api/presence/override', { mode: b.getAttribute('data-mode') }, function () {
                qsa('#overrideSeg button').forEach(function (x) { x.classList.remove('on'); });
                b.classList.add('on');
                $('overrideValue').textContent = b.textContent;
            });
        });
    });

    // --- Calibration ---
    $('calibStart').addEventListener('click', function () {
        $('calibError').innerHTML = '';
        post('/api/sensor/calibrate', { seconds: 20, delay: 8 }, function (err) {
            if (err) return;
            showCalib('calibRun');
            calibTimer = setInterval(function () {
                get('/api/sensor/calibrate', function (e, s) { if (!e) renderCalib(s); });
            }, 500);
        });
    });
    $('calibCancel').addEventListener('click', function () {
        post('/api/sensor/calibrate/cancel', {}, function () { showCalib('calibIdle'); stopCalib(); });
    });
    $('calibApply').addEventListener('click', function () {
        post('/api/sensor/calibrate/apply', {}, function (err) {
            if (err) return;
            showCalib('calibIdle');
            get('/api/settings', function (e, d) { if (!e) applySettings(d); });
        });
    });

    // --- System actions ---
    $('syncNow').addEventListener('click', function () {
        post('/api/poll', {}, function () { $('syncNow').textContent = 'Läuft…';
            setTimeout(function () { $('syncNow').textContent = 'Synchronisieren'; }, 2500); });
    });
    $('restartKiosk').addEventListener('click', function () {
        post('/api/kiosk/restart', {}, function () {});
    });

    // --- Destructive, confirmed ---
    $('radarReset').addEventListener('click', function () {
        if (!confirm('Radar wirklich auf Werkseinstellungen zurücksetzen? ' +
                     'Alle Gates und Empfindlichkeiten gehen verloren.')) return;
        post('/api/sensor/reset', {}, function () {});
    });
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else { init(); }
})();
