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
// CLOCK
// ===============================================================
function updateClock() {
    var now = new Date();
    var h = now.getHours();
    var m = now.getMinutes();
    $('clock').textContent = (h < 10 ? '0' : '') + h + ':' + (m < 10 ? '0' : '') + m;
    $('dateDisplay').textContent = formatDate(now);
}

// ===============================================================
// CALENDAR GRID
// ===============================================================
function renderCalendar() {
    var year = state.viewYear;
    var month = state.viewMonth;
    var months = ['January','February','March','April','May','June',
                 'July','August','September','October','November','December'];
    $('monthTitle').textContent = months[month] + ' ' + year;

    // Day-of-week header
    var dowEl = $('dowHeader');
    dowEl.innerHTML = '';
    var dowNames = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
    for (var i = 0; i < 7; i++) {
        var span = document.createElement('span');
        span.textContent = dowNames[i];
        dowEl.appendChild(span);
    }

    // Build day cells
    var grid = $('calGrid');
    grid.innerHTML = '';

    var firstDay = new Date(year, month, 1);
    var startDow = (firstDay.getDay() + 6) % 7; // Monday = 0
    var daysInMonth = new Date(year, month + 1, 0).getDate();
    var today = new Date();

    // Previous month padding
    var prevMonthDays = new Date(year, month, 0).getDate();
    var startDate = prevMonthDays - startDow + 1;

    // Build event map: dateKey -> array of events
    var eventMap = {};
    for (var e = 0; e < state.events.length; e++) {
        var ev = state.events[e];
        var evStart = parseUTC(ev.dtstart);
        var evEnd = ev.dtend ? parseUTC(ev.dtend) : evStart;

        if (ev.all_day) {
            // Span all days
            var cursor = new Date(evStart);
            while (cursor < evEnd) {
                var dk = dateKey(cursor);
                if (!eventMap[dk]) eventMap[dk] = [];
                eventMap[dk].push(ev);
                cursor.setDate(cursor.getDate() + 1);
            }
        } else {
            var dk = dateKey(evStart);
            if (!eventMap[dk]) eventMap[dk] = [];
            eventMap[dk].push(ev);
        }
    }

    var totalCells = 42; // 6 rows × 7 days
    var cellDate = new Date(year, month, 1 - startDow);

    for (var c = 0; c < totalCells; c++) {
        var cell = document.createElement('div');
        cell.className = 'cal-day';

        var cellMonth = cellDate.getMonth();
        var cellDay = cellDate.getDate();
        var isCurrentMonth = cellMonth === month;
        var isToday = isSameDay(cellDate, today);
        var dayOfWeek = cellDate.getDay();
        var isWeekend = dayOfWeek === 0 || dayOfWeek === 6;

        if (!isCurrentMonth) cell.classList.add('other-month');
        if (isToday) cell.classList.add('today');
        if (isWeekend && isCurrentMonth) cell.classList.add('weekend');
        if (state.selectedDate && isSameDay(cellDate, state.selectedDate)) {
            cell.classList.add('selected');
        }

        // Day number
        var numSpan = document.createElement('div');
        numSpan.className = 'cal-day-number';
        numSpan.textContent = cellDay;
        cell.appendChild(numSpan);

        // Events for this day
        var dk = dateKey(cellDate);
        var dayEvents = eventMap[dk] || [];

        if (dayEvents.length > 0 && isCurrentMonth) {
            if (state.viewMode === 'grid') {
                // Show event items in cell
                var evContainer = document.createElement('div');
                evContainer.className = 'cal-day-events';
                var maxShow = 3;
                for (var ei = 0; ei < Math.min(dayEvents.length, maxShow); ei++) {
                    var evItem = document.createElement('div');
                    evItem.className = 'cal-event-item';
                    evItem.style.background = dayEvents[ei].color + '30';
                    evItem.style.borderLeft = '2px solid ' + (dayEvents[ei].color || 'var(--accent)');
                    var timePrefix = dayEvents[ei].all_day ? 'GT ' : formatTime(dayEvents[ei].dtstart) + ' ';
                    evItem.textContent = timePrefix + dayEvents[ei].summary;
                    evItem.title = dayEvents[ei].summary;
                    evContainer.appendChild(evItem);
                }
                if (dayEvents.length > maxShow) {
                    var more = document.createElement('div');
                    more.className = 'cal-event-more';
                    more.textContent = '+' + (dayEvents.length - maxShow) + ' more';
                    evContainer.appendChild(more);
                }
                cell.appendChild(evContainer);
            }

            // Always show dots at bottom
            var dotsRow = document.createElement('div');
            dotsRow.className = 'cal-day-dots';
            var seenColors = {};
            for (var di = 0; di < dayEvents.length; di++) {
                var col = dayEvents[di].color || '#00d4aa';
                if (!seenColors[col]) {
                    seenColors[col] = true;
                    var dot = document.createElement('div');
                    dot.className = 'cal-dot';
                    dot.style.background = col;
                    dotsRow.appendChild(dot);
                }
            }
            cell.appendChild(dotsRow);
        }

        // Click handler for selecting day
        (function(d) {
            cell.addEventListener('click', function() {
                state.selectedDate = new Date(d);
                renderCalendar();
                scrollAgendaToDate(d);
            });
        })(new Date(cellDate));

        grid.appendChild(cell);
        cellDate.setDate(cellDate.getDate() + 1);
    }
}

// ===============================================================
// AGENDA SIDEBAR
// ===============================================================
function renderAgenda() {
    var list = $('agendaList');
    list.innerHTML = '';

    var today = new Date();
    today.setHours(0, 0, 0, 0);

    // Group events by day for next 14 days
    var groups = {};
    var groupOrder = [];

    for (var i = 0; i < 14; i++) {
        var d = new Date(today);
        d.setDate(d.getDate() + i);
        var dk = dateKey(d);
        groups[dk] = { date: new Date(d), events: [] };
        groupOrder.push(dk);
    }

    for (var e = 0; e < state.events.length; e++) {
        var ev = state.events[e];
        var evStart = parseUTC(ev.dtstart);
        var evEnd = ev.dtend ? parseUTC(ev.dtend) : evStart;

        if (ev.all_day) {
            var cursor = new Date(evStart);
            while (cursor < evEnd) {
                var dk = dateKey(cursor);
                if (groups[dk]) groups[dk].events.push(ev);
                cursor.setDate(cursor.getDate() + 1);
            }
        } else {
            var dk = dateKey(evStart);
            if (groups[dk]) groups[dk].events.push(ev);
        }
    }

    var hasAny = false;
    for (var gi = 0; gi < groupOrder.length; gi++) {
        var group = groups[groupOrder[gi]];
        if (group.events.length === 0) continue;
        hasAny = true;

        var section = document.createElement('div');
        section.className = 'agenda-day-group';
        section.setAttribute('data-date', groupOrder[gi]);

        var label = document.createElement('div');
        label.className = 'agenda-day-label';
        if (isSameDay(group.date, new Date())) {
            label.classList.add('today-label');
            label.textContent = 'Today — ' + formatDayLabel(group.date);
        } else if (isSameDay(group.date, new Date(new Date().setDate(new Date().getDate()+1)))) {
            label.textContent = 'Tomorrow — ' + formatDayLabel(group.date);
        } else {
            label.textContent = formatDayLabel(group.date);
        }
        section.appendChild(label);

        // Sort events: all-day first, then by time
        group.events.sort(function(a, b) {
            if (a.all_day && !b.all_day) return -1;
            if (!a.all_day && b.all_day) return 1;
            return parseUTC(a.dtstart) - parseUTC(b.dtstart);
        });

        for (var ei = 0; ei < group.events.length; ei++) {
            var ev = group.events[ei];
            var item = document.createElement('div');
            item.className = 'agenda-event';

            var bar = document.createElement('div');
            bar.className = 'agenda-event-bar';
            bar.style.background = ev.color || 'var(--accent)';
            item.appendChild(bar);

            var content = document.createElement('div');
            content.className = 'agenda-event-content';

            var timeEl = document.createElement('div');
            timeEl.className = 'agenda-event-time';
            if (ev.all_day) {
                timeEl.textContent = 'GT';
            } else {
                timeEl.textContent = formatTime(ev.dtstart) + (ev.dtend ? ' – ' + formatTime(ev.dtend) : '');
            }
            content.appendChild(timeEl);

            var title = document.createElement('div');
            title.className = 'agenda-event-title';
            title.textContent = ev.summary || 'Untitled';
            content.appendChild(title);

            if (ev.location) {
                var loc = document.createElement('div');
                loc.className = 'agenda-event-location';
                loc.textContent = '📍 ' + ev.location;
                content.appendChild(loc);
            }

            item.appendChild(content);
            section.appendChild(item);
        }

        list.appendChild(section);
    }

    if (!hasAny) {
        list.innerHTML = '<div class="agenda-empty">No upcoming events in the next 14 days</div>';
    }
}

function formatDayLabel(d) {
    var days = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return days[d.getDay()] + ', ' + months[d.getMonth()] + ' ' + d.getDate();
}

function scrollAgendaToDate(d) {
    var dk = dateKey(d);
    var el = qs('[data-date="' + dk + '"]');
    if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
}

// ===============================================================
// DATA FETCHING
// ===============================================================
function fetchEvents() {
    $('syncDot').className = 'sync-dot polling';
    $('syncText').textContent = 'Syncing...';

    apiGet('/events', function(err, data) {
        if (err) {
            $('syncDot').className = 'sync-dot error';
            $('syncText').textContent = 'Sync failed';

            // Try localStorage fallback
            var cached = localStorage.getItem('wallcal_events');
            if (cached) {
                try {
                    var parsed = JSON.parse(cached);
                    state.events = parsed.events || [];
                    renderCalendar();
                    renderAgenda();
                    $('syncText').textContent = 'Using cached data';
                } catch(e) {}
            }
            return;
        }

        state.events = data.events || [];
        state.lastPoll = data.last_poll;

        // Cache to localStorage
        localStorage.setItem('wallcal_events', JSON.stringify(data));

        $('syncDot').className = 'sync-dot';
        updateSyncStatus();
        renderCalendar();
        renderAgenda();
    });

    // Reset countdown
    var interval = parseInt(state.settings.poll_interval_minutes || '5');
    state.nextPollCountdown = interval * 60;
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

        renderCalendar();
    });
}

function fetchCalendars() {
    apiGet('/api/calendars', function(err, data) {
        if (err) return;
        state.calendars = data.calendars || [];
        renderCalendarList();
    });
}

function updateSyncStatus() {
    if (state.lastPoll) {
        var d = new Date(state.lastPoll);
        var h = d.getHours();
        var m = d.getMinutes();
        var timeStr = (h < 10 ? '0' : '') + h + ':' + (m < 10 ? '0' : '') + m;
        $('syncText').textContent = 'Last sync ' + timeStr;
    }
}

function updateCountdown() {
    state.nextPollCountdown--;
    if (state.nextPollCountdown <= 0) {
        fetchEvents();
        return;
    }
    var mins = Math.floor(state.nextPollCountdown / 60);
    var secs = state.nextPollCountdown % 60;
    $('footerRight').textContent = 'Next sync in ' + mins + ':' + (secs < 10 ? '0' : '') + secs;
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
    apiGet('/api/presence', function(err, data) {
        if (err || !data || !data.daemon_running) return;
        var on = data.display_on;
        if (on === true && lastDisplayOn === false) {
            forceRepaint();
            fetchEvents();   // whoever walked up wants current data
        }
        if (on === true || on === false) lastDisplayOn = on;
    });
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

    // Catch the display waking so the panel never shows a stale frame.
    watchForWake();
    wakeWatchTimer = setInterval(watchForWake, 4000);

    // --- Navigation ---
    $('prevMonth').addEventListener('click', function() {
        state.viewMonth--;
        if (state.viewMonth < 0) { state.viewMonth = 11; state.viewYear--; }
        state.selectedDate = null;
        renderCalendar();
    });
    $('nextMonth').addEventListener('click', function() {
        state.viewMonth++;
        if (state.viewMonth > 11) { state.viewMonth = 0; state.viewYear++; }
        state.selectedDate = null;
        renderCalendar();
    });
    $('todayBtn').addEventListener('click', function() {
        var now = new Date();
        state.viewYear = now.getFullYear();
        state.viewMonth = now.getMonth();
        state.selectedDate = now;
        renderCalendar();
        scrollAgendaToDate(now);
    });

    // --- Theme toggle ---
    $('themeToggle').addEventListener('click', function() {
        var current = document.body.getAttribute('data-theme');
        var next = current === 'dark' ? 'light' : 'dark';
        document.body.setAttribute('data-theme', next);
        $('settingTheme').checked = (next === 'dark');
        this.textContent = next === 'dark' ? '☀' : '🌙';
        apiPost('/api/settings', { theme: next }, function() {});
    });

    // --- Refresh ---
    $('refreshBtn').addEventListener('click', function() {
        apiPost('/api/poll', {}, function(err) {
            if (!err) showToast('Sync triggered', 'success');
        });
        setTimeout(fetchEvents, 2000);
    });

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
                renderCalendar();
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
        $('themeToggle').textContent = theme === 'dark' ? '☀' : '🌙';
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
    renderCalendar();
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
