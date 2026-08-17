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
// WIDGETS
//
// The server decides visibility and hands back final shapes, so nothing
// here works out whether a widget belongs on screen — it only draws.
//
// Slots are reserved in the grid whether or not their widget is showing.
// A dashboard that reshuffles itself every time a bus becomes due looks
// broken rather than clever.
// ===============================================================
var widgetTimer = null;

function fetchWidgets() {
    apiGet('/api/widgets', function(err, data) {
        if (err || !data) return;      // keep whatever is on screen
        state.widgets = data;
        renderWidgets();
    });
}

function renderWidgets() {
    var w = state.widgets || {};
    renderWeather(w.weather);
    renderTransit(w.transit);
    renderTravel(w.travel);
    renderAbfall();
    renderQr(w.qr);
    markFeedStaleness(w.feeds);
}

/** A feed quietly serving old data gets the same dot as a failed sync. */
function markFeedStaleness(list) {
    if (!list || !list.length) return;
    for (var i = 0; i < list.length; i++) {
        if (list[i].stale) { markStale(true); return; }
    }
}

// --- Weather ---------------------------------------------------------
function renderWeather(w) {
    var far = $('farWeather'), near = $('nearWeather');
    if (!w || !w.visible) { far.hidden = true; near.hidden = true; return; }

    var temp = (w.temperature === null || w.temperature === undefined)
        ? '' : w.temperature + (w.units || '°');
    far.hidden = false;
    far.innerHTML = '';
    if (temp) far.appendChild(document.createTextNode(temp));
    if (w.headline) {
        var sub = document.createElement('span');
        sub.className = 'sub';
        sub.textContent = (temp ? ' · ' : '') + w.headline;
        far.appendChild(sub);
    }

    // NEAR gets today's shape and tomorrow's number — still not a 7-day grid.
    near.hidden = false;
    near.innerHTML = '';
    var head = document.createElement('div');
    head.className = 'wx-head';
    head.textContent = temp + (w.headline ? ' · ' + w.headline : '');
    near.appendChild(head);

    if (w.hourly && w.hourly.length) {
        var row = document.createElement('div');
        row.className = 'wx-hours';
        for (var i = 0; i < w.hourly.length; i++) {
            var h = w.hourly[i];
            var cell = document.createElement('span');
            cell.className = 'wx-h';
            cell.innerHTML = '<b>' + (h.temp === null ? '–' : h.temp) + '</b>' + h.at;
            if (h.rain !== null && h.rain >= 40) cell.className += ' wet';
            row.appendChild(cell);
        }
        near.appendChild(row);
    }
    if (w.tomorrow) {
        var tm = document.createElement('div');
        tm.className = 'wx-tomorrow';
        tm.textContent = 'Morgen ' + (w.tomorrow.min !== null ? w.tomorrow.min + '°' : '')
            + ' bis ' + (w.tomorrow.max !== null ? w.tomorrow.max + '°' : '');
        near.appendChild(tm);
    }
}

// --- ÖPNV ------------------------------------------------------------
function renderTransit(t) {
    var host = $('transitBoard');
    if (!t || !t.visible) { host.hidden = true; return; }
    host.hidden = false;
    host.innerHTML = '';

    var head = document.createElement('div');
    head.className = 'rail-k';
    head.textContent = t.station || 'Abfahrten';
    host.appendChild(head);

    if (!t.departures.length) {
        var empty = document.createElement('div');
        empty.className = 'empty';
        empty.textContent = 'Keine Abfahrten';
        host.appendChild(empty);
        return;
    }

    for (var i = 0; i < t.departures.length; i++) {
        var d = t.departures[i];
        var row = document.createElement('div');
        row.className = 'dep' + (d.cancelled ? ' cancelled' : '');

        var line = document.createElement('span');
        line.className = 'line';
        line.textContent = d.line;
        row.appendChild(line);

        var dir = document.createElement('span');
        dir.className = 'dir';
        dir.textContent = d.direction;
        row.appendChild(dir);

        var when = document.createElement('span');
        when.className = 'when' + (d.minutes !== null && d.minutes <= 5 ? ' soon' : '');
        when.textContent = d.when;
        if (d.delay > 0) {
            var late = document.createElement('span');
            late.className = 'late';
            late.textContent = ' +' + d.delay;
            when.appendChild(late);
        }
        row.appendChild(when);
        host.appendChild(row);
    }
}

// --- Travel time -----------------------------------------------------
function renderTravel(t) {
    var host = $('travelHint');
    if (!t || !t.visible) { host.hidden = true; return; }
    host.hidden = false;
    host.className = 'travel' + (t.late ? ' late' : '');
    host.innerHTML = '';
    var strong = document.createElement('b');
    strong.textContent = 'Losfahren um ' + t.leave_at;
    host.appendChild(strong);
    var sub = document.createElement('span');
    sub.textContent = ' · ' + t.minutes + ' min nach ' + (t.location || '');
    host.appendChild(sub);
}

// --- Abfall ----------------------------------------------------------
//
// The banner window straddles midnight: from a time the day before
// collection until a time on the day itself.
function abfallState(now) {
    var data = state.abfall;
    if (!data || !data.days || !data.days.length) return null;

    var from = hhmmToMinutes(data.from_hour, 16 * 60);
    var until = hhmmToMinutes(data.until_hour, 10 * 60);
    var nowMin = now.getHours() * 60 + now.getMinutes();

    for (var i = 0; i < data.days.length; i++) {
        var day = data.days[i];
        var d = new Date(day.date + 'T00:00:00');
        if (isNaN(d.getTime())) continue;
        var diffDays = Math.round((startOfDay(d) - startOfDay(now)) / 86400000);

        if (diffDays === 1 && nowMin >= from) return { day: day, when: 'morgen' };
        if (diffDays === 0 && nowMin < until) return { day: day, when: 'heute' };
        if (diffDays >= 0) return { day: day, when: null, upcoming: true };
    }
    return null;
}

function renderAbfall() {
    var vis = (state.settings.widget_abfall || 'dynamic');
    var hosts = [$('farBin'), $('nearBin')];
    var i;
    if (vis === 'off') {
        for (i = 0; i < hosts.length; i++) hosts[i].hidden = true;
        return;
    }

    var st = abfallState(new Date());
    // dynamic shows only inside the banner window; always shows the next
    // collection date whenever there is one.
    var show = st && (st.when !== null || vis === 'always');
    for (i = 0; i < hosts.length; i++) {
        var host = hosts[i];
        if (!show) { host.hidden = true; continue; }
        host.hidden = false;
        host.innerHTML = '';
        // Several fractions on one day render together rather than the last
        // one overwriting the rest.
        for (var f = 0; f < st.day.fractions.length; f++) {
            var fr = st.day.fractions[f];
            var swatch = document.createElement('i');
            swatch.style.background = fr.color;
            host.appendChild(swatch);
            var label = document.createElement('span');
            label.textContent = st.when ? fr.label + ' raus' : fr.label;
            host.appendChild(label);
        }
        if (!st.when) {
            var date = document.createElement('span');
            date.className = 'sub';
            date.textContent = ' ' + fdate(fmt.weekdaySm, new Date(st.day.date + 'T00:00:00'));
            host.appendChild(date);
        }
    }
}

// --- QR companion ----------------------------------------------------
function renderQr(q) {
    var host = $('qrBox');
    if (!q || !q.visible) { host.hidden = true; return; }
    // dynamic means NEAR only: a QR code is useless from across the room.
    var density = $('app').getAttribute('data-density');
    if (q.mode === 'dynamic' && density !== 'near') { host.hidden = true; return; }
    host.hidden = false;
    var size = q.size || 96;
    if (host.getAttribute('data-size') !== String(size)) {
        host.setAttribute('data-size', String(size));
        host.style.width = size + 'px';
        host.style.height = size + 'px';
        host.innerHTML = '';
        var img = document.createElement('img');
        img.src = '/api/qr.svg?size=' + size;
        img.alt = 'Einstellungen';
        img.width = size; img.height = size;
        host.appendChild(img);
    }
}

// ===============================================================
// SCREENSAVER
// ===============================================================
function applyScreensaver(s) {
    var app = $('app');
    var saver = (s && s.screensaver) || {};
    app.setAttribute('data-saver', saver.active ? (saver.style || 'dim_dashboard') : '');
    // Drift is forced on while the screensaver is up, whatever the setting:
    // this is the state that runs for hours with nobody watching.
    if (saver.active) app.setAttribute('data-drift', 'on');
}

// ===============================================================
// TIME-OF-DAY LAYOUT
// ===============================================================
function applyTimeOfDay(now) {
    var app = $('app');
    if (state.settings.timeofday_enabled !== 'true') {
        app.setAttribute('data-slot', '');
        app.removeAttribute('data-tod');
        return;
    }
    var mins = now.getHours() * 60 + now.getMinutes();
    var morningUntil = hhmmToMinutes(state.settings.timeofday_morning_until, 11 * 60);
    var eveningFrom = hhmmToMinutes(state.settings.timeofday_evening_from, 17 * 60);
    var slot = mins < morningUntil ? 'morning'
             : (mins >= eveningFrom ? 'evening' : 'midday');
    if (app.getAttribute('data-tod') === slot) return;
    app.setAttribute('data-tod', slot);

    var wanted = String(state.settings['timeofday_' + slot] || '').split(',');
    var map = {
        transit: 'transitBoard', weather: 'nearWeather',
        weather_tomorrow: 'nearWeather', abfall: 'nearBin',
        agenda_today: 'calGrid', agenda_tomorrow: 'calGrid'
    };
    // Reuse the density crossfade rather than inventing a second transition.
    for (var key in map) {
        var el = $(map[key]);
        if (!el) continue;
        el.classList.toggle('tod-hidden', wanted.indexOf(key) < 0);
    }
}

// ===============================================================
// Small helpers
// ===============================================================
function hhmmToMinutes(text, dflt) {
    var parts = String(text || '').split(':');
    var h = parseInt(parts[0], 10), m = parseInt(parts[1] || '0', 10);
    if (isNaN(h) || isNaN(m)) return dflt;
    return h * 60 + m;
}

function startOfDay(d) {
    return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
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
    renderWidgets();
    applyTimeOfDay(new Date());
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
        state.abfall = data.abfall || null;
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
        document.body.setAttribute('data-theme', data.theme || 'dark');
        document.body.setAttribute('data-animations',
            data.animations_enabled === 'true' ? 'on' : 'off');
        applyWallSettings(data);
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
            applyScreensaver(data);
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

function init() {
    updateClock();
    clockTimer = setInterval(updateClock, 1000);
    countdownTimer = setInterval(updateCountdown, 1000);

    fetchSettings();
    fetchEvents();
    pollTimer = setInterval(fetchEvents, 5 * 60 * 1000);

    // Widgets have their own cadence: the server only refetches when a TTL
    // has lapsed, and it skips the network entirely while the panel is dark.
    fetchWidgets();
    widgetTimer = setInterval(fetchWidgets, 30000);

    // Re-render on the minute so the fortnight rolls over at midnight without
    // waiting for the next CalDAV poll.
    setInterval(function() {
        if (new Date().getSeconds() === 0) renderWall();
    }, 1000);

    // Density needs sub-second latency; this endpoint is ~140 bytes.
    watchForWake();
    wakeWatchTimer = setInterval(watchForWake, 500);

    // Settings live on their own page now — the wall has no pointer, and the
    // usual way in is scanning the QR code off it.
    var button = $('settingsBtn');
    if (button) {
        button.addEventListener('click', function() {
            window.location.href = '/settings';
        });
    }

    // A touch is unambiguous proof somebody is there, which radar can miss
    // when they stand very still at the edge of range.
    var lastWakePing = 0;
    function pingWake() {
        var now = Date.now();
        if (now - lastWakePing < 30000) return;
        lastWakePing = now;
        apiPost('/api/presence/wake', { seconds: 120 }, function() {});
    }
    ['pointerdown', 'touchstart', 'keydown'].forEach(function(evt) {
        document.addEventListener(evt, pingWake, { passive: true });
    });
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else {
    init();
}
    })();
