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
    // Each widget is drawn behind its own guard. One of them throwing used to
    // take every widget after it down with it, which on a wall reads as half
    // the display having vanished for no stated reason.
    draw('weather', function () { renderWeather(w.weather); });
    draw('transit', function () { renderTransit(w.transit); });
    draw('travel', function () { renderTravel(w.travel); });
    draw('abfall', function () { renderAbfall(); });
    draw('qr', function () { renderQr(w.qr); });
    draw('feeds', function () { markFeedStaleness(w.feeds); });
}

function draw(name, fn) {
    try {
        fn();
    } catch (e) {
        if (window.console && console.warn) {
            console.warn('widget ' + name + ' failed to render: ' + e);
        }
    }
}

/** A feed quietly serving old data gets the same dot as a failed sync. */
function markFeedStaleness(list) {
    if (!list || !list.length) return;
    for (var i = 0; i < list.length; i++) {
        if (list[i].stale) { markStale(true); return; }
    }
}

// --- Weather ---------------------------------------------------------

//: WMO weather codes, as Open-Meteo reports them, mapped onto the symbol
//: set in the template. Grouped rather than enumerated: the distinctions
//: WMO draws between "light" and "moderate" drizzle are not ones anybody
//: needs from across a kitchen.
function weatherIcon(code, isDay) {
    var c = (code === null || code === undefined) ? -1 : Number(code);
    if (c === 0 || c === 1) return isDay === false ? 'i-moon' : 'i-sun';
    if (c === 2) return isDay === false ? 'i-partly-night' : 'i-partly';
    if (c === 3) return 'i-cloud';
    if (c === 45 || c === 48) return 'i-fog';
    if (c >= 51 && c <= 57) return 'i-drizzle';
    if (c >= 61 && c <= 67) return 'i-rain';
    if ((c >= 71 && c <= 77) || c === 85 || c === 86) return 'i-snow';
    if (c >= 80 && c <= 82) return 'i-rain';
    if (c >= 95) return 'i-storm';
    return 'i-cloud';
}

function isWet(code) {
    var c = Number(code);
    return (c >= 51 && c <= 67) || (c >= 80 && c <= 99);
}

/** An <svg><use> node for one symbol. */
function icon(name, size, className) {
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', size);
    svg.setAttribute('height', size);
    svg.setAttribute('viewBox', '0 0 24 24');
    if (className) svg.setAttribute('class', className);
    var use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#' + name);
    svg.appendChild(use);
    return svg;
}

function renderWeather(w) {
    var far = $('farWeather'), near = $('nearWeather');
    if (!w || !w.visible) { far.hidden = true; near.hidden = true; return; }

    var unit = w.units || '°';
    var temp = (w.temperature === null || w.temperature === undefined)
        ? '' : w.temperature + unit;

    // FAR: one line. The symbol would be a decoration at four metres.
    far.hidden = false;
    far.innerHTML = '';
    if (temp) far.appendChild(document.createTextNode(temp));
    if (w.headline) {
        var sub = document.createElement('span');
        sub.className = 'sub';
        sub.textContent = (temp ? ' · ' : '') + w.headline;
        far.appendChild(sub);
    }

    // NEAR: the arrangement a phone uses, because everyone can read it.
    near.hidden = false;
    near.innerHTML = '';

    var now = document.createElement('div');
    now.className = 'wx-now';
    var deg = document.createElement('span');
    deg.className = 'deg';
    deg.textContent = temp;
    now.appendChild(deg);

    var meta = document.createElement('span');
    meta.className = 'meta';
    var line = document.createElement('b');
    line.textContent = w.headline || w.place || '';
    meta.appendChild(line);
    if (w.feels_like !== null && w.feels_like !== undefined) {
        meta.appendChild(document.createTextNode('gefühlt ' + w.feels_like + unit
            + (w.place && w.headline ? ' · ' + w.place : '')));
    }
    now.appendChild(meta);

    var big = icon(weatherIcon(w.code, w.is_day), 34,
                   'icon' + (isWet(w.code) ? ' wet' : ''));
    big.setAttribute('class', 'icon' + (isWet(w.code) ? ' wet' : ''));
    now.appendChild(big);
    near.appendChild(now);

    if (w.hourly && w.hourly.length) {
        var hours = document.createElement('div');
        hours.className = 'wx-hours';
        for (var i = 0; i < w.hourly.length && i < 5; i++) {
            var h = w.hourly[i];
            var cell = document.createElement('div');
            cell.className = 'wx-h' + (isWet(h.code) ? ' wet' : '');

            var tt = document.createElement('span');
            tt.className = 't';
            tt.textContent = (h.temp === null ? '–' : h.temp + '°');
            cell.appendChild(tt);
            cell.appendChild(icon(weatherIcon(h.code, h.is_day), 17));

            var rain = document.createElement('span');
            var pct = (h.rain === null || h.rain === undefined) ? 0 : h.rain;
            rain.className = 'p' + (pct < 20 ? ' dry' : '');
            rain.textContent = pct + ' %';
            cell.appendChild(rain);

            var hh = document.createElement('span');
            hh.className = 'hh';
            hh.textContent = h.at;
            cell.appendChild(hh);
            hours.appendChild(cell);
        }
        near.appendChild(hours);
    }

    if (w.tomorrow && w.tomorrow.max !== null) {
        var tm = document.createElement('div');
        tm.className = 'wx-tomorrow';
        tm.appendChild(icon(weatherIcon(w.tomorrow.code, true), 14));
        tm.appendChild(document.createTextNode(
            'Morgen ' + w.tomorrow.min + '° bis ' + w.tomorrow.max + '°'));
        near.appendChild(tm);
    }
}

//: German transit signage colours. Falls through to neutral for anything
//: unrecognised rather than inventing a colour for it.
var MODE_COLOURS = {
    SUBURBAN: '#3B8A3F', SUBWAY: '#2C6BC4', TRAM: '#C0392B',
    BUS: '#7B4EA8', COACH: '#7B4EA8', REGIONAL_RAIL: '#5A6070',
    HIGHSPEED_RAIL: '#5A6070', LONG_DISTANCE: '#5A6070', FERRY: '#1F7A8C'
};

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
        row.className = 'dep'
            + (d.cancelled ? ' cancelled' : '')
            + (!d.cancelled && d.minutes !== null && d.minutes >= 0 && d.minutes <= 8 ? ' soon' : '');

        var line = document.createElement('span');
        line.className = 'line';
        line.textContent = d.line;
        var colour = MODE_COLOURS[String(d.mode || '').toUpperCase()];
        if (colour) line.style.background = colour;
        row.appendChild(line);

        var dir = document.createElement('span');
        dir.className = 'dir';
        dir.textContent = d.direction;
        row.appendChild(dir);

        var when = document.createElement('span');
        when.className = 'when';
        when.textContent = d.cancelled ? 'fällt aus' : d.when;
        if (!d.cancelled && d.delay > 0) {
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
    var near = $('nearBin'), far = $('farBin');
    if (vis === 'off') { near.hidden = true; far.hidden = true; return; }

    var st = abfallState(new Date());
    // dynamic shows only inside the banner window; always keeps the next
    // collection date up permanently.
    var show = st && (st.when !== null || vis === 'always');
    if (!show) { near.hidden = true; far.hidden = true; return; }

    var fractions = st.day.fractions;
    var primary = fractions[0] || { label: 'Abfall', color: '#8A8F9C' };
    var when = st.when === 'heute' ? 'Heute' : (st.when === 'morgen' ? 'Morgen' : null);

    // NEAR: a card at the top of the rail, in the fraction's own colour.
    near.hidden = false;
    near.style.setProperty('--fraction', primary.color);
    near.innerHTML = '';

    var row = document.createElement('div');
    row.className = 'abfall-row';
    row.appendChild(icon('i-bin', 30));

    var text = document.createElement('div');
    var what = document.createElement('div');
    what.className = 'abfall-what';
    what.textContent = when ? primary.label + ' raus' : primary.label;
    text.appendChild(what);

    var sub = document.createElement('div');
    sub.className = 'abfall-when';
    sub.textContent = when
        ? when + (st.when === 'morgen' ? ' früh abholen' : ' abholen')
        : fdate(fmt.weekdaySm, new Date(st.day.date + 'T00:00:00')) + ', '
          + fdate(fmt.dayMonth, new Date(st.day.date + 'T00:00:00'));
    text.appendChild(sub);
    row.appendChild(text);
    near.appendChild(row);

    // Several fractions on one day render together rather than the last one
    // overwriting the rest.
    if (fractions.length > 1) {
        var more = document.createElement('div');
        more.className = 'abfall-more';
        for (var i = 1; i < fractions.length; i++) {
            var chip = document.createElement('span');
            chip.className = 'abfall-chip';
            chip.style.color = fractions[i].color;
            chip.textContent = fractions[i].label;
            more.appendChild(chip);
        }
        near.appendChild(more);
    }

    // FAR: the same fact as a banner, big enough to read across the room.
    far.hidden = false;
    far.style.setProperty('--fraction', primary.color);
    far.innerHTML = '';
    far.appendChild(icon('i-bin', 40));
    var label = document.createElement('span');
    label.textContent = when ? primary.label + ' raus' : primary.label;
    far.appendChild(label);
    if (fractions.length > 1) {
        var extra = document.createElement('span');
        extra.className = 'sub';
        extra.textContent = '+ ' + (fractions.length - 1);
        far.appendChild(extra);
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
function setDensity(which) {
    var app = $('app');
    if (which !== 'near' && which !== 'far') return;
    if (app.getAttribute('data-density') === which) return;
    app.setAttribute('data-density', which);
}

// ===============================================================
// LIVE STATE
//
// Pushed over SSE rather than polled. The daemon decides the density from
// every radar frame it sees, so all the browser has to learn is that the
// answer changed — a few times a minute, not four times a second.
//
// Polling stays as the fallback: if the stream never opens or drops, a
// wall that quietly stops responding is the worst outcome here.
// ===============================================================
var live = null;
var livePollTimer = null;
var lastDisplayOn = null;

function applyLive(data) {
    if (!data) return;
    state.presence = data;
    if (data.daemon_running) {
        var on = data.display_on;
        if (on === true && lastDisplayOn === false) {
            forceRepaint();
            fetchEvents();      // whoever walked up wants current data
        }
        if (on === true || on === false) lastDisplayOn = on;
        applyPanelState(data);
        applyScreensaver(data);
    }
    setDensity(data.density);
}

function startLive() {
    if (typeof EventSource === 'undefined') { startLivePolling(); return; }
    try {
        live = new EventSource('/api/presence/stream');
    } catch (e) {
        startLivePolling();
        return;
    }
    live.onmessage = function (evt) {
        stopLivePolling();
        try { applyLive(JSON.parse(evt.data)); } catch (e) {}
    };
    live.onerror = function () {
        // EventSource retries on its own, but the wall must keep working in
        // the meantime, so the slow poll takes over until a message arrives.
        startLivePolling();
    };
}

function startLivePolling() {
    if (livePollTimer) return;
    livePollTimer = setInterval(function () {
        apiGet('/api/presence/live', function (err, data) {
            if (!err) applyLive(data);
        });
    }, 2000);
}

function stopLivePolling() {
    if (!livePollTimer) return;
    clearInterval(livePollTimer);
    livePollTimer = null;
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

/** Staleness is decided from the cache age, not from a failed request:
 *  a feed quietly serving old data looks identical to a healthy one. */
function updateSyncStatus() {
    var age = cacheAgeMinutes();
    if (age === null) return;
    markStale(age > num(state.settings.poll_interval_minutes, 5) * 3);
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
    setInterval(updateSyncStatus, 60000);

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

    // Panel state arrives pushed; nothing here polls for it.
    startLive();

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
