// Minimal DOM + XHR + timer shim so the wall display's JavaScript can be
// exercised headlessly. Not a browser: just enough of one that the render
// and density logic run for real instead of being read and hoped over.
var __log = [];
var __now = 1755000000000;
var __timers = [];
var __routes = {};
var __status = {};
var __handlers = {};
var __detailsNodes = [];
var __requests = [];
var __posts = [];

function __node(id) {
    var node = {
        id: id, _attrs: {}, _cls: {}, _on: {},
        style: { setProperty: function (k, v) { this[k] = v; } },
        tagName: 'DIV', hidden: false, textContent: '', value: '',
        checked: false, dataset: {}, children: [],
        getAttribute: function (k) { return (k in this._attrs) ? this._attrs[k] : null; },
        setAttribute: function (k, v) { this._attrs[k] = String(v); },
        removeAttribute: function (k) { delete this._attrs[k]; },
        appendChild: function (c) { this.children.push(c); return c; },
        addEventListener: function (evt, fn) {
            // Per node as well as per id: the settings page builds its
            // editors out of elements that never get an id, and a handler
            // filed only under '' cannot be told apart from any other.
            (this._on[evt] = this._on[evt] || []).push(fn);
            if (evt !== 'click' && evt !== 'change' && evt !== 'input') return;
            (__handlers[this.id] = __handlers[this.id] || []).push(fn);
        },
        querySelector: function () { return __node('q'); },
        querySelectorAll: function () { return []; },
        classList: {
            _s: {},
            add: function (c) { this._s[c] = 1; },
            remove: function (c) { delete this._s[c]; },
            toggle: function (c, on) { if (on) this._s[c] = 1; else delete this._s[c]; },
            contains: function (c) { return !!this._s[c]; }
        },
        scrollIntoView: function () {}, focus: function () {},
        getBoundingClientRect: function () { return { top: 0 }; }
    };
    // innerHTML = '' is how the renderers clear a node before redrawing it.
    // Leaving children in place made every read return the first render ever
    // performed, which quietly turned these tests into a lie.
    var _html = '';
    Object.defineProperty(node, 'innerHTML', {
        get: function () { return _html; },
        set: function (v) { _html = String(v); if (!v) node.children.length = 0; }
    });
    return node;
}

var __nodes = {};
// The template ships data-density="far"; the shim has to start there too or
// the first transition looks like it came from nowhere.
(function () { var a = __node('app'); a._attrs['data-density'] = 'far'; __nodes['app'] = a; })();
var document = {
    readyState: 'complete',
    body: __node('body'),
    documentElement: __node('html'),
    getElementById: function (id) {
        if (!__nodes[id]) __nodes[id] = __node(id);
        return __nodes[id];
    },
    querySelector: function (sel) {
        var r = this.querySelectorAll(sel);
        return r.length ? r[0] : null;
    },
    querySelectorAll: function (sel) {
        // Only the selector shapes the settings page actually uses.
        var m = /\[data-([a-z-]+)="([^"]+)"\]/.exec(sel);
        if (m) {
            return Object.keys(__nodes).map(function (k) { return __nodes[k]; })
                .filter(function (n) { return n.getAttribute('data-' + m[1]) === m[2]; });
        }
        // '#overrideSeg button' — declared in the template, so the harness
        // has to put them there before the page can paint them.
        var kids = /^#([A-Za-z0-9_-]+)\s+([a-z]+)$/.exec(sel);
        if (kids) {
            var host = __nodes[kids[1]];
            if (!host) return [];
            return (host.children || []).filter(function (n) {
                return n.tagName === kids[2].toUpperCase();
            });
        }
        if (sel.indexOf('.more[data-needs]') === 0) return __detailsNodes;
        if (sel.indexOf('[data-setting]') === 0)
            return Object.keys(__nodes).map(function (k) { return __nodes[k]; })
                   .filter(function (n) { return n.getAttribute('data-setting'); });
        if (sel.indexOf('[data-seg]') === 0)
            return Object.keys(__nodes).map(function (k) { return __nodes[k]; })
                   .filter(function (n) { return n.getAttribute('data-seg'); });
        return [];
    },
    createElement: function (t) { var n = __node('new-' + t); n.tagName = t.toUpperCase(); return n; },
    createElementNS: function (ns, t) { var n = __node('svg-' + t); n.tagName = t.toUpperCase(); return n; },
    createTextNode: function (t) { return { nodeValue: t, textContent: t }; },
    addEventListener: function () {}
};
var window = { location: { origin: 'http://wallcal.local:5005', host: 'wallcal.local:5005' },
               addEventListener: function () {} };
var localStorage = {
    _d: {}, _writes: 0,
    getItem: function (k) { return this._d[k] === undefined ? null : this._d[k]; },
    // Counted: on the Pi this is backed by the SD card, so "did it write"
    // is the thing worth asserting, not just "what does it hold".
    setItem: function (k, v) { this._writes++; this._d[k] = String(v); }
};
var console = { log: function (m) { __log.push(String(m)); },
                warn: function (m) { __log.push('WARN ' + m); },
                error: function (m) { __log.push('ERR ' + m); } };

// Cancellation is modelled for real: the wall stops its timers while the
// panel is dark, so a clearInterval that did nothing would let the suites
// "pass" against work the browser is no longer doing.
function setInterval(fn, ms) {
    __timers.push({ fn: fn, ms: ms, kind: 'interval', live: true });
    return __timers.length;
}
function setTimeout(fn, ms) {
    __timers.push({ fn: fn, ms: ms, kind: 'timeout', live: true });
    return __timers.length;
}
function clearInterval(i) { if (__timers[i - 1]) __timers[i - 1].live = false; }
function clearTimeout(i) { clearInterval(i); }
function __liveTimers() { return __timers.filter(function (t) { return t.live; }); }

function XMLHttpRequest() {
    this.open = function (m, u) { this._m = m; this._u = u; };
    this.setRequestHeader = function () {};
    this.send = function (body) {
        __requests.push(this._u);
        // What was written, not just where: a settings page is judged on
        // whether one gesture produces one coherent write.
        __posts.push({ url: this._u, body: body || null });

        // A route or status may be qualified by method — "POST /api/settings"
        // — because reading a setting and being refused when writing it is a
        // real pair of answers, not one.
        var qualified = this._m + ' ' + this._u;
        var key = __routes[qualified] !== undefined ? qualified
                : Object.keys(__routes).filter(function (k) {
                      return k.indexOf(' ') < 0 && this._u.indexOf(k) === 0;
                  }, this)[0];
        this.status = __status[qualified] !== undefined ? __status[qualified]
                    : (__status[this._u] !== undefined ? __status[this._u]
                                                       : (key ? 200 : 404));
        this.responseText = key ? JSON.stringify(__routes[key]) : '{}';
        if (this.onload) this.onload();
    };
}

// Drive one interval by its registered period.
function __tick(ms) {
    __now += ms;
    __liveTimers().filter(function (t) { return t.kind === 'interval' && t.ms === ms; })
                  .forEach(function (t) { t.fn(); });
}
function __tickAll() { __liveTimers().forEach(function (t) { t.fn(); }); }

/** Run pending setTimeout callbacks — the settings page debounces its
 *  writes, so nothing reaches the wall until these fire. */
function __flush(ms) {
    var due = __liveTimers().filter(function (t) {
        return t.kind === 'timeout' && (ms === undefined || t.ms === ms);
    });
    due.forEach(function (t) { t.live = false; t.fn(); });
    return due.length;
}

/** Everything under a node, depth first. */
function __all(rootId) {
    var out = [];
    (function walk(n) {
        (n.children || []).forEach(function (c) { out.push(c); walk(c); });
    })(document.getElementById(rootId));
    return out;
}

/** Find a control the way a person would: by what it says it is. */
function __byLabel(rootId, label) {
    return __all(rootId).filter(function (n) {
        return n.getAttribute('aria-label') === label;
    })[0] || null;
}

function __fire(node, evt) {
    if (!node || !node._on || !node._on[evt]) return 'NO HANDLER';
    node._on[evt].forEach(function (f) {
        f.call(node, { preventDefault: function () {}, target: node });
    });
    return 'fired';
}
function __density() { return document.getElementById('app').getAttribute('data-density'); }
Date.now = function () { return __now; };

// This V8 build ships without ICU. The formatters only need to not throw —
// the density logic under test does not care what the clock says.
var Intl = {
    DateTimeFormat: function (loc, opts) {
        return { format: function (d) { return String(d && d.getHours ? d.getHours() : ''); } };
    }
};


// Register a <details data-needs="..."> the way the template does, so the
// conditional-reveal logic has something to act on.
function __addDetails(needs) {
    var n = __node('details-' + needs);
    n.setAttribute('data-needs', needs);
    n.hidden = false;
    __detailsNodes.push(n);
    return n;
}
__addDetails('widget_transit');
__addDetails('widget_weather');
__addDetails('widget_travel');
__addDetails('widget_qr');


// The wall's script is an IIFE, so the suite reaches setDensity the way the
// daemon does: by delivering a live payload that carries a new density.
function __setDensityProbe(which) {
    __routes['/api/presence/live'] = {
        daemon_running: true, display_on: true, display_mode: 'normal',
        brightness: 100, brightness_source: 'css',
        density: which || 'near'
    };
    __tick(2000);
}
