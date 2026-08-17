/* Extracted verbatim from templates/calendar.html. No logic changed. */

    (function() {
'use strict';

// ===============================================================
// STATE
// ===============================================================
var state = {
    events: [],
    calendars: [],
    settings: {},
    viewYear: new Date().getFullYear(),
    viewMonth: new Date().getMonth(),
    selectedDate: null,
    lastPoll: null,
    nextPollCountdown: 300,
    viewMode: 'grid', // 'grid' or 'dots'
};

var pollTimer = null;
var clockTimer = null;
var countdownTimer = null;

// ===============================================================
// HELPERS
// ===============================================================
function $(id) { return document.getElementById(id); }
function qs(sel) { return document.querySelector(sel); }
function qsa(sel) { return document.querySelectorAll(sel); }

function parseUTC(dateStr) {
    if (!dateStr) return null;
    if (dateStr.indexOf('Z') === -1 && dateStr.length === 19) {
        return new Date(dateStr + 'Z');
    }
    return new Date(dateStr);
}

function formatTime(dateStr) {
    var d = parseUTC(dateStr);
    var h = d.getHours();
    var m = d.getMinutes();
    return (h < 10 ? '0' : '') + h + ':' + (m < 10 ? '0' : '') + m;
}

function formatDate(d) {
    var days = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
    var months = ['January','February','March','April','May','June',
                 'July','August','September','October','November','December'];
    return days[d.getDay()] + ', ' + months[d.getMonth()] + ' ' + d.getDate() + ', ' + d.getFullYear();
}

function isSameDay(d1, d2) {
    return d1.getFullYear() === d2.getFullYear() &&
           d1.getMonth() === d2.getMonth() &&
           d1.getDate() === d2.getDate();
}

function dateKey(d) {
    return d.getFullYear() + '-' +
           ((d.getMonth()+1) < 10 ? '0' : '') + (d.getMonth()+1) + '-' +
           (d.getDate() < 10 ? '0' : '') + d.getDate();
}

function showToast(msg, type) {
    var t = $('toast');
    t.textContent = msg;
    t.className = 'toast ' + (type || 'success') + ' show';
    setTimeout(function() { t.className = 'toast'; }, 3000);
}

function apiGet(url, cb) {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', url, true);
    xhr.timeout = 10000;
    xhr.onload = function() {
        if (xhr.status >= 200 && xhr.status < 300) {
            try { cb(null, JSON.parse(xhr.responseText)); }
            catch(e) { cb(e, null); }
        } else {
            cb(new Error('HTTP ' + xhr.status), null);
        }
    };
    xhr.onerror = function() { cb(new Error('Network error'), null); };
    xhr.ontimeout = function() { cb(new Error('Timeout'), null); };
    xhr.send();
}

function apiPost(url, data, cb) {
    var xhr = new XMLHttpRequest();
    xhr.open('POST', url, true);
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.timeout = 15000;
    xhr.onload = function() {
        try { cb(null, JSON.parse(xhr.responseText)); }
        catch(e) { cb(e, null); }
    };
    xhr.onerror = function() { cb(new Error('Network error'), null); };
    xhr.ontimeout = function() { cb(new Error('Timeout'), null); };
    xhr.send(JSON.stringify(data));
}

function apiPut(url, data, cb) {
    var xhr = new XMLHttpRequest();
    xhr.open('PUT', url, true);
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.timeout = 10000;
    xhr.onload = function() {
        try { cb(null, JSON.parse(xhr.responseText)); }
        catch(e) { cb(e, null); }
    };
    xhr.onerror = function() { cb(new Error('Network error'), null); };
    xhr.send(JSON.stringify(data));
}

function apiDelete(url, cb) {
    var xhr = new XMLHttpRequest();
    xhr.open('DELETE', url, true);
    xhr.timeout = 10000;
    xhr.onload = function() {
        try { cb(null, JSON.parse(xhr.responseText)); }
        catch(e) { cb(e, null); }
    };
    xhr.onerror = function() { cb(new Error('Network error'), null); };
    xhr.send();
}

// ===============================================================
// LOCALE
//
// One formatter set, built once from the configured timezone and locale.
// Rebuilt only when those settings change — Intl.DateTimeFormat is not
// cheap enough to construct on a Pi 3B+ once per cell per minute.
// ===============================================================
var fmt = null;

function buildFormatters() {
    var loc = state.settings.locale || 'de-DE';
    var tz = state.settings.timezone || 'auto';
    var opts = (tz && tz !== 'auto') ? { timeZone: tz } : {};
    function f(extra) {
        var o = {}, k;
        for (k in opts) o[k] = opts[k];
        for (k in extra) o[k] = extra[k];
        try { return new Intl.DateTimeFormat(loc, o); }
        catch (e) { return new Intl.DateTimeFormat('de-DE', extra); }
    }
    fmt = {
        time:      f({ hour: '2-digit', minute: '2-digit', hour12: false }),
        weekdayLg: f({ weekday: 'long' }),
        weekdaySm: f({ weekday: 'short' }),
        dayMonth:  f({ day: 'numeric', month: 'long' }),
        dayMonthSm:f({ day: 'numeric', month: 'short' }),
        monthYear: f({ month: 'long', year: 'numeric' }),
        dayNum:    f({ day: 'numeric' })
    };
}

function fdate(formatter, d) {
    if (!fmt) buildFormatters();
    try { return formatter.format(d); } catch (e) { return ''; }
}

// Week number, ISO — Germans think in Kalenderwochen and it is the cheapest
// way to label a fortnight's two rows without spending horizontal space.
function isoWeek(d) {
    var t = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
    t.setUTCDate(t.getUTCDate() + 4 - (t.getUTCDay() || 7));
    var start = new Date(Date.UTC(t.getUTCFullYear(), 0, 1));
    return Math.ceil((((t - start) / 86400000) + 1) / 7);
}

function startOfWeek(d) {
    var out = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    out.setDate(out.getDate() - ((out.getDay() + 6) % 7));   // Monday = 0
    return out;
}

// ===============================================================
// EVENT INDEX
//
// Built once per fetch and reused by both layers, so a density switch
// never costs a re-index.
// ===============================================================
function buildEventIndex() {
    var map = {}, i, ev, cursor, key, end;
    for (i = 0; i < state.events.length; i++) {
        ev = state.events[i];
        var startAt = parseUTC(ev.dtstart);
        if (!startAt) continue;
        if (ev.all_day) {
            end = ev.dtend ? parseUTC(ev.dtend) : startAt;
            cursor = new Date(startAt);
            // An all-day event's DTEND is exclusive; a single-day one has
            // start == end, so always emit at least the first day.
            do {
                key = dateKey(cursor);
                (map[key] = map[key] || []).push(ev);
                cursor.setDate(cursor.getDate() + 1);
            } while (cursor < end);
        } else {
            key = dateKey(startAt);
            (map[key] = map[key] || []).push(ev);
        }
    }
    for (key in map) {
        map[key].sort(function(a, b) {
            if (a.all_day !== b.all_day) return a.all_day ? -1 : 1;
            return String(a.dtstart).localeCompare(String(b.dtstart));
        });
    }
    state.eventIndex = map;
    return map;
}

function eventsOn(d) { return (state.eventIndex || {})[dateKey(d)] || []; }

/** The next event starting from now, across every enabled calendar. */
function nextEvent() {
    var now = new Date(), i, ev, at;
    for (i = 0; i < state.events.length; i++) {
        ev = state.events[i];
        if (ev.all_day) continue;          // no meaningful start time
        at = parseUTC(ev.dtstart);
        if (at && at >= now) return ev;
    }
    return null;
}

// ===============================================================
// FAR — the clock, the next single thing, one actionable line
// ===============================================================
function renderFar() {
    var now = new Date();
    $('farClock').textContent = fdate(fmt.time, now);
    $('farDate').textContent =
        fdate(fmt.weekdayLg, now) + ' · ' + fdate(fmt.dayMonth, now);

    var host = $('farNext');
    var ev = nextEvent();
    if (!ev) {
        // An empty screen is not a failure state here — it is the answer.
        host.className = 'far-next empty';
        host.textContent = 'Nichts mehr heute';
        return;
    }
    host.className = 'far-next';
    host.innerHTML = '';

    var t = document.createElement('span');
    t.className = 't';
    t.textContent = fdate(fmt.time, parseUTC(ev.dtstart));
    host.appendChild(t);

    var body = document.createElement('span');
    body.appendChild(document.createTextNode(ev.summary || 'Termin'));
    if (ev.location) {
        var loc = document.createElement('span');
        loc.className = 'loc';
        loc.textContent = ev.location;
        body.appendChild(loc);
    }
    host.appendChild(body);
}

// ===============================================================
// NEAR — the calendar, at arm's length
// ===============================================================
function renderNear() {
    var now = new Date();
    $('nearClock').textContent = fdate(fmt.time, now);
    $('nearDate').textContent =
        fdate(fmt.weekdaySm, now) + ' ' + fdate(fmt.dayMonthSm, now);

    var view = state.settings.near_view || 'fortnight';
    if (view === 'month') renderMonth(now);
    else if (view === 'agenda') renderAgendaView(now);
    else renderFortnight(now);

    renderRail(now);
}

function dayCell(date, today, opts) {
    var cell = document.createElement('div');
    cell.className = 'day';
    var dow = date.getDay();
    if (dow === 0 || dow === 6) cell.className += ' we';
    if (opts.out) cell.className += ' out';
    if (isSameDay(date, today)) cell.className += ' today';

    var dn = document.createElement('div');
    dn.className = 'dn';
    dn.appendChild(document.createTextNode(fdate(fmt.dayNum, date)));
    if (opts.kw) {
        var kw = document.createElement('em');
        kw.textContent = 'KW ' + isoWeek(date);
        dn.appendChild(kw);
    }
    cell.appendChild(dn);

    if (opts.out) return cell;

    var events = eventsOn(date), shown = 0, i;
    for (i = 0; i < events.length && shown < opts.max; i++) {
        cell.appendChild(eventLine(events[i], opts.detail));
        shown++;
    }
    if (events.length > shown) {
        var more = document.createElement('div');
        more.className = 'more';
        more.textContent = '+' + (events.length - shown);
        cell.appendChild(more);
    }
    return cell;
}

function eventLine(ev, detail) {
    var line = document.createElement('div');
    line.className = 'ev' + (ev.all_day ? ' span' : '');
    if (!ev.all_day) line.style.borderLeftColor = ev.color || 'var(--now)';

    if (!ev.all_day) {
        var t = document.createElement('span');
        t.className = 't';
        t.textContent = fdate(fmt.time, parseUTC(ev.dtstart)) + ' ';
        line.appendChild(t);
    }
    line.appendChild(document.createTextNode(ev.summary || ''));

    if (detail && ev.location) {
        var s = document.createElement('span');
        s.className = 's';
        s.textContent = ev.location;
        line.appendChild(s);
    }
    return line;
}

function renderDow() {
    var host = $('calDow');
    host.innerHTML = '';
    var monday = startOfWeek(new Date()), i, span, d;
    for (i = 0; i < 7; i++) {
        d = new Date(monday); d.setDate(d.getDate() + i);
        span = document.createElement('span');
        span.textContent = fdate(fmt.weekdaySm, d);
        if (i >= 5) span.className = 'we';
        host.appendChild(span);
    }
}

/** Two rows of seven from this week's Monday. The default: four times the
 *  cell height of a month, so events are legible text rather than ticks. */
function renderFortnight(today) {
    var start = startOfWeek(today);
    var grid = $('calGrid');
    grid.className = 'cal-grid rows-2';
    grid.innerHTML = '';
    renderDow();

    var end = new Date(start); end.setDate(end.getDate() + 13);
    $('calTitle').textContent =
        fdate(fmt.dayNum, start) + '. – ' + fdate(fmt.dayMonth, end);

    for (var i = 0; i < 14; i++) {
        var d = new Date(start); d.setDate(d.getDate() + i);
        grid.appendChild(dayCell(d, today, { max: 6, detail: true, kw: (i % 7 === 0) }));
    }
}

/** Six rows of seven. More range, less room — better for a holiday block. */
function renderMonth(today) {
    var year = today.getFullYear(), month = today.getMonth();
    var first = new Date(year, month, 1);
    var start = startOfWeek(first);
    var grid = $('calGrid');
    grid.className = 'cal-grid rows-6';
    grid.innerHTML = '';
    renderDow();
    $('calTitle').textContent = fdate(fmt.monthYear, first);

    for (var i = 0; i < 42; i++) {
        var d = new Date(start); d.setDate(d.getDate() + i);
        grid.appendChild(dayCell(d, today, {
            max: 2, detail: false, out: d.getMonth() !== month
        }));
    }
}

/** The original two-column list, kept for portrait mounts where seven
 *  columns get too narrow to read. */
function renderAgendaView(today) {
    var grid = $('calGrid');
    grid.className = 'cal-grid';
    grid.style.gridTemplateColumns = '1fr';
    grid.innerHTML = '';
    $('calDow').innerHTML = '';
    $('calTitle').textContent = fdate(fmt.monthYear, today);

    var shown = 0;
    for (var i = 0; i < 21 && shown < 14; i++) {
        var d = new Date(today); d.setDate(d.getDate() + i);
        var events = eventsOn(d);
        if (!events.length) continue;
        var block = document.createElement('div');
        block.className = 'day' + (isSameDay(d, today) ? ' today' : '');
        var dn = document.createElement('div');
        dn.className = 'dn';
        dn.textContent = fdate(fmt.weekdaySm, d) + ' ' + fdate(fmt.dayMonth, d);
        block.appendChild(dn);
        for (var j = 0; j < events.length; j++) block.appendChild(eventLine(events[j], true));
        grid.appendChild(block);
        shown++;
    }
}

/** The rail is time-ordered actions, not a widget column: whatever happens
 *  next, whichever source it came from. Departures and leave-times join it
 *  as widgets land. */
function renderRail(today) {
    var host = $('railList');
    host.innerHTML = '';

    var rows = [], i, d, events, j, ev;
    for (i = 0; i < 3 && rows.length < 6; i++) {
        d = new Date(today); d.setDate(d.getDate() + i);
        events = eventsOn(d);
        for (j = 0; j < events.length && rows.length < 6; j++) {
            ev = events[j];
            if (i === 0 && !ev.all_day && parseUTC(ev.dtstart) < today) continue;
            rows.push({ ev: ev, day: i });
        }
    }

    if (!rows.length) {
        var empty = document.createElement('div');
        empty.className = 'empty';
        empty.textContent = 'Nichts geplant';
        host.appendChild(empty);
        return;
    }

    for (i = 0; i < rows.length; i++) {
        var r = document.createElement('div');
        r.className = 'r';
        var t = document.createElement('span');
        t.className = 't' + (rows[i].day === 0 && i === 0 ? ' now' : '');
        t.textContent = rows[i].ev.all_day
            ? fdate(fmt.weekdaySm, new Date(today.getTime() + rows[i].day * 86400000))
            : fdate(fmt.time, parseUTC(rows[i].ev.dtstart));
        r.appendChild(t);

        var body = document.createElement('span');
        body.appendChild(document.createTextNode(rows[i].ev.summary || ''));
        if (rows[i].day > 0) {
            var s = document.createElement('span');
            s.className = 's';
            s.textContent = fdate(fmt.weekdaySm,
                new Date(today.getTime() + rows[i].day * 86400000));
            body.appendChild(s);
        }
        r.appendChild(body);
        host.appendChild(r);
    }
}

// ===============================================================
// DENSITY
//
// Hysteresis is mandatory: without separate enter/exit thresholds the
// layout oscillates while somebody stands at the boundary. The debounce
// is on top of that, for the radar's own frame-to-frame jitter.
// ===============================================================
var densityPending = null;
var densityPendingSince = 0;

function densityActive() {
    var mode = state.settings.density_mode || 'auto';
    if (mode === 'off') return false;
    if (mode === 'on') return true;
    // auto: the usable FAR band is what makes this worth doing at all. With
    // auto-off enabled the panel is only lit inside wake_distance, so the
    // visible band is wake_distance - near_threshold. A 20 cm band is not a
    // feature, it is a flicker.
    if (!state.presence || state.presence.distance_cm === null ||
        state.presence.distance_cm === undefined) return false;
    var wake = num(state.settings.sensor_distance_max_cm, 300);
    var near = num(state.settings.density_near_cm, 100);
    var strategy = String(state.settings.display_off_strategy || 'hdmi');
    // Under 'none' the panel never sleeps, so the whole sensor range is
    // usable rather than just what is inside the wake distance.
    var band = strategy.indexOf('none') >= 0 ? 600 - near : wake - near;
    return band >= num(state.settings.density_min_band_cm, 80);
}

function num(v, dflt) {
    var n = parseFloat(v);
    return isNaN(n) ? dflt : n;
}

function updateDensity(distance) {
    var app = $('app');
    if (!densityActive()) {
        // Without a distance source there is nothing to switch on. NEAR is
        // the safe default: it is the layout that still makes sense when you
        // are standing right in front of it, and gpio/none sensor modes never
        // report a distance at all.
        setDensity('near');
        return;
    }
    if (distance === null || distance === undefined || distance <= 0) return;

    var near = num(state.settings.density_near_cm, 100);
    var far = num(state.settings.density_far_cm, 140);
    var current = app.getAttribute('data-density');
    var want = current;

    if (current === 'far' && distance <= near) want = 'near';
    else if (current === 'near' && distance >= far) want = 'far';

    if (want === current) { densityPending = null; return; }

    var nowMs = Date.now();
    if (densityPending !== want) {
        densityPending = want;
        densityPendingSince = nowMs;
        return;
    }
    if (nowMs - densityPendingSince >= num(state.settings.density_debounce_ms, 1500)) {
        densityPending = null;
        setDensity(want);
    }
}

function setDensity(which) {
    var app = $('app');
    if (app.getAttribute('data-density') === which) return;
    app.setAttribute('data-density', which);
}

// ===============================================================
// RENDER ENTRY POINT
// ===============================================================
function renderWall() {
    if (!fmt) buildFormatters();
    buildEventIndex();
    renderFar();
    renderNear();
}

function updateClock() {
    if (!fmt) buildFormatters();
    var now = new Date();
    $('farClock').textContent = fdate(fmt.time, now);
    $('nearClock').textContent = fdate(fmt.time, now);
    $('farDate').textContent =
        fdate(fmt.weekdayLg, now) + ' · ' + fdate(fmt.dayMonth, now);
    $('nearDate').textContent =
        fdate(fmt.weekdaySm, now) + ' ' + fdate(fmt.dayMonthSm, now);
}


// ===============================================================
// DATA FETCHING
// ===============================================================
function fetchEvents() {
    apiGet('/events', function(err, data) {
        if (err) {
            // Never a spinner, never an error state. The Pi's WLAN drops and
            // CalDAV times out; neither should be visible on a wall. Render
            // the last good data and mark it quietly.
            var cached = localStorage.getItem('wallcal_events');
            if (cached) {
                try {
                    var parsed = JSON.parse(cached);
                    state.events = parsed.events || [];
                    state.lastPoll = parsed.last_poll;
                    renderWall();
                } catch (e) {}
            }
            markStale(true);
            return;
        }

        state.events = data.events || [];
        state.lastPoll = data.last_poll;
        try { localStorage.setItem('wallcal_events', JSON.stringify(data)); } catch (e) {}
        markStale(false);
        renderWall();
    });
}

/** Staleness is a dot, never a banner. */
function markStale(stale) {
    state.stale = !!stale;
    $('app').setAttribute('data-stale', stale ? '1' : '0');
}

/** How old the cache is, in minutes — used to decide the dot on a slow feed
 *  rather than only on an outright failure. */
function cacheAgeMinutes() {
    if (!state.lastPoll) return null;
    var at = new Date(String(state.lastPoll).replace(' ', 'T') + 'Z');
    if (isNaN(at.getTime())) return null;
    return (Date.now() - at.getTime()) / 60000;
}

function fetchSettings() {
    apiGet('/api/settings', function(err, data) {
        if (err) return;
        state.settings = data;

        // Apply theme
        var theme = data.theme || 'dark';
        document.body.setAttribute('data-theme', theme);
        $('settingTheme').checked = (theme === 'dark');

        // Apply animations
        var anim = data.animations_enabled === 'true';
        document.body.setAttribute('data-animations', anim ? 'on' : 'off');
        $('settingAnimations').checked = anim;

        // Apply view mode
        state.viewMode = data.calendar_view || 'grid';
        $('settingView').value = state.viewMode;

        applyWallSettings(data);

        // Apply poll interval
        $('settingPollInterval').value = data.poll_interval_minutes || '5';

        // Sensor + presence settings
        $('settingSensorMode').value = data.sensor_mode || 'auto';
        $('settingGpioPin').value = data.sensor_gpio_pin || '18';
        $('settingUartPort').value = data.sensor_uart_port || 'auto';
        $('settingUartBaud').value = data.sensor_uart_baud || '256000';
        $('settingProgramGates').checked = data.sensor_program_gates !== 'false';

        $('settingMaxDist').value = data.sensor_distance_max_cm || '300';
        $('settingTimeout').value = data.display_off_timeout || '60';
        $('settingMinDist').value = data.sensor_distance_min_cm || '0';
        $('settingHysteresis').value = data.sensor_hysteresis_cm || '40';
        $('settingMovingEnergy').value = data.sensor_moving_energy_min || '30';
        $('settingStationaryEnergy').value = data.sensor_stationary_energy_min || '25';
        $('settingUseStationary').checked = data.sensor_use_stationary !== 'false';
        $('settingConfirmMs').value = data.presence_confirm_ms || '300';

        $('settingScheduleEnabled').checked = data.schedule_enabled === 'true';
        $('settingScheduleStart').value = data.schedule_start || '06:30';
        $('settingScheduleEnd').value = data.schedule_end || '23:00';

        setBackendOption(data.display_backend || 'auto');
        $('settingDisplayOutput').value = data.display_output || 'auto';
        $('settingDisplayRotate').value = data.display_rotate || 'normal';

        toggleSensorFields();
        syncRangeLabels();

        renderWall();
    });
}

/** Settings that change how the wall itself looks, applied without a reload
 *  so the panel picks them up within the usual couple of seconds. */
function applyWallSettings(data) {
    var app = $('app');
    app.setAttribute('data-drift', data.drift_enabled === 'false' ? 'off' : 'on');
    app.style.setProperty('--crossfade', num(data.crossfade_ms, 400) + 'ms');

    // Rebuild the Intl formatters only when the inputs actually change: they
    // are expensive enough to matter on a Pi 3B+ and this runs on every poll.
    var stamp = (data.locale || '') + '|' + (data.timezone || '');
    if (stamp !== state.localeStamp) {
        state.localeStamp = stamp;
        buildFormatters();
    }
    if (state.eventIndex) renderWall();
}

function fetchCalendars() {
    apiGet('/api/calendars', function(err, data) {
        if (err) return;
        state.calendars = data.calendars || [];
        renderCalendarList();
    });
}

function updateSyncStatus() {
    // The poll interval is generous and the age dot covers the failure case,
    // so a live "last sync" line is chrome. Kept as the staleness check.
    var age = cacheAgeMinutes();
    var limit = num(state.settings.poll_interval_minutes, 5) * 3;
    if (age !== null) markStale(age > limit);
}

function updateCountdown() {
    state.nextPollCountdown--;
    if (state.nextPollCountdown <= 0) fetchEvents();
}

// ===============================================================
// SETTINGS UI
// ===============================================================
function renderCalendarList() {
    var container = $('calendarList');
    container.innerHTML = '';

    if (state.calendars.length === 0) {
        container.innerHTML = '<div style="color:var(--text-muted);font-size:14px;padding:20px 0;text-align:center">No calendars configured. Add one below.</div>';
        return;
    }

    for (var i = 0; i < state.calendars.length; i++) {
        var cal = state.calendars[i];
        var item = document.createElement('div');
        item.className = 'cal-list-item';

        var colorDot = document.createElement('input');
        colorDot.type = 'color';
        colorDot.className = 'color-input';
        colorDot.value = cal.color || '#00d4aa';
        colorDot.style.width = '28px';
        colorDot.style.height = '28px';
        (function(calId) {
            colorDot.addEventListener('change', function() {
                apiPut('/api/calendars/' + calId, { color: this.value }, function(err) {
                    if (!err) { fetchCalendars(); fetchEvents(); }
                });
            });
        })(cal.id);
        item.appendChild(colorDot);

        var nameEl = document.createElement('span');
        nameEl.className = 'cal-list-name';
        nameEl.textContent = cal.name;
        item.appendChild(nameEl);

        var provEl = document.createElement('span');
        provEl.style.cssText = 'font-size:11px;color:var(--text-muted);margin-right:8px';
        provEl.textContent = cal.provider;
        item.appendChild(provEl);

        var actions = document.createElement('div');
        actions.className = 'cal-list-actions';

        // Enable/disable toggle
        var toggleLabel = document.createElement('label');
        toggleLabel.className = 'toggle-switch';
        toggleLabel.style.cssText = 'width:38px;height:20px';
        var toggleInput = document.createElement('input');
        toggleInput.type = 'checkbox';
        toggleInput.checked = cal.enabled;
        (function(calId) {
            toggleInput.addEventListener('change', function() {
                apiPut('/api/calendars/' + calId, { enabled: this.checked }, function(err) {
                    if (!err) { fetchCalendars(); fetchEvents(); }
                });
            });
        })(cal.id);
        var toggleSlider = document.createElement('span');
        toggleSlider.className = 'toggle-slider';
        toggleSlider.style.cssText = 'border-radius:10px';
        var toggleSliderBefore = toggleSlider.style;
        toggleLabel.appendChild(toggleInput);
        toggleLabel.appendChild(toggleSlider);
        actions.appendChild(toggleLabel);

        // Delete button
        var delBtn = document.createElement('button');
        delBtn.className = 'btn btn-danger btn-sm';
        delBtn.textContent = '✕';
        delBtn.title = 'Delete calendar';
        (function(calId, calName) {
            delBtn.addEventListener('click', function() {
                if (confirm('Delete calendar "' + calName + '"?')) {
                    apiDelete('/api/calendars/' + calId, function(err) {
                        if (!err) {
                            showToast('Calendar deleted', 'success');
                            fetchCalendars();
                            fetchEvents();
                        }
                    });
                }
            });
        })(cal.id, cal.name);
        actions.appendChild(delBtn);

        item.appendChild(actions);
        container.appendChild(item);
    }
}

// ===============================================================
// PRESENCE / SENSOR PANEL
// ===============================================================

var presenceTimer = null;

function toggleSensorFields() {
    var mode = $('settingSensorMode').value;
    // "auto" may end up using either transport, so show both.
    $('gpioSettings').style.display = (mode === 'gpio' || mode === 'auto') ? 'block' : 'none';
    $('uartSettings').style.display = (mode === 'uart' || mode === 'auto') ? 'block' : 'none';
    $('scheduleFields').style.display = $('settingScheduleEnabled').checked ? 'flex' : 'none';
}

function syncRangeLabels() {
    $('maxDistValue').textContent = $('settingMaxDist').value + ' cm';
    var seconds = parseInt($('settingTimeout').value, 10);
    $('timeoutValue').textContent = seconds >= 120
        ? Math.round(seconds / 60) + ' min'
        : seconds + ' s';
    updateMeterThreshold(parseInt($('settingMaxDist').value, 10));
}

var METER_MAX_CM = 600; // the LD2410's own ceiling: 8 gates x 75 cm

function updateMeterThreshold(cm) {
    if (isNaN(cm)) return;
    var pct = Math.max(0, Math.min(100, (cm / METER_MAX_CM) * 100));
    $('distanceMark').style.left = pct + '%';
    $('thresholdText').textContent = 'wake < ' + cm + ' cm';
}

function formatDuration(seconds) {
    if (seconds === null || seconds === undefined || isNaN(seconds)) return '—';
    seconds = Math.round(seconds);
    if (seconds < 60) return seconds + ' s';
    if (seconds < 3600) return Math.round(seconds / 60) + ' min';
    return (seconds / 3600).toFixed(1) + ' h';
}

function setBadge(el, textEl, cls, text) {
    el.className = 'live-badge ' + cls;
    textEl.textContent = text;
}

function renderPresence(s) {
    var badge = $('presenceBadge'), badgeText = $('presenceBadgeText');

    if (!s || !s.daemon_running) {
        setBadge(badge, badgeText, 'error', 'Daemon offline');
        $('presenceMeta').textContent = 'systemctl status wallcal-presence';
        setBadge($('displayBadge'), $('displayBadgeText'), '', 'Display —');
        return;
    }

    setBadge(badge, badgeText, s.present ? 'on' : 'off',
             s.present ? 'Presence detected' : 'No one there');

    var on = s.display_on;
    setBadge($('displayBadge'), $('displayBadgeText'), on ? 'on' : 'off',
             'Display ' + (on ? 'on' : 'off') +
             (s.display_reason ? ' · ' + s.display_reason : ''));

    $('presenceMeta').textContent = 'updated ' + formatDuration(s.age_seconds) + ' ago';

    var reading = s.reading || {};
    var distance = reading.distance_cm;
    var thresholds = s.thresholds || {};
    var limit = thresholds.distance_max_cm;

    if (distance === null || distance === undefined) {
        $('distanceFill').style.width = s.present ? '100%' : '0';
        $('distanceFill').className = 'meter-fill' + (s.present ? '' : ' out');
        $('distanceText').textContent = s.sensor_kind === 'gpio'
            ? (s.present ? 'detected (no distance in GPIO mode)' : 'clear')
            : '— cm';
    } else {
        var pct = Math.max(0, Math.min(100, (distance / METER_MAX_CM) * 100));
        $('distanceFill').style.width = pct + '%';
        $('distanceFill').className = 'meter-fill' +
            (distance <= limit && distance > 0 ? '' : ' out');
        $('distanceText').textContent = distance + ' cm';
    }
    if (limit) updateMeterThreshold(limit);

    $('statTarget').textContent = reading.state_name || (s.present ? 'detected' : 'none');
    $('statMoving').textContent = reading.moving_distance_cm !== undefined
        ? reading.moving_distance_cm + ' cm / ' + (reading.moving_energy || 0) : '—';
    $('statStill').textContent = reading.stationary_distance_cm !== undefined
        ? reading.stationary_distance_cm + ' cm / ' + (reading.stationary_energy || 0) : '—';
    $('statIdle').textContent = formatDuration(s.idle_seconds);
    $('statSensor').textContent = s.sensor_error
        ? 'error' : (s.sensor_description || s.sensor_kind || '—');
    $('statSensor').title = s.sensor_error || s.sensor_description || '';
    $('statBackend').textContent = (s.display_backends || []).join(', ') || '—';
    $('statWakes').textContent = s.wake_count !== undefined ? s.wake_count : '—';
    $('statOnTime').textContent = formatDuration(s.display_on_seconds_today);

    // Keep the override buttons in step with whatever set the state,
    // including the command line.
    var seg = qsa('#overrideSeg button');
    for (var i = 0; i < seg.length; i++) {
        seg[i].classList.toggle('active',
            seg[i].getAttribute('data-mode') === (s.override || 'auto'));
    }
}

function pollPresence() {
    apiGet('/api/presence', function(err, data) {
        renderPresence(err ? null : data);
    });
}

// ---------------------------------------------------------------
// Repaint on wake
//
// Some display backends (vcgencmd, CEC, output disable) power the
// HDMI link down without the compositor knowing. When it comes back
// the window is never marked dirty, so the panel shows a stale or
// blank surface even though the page is still running. Watch for the
// display waking and force a repaint plus fresh data.
// ---------------------------------------------------------------

var lastDisplayOn = null;
var wakeWatchTimer = null;

function forceRepaint() {
    // Toggling a layout-affecting property invalidates the whole
    // surface, which is what actually gets a new frame on screen.
    var el = document.getElementById('app');
    if (!el) return;
    el.style.transform = 'translateZ(0)';
    void el.offsetHeight;
    el.style.transform = '';
    void el.offsetHeight;
}

function watchForWake() {
    apiGet('/api/presence/live', function(err, data) {
        if (err || !data) return;
        state.presence = data;

        if (data.daemon_running) {
            var on = data.display_on;
            if (on === true && lastDisplayOn === false) {
                forceRepaint();
                fetchEvents();   // whoever walked up wants current data
            }
            if (on === true || on === false) lastDisplayOn = on;
            applyPanelState(data);
        }
        updateDensity(data.distance_cm);
    });
}

/** Reflect the daemon's published panel state: power, mode and the single
 *  brightness value. Under the pwm strategy the hardware is already at that
 *  level, so the overlay stays out of the way. */
function applyPanelState(s) {
    var app = $('app');
    app.setAttribute('data-power', s.display_on === false ? 'off' : 'on');
    app.setAttribute('data-mode', s.display_mode || 'normal');
    app.setAttribute('data-brightness-source', s.brightness_source || 'css');

    // A black scrim at alpha a leaves luminance (1 - a), so the perceptual
    // value maps straight onto it.
    var b = s.brightness;
    if (typeof b === 'number') {
        app.style.setProperty('--dim-alpha', String(Math.max(0, Math.min(1, 1 - b / 100))));
    }
}

function startPresencePolling() {
    if (presenceTimer) return;
    pollPresence();
    presenceTimer = setInterval(pollPresence, 1000);
}

function stopPresencePolling() {
    if (!presenceTimer) return;
    clearInterval(presenceTimer);
    presenceTimer = null;
}

// ---------------------------------------------------------------
// Calibration
//
// A run takes ~30s, so the server does it on a background thread and
// we poll. The countdown exists because whoever pressed the button
// needs time to walk over and stand where they want it to wake.
// ---------------------------------------------------------------

var calibrateTimer = null;

function stopCalibratePolling() {
    if (calibrateTimer) { clearInterval(calibrateTimer); calibrateTimer = null; }
}

function renderCalibration(s) {
    if (!s) return;
    var badge = $('calibrateBadge'), badgeText = $('calibrateBadgeText');
    var step = $('calibrateStep');
    var result = $('calibrateResult');
    var fill = $('calibrateFill');

    $('calibrateMeta').textContent = s.message || '';

    if (s.state === 'countdown') {
        setBadge(badge, badgeText, 'on', 'Get into position');
        step.innerHTML = 'Go and stand at the furthest point where the ' +
            'calendar should wake up.<div class="calibrate-countdown">' +
            s.remaining + 's</div>';
        fill.style.width = '0';
        $('calibrateNow').textContent = 'starting soon';
        $('calibrateSamples').textContent = '';
        return;
    }

    if (s.state === 'sampling') {
        setBadge(badge, badgeText, 'on', 'Sampling — hold still');
        step.innerHTML = 'Stay where you are for another ' +
            '<strong>' + s.remaining + 's</strong>.';
        var d = s.current_distance_cm || 0;
        fill.style.width = Math.max(0, Math.min(100, (d / METER_MAX_CM) * 100)) + '%';
        fill.className = 'meter-fill' + (d ? '' : ' out');
        $('calibrateNow').textContent = d ? (d + ' cm · ' + s.current_state)
                                          : 'no target detected';
        $('calibrateSamples').textContent = s.samples + ' samples';
        return;
    }

    if (s.state === 'done' && s.result) {
        stopCalibratePolling();
        setBadge(badge, badgeText, 'on', 'Done');
        step.textContent = 'Measured from ' + s.result.samples + ' detections.';
        fill.style.width = '100%';
        fill.className = 'meter-fill';
        $('calibrateNow').textContent = 'complete';
        $('calibrateSamples').textContent = '';
        result.className = 'calibrate-result';
        result.innerHTML =
            '<div class="headline">Suggested settings</div>' +
            row('Wake distance', s.result.suggested_distance_max_cm + ' cm') +
            row('Moving sensitivity gate', s.result.suggested_moving_energy_min) +
            row('Still sensitivity gate', s.result.suggested_stationary_energy_min) +
            '<div class="headline" style="margin-top:10px">What was seen</div>' +
            row('Closest / furthest', s.result.min_cm + ' – ' + s.result.max_cm + ' cm') +
            row('Median', s.result.median_cm + ' cm');
        $('calibrateApplyBtn').style.display = '';
        $('calibrateCancelBtn').textContent = 'Close';
        return;
    }

    if (s.state === 'failed' || s.state === 'cancelled') {
        stopCalibratePolling();
        setBadge(badge, badgeText, s.state === 'cancelled' ? '' : 'error',
                 s.state === 'cancelled' ? 'Cancelled' : 'Not enough detections');
        step.textContent = '';
        fill.style.width = '0';
        $('calibrateNow').textContent = '—';
        $('calibrateSamples').textContent = '';
        if (s.state === 'failed') {
            result.className = 'calibrate-error';
            result.innerHTML = '<strong>' + (s.message || 'Calibration failed') +
                '</strong><br>' + (s.error || '') +
                '<br><br>Frames received: ' + s.frames +
                ' · with a target: ' + s.frames_with_target +
                (s.furthest_seen_cm ? ' · furthest: ' + s.furthest_seen_cm + ' cm' : '');
        } else {
            result.className = '';
            result.innerHTML = '';
        }
        $('calibrateCancelBtn').textContent = 'Close';
        return;
    }

    setBadge(badge, badgeText, '', 'Starting…');
}

function row(label, value) {
    return '<div class="row"><span>' + label + '</span><span>' + value + '</span></div>';
}

function startCalibration() {
    var panel = $('calibratePanel');
    panel.style.display = '';
    $('calibrateResult').innerHTML = '';
    $('calibrateResult').className = '';
    $('calibrateApplyBtn').style.display = 'none';
    $('calibrateCancelBtn').textContent = 'Cancel';
    $('calibrateBtn').disabled = true;

    apiPost('/api/sensor/calibrate', { seconds: 20, delay: 10 }, function(err, data) {
        if (err) {
            $('calibrateResult').className = 'calibrate-error';
            $('calibrateResult').textContent =
                (data && data.error) || 'Could not start calibration.';
            $('calibrateBtn').disabled = false;
            return;
        }
        renderCalibration(data);
        stopCalibratePolling();
        calibrateTimer = setInterval(function() {
            apiGet('/api/sensor/calibrate', function(e, s) {
                if (e) return;
                renderCalibration(s);
                if (!s.running && s.state !== 'countdown' && s.state !== 'sampling') {
                    $('calibrateBtn').disabled = false;
                }
            });
        }, 500);
    });
}

function setBackendOption(value) {
    var select = $('settingDisplayBackend');
    var found = false;
    for (var i = 0; i < select.options.length; i++) {
        if (select.options[i].value === value) { found = true; break; }
    }
    if (!found) {
        var opt = document.createElement('option');
        opt.value = value;
        opt.textContent = value;
        select.appendChild(opt);
    }
    select.value = value;
}

function loadDisplayBackends() {
    $('backendHint').textContent = 'Probing…';
    apiGet('/api/display/backends', function(err, data) {
        if (err || !data || !data.backends) {
            $('backendHint').textContent = 'Could not probe backends.';
            return;
        }
        var select = $('settingDisplayBackend');
        var current = select.value;
        select.innerHTML = '';
        var auto = document.createElement('option');
        auto.value = 'auto';
        auto.textContent = 'Auto-detect';
        select.appendChild(auto);

        var usable = [];
        for (var i = 0; i < data.backends.length; i++) {
            var b = data.backends[i];
            if (b.name === 'none') continue;
            var opt = document.createElement('option');
            opt.value = b.name;
            opt.textContent = b.name + ' — ' + b.description +
                (b.available ? '' : ' (unavailable here)');
            opt.disabled = !b.available;
            select.appendChild(opt);
            if (b.available) usable.push(b.name);
        }
        setBackendOption(current || 'auto');
        $('backendHint').textContent = usable.length
            ? 'Available: ' + usable.join(', ')
            : 'No backend available — is the desktop session running?';
    });
}

// ===============================================================
// EVENT HANDLERS
// ===============================================================
function init() {
    // Clock
    updateClock();
    clockTimer = setInterval(updateClock, 1000);

    // Countdown
    countdownTimer = setInterval(updateCountdown, 1000);

    // Fetch data
    fetchSettings();
    fetchCalendars();
    fetchEvents();

    // Poll timer (fetch events)
    pollTimer = setInterval(function() {
        fetchEvents();
    }, 5 * 60 * 1000); // default 5 min, actual controlled by countdown

    // Re-render on the minute so the fortnight rolls over at midnight without
    // waiting for the next CalDAV poll.
    setInterval(function() {
        if (new Date().getSeconds() === 0) renderWall();
    }, 1000);

    // Catch the display waking so the panel never shows a stale frame.
    watchForWake();
    wakeWatchTimer = setInterval(watchForWake, 500);

    // The wall has no touch panel and no pointer, so month navigation, the
    // theme toggle and a manual refresh button were all chrome on a surface
    // nobody can reach. Theme lives in settings; the poller refreshes itself.
    // The only affordance left is the settings corner.

    // --- Settings modal ---
    $('settingsBtn').addEventListener('click', function() {
        $('settingsModal').classList.add('open');
        fetchCalendars();
        fetchSettings();
    });
    $('closeSettings').addEventListener('click', function() {
        $('settingsModal').classList.remove('open');
        stopPresencePolling();
    });
    $('settingsModal').addEventListener('click', function(e) {
        if (e.target === this) {
            this.classList.remove('open');
            stopPresencePolling();
        }
    });

    // --- Tabs ---
    var backendsLoaded = false;
    var tabs = qsa('.modal-tab');
    for (var i = 0; i < tabs.length; i++) {
        tabs[i].addEventListener('click', function() {
            for (var j = 0; j < tabs.length; j++) tabs[j].classList.remove('active');
            this.classList.add('active');
            var tabContents = qsa('.tab-content');
            for (var k = 0; k < tabContents.length; k++) tabContents[k].classList.remove('active');
            var name = this.getAttribute('data-tab');
            $('tab-' + name).classList.add('active');

            // The live readout is only worth polling while it is visible.
            if (name === 'sensor') {
                startPresencePolling();
                if (!backendsLoaded) { backendsLoaded = true; loadDisplayBackends(); }
            } else {
                stopPresencePolling();
                stopCalibratePolling();
            }
        });
    }

    // --- Add calendar ---
    $('addCalBtn').addEventListener('click', function() {
        $('addCalForm').classList.toggle('open');
    });
    $('cancelCalBtn').addEventListener('click', function() {
        $('addCalForm').classList.remove('open');
        clearAddForm();
    });

    $('newCalProvider').addEventListener('change', function() {
        if (this.value === 'icloud') {
            $('newCalUrl').value = 'https://caldav.icloud.com';
            $('urlHint').textContent = 'Use an App-Specific Password for iCloud';
        } else if (this.value === 'nextcloud') {
            $('newCalUrl').value = '';
            $('newCalUrl').placeholder = 'https://cloud.example.com/remote.php/dav/';
            $('urlHint').textContent = '';
        } else {
            $('newCalUrl').value = '';
            $('newCalUrl').placeholder = 'https://your-server.com/caldav/';
            $('urlHint').textContent = '';
        }
    });

    // Test connection
    $('testConnBtn').addEventListener('click', function() {
        var status = $('connectionStatus');
        status.className = 'status-msg';
        status.textContent = 'Testing connection...';
        status.style.display = 'block';
        status.style.background = 'var(--accent-dim)';
        status.style.color = 'var(--text-secondary)';

        apiPost('/api/test-connection', {
            caldav_url: $('newCalUrl').value,
            username: $('newCalUser').value,
            password: $('newCalPass').value,
        }, function(err, data) {
            if (err) {
                status.className = 'status-msg error';
                status.textContent = 'Connection failed: ' + err.message;
            } else if (data.ok) {
                status.className = 'status-msg success';
                status.textContent = data.message;
            } else {
                status.className = 'status-msg error';
                status.textContent = 'Failed: ' + (data.error || 'Unknown error');
            }
        });
    });

    // Discover calendars
    $('discoverBtn').addEventListener('click', function() {
        var status = $('connectionStatus');
        status.className = 'status-msg';
        status.textContent = 'Discovering calendars...';
        status.style.display = 'block';
        status.style.background = 'var(--accent-dim)';
        status.style.color = 'var(--text-secondary)';

        apiPost('/api/discover', {
            caldav_url: $('newCalUrl').value,
            username: $('newCalUser').value,
            password: $('newCalPass').value,
        }, function(err, data) {
            if (err || !data.calendars) {
                status.className = 'status-msg error';
                status.textContent = 'Discovery failed: ' + (err ? err.message : (data.error || 'Unknown'));
                return;
            }
            status.className = 'status-msg success';
            status.textContent = 'Found ' + data.calendars.length + ' calendar(s). Click "Add" to import.';

            var container = $('discoveredCalendars');
            container.innerHTML = '';
            for (var i = 0; i < data.calendars.length; i++) {
                var dc = data.calendars[i];
                var item = document.createElement('div');
                item.className = 'discovered-item';

                var dot = document.createElement('div');
                dot.style.cssText = 'width:12px;height:12px;border-radius:50%;background:' + (dc.color || '#00d4aa');
                item.appendChild(dot);

                var name = document.createElement('span');
                name.textContent = dc.name;
                name.style.cssText = 'flex:1;font-size:14px';
                item.appendChild(name);

                var addBtn = document.createElement('button');
                addBtn.className = 'btn btn-sm btn-primary';
                addBtn.textContent = 'Add';
                (function(cal) {
                    addBtn.addEventListener('click', function() {
                        apiPost('/api/calendars', {
                            name: cal.name,
                            caldav_url: $('newCalUrl').value,
                            username: $('newCalUser').value,
                            password: $('newCalPass').value,
                            provider: $('newCalProvider').value,
                            color: cal.color || $('newCalColor').value,
                            cal_path: cal.path,
                            enabled: true,
                        }, function(err) {
                            if (!err) {
                                showToast('Calendar "' + cal.name + '" added', 'success');
                                fetchCalendars();
                                this.textContent = '✓';
                                this.disabled = true;
                            }
                        }.bind(this));
                    });
                })(dc);
                item.appendChild(addBtn);
                container.appendChild(item);
            }
        });
    });

    // Save single calendar (manual entry without discovery)
    $('saveCalBtn').addEventListener('click', function() {
        var name = prompt('Calendar name:', 'My Calendar');
        if (!name) return;
        apiPost('/api/calendars', {
            name: name,
            caldav_url: $('newCalUrl').value,
            username: $('newCalUser').value,
            password: $('newCalPass').value,
            provider: $('newCalProvider').value,
            color: $('newCalColor').value,
            enabled: true,
        }, function(err) {
            if (!err) {
                showToast('Calendar added', 'success');
                $('addCalForm').classList.remove('open');
                clearAddForm();
                fetchCalendars();
                fetchEvents();
            } else {
                showToast('Failed to add calendar', 'error');
            }
        });
    });

    // --- Save display settings ---
    $('saveDisplayBtn').addEventListener('click', function() {
        var data = {
            theme: $('settingTheme').checked ? 'dark' : 'light',
            animations_enabled: $('settingAnimations').checked ? 'true' : 'false',
            calendar_view: $('settingView').value,
            poll_interval_minutes: $('settingPollInterval').value,
        };
        apiPost('/api/settings', data, function(err) {
            if (!err) {
                showToast('Display settings saved', 'success');
                state.viewMode = data.calendar_view;
                document.body.setAttribute('data-theme', data.theme);
                document.body.setAttribute('data-animations', data.animations_enabled === 'true' ? 'on' : 'off');
                state.nextPollCountdown = parseInt(data.poll_interval_minutes) * 60;
                renderWall();
            }
        });
    });

    // --- Save sensor settings ---
    $('saveSensorBtn').addEventListener('click', function() {
        var data = {
            sensor_mode: $('settingSensorMode').value,
            sensor_gpio_pin: $('settingGpioPin').value,
            sensor_uart_port: $('settingUartPort').value,
            sensor_uart_baud: $('settingUartBaud').value,
            sensor_program_gates: $('settingProgramGates').checked ? 'true' : 'false',
            sensor_distance_max_cm: $('settingMaxDist').value,
            sensor_distance_min_cm: $('settingMinDist').value,
            sensor_hysteresis_cm: $('settingHysteresis').value,
            sensor_moving_energy_min: $('settingMovingEnergy').value,
            sensor_stationary_energy_min: $('settingStationaryEnergy').value,
            sensor_use_stationary: $('settingUseStationary').checked ? 'true' : 'false',
            display_off_timeout: $('settingTimeout').value,
            presence_confirm_ms: $('settingConfirmMs').value,
            display_backend: $('settingDisplayBackend').value,
            display_output: $('settingDisplayOutput').value || 'auto',
            display_rotate: $('settingDisplayRotate').value,
            schedule_enabled: $('settingScheduleEnabled').checked ? 'true' : 'false',
            schedule_start: $('settingScheduleStart').value,
            schedule_end: $('settingScheduleEnd').value,
        };
        apiPost('/api/settings', data, function(err) {
            if (err) { showToast('Failed to save', 'error'); return; }
            showToast('Sensor settings saved', 'success');
            $('sensorSaveHint').textContent = 'Applied — the daemon picks this up immediately.';
            setTimeout(function() { $('sensorSaveHint').textContent = ''; }, 4000);
        });
    });

    $('settingSensorMode').addEventListener('change', toggleSensorFields);
    $('settingScheduleEnabled').addEventListener('change', toggleSensorFields);
    $('settingMaxDist').addEventListener('input', syncRangeLabels);
    $('settingTimeout').addEventListener('input', syncRangeLabels);

    // --- Display override (auto / on / off) ---
    var overrideButtons = qsa('#overrideSeg button');
    for (var oi = 0; oi < overrideButtons.length; oi++) {
        overrideButtons[oi].addEventListener('click', function() {
            var mode = this.getAttribute('data-mode');
            for (var j = 0; j < overrideButtons.length; j++) {
                overrideButtons[j].classList.remove('active');
            }
            this.classList.add('active');
            apiPost('/api/presence/override', { mode: mode }, function(err) {
                if (err) showToast('Could not reach the presence daemon', 'error');
                else showToast('Display set to ' + mode, 'success');
                pollPresence();
            });
        });
    }

    $('wakeBtn').addEventListener('click', function() {
        apiPost('/api/presence/wake', { seconds: 300 }, function(err) {
            if (err) showToast('Could not reach the presence daemon', 'error');
            else showToast('Display will stay on for 5 minutes', 'success');
            pollPresence();
        });
    });

    // --- Sensor scan ---
    $('scanSensorBtn').addEventListener('click', function() {
        var button = this;
        var status = $('sensorScanStatus');
        button.disabled = true;
        status.className = 'status-msg';
        status.style.display = 'block';
        status.textContent = 'Scanning serial ports…';

        apiPost('/api/sensor/scan', { save: true }, function(err, data) {
            button.disabled = false;
            if (err || !data) {
                status.className = 'status-msg error';
                status.textContent = 'Scan failed: ' + (err || 'unknown error');
                return;
            }
            if (!data.found) {
                status.className = 'status-msg error';
                status.textContent = 'No sensor found. Checked: ' +
                    ((data.ports_checked || []).join(', ') || 'no serial ports at all') +
                    '. Run "sudo ./wallcal.sh install --only uart" and reboot.';
                return;
            }
            status.className = 'status-msg success';
            status.textContent = 'Found on ' + data.port + ' @ ' + data.baudrate + ' baud — saved.';
            fetchSettings();
        });
    });

    // --- Calibration ---
    $('calibrateBtn').addEventListener('click', startCalibration);

    $('calibrateCancelBtn').addEventListener('click', function() {
        if (this.textContent === 'Close') {
            stopCalibratePolling();
            $('calibratePanel').style.display = 'none';
            $('calibrateBtn').disabled = false;
            return;
        }
        apiPost('/api/sensor/calibrate/cancel', {}, function() {
            stopCalibratePolling();
            $('calibratePanel').style.display = 'none';
            $('calibrateBtn').disabled = false;
        });
    });

    $('calibrateApplyBtn').addEventListener('click', function() {
        var button = this;
        button.disabled = true;
        apiPost('/api/sensor/calibrate/apply', {}, function(err, data) {
            button.disabled = false;
            if (err || (data && data.error)) {
                showToast('Could not apply the calibration', 'error');
                return;
            }
            showToast('Calibration applied', 'success');
            $('calibratePanel').style.display = 'none';
            $('calibrateBtn').disabled = false;
            fetchSettings();   // pull the new thresholds into the form
        });
    });

    // --- Display backends ---
    $('detectBackendBtn').addEventListener('click', loadDisplayBackends);

    $('testDisplayBtn').addEventListener('click', function() {
        showToast('Blinking the panel…', 'success');
        apiPost('/api/presence/override', { mode: 'off' }, function() {
            setTimeout(function() {
                apiPost('/api/presence/override', { mode: 'on' }, function() {
                    setTimeout(function() {
                        apiPost('/api/presence/override', { mode: 'auto' }, function() {
                            pollPresence();
                        });
                    }, 1500);
                });
            }, 2500);
        });
    });

    // --- Theme checkbox in settings ---
    $('settingTheme').addEventListener('change', function() {
        var theme = this.checked ? 'dark' : 'light';
        document.body.setAttribute('data-theme', theme);
    });

    // --- Keep the panel awake while somebody is actually using it ---
    // Radar can lose a person who stands very still at the edge of
    // range; a touch is unambiguous proof that someone is there.
    var lastWakePing = 0;
    function pingWake() {
        var now = Date.now();
        if (now - lastWakePing < 30000) return;  // throttle to 1 per 30s
        lastWakePing = now;
        apiPost('/api/presence/wake', { seconds: 120 }, function() {});
    }
    var wakeEvents = ['pointerdown', 'touchstart', 'keydown'];
    for (var wi = 0; wi < wakeEvents.length; wi++) {
        document.addEventListener(wakeEvents[wi], pingWake, { passive: true });
    }

    // Initial render
    renderWall();
}

function clearAddForm() {
    $('newCalUrl').value = '';
    $('newCalUser').value = '';
    $('newCalPass').value = '';
    $('newCalColor').value = '#00d4aa';
    $('newCalProvider').value = 'nextcloud';
    $('connectionStatus').style.display = 'none';
    $('discoveredCalendars').innerHTML = '';
}

// ===============================================================
// BOOT
// ===============================================================
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else {
    init();
}
    })();
