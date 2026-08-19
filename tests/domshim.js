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

function __node(id) {
    var node = {
        id: id, _attrs: {}, _cls: {}, style: { setProperty: function (k, v) { this[k] = v; } },
        tagName: 'DIV', hidden: false, textContent: '', value: '',
        checked: false, dataset: {}, children: [],
        getAttribute: function (k) { return (k in this._attrs) ? this._attrs[k] : null; },
        setAttribute: function (k, v) { this._attrs[k] = String(v); },
        removeAttribute: function (k) { delete this._attrs[k]; },
        appendChild: function (c) { this.children.push(c); return c; },
        addEventListener: function (evt, fn) {
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
    this.open = function (m, u) { this._u = u; };
    this.setRequestHeader = function () {};
    this.send = function () {
        __requests.push(this._u);
        var key = Object.keys(__routes).filter(function (k) {
            return this._u.indexOf(k) === 0;
        }, this)[0];
        this.status = __status[this._u] !== undefined ? __status[this._u] : (key ? 200 : 404);
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
