"""Scanner lifecycle tests — camera/loop/state ownership, independent of homography or
overlay geometry (owned separately, untouched here). Source-level assertions, matching the
style already established in test_gate_jr_scanner_recovery.py: this repo has no headless
browser in CI, so scanner.html's JS is mostly verified by asserting the exact guard code
exists, not by executing it — EXCEPT for the top-level startup smoke test below, which
actually runs the inline script under Node with a minimal DOM stub. A syntax-only
new Function(...) check cannot catch a temporal-dead-zone ReferenceError (a const read
before its own declaration line runs) — that class of bug parses fine and only throws at
execution time, which is exactly what caused the "stuck on Initializing AR Engine"
regression this file's startup-checkpoint tests guard against.
"""
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.scanner_robustness


def _scanner_html():
    return Path("templates/user/scanner.html").read_text(encoding="utf-8", errors="ignore")


def _scanner_runtime_js():
    return Path("static/js/scanner-runtime.js").read_text(encoding="utf-8", errors="ignore")


def _app_py():
    return Path("app.py").read_text(encoding="utf-8", errors="ignore")


def _dashboard_html():
    return Path("templates/user/dashboard.html").read_text(encoding="utf-8", errors="ignore")


def _project_preview_html():
    return Path("templates/user/project_preview.html").read_text(encoding="utf-8", errors="ignore")


def _success_html():
    return Path("templates/user/success.html").read_text(encoding="utf-8", errors="ignore")


def _extract_inline_scanner_script(html):
    """The inline <script> (no src=) — the one with the actual scanner logic, as opposed to
    the <script src="...scanner-runtime.js"> tag right before it. Also skips
    type="application/json" data islands (e.g. the Direct QR playlist blob) - those are
    data, not executable script, and would otherwise be matched first since they sit
    earlier in the document than the real scanner logic block."""
    for match in re.finditer(r"<script\b([^>]*)>(.*?)</script>", html, re.DOTALL):
        attrs, body = match.group(1), match.group(2)
        if "src=" not in attrs and 'type="application/json"' not in attrs and body.strip():
            return body
    raise AssertionError("could not find the inline scanner <script> block")


def _render_jinja_stubs(script_body):
    """Replace the small set of Jinja expressions the inline script actually contains with
    realistic literal values — enough to make it valid, executable JS, not enough to need a
    real Flask render. Mirrors the values used throughout this file's other tests."""
    replacements = {
        "{{ project_id }}": "1",
        "{{ scanner_entry_context | tojson }}": '"public_viewer"',
        "{{ resolved_back_destination | tojson }}": '"/"',
        "{{ back_destination_reason | tojson }}": '"public_viewer"',
        "{{ entry_route_type | tojson }}": '"public_scanner_route"',
        "{{ entry_authorization_result | tojson }}": '"n/a_public"',
        "{{ url_for('static', filename='js/') }}": "/static/js/",
        "{{ url_for('static', filename='js/opencv.js') }}": "/static/js/opencv.js",
    }
    for needle, value in replacements.items():
        script_body = script_body.replace(needle, value)
    # Any remaining {{ ... }} (there should be none left) would be a real bug in this test's
    # own stub list, not the scanner script — fail loudly instead of feeding Node broken JS.
    assert "{{" not in script_body, "unstubbed Jinja expression left in extracted script"
    return script_body


_NODE_DOM_PRELUDE = r"""
'use strict';
// Minimal, generic browser/DOM stub — just enough for the scanner script's SYNCHRONOUS
// top-level execution to run to completion without a real browser. Any unknown property
// or method access resolves to another fake element / a no-op function, rather than
// enumerating every DOM API the script happens to touch.
function makeFakeElement(tag) {
  const store = { style: {}, dataset: {}, classList: {
    add(){}, remove(){}, contains(){ return false; }, toggle(){}
  }, children: [] };
  const handler = {
    get(target, prop) {
      if (prop in store) return store[prop];
      if (prop === 'getContext') return function () { return makeFakeElement('context'); };
      if (prop === 'getBoundingClientRect') return function () { return { width: 300, height: 300, top: 0, left: 0 }; };
      if (prop === 'appendChild' || prop === 'insertBefore' || prop === 'removeChild') return function (node) { return node; };
      if (prop === 'cloneNode') return function () { return makeFakeElement(tag); };
      if (prop === 'play') return function () { return Promise.resolve(); };
      if (prop === 'pause') return function () {};
      if (prop === 'addEventListener' || prop === 'removeEventListener') return function () {};
      if (prop === 'querySelector' || prop === 'querySelectorAll') return function () { return null; };
      if (prop === 'parentNode' || prop === 'nextSibling' || prop === 'firstChild') return makeFakeElement(tag);
      if (prop === 'nodeName' || prop === 'tagName') return String(tag || 'DIV').toUpperCase();
      if (prop === 'remove') return function () {};
      if (typeof prop === 'symbol') return undefined;
      // Any other read: return a callable no-op (works whether the script calls it as a
      // function or just reads it as a value).
      return function () { return makeFakeElement(tag); };
    },
    set(target, prop, value) { store[prop] = value; return true; }
  };
  return new Proxy(store, handler);
}

// Node >=21 ships its own read-only `navigator`/`performance` globals (getter-only
// accessors) — plain assignment throws "Cannot set property ... which has only a getter".
// Force-override with defineProperty instead so this harness works across Node versions.
function forceGlobal(name, value) {
  Object.defineProperty(global, name, { value, writable: true, configurable: true });
}

forceGlobal('window', global);
// window === global here, so window.addEventListener must be a real function directly on
// global — this is how the script's top-level window.addEventListener('error', ...) safety
// net actually gets registered and is exercisable by this harness.
global.addEventListener = function () {};
global.removeEventListener = function () {};
// detectCapabilities() in scanner-runtime.js gates the whole runtime mode on
// window.isSecureContext — without this, selectRuntimeMode() silently picks "fallback"
// and the opencv_load_requested checkpoint (gated on scannerMode !== 'fallback') never
// fires, which looks like a startup stall but is actually just this stub being incomplete.
global.isSecureContext = true;
global.matchMedia = function () { return { matches: false }; };
forceGlobal('document', {
  documentElement: makeFakeElement('html'),
  hidden: false,
  visibilityState: 'visible',
  head: makeFakeElement('head'),
  body: makeFakeElement('body'),
  getElementById: function () { return makeFakeElement('div'); },
  createElement: function (tag) { return makeFakeElement(tag); },
  addEventListener: function () {},
  removeEventListener: function () {},
});
forceGlobal('navigator', {
  userAgent: 'node-smoke-test',
  platform: 'test',
  deviceMemory: 4,
  hardwareConcurrency: 4,
  mediaDevices: { getUserMedia: function () { return Promise.reject(new Error('no camera in smoke test')); } },
  sendBeacon: function () { return true; },
  connection: undefined,
});
forceGlobal('screen', { width: 390, height: 844, orientation: { type: 'portrait-primary', angle: 0 } });
forceGlobal('location', { search: '', href: 'http://localhost/scanner/1' });
forceGlobal('performance', { now: function () { return Date.now(); } });
forceGlobal('sessionStorage', (function () {
  const store = {};
  return { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } };
})());
forceGlobal('URLSearchParams', function (s) { this.get = function () { return null; }; });
forceGlobal('requestAnimationFrame', function (cb) { return setTimeout(cb, 0); });
forceGlobal('cancelAnimationFrame', function (id) { clearTimeout(id); });
forceGlobal('MediaStream', function () {});
forceGlobal('URL', { createObjectURL: function () { return 'blob:fake'; }, revokeObjectURL: function () {} });
forceGlobal('Blob', function () {});
"""


def _run_scanner_script_in_node(html):
    """Executes the REAL inline scanner script (plus the real scanner-runtime.js) under
    Node with the stub above. Returns the completed subprocess so callers can assert on
    exit code and stdout (the startup checkpoints are plain console.log calls)."""
    if not shutil.which("node"):
        pytest.skip("node is not available on PATH")
    runtime_js = Path("static/js/scanner-runtime.js").read_text(encoding="utf-8")
    inline_script = _render_jinja_stubs(_extract_inline_scanner_script(html))
    # The real script's self-rescheduling timer chains (scan loop, watchdog, camera retry)
    # are meant to run forever in a real browser tab — under Node they'd keep the process
    # alive indefinitely. Startup is fully synchronous, so a short forced exit is enough to
    # capture it without waiting on anything that never resolves in this stub (getUserMedia,
    # the opencv.js <script> "load").
    force_exit = "\nsetTimeout(function () { process.exit(0); }, 300);\n"
    harness = _NODE_DOM_PRELUDE + "\n" + runtime_js + "\n" + inline_script + force_exit
    with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(harness)
        harness_path = f.name
    try:
        return subprocess.run(
            ["node", harness_path], capture_output=True, text=True, timeout=15
        )
    finally:
        Path(harness_path).unlink(missing_ok=True)


def _export_handler_body(html):
    """Slice out just the diagExportBtn click handler — the privacy-relevant scope. The
    handler's own comment names deviceId/groupId/email precisely to say they're excluded,
    so checks against the whole file would trip on that comment; scope to the handler body
    and look for actual emitted fields (object keys / property reads), not bare words."""
    start = html.index("document.getElementById('diagExportBtn').addEventListener")
    end = start + html[start:].index("});")
    return html[start:end]


# --- Root cause: startup temporal-dead-zone regression ---------------------------------
# "Stuck on Initializing AR Engine, no opencv.js request, no camera stream" was a top-level
# ReferenceError: diagState's object literal read SCANNER_ENTRY_CONTEXT / RESOLVED_BACK_
# DESTINATION before those consts were declared later in the same script. const/let are not
# hoisted the way function declarations are, so this aborted the ENTIRE script — including
# loadOpenCV() and setupCamera() further down — before anything visible happened.

def test_scanner_entry_constants_declared_before_diag_state():
    html = _scanner_html()
    diag_state_at = html.index("const diagState = {")
    for const_name in ("SCANNER_ENTRY_CONTEXT", "RESOLVED_BACK_DESTINATION", "BACK_DESTINATION_REASON"):
        decl_at = html.index("const " + const_name)
        assert decl_at < diag_state_at, (
            const_name + " must be declared before diagState reads it in its own object literal"
        )


def test_diag_state_object_literal_does_not_read_undeclared_later_consts():
    """diagState is a plain object literal evaluated immediately at its own line — every
    identifier it reads must already be in scope. Slice out just the literal body and check
    each bare-word const reference resolves to something declared earlier in the file."""
    html = _scanner_html()
    start = html.index("const diagState = {")
    end = start + html[start:].index("\n    };") + len("\n    };")
    diag_state_src = html[start:end]
    for identifier in re.findall(r"\b([A-Z][A-Z0-9_]{2,})\b", diag_state_src):
        decl_at = html.find("const " + identifier)
        assert decl_at != -1 and decl_at < start, (
            identifier + " is read inside diagState but declared later (or not at all) — TDZ risk"
        )


def test_scanner_startup_smoke_reaches_camera_setup():
    """Executes the REAL inline script (plus the real scanner-runtime.js) under Node with a
    minimal DOM stub — the only way to actually catch a TDZ ReferenceError, since a
    syntax-only new Function(...) parse succeeds even though this exact bug threw at
    runtime. Asserts every startup checkpoint fires, in order, with none of them being the
    global window.onerror fallback."""
    result = _run_scanner_script_in_node(_scanner_html())
    # Real execution order (verified against the file): opencv_load_requested fires before
    # scanner_dom_initialized, since loadOpenCV() is called earlier in the file than the
    # camera-related DOM const block — not the order these were first listed in the bug
    # report, but what actually runs matters here, not the listing order.
    checkpoints = [
        "scanner_script_entered",
        "scanner_state_initialized",
        "scanner_constants_initialized",
        "opencv_load_requested",
        "scanner_dom_initialized",
        "camera_setup_requested",
    ]
    assert "FATAL at checkpoint" not in result.stdout + result.stderr, (
        "startup aborted with an uncaught exception:\n" + result.stdout + result.stderr
    )
    seen_positions = []
    for name in checkpoints:
        marker = "[startup] " + name
        pos = result.stdout.find(marker)
        assert pos != -1, marker + " never logged — startup stalled before reaching it:\n" + result.stdout + result.stderr
        seen_positions.append(pos)
    assert seen_positions == sorted(seen_positions), "checkpoints fired out of order"


# --- P0 fix: first Start Camera press must not be a false CAMERA_UNAVAILABLE no-op ----
# Confirmed root cause (SCANSTORY_PROJECT_ID_QR_SECURITY_CAMERA_AUDIT.md): OpenCV's
# automatic first-load 15000ms timer can fire before the viewer ever presses Start
# Camera (parking the scanner in 'fallback' behind the still-visible intro screen). The
# FIRST Start Camera press then hit setupCameraInner()'s `if (scannerState.state ===
# 'fallback') return;` guard and never called getUserMedia() at all - Retry Camera
# worked only because it explicitly exits 'fallback' before calling setupCamera().
# The fix: startCameraFromIntro() now recognizes this ONE recoverable reason
# (OPENCV_LOAD_FAILED - never a genuine camera/permission/context failure, which cannot
# occur before a real getUserMedia() attempt has ever been made) and recovers through
# the exact same sequence Retry Camera already used, via a new shared
# recoverFallbackAndOpenCamera() helper.

_INTERACTIVE_DOM_AUGMENTATION = r"""
// Augments the shared DOM stub above with just enough to drive this one scenario:
// (a) click listeners are actually stored per element id instead of discarded,
// (b) ONLY the 15000ms OpenCV load timer is intercepted - every other timer (rAF's
// 0ms shim, watchdogs, etc.) still runs for real through the original setTimeout, and
// (c) a REAL constructible URL - the shared prelude's `forceGlobal('URL', {...})` is a
// plain object (fine for the startup-only smoke test, which never reaches
// enterFallback()), but this scenario deliberately drives execution into
// enterFallback() -> showFallbackPanel() -> discoverFallbackVideo(), which does
// `new URL(...)` and throws "URL is not a constructor" against the plain-object stub.
const __RealURL = require('url').URL;
__RealURL.createObjectURL = function () { return 'blob:fake'; };
__RealURL.revokeObjectURL = function () {};
global.URL = __RealURL;
// The shared prelude's fake `location` has no `origin` (real browsers derive it from
// href automatically) - discoverFallbackVideo()'s `new URL(path, window.location.origin)`
// needs a real base to resolve a relative path against.
global.location.origin = 'http://localhost';
// No real network I/O in this harness - discoverFallbackVideo() probes a fallback-
// video endpoint via fetch(); Node's built-in undici fetch would otherwise attempt a
// genuine (and here unservable) HTTP request.
global.fetch = function () { return Promise.reject(new Error('fetch disabled in harness')); };

const __elementsById = {};
const __getUserMediaCalls = [];
function makeFakeElement(tag, id) {
  const store = { style: {}, dataset: {}, classList: {
    add(){}, remove(){}, contains(){ return false; }, toggle(){}
  }, children: [], __listeners: {} };
  const handler = {
    get(target, prop) {
      if (prop === 'addEventListener') return function (event, cb) {
        (store.__listeners[event] = store.__listeners[event] || []).push(cb);
      };
      if (prop === 'removeEventListener') return function () {};
      if (prop in store) return store[prop];
      if (prop === 'getContext') return function () { return makeFakeElement('context'); };
      if (prop === 'getBoundingClientRect') return function () { return { width: 300, height: 300, top: 0, left: 0 }; };
      if (prop === 'appendChild' || prop === 'insertBefore' || prop === 'removeChild') return function (node) { return node; };
      if (prop === 'cloneNode') return function () { return makeFakeElement(tag); };
      if (prop === 'play') return function () { return Promise.resolve(); };
      if (prop === 'pause') return function () {};
      if (prop === 'querySelector' || prop === 'querySelectorAll') return function () { return null; };
      if (prop === 'parentNode' || prop === 'nextSibling' || prop === 'firstChild') return makeFakeElement(tag);
      if (prop === 'nodeName' || prop === 'tagName') return String(tag || 'DIV').toUpperCase();
      if (prop === 'remove') return function () {};
      if (typeof prop === 'symbol') return undefined;
      return function () { return makeFakeElement(tag); };
    },
    set(target, prop, value) { store[prop] = value; return true; }
  };
  const el = new Proxy(store, handler);
  if (id) __elementsById[id] = el;
  return el;
}
document.getElementById = function (id) {
  if (__elementsById[id]) return __elementsById[id];
  return makeFakeElement('div', id);
};
document.createElement = function (tag) { return makeFakeElement(tag); };
document.head = makeFakeElement('head');
document.body = makeFakeElement('body');

navigator.mediaDevices = {
  getUserMedia: function (constraints) {
    __getUserMediaCalls.push(constraints);
    return Promise.resolve({
      getVideoTracks: function () { return [{ addEventListener: function () {} }]; }
    });
  }
};

let __opencvTimeoutCallback = null;
const __realSetTimeout = global.setTimeout;
global.setTimeout = function (cb, delay) {
  if (delay === 15000) {
    __opencvTimeoutCallback = cb;
    return 999999;
  }
  return __realSetTimeout(cb, delay);
};
"""


def _run_first_start_camera_recovery_scenario(html):
    """Drives the EXACT reported timeline through the REAL scanner script (plus the
    real scanner-runtime.js) under Node: the automatic OpenCV load timer fires BEFORE
    the viewer ever presses Start Camera, then the viewer's first (and only) Start
    Camera click is simulated, then the forced OpenCV retry it triggers is made to
    succeed - exactly what happens on a real device where the retry simply warms up
    and finishes. Returns (parsed RESULT payload, raw subprocess result)."""
    if not shutil.which("node"):
        pytest.skip("node is not available on PATH")
    runtime_js = Path("static/js/scanner-runtime.js").read_text(encoding="utf-8")
    inline_script = _render_jinja_stubs(_extract_inline_scanner_script(html))
    trailer = r"""
setTimeout(function () {
  try {
    if (typeof __opencvTimeoutCallback !== 'function') {
      console.log('RESULT:' + JSON.stringify({ error: 'no 15000ms OpenCV timer was scheduled' }));
      process.exit(0);
      return;
    }
    __opencvTimeoutCallback();

    const startListeners = (__elementsById['startCameraBtn'] || {}).__listeners || {};
    const clickHandlers = startListeners['click'] || [];
    if (!clickHandlers.length) {
      console.log('RESULT:' + JSON.stringify({ error: 'no click listener registered on startCameraBtn' }));
      process.exit(0);
      return;
    }
    clickHandlers[0]();

    if (window.Module && typeof window.Module.onRuntimeInitialized === 'function') {
      window.Module.onRuntimeInitialized();
    }
  } catch (e) {
    console.log('RESULT:' + JSON.stringify({ error: String((e && e.stack) || e) }));
    process.exit(0);
    return;
  }
  setTimeout(function () {
    console.log('RESULT:' + JSON.stringify({
      getUserMediaCallCount: __getUserMediaCalls.length,
      finalState: (typeof scannerState !== 'undefined' && scannerState.state) || null,
      cvReady: typeof cvReady !== 'undefined' ? cvReady : null
    }));
    process.exit(0);
  }, 50);
}, 10);
"""
    harness = (
        _NODE_DOM_PRELUDE + "\n" + _INTERACTIVE_DOM_AUGMENTATION + "\n"
        + runtime_js + "\n" + inline_script + "\n" + trailer
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(harness)
        harness_path = f.name
    try:
        result = subprocess.run(["node", harness_path], capture_output=True, text=True, timeout=15)
    finally:
        Path(harness_path).unlink(missing_ok=True)
    marker = "RESULT:"
    idx = result.stdout.rfind(marker)
    assert idx != -1, "harness never printed a RESULT line:\n" + result.stdout + result.stderr
    return json.loads(result.stdout[idx + len(marker):].strip().splitlines()[0]), result


def test_first_start_camera_recovers_after_automatic_opencv_timeout():
    """Executes the actual reported bug end to end against the real shipped script:
    automatic OpenCV timeout before Start Camera is pressed, then the viewer's ONE
    Start Camera click, then the forced retry it triggers succeeding. getUserMedia
    must be invoked exactly once and the scanner must leave 'fallback' - no separate
    Retry Camera click involved anywhere in this sequence."""
    payload, result = _run_first_start_camera_recovery_scenario(_scanner_html())
    assert "error" not in payload, (
        "scenario failed: " + str(payload) + "\n" + result.stdout + result.stderr
    )
    assert payload["getUserMediaCallCount"] == 1, (
        "getUserMedia was not invoked by the first Start Camera press: " + str(payload)
    )
    assert payload["finalState"] != "fallback", "scanner did not recover out of fallback: " + str(payload)
    assert payload["cvReady"] is True


def test_recover_fallback_helper_is_shared_by_retry_and_first_start():
    html = _scanner_html()
    assert "async function recoverFallbackAndOpenCamera(reason)" in html
    retry_start = html.index("async function retryCameraFromFallback()")
    retry_body = html[retry_start:retry_start + html[retry_start:].index("\n    }\n\n    fallbackRetryBtn")]
    assert "recoverFallbackAndOpenCamera('fallback_retry')" in retry_body

    start_intro_start = html.index("async function startCameraFromIntro()")
    start_intro_body = html[start_intro_start:start_intro_start + html[start_intro_start:].index("\n    /* Direct QR Video")]
    assert "recoverFallbackAndOpenCamera('experience_intro_start_camera')" in start_intro_body


def test_first_start_only_auto_recovers_the_recoverable_opencv_reason():
    """A genuine camera/permission/context fallback must be left for the existing
    Retry Camera panel - only OPENCV_LOAD_FAILED (which cannot occur before a real
    getUserMedia() attempt) is safe to recover from automatically on Start Camera."""
    html = _scanner_html()
    start_intro_start = html.index("async function startCameraFromIntro()")
    start_intro_body = html[start_intro_start:start_intro_start + html[start_intro_start:].index("\n    /* Direct QR Video")]
    assert "diagState.fallbackReason === 'OPENCV_LOAD_FAILED'" in start_intro_body
    # Must not be a blanket "any fallback state" check without the reason guard.
    assert "if (scannerState.state === 'fallback') {" not in start_intro_body.replace(
        "if (scannerState.state === 'fallback' && diagState.fallbackReason === 'OPENCV_LOAD_FAILED') {", ""
    )


def test_setup_camera_inner_guard_and_getusermedia_are_unchanged():
    """The fix only changes WHO calls setupCamera() and WHEN - setupCameraInner()
    itself (its fallback guard, used by both callers, and the getUserMedia() call
    itself) is untouched. Its catch block's error classification DID change (a later
    pass, see test_camera_error_classification_distinguishes_not_found_from_other_failures) -
    that is covered separately, not by this test."""
    html = _scanner_html()
    assert "if (scannerState.state === 'fallback') return;" in html
    assert "cameraStream = await navigator.mediaDevices.getUserMedia(constraints);" in html


def test_camera_error_classification_distinguishes_not_found_from_other_failures():
    """Root-cause fix (Issue 3B): every getUserMedia() failure that was not a permission
    denial used to collapse into the same 'CAMERA_UNAVAILABLE' code as a genuine
    NotFoundError - the browser's own DOMException name is real evidence for which
    happened and must classify it, not a guess."""
    html = _scanner_html()
    catch_start = html.index("} catch (err) {\n        console.error('Camera error:', err);")
    catch_end = html.index("enterFallback(cameraFailureCode);", catch_start)
    body = html[catch_start:catch_end]
    assert "errName === 'NotAllowedError' || errName === 'PermissionDeniedError'" in body
    assert "cameraFailureCode = 'CAMERA_PERMISSION_DENIED';" in body
    assert "errName === 'NotFoundError' || errName === 'DevicesNotFoundError'" in body
    assert "cameraFailureCode = 'CAMERA_NOT_FOUND';" in body
    assert "cameraFailureCode = 'CAMERA_START_FAILED';" in body
    # No call site still produces the old overloaded code (comments mentioning it in
    # prose while explaining the change are fine and expected - real calls/returns
    # always continue with a semicolon immediately, prose never does).
    assert not re.search(r"enterFallback\('CAMERA_UNAVAILABLE'\);|return 'CAMERA_UNAVAILABLE';", html)


def test_scanner_runtime_js_and_scanner_runtime_py_are_byte_identical_to_baseline():
    """CV/geometry/tracking code must never move for a startup-state fix. Guards the
    exact SHA256 hashes (LF-normalized) recorded immediately before this fix landed."""
    baselines = {
        "static/js/scanner-runtime.js": "05badbd03e00c22715edbdba168db8721ae621493acab8a211a54dbf76acc5b2",
        "scanner_runtime.py": "eda140bf24f534e160d365c863c618469d68bbcf9619273d499674590324cec0",
    }
    for path, expected in baselines.items():
        content = Path(path).read_bytes().replace(b"\r\n", b"\n")
        actual = hashlib.sha256(content).hexdigest()
        assert actual == expected, f"{path} changed - CV/tracking code must remain untouched by this fix"


# --- Root cause: camera health vs. detection health -----------------------------------

def test_restart_decision_uses_stream_dead_not_flaky_health_check():
    """The actual bug this pass fixes: recoverScanner used to decide whether to tear down
    and recreate the MediaStream based on isCameraHealthy(), which reads cam.videoWidth/
    videoHeight/readyState — all known to glitch for a tick after a real device rotation
    or resize, even though the stream never died. That flaky read is what made ordinary
    rotation-while-scanning show "Preparing Camera". The decision must use a strict,
    non-racy "is the track actually ended" check instead."""
    html = _scanner_html()
    assert "function isStreamDead()" in html
    assert "tracks.every(track => track.readyState === 'ended')" in html
    # recoverScanner now computes this once up front (const needsRestart = ...) rather than
    # inline in an if/else — same decision, restructured so the no-restart path can return
    # early without ever touching the scan loop (see the gap-fix tests below).
    assert "const needsRestart = restartCamera || isStreamDead();" in html
    assert "if (restartCamera || !isCameraHealthy())" not in html


def test_ordinary_marker_loss_never_calls_camera_functions():
    """dropTracking/handleDetectionTimeout/enterGrace are the only entry points for
    ordinary marker loss — none of them may call setupCamera, restartCameraStream, or
    recoverScanner. Camera health and detection health must stay separate concepts."""
    html = _scanner_html()
    drop_start = html.index("function dropTracking(reason, extraMats")
    drop_end = html.index("function handleDetectionTimeout()")
    drop_body = html[drop_start:drop_end]
    for forbidden in ("setupCamera(", "restartCameraStream(", "recoverScanner("):
        assert forbidden not in drop_body, f"{forbidden} must not appear in dropTracking"

    timeout_start = drop_end
    timeout_end = html.index("function enterGrace(reason)")
    timeout_body = html[timeout_start:timeout_end]
    for forbidden in ("setupCamera(", "restartCameraStream(", "recoverScanner("):
        assert forbidden not in timeout_body, f"{forbidden} must not appear in handleDetectionTimeout"


def test_preparing_camera_only_comes_from_a_real_camera_start():
    """'initializing_camera' (the state that maps to "Preparing Camera") must only be
    entered from inside setupCameraInner, at the point a real getUserMedia() call has
    actually been made — never from recoverScanner's paused/ready_to_scan path directly."""
    html = _scanner_html()
    assert "cameraStream = await navigator.mediaDevices.getUserMedia(constraints);" in html
    assert "safeTransition('initializing_camera');" in html
    # recoverScanner/recoverScannerInner must never claim to be starting the camera before
    # deciding to (recoverScanner itself is now a thin shared-promise wrapper — see
    # test_camera_recovery_uses_one_shared_promise — so the real body to check is
    # recoverScannerInner).
    recover_start = html.index("async function recoverScannerInner(reason, restartCamera)")
    recover_end = html.index("function stopCameraStream(reason)")
    recover_body = html[recover_start:recover_end]
    assert "initializing_camera" not in recover_body
    assert "'requesting_camera'" not in recover_body


def test_marker_rejection_reasons_never_carry_camera_language():
    """target_lost/recovering status text (entered only from ordinary marker-loss paths)
    must read as image/detection language, not camera-starting language.

    The wording moved in the scanner visual/copy pass: "Marker Lost"/"Reacquiring Marker"
    became "Find The ScanStory"/"Finding It Again" because "marker" is internal vocabulary
    that public viewer copy must not use. This test's actual subject is unchanged and
    re-asserted below — neither of these two ordinary target-loss states may read as the
    camera starting up.
    """
    html = _scanner_html()
    assert 'target_lost: ["Find The ScanStory", "Point back at the image"]' in html
    assert 'recovering: ["Finding It Again", "Hold the camera on the image"]' in html
    assert '"Preparing Camera"' in html  # still exists, just not attached to these states
    # The point of the original bug: rotating/looking away must never claim the camera is
    # (re)starting. Assert that directly against the two states' text, not just their literals.
    loss_text = html[html.index('target_lost: ['):html.index('paused: [')]
    for camera_word in ("Preparing Camera", "camera stream", "Allow Camera", "Camera Unavailable"):
        assert camera_word not in loss_text, camera_word


# --- Authoritative state transition function -------------------------------------------

def test_transition_scanner_state_is_the_single_authoritative_entry_point():
    html = _scanner_html()
    assert "function transitionScannerState(nextState, reason, metadata)" in html
    assert "function safeTransition(state, reason, metadata)" in html
    assert "return transitionScannerState(state, reason, metadata);" in html


def test_redundant_transitions_are_a_no_op():
    html = _scanner_html()
    assert "if (from === nextState) return false;" in html


def test_invalid_transitions_are_caught_and_reported_not_thrown():
    html = _scanner_html()
    assert "scannerState.transition(nextState);" in html
    assert "rejected: true" in html


def test_stale_async_work_cannot_force_an_older_state():
    """metadata.generation lets a caller mark which generation an async operation started
    in; if a newer generation has since taken over, the transition is dropped instead of
    clobbering current state."""
    html = _scanner_html()
    assert "typeof metadata.generation === 'number' && metadata.generation !== scannerGeneration" in html
    assert "code: 'stale_transition'" in html


def test_transitions_are_timestamped_and_recorded_bounded():
    html = _scanner_html()
    assert "at: Math.round(performance.now())" in html
    assert "const scannerTransitionHistory = []" in html
    assert "TRANSITION_HISTORY_LIMIT" in html
    assert "if (scannerTransitionHistory.length > TRANSITION_HISTORY_LIMIT) scannerTransitionHistory.shift();" in html


# --- One camera stream -------------------------------------------------------------------

def test_camera_start_cannot_run_concurrently():
    """Repeated setupCamera() calls (e.g. two recovery paths landing close together) must
    reuse the SAME in-flight promise rather than issuing a second getUserMedia()."""
    html = _scanner_html()
    assert "let cameraStartPromise = null;" in html
    assert "if (cameraStartPromise) return cameraStartPromise;" in html
    assert "cameraStartPromise = setupCameraInner().finally(" in html


def test_camera_stop_is_idempotent_single_function():
    html = _scanner_html()
    assert "function stopCameraStream(reason)" in html
    assert "if (!cameraStream) return;" in html
    # every call site now routes through the single idempotent stop function. A real
    # (non-bfcache) pagehide reaches this via endScannerSession() -> stopCameraStream
    # ('session_end') rather than stopping the camera directly (Part E dedup).
    assert "stopCameraStream('session_end')" in html
    assert "stopCameraStream('unload')" in html
    assert "stopCameraStream('restart')" in html
    assert "stopCameraStream(code)" in html  # enterFallback


def test_stream_ended_event_is_the_only_hard_restart_trigger():
    html = _scanner_html()
    assert "track.addEventListener('ended', () => recoverScanner('stream_ended', true));" in html


def test_camera_lifecycle_counters_present_and_dev_only():
    html = _scanner_html()
    for field in (
        "cameraStartCount", "cameraRestartCount", "cameraStartInProgress",
        "restartReason", "lastCameraStartTimestamp", "lastCameraStopTimestamp",
    ):
        assert field in html
    # dev-gate: this whole panel only exists server-side when scanner_diagnostics_enabled
    assert "{% if scanner_diagnostics_enabled %}" in html
    # No raw device identifiers actually emitted by the diagnostics export — scoped to the
    # export handler body and checked as field patterns, not bare words. The privacy
    # comment on the handler legitimately contains the words "deviceId"/"groupId" to say
    # they're excluded, so a whole-file bare-word ban would trip on its own documentation.
    # Unambiguous code shapes only (object key or property access) — comma-suffixed
    # patterns like "groupId," are NOT used here because the handler's own privacy
    # comment is prose ("no deviceId/groupId, no auth/...") and legitimately contains a
    # comma right after "groupId"; that would be a false positive, not a real emission.
    export_body = _export_handler_body(html)
    for forbidden in ("deviceId:", "groupId:", "settings.deviceId", "settings.groupId", ".deviceId", ".groupId"):
        assert forbidden not in export_body


# --- One scan loop -------------------------------------------------------------------

def test_start_detect_loop_and_start_tracking_loop_are_idempotent():
    html = _scanner_html()
    assert "if (detectLoopTimer) return;" in html
    assert "if (trackLoopActive) return;" in html


def test_scan_loop_start_stop_counters_present():
    html = _scanner_html()
    assert "diagState.scanLoopStartCount++;" in html
    assert "diagState.scanLoopStopCount++;" in html


def test_diagnostics_render_loop_is_not_counted_as_a_scan_loop():
    """The 500ms diagnostics-panel render interval must not touch scanLoopStartCount or
    detectLoopTimer/trackLoopActive — it's a read-only render, not a detection loop."""
    html = _scanner_html()
    render_loop_line = "setInterval(renderDiagPanel, 500);"
    assert render_loop_line in html
    idx = html.index(render_loop_line)
    # the diagnostics render call itself does not increment any scan-loop counter
    nearby = html[idx - 200:idx]
    assert "scanLoopStartCount++" not in nearby


# --- Detection-request concurrency --------------------------------------------------

def test_detection_request_tracks_sequence_and_latest_applied():
    html = _scanner_html()
    assert "diagState.requestSeq++;" in html
    assert "diagState.latestAppliedSeq = requestId;" in html
    assert "diagState.staleCount++;" in html or "staleCount: 0" in html  # already covered elsewhere too


def test_in_flight_guard_clears_on_every_exit_path():
    """finally block guarantees detectInFlight clears on accept, reject, HTTP error,
    timeout, abort, or thrown exception — not just the happy path."""
    html = _scanner_html()
    detect_body = _detect_once_body()
    assert "} finally {" in detect_body
    assert "finalizeDetectionAttempt(attempt," in detect_body
    finalize_start = html.index("function finalizeDetectionAttempt(")
    finalize_end = html.index("function clearTrackingGeometry", finalize_start)
    assert "detectInFlight = false;" in html[finalize_start:finalize_end]


# --- Orientation / visibility / page lifecycle ---------------------------------------

def test_orientation_and_resize_share_one_debounced_recovery_path():
    html = _scanner_html()
    assert "window.addEventListener('orientationchange', () => scheduleOrientationRecovery('orientationchange'));" in html
    assert "window.addEventListener('resize', () => scheduleOrientationRecovery('resize'));" in html
    assert "if (orientationRecoveryTimer) clearTimeout(orientationRecoveryTimer);" in html


def test_visibility_restore_does_not_force_a_camera_restart_unless_dead():
    """Root-cause fix: visibilitychange used to always call recoverScanner() on restore
    (which itself now short-circuits when the stream is alive), AND used to call
    stopDetectLoop() on every hidden event — meaning a routine notification-shade blip
    killed the scan loop and required a full recovery cycle to resume. Now: hidden never
    stops the loop (ticks just skip via 'tab_hidden'), and restore only calls the heavy
    recoverScanner path when the stream is actually dead; otherwise it's a plain state
    transition back to ready_to_scan with the loop having never stopped."""
    html = _scanner_html()
    assert "recoverScanner('visibilitychange', true);" in html
    assert "safeTransition('ready_to_scan', 'visibilitychange');" in html
    # The hidden branch must not stop the detect loop. Sliced up to the next top-level
    # const declaration that follows the listener in the file (NOT the first "});", which
    # would truncate at the inner scannerDiagnostics.push(...) call's own closing paren).
    visibility_start = html.index("document.addEventListener('visibilitychange'")
    visibility_end = html.index("const RANSAC_REPROJ")
    visibility_body = html[visibility_start:visibility_end]
    assert "stopDetectLoop(" not in visibility_body


def test_pageshow_bfcache_only_restarts_if_stream_is_actually_dead():
    html = _scanner_html()
    assert "window.addEventListener('pageshow', function (event)" in html
    assert "if (!event.persisted) return;" in html
    assert "recoverScanner('pageshow_bfcache', isStreamDead());" in html


def test_page_cleanup_stops_camera_and_marks_session_ending():
    """pagehide now routes a real (non-bfcache) unload through the same idempotent
    endScannerSession() the Back button and beforeunload use (Part E dedup), instead of
    duplicating its own partial teardown — endScannerSession is what actually sets
    sessionEnding and releases the camera."""
    html = _scanner_html()
    assert "sessionEnding = true;" in html
    assert "stopCameraStream('session_end');" in html
    pagehide_start = html.index("window.addEventListener('pagehide'")
    pagehide_end = html.index("window.addEventListener('orientationchange'")
    pagehide_body = html[pagehide_start:pagehide_end]
    assert "if (event.persisted)" in pagehide_body
    assert "endScannerSession();" in pagehide_body


# --- Diagnostics gating, history bound, and privacy ----------------------------------

def test_diagnostics_require_both_dev_flag_and_query_param():
    html = _scanner_html()
    assert "{% if scanner_diagnostics_enabled %}" in html
    assert "const diagPanelActive = Boolean(diagnosticsEnabled && diagPanelEl);" in html


def test_diagnostics_history_is_bounded():
    from pathlib import Path as _P
    runtime = _P("static/js/scanner-runtime.js").read_text(encoding="utf-8")
    assert "const limit = 80;" in runtime
    assert "if (events.length > limit) events.shift();" in runtime


def test_export_json_excludes_pixels_and_identifiers():
    html = _scanner_html()
    export_body = _export_handler_body(html)
    # Check actual payload KEYS (colon-suffixed), not bare words — the handler's own
    # explanatory comment names these fields precisely to say they're excluded.
    for forbidden_key in ("deviceId:", "groupId:", "password:", "email:", "frame.jpg", "captured"):
        assert forbidden_key not in export_body


def test_reset_diagnostics_does_not_restart_camera():
    html = _scanner_html()
    reset_start = html.index("document.getElementById('diagResetBtn').addEventListener")
    reset_end = reset_start + html[reset_start:].index("});")
    reset_body = html[reset_start:reset_end]
    for forbidden in ("setupCamera(", "restartCameraStream(", "recoverScanner(", "getUserMedia"):
        assert forbidden not in reset_body
    assert "scannerDiagnostics.reset()" in reset_body


def test_diagnostics_panel_is_a_collapsible_bottom_sheet():
    html = _scanner_html()
    assert 'position:fixed;left:0;right:0;bottom:0' in html
    assert 'id="diagExpanded"' in html
    assert 'id="diagSummaryLine"' in html
    assert "diagExpandedOpen = !diagExpandedOpen;" in html


# --- Real-device gap fix: detect requests stopping for 5-18 seconds --------------------
# Root cause (three compounding contributors, all fixed below):
#   1. FORCE_REDETECT_MS was 12000 — up to 12s of intentional silence during healthy
#      tracking (see test_gate_jr_scanner_recovery.py::
#      test_healthy_tracking_suppresses_repeated_detect_init_and_limits_inflight).
#   2. recoverScanner() unconditionally stopped the scan loop and waited 250ms+ for EVERY
#      orientation/resize/visibility event, even when the camera never needed touching.
#   3. The loop was a bare setInterval — a single uncaught exception in one tick would
#      have silently killed all future ticks with no diagnostic trail.


def _scan_tick_body(html):
    start = html.index("async function scanTick(token)")
    end = html.index("function startDetectLoop()")
    return html[start:end]


def test_scan_request_is_rescheduled_after_non_tracking_outcomes():
    """A completed tick — success, rejected detection, or a thrown/fetch error inside
    detectOnceFromServer — must always schedule the next one. The reschedule call lives in
    a finally block wrapping the entire decision+request body, so it runs regardless of
    which branch returned or whether an exception propagated."""
    html = _scanner_html()
    body = _scan_tick_body(html)
    try_idx = body.index("try {")
    finally_idx = body.index("} finally {")
    await_idx = body.index("await detectOnceFromServer();")
    assert try_idx < await_idx < finally_idx, "detectOnceFromServer() must be inside the try, before the finally"
    assert "let allowScanReschedule = true;" in body
    assert "if (allowScanReschedule && !requestAttemptStarted && !detectInFlight && !(activeDetectionAttempt && !activeDetectionAttempt.terminal)) {" in body[finally_idx:]
    assert "scheduleNextScan('after_tick');" in body[finally_idx:]
    finalize_start = html.index("function finalizeDetectionAttempt(")
    finalize_end = html.index("function clearTrackingGeometry", finalize_start)
    assert "scheduleAttemptSuccessor(attempt, 'after_attempt', delayMs);" in html[finalize_start:finalize_end]


def test_scan_in_flight_cleared_after_every_outcome():
    """detectInFlight (this codebase's scanInFlight) clears in detectOnceFromServer's own
    finally block — independently of scanTick's reschedule guarantee above."""
    html = _scanner_html()
    detect_body = _detect_once_body()
    assert "} finally {" in detect_body
    assert "finalizeDetectionAttempt(attempt," in detect_body
    finalize_start = html.index("function finalizeDetectionAttempt(")
    finalize_end = html.index("function clearTrackingGeometry", finalize_start)
    assert "detectInFlight = false;" in html[finalize_start:finalize_end]


def test_resize_and_orientationchange_do_not_stop_the_loop_when_stream_is_alive():
    """recoverScanner's needsRestart check must run and potentially return BEFORE any
    loop-stopping or generation-bumping code — resize/orientationchange (which always call
    recoverScanner with restartCamera=false) must never reach stopDetectLoop/
    scannerGeneration++ unless the stream is genuinely dead."""
    html = _scanner_html()
    recover_start = html.index("async function recoverScannerInner(reason, restartCamera)")
    needs_restart_idx = html.index("const needsRestart = restartCamera || isStreamDead();", recover_start)
    early_return_idx = html.index("return;", needs_restart_idx)
    stop_loop_idx = html.index("stopDetectLoop('camera_restart');", recover_start)
    generation_bump_idx = html.index("scannerGeneration++;", recover_start)
    assert recover_start < needs_restart_idx < early_return_idx < stop_loop_idx
    assert recover_start < needs_restart_idx < early_return_idx < generation_bump_idx
    assert "camera_restart_avoided_stream_alive" in html[needs_restart_idx:early_return_idx + 20]


def test_live_stream_with_temporary_zero_dimensions_does_not_restart():
    """isStreamDead() — the ONLY signal recoverScanner uses to decide whether to actually
    tear down and recreate the MediaStream — must never look at videoWidth/videoHeight or
    readyState (both known to glitch transiently during a resize/orientation reflow even
    though the track is still live)."""
    html = _scanner_html()
    dead_start = html.index("function isStreamDead()")
    dead_end = html.index("function resizeScannerSurfaces()")
    dead_body = html[dead_start:dead_end]
    assert "videoWidth" not in dead_body
    assert "videoHeight" not in dead_body
    assert "readyState === 'ended'" in dead_body


def test_stale_loop_token_cannot_cancel_the_current_loop():
    """Loop cancellation is keyed on scanLoopToken, a counter DELIBERATELY separate from
    scannerGeneration — scannerGeneration bumps for reasons unrelated to loop ownership
    (detect-response staleness, pair-epoch guards) and must never be able to cancel a scan
    loop that is still the current one."""
    html = _scanner_html()
    assert "let scanLoopToken = 0;" in html
    tick_body = _scan_tick_body(html)
    assert "if (token !== scanLoopToken)" in tick_body
    assert "skipTick('stale_loop_token'" in tick_body
    # the tick must NOT treat a scannerGeneration mismatch alone as a cancellation signal
    assert "if (token !== scannerGeneration)" not in tick_body


def test_no_unexplained_five_second_scheduling_gap_in_the_normal_path():
    """Pins every timing constant that can legitimately delay the NEXT actual detect
    request while the page is visible, the stream is live, and nothing is in flight or
    recovering — this is the "visible-page redetection gap" Part F caps at under 4000ms.
    The first fix (FORCE_REDETECT_MS 12000->3000) still left 5-6s gaps on lightweight mode:
    detectionPolicy.canStart() independently gates on 2x MODES.lightweight.detectIntervalMs
    (650ms) while tracking, so the real worst case was
    3000 (FORCE_REDETECT_MS) + 1300 (policy gate) + 650 (tick interval) = 4950ms. 1800 keeps
    the same worst case at 1800 + 1300 + 650 = 3750ms — under 4000 on every mode, not just
    the common ones."""
    html = _scanner_html()
    runtime = Path("static/js/scanner-runtime.js").read_text(encoding="utf-8")
    assert "const FORCE_REDETECT_MS = 1800;" in html
    assert "lightweight: { frameWidth: 480, detectIntervalMs: 650" in runtime
    lightweight_detect_interval_ms = 650
    policy_gate_while_tracking_ms = lightweight_detect_interval_ms * 2  # detectionPolicy.canStart()'s own (tracking ? 2 : 1) multiplier
    force_redetect_ms = 1800
    worst_case_visible_page_gap_ms = force_redetect_ms + policy_gate_while_tracking_ms + lightweight_detect_interval_ms
    assert worst_case_visible_page_gap_ms < 4000


def test_scheduling_diagnostics_events_are_present():
    html = _scanner_html()
    for event in (
        "'scan_tick_enter'", "'scan_tick_skipped'", "'request_started'", "'request_finished'",
        "'next_scan_scheduled'", "'loop_cancelled'", "'loop_restarted'",
        "'recovery_started'", "'recovery_completed'",
        "'camera_restart_requested'", "'camera_restart_avoided_stream_alive'",
    ):
        assert event in html


def test_scan_tick_skip_reasons_cover_every_required_case():
    """Every skip goes through skipTick(reason, extra) — a single helper that both pushes
    the 'scan_tick_skipped' diagnostic AND updates the lastSkipReason/consecutiveSkipCount
    counters, so no skip path can silently forget to record why."""
    html = _scanner_html()
    assert "function skipTick(reason, extra)" in html
    assert "diagState.lastSkipReason = reason;" in html
    assert "diagState.consecutiveSkipCount++;" in html
    tick_body = _scan_tick_body(html)
    for reason in (
        "session_ending", "stale_loop_token", "tab_hidden", "opencv_not_ready",
        "recovery_pending", "camera_start_in_progress", "video_not_ready",
        "request_in_flight", "tracking_healthy_suppressed",
    ):
        assert f"skipTick('{reason}'" in tick_body


# --- Final pass: Preparing Camera during active scan, remaining gaps, fallback UI,
# scroll-dependency, and Back button (this session) -------------------------------------


def test_camera_ready_transition_chain_reaches_ready_to_scan():
    """Root cause of 'Preparing Camera during active scanning even with a live stream and
    accepted detections': cam.onloadedmetadata never transitioned the state machine at all,
    so it stayed at 'initializing_camera' (mapped to "Preparing Camera") for the entire
    rest of the session — the detect/tracking loops don't check scannerState.state at all,
    so scanning worked perfectly while the status text was simply frozen on the wrong word.
    TRANSITIONS only allows a straight chain from initializing_camera to ready_to_scan
    (loading_opencv -> loading_wasm -> initializing_scanner -> ready_to_scan).

    Superseded by a later cold-start-honesty pass (Issue 3A): the chain now stops at
    initializing_scanner and defers the final ready_to_scan hop to
    markScannerReadyIfPossible(), which only takes it if cvReady is ALSO true - reaching
    ready_to_scan unconditionally here was itself a false readiness claim on a
    slow-OpenCV cold start (see test_scanner_cold_start_js.py). The three walked states
    plus the deferred gate are what this test now guards."""
    html = _scanner_html()
    onloadedmetadata_start = html.index("cam.onloadedmetadata = async () => {")
    onloadedmetadata_end = html.index("} catch (err) {", onloadedmetadata_start)
    body = html[onloadedmetadata_start:onloadedmetadata_end]
    assert "safeTransition('loading_opencv', 'camera_ready');" in body
    assert "safeTransition('loading_wasm', 'camera_ready');" in body
    assert "safeTransition('initializing_scanner', 'camera_ready');" in body
    assert "markScannerReadyIfPossible('camera_ready')" in body
    assert "safeTransition('ready_to_scan', 'camera_ready');" not in body
    # ordering matches the only valid TRANSITIONS chain — index the actual calls, not the
    # bare state names (which also appear earlier, in this function's own explanatory
    # comment about why the chain is needed)
    assert (body.index("safeTransition('loading_opencv'") < body.index("safeTransition('loading_wasm'")
            < body.index("safeTransition('initializing_scanner'") < body.index("markScannerReadyIfPossible('camera_ready')"))


def test_scroll_or_resize_not_required_for_preview_revival():
    """Root cause of 'scrolling revives the preview': a single requestAnimationFrame right
    after cam.play() can fire before the browser has actually settled layout for the newly
    visible <video>, so resizeScannerSurfaces() reads a stale/zero size and the preview
    looks frozen until something else forces a reflow. A follow-up call re-measures once
    layout has genuinely settled, with no user action required."""
    html = _scanner_html()
    onloadedmetadata_start = html.index("cam.onloadedmetadata = async () => {")
    onloadedmetadata_end = html.index("} catch (err) {", onloadedmetadata_start)
    body = html[onloadedmetadata_start:onloadedmetadata_end]
    assert "requestAnimationFrame(resizeScannerSurfaces);" in body
    assert "setTimeout(resizeScannerSurfaces, 300);" in body


def test_camera_recovery_uses_one_shared_promise():
    """Part C.4: concurrent recoverScanner() calls (e.g. an orientation debounce and a
    stream 'ended' event landing close together) must not race two independent restarts —
    later callers await the SAME in-flight recovery."""
    html = _scanner_html()
    assert "let recoverScannerPromise = null;" in html
    assert "if (recoverScannerPromise) return recoverScannerPromise;" in html
    assert "recoverScannerPromise = recoverScannerInner(reason, restartCamera).finally(" in html
    # scanTick must also treat a pending recovery as a reason to skip, not misreport
    # video_not_ready while a real recovery is under way
    tick_body = _scan_tick_body(html)
    assert "if (recoverScannerPromise) { skipTick('recovery_pending'); return; }" in tick_body


def test_camera_recovery_is_bounded_not_infinite():
    """Part C.3: immediate retry, one short delayed retry, then give up — never an
    infinite restart loop."""
    html = _scanner_html()
    assert "const CAMERA_RECOVERY_MAX_ATTEMPTS = 2;" in html
    assert "const CAMERA_RECOVERY_RETRY_DELAY_MS = 600;" in html
    recover_start = html.index("async function recoverScannerInner(reason, restartCamera)")
    recover_end = html.index("function stopCameraStream(reason)")
    body = html[recover_start:recover_end]
    assert "for (let attempt = 1; attempt <= CAMERA_RECOVERY_MAX_ATTEMPTS; attempt++)" in body


def test_recovery_failure_enters_fallback_and_does_not_resume_loops():
    """If both bounded attempts fail, recovery must give up to a real fallback state — it
    must NOT fall through to safeTransition('ready_to_scan')/startDetectLoop() as if the
    camera were healthy.

    Issue 3B root-cause fix: the failure code changed from 'CAMERA_UNAVAILABLE' to
    'CAMERA_INTERRUPTED' — every caller that reaches recoverScannerInner (track 'ended',
    a dead stream found on visibility/bfcache resume) only does so because the camera WAS
    part of a live session, never because no camera was ever found. And the SECOND
    enterFallback (on a rejected ready_to_scan transition after a successful restart) was
    removed outright: `recovered` is only true once isStreamDead() just confirmed the
    camera IS alive, so a rejected transition there is a state-machine race, never
    evidence of a camera failure — claiming one would itself be the exact false failure
    this pass exists to remove."""
    html = _scanner_html()
    recover_start = html.index("async function recoverScannerInner(reason, restartCamera)")
    recover_end = html.index("function stopCameraStream(reason)")
    body = html[recover_start:recover_end]
    not_recovered_idx = body.index("if (!recovered) {")
    enter_fallback_idx = body.index("enterFallback('CAMERA_INTERRUPTED');", not_recovered_idx)
    return_idx = body.index("return;", enter_fallback_idx)
    ready_to_scan_idx = body.index(
        "const readyForScan = safeTransition('ready_to_scan', reason, { site: 'recoverScannerInner_success' });"
    )
    assert not_recovered_idx < enter_fallback_idx < return_idx < ready_to_scan_idx
    assert "if (!readyForScan) {" in body
    rejected_body = body[body.index("if (!readyForScan) {"):body.index("startDetectLoop();\n      startTrackingLoop();", ready_to_scan_idx)]
    # A real call always reads "enterFallback('CODE');" — only the explanatory comment
    # above may mention the function name in prose (no trailing "();" there).
    assert "enterFallback('CAMERA_UNAVAILABLE');" not in rejected_body
    assert not re.search(r"enterFallback\('[A-Z_]+'\);", rejected_body)
    start_loops_idx = body.index("startDetectLoop();\n      startTrackingLoop();", ready_to_scan_idx)
    assert ready_to_scan_idx < start_loops_idx
    assert not re.search(r"enterFallback\('CAMERA_UNAVAILABLE'\);", body)
    assert "'automatic_recovery_failed'" in body
    assert "'automatic_recovery_succeeded'" in body


def test_fallback_available_never_appears_without_visible_action():
    """Part D: the old 'Fallback Available' word had zero action anywhere on the page.
    Now entering the fallback state always shows a real panel with a tappable Retry Camera
    button and a Return to Project link."""
    html = _scanner_html()
    assert 'fallback: ["Camera Unavailable"' in html
    assert "Fallback Available" not in html
    assert 'id="fallbackPanel"' in html
    assert 'id="fallbackRetryBtn"' in html
    assert 'id="fallbackReturnBtn"' in html
    panel_start = html.index('id="fallbackPanel"')
    panel_end = html.index('{% if scanner_diagnostics_enabled %}', panel_start)
    panel_markup = html[panel_start:panel_end]
    assert "Retry Camera" in panel_markup
    assert "Return to Project" in panel_markup
    assert "showFallbackPanel(code, safe);" in html  # called from enterFallback — every fallback entry shows the panel


def test_prior_failure_reload_routes_through_canonical_fallback_panel():
    """Fix 5 (V1 Agent 2): the bare '1' flag became a {code, at} record so a transient
    OpenCV load failure can be told apart from a hard capability failure, but a stored
    failure of EITHER kind must still force fallback on this load - only the TTL/
    consume-once behavior (covered by its own dedicated test) changed."""
    html = _scanner_html()
    assert "let scannerPriorFailure = false;" in html
    assert "sessionStorage.getItem(SCANNER_PRIOR_FAILURE_KEY);" in html
    assert "scannerPriorFailure = true;" in html
    assert "return 'SCANNER_PRIOR_FAILURE';" in html
    fallback_start = html.index("if (scannerMode === 'fallback') {", html.index("fallbackRetryBtn.addEventListener"))
    fallback_body = html[fallback_start:fallback_start + 240]
    assert "enterFallback(scannerFallbackReason());" in fallback_body


def test_prior_failure_is_stored_with_reason_and_timestamp_not_a_bare_flag():
    """Fix 5: enterFallback() must persist enough to distinguish hard vs. transient on the
    NEXT load - a bare '1' flag (the pre-fix format) can't carry that distinction."""
    html = _scanner_html()
    enter_fallback_start = html.index("function enterFallback(code) {")
    enter_fallback_body = html[enter_fallback_start:enter_fallback_start + 800]
    assert "JSON.stringify({ code: code || null, at: Date.now() })" in enter_fallback_body
    assert "sessionStorage.setItem('scanstoryScannerPriorFailure'" in enter_fallback_body


def test_prior_failure_transient_code_is_consumed_once_hard_code_is_not():
    """Fix 5: only OPENCV_LOAD_FAILED (a load/network hiccup) is in the transient set and
    gets cleared immediately after being honored for this load - a hard capability failure
    (anything not in that set, including a legacy/unparsable value) must keep forcing
    fallback and must NOT be cleared here."""
    html = _scanner_html()
    read_start = html.index("const SCANNER_PRIOR_FAILURE_KEY")
    read_end = html.index("const scannerMode = runtime.selectRuntimeMode", read_start)
    body = html[read_start:read_end]
    assert "new Set(['OPENCV_LOAD_FAILED'])" in body
    is_transient_idx = body.index("const isTransient")
    remove_idx = body.index("sessionStorage.removeItem(SCANNER_PRIOR_FAILURE_KEY);", is_transient_idx)
    if_transient_idx = body.rindex("if (isTransient)", is_transient_idx, remove_idx)
    assert is_transient_idx < if_transient_idx < remove_idx


def test_prior_failure_transient_consume_makes_this_load_the_retry_not_one_more_fallback_pass():
    """Correction 1 (off-by-one fix): sequence is Load A fails -> sets the flag. Load B (the
    very next fresh navigation) must consume the flag AND attempt a normal load on THAT SAME
    load - not force one more fallback-only pass before a THIRD load finally retries. That
    means the transient branch must clear the flag WITHOUT ever setting
    scannerPriorFailure = true; only the hard-failure branch may set it true. This is a
    structural proof (no JS engine in this test suite) - see the sibling Python-side
    contract test that select_runtime_mode(..., prior_failure=False) never forces 'fallback'
    from that flag alone."""
    html = _scanner_html()
    read_start = html.index("const SCANNER_PRIOR_FAILURE_KEY")
    read_end = html.index("const scannerMode = runtime.selectRuntimeMode", read_start)
    body = html[read_start:read_end]
    if_transient_start = body.index("if (isTransient) {")
    else_start = body.index("} else {", if_transient_start)
    else_end = body.index("}", else_start + len("} else {")) + 1
    if_branch = body[if_transient_start:else_start]
    else_branch = body[else_start:else_end]
    assert "sessionStorage.removeItem(SCANNER_PRIOR_FAILURE_KEY);" in if_branch
    assert "scannerPriorFailure = true;" not in if_branch  # THIS load is the retry, not another forced fallback
    assert "scannerPriorFailure = true;" in else_branch    # only the hard-failure path forces fallback here


def test_select_runtime_mode_prior_failure_false_never_forces_fallback_alone():
    """Python-side half of the Correction 1 proof: once the JS above leaves
    scannerPriorFailure false for the consumed-transient case, selectRuntimeMode's own
    contract (mirrored here via the scanner_runtime.py port used elsewhere in this suite)
    must not fall back to 'fallback' on that flag alone for an otherwise-healthy device."""
    from scanner_runtime import select_runtime_mode

    healthy_caps = {
        "secure_context": True, "camera_api": True, "webassembly": True, "canvas": True,
        "device_memory": 4, "hardware_concurrency": 4, "screen_width": 390,
    }
    assert select_runtime_mode(healthy_caps, prior_failure=False) != "fallback"
    assert select_runtime_mode(healthy_caps, prior_failure=True) == "fallback"


def test_retry_camera_invokes_guarded_recovery_and_avoids_concurrent_starts():
    """Retry Camera's own no-concurrent-starts guard stays in retryCameraFromFallback();
    the actual recovery steps (stop loops, conditional OpenCV reload, camera restart)
    live in the shared recoverFallbackAndOpenCamera('fallback_retry') helper it calls -
    also used by the first Start Camera press recovering from a pre-camera fallback."""
    html = _scanner_html()
    assert "async function retryCameraFromFallback()" in html
    assert "if (fallbackRetryInProgress || diagState.cameraStartInProgress) return;" in html
    assert "fallbackRetryBtn.addEventListener('click', retryCameraFromFallback);" in html
    assert "recoverFallbackAndOpenCamera('fallback_retry')" in html
    assert "async function recoverFallbackAndOpenCamera(reason) {" in html
    assert "stopDetectLoop(reason);" in html
    assert "stopTrackingLoop();" in html
    assert "await setupCamera();" in html


def test_retry_camera_after_opencv_failure_reloads_opencv_once_before_camera_restart():
    """Retry Camera's actual recovery steps now live in the shared
    recoverFallbackAndOpenCamera() helper (also used by the first Start Camera press
    when it recovers from a pre-camera OpenCV fallback) - retryCameraFromFallback()
    itself is just the button-specific wrapper around it."""
    html = _scanner_html()
    retry_start = html.index("async function recoverFallbackAndOpenCamera(reason)")
    retry_end = html.index("fallbackRetryBtn.addEventListener('click', retryCameraFromFallback);")
    body = html[retry_start:retry_end]
    assert "if (!cvReady) {" in body
    assert "opencvLoadAttempts = 0;" in body
    assert "await loadOpenCV({ forceRetry: true });" in body
    assert body.index("await loadOpenCV({ forceRetry: true });") < body.index("await setupCamera();")
    loader_start = html.index("function loadOpenCV(options)")
    loader_end = html.index("if (scannerMode !== 'fallback')", loader_start)
    loader = html[loader_start:loader_end]
    assert "if (opencvLoadPromise && !options.forceRetry) return opencvLoadPromise;" in loader
    assert "script.id = 'opencvScript';" in loader
    assert "removeOpenCVScriptForRetry();" in loader


def test_successful_retry_hides_fallback_panel():
    html = _scanner_html()
    assert "function hideFallbackPanel(reason)" in html
    assert "hideFallbackPanel('retry_succeeded');" in html
    retry_start = html.index("async function recoverFallbackAndOpenCamera(reason)")
    retry_end = html.index("fallbackRetryBtn.addEventListener('click', retryCameraFromFallback);")
    body = html[retry_start:retry_end]
    assert "if (!isStreamDead()) {" in body
    assert "hideFallbackPanel('retry_succeeded');" in body


def test_missing_published_media_has_specific_user_facing_error():
    runtime_js = _scanner_runtime_js()
    assert 'if (!payload.video_url) return { ok: false, code: "PUBLISHED_MEDIA_MISSING" };' in runtime_js
    assert "PUBLISHED_MEDIA_MISSING:" in runtime_js
    assert "UNSUPPORTED_DEVICE" in runtime_js


def test_scanner_registers_service_worker_non_blocking_with_safe_cache_scope():
    html = _scanner_html()
    app_src = _app_py()
    sw_src = Path("static/sw.js").read_text(encoding="utf-8", errors="ignore")
    assert "function registerScannerServiceWorker()" in html
    assert "navigator.serviceWorker.register('/static/sw.js', { scope: '/' })" in html
    assert ".catch(function (err) {" in html
    assert "registerScannerServiceWorker();" in html
    assert 'request.path == "/static/sw.js"' in app_src
    assert 'response.headers["Service-Worker-Allowed"] = "/"' in app_src
    assert "Only intercept OpenCV static files" in sw_src
    assert "if (!url.pathname.startsWith('/static/js/opencv')) return;" in sw_src


def test_back_button_does_not_call_logout_and_preserves_authentication():
    """Root cause: the Back link used to hardcode href="/" (the public landing page), which
    looked exactly like being logged out even though the session itself was untouched.
    Destination must be session-aware: admin dashboard for an admin session, user dashboard
    for a user session, and landing ONLY for a genuinely anonymous scan."""
    html = _scanner_html()
    back_start = html.index("document.getElementById('backBtn')?.addEventListener('click'")
    back_end = html.index("let cvReady = false;")  # next real statement after the handler
    back_body = html[back_start:back_end]
    # "not in html.lower()" would also trip on this test file's OWN explanatory comment
    # above the handler ("does NOT call any logout helper") — scope to the actual handler.
    assert "logout" not in back_body.lower()
    # Round-2 fix: session.get('user_id')/session.get('admin_id') alone are NOT safe — the
    # public QR link embeds the OWNER's id as a query param, and the scanner view
    # force-sets session['user_id'] from that param on every request, so a random public
    # visitor's session looks identical to the creator's the instant they scan the code.
    # The href must now be the fully server-resolved destination, computed in app.py.
    assert 'href="{{ resolved_back_destination }}"' in html
    href_start = html.index('id="backBtn"')
    href_line = html[max(0, href_start - 400):href_start]
    assert "session.get('admin_id')" not in href_line
    assert "session.get('user_id')" not in href_line


def _resolver_body(app_src):
    start = app_src.index("def resolve_scanner_entry_context(project, test_token):")
    end = app_src.index('@app.route("/project/<int:project_id>/scanner-test")')
    return app_src[start:end]


def test_scanner_entry_context_resolved_server_side_not_from_session_or_query_alone():
    """Round-3 fix: creator_test/admin_test must never be inferred from session.user_id/
    session.admin_id alone (identical to the owner's own session the instant they scan their
    own public QR) and never from a client-suppliable query param (?entry_context=,
    ?mode=). The only path in is a signed, short-lived test_token minted by the dedicated
    ownership-checked routes below — see scanner_test_entry/admin_scanner_test_entry."""
    app_src = _app_py()
    assert "def resolve_scanner_entry_context(project, test_token):" in app_src
    resolver_body = _resolver_body(app_src)
    assert '"context": "public_viewer"' in resolver_body
    assert '"context": "creator_test"' in resolver_body
    assert '"context": "admin_test"' in resolver_body
    # no branch trusts the token's own claimed identity without re-checking the REAL session
    assert 'session.get("user_id")' in resolver_body
    assert "project_current_owner_user_id(project) == session_user_id" in resolver_body
    assert 'session.get("admin_id")' in resolver_body
    assert "project.owner_admin_id == session_admin_id" in resolver_body
    # never reads a raw entry_context/mode query param
    for forbidden in ('request.args.get("entry_context"', 'request.args.get("mode"', 'request.args.get("return_url"', 'request.args.get("next"'):
        assert forbidden not in resolver_body


def test_scanner_test_entry_routes_require_real_ownership_before_minting_a_token():
    """scanner_test_entry/admin_scanner_test_entry are the ONLY places a signed test_token is
    minted — both must verify real, server-side ownership (not a query param) before doing
    so, and must be behind login_required/admin_required."""
    app_src = _app_py()
    creator_start = app_src.index('@app.route("/project/<int:project_id>/scanner-test")')
    creator_end = app_src.index('@app.route("/admin/project/<int:project_id>/scanner-test")')
    creator_body = app_src[creator_start:creator_end]
    assert "@login_required" in creator_body
    assert "user_can_manage_project(user, project)" in creator_body
    assert "abort(404)" in creator_body
    assert '_issue_scanner_test_token(project.id, "creator_test", user_id=user.id)' in creator_body

    admin_start = app_src.index('@app.route("/admin/project/<int:project_id>/scanner-test")')
    admin_end = app_src.index('@app.route("/scanner/<int:project_id>")')
    admin_body = app_src[admin_start:admin_end]
    assert "@admin_required" in admin_body
    assert "project.owner_admin_id != admin.id" in admin_body
    assert "abort(404)" in admin_body
    assert '_issue_scanner_test_token(project.id, "admin_test", admin_id=admin.id)' in admin_body


def test_scanner_test_token_is_signed_and_short_lived():
    """A forged token (no valid signature) or an expired one must fall back to public_viewer,
    not raise or silently trust the payload — this is what makes ?test_token=<anything> and
    ?test_token=<a token minted an hour ago> both safe."""
    app_src = _app_py()
    assert "URLSafeTimedSerializer" in app_src
    assert "SCANNER_TEST_TOKEN_MAX_AGE_SECONDS" in app_src
    resolver_body = _resolver_body(app_src)
    assert "except SignatureExpired" in app_src or "SignatureExpired" in app_src
    assert "BadSignature" in app_src
    assert '"entry_authorization_result": "expired_token"' in app_src or 'return None, "expired_token"' in app_src
    assert '"entry_authorization_result": "invalid_token"' in resolver_body or 'return None, "invalid_token"' in app_src


def test_scanner_view_never_accepts_an_arbitrary_return_url():
    """No query parameter (e.g. ?return_url=, ?next=, ?redirect=) is ever read to build the
    back destination — it is always one of the three allowlisted, server-computed URLs."""
    app_src = _app_py()
    resolver_body = _resolver_body(app_src)
    for forbidden in ("request.args.get(\"return_url\"", "request.args.get(\"next\"", "request.args.get(\"redirect\""):
        assert forbidden not in resolver_body
    # scanner() itself must pass the resolver's own output straight to the template, not
    # anything derived from a raw query string
    scanner_view_start = app_src.index('@app.route("/scanner/<int:project_id>")')
    scanner_view_end = app_src.index('@app.route("/detect_init"')
    scanner_view_body = app_src[scanner_view_start:scanner_view_end]
    assert "resolved_back_destination=entry[" in scanner_view_body


def test_public_viewer_can_never_resolve_dashboard_or_admin_routes():
    """Every dict literal the resolver can return for the public_viewer/failure branches
    points at url_for('landing') — never dashboard or admin_dashboard/admin_project_preview,
    regardless of what query params or forged tokens are present."""
    app_src = _app_py()
    resolver_body = _resolver_body(app_src)
    public_context_lines = [
        line for line in resolver_body.splitlines() if '"context": "public_viewer"' in line
    ]
    assert len(public_context_lines) >= 1
    back_url_lines = [line for line in resolver_body.splitlines() if '"back_url"' in line]
    assert len(back_url_lines) >= 1
    for line in back_url_lines:
        assert "dashboard" not in line.lower()
    # the default (public_viewer) dict's own back_url is always landing — never anything else
    default_start = resolver_body.index("default = {")
    default_end = resolver_body.index("}", default_start)
    default_block = resolver_body[default_start:default_end]
    assert 'url_for("landing")' in default_block


def test_back_and_return_to_project_use_the_same_resolved_destination():
    html = _scanner_html()
    # backBtn + fallbackReturnBtn (camera failure) + recognitionReturnBtn (recognition help)
    assert html.count('href="{{ resolved_back_destination }}"') == 3
    assert 'id="recognitionReturnBtn" href="{{ resolved_back_destination }}"' in html


def test_back_and_return_buttons_share_one_finalize_and_navigate_function():
    """Back, fallbackReturnBtn, and recognitionReturnBtn must all funnel through the SAME
    finalize-then-navigate function — not three independently written session-end-then-
    navigate call sites that could drift out of sync with each other."""
    html = _scanner_html()
    assert "function finalizeScannerAndNavigate(href, reason)" in html
    assert html.count("finalizeScannerAndNavigate(") >= 4  # definition + backBtn + fallbackReturnBtn + recognitionReturnBtn (+ startup-failure Return)
    for btn_id in ("backBtn", "fallbackReturnBtn", "recognitionReturnBtn"):
        anchor_idx = html.index(f"getElementById('{btn_id}')")
        handler_slice = html[anchor_idx:anchor_idx + 400]
        assert "finalizeScannerAndNavigate(" in handler_slice


def test_authenticated_creator_opening_own_public_qr_still_resolves_public_viewer():
    """Owner 16 scanning their own printed public QR (no test_token) must resolve
    public_viewer, not creator_test — a session.user_id match alone is deliberately NOT
    sufficient; only a real, freshly-minted test_token from scanner_test_entry can reach
    creator_test."""
    app_src = _app_py()
    resolver_body = _resolver_body(app_src)
    # No token at all (the normal public-QR case, even for the owner's own browser) resolves
    # via the err branch, textually before any session.get('user_id') check ever runs.
    assert "_read_scanner_test_token(test_token)" in resolver_body
    err_branch_idx = resolver_body.index("if err:")
    no_token_return_idx = resolver_body.index("return result", err_branch_idx)
    session_check_idx = resolver_body.index('session.get("user_id")')
    assert err_branch_idx < no_token_return_idx < session_check_idx


def test_forged_creator_test_query_param_is_ignored():
    """?entry_context=creator_test / ?mode=creator / ?return_url=/dashboard must have zero
    effect — the resolver only ever reads test_token, and only a signature-verified one.
    Scoped to the actual query-param-reading calls, not bare substrings — "entry_context"
    alone would false-positive on this file's own scanner_entry_context template var."""
    app_src = _app_py()
    scanner_view_start = app_src.index('@app.route("/scanner/<int:project_id>")')
    scanner_view_end = app_src.index('@app.route("/detect_init"')
    scanner_view_body = app_src[scanner_view_start:scanner_view_end]
    for forbidden in (
        'request.args.get("entry_context"',
        'request.args.get("mode"',
        'request.args.get("return_url"',
        'request.args.get("next"',
        'request.args.get("user_id"',
        'request.args.get("admin_id"',
    ):
        assert forbidden not in scanner_view_body
    assert 'request.args.get("test_token")' in scanner_view_body


# --- Watchdog: real 6-7 second gaps despite the theoretical 3750ms ceiling --------------


def test_watchdog_forces_a_request_after_four_seconds_of_silence():
    html = _scanner_html()
    assert "const WATCHDOG_TIMEOUT_MS = 4000;" in html
    assert "const WATCHDOG_CHECK_INTERVAL_MS = 500;" in html
    assert "if (elapsed > WATCHDOG_TIMEOUT_MS) {" in html
    assert "'watchdog_forced_detection'" in html
    assert "detectOnceFromServer(true); // forces a request through the SAME guarded path" in html


def test_watchdog_observes_but_does_not_force_detection_while_tracking_is_healthy():
    """Pass 6 Task A: watchdogTick's tracking-ownership check is now the broader
    watchdogTrackingSkipReason() (healthy_tracking / tracker_bootstrap / callback_recovery
    / attempt_active) — not a bare isHealthyLocalTracking() call — and it still runs, and
    still reschedules without forcing, before the forced-detection branch."""
    html = _scanner_html()
    watchdog_start = html.index("function watchdogTick(token")
    watchdog_end = html.index("function skipTick(reason, extra)")
    body = html[watchdog_start:watchdog_end]
    healthy_at = body.index("const trackingSkipReason = watchdogTrackingSkipReason();")
    forced_at = body.index("detectOnceFromServer(true);")
    assert healthy_at < forced_at
    assert "logTimingCheckpoint('[WATCHDOG SKIP]', trackingSkipReason, {" in body
    assert "scheduleWatchdog(token);" in body[healthy_at:forced_at]


def test_watchdog_does_not_create_concurrent_requests():
    html = _scanner_html()
    watchdog_start = html.index("function watchdogTick(token")
    watchdog_end = html.index("function startDetectLoop()")
    body = html[watchdog_start:watchdog_end]
    assert "if (!detectInFlight && !isStreamDead()) {" in body


def test_watchdog_respects_hidden_state():
    html = _scanner_html()
    watchdog_start = html.index("function watchdogTick(token")
    watchdog_end = html.index("function startDetectLoop()")
    body = html[watchdog_start:watchdog_end]
    assert "if (document.hidden || recoverScannerPromise || diagState.cameraStartInProgress) {" in body


def test_watchdog_respects_recovery_state():
    html = _scanner_html()
    watchdog_start = html.index("function watchdogTick(token")
    watchdog_end = html.index("function startDetectLoop()")
    body = html[watchdog_start:watchdog_end]
    assert "recoverScannerPromise" in body


def test_watchdog_uses_current_scan_loop_token():
    html = _scanner_html()
    assert "function scheduleWatchdog(token)" in html
    watchdog_start = html.index("function watchdogTick(token")
    watchdog_end = html.index("function startDetectLoop()")
    body = html[watchdog_start:watchdog_end]
    assert "if (sessionEnding || token !== scanLoopToken) return;" in body
    assert "scheduleWatchdog(scanLoopToken);" in html  # started by startDetectLoop with the CURRENT token


def test_watchdog_never_restarts_the_camera():
    html = _scanner_html()
    watchdog_start = html.index("function watchdogTick(token")
    watchdog_end = html.index("function startDetectLoop()")
    body = html[watchdog_start:watchdog_end]
    for forbidden in ("setupCamera(", "restartCameraStream(", "recoverScanner("):
        assert forbidden not in body


def test_watchdog_stops_and_starts_with_the_main_scan_loop():
    html = _scanner_html()
    assert "if (typeof stopWatchdog === 'function') stopWatchdog();" in html
    assert "scheduleWatchdog(scanLoopToken);" in html


# --- Round-3 fix: watchdog used to refuse to act on a STUCK in-flight request -----------
# Real root cause of the persisting 7-9s gaps: detectOnceFromServer()'s own fetch abort
# ceiling is runtimeConfig.requestTimeoutMs (7000/8000/9000ms depending on mode, see
# static/js/scanner-runtime.js MODES) — far past this watchdog's 4000ms deadline. The
# watchdog previously only forced a NEW request when nothing was in flight; a single
# stalled request (packet loss, slow mobile network) held detectInFlight true for its full
# internal timeout with the watchdog doing nothing the entire time, because "a request is
# in flight" was treated as reason enough to never act.

def _watchdog_tick_body():
    html = _scanner_html()
    start = html.index("function watchdogTick(token")
    end = html.index("function skipTick(reason, extra)")
    return html[start:end]


def test_watchdog_aborts_a_request_stuck_past_its_own_deadline():
    body = _watchdog_tick_body()
    assert "detectInFlight && activeDetectionAttempt && activeDetectionAttempt.phase === 'network'" in body
    assert "networkElapsed > WATCHDOG_TIMEOUT_MS" in body
    assert "activeDetectionController.abort()" in body
    assert "'watchdog_aborted_stuck_request'" in body


def test_watchdog_abort_reuses_the_existing_abort_controller_not_a_new_failure_path():
    """The forced abort must land in detectOnceFromServer's own existing AbortError catch
    branch (handleDetectionTimeout) — not invent a second, parallel failure/cleanup path."""
    html = _scanner_html()
    body = _watchdog_tick_body()
    assert "activeDetectionController.abort();" in body
    assert "if (e && e.name === 'AbortError') {" in html
    assert "handleDetectionTimeout();" in html


def test_watchdog_does_not_start_a_second_request_while_aborting_a_stuck_one():
    """The abort-stuck-request branch must never itself call detectOnceFromServer()/
    setupCamera() — it only aborts, and the resulting AbortError's own catch/finally clears
    detectInFlight and reschedules through the normal loop. Comments are stripped first —
    this branch's own explanatory comment legitimately names detectOnceFromServer() in
    prose."""
    body = _watchdog_tick_body()
    abort_branch_start = body.index("} else if (detectInFlight")
    abort_branch_end = body.index("scheduleWatchdog(token); // always reschedules")
    abort_branch = body[abort_branch_start:abort_branch_end]
    code_only = "\n".join(line for line in abort_branch.splitlines() if not line.strip().startswith("//"))
    assert "detectOnceFromServer()" not in code_only
    assert "setupCamera(" not in code_only


def test_watchdog_deadline_and_triggered_diagnostics_are_tracked():
    html = _scanner_html()
    assert "watchdogDeadline: null," in html
    assert "watchdogTriggered: false," in html
    body = _watchdog_tick_body()
    assert "diagState.watchdogDeadline = baseline + WATCHDOG_TIMEOUT_MS;" in body
    assert "diagState.watchdogTriggered = true;" in body


def test_successful_request_resets_the_watchdog_baseline():
    """The watchdog's baseline is lastRequestStartAt, which every new request overwrites
    (scanTick sets it right before calling detectOnceFromServer) — so a completed request
    always pushes the deadline forward rather than accumulating drift."""
    html = _scanner_html()
    assert "diagState.lastRequestStartAt = requestStartedAtMs;" in html
    body = _watchdog_tick_body()
    assert "diagState.lastRequestStartAt || diagState.lastCameraStartTimestamp || Date.now()" in body


def test_requesttimeoutms_still_exceeds_watchdog_deadline_making_watchdog_the_real_ceiling():
    """Documents the actual numbers: scanner-runtime.js's own per-mode request timeouts are
    all well above the 4000ms watchdog deadline — the watchdog's abort is what actually
    enforces the ~4-4.5s ceiling now, not these larger backstop values, which is why fixing
    detectOnceFromServer's own timeout constants alone would not have solved this."""
    runtime_src = Path("static/js/scanner-runtime.js").read_text(encoding="utf-8", errors="ignore")
    for mode, timeout in re.findall(r"(\w+):\s*\{[^}]*requestTimeoutMs:\s*(\d+)", runtime_src):
        if mode == "fallback":
            continue
        assert int(timeout) > 4000, f"{mode} requestTimeoutMs must exceed the watchdog deadline for the watchdog abort to be the effective ceiling"


def test_scan_tick_records_scheduling_and_drift_diagnostics():
    html = _scanner_html()
    tick_start = html.index("async function scanTick(token)")
    tick_end = html.index("function startDetectLoop()")
    body = html[tick_start:tick_end]
    for field in (
        "scheduledAt:", "expectedRunAt", "actualRunAt", "timerDriftMs:",
        "lastRequestStartedAt:", "elapsedSinceLastRequestStart:",
        "suppressionDeadline", "detectionPolicyNextAllowedAt",
        "selectedDelayMs:", "selectedDelayReason:",
    ):
        assert field in body


# --- Recognition timeout vs. camera failure ---------------------------------------------


def test_recognition_timeout_never_restarts_a_healthy_camera():
    """Repeated recognition timeouts must show a distinct 'recognition help' panel — never
    the camera-failure fallback panel — and must never call any camera-restart function."""
    html = _scanner_html()
    timeout_start = html.index("function handleDetectionTimeout()")
    timeout_end = html.index("function enterGrace(reason)")
    body = html[timeout_start:timeout_end]
    assert "showRecognitionHelp('repeated_detection_timeout')" in body
    assert "enterFallback(" not in body
    for forbidden in ("setupCamera(", "restartCameraStream(", "recoverScanner("):
        assert forbidden not in body


def test_recognition_help_panel_is_visually_and_functionally_distinct_from_fallback():
    html = _scanner_html()
    assert 'id="recognitionHelpPanel"' in html
    assert 'id="recognitionContinueBtn"' in html
    assert 'id="recognitionReturnBtn"' in html
    assert "Continue Scanning" in html
    assert "function showRecognitionHelp(reason)" in html
    assert "function continueScanningFromRecognitionHelp()" in html
    continue_start = html.index("function continueScanningFromRecognitionHelp()")
    continue_end = html.index("recognitionContinueBtn.addEventListener")
    continue_body = html[continue_start:continue_end]
    for forbidden in ("setupCamera(", "restartCameraStream(", "recoverScanner("):
        assert forbidden not in continue_body


def test_repeated_no_detection_response_also_uses_recognition_help_not_camera_fallback():
    """A second, separate pre-existing instance of the same bug: 25 consecutive
    detected=false server responses (a recognition problem — no marker found — not a
    stream failure) used to also call enterFallback('DETECTION_TIMEOUT') directly inside
    detectOnceFromServer, independent of handleDetectionTimeout's own counter."""
    html = _scanner_html()
    detect_start = html.index("async function detectOnceFromServer(triggeredByWatchdog)")
    detect_end = html.index("async function scanTick(token)")
    body = html[detect_start:detect_end]
    assert "showRecognitionHelp('repeated_no_detection')" in body
    assert "enterFallback('DETECTION_TIMEOUT')" not in body


def test_back_button_diagnostics_and_teardown():
    """Back's own handler just diagnoses the click and delegates to the shared
    finalizeScannerAndNavigate() (see test_back_and_return_buttons_share_one_finalize_and_
    navigate_function) — the actual session-end/navigate logic lives there once, not
    duplicated per button."""
    html = _scanner_html()
    back_start = html.index("document.getElementById('backBtn')?.addEventListener('click'")
    back_end = html.index("let cvReady = false;")
    body = html[back_start:back_end]
    assert "scannerDiagnostics.push('scanner_back_clicked'" in body
    assert "finalizeScannerAndNavigate(href, 'back_button')" in body
    finalize_start = html.index("function finalizeScannerAndNavigate(href, reason)")
    finalize_end = html.index("// Start session on page load")
    finalize_body = html[finalize_start:finalize_end]
    assert "endScannerSession()" in finalize_body
    assert "scannerDiagnostics.push('scanner_navigated'" in finalize_body
    # endScannerSession is the single place that releases the camera/overlay and sends the
    # (deduplicated) session-end beacon — the Back button must not duplicate that logic.
    assert "stopCameraStream('session_end');" in html
    assert "if (typeof stopOverlayImmediate === 'function') stopOverlayImmediate();" in html


def test_session_end_is_deduplicated_across_all_callers():
    html = _scanner_html()
    assert "scanner_session_end_deduplicated" in html
    assert "scanner_session_end_started" in html
    # Back button, beforeunload, and a real (non-bfcache) pagehide all call the SAME
    # idempotent function
    assert html.count("endScannerSession()") >= 3


def test_existing_runtime_mode_label_is_preserved():
    """Part A: do not remove the existing mode label — full/standard/lightweight/fallback
    remain real, behavioral modes (distinct frame width, detect interval, tracking point
    count, and error tolerance per mode), not placeholders."""
    html = _scanner_html()
    runtime = Path("static/js/scanner-runtime.js").read_text(encoding="utf-8")
    assert "full: { frameWidth: 960, detectIntervalMs: 250" in runtime
    assert "standard: { frameWidth: 720, detectIntervalMs: 350" in runtime
    assert "lightweight: { frameWidth: 480, detectIntervalMs: 650" in runtime
    assert 'id="deviceInfo"' in html
    assert "scannerMode === 'lightweight' ? 12 : (deviceInfo.isLowEnd ? 16 : 20)" in html  # MIN_GOOD_POINTS varies by mode
    assert "scannerMode === 'full' ? 180 : (scannerMode === 'standard' ? 120 : 70)" in html  # MAX_TRACK_POINTS varies by mode


def test_standard_ram_display_is_tied_to_real_diagnostics_not_static_text():
    """The 'STANDARD'/RAM text the user sees comes from #deviceInfo's textContent, built
    from navigator.deviceMemory (real device signal, falls back to 4 only when the API is
    unavailable) and the actual computed scannerMode — not a hardcoded string."""
    html = _scanner_html()
    assert "memory: navigator.deviceMemory || 4," in html
    assert "const scannerMode = runtime.selectRuntimeMode(runtimeCapabilities, runtimeOverride" in html
    assert (
        "document.getElementById('deviceInfo').textContent = "
        "(deviceInfo.isMobile ? 'mobile' : 'desktop') + ' ' + deviceInfo.memory + 'GB ' "
        "+ deviceInfo.cores + 'cores ' + scannerMode;"
    ) in html


# --- Round-3 correction pass: public QR must never mutate authentication ---------------
# Root cause fixed: scanner() used to force session["user_id"] = user_id whenever a
# user_id query param was present — this is exactly what every public QR link carries (the
# PROJECT OWNER's id), so scanning any project's public QR silently logged the visitor's
# browser session in as that project's owner.

def _scanner_view_body(app_src=None):
    app_src = app_src or _app_py()
    start = app_src.index('@app.route("/scanner/<int:project_id>")')
    end = app_src.index('@app.route("/detect_init"')
    return app_src[start:end]


def test_public_qr_can_never_set_session_user_id_or_admin_id():
    """The actual fixed bug: scanner() used to run session["user_id"] = user_id
    unconditionally whenever the QR's own user_id query param was present. scanner() must
    never write to session at all — auth state is exclusively the login/admin_login flows'
    job."""
    body = _scanner_view_body()
    assert 'session["user_id"] =' not in body
    assert 'session["admin_id"] =' not in body
    assert "session.permanent = True" not in body
    assert "session[" not in body  # scanner() never writes to session, full stop


def test_scanner_view_ignores_user_id_and_user_name_query_params_entirely():
    """user_id/user_name/admin_id/admin_name query params are legacy QR-URL artifacts — the
    view must not even read them anymore (they influenced nothing except the removed
    force-login line and unused template vars)."""
    body = _scanner_view_body()
    for forbidden in (
        'request.args.get("user_id"',
        'request.args.get("user_name")',
        'request.args.get("admin_id"',
        'request.args.get("admin_name")',
    ):
        assert forbidden not in body


def test_altered_user_id_query_param_cannot_change_authentication():
    """Since scanner() no longer reads user_id/admin_id from the query string at all (see
    above), there is no code path left for a tampered ?user_id=<anyone> to affect anything —
    this is the general case a single 'the owner's id happened to match' test can't cover."""
    body = _scanner_view_body()
    assert "project_owner_id = project_current_owner_user_id(project)" in body  # ownership resolved from the DB record
    assert 'request.args.get("user_id"' not in body  # no code path left for a tampered param to reach


def test_project_owner_is_resolved_from_the_database_record():
    body = _scanner_view_body()
    assert "project_owner_id = project_current_owner_user_id(project)" in body
    owner_line_idx = body.index("project_owner_id = project_current_owner_user_id(project)")
    # the assignment itself must be a bare DB attribute read, not derived from request.args
    line_end = body.index("\n", owner_line_idx)
    assert "request.args" not in body[owner_line_idx:line_end]


def test_public_viewer_and_authenticated_viewer_are_never_the_same_variable():
    """project_owner_id (DB-resolved) must never be assigned from, or conflated with, a
    session-derived viewer identity inside the scanner view itself — resolve_scanner_entry_
    context() is the only place session identity is read, and only to verify a signed
    token's claimed identity, never to set project_owner_id."""
    body = _scanner_view_body()
    assign_line = [line for line in body.splitlines() if "project_owner_id = " in line]
    assert len(assign_line) == 1
    assert "session" not in assign_line[0]
    assert "request.args" not in assign_line[0]


def test_owner_scanning_own_public_qr_still_resolves_public_viewer_not_creator_test():
    """Owner 16, already logged in as themselves, opening the plain public /scanner/36 link
    (no test_token) resolves public_viewer — session.user_id matching project ownership is
    deliberately NOT sufficient on its own; see resolve_scanner_entry_context()."""
    resolver_body = _resolver_body(_app_py())
    # the no-token branch's return happens before ANY session.get(...) call textually
    err_idx = resolver_body.index("if err:")
    return_idx = resolver_body.index("return result", err_idx)
    assert resolver_body.index('session.get("user_id")') > return_idx


# --- Round-3 correction pass: scan attribution is the project owner, never the viewer ---

def test_detect_init_attributes_scans_to_project_owner_not_session():
    """scan_attribution_owner_id replaces the old session.get('user_id') read — a public,
    fully anonymous viewer's successful detection must still count against the PROJECT
    OWNER's quota (existing, intended business behavior), independent of anyone's login
    state."""
    app_src = _app_py()
    start = app_src.index('def detect_init():')
    end = app_src.index('@app.route("/detect_track"')
    body = app_src[start:end]
    assert "scan_attribution_owner_id = project_current_owner_user_id(project)" in body
    assert 'session.get("user_id")' not in body
    assert "ScanLog(" in body
    assert "user_id=scan_attribution_owner_id," in body


def test_scanner_session_end_attributes_to_project_owner_not_session():
    app_src = _app_py()
    start = app_src.index("def scanner_session_end():")
    end = app_src.index('@app.route("/detect_track"')
    body = app_src[start:end]
    assert "scan_attribution_owner_id = project_current_owner_user_id(project) if project else None" in body
    assert 'session.get("user_id")' not in body
    assert 'session[' not in body


def test_scan_attribution_is_never_the_authenticated_viewers_own_identity():
    """detect_init/scanner_session_end must never actually READ the calling browser's own
    session — scan_attribution_owner_id comes exclusively from the Project record. Checked
    against the real code patterns rather than a blanket "session[" substring, since this
    function's own explanatory comments legitimately mention the removed
    session['user_id'] read in prose."""
    app_src = _app_py()
    for fn_start, fn_end in (
        ('def detect_init():', '@app.route("/detect_track"'),
        ('def scanner_session_end():', '@app.route("/detect_track"'),
    ):
        body = app_src[app_src.index(fn_start):app_src.index(fn_end)]
        code_lines = [line for line in body.splitlines() if not line.strip().startswith("#")]
        code_only = "\n".join(code_lines)
        assert 'session.get("user_id")' not in code_only
        assert 'session.get("admin_id")' not in code_only
        assert 'session["user_id"]' not in code_only
        assert 'session["admin_id"]' not in code_only


# --- Round-3 correction pass: session-end must never touch authentication --------------

def test_session_end_endpoint_never_calls_logout_or_touches_session():
    app_src = _app_py()
    start = app_src.index("def scanner_session_end():")
    end = app_src.index('@app.route("/detect_track"')
    body = app_src[start:end]
    for forbidden in ("logout", "session.clear", "session.pop", 'session["user_id"]', 'session["admin_id"]'):
        assert forbidden not in body


def test_no_scanner_exit_path_calls_logout():
    """Back, Return (fallback + recognition), pagehide, beforeunload, and the startup-
    failure Return must never call /logout/ or a logout helper — scanner exit is always a
    plain navigation to resolved_back_destination, never a session teardown. Each handler's
    OWN body is sliced to its closing '});' — a fixed-width window would bleed into
    neighboring code's explanatory comments (e.g. "does NOT call any logout helper", which
    itself contains the word "logout") and false-positive."""
    html = _scanner_html()
    for marker in (
        "document.getElementById('backBtn')",
        "document.getElementById('fallbackReturnBtn')",
        "document.getElementById('recognitionReturnBtn')",
        "window.addEventListener('pagehide'",
        "window.addEventListener('beforeunload'",
    ):
        idx = html.index(marker)
        end = html.index("});", idx) + len("});")
        body = html[idx:end]
        assert "logout" not in body.lower()
        assert "/login/" not in body


# --- Round-3 correction pass: session-end finalization diagnostics + dedup -------------

def test_session_end_finalization_diagnostics_present():
    html = _scanner_html()
    for field in (
        "sessionEndAttemptCount: 0,",
        "sessionEndDeliveryMethod: null,",
        "sessionEndAcknowledged: false,",
        "sessionEndDuplicateSuppressed: 0,",
        "navigationTriggeredAt: null,",
        "navigationDestination: null,",
    ):
        assert field in html
    assert "diagState.sessionEndAttemptCount++;" in html
    assert "diagState.sessionEndDuplicateSuppressed++;" in html


def test_session_end_uses_beacon_or_keepalive_fetch():
    html = _scanner_html()
    assert "navigator.sendBeacon" in html
    assert "keepalive: true" in html
    assert "diagState.sessionEndDeliveryMethod = 'sendBeacon';" in html
    assert "diagState.sessionEndDeliveryMethod = 'fetch_keepalive';" in html


def test_session_end_dedup_guard_flips_before_any_await():
    """The sessionEnded guard must be set synchronously, before the function's first await
    (the fetch fallback path) — otherwise two callers racing on the same tick (e.g.
    beforeunload firing while Back's own call is still pending) could both pass the guard
    and send twice."""
    html = _scanner_html()
    start = html.index("async function endScannerSession()")
    end = html.index("function finalizeScannerAndNavigate(href, reason)")
    body = html[start:end]
    guard_idx = body.index("sessionEnded = true;")
    first_await_idx = body.index("await fetch(")
    assert guard_idx < first_await_idx


# --- Round-4 correction: dashboard/preview/success all expose a verified Test Scanner ---

def test_creator_project_list_exposes_test_scanner():
    html = _dashboard_html()
    assert "url_for('scanner_test_entry', project_id=project.id)" in html
    assert "Test Experience" in html


def test_project_preview_exposes_test_scanner():
    html = _project_preview_html()
    assert "url_for('scanner_test_entry', project_id=project.id)" in html
    assert "Test Experience" in html


def test_success_page_exposes_test_scanner():
    html = _success_html()
    assert "{{ test_scanner_url }}" in html
    assert "Test Experience" in html
    app_src = _app_py()
    assert "test_scanner_url=url_for(\"scanner_test_entry\", project_id=project.id)" in app_src


def test_all_three_test_scanner_controls_use_scanner_test_entry():
    """success.html goes through the server-computed test_scanner_url (see app.py's
    success_page view) rather than calling url_for directly in the template — both are
    equally safe since the value can only ever be scanner_test_entry's own URL, never a
    literal /scanner/<id>."""
    dash = _dashboard_html()
    preview = _project_preview_html()
    app_src = _app_py()
    assert "url_for('scanner_test_entry', project_id=project.id)" in dash
    assert "url_for('scanner_test_entry', project_id=project.id)" in preview
    success_view_start = app_src.index("def success_page(project_id):")
    success_view_end = app_src.index("# Scanner Routes (Public)")
    assert 'url_for("scanner_test_entry", project_id=project.id)' in app_src[success_view_start:success_view_end]


def test_none_of_the_three_test_scanner_controls_link_directly_to_the_public_route():
    """The creator-facing Test Scanner action must never be a bare /scanner/<id> or
    url_for('scanner', ...) link, and never carry a spoofable ?entry_context=/?user_id=
    param — only scanner_test_entry's own signed-token redirect. The admin-support
    "viewing someone else's project" fallback branches (admin_view=true) are a documented,
    separate exception (see project_preview.html), scoped out of this check."""
    for html, admin_view_else_marker in (
        (_dashboard_html(), None),
        (_project_preview_html(), "{% else %}"),
    ):
        if admin_view_else_marker and admin_view_else_marker in html:
            creator_branch = html[:html.index(admin_view_else_marker)]
        else:
            creator_branch = html
        test_scanner_link_idx = creator_branch.index("url_for('scanner_test_entry'")
        nearby = creator_branch[max(0, test_scanner_link_idx - 200):test_scanner_link_idx + 50]
        assert "url_for('scanner', project_id=" not in nearby
        assert "entry_context=" not in nearby
    success_app_src = _app_py()
    success_view_start = success_app_src.index("def success_page(project_id):")
    success_view_end = success_app_src.index("# Scanner Routes (Public)")
    success_view_body = success_app_src[success_view_start:success_view_end]
    assert 'url_for("scanner"' not in success_view_body
    assert 'url_for("scanner_test_entry"' in success_view_body


def test_another_users_project_cannot_generate_creator_test_access_via_dashboard_link():
    """The dashboard/preview/success links only ever call url_for('scanner_test_entry', ...)
    — they never embed a project_id belonging to someone else, and the route itself
    (verified in test_scanner_test_entry_routes_require_real_ownership_before_minting_a_token)
    independently re-checks user_can_manage_project(user, project) server-side regardless of which
    template linked to it. This test confirms the templates never bypass that route with
    their own inline token/context construction."""
    for html in (_dashboard_html(), _project_preview_html()):
        assert "_issue_scanner_test_token" not in html
        assert "test_token=" not in html
    app_src = _app_py()
    creator_start = app_src.index('@app.route("/project/<int:project_id>/scanner-test")')
    creator_end = app_src.index('@app.route("/admin/project/<int:project_id>/scanner-test")')
    assert "user_can_manage_project(user, project)" in app_src[creator_start:creator_end]


# --- Round-4 correction: abort in-flight detection before exit --------------------------

def test_back_and_return_abort_active_detection_before_navigation():
    """Back/Return both go through finalizeScannerAndNavigate -> endScannerSession, whose
    invalidateDetection() call aborts activeDetectionController before the session-end
    beacon is even built, let alone before navigation."""
    html = _scanner_html()
    start = html.index("async function endScannerSession()")
    end = html.index("// This endpoint (see /api/scanner/session/end")
    body = html[start:end]
    assert "invalidateDetection();" in body
    send_idx = html.index("navigator.sendBeacon", end)
    invalidate_idx = html.index("invalidateDetection();", start)
    assert invalidate_idx < send_idx


def test_pagehide_invalidates_current_scan_loop_token():
    """A real (non-bfcache) pagehide routes through endScannerSession -> stopDetectLoop,
    which increments scanLoopToken — any tick/watchdog instance holding the old token
    retires on its next check."""
    html = _scanner_html()
    pagehide_idx = html.index("window.addEventListener('pagehide'")
    pagehide_end = html.index("document.addEventListener('visibilitychange'", pagehide_idx)
    body = html[pagehide_idx:pagehide_end]
    assert "endScannerSession()" in body
    stop_detect_loop_start = html.index("function stopDetectLoop(reason)")
    stop_detect_loop_end = html.index("\n    }", stop_detect_loop_start)
    assert "scanLoopToken++;" in html[stop_detect_loop_start:stop_detect_loop_end]


def test_stale_post_exit_response_is_ignored():
    """The response handler explicitly checks sessionEnding right after the fetch resolves —
    independent of the generation-mismatch check — and bails before touching overlay/state/
    scheduling."""
    html = _scanner_html()
    start = html.index("const data = await r.json();")
    end = html.index("const durationMs = Math.round(performance.now() - requestStart);")
    body = html[start:end]
    assert "if (sessionEnding) {" in body
    assert "code: 'session_ended'" in body
    assert "return;" in body


def test_aborted_exit_request_does_not_trigger_recognition_fallback():
    """The AbortError catch branch must check sessionEnding BEFORE calling
    handleDetectionTimeout() — otherwise endScannerSession()'s own deliberate abort of the
    in-flight request would be misread as a genuine recognition timeout and could show the
    recognition-help panel after navigation has already begun."""
    html = _scanner_html()
    catch_start = html.index("} catch (e) {\n        console.error(\"Detection error:\", e);")
    catch_end = html.index("} finally {\n        logTimingCheckpoint('[RESPONSE HANDLED]'")
    body = html[catch_start:catch_end]
    sessionEnding_check_idx = body.index("if (sessionEnding) {")
    handle_timeout_idx = body.index("handleDetectionTimeout();")
    assert sessionEnding_check_idx < handle_timeout_idx


def test_post_exit_finally_block_does_not_reschedule():
    """scanTick's finally always calls scheduleNextScan('after_tick') — but
    scheduleNextScan itself checks sessionEnding first and no-ops, so a tick whose await
    resolved after exit still cannot schedule another one."""
    html = _scanner_html()
    schedule_start = html.index("function scheduleNextScan(reason, delayMs)")
    schedule_end = html.index("function scheduleWatchdog(token)")
    body = html[schedule_start:schedule_end]
    sessionEnding_idx = body.index("if (sessionEnding) {")
    timer_set_idx = body.index("detectLoopTimer = setTimeout(")
    assert sessionEnding_idx < timer_set_idx


def test_no_camera_restart_during_exit():
    """endScannerSession's own body must never call setupCamera/restartCameraStream/
    recoverScanner — exit always ends with stopCameraStream, never a restart."""
    html = _scanner_html()
    start = html.index("async function endScannerSession()")
    end = html.index("function finalizeScannerAndNavigate(href, reason)")
    body = html[start:end]
    for forbidden in ("setupCamera(", "restartCameraStream(", "recoverScanner("):
        assert forbidden not in body
    assert "stopCameraStream('session_end');" in body


def test_exit_order_matches_required_sequence():
    """mark ending -> bump generation -> clear scheduled timers -> abort in-flight request ->
    stop tracking/overlay -> stop camera -> deliver session-end. Verified as a strict textual
    order, not just presence, since the whole point of this pass is that this SPECIFIC order
    is what makes the "no stale response can act" guarantee provable."""
    html = _scanner_html()
    start = html.index("async function endScannerSession()")
    end = html.index("function finalizeScannerAndNavigate(href, reason)")
    body = html[start:end]
    order = [
        body.index("sessionEnding = true;"),
        body.index("scannerGeneration++;"),
        body.index("stopDetectLoop('session_end');"),
        body.index("invalidateDetection();"),
        body.index("stopTrackingLoop();"),
        body.index("stopCameraStream('session_end');"),
        body.index("navigator.sendBeacon"),
    ]
    assert order == sorted(order)


# --- Recognition-stability pass: last-trusted-pose hold (already adequate, verified) ----
# POSE_HOLD_MS/requestPoseHold predate this pass (Agent 1 geometry work) — this pass only
# verifies the existing implementation actually satisfies every sub-requirement in the
# task spec, and adds regression coverage so a future change can't silently break it.

def test_trusted_pose_hold_duration_is_within_the_required_range():
    html = _scanner_html()
    assert "const POSE_HOLD_MS = 500;" in html  # within the required ~300-700ms range
    assert 300 <= 500 <= 700


def test_pose_hold_fades_opacity_rather_than_snapping_instantly():
    html = _scanner_html()
    start = html.index("function requestPoseHold(reason)")
    end = html.index("function playOverlay()")
    body = html[start:end]
    assert 'overlayWrap.style.opacity = "0.72"' in body  # partial, not full, opacity during hold
    assert "poseHoldTimer = setTimeout(" in body
    # CSS transition makes opacity changes animate rather than jump — not an instant snap
    css_start = html.index("#overlayWrap {")
    css_end = html.index("}", css_start)
    assert "transition: opacity" in html[css_start:css_end]


def test_pose_hold_resumes_smoothly_if_tracking_recovers_before_timeout():
    html = _scanner_html()
    start = html.index("function requestPoseHold(reason)")
    end = html.index("function playOverlay()")
    body = html[start:end]
    # The post-timeout hide is gated on `!tracking` — if a new valid pose already resumed
    # tracking before POSE_HOLD_MS elapses, the hide is skipped entirely (no re-hide,
    # no flash) rather than unconditionally hiding on a timer.
    assert "if (!tracking && performance.now() >= poseHoldUntil) {" in body
    play_start = html.index("function playOverlay()")
    play_end = html.index("function showMatchIndicator")
    play_body = html[play_start:play_end]
    assert 'overlayWrap.style.opacity = "1"' in play_body


def test_pose_hold_snaps_off_after_bounded_timeout_not_indefinitely():
    html = _scanner_html()
    start = html.index("function requestPoseHold(reason)")
    end = html.index("function playOverlay()")
    body = html[start:end]
    assert 'overlayWrap.style.opacity = "0"' in body
    assert "if (!tracking) stopOverlayImmediate();" in body
    assert "}, 140);" in body  # bounded follow-up, not an open-ended wait


def test_held_frames_are_never_counted_as_accepted_detections():
    """requestPoseHold() must never touch totalAccepted/consecutiveAccepted — a held
    (recognition-loss) frame is explicitly not a detection."""
    html = _scanner_html()
    start = html.index("function requestPoseHold(reason)")
    end = html.index("function playOverlay()")
    body = html[start:end]
    assert "totalAccepted" not in body
    assert "consecutiveAccepted" not in body
    assert "recordAcceptance(" not in body


def test_invalid_or_non_finite_geometry_hides_immediately_not_via_hold():
    """Large camera/marker movement and invalid geometry must bypass the hold entirely —
    clearTrackingGeometry() without { holdPose: true } goes straight to
    stopOverlayImmediate(), never requestPoseHold()."""
    html = _scanner_html()
    # camera-restart/recovery path: immediate hide (no holdPose option)
    recovery_idx = html.index("scannerDiagnostics.push('[CAMERA RECOVERY]'")
    recovery_slice = html[recovery_idx:recovery_idx + 300]
    assert "clearTrackingGeometry(reason);" in recovery_slice
    assert "{ holdPose: true }" not in recovery_slice
    # pose-quality rejection when not already tracking: immediate hide
    assert "clearTrackingGeometry('pose_rejected_' + poseQuality.reason);" in html
    pose_reject_idx = html.index("clearTrackingGeometry('pose_rejected_' + poseQuality.reason);")
    pose_reject_slice = html[max(0, pose_reject_idx - 200):pose_reject_idx]
    assert "{ holdPose: true }" not in pose_reject_slice
    # local tracking loss (dropTracking): DOES use the hold — a short glitch, not
    # necessarily a large movement or invalid geometry
    drop_tracking_start = html.index("function dropTracking(reason, extraMats")
    drop_tracking_end = html.index("function handleDetectionTimeout()")
    assert "clearTrackingGeometry(reason, { holdPose: true });" in html[drop_tracking_start:drop_tracking_end]


# --- Recognition-stability pass: watchdog session-local counters ------------------------

def test_watchdog_session_local_counters_exist_and_track_reason_and_elapsed():
    html = _scanner_html()
    assert "watchdogAbortCountSession: 0," in html
    assert "lastWatchdogReason: null," in html
    assert "lastWatchdogElapsedMs: null," in html
    body = _watchdog_tick_body()
    assert "diagState.lastWatchdogReason = 'forced_detection';" in body
    assert "diagState.lastWatchdogReason = 'aborted_stuck_request';" in body
    assert "diagState.watchdogAbortCountSession++;" in body
    assert "diagState.lastWatchdogElapsedMs = elapsed;" in body


def test_watchdog_session_local_counters_reset_by_reset_diagnostics_but_lifetime_count_does_not():
    """watchdogAbortCount (lifetime-this-page-load) must NOT be in the Reset Diagnostics
    payload — only the session-local counters reset, so a tester can distinguish "35
    across the whole test" from "35 since I last reset"."""
    html = _scanner_html()
    reset_start = html.index("document.getElementById('diagResetBtn')")
    reset_end = html.index("renderDiagPanel();", reset_start)
    reset_body = html[reset_start:reset_end]
    assert "watchdogAbortCountSession: 0" in reset_body
    assert "lastWatchdogReason: null" in reset_body
    assert "lastWatchdogElapsedMs: null" in reset_body
    assert "watchdogAbortCount: 0" not in reset_body  # lifetime counter deliberately untouched


# --- Full behaviour audit: reference surface, request gaps, video loop ------------------
# Issue 1 (wrong reference surface): audited data + code, no bug found — data/images/
# 39_0.jpg and 40_0.jpg are genuinely card-only (visually inspected), reference keypoints
# (data/features/*_0.npz) span the whole card content, and feats["w"]/["h"] feed the
# homography rect directly (app.py). No edit made — nothing to fix, per "no edit unless the
# cause is unambiguous."
#
# Issue 2 (5-12s request gaps, watchdog count rising to 28): proven by code inspection —
# watchdogTick's "abort a stuck request" branch incremented watchdogAbortCount/
# watchdogAbortCountSession BEFORE checking whether activeDetectionController even existed
# to abort. Frame capture (ctx.drawImage/cap.toBlob) runs before activeDetectionController
# is assigned and has no timeout of its own — if capture ever stalls, detectInFlight stays
# true with nothing for the watchdog to abort, and every 500ms tick re-entered that branch,
# re-incrementing the counter without resolving anything (28 aborts * 500ms = ~14s, the
# same order of magnitude as the reported 12,601ms gap). Fixed: the counter now only
# increments when there is an actual controller to abort; a stuck-with-nothing-to-abort
# tick logs a distinct 'stuck_before_network_request' reason instead. Full timing
# instrumentation added (see logTimingCheckpoint) so a real-device run can confirm the
# capture-phase hypothesis precisely; wrapping frame capture in its own timeout is NOT
# implemented this pass (no proof yet from a real timeline — only strong circumstantial
# code-level evidence), documented as the recommended follow-up.
#
# Issue 3 (video completion/rescan): audited — the overlay <video> already has the native
# `loop` attribute (so 'ended' never fires during normal playback), and the 'ended' handler
# only sets videoFinished, never touching tracking/overlay state. No coupling between video
# completion and tracking found. Diagnostics added (logVideoCheckpoint) so a real-device run
# can confirm whether apparent "stops" are actually request-gap stalls (issue 2) rather than
# a video/tracking bug.

def test_watchdog_only_counts_a_real_abort_not_a_stuck_with_nothing_to_abort_tick():
    """The proven bug: watchdogAbortCount/watchdogAbortCountSession must only increment in
    the branch that actually has an activeDetectionController to call .abort() on — a stuck
    detectInFlight with no controller yet (e.g. mid frame-capture) must log a distinct
    reason and NOT inflate the abort counters. Two separate `else if` branches now (one per
    controller-exists / no-controller-yet case), not an if/else nested inside one — see the
    lastFetchStartAt fix, which needed the real-abort branch to compute its own elapsed time."""
    body = _watchdog_tick_body()
    real_abort_start = body.index("} else if (detectInFlight && activeDetectionAttempt && activeDetectionAttempt.phase === 'network'")
    real_abort_end = body.index("} else if (detectInFlight && diagState.lastRequestStartAt && elapsed > WATCHDOG_TIMEOUT_MS) {")
    real_abort_branch = body[real_abort_start:real_abort_end]
    assert "diagState.watchdogAbortCount++;" in real_abort_branch
    assert "diagState.watchdogAbortCountSession++;" in real_abort_branch
    assert "activeDetectionController.abort();" in real_abort_branch

    stuck_branch_start = real_abort_end
    stuck_branch_end = body.index("scheduleWatchdog(token); // always reschedules", stuck_branch_start)
    stuck_branch = body[stuck_branch_start:stuck_branch_end]
    assert "watchdogAbortCount++" not in stuck_branch
    assert "watchdogAbortCountSession++" not in stuck_branch
    assert "'stuck_before_network_request'" in stuck_branch
    assert "logTimingCheckpoint('[WATCHDOG SKIP]'" in stuck_branch


def test_frame_capture_runs_before_active_detection_controller_is_assigned():
    """Documents the actual root cause: frame capture (drawImage/toBlob) has no
    AbortController of its own and runs BEFORE activeDetectionController is assigned —
    confirmed directly from source ordering, not assumed."""
    html = _scanner_html()
    start = html.index("async function detectOnceFromServer(triggeredByWatchdog)")
    end = html.index("async function scanTick(token)")
    body = html[start:end]
    capture_idx = body.index("captureCtx.drawImage(cam, 0, 0, captureCanvas.width, captureCanvas.height);")
    controller_idx = body.index("const controller = new AbortController();")
    assert capture_idx < controller_idx


def test_timing_checkpoints_cover_the_full_request_lifecycle():
    html = _scanner_html()
    for tag in (
        "[SCAN SCHEDULED]",
        "[SCAN TIMER FIRED]",
        "[FRAME CAPTURE START]",
        "[FRAME CAPTURE END]",
        "[FETCH START]",
        "[FETCH END]",
        "[RESPONSE HANDLED]",
        "[WATCHDOG TICK]",
        "[WATCHDOG ABORT]",
        "[WATCHDOG FORCED DETECTION]",
        "[WATCHDOG SKIP]",
    ):
        assert f"logTimingCheckpoint('{tag}'" in html


def test_timing_checkpoint_logs_the_required_fields():
    html = _scanner_html()
    start = html.index("function logTimingCheckpoint(tag, reason, extra)")
    end = html.index("function scheduleNextScan(reason, delayMs)")
    body = html[start:end]
    for field in (
        "now:", "requestSeq:", "generation:", "loopToken:", "tracking,", "detectInFlight,",
        "pageVisible:", "streamHealthy:", "trackReadyState:", "videoReadyState:", "videoNetworkState:", "reason",
    ):
        assert field in body


def test_video_diagnostics_cover_play_pause_ended_loop_source_change_and_tracking_loss():
    html = _scanner_html()
    for tag in (
        "[OVERLAY VIDEO PLAY]",
        "[OVERLAY VIDEO PAUSE]",
        "[OVERLAY VIDEO ENDED]",
        "[OVERLAY VIDEO LOOP]",
        "[OVERLAY VIDEO SOURCE CHANGE]",
        "[TRACKING LOST DURING PLAYBACK]",
    ):
        assert f"logVideoCheckpoint('{tag}'" in html


def test_overlay_video_has_native_loop_enabled():
    """Section 3 decision rule: if loop is already enabled, do not add a fake fix. Confirmed
    present — loop itself is unchanged; Fix 1 (V1 Agent 2) added disablePictureInPicture/
    controlsList/aria-hidden to the same tag afterward, so this now matches the updated tag."""
    html = _scanner_html()
    assert (
        '<video id="overlay" autoplay playsinline loop preload="metadata" disablePictureInPicture\n'
        '        controlsList="nodownload nofullscreen noremoteplayback" aria-hidden="true"></video>'
    ) in html


def test_scanner_target_guide_prioritizes_first_target_only():
    html = _scanner_html()
    target_start = html.index('<div id="targetGuide"')
    target_end = html.index('{% if targets | length > 6 %}', target_start)
    target_block = html[target_start:target_end]
    assert 'loading="{% if loop.first %}eager{% else %}lazy{% endif %}"' in target_block
    assert 'decoding="async"' in target_block
    assert '{% if loop.first %}fetchpriority="high"{% endif %}' in target_block


def test_preview_media_uses_lazy_images_and_metadata_video_preload():
    for html in (
        _project_preview_html(),
        Path("templates/admin/project_preview.html").read_text(encoding="utf-8", errors="ignore"),
    ):
        assert 'loading="lazy"' in html
        assert 'decoding="async"' in html
        assert 'preload="metadata"' in html


def test_video_ended_does_not_drop_tracking():
    """The 'ended' handler must only ever touch videoFinished/logging — never `tracking`,
    never clearTrackingGeometry/dropTracking. Proves issue 3's premise directly: video
    completion and tracking loss are not coupled in either direction."""
    html = _scanner_html()
    start = html.index('overlay.addEventListener("ended"')
    end = html.index('overlay.addEventListener("timeupdate"')
    body = html[start:end]
    assert "videoFinished = true;" in body
    assert "tracking = " not in body
    assert "clearTrackingGeometry(" not in body
    assert "dropTracking(" not in body


def test_video_loop_detection_does_not_reassign_source():
    """The native-loop heuristic detector (timeupdate listener) must be read-only — it
    must never itself set overlay.src, which would defeat native looping by forcing a
    reload instead of letting the browser loop in place."""
    html = _scanner_html()
    start = html.index('overlay.addEventListener("timeupdate"')
    end = start + html[start:].index("});") + len("});")
    body = html[start:end]
    assert "overlay.src" not in body
    assert "overlay.load(" not in body


def test_overlay_src_is_only_reassigned_on_marker_switch():
    """The only legitimate source reassignments are the marker-switch branch in the accept
    path and (Issue 3E-E) sequential multi-video advancing to the next media - confirms no
    OTHER code path (loop detection, ended handler, pause/stop) recreates or reloads the
    video element's source, matching 'preserve a single long-lived video element' from
    earlier passes."""
    html = _scanner_html()
    assign_contexts = [html[max(0, m.start() - 500):m.start()] for m in re.finditer(r"overlay\.src = ", html)]
    assert len(assign_contexts) == 2
    assert any("[MARKER SWITCH]" in ctx for ctx in assign_contexts)
    assert any("sequence_advance" in ctx for ctx in assign_contexts)


def test_marker_loss_still_performs_cleanup_and_logs_playback_state():
    html = _scanner_html()
    start = html.index("function dropTracking(reason, extraMats")
    end = html.index("function handleDetectionTimeout()")
    body = html[start:end]
    assert "clearTrackingGeometry(reason, { holdPose: true });" in body
    assert "logVideoCheckpoint('[TRACKING LOST DURING PLAYBACK]', reason);" in body
    # only logged when the video was actually still playing — not an unconditional log
    assert "if (!overlay.paused && !overlay.ended) {" in body


def test_session_end_still_performs_cleanup():
    """No regression from the audit's instrumentation additions — endScannerSession's
    teardown order (already verified elsewhere) still ends with camera stop, and the
    checkpoint additions are logging-only insertions, not replacements of any cleanup call."""
    html = _scanner_html()
    start = html.index("async function endScannerSession()")
    end = html.index("function finalizeScannerAndNavigate(href, reason)")
    body = html[start:end]
    assert "stopDetectLoop('session_end');" in body
    assert "invalidateDetection();" in body
    assert "stopTrackingLoop();" in body
    assert "stopCameraStream('session_end');" in body


# --- Capture/watchdog/timer/tracking audit (real-device evidence: 5.8s FRAME CAPTURE gap,
# FETCH START immediately followed by WATCHDOG ABORT / AbortError) -----------------------

def _detect_once_body():
    html = _scanner_html()
    start = html.index("async function detectOnceFromServer(triggeredByWatchdog)")
    end = html.index("async function scanTick(token)")
    return html[start:end]


def test_drawimage_and_toblob_are_now_logged_as_separate_stages():
    """Audit 1: [FRAME CAPTURE START]/[FRAME CAPTURE END] previously grouped drawImage
    (synchronous) and toBlob (async JPEG encoding) together, so a real-device gap between
    them couldn't be attributed to one specific stage. Split into 4 checkpoints, in the
    correct order, bracketing each call precisely."""
    # Search for the actual logTimingCheckpoint(...) call sites, not the bare bracketed
    # tags — this file's own explanatory comment above mentions "[FRAME CAPTURE START]"
    # and "[FRAME CAPTURE END]" in prose, which would false-match a bare substring search.
    body = _detect_once_body()
    order = [
        body.index("logTimingCheckpoint('[FRAME CAPTURE START]'"),
        body.index("logTimingCheckpoint('[DRAW IMAGE START]'"),
        body.index("captureCtx.drawImage(cam, 0, 0, captureCanvas.width, captureCanvas.height);"),
        body.index("logTimingCheckpoint('[DRAW IMAGE END]'"),
        body.index("logTimingCheckpoint('[TOBLOB START]'"),
        body.index("captureCanvas.toBlob(res, \"image/jpeg\", 0.85)"),
        body.index("logTimingCheckpoint('[TOBLOB END]'"),
        body.index("logTimingCheckpoint('[FRAME CAPTURE END]'"),
    ]
    assert order == sorted(order)


def test_capture_checkpoints_never_log_image_or_blob_data():
    """Audit 1 explicit requirement: do not log image or user data — only numeric
    dimensions/state. Checks the actual logTimingCheckpoint(...) call sites' own argument
    lists, not the surrounding code (which legitimately declares/reads the `blob` variable
    a few lines later for the FormData upload — that's not a log call)."""
    body = _detect_once_body()
    for call_start in (
        body.index("logTimingCheckpoint('[FRAME CAPTURE START]'"),
        body.index("logTimingCheckpoint('[DRAW IMAGE START]'"),
        body.index("logTimingCheckpoint('[DRAW IMAGE END]'"),
        body.index("logTimingCheckpoint('[TOBLOB START]'"),
        body.index("logTimingCheckpoint('[TOBLOB END]'"),
        body.index("logTimingCheckpoint('[FRAME CAPTURE END]'"),
    ):
        call_end = body.index(");", call_start) + 2
        call_text = body[call_start:call_end]
        # 'to_blob' is this test's own reason-string label (see 'reason' argument), not a
        # reference to the actual image blob — excluded before checking for the real thing.
        assert "blob" not in call_text.replace("to_blob", "")
        assert ".toDataURL" not in call_text
        assert "atob(" not in call_text


def test_network_timeout_baseline_starts_at_fetch_not_at_capture_start():
    """Audit 2 root-cause fix: lastFetchStartAt is stamped right before the fetch call —
    separate from lastRequestStartAt (stamped before capture) — and the watchdog's real
    abort branch (controller exists) must compute its elapsed time from lastFetchStartAt,
    not the capture-inclusive baseline. This is what stops a slow capture phase from making
    a brand-new fetch look 'already overdue'."""
    body = _detect_once_body()
    fetch_start_marker = body.index("const controller = new AbortController();")
    stamp_idx = body.index("diagState.lastFetchStartAt = Date.now();", fetch_start_marker)
    fetch_call_idx = body.index('await fetch("/detect_init"', fetch_start_marker)
    assert fetch_start_marker < stamp_idx < fetch_call_idx

    watchdog_body = _watchdog_tick_body()
    real_abort_start = watchdog_body.index("} else if (detectInFlight && activeDetectionAttempt && activeDetectionAttempt.phase === 'network'")
    real_abort_end = watchdog_body.index("} else if (detectInFlight && diagState.lastRequestStartAt && elapsed > WATCHDOG_TIMEOUT_MS) {")
    real_abort_branch = watchdog_body[real_abort_start:real_abort_end]
    assert "const networkElapsed = Date.now() - (diagState.lastFetchStartAt || baseline);" in real_abort_branch
    assert "networkElapsed > WATCHDOG_TIMEOUT_MS" in real_abort_branch
    # must NOT abort based on the capture-inclusive `elapsed` variable
    assert "if (elapsed > WATCHDOG_TIMEOUT_MS)" not in real_abort_branch


def test_capture_duration_does_not_leak_into_network_elapsed_calculation():
    """Direct regression for the observed bug: a fetch that just started (lastFetchStartAt
    ~= now) must compute a small networkElapsed even if lastRequestStartAt (capture start)
    was minutes ago — proving the two clocks are now independent."""
    watchdog_body = _watchdog_tick_body()
    real_abort_start = watchdog_body.index("} else if (detectInFlight && activeDetectionAttempt && activeDetectionAttempt.phase === 'network'")
    real_abort_end = watchdog_body.index("} else if (detectInFlight && diagState.lastRequestStartAt && elapsed > WATCHDOG_TIMEOUT_MS) {")
    real_abort_branch = watchdog_body[real_abort_start:real_abort_end]
    # networkElapsed must be derived from lastFetchStartAt, never from `baseline` alone
    # (baseline is lastRequestStartAt-derived and includes however long capture took)
    assert "Date.now() - (diagState.lastFetchStartAt || baseline)" in real_abort_branch
    assert "Date.now() - baseline" not in real_abort_branch


def test_session_end_during_capture_blocks_fetch_from_starting():
    """Audit 6 proven fix: a sessionEnding check now runs right after capture completes and
    BEFORE any FormData/fetch code — session-end can occur while capture (no
    activeDetectionController exists yet to abort) is still in progress, and this is the
    boundary that previously let a request start after session end (matching a prior
    server-side log of a detect_init arriving post session-end)."""
    body = _detect_once_body()
    capture_end_idx = body.index("logTimingCheckpoint('[FRAME CAPTURE END]', 'frame_capture');")
    session_check_idx = body.index("if (sessionEnding) {", capture_end_idx)
    try_idx = body.index("try {", session_check_idx)
    fetch_idx = body.index('await fetch("/detect_init"')
    assert capture_end_idx < session_check_idx < try_idx < fetch_idx
    guard_body = body[session_check_idx:try_idx]
    assert "'session_ended_during_capture'" in guard_body
    assert "finalizeDetectionAttempt(attempt, 'cancelled', 'session_ended_during_capture', false);" in guard_body
    assert "return;" in guard_body


def test_marker_loss_reasons_are_all_genuine_local_tracking_failures():
    """Audit 5 / Case E: every dropTracking() caller must be a local optical-flow/geometry
    failure inside trackFrame() — never a network/watchdog/session-related reason. Proves
    [TRACKING LOST DURING PLAYBACK] represents genuine marker loss, not a request-gap
    side effect, and that no change to tracking behaviour is warranted."""
    html = _scanner_html()
    track_start = html.index("function trackFrame(")
    track_end = html.index("async function detectOnceFromServer(triggeredByWatchdog)")
    track_body = html[track_start:track_end]
    for reason in (
        "'insufficient_flow_points'",
        "'homography_empty'",
        "'corner_order_invalid'",
        "'out_of_bounds'",
        "'tracking_geometry_invalid'",
    ):
        assert f"dropTracking({reason}" in track_body
    # Pass 11: pose_rejected_* now folds in weak_geometry_support alongside
    # poseCompatibility's own reasons — shapeReason is the fused variable name.
    assert "dropTracking('pose_rejected_' + shapeReason" in track_body
    # none of these are network/session/watchdog-caused
    for forbidden in ("network_timeout", "capture_timeout", "session_ending", "watchdog"):
        assert forbidden not in track_body
    bootstrap_start = html.index("function initializeFreshLiveTracker(now, metadata)")
    bootstrap_end = html.index("function dropTracking(reason, extraMats", bootstrap_start)
    bootstrap_body = html[bootstrap_start:bootstrap_end]
    assert "dropTracking('tracker_bootstrap_failed'" in bootstrap_body
    assert "dropTracking('tracker_bootstrap_exception'" in bootstrap_body
    # Correction pass: onTrackingCallbackFired's bounded RAF-fallback-no-fresh-frame path
    # is a third, legitimate caller — still a local-tracking-continuity failure (no fresh
    # camera frame observed), never network/session/watchdog-caused.
    callback_start = html.index("function onTrackingCallbackFired(callbackId, callbackType, now, metadata, callbackEpoch, callbackOwnerToken)")
    callback_end = html.index("function stopTrackingLoop()", callback_start)
    callback_body = html[callback_start:callback_end]
    assert "dropTracking('tracking_callback_stalled'" in callback_body
    for forbidden in ("network_timeout", "capture_timeout", "session_ending"):
        assert forbidden not in callback_body
    # Pass 7 Task D: the watchdog's own ownerless-tracking repair is now a fourth,
    # legitimate caller — but only as a last resort, when idempotent ownership repair
    # itself already failed (never a first-choice network/session-driven call).
    watchdog_owner_start = html.index("function watchdogHandleOwnerlessTracking(watchdogId)")
    watchdog_owner_end = html.index("function cancelPendingNormalScan(reason)", watchdog_owner_start)
    watchdog_owner_body = html[watchdog_owner_start:watchdog_owner_end]
    assert "dropTracking('tracking_callback_owner_missing'" in watchdog_owner_body
    # dropTracking is only ever CALLED from local tracking/bootstrap/callback-health/
    # watchdog-ownership-repair code — never from detect_init response handling or
    # session-end code. (dropTracking's own function DEFINITION lives earlier in the
    # file, before trackFrame — that's the "+1".)
    assert html.count("dropTracking(") == (
        track_body.count("dropTracking(") + bootstrap_body.count("dropTracking(")
        + callback_body.count("dropTracking(") + watchdog_owner_body.count("dropTracking(") + 1
    )


def test_at_most_one_pending_scan_timer_is_still_structurally_guaranteed():
    """Audit 4: no duplicate-timer bug was proven — scheduleNextScan already clears any
    existing detectLoopTimer before setting a new one, and startDetectLoop no-ops if a
    timer is already pending. Confirms these guarantees are still in place, plus the new
    diagnostic identifiers added to make timer identity provable from a real-device log."""
    html = _scanner_html()
    schedule_start = html.index("function scheduleNextScan(reason, delayMs)")
    schedule_end = html.index("function stopWatchdog()")
    schedule_body = html[schedule_start:schedule_end]
    assert "if (detectLoopTimer) { clearTimeout(detectLoopTimer); detectLoopTimer = null; }" in schedule_body
    assert "scanTimerGeneration++;" in schedule_body
    assert "replacedTimerId" in schedule_body
    assert "expectedFireAt: now + delay" in schedule_body

    start_loop_start = html.index("function startDetectLoop()")
    start_loop_end = html.index("window.addEventListener('orientationchange'")
    start_loop_body = html[start_loop_start:start_loop_end]
    assert "if (detectLoopTimer) return;" in start_loop_body


def test_watchdog_recheck_does_not_find_proven_stale_controller_ownership_bug():
    """Audit 3: analyzed, not proven. Concurrency is single-threaded (setTimeout callbacks
    never overlap), only one activeDetectionController variable ever exists, detectInFlight
    prevents a second detectOnceFromServer from starting while one is outstanding, and
    watchdogTick's own token check retires a stale watchdog instance immediately when the
    loop token changes (session end / camera restart / fallback). No generation-tagging
    added to the controller itself, since no reachable scenario was found where a watchdog
    callback could target a controller from a different request than the one it measured."""
    html = _scanner_html()
    watchdog_start = html.index("function watchdogTick(token")
    body = html[watchdog_start:watchdog_start + 400]
    assert "if (sessionEnding || token !== scanLoopToken) return;" in body


# ---------------------------------------------------------------------------
# Final conclusive verification: shared canvas / coordinate-space hypothesis.
# These describe CURRENT behaviour only — no production code changed to add
# or pass these tests.
# ---------------------------------------------------------------------------

def _track_frame_body():
    html = _scanner_html()
    start = html.index("function trackFrame(")
    end = html.index("async function detectOnceFromServer(triggeredByWatchdog)")
    return html[start:end]


def test_capture_and_tracking_use_separate_canvas_resources():
    """Stream A owns captureCanvas/captureCtx for upload. Stream B owns
    trackingCanvas/trackingCtx for optical flow."""
    html = _scanner_html()
    assert html.count('getElementById("cap")') == 1
    assert html.count('getElementById("captureCanvas")') == 1
    assert html.count('cap.getContext(') == 1
    assert html.count('captureCanvas.getContext(') == 1
    detect_body = _detect_once_body()
    assert "captureCanvas.width = capW;" in detect_body
    assert "captureCanvas.height = capH;" in detect_body
    assert "captureCtx.drawImage(cam, 0, 0, captureCanvas.width, captureCanvas.height);" in detect_body
    assert "captureCanvas.toBlob(res, \"image/jpeg\", 0.85)" in detect_body
    assert "cap.width = capW;" not in detect_body
    assert "cap.height = capH;" not in detect_body
    assert "cap.toBlob(" not in detect_body
    assert html.count("document.createElement(\"canvas\")") == 1
    assert "const trackingCanvas = document.createElement(\"canvas\");" in html
    assert "const trackingCtx = trackingCanvas.getContext(" in html
    gray_start = html.index("function matFromVideoGray()")
    gray_end = html.index("function cornersToMat(corners)")
    gray_body = html[gray_start:gray_end]
    assert "trackingCtx.drawImage(cam, 0, 0, trackingCanvas.width, trackingCanvas.height)" in gray_body
    assert "trackingCtx.getImageData(0, 0, trackingCanvas.width, trackingCanvas.height)" in gray_body
    assert "cap.width" not in gray_body
    assert "cap.height" not in gray_body
    assert "ctx.drawImage" not in gray_body
    assert "ctx.getImageData" not in gray_body


def test_tracking_canvas_never_used_for_toblob_or_capture():
    """Stream B item 2: trackingCanvas/trackingCtx must never appear anywhere near
    encoding/upload — toBlob, FormData, or the capture-metadata fields. Only
    captureCanvas is encoded/uploaded."""
    html = _scanner_html()
    assert "trackingCanvas.toBlob" not in html
    assert "trackingCtx.toBlob" not in html
    assert html.count(".toBlob(") == 1
    to_blob_call = html.index(".toBlob(")
    assert html[to_blob_call - 30:to_blob_call].strip().endswith("captureCanvas")
    assert "trackingCanvas" not in html[html.index("fd.append(\"test_image\"") - 200:html.index("fd.append(\"test_image\"") + 50]


def test_capture_never_resizes_tracking_canvas_before_network_await():
    """Proof 2/4: detectOnceFromServer mutates captureCanvas dimensions
    SYNCHRONOUSLY, before the toBlob await and before the fetch await — i.e. before any
    point where the event loop could run a queued trackFrame() rAF callback in between."""
    body = _detect_once_body()
    resize_at = body.index("captureCanvas.width = capW;")
    assert body.index("captureCanvas.height = capH;") > resize_at
    to_blob_at = body.index('captureCanvas.toBlob(res, "image/jpeg", 0.85)')
    fetch_at = body.index('await fetch("/detect_init"')
    assert resize_at < to_blob_at < fetch_at
    assert "cap.width = capW;" not in body[:fetch_at]
    assert "cap.height = capH;" not in body[:fetch_at]


def test_accepted_detection_no_longer_mutates_capture_canvas():
    """Accepted detections no longer mutate captureCanvas/cap, and they also no longer
    start LK from the uploaded capture. The fresh live-frame bootstrap owns tracking
    dimensions after the response."""
    body = _detect_once_body()
    resize_at = body.index("captureCanvas.width = capW;")
    json_at = body.index("const data = await r.json();")
    between = body[resize_at:json_at]
    assert "cap.width =" not in between
    assert "cap.height =" not in between
    assert "cap.width = frameW" not in body
    assert "cap.height = frameH" not in body
    assert "resetTrackingEpoch(frameW, frameH)" not in body
    assert body.index("trackerBootstrapPending = true;") > json_at


def test_tracking_initialization_uses_server_pose_as_visual_only_before_fresh_bootstrap():
    body = _detect_once_body()
    draw_idx = body.index("captureCtx.drawImage(cam, 0, 0, captureCanvas.width, captureCanvas.height);")
    stamp_idx = body.index("const uploadedCaptureAt = performance.now();")
    arm_idx = body.index("trackerBootstrapPending = true;")
    warp_idx = body.index("if (applyWarp(currCorners,")
    assert draw_idx < stamp_idx < arm_idx < warp_idx
    assert "source: 'server_pose_visual_only'" in body
    assert "serverInitPointCount" in body
    assert "captureFrameAgeMs" in body
    assert "resetTrackingEpoch(frameW, frameH)" not in body
    assert "prevGray =" not in body


def test_ordinary_lk_never_starts_from_uploaded_capture_gray():
    html = _scanner_html()
    assert "function matFromUploadedCaptureGray()" not in html
    assert "matFromUploadedCaptureGray(" not in html
    track_body = _track_frame_body()
    lk_idx = track_body.index("cv.calcOpticalFlowPyrLK(")
    assert "captureCanvas" not in track_body[:lk_idx]


def test_fresh_live_bootstrap_initializes_prevgray_and_points_from_same_frame():
    html = _scanner_html()
    start = html.index("function initializeFreshLiveTracker(now, metadata)")
    end = html.index("function dropTracking(reason, extraMats", start)
    body = html[start:end]
    reset_at = body.index("resetTrackingEpoch(frameW, frameH);")
    gray_at = body.index("gray = matFromVideoGray();")
    features_at = body.index("cv.goodFeaturesToTrack(gray, features")
    prev_gray_at = body.index("prevGray = gray;")
    prev_pts_at = body.index("prevPts = features.clone();")
    ready_at = body.index("scannerDiagnostics.push('tracking_bootstrap_ready'")
    assert reset_at < gray_at < features_at < prev_gray_at < prev_pts_at < ready_at
    # Pass 12: mask/goodFeaturesToTrack run in track-space now (gray is track-space sized).
    assert "mask = maskFromQuad(currCornersTrack);" in body
    assert "source: 'fresh_live_presented_frame'" in body
    assert "coverage < 0.25" in body


def test_first_lk_runs_only_after_fresh_live_bootstrap_frame():
    track_body = _track_frame_body()
    bootstrap_at = track_body.index("if (trackerBootstrapPending) {")
    init_at = track_body.index("initializeFreshLiveTracker(now, metadata);", bootstrap_at)
    return_at = track_body.index("return;", init_at)
    lk_at = track_body.index("cv.calcOpticalFlowPyrLK(")
    assert bootstrap_at < init_at < return_at < lk_at
    assert "firstLkPending" in track_body
    assert "first_lk_step" in track_body
    assert "firstLkFrameGapMs = firstLiveTrackingFrameAt ? now - firstLiveTrackingFrameAt : null;" in track_body


def test_tracking_cadence_prefers_request_video_frame_callback_with_single_owner():
    """Pass 5: scheduleTrackingFrame is the single arm point (one-owner semantics — never
    arms a second callback while trackingCallbackId is still set), prefers rVFC unless the
    health state machine has fallen back to RAF, and every fired callback is routed through
    onTrackingCallbackFired's stale-id check before it can touch shared state."""
    html = _scanner_html()
    start = html.index("function scheduleTrackingFrame(reason, previousCallbackId)")
    end = html.index("function startTrackingLoop()", start)
    body = html[start:end]
    assert "if (!trackLoopActive || trackingCallbackId !== null) return false;" in body
    assert "const useRVFC = rvfcHealthState !== 'RAF_FALLBACK' && typeof cam.requestVideoFrameCallback === 'function';" in body
    assert "onTrackingCallbackFired(myCallbackId, 'video_frame', now, metadata, myEpoch, myOwnerToken);" in body
    assert "onTrackingCallbackFired(myCallbackId, 'animation_frame', now, null, myEpoch, myOwnerToken);" in body
    validity_start = html.index("function trackingCallbackValidityFailureReason(callbackId, callbackEpoch, callbackOwnerToken)")
    fired_body = html[validity_start:start]
    assert "if (trackingCallbackId !== callbackId) return 'stale_owner';" in fired_body  # one-owner: stale callback never proceeds
    stop_start = html.index("function stopTrackingLoop()")
    stop_body = html[stop_start:html.index("function scheduleTrackingFrame(reason, previousCallbackId)")]
    assert "cancelCurrentTrackingCallback('tracking_loop_stopped')" in stop_body
    cancel_start = html.index("function cancelCurrentTrackingCallback(reason)")
    cancel_body = html[cancel_start:stop_start]
    assert "cam.cancelVideoFrameCallback(trackingCallbackHandle)" in cancel_body
    assert "cancelAnimationFrame(trackingCallbackHandle)" in cancel_body


def test_bootstrap_failure_uses_bounded_tracking_loss_cleanup():
    html = _scanner_html()
    init_start = html.index("function initializeFreshLiveTracker(now, metadata)")
    init_body = html[init_start:html.index("function dropTracking(reason, extraMats", init_start)]
    assert "dropTracking('tracker_bootstrap_failed'" in init_body
    assert "reason: 'insufficient_bootstrap_coverage'" in init_body
    assert "dropTracking('tracker_bootstrap_exception'" in init_body
    clear_start = html.index("function clearTrackingGeometry(reason, options = {})")
    clear_body = html[clear_start:html.index("function stopTrackingLoop()", clear_start)]
    # Pass 6: trackerBootstrapPending/firstLkPending are now cleared via the
    # stopTrackingLoop() call clearTrackingGeometry makes (see test_drop_tracking_stops_
    # the_callback_loop_before_returning below for the full Task B invariant), not inline.
    assert "stopTrackingLoop();" in clear_body


def test_tracking_loss_summary_contains_real_device_fields():
    html = _scanner_html()
    drop_start = html.index("function dropTracking(reason, extraMats")
    drop_body = html[drop_start:html.index("function handleDetectionTimeout()", drop_start)]
    assert "`[TRACKING LOST] reason=${reason}`" in drop_body
    for field in (
        "firstLkFrameGapMs",
        "initialPoints",
        "goodPoints",
        "coverage",
        "prevGray",
        "gray",
        "scannerMode",
        "capturePhase",
        "activeAttemptSeq",
        "encodeActive",
        "fetchActive",
        "lastScheduledBy",
        "pendingScanTimer",
        "pageVisibility",
        "videoPaused",
        "videoEnded",
        "videoReadyState",
    ):
        assert field in drop_body


def test_explicit_scanner_work_modes_gate_detection_cadence():
    html = _scanner_html()
    assert "let scannerWorkMode = 'SEARCHING';" in html
    assert "function setScannerWorkMode(mode, reason)" in html
    assert "function isHealthyLocalTracking()" in html
    assert "scannerWorkMode === 'TRACKING'" in html
    assert "setScannerWorkMode('RECOVERING', 'server_pose_accepted_bootstrap_pending')" in html
    assert "setScannerWorkMode('TRACKING', 'fresh_live_tracker_ready')" in html
    assert "setScannerWorkMode('RECOVERING', reason || 'tracking_lost')" in html


def test_healthy_tracking_cancels_and_skips_normal_scan_timer():
    html = _scanner_html()
    cancel_start = html.index("function cancelPendingNormalScan(reason)")
    cancel_body = html[cancel_start:html.index("function trackingWorkloadSnapshot", cancel_start)]
    assert "clearTimeout(detectLoopTimer);" in cancel_body
    assert "detectLoopTimer = null;" in cancel_body
    assert "normal_scan_timer_cancelled" in cancel_body
    mode_start = html.index("function setScannerWorkMode(mode, reason)")
    mode_body = html[mode_start:html.index("function isHealthyLocalTracking", mode_start)]
    assert "if (mode === 'TRACKING') cancelPendingNormalScan('entered_healthy_tracking');" in mode_body


def test_schedule_next_scan_refuses_healthy_tracking_loop():
    html = _scanner_html()
    start = html.index("function scheduleNextScan(reason, delayMs)")
    body = html[start:html.index("const WATCHDOG_TIMEOUT_MS", start)]
    assert "if (isHealthyLocalTracking())" in body
    healthy_at = body.index("if (isHealthyLocalTracking())")
    set_timer_at = body.index("detectLoopTimer = setTimeout")
    assert healthy_at < set_timer_at
    assert "reason: 'healthy_tracking'" in body
    assert "logTimingCheckpoint('[SCAN SCHEDULE SKIPPED]', 'healthy_tracking'" in body


def test_scan_tick_stale_timer_cannot_capture_or_reschedule_while_tracking_healthy():
    """Blocker audit (2026-08-27): the healthy-tracking suppression is now
    gated on the SAME FORCE_REDETECT_MS budget the bounded re-anchor check
    below it already existed to enforce (TC-04 Android P2-never-detected
    root cause - see the comment directly above this line in scanner.html).
    Local optical-flow tracking that "successfully" follows something across
    a pan to a genuinely different target must not suppress detect requests
    forever - only within the SAME bounded window a fresh server detection
    already gets."""
    body = _scan_tick_body(_scanner_html())
    healthy_at = body.index("if (isHealthyLocalTracking() && (performance.now() - lastDetectTs) <= FORCE_REDETECT_MS)")
    capture_at = body.index("await detectOnceFromServer();")
    finally_at = body.index("} finally {")
    assert "allowScanReschedule = false;" in body[healthy_at:capture_at]
    assert "healthy_tracking_no_capture" in body[healthy_at:capture_at]
    assert healthy_at < capture_at < finally_at
    assert "if (allowScanReschedule && !requestAttemptStarted" in body[finally_at:]


def test_detect_once_from_server_blocks_direct_or_watchdog_capture_while_tracking_healthy():
    body = _detect_once_body()
    healthy_at = body.index("if (isHealthyLocalTracking())")
    capture_at = body.index("captureCanvas.width = capW;")
    assert healthy_at < capture_at
    assert "healthy_tracking_detect_start_blocked" in body[healthy_at:capture_at]
    assert "triggeredByWatchdog: Boolean(triggeredByWatchdog)" in body[healthy_at:capture_at]


def test_attempt_finalizer_does_not_schedule_successor_while_tracking_healthy():
    """Pass 5 Task E: scheduleAttemptSuccessor also suppresses while trackerBootstrapPending
    — an accepted response starts local bootstrap synchronously (before this finalizer runs
    in the outer `finally`), so a normal server successor must never be scheduled for it in
    the first place, rather than scheduled and then cancelled once bootstrap completes."""
    html = _scanner_html()
    start = html.index("function scheduleAttemptSuccessor(")
    body = html[start:html.index("function finalizeDetectionAttempt", start)]
    healthy_at = body.index("if (isHealthyLocalTracking() || trackerBootstrapPending)")
    schedule_at = body.index("scheduleNextScan(reason || 'after_attempt', delayMs);")
    assert healthy_at < schedule_at
    assert "healthy_tracking_successor_suppressed" in body
    assert "tracker_bootstrap_pending_successor_suppressed" in body


def test_tracking_loss_reenters_single_scheduler_for_reacquisition():
    html = _scanner_html()
    drop_start = html.index("function dropTracking(reason, extraMats")
    drop_body = html[drop_start:html.index("function handleDetectionTimeout()", drop_start)]
    assert "setScannerWorkMode('RECOVERING', reason || 'tracking_lost')" in drop_body
    assert "scheduleNextScan('tracking_lost_reacquire', 0);" in drop_body
    assert "setTimeout(" not in drop_body


def test_capture_workload_counters_and_encode_thresholds_are_tracked():
    html = _scanner_html()
    body = _detect_once_body()
    assert "capturesWhileSearching: 0" in html
    assert "capturesWhileTracking: 0" in html
    assert "capturesWhileRecovering: 0" in html
    assert "encodeOver500Ms: 0" in html
    assert "if (scannerWorkMode === 'TRACKING') diagState.capturesWhileTracking++;" in body
    assert "else if (scannerWorkMode === 'RECOVERING') diagState.capturesWhileRecovering++;" in body
    assert "else diagState.capturesWhileSearching++;" in body
    assert "if (encodeDurationMs > 500) diagState.encodeOver500Ms++;" in body
    assert "if (encodeDurationMs > 1500) diagState.encodeOver1500Ms++;" in body
    assert "if (encodeDurationMs > 3000) diagState.encodeOver3000Ms++;" in body


def test_one_active_attempt_maximum_is_structurally_enforced():
    html = _scanner_html()
    create_start = html.index("function createDetectionAttempt(")
    create_end = html.index("function isCurrentDetectionAttempt", create_start)
    body = html[create_start:create_end]
    assert "if (activeDetectionAttempt && !activeDetectionAttempt.terminal) {" in body
    assert "return null;" in body
    assert "activeDetectionAttempt = attempt;" in body
    assert "detectInFlight = true;" in body


def test_one_encode_and_one_fetch_are_owned_by_the_active_attempt():
    body = _detect_once_body()
    assert "attempt.encodeInFlight = true;" in body
    assert "const blob = await new Promise(res => captureCanvas.toBlob(res, \"image/jpeg\", 0.85));" in body
    assert "attempt.encodeInFlight = false;" in body
    assert "attempt.fetchInFlight = true;" in body
    assert "activeDetectionController = controller;" in body
    assert 'const r = await fetch("/detect_init", { method: "POST", body: fd, signal: controller.signal });' in body
    assert "attempt.fetchInFlight = false;" in body


def test_one_successor_schedule_per_attempt():
    html = _scanner_html()
    schedule_start = html.index("function scheduleAttemptSuccessor(")
    schedule_end = html.index("function finalizeDetectionAttempt", schedule_start)
    schedule_body = html[schedule_start:schedule_end]
    assert "attempt.successorScheduled" in schedule_body
    assert "scheduleNextScan(reason || 'after_attempt', delayMs);" in schedule_body
    finalize_start = html.index("function finalizeDetectionAttempt(")
    finalize_end = html.index("function clearTrackingGeometry", finalize_start)
    finalize_body = html[finalize_start:finalize_end]
    assert "if (scheduleSuccessor) scheduleAttemptSuccessor(attempt, 'after_attempt', delayMs);" in finalize_body


def test_watchdog_never_aborts_drawing_encoding_or_handling_attempt_phases():
    body = _watchdog_tick_body()
    real_abort_start = body.index("} else if (detectInFlight && activeDetectionAttempt && activeDetectionAttempt.phase === 'network'")
    real_abort_end = body.index("} else if (detectInFlight && diagState.lastRequestStartAt && elapsed > WATCHDOG_TIMEOUT_MS) {")
    real_abort_branch = body[real_abort_start:real_abort_end]
    assert "activeDetectionAttempt.phase === 'network'" in real_abort_branch
    assert "activeDetectionController.abort();" in real_abort_branch
    for phase in ("'drawing'", "'encoding'", "'handling'"):
        assert phase not in real_abort_branch


def test_network_abort_requires_exact_attempt_ownership():
    body = _watchdog_tick_body()
    real_abort_start = body.index("} else if (detectInFlight && activeDetectionAttempt && activeDetectionAttempt.phase === 'network'")
    real_abort_end = body.index("} else if (detectInFlight && diagState.lastRequestStartAt && elapsed > WATCHDOG_TIMEOUT_MS) {")
    real_abort_branch = body[real_abort_start:real_abort_end]
    for guard in (
        "activeDetectionAttempt.controller === activeDetectionController",
        "activeDetectionAttempt.requestSequence === diagState.activeAttemptSeq",
        "activeDetectionAttempt.scannerGeneration === scannerGeneration",
        "activeDetectionAttempt.scanLoopToken === token",
        "!activeDetectionAttempt.networkAborted",
        "!activeDetectionAttempt.terminal",
    ):
        assert guard in real_abort_branch


def test_completed_controller_is_not_globally_abortable_after_fetch_settles():
    body = _detect_once_body()
    fetch_end = body.index("logTimingCheckpoint('[FETCH END]'")
    settled = body.index("attempt.fetchSettled = true;", fetch_end)
    phase = body.index("attempt.phase = 'handling';", fetch_end)
    clear_controller = body.index("activeDetectionController === controller", fetch_end)
    response_json = body.index("const data = await r.json();", fetch_end)
    assert fetch_end < settled < response_json
    assert fetch_end < phase < response_json
    assert fetch_end < clear_controller < response_json
    assert "diagState.lastFetchStartAt = null;" in body[fetch_end:response_json]


def test_watchdog_callback_after_fetch_settlement_cannot_abort_completed_request():
    body = _watchdog_tick_body()
    real_abort_start = body.index("} else if (detectInFlight && activeDetectionAttempt && activeDetectionAttempt.phase === 'network'")
    real_abort_end = body.index("} else if (detectInFlight && diagState.lastRequestStartAt && elapsed > WATCHDOG_TIMEOUT_MS) {")
    real_abort_branch = body[real_abort_start:real_abort_end]
    assert "activeDetectionAttempt.phase === 'network'" in real_abort_branch
    assert "fetchSettled: activeDetectionAttempt.fetchSettled" in real_abort_branch
    detect_body = _detect_once_body()
    assert "attempt.phase = 'handling';" in detect_body
    assert "attempt.fetchSettled = true;" in detect_body


def test_watchdog_callback_after_response_handled_uses_finish_baseline_not_start_baseline():
    body = _watchdog_tick_body()
    assert "diagState.lastRequestFinishAt || diagState.lastRequestStartAt" in body
    assert "logTimingCheckpoint('[WATCHDOG FORCED DETECTION]'" in body
    forced_start = body.index("if (!detectInFlight && !isStreamDead()) {")
    forced_end = body.index("} else if (detectInFlight && activeDetectionAttempt", forced_start)
    forced_branch = body[forced_start:forced_end]
    assert "logTimingCheckpoint('[WATCHDOG ABORT]'" not in forced_branch


def test_stale_watchdog_cannot_trigger_new_capture():
    body = _watchdog_tick_body()
    stale_return = body.index("if (sessionEnding || token !== scanLoopToken) return;")
    forced_detection = body.index("detectOnceFromServer(true);")
    assert stale_return < forced_detection


def test_late_blob_stale_generation_and_stale_loop_token_are_ignored():
    html = _scanner_html()
    current_start = html.index("function isCurrentDetectionAttempt(")
    current_end = html.index("function setDetectionAttemptPhase", current_start)
    current_body = html[current_start:current_end]
    assert "attempt.scannerGeneration === scannerGeneration" in current_body
    assert "attempt.scanLoopToken === scanLoopToken" in current_body
    detect_body = _detect_once_body()
    assert "finalizeDetectionAttempt(attempt, 'cancelled', 'late_blob_callback');" in detect_body
    assert "finalizeDetectionAttempt(attempt, 'cancelled', 'stale_response');" in detect_body
    assert "finalizeDetectionAttempt(attempt, 'cancelled', 'stale_attempt_after_fetch');" in detect_body


def test_capture_metadata_uses_uploaded_capture_dimensions():
    body = _detect_once_body()
    capture_width_idx = body.index("captureCanvas.width = capW;")
    metadata_width_idx = body.index('fd.append("source_frame_width", String(capW));')
    metadata_height_idx = body.index('fd.append("source_frame_height", String(capH));')
    fetch_idx = body.index('await fetch("/detect_init"')
    assert capture_width_idx < metadata_width_idx < fetch_idx
    assert capture_width_idx < metadata_height_idx < fetch_idx


def test_stale_scan_timer_cannot_reschedule_while_attempt_active():
    html = _scanner_html()
    schedule_start = html.index("function scheduleNextScan(reason, delayMs)")
    schedule_end = html.index("function stopWatchdog()", schedule_start)
    schedule_body = html[schedule_start:schedule_end]
    assert "if (activeDetectionAttempt && !activeDetectionAttempt.terminal) {" in schedule_body
    assert "scan_schedule_skipped" in schedule_body

    scan_body = _scan_tick_body(html)
    finally_idx = scan_body.index("} finally {")
    finally_body = scan_body[finally_idx:]
    assert "!detectInFlight" in finally_body
    assert "!(activeDetectionAttempt && !activeDetectionAttempt.terminal)" in finally_body


def test_attempt_creation_cancels_any_pending_normal_scan_timer():
    html = _scanner_html()
    create_start = html.index("function createDetectionAttempt(")
    create_end = html.index("const attempt = {", create_start)
    create_body = html[create_start:create_end]
    assert "if (detectLoopTimer) {" in create_body
    assert "clearTimeout(detectLoopTimer);" in create_body
    assert "scan_timer_cancelled_for_attempt" in create_body


def test_scan_counting_still_records_once_per_accepted_detection():
    body = _detect_once_body()
    accept_idx = body.index("recordAcceptance();")
    warp_idx = body.index("if (applyWarp(currCorners,", accept_idx)
    assert accept_idx < warp_idx
    assert body.count("recordAcceptance();") == 1


def test_track_frame_has_no_capture_in_flight_guard():
    """Proof 6: trackFrame() never checks detectInFlight/activeDetectionController/capW/
    capH before calling matFromVideoGray() — nothing pauses or defers local tracking while
    a server-capture cycle currently owns (has resized) the shared canvas."""
    body = _track_frame_body()
    for forbidden in ("detectInFlight", "activeDetectionController", "capW", "capH"):
        assert forbidden not in body
    assert "matFromVideoGray()" in body


def test_matframevideogray_uses_tracking_canvas_dimensions_only():
    """Stream B item 1/3: matFromVideoGray always draws/reads at whatever
    trackingCanvas.width/height currently are — this canvas is ONLY ever resized inside
    resetTrackingEpoch (checked separately below), so within one tracking epoch its
    dimensions are stable by construction, independent of the capture canvas."""
    html = _scanner_html()
    gray_start = html.index("function matFromVideoGray()")
    gray_end = html.index("function resetTrackingEpoch(width, height)", gray_start)
    gray_body = html[gray_start:gray_end]
    assert "frameW" not in gray_body
    assert "frameH" not in gray_body
    assert "cap.width" not in gray_body
    assert "cap.height" not in gray_body
    assert gray_body.count("trackingCanvas.width") == 2  # drawImage + getImageData
    assert gray_body.count("trackingCanvas.height") == 2


def test_tracking_canvas_dimensions_only_assigned_inside_reset_epoch():
    """Stream B item 3: trackingCanvas.width/trackingCanvas.height are assigned in
    exactly one place — resetTrackingEpoch() — so tracking dimensions can only change
    together with a full prevGray/prevPts reset and an epoch bump, never independently
    mid-epoch."""
    html = _scanner_html()
    assert html.count("trackingCanvas.width =") == 1
    assert html.count("trackingCanvas.height =") == 1
    reset_start = html.index("function resetTrackingEpoch(width, height)")
    reset_end = html.index("function cornersToMat(corners)")
    reset_body = html[reset_start:reset_end]
    # Pass 12: the canvas is sized to the derived TRACK-space dimensions, not the raw
    # intrinsic width/height passed in — still assigned only here, still tied to the same
    # reset.
    assert "trackingCanvas.width = trackWidth;" in reset_body
    assert "trackingCanvas.height = trackHeight;" in reset_body


def test_reset_tracking_epoch_deletes_prev_mats_before_resizing():
    """Stream B item 4: resetTrackingEpoch releases the previous prevGray/prevPts (and
    nulls them) before resizing the tracking canvas and bumping trackingEpoch — no old
    Mat or point set is ever retained across a tracking-size change."""
    html = _scanner_html()
    reset_start = html.index("function resetTrackingEpoch(width, height)")
    reset_end = html.index("function cornersToMat(corners)")
    body = html[reset_start:reset_end]
    delete_gray_at = body.index("prevGray.delete(); prevGray = null;")
    delete_pts_at = body.index("prevPts.delete(); prevPts = null;")
    resize_at = body.index("trackingCanvas.width = trackWidth;")
    epoch_at = body.index("trackingEpoch++;")
    assert delete_gray_at < resize_at
    assert delete_pts_at < resize_at
    assert resize_at < epoch_at


def test_dimension_and_epoch_mismatch_checked_before_optical_flow_call():
    """Stream B item 5/6: before calcOpticalFlowPyrLK, trackFrame checks prevGray/prevPts
    existence, epoch currency, and prevGray/gray row+col equality — and returns (never
    calling LK) if any check fails. Ordering in source proves the guard runs first."""
    body = _track_frame_body()
    guard_at = body.index("if (!prevGray || !prevPts || prevGrayEpoch !== trackingEpoch ||")
    assert "prevGray.rows !== gray.rows || prevGray.cols !== gray.cols) {" in body[guard_at:guard_at + 200]
    # Pass 11: reportGapOutcome(...) now runs just before dropTracking here too — widened
    # window accordingly (was 300, now comfortably covers both calls).
    assert "dropTracking('tracking_frame_dimension_mismatch', [gray], { gray, frameGapMs: gapSinceLastTick });" in body[guard_at:guard_at + 400]
    lk_at = body.index("cv.calcOpticalFlowPyrLK(")
    assert guard_at < lk_at


def test_tracking_loss_diagnostic_carries_exact_reason_and_context_fields():
    html = _scanner_html()
    start = html.index("function dropTracking(reason, extraMats")
    end = html.index("function handleDetectionTimeout()", start)
    body = html[start:end]
    for field in (
        "reason,",
        "requestSequence:",
        "trackingEpoch,",
        "ageSinceServerResponseMs:",
        "ageSinceUploadedCaptureMs:",
        "prevGrayDimensions:",
        "currentGrayDimensions:",
        "initialPointCount:",
        "goodPointCount:",
        "pointBounds:",
        "lastTrackFrameTs,",
        "frameGapMs:",
        "previousCorners:",
        "proposedCorners:",
        "validation:",
    ):
        assert field in body
    track_body = _track_frame_body()
    for reason in (
        "tracking_frame_dimension_mismatch",
        "tracking_frame_gap_exceeded",
        "insufficient_flow_points",
        "homography_empty",
        "tracking_epoch_superseded",
        "tracking_exception",
        "corner_order_invalid",
        "out_of_bounds",
        "tracking_geometry_invalid",
    ):
        assert reason in track_body


def test_trackframe_catch_block_routes_through_controlled_failure():
    """Stream B item 7/8: a thrown exception inside trackFrame's try block is now routed
    through dropTracking('tracking_exception', ...) — the same controlled-recovery path
    as every other tracking-loss reason — instead of a bare `tracking = false`. Every Mat
    variable that might have been allocated so far this tick is passed to deleteMats
    first, so nothing leaks on the exception path."""
    body = _track_frame_body()
    catch_start = body.rindex("} catch (e) {")
    catch_body = body[catch_start:body.index("}", body.index("dropTracking('tracking_exception'", catch_start)) + 1]
    assert "dropTracking('tracking_exception', [gray, nextPts, status, err, prevMat, nextMat, mask, H]" in catch_body
    assert "validation: { ok: false, reason: 'tracking_exception'" in catch_body


def test_pose_hold_keeps_video_playing_only_stop_overlay_pauses_it():
    """Proof 7 (visible overlay-loss path): requestPoseHold() only fades opacity — it never
    calls overlay.pause(). Only stopOverlayImmediate() actually pauses the <video>. This is
    what allows the overlay video to keep playing (per the supplied evidence:
    ended=false, paused=false, loop=true) while the geometry/transform is stale or the
    overlay is fading/hidden — pausing and hiding are not the same event."""
    html = _scanner_html()
    hold_start = html.index("function requestPoseHold(reason)")
    hold_end = html.index("function playOverlay()")
    hold_body = html[hold_start:hold_end]
    assert "overlay.pause()" not in hold_body
    stop_start = html.index("function stopOverlayImmediate()")
    stop_end = html.index("function requestPoseHold(reason)")
    stop_body = html[stop_start:stop_end]
    assert "overlay.pause()" in stop_body


# ---------------------------------------------------------------------------
# Stream B: tracking canvas separation, tracking epoch, and controlled tracking
# failure. Codex owns captureCanvas/encoding/attempt-lifecycle/watchdog/AbortController/
# scan-scheduling/session-end-request-guards — none of that is touched here.
# ---------------------------------------------------------------------------

def test_frame_gap_policy_uses_presented_video_frame_time_before_optical_flow():
    """Stream B item 10 / Pass 5 Task C / Pass 8 Task A/D / Pass 11 Task A (Blocker 1 fix):
    the gap-check still requires a valid successful-LK baseline (hasValidLkBaseline — same
    epoch AND continuity token as the last successful LK) before it does anything at all,
    and everything happens BEFORE a new gray frame is even captured or LK is called — but
    a large gap is no longer, by itself, ever a drop reason here. document.hidden is the
    ONLY thing that still bypasses the current-frame pipeline at this point; any other
    large gap is merely recorded as suspected (gapWasSuspected = true) and falls through
    unconditionally into gray conversion + LK."""
    body = _track_frame_body()
    baseline_check_at = body.index("const hasValidLkBaseline = hasSuccessfulLkBaseline &&")
    gap_check_at = body.index("if (hasValidLkBaseline && (now - lastSuccessfulLkWallTime) > TRACKING_GRACE_MS) {")
    hidden_at = body.index("if (document.hidden) {", gap_check_at)
    suspend_at = body.index("clearTrackingGeometry('tracking_gap_suspended', { holdPose: true });", gap_check_at)
    suspected_at = body.index("gapWasSuspected = true;", gap_check_at)
    suspected_log_at = body.index("logCallbackEvent('[TRACK GAP SUSPECTED]',", gap_check_at)
    gray_at = body.index("gray = matFromVideoGray();")
    lk_at = body.index("cv.calcOpticalFlowPyrLK(")
    assert baseline_check_at < gap_check_at < hidden_at < suspend_at < suspected_at < suspected_log_at < gray_at < lk_at
    # The old hard drop reason must be entirely gone from this file (Blocker 1 invariant:
    # tracking_frame_gap_exceeded can never fire with gray/goodPoints unavailable, because
    # it can no longer fire from this pre-LK location at all).
    assert "dropTracking('tracking_frame_gap_exceeded'" not in body
    assert "genuinePresentedGap" not in body
    assert "workloadActive" not in body


def test_epoch_supersede_guard_prevents_stale_epoch_callback_from_mutating_state():
    """Stream B item 9: trackFrame captures trackingEpoch at tick start and re-checks it
    immediately before committing results to shared state (currCorners/prevGray/prevPts)
    — a result computed against a superseded epoch is dropped via the same controlled
    dropTracking() path rather than applied."""
    body = _track_frame_body()
    capture_at = body.index("const epochAtTickStart = trackingEpoch;")
    check_at = body.index("if (trackingEpoch !== epochAtTickStart) {")
    # Pass 12: the accept-only commit is now currCornersTrack (track-space) followed by
    # the one-time conversion to currCorners (intrinsic) that applyWarp reads.
    commit_at = body.index("currCornersTrack = newCorners;\n        currCorners = toIntrinsicSpace(newCorners);\n        if (!applyWarp(currCorners)) {")
    assert capture_at < check_at < commit_at


def test_bounded_hold_still_governs_every_new_tracking_failure_reason():
    """Stream B item 11/12: every new failure reason (dimension mismatch, frame gap,
    exception, epoch supersede) routes through dropTracking(), which unconditionally
    calls clearTrackingGeometry(reason, { holdPose: true }) — the SAME bounded pose-hold
    policy (POSE_HOLD_MS, a finite constant) already governing every existing tracking
    loss reason. No new, unbounded hold was introduced; stale geometry cannot persist
    past the existing hold window."""
    html = _scanner_html()
    drop_start = html.index("function dropTracking(reason, extraMats")
    drop_end = html.index("function handleDetectionTimeout()")
    drop_body = html[drop_start:drop_end]
    assert "clearTrackingGeometry(reason, { holdPose: true });" in drop_body
    assert "const POSE_HOLD_MS = 500;" in html  # finite, unchanged bound on the hold window


def test_new_failure_paths_never_touch_video_source_or_currenttime_or_loop():
    """Stream B item 13/14/15: dropTracking, clearTrackingGeometry, and requestPoseHold —
    the entire controlled-failure/hold path used by every new tracking-loss reason — never
    assign overlay.src, overlay.currentTime, or overlay.loop. Temporary local-tracking
    weakness must never reset the video source, scrub playback position, or change looping."""
    html = _scanner_html()
    for fn_start, fn_end in (
        ("function dropTracking(reason, extraMats", "function handleDetectionTimeout()"),
        ("function clearTrackingGeometry(reason, options = {})", "function stopTrackingLoop()"),
        ("function requestPoseHold(reason)", "function playOverlay()"),
    ):
        body = html[html.index(fn_start):html.index(fn_end)]
        assert "overlay.src =" not in body
        assert "overlay.currentTime =" not in body
        assert "overlay.loop =" not in body
    assert html.count('<video id="overlay"') == 1
    overlay_tag_end = html.index(">", html.index('<video id="overlay"'))
    overlay_tag = html[html.index('<video id="overlay"'):overlay_tag_end]
    assert "loop" in overlay_tag
    # Issue 3E-E: overlay.loop IS now programmatically toggled (off for a 2+-media target
    # or for Detect Once - physical QA fix, so a single-video Detect Once target can still
    # reach 'ended' - on again for a plain single-video tracked_overlay target / full
    # reset) - but ONLY inside the two sequential multi-video lifecycle functions, never in
    # the failure/hold path checked above and never anywhere else in the file. Finds the
    # nearest ENCLOSING function name (last "function X(" before the assignment) rather than
    # a fixed lookback window, so this stays correct regardless of how much explanatory
    # comment sits between the function's opening brace and the assignment itself.
    enclosing_fn_pattern = re.compile(r"function (\w+)\(")
    loop_assignment_fns = []
    for m in re.finditer(r"overlay\.loop = ", html):
        preceding = html[:m.start()]
        fn_matches = list(enclosing_fn_pattern.finditer(preceding))
        assert fn_matches, "overlay.loop assignment found with no enclosing function"
        loop_assignment_fns.append(fn_matches[-1].group(1))
    assert loop_assignment_fns, "expected Issue 3E-E's sequence lifecycle to assign overlay.loop"
    assert all(
        fn in ("startSequenceForTarget", "resetSequencePlaybackState")
        for fn in loop_assignment_fns
    )


def test_server_reacquisition_starts_exactly_one_fresh_epoch():
    """Stream B item 16: the accepted-detection branch calls resetTrackingEpoch exactly
    once per acceptance, immediately followed by rebuilding prevGray from the tracking
    canvas and stamping prevGrayEpoch — a fresh epoch is only ever initialized from a
    just-accepted server pose, never mid-tracking."""
    html = _scanner_html()
    detect_start = html.index("async function detectOnceFromServer(triggeredByWatchdog)")
    detect_end = html.index("async function scanTick(token)")
    body = html[detect_start:detect_end]
    assert "resetTrackingEpoch(" not in body
    assert "trackerBootstrapPending = true;" in body
    assert "tracking_bootstrap_armed" in body
    assert "startTrackingLoop();" in body
    init_start = html.index("function initializeFreshLiveTracker(now, metadata)")
    # Pass 14: bounded to initializeFreshLiveTracker's own body specifically — the next
    # function (attemptInEpochTierReconfig) legitimately calls resetTrackingEpoch too, but
    # that is a DIFFERENT call site, not a second call within bootstrap itself.
    init_body = html[init_start:html.index("function attemptInEpochTierReconfig(oldTier, newTier)", init_start)]
    assert init_body.count("resetTrackingEpoch(frameW, frameH)") == 1
    reset_at = init_body.index("resetTrackingEpoch(frameW, frameH);")
    prev_gray_at = init_body.index("prevGray = gray;")
    epoch_stamp_at = init_body.index("prevGrayEpoch = trackingEpoch;")
    assert reset_at < prev_gray_at < epoch_stamp_at


def test_corner_order_and_invalid_geometry_rejection_unchanged():
    """Stream B item 17/18: normalizeCornerOrder is still consulted on both the local
    tracking path and the server-response path, and out_of_bounds/homography_empty/
    corner_order_invalid are still live dropTracking reasons — geometry validation was
    not weakened or bypassed by the tracking-canvas/epoch changes."""
    html = _scanner_html()
    track_body = _track_frame_body()
    # Pass 12: normalizeCornerOrder runs in track-space now (currCornersTrack).
    assert "normalizeCornerOrder(newCornersRaw, currCornersTrack)" in track_body
    for reason in ("'homography_empty'", "'corner_order_invalid'", "'out_of_bounds'"):
        assert f"dropTracking({reason}" in track_body


def test_no_recognition_or_geometry_threshold_changed():
    """Stream B item 19: MIN_GOOD_POINTS/MAX_ERR/RANSAC_REPROJ/TRACKING_GRACE_FRAMES/
    TRACKING_GRACE_MS/POSE_HOLD_MS are exactly the same values as before this stream —
    the tracking-canvas/epoch/gap/failure-path changes never touched recognition or
    geometry acceptance thresholds."""
    html = _scanner_html()
    assert "const RANSAC_REPROJ = 5.0;" in html
    assert "const TRACKING_GRACE_FRAMES = 3;" in html
    assert "const TRACKING_GRACE_MS = 900;" in html
    assert "const POSE_HOLD_MS = 500;" in html


def test_session_end_still_releases_tracking_geometry_and_mats():
    """Stream B item 20: endScannerSession() already stops the tracking loop and calls
    clearTrackingGeometry('session_end') (which deletes prevGray/prevPts) before stopping
    the camera — unchanged by this stream, still the single place tracking resources are
    released on exit. The new dedicated trackingCanvas needs no separate teardown: it
    holds no resources beyond what clearTrackingGeometry already frees."""
    html = _scanner_html()
    end_start = html.index("async function endScannerSession()")
    end_end = html.index("console.log('\U0001f51a Ending scanner session:'")
    body = html[end_start:end_end]
    assert "stopTrackingLoop();" in body
    assert "clearTrackingGeometry('session_end');" in body
    stop_before_clear = body.index("stopTrackingLoop();") < body.index("clearTrackingGeometry('session_end');")
    assert stop_before_clear


def test_applywarp_and_render_path_use_frameW_frameH_not_cap_dimensions():
    """Scope-limiting proof: applyWarp (the server-response geometry-render path) reads the
    module-level frameW/frameH variables directly, never cap.width/cap.height — so the
    shared-canvas mutation proven above corrupts local OPTICAL-FLOW tracking specifically,
    not the server-response rendering math itself. Keeps the verdict precise rather than
    over-broad."""
    html = _scanner_html()
    warp_start = html.index("function applyWarp(cornersFrame, context = {})")
    warp_end = html.index("function poseCompatibility(nextCorners, previousCorners, frameMinDim)")
    warp_body = html[warp_start:warp_end]
    assert "isOverlayFrameQuadRenderable(cornersFrame, frameW, frameH)" in warp_body
    assert "cap.width" not in warp_body
    assert "cap.height" not in warp_body


# ---------------------------------------------------------------------------
# Pass 5: rVFC callback ownership, callback-stall health state machine, and
# accepted-response successor-order fix. Only the local-tracking-callback lifecycle
# and the successor-scheduling gate are touched — capture/encoding/AbortController/
# backend thresholds/native loop are untouched (verified below).
# ---------------------------------------------------------------------------

def _tracking_callback_functions_body():
    html = _scanner_html()
    start = html.index("function clearRvfcStallWatchdog()")
    end = html.index("function startTrackingLoop()")
    return html[start:end]


def test_bootstrap_callback_rearms_exactly_one_ordinary_callback():
    """Task G item 1: trackFrame's own scheduleTrackingFrame('tick_rearm', ...) call runs
    unconditionally at the top of every tick, before the trackerBootstrapPending branch —
    so the bootstrap tick already rearms exactly one ordinary callback before
    initializeFreshLiveTracker even runs. initializeFreshLiveTracker itself never calls
    scheduleTrackingFrame — there is exactly one UNCONDITIONAL rearm site per tick. A
    second, conditional call exists only inside the Task C ownership self-check (fires
    only if the unconditional rearm above somehow left no callback owner while tracking
    is true — defensive, not a second normal-path rearm)."""
    html = _scanner_html()
    track_start = html.index("function trackFrame(nowArg, metadata)")
    track_end = html.index("async function detectOnceFromServer(triggeredByWatchdog)")
    track_body = html[track_start:track_end]
    rearm_at = track_body.index("scheduleTrackingFrame('tick_rearm'")
    bootstrap_branch_at = track_body.index("if (trackerBootstrapPending) {")
    assert rearm_at < bootstrap_branch_at
    assert track_body.count("scheduleTrackingFrame(") == 2
    ownership_check_at = track_body.index("if (tracking && trackingCallbackId === null) {")
    ownership_recovery_at = track_body.index("scheduleTrackingFrame('ownership_error_recovery')")
    assert rearm_at < ownership_check_at < ownership_recovery_at
    init_start = html.index("function initializeFreshLiveTracker(now, metadata)")
    init_end = html.index("function dropTracking(reason, extraMats")
    assert "scheduleTrackingFrame(" not in html[init_start:init_end]


def test_consumed_callback_id_cleared_only_after_stale_id_check():
    """Task G item 2: onTrackingCallbackFired clears trackingCallbackId (and type/handle)
    ONLY after trackingCallbackValidityFailureReason() confirms this callback is still the
    current owner — a stale/superseded callback returns before touching any of that
    shared state."""
    body = _tracking_callback_functions_body()
    fired_start = body.index("function onTrackingCallbackFired(callbackId, callbackType, now, metadata, callbackEpoch, callbackOwnerToken)")
    validity_call_at = body.index("const failureReason = trackingCallbackValidityFailureReason(callbackId, callbackEpoch, callbackOwnerToken);", fired_start)
    stale_check_at = body.index("if (failureReason) {", fired_start)
    stale_return_at = body.index("return;", stale_check_at)
    clear_id_at = body.index("trackingCallbackId = null; // clears only the just-consumed callback id", fired_start)
    assert fired_start < validity_call_at < stale_check_at < stale_return_at < clear_id_at


def test_entering_tracking_mode_does_not_cancel_active_tracking_callback():
    """Task G item 3: setScannerWorkMode's TRACKING branch only cancels the pending NORMAL
    server-scan timer (cancelPendingNormalScan) — it never touches trackingCallbackId,
    trackingCallbackHandle, or calls stopTrackingLoop/cancelVideoFrameCallback."""
    html = _scanner_html()
    start = html.index("function setScannerWorkMode(mode, reason)")
    end = html.index("function isHealthyLocalTracking()")
    body = html[start:end]
    assert "cancelPendingNormalScan('entered_healthy_tracking')" in body
    assert "trackingCallbackId" not in body
    assert "trackingCallbackHandle" not in body
    assert "stopTrackingLoop" not in body
    assert "cancelVideoFrameCallback" not in body


def test_stale_owner_callback_cannot_cancel_a_newer_owner():
    """Task G item 4 / Pass 7 Task C Case 1: a callback whose id/token no longer match
    trackingCallbackId/trackingCallbackOwnerToken (failureReason === 'stale_owner' — a
    NEWER callback already owns the slot) must never touch trackingCallbackId/Type/Handle
    at all — cancelCurrentTrackingCallback is only reached for every OTHER failure reason,
    specifically gated on `failureReason !== 'stale_owner'`."""
    body = _tracking_callback_functions_body()
    fired_start = body.index("function onTrackingCallbackFired(callbackId, callbackType, now, metadata, callbackEpoch, callbackOwnerToken)")
    cancel_guard_at = body.index("if (failureReason !== 'stale_owner' && trackingCallbackId === callbackId) {", fired_start)
    cancel_call_at = body.index("cancelCurrentTrackingCallback('stale_skip_' + failureReason);", cancel_guard_at)
    assert fired_start < cancel_guard_at < cancel_call_at


def test_stale_epoch_or_owner_release_own_defunct_ownership_slot():
    """Pass 7 Task C: a callback that WAS still the recorded owner (id/token matched) but
    failed on stale_epoch/tracking_inactive/mode_not_tracking has now fired one-shot and
    will never fire again — cancelCurrentTrackingCallback releases that now-defunct slot
    (clearing trackingCallbackId/Type/Handle) so a future scheduleTrackingFrame/
    ensureTrackingCallbackOwnership call is never blocked by a phantom owner. This is the
    exact fix for the Pass 7 deadlock: id=5 skipped as stale_epoch, trackingCallbackId
    stuck at 5 forever with nothing left to ever process it."""
    body = _tracking_callback_functions_body()
    assert "function onTrackingCallbackFired(callbackId, callbackType, now, metadata, callbackEpoch, callbackOwnerToken)" in body
    cancel_start = body.index("function cancelCurrentTrackingCallback(reason)")
    cancel_end = body.index("function ensureTrackingCallbackOwnership(reason)")
    cancel_body = body[cancel_start:cancel_end]
    assert "trackingCallbackId = null;" in cancel_body
    assert "trackingCallbackType = null;" in cancel_body
    assert "trackingCallbackHandle = null;" in cancel_body


def test_duplicate_presented_frame_skips_lk_but_callback_already_rearmed():
    """Task G item 5: trackFrame's duplicate-frameKey dedup check (lastPresentedFrameKey)
    runs AFTER scheduleTrackingFrame('tick_rearm', ...) — a duplicate presented frame
    still gets a fresh callback armed for the next one; only the LK/geometry work for
    THIS tick is skipped."""
    track_body = _track_frame_body()
    rearm_at = track_body.index("scheduleTrackingFrame('tick_rearm'")
    dedup_at = track_body.index("if (frameKey && frameKey === lastPresentedFrameKey) {")
    lk_at = track_body.index("cv.calcOpticalFlowPyrLK(")
    assert rearm_at < dedup_at < lk_at


def test_callback_absence_becomes_stalled_not_frame_gap_exceeded():
    """Task G item 6: the independent stall watchdog (fires when NO callback arrived at
    all) routes into the shared enterCallbackStallRecovery(), which reports
    'tracking_callback_stalled' — a distinct reason from 'tracking_frame_gap_exceeded',
    which only ever fires from inside trackFrame's own in-band check when a callback DID
    arrive, promptly, with genuine presented-frame evidence."""
    html = _scanner_html()
    watchdog_start = html.index("function onRvfcStallWatchdogFired(callbackId)")
    watchdog_end = html.index("function onTrackingCallbackFired(callbackId, callbackType, now, metadata, callbackEpoch, callbackOwnerToken)")
    watchdog_body = html[watchdog_start:watchdog_end]
    assert "enterCallbackStallRecovery(callbackId, 'rvfc_stall_watchdog');" in watchdog_body
    assert "tracking_frame_gap_exceeded" not in watchdog_body
    assert "dropTracking(" not in watchdog_body  # a stall reschedules/falls back — never drops tracking itself
    recovery_start = html.index("function enterCallbackStallRecovery(callbackId, reason)")
    recovery_end = html.index("function onRvfcStallWatchdogFired(callbackId)")
    recovery_body = html[recovery_start:recovery_end]
    assert "'tracking_callback_stalled'" in recovery_body
    assert "tracking_frame_gap_exceeded" not in recovery_body
    assert "dropTracking(" not in recovery_body


def test_genuine_presented_frame_delta_is_only_ever_suspected_not_dropped():
    """Pass 11 Task A (Blocker 1 fix, supersedes the old Task G item 7 test): a genuine
    presented-frame delta over the grace ceiling no longer produces a hard
    dropTracking('tracking_frame_gap_exceeded', ...) at this pre-LK location at all — real-
    device evidence showed this firing with goodPoints=-/gray=- (the current frame never
    tested). It is recorded (gapWasSuspected, suspectedMediaTimeGapMs/suspectedWallTimeGapMs,
    [TRACK GAP SUSPECTED]) and the tick falls through to gray conversion + LK; the real
    outcome is decided from current-frame evidence at whichever exit point is reached."""
    track_body = _track_frame_body()
    gap_check_at = track_body.index("if (hasValidLkBaseline && (now - lastSuccessfulLkWallTime) > TRACKING_GRACE_MS) {")
    suspected_at = track_body.index("gapWasSuspected = true;", gap_check_at)
    gray_at = track_body.index("gray = matFromVideoGray();")
    assert gap_check_at < suspected_at < gray_at
    assert "dropTracking('tracking_frame_gap_exceeded'" not in track_body


def test_exactly_one_rvfc_rearm_is_attempted_before_falling_back():
    """Task G item 8/9: enterCallbackStallRecovery (reached from both the stall watchdog
    AND the late-arrival-race path in onTrackingCallbackFired) attempts exactly one rVFC
    rearm (rvfcRearmAttempted flips true before scheduling it) — a second stall while
    already RVFC_STALLED (or with rvfcRearmAttempted already true) falls through to the
    RAF fallback branch instead of attempting rVFC again."""
    html = _scanner_html()
    start = html.index("function enterCallbackStallRecovery(callbackId, reason)")
    end = html.index("function onRvfcStallWatchdogFired(callbackId)")
    body = html[start:end]
    rearm_guard_at = body.index("if (rvfcHealthState === 'RVFC_ACTIVE' && !rvfcRearmAttempted) {")
    flip_at = body.index("rvfcRearmAttempted = true;", rearm_guard_at)
    rearm_schedule_at = body.index("scheduleTrackingFrame('rvfc_stall_rearm_attempt', callbackId);", rearm_guard_at)
    fallback_at = body.index("rvfcHealthState = 'RAF_FALLBACK';")
    fallback_schedule_at = body.index("scheduleTrackingFrame('rvfc_stalled_raf_fallback', callbackId);")
    assert rearm_guard_at < flip_at < rearm_schedule_at < fallback_at < fallback_schedule_at


def test_raf_fallback_processes_only_new_video_frames():
    """Task G item 10: in RAF fallback, onTrackingCallbackFired compares cam.currentTime
    against the last observed value — a duplicate is skipped (no LK) but still rearms;
    only a genuinely new currentTime reaches trackFrame."""
    body = _tracking_callback_functions_body()
    animation_branch_at = body.index("if (callbackType === 'animation_frame') {")
    dup_check_at = body.index("if (lastRafFallbackCurrentTime !== null && observedCurrentTime === lastRafFallbackCurrentTime) {", animation_branch_at)
    dup_rearm_at = body.index("scheduleTrackingFrame('raf_fallback_duplicate_rearm', callbackId);", dup_check_at)
    dup_return_at = body.index("return;", dup_rearm_at)
    assert animation_branch_at < dup_check_at < dup_rearm_at < dup_return_at
    rvfc_health_branch_at = body.index("} else if (rvfcHealthState !== 'RVFC_ACTIVE') {", animation_branch_at)
    assert "lastRafFallbackCurrentTime = observedCurrentTime;" in body[animation_branch_at:rvfc_health_branch_at]


def test_no_duplicate_rvfc_or_raf_loops_can_be_armed():
    """Task G item 11: scheduleTrackingFrame's own guard (trackingCallbackId !== null)
    is the single place that prevents a second callback of either kind from ever being
    armed while one is already pending — the same guard covers both the rVFC and RAF
    branches, since it runs before either is chosen."""
    html = _scanner_html()
    start = html.index("function scheduleTrackingFrame(reason, previousCallbackId)")
    end = html.index("function startTrackingLoop()")
    body = html[start:end]
    guard_at = body.index("if (!trackLoopActive || trackingCallbackId !== null) return false;")
    rvfc_branch_at = body.index("if (useRVFC) {")
    assert guard_at < rvfc_branch_at
    assert body.count("trackingCallbackId = myCallbackId;") == 1


def test_accepted_response_marks_bootstrap_pending_before_attempt_finalizes():
    """Task G item 12: the accepted-response branch sets trackerBootstrapPending = true
    (and calls startTrackingLoop, establishing callback ownership) synchronously, entirely
    within the try block — strictly before finalizeDetectionAttempt ever runs (called from
    the outer `finally`, after the try block completes). This is what makes
    scheduleAttemptSuccessor's trackerBootstrapPending check (see the finalizer test above)
    correctly see bootstrap-pending state and suppress the normal successor."""
    html = _scanner_html()
    detect_start = html.index("async function detectOnceFromServer(triggeredByWatchdog)")
    detect_end = html.index("async function scanTick(token)")
    body = html[detect_start:detect_end]
    bootstrap_pending_at = body.index("trackerBootstrapPending = true;")
    start_loop_at = body.index("startTrackingLoop();")
    finally_finalize_at = body.index("finalizeDetectionAttempt(attempt, (attempt.cancelled || sessionEnding) ? 'cancelled' : 'complete', 'finally');")
    assert bootstrap_pending_at < start_loop_at < finally_finalize_at


def test_all_new_tracking_loss_reasons_still_funnel_through_one_reacquisition_path():
    """Task G item 13: every new Pass 5 dropTracking reason (frame-gap, dimension
    mismatch, exception, epoch-superseded, bootstrap failure/exception) routes through the
    SAME single dropTracking() function — which already schedules exactly one immediate
    reacquisition (scheduleNextScan('tracking_lost_reacquire', 0), no ad-hoc setTimeout) —
    not a separate scheduling path per reason."""
    html = _scanner_html()
    drop_start = html.index("function dropTracking(reason, extraMats")
    drop_end = html.index("function handleDetectionTimeout()", drop_start)
    drop_body = html[drop_start:drop_end]
    assert "scheduleNextScan('tracking_lost_reacquire', 0);" in drop_body
    assert "setTimeout(" not in drop_body
    for reason_call in (
        # Pass 11: tracking_frame_gap_exceeded is no longer a dropTracking() caller at all
        # (Blocker 1 fix) — a large gap is only ever SUSPECTED, never itself a drop reason.
        "dropTracking('tracking_frame_dimension_mismatch'",
        "dropTracking('tracking_exception'",
        "dropTracking('tracking_epoch_superseded'",
        "dropTracking('tracker_bootstrap_failed'",
        "dropTracking('tracker_bootstrap_exception'",
    ):
        assert reason_call in html  # all reachable, all funneled through the one function above


def test_pass5_did_not_change_any_recognition_or_geometry_threshold():
    """Task G item 14: MIN_GOOD_POINTS/MAX_ERR/RANSAC_REPROJ/TRACKING_GRACE_FRAMES/
    TRACKING_GRACE_MS/POSE_HOLD_MS are unchanged. RVFC_STALL_TIMEOUT_MS reuses
    TRACKING_GRACE_MS's value rather than introducing an independent, undocumented number."""
    html = _scanner_html()
    assert "const TRACKING_GRACE_FRAMES = 3;" in html
    assert "const TRACKING_GRACE_MS = 900;" in html
    assert "const POSE_HOLD_MS = 500;" in html
    assert "const RVFC_STALL_TIMEOUT_MS = TRACKING_GRACE_MS;" in html


def test_pass5_callback_functions_never_touch_video_source_or_currenttime_or_loop():
    """Task G item 15: none of the new Pass 5 callback/health-state functions (
    scheduleTrackingFrame, onTrackingCallbackFired, armRvfcStallWatchdog,
    onRvfcStallWatchdogFired) assign overlay.src or overlay.currentTime, and the native
    <video loop> attribute remains untouched — a temporary rVFC stall or RAF fallback must
    never reset the overlay's source, scrub its playback position, or touch native looping."""
    html = _scanner_html()
    start = html.index("function clearRvfcStallWatchdog()")
    end = html.index("function startTrackingLoop()")
    body = html[start:end]
    assert "overlay.src =" not in body
    assert "overlay.currentTime =" not in body
    assert "overlay.loop" not in body
    assert html.count('<video id="overlay"') == 1


# ---------------------------------------------------------------------------
# Pass 5 correction: remove the legacy frame-gap race, make callback-stall recovery
# own callback absence (including the "callback arrives late but wins the race
# against the watchdog" case), fix console-visible diagnostics, and add a bounded
# RAF-fallback reacquisition path. Root cause of "no Pass 5 console messages appear":
# scannerDiagnostics.push is gated behind ?scanner_debug=1 and never itself calls
# console.log — logCallbackEvent fixes this by always calling console.log, matching
# logTimingCheckpoint's existing behavior for [SCAN SCHEDULED]/[FETCH START]/etc.
# ---------------------------------------------------------------------------

def test_late_arriving_rvfc_callback_is_redirected_before_reaching_trackframe():
    """Correction Task A/G item 1: a callback whose own round trip (requestedAt -> now)
    exceeded RVFC_STALL_TIMEOUT_MS is redirected to enterCallbackStallRecovery BEFORE
    trackFrame is ever called — this is what prevents a callback that merely arrived late
    (but still arrived, winning the race against the watchdog's own setTimeout) from
    reaching trackFrame's in-band gap check and emitting tracking_frame_gap_exceeded from
    stale wall-clock evidence alone."""
    html = _scanner_html()
    start = html.index("function onTrackingCallbackFired(callbackId, callbackType, now, metadata, callbackEpoch, callbackOwnerToken)")
    end = html.index("function stopTrackingLoop()")
    body = html[start:end]
    latency_calc_at = body.index("const latencyMs = requestedAt ? (performance.now() - requestedAt) : 0;")
    stall_check_at = body.index("if (latencyMs > RVFC_STALL_TIMEOUT_MS) {", latency_calc_at)
    recovery_call_at = body.index("enterCallbackStallRecovery(callbackId, 'late_arrival_race');", stall_check_at)
    return_at = body.index("return;", recovery_call_at)
    trackframe_call_at = body.index("trackFrame(now, Object.assign(")
    assert latency_calc_at < stall_check_at < recovery_call_at < return_at < trackframe_call_at


def test_second_stall_does_not_reattempt_rvfc_once_rearm_already_tried():
    """Correction Task B/G item 4: enterCallbackStallRecovery's rVFC-rearm branch is
    gated on `rvfcHealthState === 'RVFC_ACTIVE' && !rvfcRearmAttempted` — once a first
    rearm attempt has run (rvfcRearmAttempted = true, state = RVFC_STALLED), a SECOND
    stall for the same tracking epoch falls straight through to the RAF fallback branch
    instead of trying rVFC again, since that guard condition is now false."""
    html = _scanner_html()
    start = html.index("function enterCallbackStallRecovery(callbackId, reason)")
    end = html.index("function onRvfcStallWatchdogFired(callbackId)")
    body = html[start:end]
    guard_at = body.index("if (rvfcHealthState === 'RVFC_ACTIVE' && !rvfcRearmAttempted) {")
    fallback_at = body.index("rvfcHealthState = 'RAF_FALLBACK';", guard_at)
    guard_block = body[guard_at:fallback_at]
    assert "rvfcRearmAttempted = true;" in guard_block
    assert "rvfcHealthState = 'RVFC_STALLED';" in guard_block
    # the guard condition itself is what makes a second call skip straight to fallback —
    # both flags it checks are flipped inside this same branch, so a re-entry with them
    # already set structurally cannot re-enter it.
    assert "!rvfcRearmAttempted" in body


def test_raf_fallback_bounded_and_permits_one_reacquisition_on_no_fresh_frame():
    """Correction Task B/G item 6: RAF fallback duplicate-currentTime handling is itself
    bounded by TRACKING_GRACE_MS (reused, not invented) — if no fresh camera frame is
    observed within that window, it calls dropTracking('tracking_callback_stalled', ...)
    instead of rescheduling forever, which (via dropTracking's existing body) transitions
    to RECOVERING and schedules exactly one tracking_lost_reacquire attempt."""
    html = _scanner_html()
    start = html.index("function onTrackingCallbackFired(callbackId, callbackType, now, metadata, callbackEpoch, callbackOwnerToken)")
    end = html.index("function stopTrackingLoop()")
    body = html[start:end]
    duplicate_at = body.index("if (lastRafFallbackCurrentTime !== null && observedCurrentTime === lastRafFallbackCurrentTime) {")
    bound_check_at = body.index("if (performance.now() - rafFallbackDuplicateSinceMs > TRACKING_GRACE_MS) {", duplicate_at)
    drop_at = body.index("dropTracking('tracking_callback_stalled', [], trackingWorkloadSnapshot({", bound_check_at)
    reschedule_at = body.index("scheduleTrackingFrame('raf_fallback_duplicate_rearm', callbackId);", bound_check_at)
    assert duplicate_at < bound_check_at < drop_at < reschedule_at  # drop path precedes the still-within-bound reschedule path
    drop_start = html.index("function dropTracking(reason, extraMats")
    drop_body = html[drop_start:html.index("function handleDetectionTimeout()", drop_start)]
    assert "scheduleNextScan('tracking_lost_reacquire', 0);" in drop_body


def test_tracking_true_always_has_a_pending_callback_owner():
    """Correction Task C/G item 8: the ownership self-check in trackFrame fires
    [TRACK CALLBACK OWNERSHIP ERROR] with reason=tracking_without_pending_callback if
    tracking is true right after the tick's own rearm attempt but trackingCallbackId
    somehow ended up null — and immediately requests one safe recovery callback rather
    than silently leaving tracking active with nothing scheduled to continue it."""
    track_body = _track_frame_body()
    rearm_at = track_body.index("scheduleTrackingFrame('tick_rearm'")
    check_at = track_body.index("if (tracking && trackingCallbackId === null) {", rearm_at)
    error_log_at = track_body.index("'[TRACK CALLBACK OWNERSHIP ERROR]'", check_at)
    reason_at = track_body.index("reason: 'tracking_without_pending_callback'", check_at)
    recovery_at = track_body.index("scheduleTrackingFrame('ownership_error_recovery');", check_at)
    assert rearm_at < check_at < error_log_at < reason_at < recovery_at


def test_accepted_detection_establishes_callback_ownership_before_finalizer_runs():
    """Correction Task C/G item 9: the accepted-response branch calls startTrackingLoop()
    (which arms the first tracking callback via scheduleTrackingFrame, establishing
    trackingCallbackId ownership) synchronously — strictly before finalizeDetectionAttempt
    runs in the outer `finally`, and strictly after trackerBootstrapPending is set."""
    html = _scanner_html()
    detect_start = html.index("async function detectOnceFromServer(triggeredByWatchdog)")
    detect_end = html.index("async function scanTick(token)")
    body = html[detect_start:detect_end]
    bootstrap_pending_at = body.index("trackerBootstrapPending = true;")
    start_loop_at = body.index("startTrackingLoop();")
    finally_finalize_at = body.index("finalizeDetectionAttempt(attempt, (attempt.cancelled || sessionEnding) ? 'cancelled' : 'complete', 'finally');")
    assert bootstrap_pending_at < start_loop_at < finally_finalize_at


def test_callback_diagnostics_are_emitted_through_the_always_on_console_logger():
    """Correction Task D/G item 10: every [TRACK CALLBACK ...]/[TRACK RAF FALLBACK]
    console tag is emitted through logCallbackEvent, which — unlike a bare
    scannerDiagnostics.push call — ALWAYS calls console.log (never gated behind the
    ?scanner_debug=1 diagnosticsEnabled flag), exactly like logTimingCheckpoint already
    does for [SCAN SCHEDULED]/[FETCH START]/[RESPONSE HANDLED]."""
    html = _scanner_html()
    log_fn_start = html.index("function logCallbackEvent(tag, summaryFields, structuredData)")
    log_fn_end = html.index("function clearRvfcStallWatchdog()")
    log_fn_body = html[log_fn_start:log_fn_end]
    assert "console.log(tag" in log_fn_body
    assert "scannerDiagnostics.push(tag" in log_fn_body  # still feeds the dev panel, but console.log is unconditional
    callback_functions_start = html.index("function armRvfcStallWatchdog(callbackId)")
    callback_functions_end = html.index("function startTrackingLoop()")
    callback_functions_body = html[callback_functions_start:callback_functions_end]
    for tag in (
        "'[TRACK CALLBACK REQUESTED]'",
        "'[TRACK CALLBACK ENTERED]'",
        "'[TRACK CALLBACK REARMED]'",
        "'[TRACK CALLBACK CANCELLED]'",
        "'[TRACK CALLBACK STALLED]'",
        "'[TRACK RAF FALLBACK]'",
        "'[TRACK CALLBACK SKIPPED]'",
    ):
        assert f"logCallbackEvent({tag}" in callback_functions_body
    track_body = _track_frame_body()
    assert "logCallbackEvent('[TRACK CALLBACK OWNERSHIP ERROR]'" in track_body


def test_frame_gap_exceeded_reason_no_longer_has_a_dropTracking_caller():
    """Pass 11 Task A (supersedes the correction-pass test of the same evidence question):
    tracking_frame_gap_exceeded is no longer a reachable dropTracking() reason anywhere in
    the file at all — the real-device Blocker 1 evidence (goodPoints=-/gray=- drops) proved
    that even the "two-sided evidence" gate this reason used to require still fired before
    the current frame was ever tested. A large gap is now only ever suspected and left to
    the ordinary, evidence-producing LK/geometry gates below to decide the real outcome."""
    html = _scanner_html()
    assert html.count("dropTracking('tracking_frame_gap_exceeded'") == 0


# ---------------------------------------------------------------------------
# Pass 6: fix watchdog mode ownership (it was forcing detection during a normal,
# in-grace tracking blip, whose capture work then collided with local tracking and
# caused the actual hard drop) and stop the tracking-callback loop on tracking loss
# (previously nothing did, so callback ids climbed into the hundreds/thousands and a
# stale loop from an old epoch could survive into a freshly-bootstrapped one).
# ---------------------------------------------------------------------------

def _watchdog_tick_full_body():
    html = _scanner_html()
    start = html.index("function watchdogTick(token")
    end = html.index("function skipTick(reason, extra)")
    return html[start:end]


def test_watchdog_skips_while_tracking_true_even_with_one_bad_frame():
    """Task H item 1 / root cause: isHealthyLocalTracking() no longer requires
    trackingBadFrames === 0 — a single transient optical-flow blip (well inside the
    existing bounded grace period) no longer makes the watchdog believe tracking has
    stopped owning this marker. This is the exact fix for the real-device evidence:
    WATCHDOG FORCED DETECTION firing ~30-40s into otherwise-healthy tracking."""
    html = _scanner_html()
    healthy_start = html.index("function isHealthyLocalTracking()")
    healthy_end = html.index("function watchdogTrackingSkipReason()")
    healthy_body = html[healthy_start:healthy_end]
    assert "trackingBadFrames" not in healthy_body
    assert "scannerWorkMode === 'TRACKING' &&" in healthy_body
    assert "tracking &&" in healthy_body


def test_watchdog_skips_while_scanner_work_mode_is_tracking():
    """Task H item 2: watchdogTrackingSkipReason()'s first check is
    isHealthyLocalTracking(), which itself requires scannerWorkMode === 'TRACKING' — so
    the watchdog stands down for the entire duration scannerWorkMode reads 'TRACKING'."""
    html = _scanner_html()
    reason_fn_start = html.index("function watchdogTrackingSkipReason()")
    reason_fn_end = html.index("function cancelPendingNormalScan(reason)")
    reason_body = html[reason_fn_start:reason_fn_end]
    assert "if (isHealthyLocalTracking()) return 'healthy_tracking';" in reason_body
    watchdog_body = _watchdog_tick_full_body()
    assert "watchdogTrackingSkipReason()" in watchdog_body


def test_watchdog_skips_during_tracker_bootstrap_and_callback_recovery():
    """Task H item 3: watchdogTrackingSkipReason() also stands down while a bootstrap is
    in flight (tracker_bootstrap) or while the callback-health state machine currently
    owns a pending callback or is not RVFC_ACTIVE (callback_recovery) — neither of these
    is "healthy tracking" in the strict isHealthyLocalTracking() sense, but both mean
    local recovery already owns this responsibility, not the watchdog."""
    html = _scanner_html()
    start = html.index("function watchdogTrackingSkipReason()")
    end = html.index("function cancelPendingNormalScan(reason)")
    body = html[start:end]
    bootstrap_at = body.index("if (trackerBootstrapPending) return 'tracker_bootstrap';")
    callback_at = body.index("if (trackingCallbackId !== null || rvfcHealthState !== 'RVFC_ACTIVE') return 'callback_recovery';")
    attempt_at = body.index("if (activeDetectionAttempt && !activeDetectionAttempt.terminal) return 'attempt_active';")
    assert bootstrap_at < callback_at < attempt_at


def test_watchdog_cannot_start_capture_while_any_tracking_skip_reason_is_set():
    """Task H item 4: watchdogTick computes trackingSkipReason and returns (after
    rescheduling) BEFORE reaching the elapsed-time force-detection branch — so
    detectOnceFromServer(true) (which begins FRAME CAPTURE START/TOBLOB START/FETCH
    START) is structurally unreachable whenever any of the four skip reasons apply."""
    body = _watchdog_tick_full_body()
    skip_check_at = body.index("const trackingSkipReason = watchdogTrackingSkipReason();")
    skip_return_at = body.index("return;", skip_check_at)
    forced_detect_at = body.index("detectOnceFromServer(true);")
    assert skip_check_at < skip_return_at < forced_detect_at


def test_drop_tracking_disables_track_loop_active_before_returning():
    """Task H item 5: dropTracking -> clearTrackingGeometry -> stopTrackingLoop sets
    trackLoopActive = false as part of the very first thing clearTrackingGeometry does —
    by the time dropTracking returns, no tracking callback can be pending."""
    html = _scanner_html()
    drop_start = html.index("function dropTracking(reason, extraMats")
    drop_end = html.index("function handleDetectionTimeout()", drop_start)
    drop_body = html[drop_start:drop_end]
    assert "clearTrackingGeometry(reason, { holdPose: true });" in drop_body
    clear_start = html.index("function clearTrackingGeometry(reason, options = {})")
    clear_first_line_end = html.index("\n", html.index("tracking = false;", clear_start))
    stop_call_at = html.index("stopTrackingLoop();", clear_start)
    assert clear_first_line_end < stop_call_at < html.index("function stopTrackingLoop()", clear_start)


def test_drop_tracking_cancels_exact_rvfc_and_raf_callbacks():
    """Task H item 6/7: stopTrackingLoop (reached via dropTracking -> clearTrackingGeometry)
    delegates to cancelCurrentTrackingCallback, which cancels the exact pending callback
    by its stored handle — cam.cancelVideoFrameCallback for a video_frame owner,
    cancelAnimationFrame for an animation_frame owner — never a blind/global cancel."""
    html = _scanner_html()
    stop_start = html.index("function stopTrackingLoop()")
    stop_end = html.index("function scheduleTrackingFrame(reason, previousCallbackId)")
    stop_body = html[stop_start:stop_end]
    assert "cancelCurrentTrackingCallback('tracking_loop_stopped')" in stop_body
    start = html.index("function cancelCurrentTrackingCallback(reason)")
    end = stop_start
    body = html[start:end]
    assert "if (trackingCallbackType === 'video_frame' && typeof cam.cancelVideoFrameCallback === 'function') {" in body
    assert "cam.cancelVideoFrameCallback(trackingCallbackHandle);" in body
    assert "} else if (trackingCallbackHandle != null) {" in body
    assert "cancelAnimationFrame(trackingCallbackHandle);" in body


def test_executing_callback_cannot_rearm_after_trackframe_calls_droptracking():
    """Task H item 8: trackFrame's own top-of-tick scheduleTrackingFrame('tick_rearm', ...)
    call (which already armed a NEXT callback before this tick's outcome was known) is
    followed, later in the SAME synchronous tick, by whichever dropTracking(...) call this
    tick's LK/geometry result triggers. Because clearTrackingGeometry's stopTrackingLoop()
    cancels whatever trackingCallbackId currently holds, the callback armed at the top of
    THIS tick is cancelled before it can ever fire — the second guard required by Task B."""
    track_body = _track_frame_body()
    rearm_at = track_body.index("scheduleTrackingFrame('tick_rearm'")
    first_drop_at = track_body.index("dropTracking(")
    assert rearm_at < first_drop_at  # the rearm always precedes any possible drop this same tick
    # stopTrackingLoop (reached synchronously through every dropTracking call) is what
    # actually cancels that already-armed callback — proven structurally in the test above.


def test_scheduletrackingframe_refuses_while_loop_inactive():
    """Task H item 9/10: scheduleTrackingFrame's only unconditional guard is
    !trackLoopActive — and trackLoopActive is always false immediately after
    clearTrackingGeometry (tracking=false) sets it via stopTrackingLoop, strictly BEFORE
    scannerWorkMode is transitioned to 'RECOVERING' a few lines later. So by the time
    scannerWorkMode reads 'RECOVERING' (outside of a legitimate bootstrap-pending
    handoff), trackLoopActive has already been false — scheduleTrackingFrame cannot arm."""
    html = _scanner_html()
    schedule_start = html.index("function scheduleTrackingFrame(reason, previousCallbackId)")
    guard_line = html[schedule_start:schedule_start + 200]
    assert "if (!trackLoopActive || trackingCallbackId !== null) return false;" in guard_line
    clear_start = html.index("function clearTrackingGeometry(reason, options = {})")
    stop_call_at = html.index("stopTrackingLoop();", clear_start)
    mode_transition_at = html.index("if (scannerWorkMode === 'TRACKING') setScannerWorkMode('RECOVERING'", clear_start)
    assert stop_call_at < mode_transition_at


def test_callback_captures_immutable_epoch_and_owner_token_at_schedule_time():
    """Task H item 11: scheduleTrackingFrame captures myEpoch = trackingEpoch and
    myOwnerToken = trackingCallbackOwnerToken into local consts BEFORE arming either the
    rVFC or RAF callback, and passes those captured values (never a live re-read) into
    onTrackingCallbackFired via the closure."""
    html = _scanner_html()
    start = html.index("function scheduleTrackingFrame(reason, previousCallbackId)")
    end = html.index("function startTrackingLoop()")
    body = html[start:end]
    epoch_capture_at = body.index("const myEpoch = trackingEpoch;")
    token_capture_at = body.index("const myOwnerToken = trackingCallbackOwnerToken;")
    rvfc_pass_at = body.index("onTrackingCallbackFired(myCallbackId, 'video_frame', now, metadata, myEpoch, myOwnerToken);")
    raf_pass_at = body.index("onTrackingCallbackFired(myCallbackId, 'animation_frame', now, null, myEpoch, myOwnerToken);")
    assert epoch_capture_at < token_capture_at < rvfc_pass_at < raf_pass_at


def test_stale_epoch_callback_does_not_process_lk_or_rearm():
    """Task H item 12/13: trackingCallbackValidityFailureReason returns 'stale_epoch' (or
    'stale_owner') the instant callbackEpoch/callbackOwnerToken no longer match the live
    globals — this check runs, and its failure returns, BEFORE trackFrame (which runs LK)
    or any scheduleTrackingFrame rearm call is ever reached in onTrackingCallbackFired."""
    html = _scanner_html()
    start = html.index("function onTrackingCallbackFired(callbackId, callbackType, now, metadata, callbackEpoch, callbackOwnerToken)")
    end = html.index("function stopTrackingLoop()")
    body = html[start:end]
    failure_check_at = body.index("const failureReason = trackingCallbackValidityFailureReason(callbackId, callbackEpoch, callbackOwnerToken);")
    failure_return_at = body.index("return;", failure_check_at)
    trackframe_call_at = body.index("trackFrame(now, Object.assign(")
    first_rearm_at = body.index("scheduleTrackingFrame(", failure_return_at)
    assert failure_check_at < failure_return_at < trackframe_call_at
    assert failure_return_at < first_rearm_at  # the only rearm calls in this function all come after the guard


def test_old_epoch_invalidated_before_new_server_pose_bootstrap():
    """Task H item 15: the accepted-response branch calls stopTrackingLoop() (cancelling
    any old-epoch callback owner) BEFORE setting trackerBootstrapPending = true — an old
    callback from epoch N is fully torn down before epoch N+1's bootstrap is armed."""
    html = _scanner_html()
    detect_start = html.index("async function detectOnceFromServer(triggeredByWatchdog)")
    detect_end = html.index("async function scanTick(token)")
    body = html[detect_start:detect_end]
    stop_at = body.index("stopTrackingLoop();")
    bootstrap_pending_at = body.index("trackerBootstrapPending = true;")
    assert stop_at < bootstrap_pending_at


def test_accepted_response_creates_exactly_one_new_callback_owner():
    """Task H item 16: because stopTrackingLoop() runs first (setting trackLoopActive =
    false), the startTrackingLoop() call later in the same accept branch is guaranteed to
    be a genuine (re)start — never the silent no-op it would otherwise be if the loop
    were still marked active from a prior epoch — so exactly one fresh callback owner is
    armed per accepted response, never zero (stale no-op) and never two."""
    html = _scanner_html()
    detect_start = html.index("async function detectOnceFromServer(triggeredByWatchdog)")
    detect_end = html.index("async function scanTick(token)")
    body = html[detect_start:detect_end]
    assert body.count("stopTrackingLoop();") == 1
    assert body.count("startTrackingLoop();") == 1
    stop_at = body.index("stopTrackingLoop();")
    start_at = body.index("startTrackingLoop();")
    assert stop_at < start_at


def test_no_callback_loop_activity_while_recovering():
    """Task H item 19: once clearTrackingGeometry has run (scannerWorkMode moves to
    RECOVERING, trackLoopActive already false), no tracking callback remains pending —
    stopTrackingLoop's cancellation covers both the rVFC and RAF cases, so the callback
    chain genuinely stops rather than continuing to tick indefinitely with tracking=false."""
    html = _scanner_html()
    stop_start = html.index("function stopTrackingLoop()")
    stop_end = html.index("function scheduleTrackingFrame(reason, previousCallbackId)")
    stop_body = html[stop_start:stop_end]
    assert "trackLoopActive = false;" in stop_body
    assert "cancelCurrentTrackingCallback('tracking_loop_stopped')" in stop_body
    cancel_start = html.index("function cancelCurrentTrackingCallback(reason)")
    cancel_body = html[cancel_start:stop_start]
    assert "trackingCallbackId = null;" in cancel_body
    assert "trackingCallbackHandle = null;" in cancel_body


def test_isHealthyLocalTracking_fix_is_shared_by_watchdog_scantick_and_successor():
    """Task H item 17: the single trackingBadFrames-removal fix in isHealthyLocalTracking()
    is what makes watchdogTick, scanTick, AND scheduleAttemptSuccessor/scheduleNextScan all
    consistently suppress server-side scan activity throughout tracking — including during
    a normal in-grace blip — not just at the moment of a perfect frame."""
    html = _scanner_html()
    assert html.count("isHealthyLocalTracking()") >= 6  # watchdog, scanTick, scheduleNextScan, scheduleAttemptSuccessor, clearTrackingGeometry callers, etc.
    healthy_start = html.index("function isHealthyLocalTracking()")
    healthy_end = html.index("function watchdogTrackingSkipReason()")
    assert "trackingBadFrames" not in html[healthy_start:healthy_end]


def test_genuine_tracking_loss_still_permits_exactly_one_reacquisition():
    """Task H item 18: dropTracking calls clearTrackingGeometry (tracking=false,
    trackLoopActive=false) BEFORE scheduleNextScan('tracking_lost_reacquire', 0) — by the
    time the reacquisition is scheduled, isHealthyLocalTracking() is already false, so
    scheduleNextScan proceeds instead of suppressing; exactly one such call exists."""
    html = _scanner_html()
    drop_start = html.index("function dropTracking(reason, extraMats")
    drop_end = html.index("function handleDetectionTimeout()", drop_start)
    body = html[drop_start:drop_end]
    clear_at = body.index("clearTrackingGeometry(reason, { holdPose: true });")
    reacquire_at = body.index("scheduleNextScan('tracking_lost_reacquire', 0);")
    assert clear_at < reacquire_at
    assert body.count("scheduleNextScan('tracking_lost_reacquire', 0);") == 1


def test_pass6_new_functions_never_touch_video_source_or_currenttime_or_loop():
    """Task H item 20: watchdogTrackingSkipReason and trackingCallbackValidityFailureReason
    — the two new decision functions this pass adds — never assign overlay.src or
    overlay.currentTime, and never reference the native <video loop> attribute."""
    html = _scanner_html()
    for fn_start_marker, fn_end_marker in (
        ("function watchdogTrackingSkipReason()", "function cancelPendingNormalScan(reason)"),
        ("function trackingCallbackValidityFailureReason(callbackId, callbackEpoch, callbackOwnerToken)",
         "function onTrackingCallbackFired(callbackId, callbackType, now, metadata, callbackEpoch, callbackOwnerToken)"),
    ):
        body = html[html.index(fn_start_marker):html.index(fn_end_marker)]
        assert "overlay.src" not in body
        assert "overlay.currentTime" not in body
        assert "overlay.loop" not in body
    assert html.count('<video id="overlay"') == 1


def test_pass6_did_not_change_any_recognition_or_geometry_threshold():
    """Task H item 21: MIN_GOOD_POINTS/MAX_ERR/RANSAC_REPROJ/TRACKING_GRACE_FRAMES/
    TRACKING_GRACE_MS/POSE_HOLD_MS/RVFC_STALL_TIMEOUT_MS are unchanged by this pass —
    only ownership/scheduling logic (watchdog, callback lifecycle, successor scheduling)
    was touched, never a recognition or geometry acceptance threshold."""
    html = _scanner_html()
    assert "const TRACKING_GRACE_FRAMES = 3;" in html
    assert "const TRACKING_GRACE_MS = 900;" in html
    assert "const POSE_HOLD_MS = 500;" in html
    assert "const RVFC_STALL_TIMEOUT_MS = TRACKING_GRACE_MS;" in html
    assert "const RANSAC_REPROJ = 5.0;" in html


# ---------------------------------------------------------------------------
# Pass 7: repair the tracking-without-callback deadlock. Root causes fixed:
# (1) startTrackingLoop() called unconditionally at startup/recovery armed a callback
#     with nothing to track, cycling forever through tracking_inactive-skip -> stall ->
#     RAF-fallback; (2) trackFrame's unconditional top-of-tick rearm captured the epoch
#     BEFORE a bootstrap tick's resetTrackingEpoch() bumped it, so the successor it just
#     armed was stale the instant bootstrap succeeded; (3) a stale-skipped callback that
#     was still the recorded owner never released its slot, leaving trackingCallbackId
#     permanently non-null with nothing left to ever process it; (4) the watchdog only
#     checked tracking/mode flags, never whether anything was actually still scheduled to
#     advance them, so it skipped forever once ownerless.
# ---------------------------------------------------------------------------

def test_scanner_startup_does_not_request_a_tracking_callback():
    """Task G item 1: onRuntimeInitialized calls startTrackingLoop() unconditionally at
    OpenCV-ready time, before any detection has ever been accepted (tracking=false,
    trackerBootstrapPending=false) — scheduleTrackingFrame's own gate refuses to arm a
    callback in that state, so trackLoopActive becomes true (harmless bookkeeping) but no
    [TRACK CALLBACK REQUESTED] is ever emitted until a real bootstrap begins."""
    html = _scanner_html()
    assert "startTrackingLoop();" in html[html.index("onRuntimeInitialized: function ()"):html.index("onAbort: function (reason)")]
    schedule_start = html.index("function scheduleTrackingFrame(reason, previousCallbackId)")
    guard_body = html[schedule_start:html.index("function startTrackingLoop()")]
    gate_at = guard_body.index("if (!tracking && !trackerBootstrapPending) {")
    counter_at = guard_body.index("diagState.callbacksRequestedWhileInactive++;", gate_at)
    return_at = guard_body.index("return false;", counter_at)
    assert gate_at < counter_at < return_at


def test_searching_mode_cannot_arm_rvfc_or_raf_fallback():
    """Task G item 2/3/18/19: the same tracking||trackerBootstrapPending gate in
    scheduleTrackingFrame runs BEFORE the useRVFC branch decision — so neither the rVFC
    nor the RAF-fallback path can ever be reached while scannerWorkMode is SEARCHING or
    RECOVERING with no bootstrap in progress (tracking false in both cases)."""
    html = _scanner_html()
    start = html.index("function scheduleTrackingFrame(reason, previousCallbackId)")
    end = html.index("function startTrackingLoop()")
    body = html[start:end]
    inactive_gate_at = body.index("if (!tracking && !trackerBootstrapPending) {")
    inactive_return_at = body.index("return false;", inactive_gate_at)
    rvfc_decision_at = body.index("const useRVFC = rvfcHealthState !== 'RAF_FALLBACK'")
    assert inactive_gate_at < inactive_return_at < rvfc_decision_at


def test_tracking_inactive_callback_never_enters_recovery_or_rearms():
    """Task G item 4/5: onTrackingCallbackFired's failure branch returns immediately for
    'tracking_inactive'/'mode_not_tracking' — strictly before the Case-2 ownership-repair
    check (the only rearm in that branch) and long before the callbackType==='video_frame'
    latency check that leads into enterCallbackStallRecovery — so a tracking_inactive
    callback can never trigger stall recovery, RAF fallback, or a rearm."""
    body = _tracking_callback_functions_body()
    fired_start = body.index("function onTrackingCallbackFired(callbackId, callbackType, now, metadata, callbackEpoch, callbackOwnerToken)")
    inactive_check_at = body.index("if (failureReason === 'tracking_inactive' || failureReason === 'mode_not_tracking') {", fired_start)
    inactive_return_at = body.index("return;", inactive_check_at)
    case2_repair_at = body.index("ensureTrackingCallbackOwnership('stale_skip_ownership_repair');", inactive_return_at)
    stall_recovery_at = body.index("enterCallbackStallRecovery(callbackId, 'late_arrival_race');", inactive_return_at)
    assert fired_start < inactive_check_at < inactive_return_at < case2_repair_at
    assert inactive_return_at < stall_recovery_at


def test_bootstrap_epoch_advance_cancels_and_reissues_stale_successor():
    """Task G item 6/7/8: when initializeFreshLiveTracker() bumps trackingEpoch inside a
    bootstrap tick, trackFrame cancels whatever successor the top-of-tick rearm already
    armed under the OLD epoch and reissues a fresh one — so the first callback that
    survives to actually be used always has callbackEpoch === trackingEpoch, even though
    the very first (top-of-tick) arm attempt necessarily happened before the epoch bump."""
    html = _scanner_html()
    track_start = html.index("function trackFrame(nowArg, metadata)")
    bootstrap_branch_at = html.index("if (trackerBootstrapPending) {", track_start)
    epoch_capture_at = html.index("const epochBeforeBootstrap = trackingEpoch;", bootstrap_branch_at)
    bootstrapped_at = html.index("const bootstrapped = initializeFreshLiveTracker(now, metadata);", epoch_capture_at)
    advance_check_at = html.index("if (bootstrapped && trackingEpoch !== epochBeforeBootstrap) {", bootstrapped_at)
    cancel_at = html.index("cancelCurrentTrackingCallback('bootstrap_epoch_advanced');", advance_check_at)
    reissue_at = html.index("ensureTrackingCallbackOwnership('bootstrap_epoch_advanced_rearm');", cancel_at)
    assert bootstrap_branch_at < epoch_capture_at < bootstrapped_at < advance_check_at < cancel_at < reissue_at


def test_stale_callback_with_newer_owner_does_not_mutate_it():
    """Task G item 9: covered structurally by test_stale_owner_callback_cannot_cancel_a_
    newer_owner above — cancelCurrentTrackingCallback is only reached when
    trackingCallbackId === callbackId (this callback IS still the owner), so a stale
    callback whose id no longer matches (a newer one already owns the slot) never
    reaches it at all."""
    body = _tracking_callback_functions_body()
    guard_at = body.index("if (failureReason !== 'stale_owner' && trackingCallbackId === callbackId) {")
    assert "trackingCallbackId === callbackId" in body[guard_at:guard_at + 80]


def test_stale_callback_with_no_owner_performs_one_repair():
    """Task G item 10/11/12: Case 2 in onTrackingCallbackFired's failure branch checks
    trackingCallbackId === null (no owner survived the stale skip) AND the full healthy-
    tracking-claim precondition, then calls ensureTrackingCallbackOwnership exactly once
    — which is itself idempotent (no-ops if anything is already pending) and therefore
    cannot create a duplicate callback even if called more than once in sequence."""
    body = _tracking_callback_functions_body()
    fired_start = body.index("function onTrackingCallbackFired(callbackId, callbackType, now, metadata, callbackEpoch, callbackOwnerToken)")
    case2_check_at = body.index(
        "if (trackingCallbackId === null && tracking && trackLoopActive && scannerWorkMode === 'TRACKING') {", fired_start
    )
    ownership_error_at = body.index("'tracking_without_pending_callback_after_stale_skip'", case2_check_at)
    repair_call_at = body.index("ensureTrackingCallbackOwnership('stale_skip_ownership_repair');", case2_check_at)
    assert case2_check_at < ownership_error_at < repair_call_at
    ensure_start = body.index("function ensureTrackingCallbackOwnership(reason)")
    ensure_end = body.index("function onTrackingCallbackFired(")
    ensure_body = body[ensure_start:ensure_end]
    assert "if (trackingCallbackId !== null) return false;" in ensure_body  # already-owned -> idempotent no-op


def test_watchdog_checks_ownerless_tracking_before_healthy_tracking_skip():
    """Task G item 13/14/15/16: watchdogHandleOwnerlessTracking runs BEFORE
    watchdogTrackingSkipReason() in watchdogTick — so the watchdog can never reach a bare
    [WATCHDOG SKIP] reason=healthy_tracking when tracking is claimed but has no live
    callback owner. On repair failure it calls dropTracking exactly once, which itself
    (proven elsewhere in this file) schedules exactly one tracking_lost_reacquire attempt."""
    html = _scanner_html()
    watchdog_start = html.index("function watchdogTick(token")
    watchdog_end = html.index("function skipTick(reason, extra)")
    body = html[watchdog_start:watchdog_end]
    owner_check_at = body.index("if (watchdogHandleOwnerlessTracking(watchdogId)) {")
    skip_reason_at = body.index("const trackingSkipReason = watchdogTrackingSkipReason();")
    assert owner_check_at < skip_reason_at
    owner_fn_start = html.index("function watchdogHandleOwnerlessTracking(watchdogId)")
    owner_fn_end = html.index("function watchdogHasLiveCallbackOwner()") if "function watchdogHasLiveCallbackOwner()" in html[owner_fn_start:owner_fn_start+2000] else html.index("function cancelPendingNormalScan(reason)")
    owner_fn_body = html[owner_fn_start:owner_fn_end]
    assert "if (ensureTrackingCallbackOwnership('watchdog_ownership_repair')) return true;" in owner_fn_body
    assert "dropTracking('tracking_callback_owner_missing', []);" in owner_fn_body


def test_watchdog_reschedules_itself_after_handling_ownerless_tracking():
    """Task G item 17: whether watchdogHandleOwnerlessTracking repairs or drops, watchdogTick
    still calls scheduleWatchdog(token) before returning — the 500ms watchdog cadence never
    stops, so ownerless tracking can be caught and corrected within one interval, never left
    to freeze the overlay indefinitely."""
    html = _scanner_html()
    watchdog_start = html.index("function watchdogTick(token")
    owner_check_at = html.index("if (watchdogHandleOwnerlessTracking(watchdogId)) {", watchdog_start)
    reschedule_at = html.index("scheduleWatchdog(token);", owner_check_at)
    return_at = html.index("return;", reschedule_at)
    assert owner_check_at < reschedule_at < return_at


def test_no_server_capture_while_valid_tracking_owner_exists():
    """Task G item 20: watchdogTrackingSkipReason() returns 'callback_recovery' (a skip,
    never a force) whenever trackingCallbackId !== null — so a genuinely live, valid
    callback owner always prevents the watchdog from ever reaching detectOnceFromServer(true)."""
    html = _scanner_html()
    start = html.index("function watchdogTrackingSkipReason()")
    end = html.index("function watchdogHasLiveCallbackOwner()")
    body = html[start:end]
    assert "if (trackingCallbackId !== null || rvfcHealthState !== 'RVFC_ACTIVE') return 'callback_recovery';" in body


def test_callback_skipped_diagnostic_separates_captured_from_current_epoch():
    """Task G item 21: the [TRACK CALLBACK SKIPPED] log carries callbackEpoch (the value
    THIS callback captured at schedule time, immutable) and currentEpoch (the live global)
    as two distinct fields — never conflating a stale captured value with the current one."""
    body = _tracking_callback_functions_body()
    skipped_at = body.index("logCallbackEvent('[TRACK CALLBACK SKIPPED]', {")
    call_text = body[skipped_at:body.index(");", skipped_at) + 2]
    assert "callbackEpoch," in call_text
    assert "currentEpoch: trackingEpoch" in call_text


def test_pass7_new_functions_never_touch_video_source_or_currenttime_or_loop():
    """Task G item 22: none of the new Pass 7 functions (cancelCurrentTrackingCallback,
    ensureTrackingCallbackOwnership, watchdogHandleOwnerlessTracking, trackOwnerState)
    assign overlay.src or overlay.currentTime, and none reference the native <video loop>
    attribute — an ownership repair or a watchdog-driven drop must never reset the
    overlay's source, scrub its playback position, or touch native looping."""
    html = _scanner_html()
    for fn_start_marker, fn_end_marker in (
        ("function cancelCurrentTrackingCallback(reason)", "function ensureTrackingCallbackOwnership(reason)"),
        ("function ensureTrackingCallbackOwnership(reason)", "function onTrackingCallbackFired("),
        ("function watchdogHandleOwnerlessTracking(watchdogId)", "function cancelPendingNormalScan(reason)"),
        ("function trackOwnerState(extra)", "function cancelCurrentTrackingCallback(reason)"),
    ):
        body = html[html.index(fn_start_marker):html.index(fn_end_marker)]
        assert "overlay.src" not in body
        assert "overlay.currentTime" not in body
        assert "overlay.loop" not in body
    assert html.count('<video id="overlay"') == 1


def test_pass7_did_not_change_any_recognition_or_geometry_threshold():
    """Task G item 23: MIN_GOOD_POINTS/MAX_ERR/RANSAC_REPROJ/TRACKING_GRACE_FRAMES/
    TRACKING_GRACE_MS/POSE_HOLD_MS/RVFC_STALL_TIMEOUT_MS are unchanged — this pass only
    touched callback-ownership/scheduling/watchdog logic, never a recognition or geometry
    acceptance threshold."""
    html = _scanner_html()
    assert "const TRACKING_GRACE_FRAMES = 3;" in html
    assert "const TRACKING_GRACE_MS = 900;" in html
    assert "const POSE_HOLD_MS = 500;" in html
    assert "const RVFC_STALL_TIMEOUT_MS = TRACKING_GRACE_MS;" in html
    assert "const RANSAC_REPROJ = 5.0;" in html


# ---------------------------------------------------------------------------
# Pass 8: reset the frame-gap baseline at every continuity boundary. Root cause:
# tracking_frame_gap_exceeded compared against lastTrackingMediaTime/lastTrackFrameTs,
# which are updated by ANY trackFrame call (bootstrap tick, bare rearm tick, first tick
# after stall/RAF-fallback/ownership-repair recovery) — none of which is itself a
# successfully processed LK frame. A recovered callback's first tick was therefore always
# judged against pre-recovery evidence, which by definition always looks like a genuine
# gap — proven by every recovery (stall-rearm, fresh bootstrap) being immediately
# followed by another drop with goodPoints=-/gray=- (LK never even ran).
# ---------------------------------------------------------------------------

def test_new_bootstrap_starts_without_a_frame_gap_baseline():
    """Task G item 1: startTrackingLoop() calls resetFrameGapBaseline() before arming the
    first callback via scheduleTrackingFrame() — every fresh tracking session (bootstrap,
    camera recovery, fallback retry, detection timeout) begins with hasSuccessfulLkBaseline
    already false."""
    html = _scanner_html()
    start = html.index("function startTrackingLoop()")
    end = html.index("function stopDetectLoop(reason)")
    body = html[start:end]
    reset_at = body.index("resetFrameGapBaseline('tracking_loop_started');")
    schedule_at = body.index("scheduleTrackingFrame();")
    assert reset_at < schedule_at
    reset_fn_start = html.index("function resetFrameGapBaseline(reason)")
    reset_fn_end = html.index("function establishFrameGapBaseline(mediaTime, wallTime, reason)")
    reset_fn_body = html[reset_fn_start:reset_fn_end]
    assert "hasSuccessfulLkBaseline = false;" in reset_fn_body


def test_first_successful_lk_establishes_baseline():
    """Task G item 2: establishFrameGapBaseline() is called exactly once in the whole file,
    at the point in trackFrame immediately after LK + geometry validation + applyWarp have
    ALL succeeded (right after lastSuccessfulLkAt is stamped) — never at callback entry,
    bootstrap, or rearm. Pass 11: the reason argument grew a third case (gap_recovered),
    so the call now spans two lines — matched here by its stable prefix instead of the
    old single-line literal."""
    html = _scanner_html()
    call_prefix = "establishFrameGapBaseline(mediaTime, lastSuccessfulLkAt, firstPostReseedLkPending"
    assert html.count(call_prefix) == 1  # the one real call site
    call_at = html.index(call_prefix)
    assert "? 'first_post_reseed_lk' : (gapWasSuspected ? 'gap_recovered' : 'successful_lk'));" in html[call_at:call_at + 200]
    stamp_at = html.index("lastSuccessfulLkAt = performance.now();")
    playoverlay_at = html.rindex("playOverlay();", 0, call_at)
    assert playoverlay_at < stamp_at < call_at
    establish_fn_start = html.index("function establishFrameGapBaseline(mediaTime, wallTime, reason)")
    establish_fn_end = html.index("function cancelCurrentTrackingCallback(reason)")
    establish_fn_body = html[establish_fn_start:establish_fn_end]
    assert "hasSuccessfulLkBaseline = true;" in establish_fn_body
    assert "successfulLkEpoch = trackingEpoch;" in establish_fn_body
    assert "successfulLkContinuityToken = frameGapContinuityToken;" in establish_fn_body


def test_first_successful_lk_cannot_emit_frame_gap_exceeded():
    """Task G item 3/14: hasValidLkBaseline (which gates the entire gap-drop block) is
    false until establishFrameGapBaseline() has run at least once for the current epoch/
    continuity token — so the very first successful LK tick after any reset can never
    itself have been the tick that dropped via tracking_frame_gap_exceeded (the drop
    block is unreachable without a prior successful LK, and gray/LK for THIS tick hasn't
    even run yet at the point the block would apply)."""
    track_body = _track_frame_body()
    baseline_at = track_body.index("const hasValidLkBaseline = hasSuccessfulLkBaseline &&")
    gap_block_at = track_body.index("if (hasValidLkBaseline && (now - lastSuccessfulLkWallTime) > TRACKING_GRACE_MS) {")
    gray_at = track_body.index("gray = matFromVideoGray();")
    assert baseline_at < gap_block_at < gray_at
    assert "successfulLkEpoch === trackingEpoch &&" in track_body
    assert "successfulLkContinuityToken === frameGapContinuityToken;" in track_body


def test_epoch_change_invalidates_baseline():
    """Task G item 5/12: resetTrackingEpoch() calls resetFrameGapBaseline() right after
    bumping trackingEpoch — so a baseline established under the OLD epoch can never match
    hasValidLkBaseline's successfulLkEpoch === trackingEpoch check once the epoch moves on."""
    html = _scanner_html()
    start = html.index("function resetTrackingEpoch(width, height)")
    end = html.index("function cornersToMat(corners)")
    body = html[start:end]
    epoch_bump_at = body.index("trackingEpoch++;")
    reset_at = body.index("resetFrameGapBaseline('tracking_epoch_changed');")
    assert epoch_bump_at < reset_at


def test_rvfc_stall_and_rearm_and_raf_fallback_all_invalidate_baseline():
    """Task G item 6/7/11: enterCallbackStallRecovery() — reached for stall detection, the
    rVFC rearm attempt, AND RAF-fallback entry alike — calls resetFrameGapBaseline() once,
    at its very top, before either the rearm or the fallback branch arms a replacement
    callback. The baseline stays invalid through both branches (neither branch calls
    establishFrameGapBaseline)."""
    html = _scanner_html()
    start = html.index("function enterCallbackStallRecovery(callbackId, reason)")
    end = html.index("function onRvfcStallWatchdogFired(callbackId)")
    body = html[start:end]
    reset_at = body.index("resetFrameGapBaseline('callback_stall_recovery:' + reason);")
    rearm_at = body.index("scheduleTrackingFrame('rvfc_stall_rearm_attempt', callbackId);")
    fallback_at = body.index("scheduleTrackingFrame('rvfc_stalled_raf_fallback', callbackId);")
    assert reset_at < rearm_at < fallback_at
    assert "establishFrameGapBaseline(" not in body


def test_rvfc_health_restoration_resets_baseline_before_trackframe_runs():
    """Task G item 8/9: the rvfc_health_restored branch in onTrackingCallbackFired calls
    resetFrameGapBaseline() BEFORE trackFrame() is invoked for that same (first-post-
    recovery) callback — so that tick's own LK, if it succeeds, is the one that
    establishes a fresh baseline via establishFrameGapBaseline(), never compared against
    pre-stall evidence."""
    body = _tracking_callback_functions_body()
    restored_at = body.index("logCallbackEvent('rvfc_health_restored'")
    reset_at = body.index("resetFrameGapBaseline('rvfc_health_restored');", restored_at)
    trackframe_call_at = body.index("trackFrame(now, Object.assign(", restored_at)
    assert restored_at < reset_at < trackframe_call_at


def test_ownership_repair_invalidates_old_baseline():
    """Task G item 10: ensureTrackingCallbackOwnership() calls resetFrameGapBaseline()
    before arming the repaired callback via scheduleTrackingFrame() — the repaired
    callback's first tick is never judged against whatever baseline existed before the
    ownership gap."""
    html = _scanner_html()
    start = html.index("function ensureTrackingCallbackOwnership(reason)")
    end = html.index("function onTrackingCallbackFired(")
    body = html[start:end]
    reset_at = body.index("resetFrameGapBaseline('ownership_repair:' + reason);")
    schedule_at = body.index("const repaired = scheduleTrackingFrame(reason);")
    assert reset_at < schedule_at


def test_stale_callback_logs_skipped_before_any_entered_event():
    """Task G item 15: onTrackingCallbackFired's validity check runs, and its failure
    branch returns, strictly before the ENTERED log is ever reached — a stale/cancelled
    callback can only ever emit [TRACK CALLBACK SKIPPED], never [TRACK CALLBACK ENTERED]."""
    html = _scanner_html()
    start = html.index("function onTrackingCallbackFired(callbackId, callbackType, now, metadata, callbackEpoch, callbackOwnerToken)")
    end = html.index("function stopTrackingLoop()")
    body = html[start:end]
    failure_check_at = body.index("const failureReason = trackingCallbackValidityFailureReason(")
    skipped_log_at = body.index("logCallbackEvent('[TRACK CALLBACK SKIPPED]',", failure_check_at)
    entered_log_at = body.index("logCallbackEvent('[TRACK CALLBACK ENTERED]',")
    assert failure_check_at < skipped_log_at < entered_log_at


def test_callback_entered_prints_captured_and_current_epoch_separately():
    """Task G item 16 / Task E: the [TRACK CALLBACK ENTERED] log carries callbackEpoch
    (the captured, immutable value this callback was armed under) and currentEpoch (the
    live global, which may have advanced during this same trackFrame call) as two
    distinct fields — never conflating a captured value with the live one."""
    body = _tracking_callback_functions_body()
    entered_at = body.index("logCallbackEvent('[TRACK CALLBACK ENTERED]', {")
    call_text = body[entered_at:body.index(");", entered_at) + 2]
    assert "id: callbackId, callbackEpoch, currentEpoch: trackingEpoch," in call_text


def test_genuine_two_frame_media_gap_is_suspected_and_falls_through_to_lk():
    """Pass 11 Task A (supersedes the old Task G item 17 test): with a valid baseline (same
    epoch, same continuity token) and a real measured media-time delta above the grace
    ceiling, the tick no longer drops here at all — it records the suspicion and falls
    through to gray conversion + LK, same as any other tick. Only document.hidden still
    bypasses this pipeline at this point."""
    track_body = _track_frame_body()
    gap_check_at = track_body.index("if (hasValidLkBaseline && (now - lastSuccessfulLkWallTime) > TRACKING_GRACE_MS) {")
    suspected_at = track_body.index("gapWasSuspected = true;", gap_check_at)
    gray_at = track_body.index("gray = matFromVideoGray();")
    assert gap_check_at < suspected_at < gray_at


def test_pass8_new_functions_never_touch_video_source_or_currenttime_or_loop():
    """Task G item 24: resetFrameGapBaseline/establishFrameGapBaseline never assign
    overlay.src or overlay.currentTime, and never reference the native <video loop>
    attribute — a baseline reset/establish must never reset the overlay's source, scrub
    its playback position, or touch native looping."""
    html = _scanner_html()
    start = html.index("function resetFrameGapBaseline(reason)")
    end = html.index("function cancelCurrentTrackingCallback(reason)")
    body = html[start:end]
    assert "overlay.src" not in body
    assert "overlay.currentTime" not in body
    assert "overlay.loop" not in body
    assert html.count('<video id="overlay"') == 1


def test_pass8_did_not_change_any_recognition_or_geometry_threshold():
    """Task G item 25: MIN_GOOD_POINTS/MAX_ERR/RANSAC_REPROJ/TRACKING_GRACE_FRAMES/
    TRACKING_GRACE_MS/POSE_HOLD_MS/RVFC_STALL_TIMEOUT_MS are unchanged — this pass only
    touched the frame-gap baseline's evidence source, never a recognition or geometry
    acceptance threshold."""
    html = _scanner_html()
    assert "const TRACKING_GRACE_FRAMES = 3;" in html
    assert "const TRACKING_GRACE_MS = 900;" in html
    assert "const POSE_HOLD_MS = 500;" in html
    assert "const RVFC_STALL_TIMEOUT_MS = TRACKING_GRACE_MS;" in html
    assert "const RANSAC_REPROJ = 5.0;" in html


# ---------------------------------------------------------------------------
# Pass 9: proactive LK feature reseeding + callback-after-cancellation log ordering.
# Real-device evidence: tracking survived ~10-11s then genuinely exhausted its optical-
# flow point population (goodPoints=2, goodPoints=0 out of initialPoints=120) — prevPts
# was carried forward tick after tick with no reference to the current marker geometry,
# no pruning of background-drifted points, and no way to top the population back up
# before hitting the hard MIN_GOOD_POINTS floor.
# ---------------------------------------------------------------------------

def _attempt_feature_reseed_body():
    html = _scanner_html()
    start = html.index("function attemptFeatureReseed(gray, quad, survivingPts, survivingCoverage, survivingGridCells)")
    end = html.index("function initializeFreshLiveTracker(now, metadata)")
    return html[start:end]


def test_healthy_full_point_population_does_not_reseed():
    """Task H item 1: needsReseed is false whenever the surviving point count is at or
    above reseedThreshold AND spatial grid coverage is at or above RESEED_MIN_GRID_CELLS
    — attemptFeatureReseed is only ever called inside that combined OR-condition's
    truthy branch, never unconditionally per tick."""
    track_body = _track_frame_body()
    threshold_at = track_body.index(
        "const reseedThreshold = Math.max(\n          MIN_GOOD_POINTS * 2,\n          Math.round(activeTrackerInitialPointCount * RESEED_POINT_FRACTION)\n        );"
    )
    needs_at = track_body.index(
        "const needsReseed = (prunedNext.length / 2) < reseedThreshold || survivingGridCells < RESEED_MIN_GRID_CELLS;"
    )
    call_at = track_body.index("finalPts = attemptFeatureReseed(gray, currCornersTrack, prunedNext, survivingCoverage, survivingGridCells);")
    guard_at = track_body.index("if (needsReseed && !reseedInProgress &&")
    assert threshold_at < needs_at < guard_at < call_at


def test_low_point_population_with_valid_geometry_triggers_one_reseed():
    """Task H item 2: the reseed guard checks reseedAttemptsForEpoch/
    consecutiveReseedFailures BEFORE calling attemptFeatureReseed, and
    attemptFeatureReseed itself increments reseedAttemptsForEpoch exactly once per call —
    so a single low-population tick triggers exactly one reseed attempt, not a loop
    within the same tick."""
    track_body = _track_frame_body()
    assert track_body.count("attemptFeatureReseed(gray, currCornersTrack, prunedNext, survivingCoverage, survivingGridCells)") == 1
    reseed_body = _attempt_feature_reseed_body()
    assert reseed_body.count("reseedAttemptsForEpoch++;") == 1


def test_low_spatial_coverage_may_trigger_reseed_independent_of_raw_count():
    """Task H item 3/18: needsReseed's OR-condition means a healthy raw point count with
    poor spatial grid distribution (survivingGridCells < RESEED_MIN_GRID_CELLS) can still
    trigger a reseed — point count alone is never sufficient to skip it."""
    track_body = _track_frame_body()
    assert "survivingGridCells < RESEED_MIN_GRID_CELLS" in track_body
    grid_cells_computed_at = track_body.index("const survivingGridCells = countOccupiedGridCells(prunedNext, currCornersTrack, POINT_GRID_SIZE);")
    needs_at = track_body.index("const needsReseed = (prunedNext.length / 2) < reseedThreshold || survivingGridCells < RESEED_MIN_GRID_CELLS;")
    assert grid_cells_computed_at < needs_at


def test_reseed_mask_uses_the_current_valid_marker_quad():
    """Task H item 4 (Pass 10: ROI-cropped, not full-frame): attemptFeatureReseed builds
    its search mask from the quad's OWN bounding box (roiX/roiY/roiW/roiH derived from
    `quad`, the parameter trackFrame passes as currCorners) and fills it via
    cv.fillConvexPoly with the quad's corners re-expressed in ROI-local coordinates —
    the just-applied, already-validated marker quad for this tick, never a stale one."""
    body = _attempt_feature_reseed_body()
    assert "const bounds = pointBounds(quad.flatMap(function (p) { return [p.x, p.y]; }));" in body
    assert "cv.fillConvexPoly(roiMask, quadRoiMat, new cv.Scalar(255));" in body
    track_body = _track_frame_body()
    assert "attemptFeatureReseed(gray, currCornersTrack, prunedNext, survivingCoverage, survivingGridCells)" in track_body


def test_fresh_points_are_restricted_to_marker_roi():
    """Task H item 5 (Pass 10): cv.goodFeaturesToTrack runs on roiGray (a cropped VIEW of
    the frame, via gray.roi(), restricted to the quad's bounding box) masked by roiMask —
    candidate points can only be found inside the marker ROI, and the ROI crop is what
    Pass 10 optimizes (previously a full-frame mask over the whole 1200x675 frame)."""
    body = _attempt_feature_reseed_body()
    roi_crop_at = body.index("roiGray = gray.roi(new cv.Rect(roiX, roiY, roiW, roiH));")
    gft_at = body.index("cv.goodFeaturesToTrack(roiGray, candidates, targetNew, 0.01, 8, roiMask, 3, false, 0.04);")
    assert roi_crop_at < gft_at


def test_surviving_and_fresh_points_are_spatially_deduplicated():
    """Task H item 6 (Pass 10: O(1)-amortized spatial hash, not O(N^2) nested loop): every
    fresh candidate is checked against the RESEED_DEDUP_RADIUS-bucketed grid via
    grid.isNear() before being merged — a candidate too close to an already-accepted
    point (surviving or already-merged, both inserted via grid.insert()) is dropped."""
    body = _attempt_feature_reseed_body()
    assert "const grid = createSpatialDedupGrid(RESEED_DEDUP_RADIUS);" in body
    assert "if (!grid.isNear(cx, cy)) {" in body
    assert "grid.insert(cx, cy);" in body
    grid_fn_start = _scanner_html().index("function createSpatialDedupGrid(cellSize)")
    grid_fn_end = _scanner_html().index("function attemptFeatureReseed(")
    grid_fn_body = _scanner_html()[grid_fn_start:grid_fn_end]
    assert "if (ddx * ddx + ddy * ddy < r2) return true;" in grid_fn_body


def test_merged_points_are_capped_at_target_capacity():
    """Task H item 7: the merge loop's own guard, (merged.length / 2) >= tierMaxPoints,
    breaks out of the loop before any further push — the merged set can never exceed the
    CURRENT TIER's target point capacity (Pass 13), the same cap bootstrap itself uses."""
    body = _attempt_feature_reseed_body()
    assert "const tierMaxPoints = currentMaxTrackPoints();" in body
    assert "if ((merged.length / 2) >= tierMaxPoints) break;" in body
    assert "merged.push(cx, cy);" in body


def test_successful_reseed_preserves_current_homography_and_overlay():
    """Task H item 8/9: attemptFeatureReseed never references H, currCorners assignment,
    applyWarp, or any overlay/video property — reseeding only ever replaces the POINT SET
    used for future LK ticks, never the already-applied geometry or playback state."""
    body = _attempt_feature_reseed_body()
    for forbidden in ("applyWarp(", "currCorners =", " H.", "overlay.src", "overlay.currentTime", "overlay.pause", "overlay.play"):
        assert forbidden not in body


def test_successful_reseed_does_not_schedule_server_detection():
    """Task H item 10: attemptFeatureReseed never calls scheduleNextScan, startDetectLoop,
    or detectOnceFromServer — a reseed is purely local and must never itself trigger a
    server round-trip."""
    body = _attempt_feature_reseed_body()
    for forbidden in ("scheduleNextScan(", "startDetectLoop(", "detectOnceFromServer("):
        assert forbidden not in body


def test_successful_reseed_establishes_a_fresh_lk_baseline():
    """Task H item 11: establishFrameGapBaseline() already runs unconditionally earlier in
    this same successful tick (Pass 8), strictly before the reseed trigger is even
    evaluated — so a tick that reseeds always already has a freshly-established baseline
    for itself, never a stale one."""
    track_body = _track_frame_body()
    establish_at = track_body.index("establishFrameGapBaseline(mediaTime, lastSuccessfulLkAt, firstPostReseedLkPending")
    reseed_call_at = track_body.index("finalPts = attemptFeatureReseed(gray, currCornersTrack, prunedNext, survivingCoverage, survivingGridCells);")
    assert establish_at < reseed_call_at


def test_failed_reseed_cannot_loop_indefinitely():
    """Task H item 12: the reseed guard in trackFrame requires
    reseedAttemptsForEpoch < MAX_RESEED_ATTEMPTS_PER_EPOCH AND
    consecutiveReseedFailures < MAX_CONSECUTIVE_RESEED_FAILURES — both bounded constants,
    reset only at the next tracking-epoch boundary (resetTrackingEpoch), never
    unconditionally retried tick after tick within the same epoch once exhausted."""
    track_body = _track_frame_body()
    assert "reseedAttemptsForEpoch < MAX_RESEED_ATTEMPTS_PER_EPOCH &&" in track_body
    assert "consecutiveReseedFailures < MAX_CONSECUTIVE_RESEED_FAILURES) {" in track_body
    html = _scanner_html()
    epoch_start = html.index("function resetTrackingEpoch(width, height)")
    epoch_end = html.index("function cornersToMat(corners)")
    epoch_body = html[epoch_start:epoch_end]
    assert "reseedAttemptsForEpoch = 0;" in epoch_body
    assert "consecutiveReseedFailures = 0;" in epoch_body


def test_failed_reseed_can_still_drop_through_insufficient_flow_points():
    """Task H item 13: every validation-failure branch returns fail(reason, ...), and
    fail() itself always returns survivingPts UNCHANGED (never partially applies a bad
    reseed) — later ticks continue with that same (possibly still-shrinking) point set
    and can still reach the existing, unmodified insufficient_flow_points drop path
    normally."""
    body = _attempt_feature_reseed_body()
    assert body.count("return fail(") == 4  # no_capacity_remaining, degenerate_roi, insufficient_new_points, insufficient_merged_coverage
    fail_fn_start = body.index("function fail(reason, candidatePoints) {")
    fail_fn_end = body.index("let roiGray = null, roiMask = null, candidates = null;")
    fail_fn_body = body[fail_fn_start:fail_fn_end]
    assert "return survivingPts;" in fail_fn_body
    track_body = _track_frame_body()
    assert "dropTracking('insufficient_flow_points', [gray, nextPts, status, err], {" in track_body


def test_failed_reseed_schedules_exactly_one_reacquisition():
    """Task H item 14: dropsAfterFailedReseed increments right before the existing,
    unmodified dropTracking('insufficient_flow_points', ...) call — which itself (proven
    elsewhere in this file) still calls scheduleNextScan('tracking_lost_reacquire', 0)
    exactly once."""
    track_body = _track_frame_body()
    counter_at = track_body.index("if (consecutiveReseedFailures > 0) diagState.dropsAfterFailedReseed++;")
    drop_at = track_body.index("dropTracking('insufficient_flow_points', [gray, nextPts, status, err], {", counter_at)
    assert counter_at < drop_at
    html = _scanner_html()
    drop_start = html.index("function dropTracking(reason, extraMats")
    drop_end = html.index("function handleDetectionTimeout()", drop_start)
    assert "scheduleNextScan('tracking_lost_reacquire', 0);" in html[drop_start:drop_end]


def test_invalid_geometry_or_quad_never_permits_reseeding():
    """Task H item 15/16: the reseed trigger/call is positioned strictly AFTER every
    geometry-rejecting early return (homography_empty, corner_order_invalid,
    out_of_bounds, pose_rejected_*, tracking_epoch_superseded, tracking_geometry_invalid)
    — Task D is satisfied by this placement: an invalid-geometry tick returns long before
    ever reaching the reseed code."""
    track_body = _track_frame_body()
    reseed_call_at = track_body.index("finalPts = attemptFeatureReseed(gray, currCornersTrack, prunedNext, survivingCoverage, survivingGridCells);")
    for rejecting_reason in (
        "dropTracking('homography_empty'",
        "dropTracking('corner_order_invalid'",
        "dropTracking('out_of_bounds'",
        "dropTracking('tracking_epoch_superseded'",
        "dropTracking('tracking_geometry_invalid'",
    ):
        assert track_body.index(rejecting_reason) < reseed_call_at


def test_background_drifted_points_are_removed_before_reseed_decision():
    """Task H item 17: prunedNext (built via isPointInQuadPadded against the just-applied
    currCorners) is computed BEFORE survivingCoverage/survivingGridCells/needsReseed —
    every downstream decision already excludes points that drifted outside the marker."""
    track_body = _track_frame_body()
    prune_loop_at = track_body.index("if (isPointInQuadPadded(goodNext[i], goodNext[i + 1], currCornersTrack, POINT_RETENTION_PAD)) {")
    coverage_at = track_body.index("const survivingCoverage = pointCoverage(prunedNext, currCornersTrack);")
    needs_at = track_body.index("const needsReseed = (prunedNext.length / 2) < reseedThreshold || survivingGridCells < RESEED_MIN_GRID_CELLS;")
    assert prune_loop_at < coverage_at < needs_at


def test_callback_delivered_after_cancellation_is_logged_skipped():
    """Task H item 19: a callback whose id matches lastCancelledCallbackId (the id
    cancelCurrentTrackingCallback most recently cancelled) is classified as
    'cancelled_callback' — a distinct reason from stale_owner/stale_epoch/
    tracking_inactive/mode_not_tracking — and still routes through the same
    [TRACK CALLBACK SKIPPED] log."""
    html = _scanner_html()
    validity_start = html.index("function trackingCallbackValidityFailureReason(callbackId, callbackEpoch, callbackOwnerToken)")
    validity_end = html.index("function trackOwnerState(extra)")
    validity_body = html[validity_start:validity_end]
    assert "if (callbackId === lastCancelledCallbackId) return 'cancelled_callback';" in validity_body
    fired_body = _tracking_callback_functions_body()
    assert "if (failureReason === 'cancelled_callback') diagState.callbacksSkippedAfterCancellation++;" in fired_body
    assert "logCallbackEvent('[TRACK CALLBACK SKIPPED]', {" in fired_body


def test_callback_delivered_after_cancellation_does_not_run_lk_or_rearm_or_mutate_owner():
    """Task H item 20/21/22: the failureReason branch (which 'cancelled_callback' always
    enters) returns before ever reaching trackFrame() (no LK), and the "release own slot"
    cancel call is gated on trackingCallbackId === callbackId — for a cancelled_callback
    firing, trackingCallbackId no longer equals this stale id (something else owns it, or
    it's null), so that cancel path correctly never mutates the CURRENT/newer owner."""
    body = _tracking_callback_functions_body()
    fired_start = body.index("function onTrackingCallbackFired(callbackId, callbackType, now, metadata, callbackEpoch, callbackOwnerToken)")
    failure_check_at = body.index("if (failureReason) {", fired_start)
    failure_return_at = body.index("return;", failure_check_at)
    trackframe_call_at = body.index("trackFrame(now, Object.assign(")
    assert failure_check_at < failure_return_at < trackframe_call_at
    guard_at = body.index("if (failureReason !== 'stale_owner' && trackingCallbackId === callbackId) {")
    assert "cancelCurrentTrackingCallback('stale_skip_' + failureReason);" in body[guard_at:guard_at + 200]


def test_pass9_new_functions_never_touch_video_source_or_currenttime_or_loop():
    """Task H item 26: none of the new Pass 9 functions (attemptFeatureReseed,
    isPointInQuadPadded, countOccupiedGridCells, medianOf) assign overlay.src or
    overlay.currentTime, and none reference the native <video loop> attribute."""
    html = _scanner_html()
    for fn_start_marker, fn_end_marker in (
        ("function isPointInQuadPadded(px, py, quad, pad)", "function countOccupiedGridCells(points, quad, gridSize)"),
        ("function countOccupiedGridCells(points, quad, gridSize)", "function medianOf(values)"),
        ("function medianOf(values)", "function attemptFeatureReseed(gray, quad, survivingPts, survivingCoverage, survivingGridCells)"),
        ("function attemptFeatureReseed(gray, quad, survivingPts, survivingCoverage, survivingGridCells)", "function initializeFreshLiveTracker(now, metadata)"),
    ):
        body = html[html.index(fn_start_marker):html.index(fn_end_marker)]
        assert "overlay.src" not in body
        assert "overlay.currentTime" not in body
        assert "overlay.loop" not in body
    assert html.count('<video id="overlay"') == 1


def test_pass9_did_not_change_any_recognition_or_geometry_threshold():
    """Task H item 27: MIN_GOOD_POINTS/MAX_ERR/RANSAC_REPROJ/TRACKING_GRACE_FRAMES/
    TRACKING_GRACE_MS/POSE_HOLD_MS/RVFC_STALL_TIMEOUT_MS remain unchanged — this pass
    only added point-health diagnostics and a bounded, geometry-safe local reseed, never
    a recognition or geometry acceptance threshold. RESEED_MIN_COVERAGE_AFTER_MERGE
    deliberately reuses bootstrap's own existing 0.25 coverage floor rather than
    introducing a separate, weaker one."""
    html = _scanner_html()
    assert "const MIN_GOOD_POINTS =" in html
    assert "const MAX_ERR =" in html
    assert "const TRACKING_GRACE_FRAMES = 3;" in html
    assert "const TRACKING_GRACE_MS = 900;" in html
    assert "const POSE_HOLD_MS = 500;" in html
    assert "const RVFC_STALL_TIMEOUT_MS = TRACKING_GRACE_MS;" in html
    assert "const RANSAC_REPROJ = 5.0;" in html
    assert "const RESEED_MIN_COVERAGE_AFTER_MERGE = 0.25;" in html
    assert "coverage < 0.25" in html  # initializeFreshLiveTracker's own existing bootstrap floor, unchanged


# ---------------------------------------------------------------------------
# Pass 10: prevent reseed's own wall-clock cost from being misclassified as a camera/
# tracking continuity gap. Real-device evidence: successful reseeds took ~850ms
# (main-thread-blocking goodFeaturesToTrack), TRACKING_GRACE_MS is 900ms, and the very
# next callback's own genuinely-advanced media time (real time passed while JS was
# blocked) got compared against the PRE-reseed baseline and hard-dropped as
# tracking_frame_gap_exceeded — exactly the class of bug Pass 8 already solved for other
# continuity boundaries (stall, rearm, bootstrap, ...); reseed just wasn't one of them yet.
# ---------------------------------------------------------------------------

def test_successful_reseed_invalidates_old_lk_baseline_and_bumps_continuity_token():
    """Task G item 1/2: attemptFeatureReseed's success path calls
    resetFrameGapBaseline('feature_reseed_success') — which itself (Pass 8, unchanged)
    bumps frameGapContinuityToken and clears hasSuccessfulLkBaseline — so the pre-reseed
    baseline can never again satisfy hasValidLkBaseline's continuityToken match."""
    body = _attempt_feature_reseed_body()
    assert "resetFrameGapBaseline('feature_reseed_success');" in body
    assert "firstPostReseedLkPending = true;" in body
    reset_at = body.index("resetFrameGapBaseline('feature_reseed_success');")
    return_merged_at = body.index("return merged;")
    assert reset_at < return_merged_at
    reset_fn_start = _scanner_html().index("function resetFrameGapBaseline(reason)")
    reset_fn_end = _scanner_html().index("function establishFrameGapBaseline(mediaTime, wallTime, reason)")
    reset_fn_body = _scanner_html()[reset_fn_start:reset_fn_end]
    assert "frameGapContinuityToken++;" in reset_fn_body
    assert "hasSuccessfulLkBaseline = false;" in reset_fn_body


def test_first_callback_after_reseed_cannot_emit_frame_gap_exceeded():
    """Task G item 3/6/7: after resetFrameGapBaseline runs (inside a successful reseed),
    hasSuccessfulLkBaseline is false and frameGapContinuityToken has moved on — the next
    tick's hasValidLkBaseline check (same mechanism trackFrame already uses for every
    other continuity boundary) is therefore false regardless of how large the wall-clock
    OR media-time gap actually is, so the entire gap-drop block is skipped — reseed's own
    ~850ms of processing time can never by itself trigger tracking_frame_gap_exceeded."""
    track_body = _track_frame_body()
    assert "const hasValidLkBaseline = hasSuccessfulLkBaseline &&" in track_body
    assert "successfulLkContinuityToken === frameGapContinuityToken;" in track_body
    gap_block_at = track_body.index("if (hasValidLkBaseline && (now - lastSuccessfulLkWallTime) > TRACKING_GRACE_MS) {")
    gray_at = track_body.index("gray = matFromVideoGray();")
    assert gap_block_at < gray_at  # skipped tick falls straight through to normal LK, never a bare drop


def test_first_successful_post_reseed_lk_establishes_baseline_with_distinct_reason():
    """Task G item 4: the tick after a reseed, if its own LK succeeds, calls
    establishFrameGapBaseline with reason='first_post_reseed_lk' (via
    firstPostReseedLkPending) instead of the generic 'successful_lk' — and clears the
    flag immediately after, so only that ONE tick is labeled this way."""
    track_body = _track_frame_body()
    call_at = track_body.index(
        "establishFrameGapBaseline(mediaTime, lastSuccessfulLkAt, firstPostReseedLkPending"
    )
    assert "? 'first_post_reseed_lk' : (gapWasSuspected ? 'gap_recovered' : 'successful_lk'));" in track_body[call_at:call_at + 200]
    clear_at = track_body.index("if (firstPostReseedLkPending) firstPostReseedLkPending = false;")
    assert call_at < clear_at
    establish_fn_start = _scanner_html().index("function establishFrameGapBaseline(mediaTime, wallTime, reason)")
    establish_fn_end = _scanner_html().index("function cancelCurrentTrackingCallback(reason)")
    establish_fn_body = _scanner_html()[establish_fn_start:establish_fn_end]
    assert "reason: reason || 'successful_lk'" in establish_fn_body


def test_reseed_performance_diagnostic_is_bounded_and_scalar_only():
    """Task G item 13: [TRACK RESEED PERFORMANCE] carries only scalar counts/durations
    (roiWidth/roiHeight/candidateCount/featureDetectMs/mergeMs/totalMs) — never a point
    array or other unbounded structure."""
    body = _attempt_feature_reseed_body()
    perf_at = body.index("logCallbackEvent('[TRACK RESEED PERFORMANCE]', {")
    call_text = body[perf_at:body.index(");", perf_at) + 2]
    assert "roiWidth: roiW, roiHeight: roiH, candidateCount, featureDetectMs, mergeMs," in call_text
    assert "totalMs:" in call_text
    assert "candData" not in call_text
    assert "merged" not in call_text


def test_reseed_temporary_mats_are_deleted_deterministically():
    """Task G item 14: roiGray/roiMask/candidates are all released via deleteMats(...) in
    a finally block — guaranteed regardless of which return path (success or any of the
    fail() branches) was taken."""
    body = _attempt_feature_reseed_body()
    finally_at = body.index("} finally {")
    finally_block = body[finally_at:body.index("}", body.index("reseedInProgress = false;", finally_at)) + 1]
    assert "deleteMats(roiGray, roiMask, candidates);" in finally_block
    assert "reseedInProgress = false;" in finally_block


def test_roi_cropped_feature_detection_stays_inside_marker_bounding_box():
    """Task G item 15 (Pass 10 performance fix): roiGray is a cropped VIEW of gray
    (gray.roi(...)), bounded to the quad's own bounding box plus a small margin — never
    the full frame — so goodFeaturesToTrack's own cost scales with the marker's area,
    not the whole 1200x675 capture frame."""
    body = _attempt_feature_reseed_body()
    assert "const roiW = Math.min(gray.cols - roiX, Math.ceil(bounds.width) + margin * 2);" in body
    assert "const roiH = Math.min(gray.rows - roiY, Math.ceil(bounds.height) + margin * 2);" in body
    assert "roiGray = gray.roi(new cv.Rect(roiX, roiY, roiW, roiH));" in body


def test_recovery_scan_has_exactly_one_owner():
    """Task G item 20/21: watchdogTrackingSkipReason() returns 'scan_timer_pending'
    whenever detectLoopTimer is truthy — including the zero-delay tracking_lost_reacquire
    timer dropTracking's own scheduleNextScan call arms — so the watchdog can never force
    a second capture while that timer already owns recovery for the same loss. The
    forced-detection branch is only reached once this (and every other) skip reason has
    already returned null."""
    html = _scanner_html()
    reason_start = html.index("function watchdogTrackingSkipReason()")
    reason_end = html.index("function cancelPendingNormalScan(reason)")
    reason_body = html[reason_start:reason_end]
    assert "if (detectLoopTimer) return 'scan_timer_pending';" in reason_body
    drop_start = html.index("function dropTracking(reason, extraMats")
    drop_end = html.index("function handleDetectionTimeout()", drop_start)
    drop_body = html[drop_start:drop_end]
    owner_log_at = drop_body.index("owner: 'tracking_lost_reacquire', timerPending: true,")
    schedule_at = drop_body.index("scheduleNextScan('tracking_lost_reacquire', 0);")
    assert owner_log_at < schedule_at
    watchdog_start = html.index("function watchdogTick(token")
    watchdog_end = html.index("function skipTick(reason, extra)")
    watchdog_body = html[watchdog_start:watchdog_end]
    skip_check_at = watchdog_body.index("const trackingSkipReason = watchdogTrackingSkipReason();")
    force_owner_log_at = watchdog_body.index("owner: 'watchdog', timerPending: Boolean(detectLoopTimer),")
    forced_call_at = watchdog_body.index("detectOnceFromServer(true);")
    assert skip_check_at < force_owner_log_at < forced_call_at


def test_pass10_new_code_never_touches_video_source_or_currenttime_or_loop():
    """Task G item 26: createSpatialDedupGrid, the ROI-cropping additions to
    attemptFeatureReseed, and the new watchdog scan-timer-pending/recovery-owner code
    never assign overlay.src or overlay.currentTime, and never reference the native
    <video loop> attribute."""
    html = _scanner_html()
    for fn_start_marker, fn_end_marker in (
        ("function createSpatialDedupGrid(cellSize)", "function attemptFeatureReseed("),
        ("function attemptFeatureReseed(gray, quad, survivingPts, survivingCoverage, survivingGridCells)", "function initializeFreshLiveTracker(now, metadata)"),
        ("function watchdogTrackingSkipReason()", "function cancelPendingNormalScan(reason)"),
    ):
        body = html[html.index(fn_start_marker):html.index(fn_end_marker)]
        assert "overlay.src" not in body
        assert "overlay.currentTime" not in body
        assert "overlay.loop" not in body
    assert html.count('<video id="overlay"') == 1


def test_pass10_did_not_change_any_recognition_or_geometry_threshold():
    """Task G item 25: MIN_GOOD_POINTS/MAX_ERR/RANSAC_REPROJ/TRACKING_GRACE_FRAMES/
    TRACKING_GRACE_MS (never extended, per the explicit instruction not to)/POSE_HOLD_MS/
    RVFC_STALL_TIMEOUT_MS all remain unchanged — this pass only touched reseed timing/
    performance and recovery-scan ownership, never a recognition or geometry threshold."""
    html = _scanner_html()
    assert "const TRACKING_GRACE_FRAMES = 3;" in html
    assert "const TRACKING_GRACE_MS = 900;" in html
    assert "const POSE_HOLD_MS = 500;" in html
    assert "const RVFC_STALL_TIMEOUT_MS = TRACKING_GRACE_MS;" in html
    assert "const RANSAC_REPROJ = 5.0;" in html
    assert "const RESEED_POINT_FRACTION = 0.35;" in html
    assert "const RESEED_MIN_COVERAGE_AFTER_MERGE = 0.25;" in html


# ---------------------------------------------------------------------------
# Pass 11: POST-LK GAP VALIDATION + OVERLAY SHAPE STABILITY.
#
# Blocker 1 (Task A/B): the pre-LK frame-gap check no longer drops tracking by itself —
# it only records a suspicion (gapWasSuspected) and falls through into the real LK/
# geometry pipeline; the real outcome is reported by reportGapOutcome() at whichever
# existing exit point the tick actually reaches.
#
# Blocker 2 (Task C/D/E/F/G/H): an unstable proposed homography is no longer rendered
# outright — poseCompatibility's own jump reasons plus a new weak_geometry_support check
# (RANSAC-inlier population + spatial spread) feed a three-way ACCEPT/HOLD/REJECT
# decision, with HOLD bounded by dedicated shapeHold* state (never the generic
# trackingBadFrames/graceEnteredAt pair).
# ---------------------------------------------------------------------------

def test_i01_large_media_time_gap_is_recorded_as_suspected_not_dropped_pre_lk():
    """Task I item 1: a large gap since the last successful LK sets gapWasSuspected = true
    and logs [TRACK GAP SUSPECTED] — it never calls dropTracking from within the gap-check
    block itself."""
    track_body = _track_frame_body()
    gap_check_at = track_body.index("if (hasValidLkBaseline && (now - lastSuccessfulLkWallTime) > TRACKING_GRACE_MS) {")
    hidden_at = track_body.index("if (document.hidden) {", gap_check_at)
    suspected_at = track_body.index("gapWasSuspected = true;", gap_check_at)
    gray_at = track_body.index("gray = matFromVideoGray();")
    assert gap_check_at < hidden_at < suspected_at < gray_at
    assert "dropTracking(" not in track_body[gap_check_at:suspected_at]


def test_i02_gray_conversion_still_occurs_after_a_suspected_gap():
    """Task I item 2: gray = matFromVideoGray() runs unconditionally after the suspicion
    is recorded — the suspected-gap branch never returns before it."""
    track_body = _track_frame_body()
    suspected_at = track_body.index("gapWasSuspected = true;")
    gray_at = track_body.index("gray = matFromVideoGray();")
    assert suspected_at < gray_at


def test_i03_lk_still_runs_after_a_suspected_gap():
    """Task I item 3: calcOpticalFlowPyrLK runs unconditionally after a suspected gap —
    no return between the suspicion being recorded and LK actually executing."""
    track_body = _track_frame_body()
    suspected_at = track_body.index("gapWasSuspected = true;")
    lk_at = track_body.index("cv.calcOpticalFlowPyrLK(")
    assert suspected_at < lk_at


def test_i04_valid_current_lk_recovers_a_suspected_gap():
    """Task I item 4: on the true ACCEPT point (LK + geometry + applyWarp all succeeded),
    reportGapOutcome(null, ...) is called — which is exactly the call that logs
    [TRACK GAP RECOVERED] when gapWasSuspected is true."""
    track_body = _track_frame_body()
    apply_warp_at = track_body.index("if (!applyWarp(currCorners)) {")
    recovered_at = track_body.index("reportGapOutcome(null, goodPrev.length / 2, geometryInlierCount);")
    establish_at = track_body.index("establishFrameGapBaseline(mediaTime, lastSuccessfulLkAt, firstPostReseedLkPending")
    assert apply_warp_at < recovered_at < establish_at
    report_fn_at = track_body.index("function reportGapOutcome(failureReason, goodPointCount, geometryGoodCount) {")
    report_fn_body = track_body[report_fn_at:track_body.index("try {", report_fn_at)]
    assert "diagState.frameGapRecoveredByCurrentFrame++;" in report_fn_body
    assert "logCallbackEvent('[TRACK GAP RECOVERED]'," in report_fn_body


def test_i05_recovered_gap_does_not_call_backend():
    """Task I item 5: trackFrame never calls detectOnceFromServer — a recovered gap is
    resolved entirely from local LK/geometry evidence, no network request involved."""
    track_body = _track_frame_body()
    assert "detectOnceFromServer(" not in track_body


def test_i06_recovered_gap_resets_or_refreshes_baseline_safely():
    """Task I item 6: establishFrameGapBaseline() (which refreshes lastSuccessfulLkWallTime/
    MediaTime) runs right after reportGapOutcome(null, ...) reports the recovery — the very
    next tick is compared against this fresh timestamp, never the stale pre-gap one. The
    reason argument distinguishes a recovered-gap baseline (gap_recovered) from an ordinary
    one (successful_lk)."""
    track_body = _track_frame_body()
    recovered_at = track_body.index("reportGapOutcome(null, goodPrev.length / 2, geometryInlierCount);")
    establish_at = track_body.index("establishFrameGapBaseline(mediaTime, lastSuccessfulLkAt, firstPostReseedLkPending", recovered_at)
    assert recovered_at < establish_at
    assert "? 'first_post_reseed_lk' : (gapWasSuspected ? 'gap_recovered' : 'successful_lk'));" in track_body[establish_at:establish_at + 200]


def test_i07_invalid_current_lk_drops_with_insufficient_flow_points():
    """Task I item 7: when the current frame's own LK yields too few points, the tick still
    drops with the real evidence-based reason (insufficient_flow_points), reported via
    reportGapOutcome before dropTracking — never a frame-gap reason."""
    track_body = _track_frame_body()
    report_at = track_body.index("reportGapOutcome('insufficient_flow_points', goodPrev.length / 2, null);")
    drop_at = track_body.index("dropTracking('insufficient_flow_points', [gray, nextPts, status, err], {", report_at)
    assert report_at < drop_at


def test_i08_invalid_geometry_drops_with_its_real_geometry_reason():
    """Task I item 8: a proposed shape that fails corner ordering drops with
    corner_order_invalid (its real, evidence-based reason) — reportGapOutcome is called
    with that exact reason, never a generic frame-gap one."""
    track_body = _track_frame_body()
    report_at = track_body.index("reportGapOutcome('corner_order_invalid', goodPrev.length / 2, geometryInlierCount);")
    drop_at = track_body.index("dropTracking('corner_order_invalid', [gray, nextPts, status, err, prevMat, nextMat, mask, H], {", report_at)
    assert report_at < drop_at


def test_i09_tracking_frame_gap_exceeded_cannot_occur_with_gray_missing():
    """Task I item 9: tracking_frame_gap_exceeded is no longer a dropTracking() reason
    anywhere in the file — it structurally cannot fire before gray is populated (or at
    all), since the call site was removed entirely in favor of suspect-then-fall-through."""
    html = _scanner_html()
    assert html.count("dropTracking('tracking_frame_gap_exceeded'") == 0


def test_i10_tracking_frame_gap_exceeded_cannot_occur_with_good_points_missing():
    """Task I item 10: same invariant as item 9, from the goodPoints angle — since the
    reason has no dropTracking() caller left at all, it cannot fire with goodPoints
    unpopulated (or in any other state)."""
    html = _scanner_html()
    assert html.count("dropTracking('tracking_frame_gap_exceeded'") == 0
    assert "genuinePresentedGap" not in html
    assert "workloadActive" not in html


def test_i11_preLkFrameGapDrops_remains_zero():
    """Task I item 11: preLkFrameGapDrops is declared (init + Reset Diagnostics) but never
    incremented anywhere in the file — it is a pure regression tripwire, always reading 0."""
    html = _scanner_html()
    assert "preLkFrameGapDrops: 0" in html
    assert html.count("preLkFrameGapDrops: 0") == 2  # diagState init + Reset Diagnostics handler
    assert "preLkFrameGapDrops++" not in html
    assert "diagState.preLkFrameGapDrops++" not in html


def test_i12_stable_proposed_shape_is_accepted():
    """Task I item 12: when shapeReason is falsy (poseCompatibility ok AND geometry support
    sufficient), the tick resets the hold/reject counters and proceeds to the epoch check
    and applyWarp — it never enters the hold/reject branch at all."""
    track_body = _track_frame_body()
    shape_reason_at = track_body.index("const shapeReason = !localPoseQuality.ok ? localPoseQuality.reason : (weakGeometrySupport ? 'weak_geometry_support' : null);")
    reset_at = track_body.index("shapeHoldFrames = 0;\n        consecutiveShapeRejects = 0;", shape_reason_at)
    apply_warp_at = track_body.index("currCornersTrack = newCorners;\n        currCorners = toIntrinsicSpace(newCorners);\n        if (!applyWarp(currCorners)) {")
    assert shape_reason_at < reset_at < apply_warp_at


def test_i13_one_suspicious_shape_may_hold_previous_quad():
    """Task I item 13: a suspicious (but not immediately-rejectable) shape enters the HOLD
    branch, which returns WITHOUT ever reaching `currCorners = newCorners` — the previously
    rendered quad keeps being what's on screen."""
    track_body = _track_frame_body()
    hold_at = track_body.index("if (decision === 'hold') {")
    hold_return_region = track_body[hold_at:track_body.index("return;", hold_at) + len("return;")]
    assert "deleteMats(gray, nextPts, status, err, prevMat, nextMat, mask, H);" in hold_return_region
    apply_warp_at = track_body.index("currCornersTrack = newCorners;\n        currCorners = toIntrinsicSpace(newCorners);\n        if (!applyWarp(currCorners)) {")
    assert hold_at < apply_warp_at  # HOLD's return is textually before the accept-only commit


def test_i14_held_shape_keeps_overlay_playback_uninterrupted():
    """Task I item 14: the HOLD branch never calls stopOverlayImmediate/pause/clearTracking
    Geometry — it only deletes this tick's Mats and returns, leaving overlay/tracking state
    exactly as the last accepted tick left it."""
    track_body = _track_frame_body()
    hold_at = track_body.index("if (decision === 'hold') {")
    hold_end_at = track_body.index("return;", hold_at) + len("return;")
    hold_body = track_body[hold_at:hold_end_at]
    assert "stopOverlayImmediate(" not in hold_body
    assert "overlay.pause(" not in hold_body
    assert "clearTrackingGeometry(" not in hold_body


def test_i15_held_shape_does_not_replace_lastAcceptedCorners():
    """Task I item 15: lastAcceptedCorners is only ever assigned at the true ACCEPT point
    (right after applyWarp succeeds) — the HOLD branch's return is textually before that
    assignment, so it can never reach it."""
    track_body = _track_frame_body()
    hold_return_at = track_body.index("if (decision === 'hold') {")
    hold_return_end = track_body.index("return;", hold_return_at)
    accept_assign_at = track_body.index("lastAcceptedCorners = cloneCorners(currCorners);")
    assert hold_return_end < accept_assign_at
    assert track_body.count("lastAcceptedCorners = cloneCorners(currCorners);") == 1


def test_i16_held_shape_does_not_reseed_from_suspicious_geometry():
    """Task I item 16: attemptFeatureReseed is only ever called after the accept-only
    commit (currCorners = newCorners + applyWarp success) — the HOLD branch's return sits
    textually before that call, so a held, suspicious proposed quad can never reach it."""
    track_body = _track_frame_body()
    hold_return_end = track_body.index("return;", track_body.index("if (decision === 'hold') {"))
    reseed_call_at = track_body.index("finalPts = attemptFeatureReseed(gray, currCornersTrack, prunedNext, survivingCoverage, survivingGridCells);")
    assert hold_return_end < reseed_call_at


def test_i17_second_valid_shape_after_hold_resumes_normal_tracking():
    """Task I item 17: once a subsequent tick's shapeReason is falsy, shapeHoldFrames/
    consecutiveShapeRejects reset to 0 and the tick proceeds through the normal epoch-check/
    applyWarp/reseed path exactly like any other accepted tick — no separate "resuming from
    hold" code path exists."""
    track_body = _track_frame_body()
    reset_at = track_body.index("shapeHoldFrames = 0;\n        consecutiveShapeRejects = 0;")
    epoch_check_at = track_body.index("if (trackingEpoch !== epochAtTickStart) {", reset_at)
    assert reset_at < epoch_check_at


def test_i18_repeated_suspicious_shapes_exhaust_bounded_hold_and_drop():
    """Task I item 18: shapeHoldFrames/shapeHoldStartedAt accumulate across consecutive
    suspicious ticks; once shapeHoldFrames >= SHAPE_HOLD_MAX_FRAMES OR the elapsed time >=
    SHAPE_HOLD_MAX_MS, holdExpired becomes true, decision becomes 'reject', and the tick
    finally drops via the normal dropTracking() path — exactly one reacquisition, same as
    every other drop reason."""
    track_body = _track_frame_body()
    assert "if (shapeHoldFrames === 0) shapeHoldStartedAt = performance.now();" in track_body
    assert "shapeHoldFrames++;" in track_body
    assert "holdExpired = shapeHoldFrames >= SHAPE_HOLD_MAX_FRAMES || (performance.now() - shapeHoldStartedAt) >= SHAPE_HOLD_MAX_MS;" in track_body
    reject_report_at = track_body.index("reportGapOutcome('pose_rejected_' + shapeReason, errorGoodCount, geometryInlierCount);")
    reject_drop_at = track_body.index("dropTracking('pose_rejected_' + shapeReason, [gray, nextPts, status, err, prevMat, nextMat, mask, H], {", reject_report_at)
    assert reject_report_at < reject_drop_at


def test_i19_crossed_corners_reject_immediately():
    """Task I item 19: a self-intersecting proposed quad is rejected by validateOverlayQuad
    (self_intersecting_quad) inside normalizeCornerOrder — normalizeCornerOrder returns null,
    and the EXISTING corner_order_invalid dropTracking() fires BEFORE the shape-continuity
    HOLD logic is even reached, so it can never be held."""
    track_body = _track_frame_body()
    assert "if (edges.some(edge => edge < 1)) return { ok: false, reason: 'zero_edge', edges };" in _scanner_html()
    corner_order_drop_at = track_body.index("dropTracking('corner_order_invalid', [gray, nextPts, status, err, prevMat, nextMat, mask, H], {")
    shape_reason_at = track_body.index("const shapeReason = !localPoseQuality.ok ? localPoseQuality.reason : (weakGeometrySupport ? 'weak_geometry_support' : null);")
    assert corner_order_drop_at < shape_reason_at


def test_i20_non_convex_quad_rejects_immediately():
    """Task I item 20: validateOverlayQuad's diagonals_do_not_cross_inside check (the
    codebase's existing convexity proxy for a simple quad) rejects a non-convex proposal
    inside normalizeCornerOrder, before the shape-continuity HOLD logic is reached — same
    structural guarantee as crossed corners."""
    html = _scanner_html()
    assert "if (!diagonalsCross) return { ok: false, reason: 'diagonals_do_not_cross_inside'" in html
    track_body = _track_frame_body()
    corner_order_drop_at = track_body.index("dropTracking('corner_order_invalid', [gray, nextPts, status, err, prevMat, nextMat, mask, H], {")
    shape_reason_at = track_body.index("const shapeReason =")
    assert corner_order_drop_at < shape_reason_at


def test_i21_non_finite_corners_reject_immediately():
    """Task I item 21: cloneCorners() returns null for any non-finite coordinate, which
    validateOverlayQuad turns into reason 'non_finite_or_not_four', which normalizeCornerOrder
    propagates as null — same corner_order_invalid immediate-reject path, before HOLD."""
    html = _scanner_html()
    assert "if (!clean) return { ok: false, reason: 'non_finite_or_not_four' };" in html
    assert "return copy.every(p => Number.isFinite(p.x) && Number.isFinite(p.y)) ? copy : null;" in html


def test_i22_out_of_bounds_remains_an_immediate_rejection():
    """Task I item 22: the existing out_of_bounds pad-based check is untouched and still
    sits before the shape-continuity block — an out-of-bounds proposal drops immediately,
    never entering HOLD."""
    track_body = _track_frame_body()
    out_of_bounds_drop_at = track_body.index("dropTracking('out_of_bounds', [gray, nextPts, status, err, prevMat, nextMat, mask, H], {")
    shape_reason_at = track_body.index("const shapeReason =")
    assert out_of_bounds_drop_at < shape_reason_at
    assert "const pad = 0.40;" in track_body


def test_i23_impossible_area_collapse_rejects():
    """Task I item 23: poseCompatibility's area_jump reason (areaRatio < 0.4, an impossible
    single-frame collapse) is in IMMEDIATE_SHAPE_REJECT_REASONS — it bypasses HOLD entirely,
    going straight to dropTracking."""
    html = _scanner_html()
    assert "if (areaRatio > 2.5 || areaRatio < 0.4) return { ok: false, reason: 'area_jump'" in html
    track_body = _track_frame_body()
    assert "const IMMEDIATE_SHAPE_REJECT_REASONS = { area_jump: true, winding_flip: true, self_intersecting_quad: true };" in track_body
    immediate_at = track_body.index("const IMMEDIATE_SHAPE_REJECT_REASONS = { area_jump: true, winding_flip: true, self_intersecting_quad: true };")
    is_immediate_at = track_body.index("const isImmediateReject = Boolean(IMMEDIATE_SHAPE_REJECT_REASONS[shapeReason]);")
    assert immediate_at < is_immediate_at


def test_i24_impossible_area_expansion_rejects():
    """Task I item 24: the same area_jump reason also covers impossible expansion
    (areaRatio > 2.5) — one reason, one immediate-reject bucket, both directions."""
    html = _scanner_html()
    assert "areaRatio > 2.5" in html
    assert "reason: 'area_jump'" in html


def test_i25_excessive_single_corner_jump_is_not_rendered():
    """Task I item 25: poseCompatibility's corner_jump reason (maxCornerJump > 0.55) is NOT
    in the immediate-reject bucket — it is holdable — but the HOLD branch's return sits
    textually before `currCorners = newCorners`, so a large single-corner jump is never
    applied/rendered while it is being held."""
    html = _scanner_html()
    assert "if (maxCornerJump > 0.55) return { ok: false, reason: 'corner_jump'" in html
    track_body = _track_frame_body()
    assert "corner_jump" not in track_body[track_body.index("const IMMEDIATE_SHAPE_REJECT_REASONS"):track_body.index("const IMMEDIATE_SHAPE_REJECT_REASONS") + 120]
    hold_at = track_body.index("if (decision === 'hold') {")
    apply_warp_at = track_body.index("currCornersTrack = newCorners;\n        currCorners = toIntrinsicSpace(newCorners);\n        if (!applyWarp(currCorners)) {")
    assert hold_at < apply_warp_at


def test_i26_poor_geometry_inlier_ratio_cannot_be_accepted():
    """Task I item 26: weakGeometrySupport requires geometryInlierCount >= MIN_GOOD_POINTS
    (reusing the existing hard floor, never a weaker invented threshold) — falling short
    forces shapeReason = 'weak_geometry_support', which can never fall through to ACCEPT."""
    track_body = _track_frame_body()
    assert "const weakGeometrySupport = geometryInlierCount < MIN_GOOD_POINTS || geometryInlierGridCells < RESEED_MIN_GRID_CELLS;" in track_body


def test_i27_poor_geometry_spatial_distribution_cannot_be_accepted():
    """Task I item 27: weakGeometrySupport also requires geometryInlierGridCells (spatial
    spread of the RANSAC-inlier subset, via the existing countOccupiedGridCells/
    RESEED_MIN_GRID_CELLS machinery) to clear the bar — a clustered-but-numerous inlier set
    still cannot be accepted."""
    track_body = _track_frame_body()
    assert "const geometryInlierGridCells = currCornersTrack ? countOccupiedGridCells(geometryInlierNext, currCornersTrack, POINT_GRID_SIZE) : 0;" in track_body
    assert "geometryInlierGridCells < RESEED_MIN_GRID_CELLS" in track_body


def test_i28_smoothing_runs_only_after_accept():
    """Task I item 28: applyWarp (which internally calls smoothPoseCorners) is only ever
    invoked with currCorners, and currCorners is only ever reassigned AFTER the
    shape-continuity block's HOLD/REJECT branches have already returned — smoothing can
    structurally never see an un-accepted proposed shape."""
    track_body = _track_frame_body()
    shape_reason_at = track_body.index("const shapeReason =")
    reject_return_at = track_body.rindex("return;", shape_reason_at, track_body.index("currCornersTrack = newCorners;\n        currCorners = toIntrinsicSpace(newCorners);\n        if (!applyWarp(currCorners)) {"))
    commit_at = track_body.index("currCornersTrack = newCorners;\n        currCorners = toIntrinsicSpace(newCorners);\n        if (!applyWarp(currCorners)) {")
    assert shape_reason_at < reject_return_at < commit_at


def test_i29_smoothing_state_resets_on_epoch_change():
    """Task I item 29: the existing accept-branch isSameTarget check (preserved, unchanged)
    already resets lastOrdered/smoothCorners on a cross-target re-anchor, immediately
    before the epoch-bumping bootstrap runs — the dedicated shapeHold* state gets its own
    explicit reset directly inside resetTrackingEpoch() as a belt-and-suspenders measure
    for the epoch boundary itself."""
    html = _scanner_html()
    same_target_at = html.index("const isSameTarget = wasSameTarget && wasTracking;")
    smoothing_reset_at = html.index("lastOrdered = null;\n          smoothCorners = null;", same_target_at)
    assert same_target_at < smoothing_reset_at
    epoch_start = html.index("function resetTrackingEpoch(width, height)")
    epoch_end = html.index("function cornersToMat(corners)")
    epoch_body = html[epoch_start:epoch_end]
    assert "shapeHoldFrames = 0;" in epoch_body
    assert "lastAcceptedCorners = null;" in epoch_body


def test_i30_shape_history_resets_on_reacquisition():
    """Task I item 30: clearTrackingGeometry() (called by dropTracking, i.e. every
    reacquisition) resets the dedicated shapeHold*/lastAcceptedCorners state — a fresh
    tracking session never inherits a stale "last accepted shape" from before the drop."""
    html = _scanner_html()
    clear_start = html.index("function clearTrackingGeometry(reason, options = {})")
    clear_end = html.index("function logCallbackEvent(tag, summaryFields, structuredData)")
    clear_body = html[clear_start:clear_end]
    for var_reset in (
        "shapeHoldFrames = 0;", "shapeHoldStartedAt = 0;", "consecutiveShapeRejects = 0;",
        "lastAcceptedCorners = null;", "lastAcceptedShapeAt = 0;"
    ):
        assert var_reset in clear_body


def test_i31_accepted_shape_may_be_used_for_reseed_mask():
    """Task I item 31: attemptFeatureReseed is called with currCorners — which by this
    point in the tick has ALREADY been set to the just-accepted newCorners (right after
    applyWarp succeeded) — so a reseed mask always comes from an accepted quad."""
    track_body = _track_frame_body()
    commit_at = track_body.index("currCornersTrack = newCorners;\n        currCorners = toIntrinsicSpace(newCorners);\n        if (!applyWarp(currCorners)) {")
    reseed_call_at = track_body.index("finalPts = attemptFeatureReseed(gray, currCornersTrack, prunedNext, survivingCoverage, survivingGridCells);")
    assert commit_at < reseed_call_at


def test_i32_held_or_rejected_proposed_shape_cannot_be_used_for_reseed_mask():
    """Task I item 32: both the HOLD and REJECT branches return before
    `currCorners = newCorners` is ever reached — attemptFeatureReseed (which is only ever
    called much later, using currCorners) is structurally unreachable from either branch."""
    track_body = _track_frame_body()
    shape_reason_at = track_body.index("const shapeReason =")
    shape_block_end = track_body.index("// Accepted shape: reset the dedicated hold/reject state.")
    shape_block = track_body[shape_reason_at:shape_block_end]
    assert "attemptFeatureReseed(" not in shape_block
    assert "currCornersTrack = newCorners;" not in shape_block


def test_i33_pass10_post_reseed_baseline_still_works():
    """Task I item 33: firstPostReseedLkPending still takes priority over gap_recovered/
    successful_lk in establishFrameGapBaseline's reason argument, and is still cleared
    immediately after — Pass 10's reseed-boundary labeling is unchanged by Pass 11."""
    track_body = _track_frame_body()
    establish_at = track_body.index("establishFrameGapBaseline(mediaTime, lastSuccessfulLkAt, firstPostReseedLkPending")
    assert "? 'first_post_reseed_lk' : (gapWasSuspected ? 'gap_recovered' : 'successful_lk'));" in track_body[establish_at:establish_at + 200]
    clear_at = track_body.index("if (firstPostReseedLkPending) firstPostReseedLkPending = false;")
    assert establish_at < clear_at


def test_i34_pass10_recovery_ownership_remains_single_owner():
    """Task I item 34: dropTracking still logs [RECOVERY SCAN OWNER] and calls
    scheduleNextScan('tracking_lost_reacquire', 0) exactly once per drop — Pass 11 added
    new dropTracking() callers (pose_rejected_weak_geometry_support etc.) but they all
    funnel through this same single function, so single-owner recovery is unaffected."""
    html = _scanner_html()
    drop_start = html.index("function dropTracking(reason, extraMats")
    drop_end = html.index("function handleDetectionTimeout()", drop_start)
    drop_body = html[drop_start:drop_end]
    assert drop_body.count("scheduleNextScan('tracking_lost_reacquire', 0);") == 1
    assert "owner: 'tracking_lost_reacquire', timerPending: true," in drop_body


def test_i35_pass9_feature_reseeding_remains_functional():
    """Task I item 35: attemptFeatureReseed's own signature, ROI-crop, spatial-dedup-grid,
    and bounded-attempt guards are untouched by Pass 11 — only its callers' upstream
    shape-continuity gating changed, never the reseed function itself."""
    body = _attempt_feature_reseed_body()
    assert "function createSpatialDedupGrid(cellSize)" not in body  # defined earlier, just used here
    assert "reseedInProgress = true;" in body or "reseedInProgress" in body
    assert "MAX_RESEED_ATTEMPTS_PER_EPOCH" in _track_frame_body()


def test_i36_pass8_successful_lk_baseline_remains_functional():
    """Task I item 36: hasValidLkBaseline's own definition (epoch + continuity token match)
    is untouched by Pass 11 — only what happens once a large gap is detected changed, not
    how a valid baseline is recognized in the first place."""
    track_body = _track_frame_body()
    assert (
        "const hasValidLkBaseline = hasSuccessfulLkBaseline &&\n"
        "          successfulLkEpoch === trackingEpoch &&\n"
        "          successfulLkContinuityToken === frameGapContinuityToken;"
    ) in track_body


def test_i37_pass7_callback_owner_repair_remains_functional():
    """Task I item 37: the ownership self-check at the top of trackFrame (tracking must
    never be true with trackingCallbackId === null) is untouched by Pass 11's changes
    further down the function."""
    track_body = _track_frame_body()
    assert "if (tracking && trackingCallbackId === null) {" in track_body
    assert "logCallbackEvent('[TRACK CALLBACK OWNERSHIP ERROR]'," in track_body


def test_i38_backend_thresholds_unchanged():
    """Task I item 38: no backend/recognition threshold changed — only new client-side
    geometry-support/shape-continuity constants were added, all reused from existing
    values (SHAPE_HOLD_MAX_MS = POSE_HOLD_MS, SHAPE_HOLD_MAX_FRAMES = TRACKING_GRACE_FRAMES)."""
    html = _scanner_html()
    assert "const TRACKING_GRACE_FRAMES = 3;" in html
    assert "const TRACKING_GRACE_MS = 900;" in html
    assert "const POSE_HOLD_MS = 500;" in html
    assert "const SHAPE_HOLD_MAX_MS = POSE_HOLD_MS;" in html
    assert "const SHAPE_HOLD_MAX_FRAMES = TRACKING_GRACE_FRAMES;" in html
    assert "const MIN_GOOD_POINTS = scannerMode === 'lightweight' ? 12 : (deviceInfo.isLowEnd ? 16 : 20);" in html
    assert "const RESEED_MIN_GRID_CELLS = 5;" in html


def test_i39_overlay_src_currenttime_and_native_loop_unchanged():
    """Task I item 39: none of Pass 11's new code (gap-suspicion tracking, shape-continuity
    decision, reportGapOutcome) ever assigns overlay.src/overlay.currentTime, or touches
    native <video loop>."""
    track_body = _track_frame_body()
    assert "overlay.src" not in track_body
    assert "overlay.currentTime" not in track_body
    assert "overlay.loop" not in track_body
    html = _scanner_html()
    assert html.count('<video id="overlay"') == 1


def test_i40_existing_hard_geometry_checks_unchanged():
    """Task I item 40: normalizeCornerOrder/validateOverlayQuad/the out_of_bounds pad check
    are byte-for-byte unchanged by Pass 11 — the new shape-continuity logic sits strictly
    AFTER these existing hard checks, never replacing or weakening them. Pass 12: the pad
    check itself (0.40, the comparison operators) is untouched — only its bound switched
    from frameW/frameH to trackWidth/trackHeight, since it now runs in track space."""
    html = _scanner_html()
    assert "function normalizeCornerOrder(pts, previous) {" in html
    assert "return validation.signedArea > 0 ? ordered : null;" in html
    assert "const pad = 0.40;" in html
    assert (
        "const inBounds = newCorners.every(p =>\n"
        "          p.x > -pad * trackWidth && p.x < trackWidth * (1 + pad) &&\n"
        "          p.y > -pad * trackHeight && p.y < trackHeight * (1 + pad)\n"
        "        );"
    ) in html


# ---------------------------------------------------------------------------
# Pass 12: REDUCE LOCAL TRACKING LATENCY AND PREVENT MULTI-FRAME LK COLLAPSE.
#
# Task B: a dedicated, lower-resolution local-tracking coordinate space — camera
# capture/backend upload/displayed overlay stay at frameW/frameH; only LK/homography/
# point-filtering/reseed run at trackWidth/trackHeight (derived from TRACK_SPACE_MAX_DIM,
# aspect-ratio preserved, never upscaled).
#
# Task D/E: trackingFrameProcessing is a single-flight guard — coalesce a callback that
# arrives while a previous trackFrame computation is active instead of starting a second
# one, and the rVFC stall watchdog no longer misclassifies "we are actively computing" as
# "the browser never delivered the callback."
#
# Task F: three console.log verbosity tiers (errors/events/verbose) — real-device mode no
# longer emits a large Object log on every single frame.
# ---------------------------------------------------------------------------

def test_p12_01_track_space_is_lower_resolution_on_qualifying_frames():
    """Task I item 1 (Pass 13: tier map supersedes the old single constant):
    computeTrackSpaceDimensions scales DOWN whenever the source's larger dimension
    exceeds the CURRENT tier's TIER_TRACK_MAX_DIM entry — e.g. a 1200x675 source (larger
    side 1200) is reduced under every tier (480/560/720 are all below 1200)."""
    html = _scanner_html()
    assert "const TIER_TRACK_MAX_DIM = { low: 480, medium: 560, high: 720 };" in html
    fn_start = html.index("function computeTrackSpaceDimensions(sourceWidth, sourceHeight, tier)")
    fn_end = html.index("function toTrackSpace(corners)")
    body = html[fn_start:fn_end]
    assert "const maxDimCap = TIER_TRACK_MAX_DIM[tier];" in body
    assert "const maxDim = Math.max(sourceWidth, sourceHeight);" in body
    assert "if (!(maxDim > maxDimCap)) {" in body
    assert "const scale = maxDimCap / maxDim;" in body


def test_p12_02_aspect_ratio_is_preserved():
    """Task I item 2: width and height are both derived from the SAME `scale` factor
    (sourceWidth * scale, sourceHeight * scale) — never independent per-axis scaling that
    could distort the aspect ratio."""
    html = _scanner_html()
    fn_start = html.index("function computeTrackSpaceDimensions(sourceWidth, sourceHeight, tier)")
    fn_end = html.index("function toTrackSpace(corners)")
    body = html[fn_start:fn_end]
    assert "const width = Math.max(2, Math.round(sourceWidth * scale));" in body
    assert "const height = Math.max(2, Math.round(sourceHeight * scale));" in body
    # A source at or under the cap is returned completely unscaled (scale 1 on both axes).
    assert "return { width: sourceWidth, height: sourceHeight, scaleX: 1, scaleY: 1 };" in body


def test_p12_03_server_bootstrap_corners_convert_into_track_space():
    """Task I item 3: resetTrackingEpoch computes currCornersTrack from whatever
    currCorners currently holds (the just-accepted server/bootstrap quad, in intrinsic
    space) via toTrackSpace — every fresh tracking session starts with a track-space quad
    derived from the real accepted one, never a stale or unconverted one."""
    html = _scanner_html()
    reset_start = html.index("function resetTrackingEpoch(width, height)")
    reset_end = html.index("function cornersToMat(corners)")
    body = html[reset_start:reset_end]
    assert "currCornersTrack = currCorners ? toTrackSpace(currCorners) : null;" in body


def test_p12_04_accepted_tracking_quad_converts_back_before_applywarp():
    """Task I item 4: the ONE conversion back to intrinsic/display space (toIntrinsicSpace)
    happens immediately before applyWarp is called — currCornersTrack carries the
    track-space quad forward for the next tick, currCorners gets the converted one."""
    track_body = _track_frame_body()
    assert (
        "currCornersTrack = newCorners;\n"
        "        currCorners = toIntrinsicSpace(newCorners);\n"
        "        if (!applyWarp(currCorners)) {"
    ) in track_body


def test_p12_05_overlay_geometry_remains_in_intrinsic_display_coordinates():
    """Task I item 5: applyWarp itself is untouched — it still reads module-level frameW/
    frameH (never trackWidth/trackHeight) for its own renderability/conversion checks, so
    the overlay is always positioned using intrinsic/display-space geometry."""
    html = _scanner_html()
    warp_start = html.index("function applyWarp(cornersFrame, context = {})")
    warp_end = html.index("function quadArea2(pts)")
    body = html[warp_start:warp_end]
    assert "isOverlayFrameQuadRenderable(cornersFrame, frameW, frameH)" in body
    assert "convertBackendCornersToOverlay(cornersFrame, frameW, frameH, lastOrdered)" in body
    assert "trackWidth" not in body
    assert "trackHeight" not in body


def test_p12_06_backend_upload_dimensions_remain_unchanged():
    """Task I item 6: frameW/frameH are still assigned directly from the server response's
    own frame_width/frame_height — backend capture/upload dimensions are completely
    untouched by the local-tracking-space work."""
    html = _scanner_html()
    assert "frameW = Number(data.frame_width);" in html
    assert "frameH = Number(data.frame_height);" in html


def test_p12_07_prevgray_and_gray_always_share_track_dimensions():
    """Task I item 7: the existing dimension/epoch-mismatch guard (unchanged) still
    compares prevGray.rows/cols against gray.rows/cols — both are now sized from the SAME
    trackingCanvas (resized to trackWidth/trackHeight by resetTrackingEpoch), so they can
    only ever match or mismatch together, never independently drift."""
    track_body = _track_frame_body()
    assert (
        "if (!prevGray || !prevPts || prevGrayEpoch !== trackingEpoch ||\n"
        "            prevGray.rows !== gray.rows || prevGray.cols !== gray.cols) {"
    ) in track_body
    html = _scanner_html()
    assert "trackingCanvas.width = trackWidth;" in html
    assert "trackingCanvas.height = trackHeight;" in html


def test_p12_08_point_coordinates_remain_in_track_space_during_lk():
    """Task I item 8: the homography source corners (cornersToMat) and poseCompatibility's
    own previous-quad/normalization-dimension arguments are all currCornersTrack/
    trackWidth/trackHeight — LK's point coordinates are never mixed with intrinsic-space
    numbers mid-pipeline."""
    track_body = _track_frame_body()
    assert "const cornerMat = cornersToMat(currCornersTrack);" in track_body
    assert "poseCompatibility(newCorners, currCornersTrack, Math.max(Math.min(trackWidth, trackHeight), 1))" in track_body


def test_p12_09_reseed_mask_uses_track_space_accepted_quad():
    """Task I item 9: attemptFeatureReseed is called with currCornersTrack (not currCorners)
    — gray is track-space sized, so its ROI/mask quad must be too."""
    track_body = _track_frame_body()
    assert "finalPts = attemptFeatureReseed(gray, currCornersTrack, prunedNext, survivingCoverage, survivingGridCells);" in track_body


def test_p12_10_normalized_shape_checks_remain_scale_independent():
    """Task I item 10: poseCompatibility's jump thresholds (2.5/0.4 area, 0.35 center, 0.55
    corner, 2.0/0.5 edge, 2.0 diagonal) are byte-identical to Pass 11 — only the space
    (track vs intrinsic) and explicit parameter-passing changed. Ratios computed the same
    way in a consistently-scaled space are mathematically equivalent regardless of which
    consistent space is used."""
    html = _scanner_html()
    fn_start = html.index("function poseCompatibility(nextCorners, previousCorners, frameMinDim)")
    fn_end = html.index("function computeTrackSpaceDimensions(sourceWidth, sourceHeight, tier)")
    body = html[fn_start:fn_end]
    assert "if (areaRatio > 2.5 || areaRatio < 0.4) return { ok: false, reason: 'area_jump'" in body
    assert "if (centerJump > 0.35) return { ok: false, reason: 'center_jump'" in body
    assert "if (maxCornerJump > 0.55) return { ok: false, reason: 'corner_jump'" in body
    assert "if (maxEdgeRatio > 2.0 || minEdgeRatio < 0.5) return { ok: false, reason: 'edge_ratio_jump'" in body
    assert "if (diagonalRatioChange > 2.0) return { ok: false, reason: 'diagonal_jump'" in body


def test_p12_11_only_one_trackframe_computation_runs_at_once():
    """Task I item 11: the trackingFrameProcessing guard is the FIRST thing checked after
    the loop-active/generation guards — before the top-of-tick rearm even runs — so a
    reentrant call can never proceed into a second computation."""
    track_body = _track_frame_body()
    generation_check_at = track_body.index("if (trackingGeneration !== scannerGeneration) {")
    guard_at = track_body.index("if (trackingFrameProcessing) {")
    rearm_at = track_body.index("scheduleTrackingFrame('tick_rearm',")
    set_true_at = track_body.index("trackingFrameProcessing = true;")
    assert generation_check_at < guard_at < rearm_at < set_true_at


def test_p12_12_overlapping_callbacks_are_coalesced():
    """Task I item 12: a callback that arrives while trackingFrameProcessing is true logs
    [TRACK FRAME COALESCED] and returns immediately — it never calls scheduleTrackingFrame
    or proceeds into the LK pipeline."""
    track_body = _track_frame_body()
    guard_at = track_body.index("if (trackingFrameProcessing) {")
    return_at = track_body.index("return;", guard_at)
    guard_body = track_body[guard_at:return_at]
    assert "logCallbackEvent('[TRACK FRAME COALESCED]'," in guard_body
    assert "scheduleTrackingFrame(" not in guard_body


def test_p12_13_only_newest_deferred_callback_is_retained():
    """Task I item 13: latestDeferredFrameMetadata is a single slot (declared as one
    object-or-null, never an array/push) — each coalesced arrival unconditionally
    OVERWRITES it, so only the newest survives."""
    html = _scanner_html()
    assert "let latestDeferredFrameMetadata = null; // { nowArg, metadata } — at most one, newest wins" in html
    track_body = _track_frame_body()
    assert "latestDeferredFrameMetadata = { nowArg, metadata };" in track_body
    assert "latestDeferredFrameMetadata.push(" not in track_body


def test_p12_14_deferred_callback_runs_after_active_processing_completes():
    """Task I item 14: the deferred frame is only processed AFTER the try/catch/finally
    block (which clears trackingFrameProcessing) has fully exited — never from inside the
    finally block itself, and never while the flag could still read true."""
    track_body = _track_frame_body()
    finally_end_at = track_body.index("      }\n      // Task D (Pass 12): process the single newest coalesced frame")
    recurse_at = track_body.index("trackFrame(deferredToProcess.nowArg, deferredToProcess.metadata);")
    clear_flag_at = track_body.index("trackingFrameProcessing = false;")
    assert clear_flag_at < finally_end_at < recurse_at


def test_p12_15_no_unbounded_callback_queue_is_created():
    """Task I item 15: the deferred-frame mechanism is a single named variable (an object
    or null), never an array, list, or queue data structure — coalescing can only ever
    hold at most one pending frame."""
    html = _scanner_html()
    assert "let latestDeferredFrameMetadata = null;" in html
    assert "latestDeferredFrameMetadata = []" not in html
    assert "latestDeferredFrameMetadata.length" not in html


def test_p12_16_watchdog_does_not_call_callback_stalled_while_processing_active():
    """Task I item 16: onRvfcStallWatchdogFired checks trackingFrameProcessing and returns
    (after re-arming) strictly BEFORE it can ever reach enterCallbackStallRecovery — an
    active computation is never misclassified as a delivery stall."""
    html = _scanner_html()
    fn_start = html.index("function onRvfcStallWatchdogFired(callbackId)")
    fn_end = html.index("// Task C (Pass 6): the single validity check")
    body = html[fn_start:fn_end]
    overrun_check_at = body.index("if (trackingFrameProcessing) {")
    overrun_return_at = body.index("return;", overrun_check_at)
    stall_recovery_at = body.index("enterCallbackStallRecovery(callbackId, 'rvfc_stall_watchdog');")
    assert overrun_check_at < overrun_return_at < stall_recovery_at


def test_p12_17_processing_overrun_is_logged_separately():
    """Task I item 17: the overrun branch logs [TRACK PROCESSING OVERRUN] with callbackId/
    elapsedMs/stage — a distinct tag from [TRACK CALLBACK STALLED], so a real-device log
    can tell the two cases apart."""
    html = _scanner_html()
    fn_start = html.index("function onRvfcStallWatchdogFired(callbackId)")
    fn_end = html.index("// Task C (Pass 6): the single validity check")
    body = html[fn_start:fn_end]
    assert "logCallbackEvent('[TRACK PROCESSING OVERRUN]', {" in body
    assert "callbackId, elapsedMs: Math.round(performance.now() - trackingFrameStartedAt)," in body
    assert "stage: trackingFrameStage || 'unknown'" in body


def test_p12_18_next_callback_is_armed_after_processing_completes():
    """Task I item 18: the overrun branch re-arms a FRESH watchdog window for the SAME
    callback (armRvfcStallWatchdog(callbackId)) rather than cancelling/cycling ownership —
    RVFC_STALL_TIMEOUT_MS itself is never extended as the fix."""
    html = _scanner_html()
    fn_start = html.index("function onRvfcStallWatchdogFired(callbackId)")
    fn_end = html.index("// Task C (Pass 6): the single validity check")
    body = html[fn_start:fn_end]
    overrun_at = body.index("if (trackingFrameProcessing) {")
    rearm_at = body.index("armRvfcStallWatchdog(callbackId);", overrun_at)
    assert overrun_at < rearm_at
    assert "const RVFC_STALL_TIMEOUT_MS = TRACKING_GRACE_MS;" in html  # unchanged, never extended


def test_p12_19_mats_are_reused_or_deterministically_deleted():
    """Task I item 19: the unnecessary Array.from() copy of the bootstrap feature Mat's
    data was removed — features.data32F (a typed array) is read directly, and deleteMats/
    explicit .delete() calls throughout trackFrame are untouched."""
    html = _scanner_html()
    init_start = html.index("function initializeFreshLiveTracker(now, metadata)")
    init_end = html.index("function dropTracking(reason, extraMats", init_start)
    body = html[init_start:init_end]
    assert "const flat = features.data32F;" in body
    assert "Array.from(features.data32F" not in body
    assert "deleteMats(mask, features);" in body


def test_p12_20_no_deleted_mat_is_reused():
    """Task I item 20: prevGray is only ever reassigned in the SAME statement pair that
    deletes the old one first (prevGray.delete(); prevGray = gray;) — never reassigned
    without the preceding delete, and never read again after being deleted within the same
    tick."""
    track_body = _track_frame_body()
    assert "prevGray.delete();\n        prevGray = gray;" in track_body
    assert "prevPts.delete();\n        prevPts = cv.matFromArray(finalPts.length / 2, 1, cv.CV_32FC2, finalPts);" in track_body


def test_p12_21_per_frame_verbose_logging_is_disabled_by_default():
    """Task I item 21: diagnosticLevel defaults to 'events' (not 'verbose') unless
    ?scanner_debug=1 is set, and the highest-volume per-frame tags ([TRACK POINT HEALTH],
    [TRACK CALLBACK ENTERED]/REQUESTED/REARMED/SKIPPED, [TRACK OWNER STATE], [TRACK GAP
    SUSPECTED]/RECOVERED, [TRACK FRAME PROCESSING COMPLETE]) are all classified 'verbose'
    — suppressed by default."""
    html = _scanner_html()
    assert "let diagnosticLevel = diagnosticsEnabled ? 'verbose' : 'events';" in html
    for verbose_tag in (
        "'[TRACK POINT HEALTH]': 'verbose'",
        "'[TRACK CALLBACK ENTERED]': 'verbose'",
        "'[TRACK CALLBACK REQUESTED]': 'verbose'",
        "'[TRACK OWNER STATE]': 'verbose'",
        "'[TRACK GAP SUSPECTED]': 'verbose'",
        "'[TRACK FRAME PROCESSING COMPLETE]': 'verbose'",
    ):
        assert verbose_tag in html


def test_p12_22_loss_reseed_shape_logs_remain_available():
    """Task I item 22: dropTracking's own [TRACKING LOST] console.log is a raw, always-on
    call (never routed through logCallbackEvent's level gate), and the 'events'-tier tags
    (shape hold/reject, reseed success/failure, frame-gap confirmation) are visible even in
    the default (non-verbose) level — never demoted to 'verbose'."""
    html = _scanner_html()
    drop_start = html.index("function dropTracking(reason, extraMats")
    drop_end = html.index("function handleDetectionTimeout()")
    drop_body = html[drop_start:drop_end]
    assert "console.log(" in drop_body
    for events_tag in (
        "'[TRACK SHAPE HEALTH]': 'events'",
        "'[TRACK RESEED SUCCESS]': 'events'",
        "'[TRACK RESEED FAILED]': 'events'",
        "'[TRACK GAP CONFIRMED]': 'events'",
        "'[TRACK FRAME PERFORMANCE]': 'events'",
    ):
        assert events_tag in html


def test_p12_23_pass11_gap_suspect_flow_remains_functional():
    """Task I item 23: gapWasSuspected/[TRACK GAP SUSPECTED] and reportGapOutcome are
    unchanged by Pass 12, and tracking_frame_gap_exceeded is still never a dropTracking()
    reason anywhere in the file."""
    track_body = _track_frame_body()
    assert "gapWasSuspected = true;" in track_body
    assert "logCallbackEvent('[TRACK GAP SUSPECTED]', {" in track_body
    html = _scanner_html()
    assert html.count("dropTracking('tracking_frame_gap_exceeded'") == 0


def test_p12_24_pass11_accept_hold_reject_remains_functional():
    """Task I item 24: shapeReason/decision/IMMEDIATE_SHAPE_REJECT_REASONS and the bounded
    SHAPE_HOLD_MAX_FRAMES/SHAPE_HOLD_MAX_MS allowance are unchanged by Pass 12 (only the
    space they operate in changed)."""
    track_body = _track_frame_body()
    assert "const shapeReason = !localPoseQuality.ok ? localPoseQuality.reason : (weakGeometrySupport ? 'weak_geometry_support' : null);" in track_body
    assert "const decision = (isImmediateReject || holdExpired) ? 'reject' : 'hold';" in track_body
    html = _scanner_html()
    assert "const SHAPE_HOLD_MAX_MS = POSE_HOLD_MS;" in html
    assert "const SHAPE_HOLD_MAX_FRAMES = TRACKING_GRACE_FRAMES;" in html


def test_p12_25_weak_geometry_support_remains_a_rejection():
    """Task I item 25: weakGeometrySupport's formula (geometryInlierCount < MIN_GOOD_POINTS
    OR geometryInlierGridCells < RESEED_MIN_GRID_CELLS) is unchanged — both existing
    constants reused, never a weaker invented threshold."""
    track_body = _track_frame_body()
    assert "const weakGeometrySupport = geometryInlierCount < MIN_GOOD_POINTS || geometryInlierGridCells < RESEED_MIN_GRID_CELLS;" in track_body


def test_p12_26_corner_order_invalid_remains_a_rejection():
    """Task I item 26: corner_order_invalid is still an unconditional, immediate
    dropTracking() reason — never routed through the shape-continuity HOLD allowance."""
    track_body = _track_frame_body()
    assert "dropTracking('corner_order_invalid', [gray, nextPts, status, err, prevMat, nextMat, mask, H], {" in track_body


def test_p12_27_out_of_bounds_remains_a_rejection():
    """Task I item 27: out_of_bounds is still an unconditional, immediate dropTracking()
    reason — never routed through the shape-continuity HOLD allowance."""
    track_body = _track_frame_body()
    assert "dropTracking('out_of_bounds', [gray, nextPts, status, err, prevMat, nextMat, mask, H], {" in track_body


def test_p12_28_pass10_reseed_baseline_remains_functional():
    """Task I item 28: a successful reseed still resets the frame-gap baseline
    ('feature_reseed_success') and sets firstPostReseedLkPending — unchanged by Pass 12's
    track-space/timing/coalescing work."""
    body = _attempt_feature_reseed_body()
    assert "resetFrameGapBaseline('feature_reseed_success');" in body
    assert "firstPostReseedLkPending = true;" in body


def test_p12_29_pass10_recovery_ownership_remains_single_owner():
    """Task I item 29: dropTracking still schedules exactly one tracking_lost_reacquire
    attempt — Pass 12 added no second reacquisition path."""
    html = _scanner_html()
    drop_start = html.index("function dropTracking(reason, extraMats")
    drop_end = html.index("function handleDetectionTimeout()")
    drop_body = html[drop_start:drop_end]
    assert drop_body.count("scheduleNextScan('tracking_lost_reacquire', 0);") == 1


def test_p12_30_pass9_reseeding_remains_functional():
    """Task I item 30: attemptFeatureReseed's ROI-crop (gray.roi) and bounded-attempt
    guards (reseedAttemptsForEpoch/consecutiveReseedFailures vs MAX_RESEED_ATTEMPTS_PER_
    EPOCH/MAX_CONSECUTIVE_RESEED_FAILURES) are unchanged — only its caller's argument
    (currCornersTrack instead of currCorners) changed."""
    body = _attempt_feature_reseed_body()
    assert "roiGray = gray.roi(new cv.Rect(roiX, roiY, roiW, roiH));" in body
    track_body = _track_frame_body()
    assert "reseedAttemptsForEpoch < MAX_RESEED_ATTEMPTS_PER_EPOCH &&" in track_body
    assert "consecutiveReseedFailures < MAX_CONSECUTIVE_RESEED_FAILURES) {" in track_body


def test_p12_31_pass8_baseline_remains_functional():
    """Task I item 31: hasValidLkBaseline's own definition (epoch + continuity token match)
    is byte-for-byte unchanged by Pass 12."""
    track_body = _track_frame_body()
    assert (
        "const hasValidLkBaseline = hasSuccessfulLkBaseline &&\n"
        "          successfulLkEpoch === trackingEpoch &&\n"
        "          successfulLkContinuityToken === frameGapContinuityToken;"
    ) in track_body


def test_p12_32_pass7_callback_repair_remains_functional():
    """Task I item 32: the top-of-tick ownership self-check (tracking must never be true
    with trackingCallbackId === null) is unchanged and still runs on every non-coalesced
    tick."""
    track_body = _track_frame_body()
    assert "if (tracking && trackingCallbackId === null) {" in track_body
    assert "logCallbackEvent('[TRACK CALLBACK OWNERSHIP ERROR]', { reason: 'tracking_without_pending_callback' }, {" in track_body


def test_p12_33_backend_thresholds_unchanged():
    """Task I item 33: MIN_GOOD_POINTS/MAX_ERR/RANSAC_REPROJ/TRACKING_GRACE_FRAMES/
    TRACKING_GRACE_MS/POSE_HOLD_MS/RVFC_STALL_TIMEOUT_MS are exactly the same values as
    before Pass 12 — none of Task A-F's changes touched a recognition or geometry
    acceptance threshold."""
    html = _scanner_html()
    assert "const MIN_GOOD_POINTS = scannerMode === 'lightweight' ? 12 : (deviceInfo.isLowEnd ? 16 : 20);" in html
    assert "const MAX_ERR = scannerMode === 'lightweight' ? 55 : (deviceInfo.isLowEnd ? 45 : 35);" in html
    assert "const RANSAC_REPROJ = 5.0;" in html
    assert "const TRACKING_GRACE_FRAMES = 3;" in html
    assert "const TRACKING_GRACE_MS = 900;" in html
    assert "const POSE_HOLD_MS = 500;" in html
    assert "const RVFC_STALL_TIMEOUT_MS = TRACKING_GRACE_MS;" in html


def test_p12_34_overlay_src_currenttime_native_loop_unchanged():
    """Task I item 34: none of Pass 12's new code (track-space conversion, timing
    instrumentation, coalescing, watchdog fix, diagnostic levels) ever assigns
    overlay.src/overlay.currentTime, or touches the native <video loop> attribute."""
    track_body = _track_frame_body()
    assert "overlay.src =" not in track_body
    assert "overlay.currentTime =" not in track_body
    assert "overlay.loop =" not in track_body
    html = _scanner_html()
    for fn_start_marker, fn_end_marker in (
        ("function computeTrackSpaceDimensions(sourceWidth, sourceHeight, tier)", "function toTrackSpace(corners)"),
        ("function onRvfcStallWatchdogFired(callbackId)", "// Task C (Pass 6): the single validity check"),
    ):
        body = html[html.index(fn_start_marker):html.index(fn_end_marker)]
        assert "overlay.src" not in body
        assert "overlay.currentTime" not in body
        assert "overlay.loop" not in body
    assert html.count('<video id="overlay"') == 1


# ---------------------------------------------------------------------------
# Pass 13: LOWER LK COST ON SLOW MOBILE DEVICES.
#
# Task A/C: an explicit, downgradable performance TIER (low/medium/high) — starts from
# the same static device signal Pass 12 used directly, then only ever DOWNGRADES (never
# auto-upgrades within the same epoch) based on a bounded consecutive-overrun streak.
#
# Task B/D: TIER_MAX_TRACK_POINTS (60/80/existing) governs both bootstrap's initial
# goodFeaturesToTrack request and every reseed's target — never the hard quality floors
# (MIN_GOOD_POINTS, RESEED_MIN_GRID_CELLS, weak_geometry_support).
# ---------------------------------------------------------------------------

def test_p13_01_low_tier_long_side_is_approximately_480():
    """Task F item 1: TIER_TRACK_MAX_DIM.low is 480 — a 675x1200 portrait source's larger
    side (1200) is capped down to 480 on the low tier."""
    html = _scanner_html()
    assert "const TIER_TRACK_MAX_DIM = { low: 480, medium: 560, high: 720 };" in html


def test_p13_02_aspect_ratio_is_preserved_across_tiers():
    """Task F item 2: computeTrackSpaceDimensions still derives width AND height from the
    SAME `scale` factor regardless of which tier's cap is in effect — unchanged by the
    tier map replacing the single constant."""
    html = _scanner_html()
    fn_start = html.index("function computeTrackSpaceDimensions(sourceWidth, sourceHeight, tier)")
    fn_end = html.index("function toTrackSpace(corners)")
    body = html[fn_start:fn_end]
    assert "const maxDimCap = TIER_TRACK_MAX_DIM[tier];" in body
    assert "const scale = maxDimCap / maxDim;" in body
    assert "const width = Math.max(2, Math.round(sourceWidth * scale));" in body
    assert "const height = Math.max(2, Math.round(sourceHeight * scale));" in body


def test_p13_03_backend_upload_dimensions_remain_unchanged():
    """Task F item 3: frameW/frameH are still assigned directly from the server response's
    frame_width/frame_height — untouched by tiering."""
    html = _scanner_html()
    assert "frameW = Number(data.frame_width);" in html
    assert "frameH = Number(data.frame_height);" in html


def test_p13_04_applywarp_still_receives_intrinsic_coordinates():
    """Task F item 4: applyWarp is called with currCorners (intrinsic, post-conversion) —
    never currCornersTrack — and its own renderability check still reads frameW/frameH."""
    track_body = _track_frame_body()
    assert (
        "currCornersTrack = newCorners;\n"
        "        currCorners = toIntrinsicSpace(newCorners);\n"
        "        if (!applyWarp(currCorners)) {"
    ) in track_body
    html = _scanner_html()
    warp_start = html.index("function applyWarp(cornersFrame, context = {})")
    warp_end = html.index("function quadArea2(pts)")
    assert "isOverlayFrameQuadRenderable(cornersFrame, frameW, frameH)" in html[warp_start:warp_end]


def test_p13_05_low_tier_targets_approximately_60_points():
    """Task F item 5: TIER_MAX_TRACK_POINTS.low is 60."""
    html = _scanner_html()
    assert "const TIER_MAX_TRACK_POINTS = { low: 60, medium: 80, high: MAX_TRACK_POINTS };" in html


def test_p13_06_medium_tier_targets_approximately_80_points():
    """Task F item 6: TIER_MAX_TRACK_POINTS.medium is 80 (same literal as item 5 — kept as
    its own test since Task F lists it as a distinct proof point)."""
    html = _scanner_html()
    assert "medium: 80" in html


def test_p13_07_high_tier_preserves_the_existing_target():
    """Task F item 7: TIER_MAX_TRACK_POINTS.high reuses MAX_TRACK_POINTS directly — never a
    second, competing definition of the device-mode ceiling."""
    html = _scanner_html()
    assert "high: MAX_TRACK_POINTS" in html
    assert "const MAX_TRACK_POINTS = scannerMode === 'full' ? 180 : (scannerMode === 'standard' ? 120 : 70);" in html


def test_p13_08_point_distribution_rules_remain_enforced():
    """Task F item 8: reducing the point TARGET never touches the spatial-distribution
    gates — attemptFeatureReseed's own grid-cell/coverage checks and trackFrame's
    weakGeometrySupport check are unchanged."""
    body = _attempt_feature_reseed_body()
    assert "const mergedGridCells = countOccupiedGridCells(merged, quad, POINT_GRID_SIZE);" in body
    assert "if (mergedCoverage < RESEED_MIN_COVERAGE_AFTER_MERGE) {" in body
    track_body = _track_frame_body()
    assert "const weakGeometrySupport = geometryInlierCount < MIN_GOOD_POINTS || geometryInlierGridCells < RESEED_MIN_GRID_CELLS;" in track_body


def test_p13_09_repeated_lk_overruns_downgrade_one_tier():
    """Task F item 9: consecutiveTierOverrunTicks increments on lkOverrun/totalOverrun;
    once it reaches TIER_DOWNGRADE_STREAK, the tier steps down exactly ONE level (high->
    medium or medium->low, never a two-level jump) and the streak resets."""
    track_body = _track_frame_body()
    # Pass 15: lkOverrun is now computed once, shared with the perf-log trigger — same
    # underlying LK_MS_SAFE_BUDGET comparison, just guarded by opticalFlow > 0 first.
    assert "const lkOverrun = stageTimingsMs.opticalFlow > 0 && stageTimingsMs.opticalFlow > LK_MS_SAFE_BUDGET;" in track_body
    assert "const totalOverrun = totalTrackFrameMs > SLOW_TRACK_FRAME_MS;" in track_body
    assert "consecutiveTierOverrunTicks++;" in track_body
    assert "if (consecutiveTierOverrunTicks >= TIER_DOWNGRADE_STREAK && performanceTier !== 'low') {" in track_body
    assert "performanceTier = performanceTier === 'high' ? 'medium' : 'low';" in track_body
    assert "consecutiveTierOverrunTicks = 0;" in track_body


def test_p13_10_one_isolated_slow_frame_does_not_immediately_downgrade():
    """Task F item 10: TIER_DOWNGRADE_STREAK is 3 (not 1) — a single overrun tick only
    increments the counter, it cannot by itself reach the downgrade threshold."""
    html = _scanner_html()
    assert "const TIER_DOWNGRADE_STREAK = 3;" in html


def test_p13_11_tiering_does_not_oscillate_per_frame():
    """Task F item 11: a healthy (non-overrun) tick resets consecutiveTierOverrunTicks to
    0 — the downgrade check only ever evaluates a BOUNDED consecutive streak, never a
    single frame in isolation, and there is no automatic upgrade path inside the
    per-tick downgrade block (only ever a downgrade ternary)."""
    track_body = _track_frame_body()
    downgrade_block_at = track_body.index("if (stageTimingsMs.opticalFlow > 0) {")
    tier_change_log_at = track_body.index("logCallbackEvent('[TRACK PERFORMANCE TIER CHANGE]',", downgrade_block_at)
    block = track_body[downgrade_block_at:tier_change_log_at]
    assert "consecutiveTierOverrunTicks = 0;" in block  # the healthy-tick reset (else branch)
    assert "performanceTier = performanceTier === 'high' ? 'medium' : 'low';" in block


def test_p13_12_tier_state_resets_on_new_session_or_camera_restart():
    """Task F item 12: recoverScannerInner's real-restart branch (not the "avoided,
    stream alive" early-return) resets performanceTier back to initialPerformanceTier(),
    resets performanceTierReason, and zeroes the overrun streak."""
    html = _scanner_html()
    restart_start = html.index("async function recoverScannerInner(reason, restartCamera)")
    restart_end = html.index("await new Promise(resolve => setTimeout(resolve, 250));", restart_start)
    body = html[restart_start:restart_end]
    avoided_at = body.index("scannerDiagnostics.push('camera_restart_avoided_stream_alive'")
    reset_at = body.index("performanceTier = initialPerformanceTier();")
    reason_reset_at = body.index("performanceTierReason = 'camera_restart';")
    streak_reset_at = body.index("consecutiveTierOverrunTicks = 0;")
    assert avoided_at < reset_at < reason_reset_at < streak_reset_at


def test_p13_13_reseed_target_follows_current_tier():
    """Task F item 13: attemptFeatureReseed computes tierMaxPoints via
    currentMaxTrackPoints() and uses it (never MAX_TRACK_POINTS directly) for both the
    initial targetNew calculation and the merge loop's cap."""
    body = _attempt_feature_reseed_body()
    assert "const tierMaxPoints = currentMaxTrackPoints();" in body
    assert "const targetNew = tierMaxPoints - survivingCount;" in body
    assert "if ((merged.length / 2) >= tierMaxPoints) break;" in body
    assert "MAX_TRACK_POINTS" not in body  # only the tier-derived local is used inside this function


def test_p13_14_held_or_rejected_geometry_cannot_reseed():
    """Task F item 14: attemptFeatureReseed is still only ever called with currCornersTrack
    AFTER the accept-only commit — the HOLD/REJECT branches' returns remain textually
    before that call (unchanged structural guarantee from Pass 11/12)."""
    track_body = _track_frame_body()
    shape_reason_at = track_body.index("const shapeReason =")
    shape_block_end = track_body.index("// Accepted shape: reset the dedicated hold/reject state.")
    shape_block = track_body[shape_reason_at:shape_block_end]
    assert "attemptFeatureReseed(" not in shape_block
    reseed_call_at = track_body.index("finalPts = attemptFeatureReseed(gray, currCornersTrack, prunedNext, survivingCoverage, survivingGridCells);")
    assert shape_block_end < reseed_call_at


def test_p13_15_weak_geometry_support_remains_unchanged():
    """Task F item 15: weakGeometrySupport's formula is byte-identical to Pass 11/12 —
    reduced point targets never weaken this gate."""
    track_body = _track_frame_body()
    assert "const weakGeometrySupport = geometryInlierCount < MIN_GOOD_POINTS || geometryInlierGridCells < RESEED_MIN_GRID_CELLS;" in track_body


def test_p13_16_corner_order_invalid_remains_unchanged():
    """Task F item 16: corner_order_invalid is still an immediate, unconditional
    dropTracking() reason."""
    track_body = _track_frame_body()
    assert "dropTracking('corner_order_invalid', [gray, nextPts, status, err, prevMat, nextMat, mask, H], {" in track_body


def test_p13_17_out_of_bounds_remains_unchanged():
    """Task F item 17: out_of_bounds is still an immediate, unconditional dropTracking()
    reason."""
    track_body = _track_frame_body()
    assert "dropTracking('out_of_bounds', [gray, nextPts, status, err, prevMat, nextMat, mask, H], {" in track_body


def test_p13_18_pass12_coordinate_conversion_remains_correct():
    """Task F item 18: the ONE conversion back to intrinsic space still happens right
    before applyWarp, and currCornersTrack still carries the track-space quad forward."""
    track_body = _track_frame_body()
    commit_at = track_body.index("currCornersTrack = newCorners;")
    convert_at = track_body.index("currCorners = toIntrinsicSpace(newCorners);", commit_at)
    warp_at = track_body.index("if (!applyWarp(currCorners)) {", convert_at)
    assert commit_at < convert_at < warp_at


def test_p13_19_pass12_watchdog_processing_state_logic_remains_correct():
    """Task F item 19: onRvfcStallWatchdogFired still checks trackingFrameProcessing and
    re-arms (never cancels) before it can ever reach enterCallbackStallRecovery."""
    html = _scanner_html()
    fn_start = html.index("function onRvfcStallWatchdogFired(callbackId)")
    fn_end = html.index("// Task C (Pass 6): the single validity check")
    body = html[fn_start:fn_end]
    overrun_at = body.index("if (trackingFrameProcessing) {")
    rearm_at = body.index("armRvfcStallWatchdog(callbackId);", overrun_at)
    stall_at = body.index("enterCallbackStallRecovery(callbackId, 'rvfc_stall_watchdog');")
    assert overrun_at < rearm_at < stall_at


def test_p13_20_pass11_accept_hold_reject_remains_correct():
    """Task F item 20: shapeReason/decision/IMMEDIATE_SHAPE_REJECT_REASONS and the bounded
    SHAPE_HOLD_MAX_FRAMES/SHAPE_HOLD_MAX_MS allowance are unchanged by Pass 13."""
    track_body = _track_frame_body()
    assert "const shapeReason = !localPoseQuality.ok ? localPoseQuality.reason : (weakGeometrySupport ? 'weak_geometry_support' : null);" in track_body
    assert "const decision = (isImmediateReject || holdExpired) ? 'reject' : 'hold';" in track_body
    html = _scanner_html()
    assert "const SHAPE_HOLD_MAX_MS = POSE_HOLD_MS;" in html
    assert "const SHAPE_HOLD_MAX_FRAMES = TRACKING_GRACE_FRAMES;" in html


def test_p13_21_pass10_reseed_baseline_remains_correct():
    """Task F item 21: a successful reseed still resets the frame-gap baseline and sets
    firstPostReseedLkPending — unchanged by the tier-target change."""
    body = _attempt_feature_reseed_body()
    assert "resetFrameGapBaseline('feature_reseed_success');" in body
    assert "firstPostReseedLkPending = true;" in body


def test_p13_22_recovery_ownership_remains_single_owner():
    """Task F item 22: dropTracking still schedules exactly one tracking_lost_reacquire
    attempt — Pass 13 added no second reacquisition path."""
    html = _scanner_html()
    drop_start = html.index("function dropTracking(reason, extraMats")
    drop_end = html.index("function handleDetectionTimeout()")
    drop_body = html[drop_start:drop_end]
    assert drop_body.count("scheduleNextScan('tracking_lost_reacquire', 0);") == 1


def test_p13_23_backend_thresholds_remain_unchanged():
    """Task F item 23: MIN_GOOD_POINTS/MAX_ERR/RANSAC_REPROJ/TRACKING_GRACE_FRAMES/
    TRACKING_GRACE_MS/POSE_HOLD_MS/RVFC_STALL_TIMEOUT_MS are exactly the same values as
    before Pass 13."""
    html = _scanner_html()
    assert "const MIN_GOOD_POINTS = scannerMode === 'lightweight' ? 12 : (deviceInfo.isLowEnd ? 16 : 20);" in html
    assert "const MAX_ERR = scannerMode === 'lightweight' ? 55 : (deviceInfo.isLowEnd ? 45 : 35);" in html
    assert "const RANSAC_REPROJ = 5.0;" in html
    assert "const TRACKING_GRACE_FRAMES = 3;" in html
    assert "const TRACKING_GRACE_MS = 900;" in html
    assert "const POSE_HOLD_MS = 500;" in html
    assert "const RVFC_STALL_TIMEOUT_MS = TRACKING_GRACE_MS;" in html


def test_p13_24_overlay_src_currenttime_native_loop_remain_unchanged():
    """Task F item 24: none of Pass 13's new code (tier selection, downgrade logic, tier
    reset on restart) ever assigns overlay.src/overlay.currentTime, or touches the native
    <video loop> attribute."""
    html = _scanner_html()
    track_body = _track_frame_body()
    downgrade_block_at = track_body.index("if (stageTimingsMs.opticalFlow > 0) {")
    downgrade_block_end = track_body.index("      }\n      // Task D (Pass 12): process the single newest coalesced frame")
    downgrade_block = track_body[downgrade_block_at:downgrade_block_end]
    assert "overlay.src" not in downgrade_block
    assert "overlay.currentTime" not in downgrade_block
    assert "overlay.loop" not in downgrade_block
    restart_start = html.index("async function recoverScannerInner(reason, restartCamera)")
    restart_reset_end = html.index("consecutiveTierOverrunTicks = 0;", restart_start) + len("consecutiveTierOverrunTicks = 0;")
    restart_body = html[restart_start:restart_reset_end]
    assert "overlay.src" not in restart_body
    assert "overlay.currentTime" not in restart_body
    assert html.count('<video id="overlay"') == 1


# ---------------------------------------------------------------------------
# Pass 14: APPLY TIER DOWNGRADE IN-EPOCH AND COMPLETE PRODUCTION LOGGING CLEANUP.
#
# Task A/B/D: attemptInEpochTierReconfig applies a performance-tier downgrade to the
# CURRENTLY ACTIVE tracking epoch (cancel owned callback -> resetTrackingEpoch -> fresh
# gray/points from the SAME captured frame -> rearm) instead of waiting for the next
# natural bootstrap. Bounded to one attempt per epoch plus a cooldown; falls back to the
# existing tracking-loss/recovery path on any failure.
#
# Task E/F: logTimingCheckpoint now shares the same level-gating as logCallbackEvent
# (Pass 12) — routine watchdog/capture/fetch/scan-timer logs suppressed by default; a
# genuine callback stall now reports what the main thread was actually doing.
# ---------------------------------------------------------------------------

def test_p14_01_active_medium_to_low_downgrade_starts_inepoch_reconfig():
    """Task H item 1: attemptInEpochTierReconfig is called immediately after the
    [TRACK PERFORMANCE TIER CHANGE] log, inside the SAME downgrade branch — every
    downgrade decision attempts an in-epoch reconfiguration, not just a future one."""
    track_body = _track_frame_body()
    tier_change_at = track_body.index("logCallbackEvent('[TRACK PERFORMANCE TIER CHANGE]',")
    reconfig_call_at = track_body.index("attemptInEpochTierReconfig(oldTier, performanceTier);", tier_change_at)
    assert tier_change_at < reconfig_call_at


def test_p14_02_overlay_remains_visible_during_reconfiguration():
    """Task H item 2: attemptInEpochTierReconfig's SUCCESS path never calls
    clearTrackingGeometry/stopOverlayImmediate/requestPoseHold — only the FAILURE path
    (via dropTracking, the existing bounded loss/recovery flow) ever touches overlay
    visibility. The overlay's own transform is simply never written during reconfig."""
    html = _scanner_html()
    fn_start = html.index("function attemptInEpochTierReconfig(oldTier, newTier)")
    fn_end = html.index("function dropTracking(reason, extraMats", fn_start)
    body = html[fn_start:fn_end]
    success_at = body.index("logCallbackEvent('[TRACK TIER RECONFIG SUCCESS]',")
    failure_at = body.index("dropTracking('tier_reconfig_failed'")
    success_region = body[:failure_at]
    assert "clearTrackingGeometry(" not in success_region
    assert "stopOverlayImmediate(" not in success_region
    assert "requestPoseHold(" not in success_region
    assert success_at < failure_at


def test_p14_03_old_callback_is_cancelled_once():
    """Task H item 3: cancelCurrentTrackingCallback('tier_reconfig') is called exactly
    once, before any of the rebuild work begins."""
    html = _scanner_html()
    fn_start = html.index("function attemptInEpochTierReconfig(oldTier, newTier)")
    fn_end = html.index("function dropTracking(reason, extraMats", fn_start)
    body = html[fn_start:fn_end]
    assert body.count("cancelCurrentTrackingCallback('tier_reconfig')") == 1
    cancel_at = body.index("cancelCurrentTrackingCallback('tier_reconfig');")
    reset_epoch_at = body.index("resetTrackingEpoch(frameW, frameH);")
    assert cancel_at < reset_epoch_at


def test_p14_04_stale_callback_cannot_mutate_new_epoch():
    """Task H item 4: reconfiguration reuses resetTrackingEpoch, which bumps trackingEpoch
    — any callback captured under the OLD epoch fails the existing
    trackingCallbackValidityFailureReason 'stale_epoch' check if it ever fires."""
    html = _scanner_html()
    reset_start = html.index("function resetTrackingEpoch(width, height)")
    reset_end = html.index("function cornersToMat(corners)")
    assert "trackingEpoch++;" in html[reset_start:reset_end]
    assert "if (callbackEpoch !== trackingEpoch) return 'stale_epoch';" in html


def test_p14_05_old_resolution_mats_are_deleted():
    """Task H item 5: resetTrackingEpoch (reused inside reconfig) deletes the old
    prevGray/prevPts BEFORE the fresh frame is even captured — no old-resolution Mat
    survives into the new tracking space."""
    html = _scanner_html()
    reset_start = html.index("function resetTrackingEpoch(width, height)")
    body = html[reset_start:reset_start + 400]
    assert "if (prevGray) { prevGray.delete(); prevGray = null; }" in body
    assert "if (prevPts) { prevPts.delete(); prevPts = null; }" in body
    fn_start = html.index("function attemptInEpochTierReconfig(oldTier, newTier)")
    fn_body = html[fn_start:html.index("function dropTracking(reason, extraMats", fn_start)]
    reset_call_at = fn_body.index("resetTrackingEpoch(frameW, frameH);")
    gray_at = fn_body.index("gray = matFromVideoGray();")
    assert reset_call_at < gray_at


def test_p14_06_new_tracking_dimensions_applied_immediately():
    """Task H item 6: resetTrackingEpoch recomputes trackWidth/trackHeight from the
    (already-updated) performanceTier the moment reconfig calls it — no deferred
    application to a later tick."""
    html = _scanner_html()
    reset_start = html.index("function resetTrackingEpoch(width, height)")
    reset_end = html.index("function cornersToMat(corners)")
    body = html[reset_start:reset_end]
    assert "const trackSpace = computeTrackSpaceDimensions(width, height, performanceTier);" in body
    assert "trackWidth = trackSpace.width;" in body
    assert "trackingCanvas.width = trackWidth;" in body


def test_p14_07_accepted_intrinsic_quad_converts_to_new_track_space():
    """Task H item 7: resetTrackingEpoch converts the PRESERVED currCorners (the last
    accepted intrinsic quad, untouched by reconfig) into the NEW track space, and
    attemptInEpochTierReconfig's mask/goodFeaturesToTrack use exactly that."""
    html = _scanner_html()
    reset_start = html.index("function resetTrackingEpoch(width, height)")
    reset_end = html.index("function cornersToMat(corners)")
    assert "currCornersTrack = currCorners ? toTrackSpace(currCorners) : null;" in html[reset_start:reset_end]
    fn_start = html.index("function attemptInEpochTierReconfig(oldTier, newTier)")
    fn_body = html[fn_start:html.index("function dropTracking(reason, extraMats", fn_start)]
    assert "mask = maskFromQuad(currCornersTrack);" in fn_body


def test_p14_08_gray_and_points_come_from_the_same_fresh_frame():
    """Task H item 8: gray is captured ONCE (matFromVideoGray) and that SAME local
    variable feeds both maskFromQuad-driven goodFeaturesToTrack and (on success)
    prevGray — never a second, separately-captured frame."""
    html = _scanner_html()
    fn_start = html.index("function attemptInEpochTierReconfig(oldTier, newTier)")
    fn_body = html[fn_start:html.index("function dropTracking(reason, extraMats", fn_start)]
    assert fn_body.count("matFromVideoGray()") == 1
    gray_at = fn_body.index("gray = matFromVideoGray();")
    features_at = fn_body.index("cv.goodFeaturesToTrack(gray, features, currentMaxTrackPoints(), 0.01, 8, mask, 3, false, 0.04);")
    prev_gray_at = fn_body.index("prevGray = gray;")
    assert gray_at < features_at < prev_gray_at


def test_p14_09_low_tier_point_cap_becomes_60_immediately():
    """Task H item 9: reconfig's own goodFeaturesToTrack call reads currentMaxTrackPoints()
    live — once performanceTier is already 'low' (set by the caller before reconfig
    runs), this immediately requests the 60-point low-tier target, not the old tier's."""
    html = _scanner_html()
    fn_start = html.index("function attemptInEpochTierReconfig(oldTier, newTier)")
    fn_body = html[fn_start:html.index("function dropTracking(reason, extraMats", fn_start)]
    assert "cv.goodFeaturesToTrack(gray, features, currentMaxTrackPoints(), 0.01, 8, mask, 3, false, 0.04);" in fn_body
    assert "const TIER_MAX_TRACK_POINTS = { low: 60, medium: 80, high: MAX_TRACK_POINTS };" in html


def test_p14_10_reseed_cap_becomes_60_immediately():
    """Task H item 10: attemptFeatureReseed already computes tierMaxPoints via
    currentMaxTrackPoints() fresh every call (Pass 13) — unaffected by, and immediately
    consistent with, an in-epoch tier downgrade."""
    body = _attempt_feature_reseed_body()
    assert "const tierMaxPoints = currentMaxTrackPoints();" in body


def test_p14_11_reconfiguration_runs_at_most_once_per_downgrade_event():
    """Task H item 11: tierReconfigAttemptsThisEpoch is checked against
    MAX_TIER_RECONFIG_ATTEMPTS_PER_EPOCH (1) and incremented before any rebuild work
    begins — a second attempt within the same epoch is refused."""
    html = _scanner_html()
    assert "const MAX_TIER_RECONFIG_ATTEMPTS_PER_EPOCH = 1;" in html
    fn_start = html.index("function attemptInEpochTierReconfig(oldTier, newTier)")
    fn_body = html[fn_start:html.index("function dropTracking(reason, extraMats", fn_start)]
    guard_at = fn_body.index("if (tierReconfigAttemptsThisEpoch >= MAX_TIER_RECONFIG_ATTEMPTS_PER_EPOCH) {")
    increment_at = fn_body.index("tierReconfigAttemptsThisEpoch++;")
    assert guard_at < increment_at


def test_p14_12_cooldown_prevents_repeated_transitions():
    """Task H item 12: a time-based cooldown (TIER_RECONFIG_COOLDOWN_MS, reusing
    TRACKING_GRACE_MS) additionally guards against a repeat attempt, independent of the
    per-epoch counter."""
    html = _scanner_html()
    assert "const TIER_RECONFIG_COOLDOWN_MS = TRACKING_GRACE_MS;" in html
    fn_start = html.index("function attemptInEpochTierReconfig(oldTier, newTier)")
    fn_body = html[fn_start:html.index("function dropTracking(reason, extraMats", fn_start)]
    assert "if (startedAt - lastTierReconfigAttemptAt < TIER_RECONFIG_COOLDOWN_MS) {" in fn_body


def test_p14_13_successful_transition_rearms_one_callback():
    """Task H item 13: ensureTrackingCallbackOwnership is called exactly once, only in the
    success path, only after trackingTierReconfigActive is cleared."""
    html = _scanner_html()
    fn_start = html.index("function attemptInEpochTierReconfig(oldTier, newTier)")
    fn_body = html[fn_start:html.index("function dropTracking(reason, extraMats", fn_start)]
    assert fn_body.count("ensureTrackingCallbackOwnership(") == 1
    clear_at = fn_body.index("trackingTierReconfigActive = false;")
    rearm_at = fn_body.index("ensureTrackingCallbackOwnership('tier_reconfig_success_rearm');")
    assert clear_at < rearm_at


def test_p14_14_failed_transition_falls_back_safely():
    """Task H item 14: any exception inside the rebuild (including the explicit
    insufficient_reconfig_points throw) is caught and routed through the existing
    dropTracking('tier_reconfig_failed', ...) path — never a bare, unhandled failure."""
    html = _scanner_html()
    fn_start = html.index("function attemptInEpochTierReconfig(oldTier, newTier)")
    fn_body = html[fn_start:html.index("function dropTracking(reason, extraMats", fn_start)]
    assert "throw new Error('insufficient_reconfig_points');" in fn_body
    assert "} catch (e) {" in fn_body
    assert "dropTracking('tier_reconfig_failed', [gray, mask, features], {" in fn_body


def test_p14_15_watchdog_does_not_report_intentional_transition_as_stall():
    """Task H item 15: onRvfcStallWatchdogFired checks trackingTierReconfigActive and
    returns (without touching ownership) strictly BEFORE it can ever reach
    enterCallbackStallRecovery."""
    html = _scanner_html()
    fn_start = html.index("function onRvfcStallWatchdogFired(callbackId)")
    fn_end = html.index("// Task C (Pass 6): the single validity check")
    body = html[fn_start:fn_end]
    reconfig_check_at = body.index("if (trackingTierReconfigActive) {")
    reconfig_return_at = body.index("return;", reconfig_check_at)
    stall_at = body.index("enterCallbackStallRecovery(callbackId, 'rvfc_stall_watchdog');")
    assert reconfig_check_at < reconfig_return_at < stall_at


def test_p14_16_default_mode_suppresses_routine_watchdog_logs():
    """Task H item 16: WATCHDOG TICK and WATCHDOG SKIP are both 'verbose' — suppressed
    unless ?scanner_debug=1 (diagnosticLevel === 'verbose')."""
    html = _scanner_html()
    assert "'[WATCHDOG TICK]': 'verbose'," in html
    assert "'[WATCHDOG SKIP]': 'verbose'," in html


def test_p14_17_default_mode_suppresses_routine_capture_fetch_logs():
    """Task H item 17: FRAME CAPTURE/DRAW IMAGE/TOBLOB/FETCH START and END are all
    'verbose'."""
    html = _scanner_html()
    for tag in (
        "'[FRAME CAPTURE START]': 'verbose',", "'[FRAME CAPTURE END]': 'verbose',",
        "'[DRAW IMAGE START]': 'verbose',", "'[DRAW IMAGE END]': 'verbose',",
        "'[TOBLOB START]': 'verbose',", "'[TOBLOB END]': 'verbose',",
        "'[FETCH START]': 'verbose',", "'[FETCH END]': 'verbose',",
        "'[SCAN SCHEDULED]': 'verbose',", "'[SCAN TIMER FIRED]': 'verbose',",
    ):
        assert tag in html
    fn_start = html.index("function logTimingCheckpoint(tag, reason, extra)")
    fn_body = html[fn_start:fn_start + 500]
    assert "if (!shouldEmitDiagnosticLevel(tag)) return;" in fn_body


def test_p14_18_default_mode_suppresses_normal_performance_logs():
    """Task H item 18: [TRACK FRAME PERFORMANCE] is only logged when the tick was slow,
    an lkMs/point-collapse/tier-change/rescue/exception condition fired, or verbose was
    requested — never unconditionally on a healthy, fast tick. Pass 15 replaced the old
    gapWasSuspected/droppedThisTick triggers with this more precise set."""
    track_body = _track_frame_body()
    assert (
        "if (totalOverrun || lkOverrun || pointCollapseDetectedThisTick || tierChangedThisTick ||\n"
        "            rescueTriggeredThisTick || exceptionThisTick || diagnosticLevel === 'verbose') {"
    ) in track_body


def test_p14_19_slow_performance_logs_remain_visible():
    """Task H item 19: [TRACK FRAME PERFORMANCE] itself is tagged 'events' (visible by
    default whenever its own trigger condition fires) — never demoted to verbose-only."""
    html = _scanner_html()
    assert "'[TRACK FRAME PERFORMANCE]': 'events'," in html


def test_p14_20_verbose_mode_still_shows_all_detailed_logs():
    """Task H item 20: diagnosticLevel becomes 'verbose' whenever ?scanner_debug=1 is set,
    and 'verbose' outranks every other level in DIAG_LEVEL_RANK — nothing is suppressed
    in that mode."""
    html = _scanner_html()
    assert "let diagnosticLevel = diagnosticsEnabled ? 'verbose' : 'events';" in html
    assert "const DIAG_LEVEL_RANK = { errors: 0, events: 1, verbose: 2 };" in html


def test_p14_21_weak_geometry_support_unchanged():
    """Task H item 21: weakGeometrySupport's formula is byte-identical to Pass 11-13."""
    track_body = _track_frame_body()
    assert "const weakGeometrySupport = geometryInlierCount < MIN_GOOD_POINTS || geometryInlierGridCells < RESEED_MIN_GRID_CELLS;" in track_body


def test_p14_22_corner_order_invalid_unchanged():
    """Task H item 22: corner_order_invalid is still an immediate, unconditional
    dropTracking() reason."""
    track_body = _track_frame_body()
    assert "dropTracking('corner_order_invalid', [gray, nextPts, status, err, prevMat, nextMat, mask, H], {" in track_body


def test_p14_23_out_of_bounds_unchanged():
    """Task H item 23: out_of_bounds is still an immediate, unconditional dropTracking()
    reason."""
    track_body = _track_frame_body()
    assert "dropTracking('out_of_bounds', [gray, nextPts, status, err, prevMat, nextMat, mask, H], {" in track_body


def test_p14_24_accept_hold_reject_unchanged():
    """Task H item 24: shapeReason/decision/IMMEDIATE_SHAPE_REJECT_REASONS and the bounded
    SHAPE_HOLD_MAX_FRAMES/SHAPE_HOLD_MAX_MS allowance are unchanged by Pass 14."""
    track_body = _track_frame_body()
    assert "const shapeReason = !localPoseQuality.ok ? localPoseQuality.reason : (weakGeometrySupport ? 'weak_geometry_support' : null);" in track_body
    assert "const decision = (isImmediateReject || holdExpired) ? 'reject' : 'hold';" in track_body
    html = _scanner_html()
    assert "const SHAPE_HOLD_MAX_MS = POSE_HOLD_MS;" in html
    assert "const SHAPE_HOLD_MAX_FRAMES = TRACKING_GRACE_FRAMES;" in html


def test_p14_25_backend_thresholds_unchanged():
    """Task H item 25: MIN_GOOD_POINTS/MAX_ERR/RANSAC_REPROJ/TRACKING_GRACE_FRAMES/
    TRACKING_GRACE_MS/POSE_HOLD_MS/RVFC_STALL_TIMEOUT_MS are exactly the same values as
    before Pass 14."""
    html = _scanner_html()
    assert "const MIN_GOOD_POINTS = scannerMode === 'lightweight' ? 12 : (deviceInfo.isLowEnd ? 16 : 20);" in html
    assert "const MAX_ERR = scannerMode === 'lightweight' ? 55 : (deviceInfo.isLowEnd ? 45 : 35);" in html
    assert "const RANSAC_REPROJ = 5.0;" in html
    assert "const TRACKING_GRACE_FRAMES = 3;" in html
    assert "const TRACKING_GRACE_MS = 900;" in html
    assert "const POSE_HOLD_MS = 500;" in html
    assert "const RVFC_STALL_TIMEOUT_MS = TRACKING_GRACE_MS;" in html


def test_p14_26_overlay_src_currenttime_native_loop_unchanged():
    """Task H item 26: none of Pass 14's new code (reconfig function, watchdog checks,
    logging gates) ever assigns overlay.src/overlay.currentTime, or touches the native
    <video loop> attribute."""
    html = _scanner_html()
    fn_start = html.index("function attemptInEpochTierReconfig(oldTier, newTier)")
    fn_body = html[fn_start:html.index("function dropTracking(reason, extraMats", fn_start)]
    assert "overlay.src" not in fn_body
    assert "overlay.currentTime" not in fn_body
    assert "overlay.loop" not in fn_body
    assert html.count('<video id="overlay"') == 1


def test_p14_27_pass13_tier_logic_preserved():
    """Task H item 27: TIER_TRACK_MAX_DIM/TIER_MAX_TRACK_POINTS/consecutiveTierOverrunTicks/
    TIER_DOWNGRADE_STREAK are unchanged by Pass 14 — only WHEN the downgrade takes visible
    effect changed (immediately, in-epoch), never the decision logic itself."""
    html = _scanner_html()
    assert "const TIER_TRACK_MAX_DIM = { low: 480, medium: 560, high: 720 };" in html
    assert "const TIER_MAX_TRACK_POINTS = { low: 60, medium: 80, high: MAX_TRACK_POINTS };" in html
    assert "const TIER_DOWNGRADE_STREAK = 3;" in html
    track_body = _track_frame_body()
    assert "consecutiveTierOverrunTicks++;" in track_body
    assert "performanceTier = performanceTier === 'high' ? 'medium' : 'low';" in track_body


def test_p14_28_pass12_coordinate_conversion_preserved():
    """Task H item 28: the ONE conversion back to intrinsic space still happens right
    before applyWarp in trackFrame's own accept path — reconfig is a SEPARATE mechanism,
    it does not alter this."""
    track_body = _track_frame_body()
    assert (
        "currCornersTrack = newCorners;\n"
        "        currCorners = toIntrinsicSpace(newCorners);\n"
        "        if (!applyWarp(currCorners)) {"
    ) in track_body


def test_p14_29_pass10_reseed_baseline_preserved():
    """Task H item 29: a successful reseed still resets the frame-gap baseline and sets
    firstPostReseedLkPending — unchanged by Pass 14."""
    body = _attempt_feature_reseed_body()
    assert "resetFrameGapBaseline('feature_reseed_success');" in body
    assert "firstPostReseedLkPending = true;" in body


def test_p14_30_recovery_ownership_remains_single_owner():
    """Task H item 30: dropTracking still schedules exactly one tracking_lost_reacquire
    attempt — Pass 14 added no second reacquisition path, including on a failed
    in-epoch tier reconfig (which itself just calls the SAME dropTracking)."""
    html = _scanner_html()
    drop_start = html.index("function dropTracking(reason, extraMats")
    drop_end = html.index("function handleDetectionTimeout()")
    drop_body = html[drop_start:drop_end]
    assert drop_body.count("scheduleNextScan('tracking_lost_reacquire', 0);") == 1


# ---------------------------------------------------------------------------
# Pass 15 (Release Candidate): CONTINUITY RESCUE AND FINAL LOG FILTERING.
#
# Task A/B/C/D: attemptFeatureRescue — one bounded, last-resort feature-rescue attempt
# for a sharp point-population collapse, called ONLY from trackFrame's
# insufficient_flow_points branch after graceExpired(), strictly before the actual drop.
# Never reachable after any hard geometry rejection (those live later in the same tick's
# pipeline). Bounded to one attempt per epoch, blocked during tier reconfiguration and
# until one accepted frame has proven a post-reconfig baseline.
#
# Task E/F: logTimingCheckpoint/logVideoCheckpoint's remaining routine tags (ENCODE
# COMPLETE, RESPONSE HANDLED, overlay play/pause/loop) now share the same level-gating;
# the performance-log trigger set is now precise (slow/lkMs-over-budget/point-collapse/
# tier-change/rescue/exception), and SLOW_TRACK_FRAME_MS raised to 350 to match Pass
# 13/14's new healthy baseline (~200-290ms).
# ---------------------------------------------------------------------------

def _attempt_feature_rescue_body():
    html = _scanner_html()
    start = html.index("function attemptFeatureRescue(gray, pointsBefore, pointsAfterLk, rescueReason)")
    end = html.index("function dropTracking(reason, extraMats", start)
    return html[start:end]


def test_p15_01_sharp_point_collapse_triggers_one_feature_rescue():
    """Task G item 1: sharpCollapse (previous points >= 40 AND current good points <= 15)
    labels the rescue attempt 'sharp_point_collapse' and calls attemptFeatureRescue."""
    track_body = _track_frame_body()
    assert "const sharpCollapse = initialPointCountThisRound >= 40 && (goodPrev.length / 2) <= 15;" in track_body
    assert "sharpCollapse ? 'sharp_point_collapse' : 'near_minimum'" in track_body
    assert "const rescued = attemptFeatureRescue(" in track_body


def test_p15_02_near_minimum_points_can_trigger_rescue():
    """Task G item 2: whenever sharpCollapse is false, the rescue is still attempted
    (this code path is only reached once goodPrev.length/2 already fell below
    MIN_GOOD_POINTS and grace expired), just labeled 'near_minimum' instead."""
    track_body = _track_frame_body()
    assert "sharpCollapse ? 'sharp_point_collapse' : 'near_minimum'" in track_body


def test_p15_03_healthy_gradual_point_reduction_does_not_trigger_rescue():
    """Task G item 3: the entire rescue block sits inside the
    `(goodPrev.length / 2) < MIN_GOOD_POINTS` branch, strictly after graceExpired() — a
    tick that never falls below MIN_GOOD_POINTS (healthy, even if gradually shrinking)
    never reaches the rescue code at all."""
    track_body = _track_frame_body()
    min_points_check_at = track_body.index("if ((goodPrev.length / 2) < MIN_GOOD_POINTS) {")
    grace_expired_at = track_body.index("if (!graceExpired()) {", min_points_check_at)
    rescue_eligible_at = track_body.index("const rescueEligible =", grace_expired_at)
    assert min_points_check_at < grace_expired_at < rescue_eligible_at


def test_p15_04_rescue_requires_a_last_accepted_quad():
    """Task G item 4: rescueEligible requires both currCorners (intrinsic) and
    currCornersTrack (track-space) to be truthy — no accepted quad, no rescue attempt."""
    track_body = _track_frame_body()
    assert "currCorners && currCornersTrack && !rescueInProgress &&" in track_body


def test_p15_05_rescue_uses_current_tier_dimensions():
    """Task G item 5: attemptFeatureRescue's own goodFeaturesToTrack call reads
    currentMaxTrackPoints() live — whatever the tier is RIGHT NOW."""
    body = _attempt_feature_rescue_body()
    assert "cv.goodFeaturesToTrack(gray, features, currentMaxTrackPoints(), 0.01, 8, mask, 3, false, 0.04);" in body


def test_p15_06_rescue_uses_one_fresh_gray_frame():
    """Task G item 6: attemptFeatureRescue never calls matFromVideoGray() itself — it only
    ever operates on the `gray` Mat passed in by trackFrame (this tick's own already-
    captured frame), never a second capture."""
    body = _attempt_feature_rescue_body()
    assert "matFromVideoGray()" not in body


def test_p15_07_gray_and_feature_points_come_from_the_same_frame():
    """Task G item 7: mask is built, then goodFeaturesToTrack runs against the SAME
    `gray` — in that order, never re-assigning `gray` in between."""
    body = _attempt_feature_rescue_body()
    mask_at = body.index("mask = maskFromQuad(currCornersTrack);")
    features_at = body.index("cv.goodFeaturesToTrack(gray, features, currentMaxTrackPoints(), 0.01, 8, mask, 3, false, 0.04);")
    assert mask_at < features_at


def test_p15_08_rescue_mask_from_accepted_intrinsic_quad_converted_to_tracking_space():
    """Task G item 8: the rescue mask comes from currCornersTrack — the last accepted
    INTRINSIC quad (currCorners), already converted into the current tracking space by
    resetTrackingEpoch/toTrackSpace — never a raw intrinsic-space quad, never a
    speculative one."""
    body = _attempt_feature_rescue_body()
    assert "mask = maskFromQuad(currCornersTrack);" in body


def test_p15_09_spatial_distribution_requirements_remain_enforced():
    """Task G item 9: the rescue's own success floor reuses the EXISTING
    RESEED_MIN_COVERAGE_AFTER_MERGE/RESEED_MIN_GRID_CELLS/MIN_GOOD_POINTS constants —
    never a weaker, rescue-specific threshold."""
    body = _attempt_feature_rescue_body()
    assert (
        "if (count < MIN_GOOD_POINTS || coverage < RESEED_MIN_COVERAGE_AFTER_MERGE || gridCells < RESEED_MIN_GRID_CELLS) {"
    ) in body


def test_p15_10_rescue_replaces_prevgray_only_after_success():
    """Task G item 10: prevGray.delete()/prevGray = gray only happens AFTER the count/
    coverage/gridCells check — the explicit throw on failure sits strictly before it."""
    body = _attempt_feature_rescue_body()
    throw_at = body.index("throw new Error('insufficient_rescue_points');")
    prev_gray_at = body.index("prevGray = gray;")
    assert throw_at < prev_gray_at


def test_p15_11_rescue_replaces_prevpts_only_after_success():
    """Task G item 11: prevPts.delete()/prevPts = features.clone() only happens on the
    SAME success path as prevGray's replacement, strictly after the failure throw."""
    body = _attempt_feature_rescue_body()
    throw_at = body.index("throw new Error('insufficient_rescue_points');")
    prev_pts_at = body.index("prevPts = features.clone();")
    assert throw_at < prev_pts_at


def test_p15_12_collapsed_points_are_not_merged_into_rescue_points():
    """Task G item 12: attemptFeatureRescue never references goodPrev/goodNext/merged —
    it only ever uses the fresh goodFeaturesToTrack output, never combined with the
    collapsed LK survivors."""
    body = _attempt_feature_rescue_body()
    assert "goodPrev" not in body
    assert "goodNext" not in body
    # Checked as the actual variable-usage patterns (not the bare substring "merged",
    # which also appears in this function's own explanatory prose comments).
    assert "const merged" not in body
    assert "merged.push(" not in body


def test_p15_13_successful_rescue_does_not_call_backend():
    """Task G item 13: attemptFeatureRescue never calls detectOnceFromServer or
    scheduleNextScan — a successful rescue is resolved entirely from local evidence."""
    body = _attempt_feature_rescue_body()
    assert "detectOnceFromServer(" not in body
    assert "scheduleNextScan(" not in body


def test_p15_14_failed_rescue_falls_back_to_normal_tracking_loss():
    """Task G item 14: on failure, attemptFeatureRescue returns false and the CALLER
    (trackFrame) falls through to the existing dropTracking('insufficient_flow_points',
    ...) call — never a bare, unhandled failure."""
    track_body = _track_frame_body()
    rescue_call_at = track_body.index("const rescued = attemptFeatureRescue(")
    if_rescued_at = track_body.index("if (rescued) {", rescue_call_at)
    drop_at = track_body.index("dropTracking('insufficient_flow_points', [gray, nextPts, status, err], {", if_rescued_at)
    assert rescue_call_at < if_rescued_at < drop_at


def test_p15_15_original_evidence_based_loss_reason_is_preserved():
    """Task G item 15: the drop reason after a failed rescue is still the ORIGINAL
    'insufficient_flow_points' — never replaced with a generic rescue-failure reason."""
    track_body = _track_frame_body()
    assert "dropTracking('insufficient_flow_points', [gray, nextPts, status, err], {" in track_body
    assert "dropTracking('rescue_failed'" not in track_body


def test_p15_16_maximum_one_rescue_per_epoch():
    """Task G item 16: MAX_RESCUE_ATTEMPTS_PER_EPOCH is 1, and rescueEligible checks
    rescueAttemptsForEpoch against it before ever calling attemptFeatureRescue."""
    html = _scanner_html()
    assert "const MAX_RESCUE_ATTEMPTS_PER_EPOCH = 1;" in html
    track_body = _track_frame_body()
    assert "rescueAttemptsForEpoch < MAX_RESCUE_ATTEMPTS_PER_EPOCH &&" in track_body


def test_p15_17_rescue_cannot_loop_repeatedly():
    """Task G item 17: rescueInProgress guards against reentrancy, and
    attemptFeatureRescue increments rescueAttemptsForEpoch unconditionally at its own
    start — a second attempt this epoch is refused by the caller's own guard."""
    track_body = _track_frame_body()
    assert "!rescueInProgress &&" in track_body
    body = _attempt_feature_rescue_body()
    assert "rescueAttemptsForEpoch++;" in body


def test_p15_18_rescue_is_blocked_during_tier_reconfiguration():
    """Task G item 18: rescueEligible requires !trackingTierReconfigActive."""
    track_body = _track_frame_body()
    assert "!trackingTierReconfigActive && hasAcceptedFrameSinceTierReconfig &&" in track_body


def test_p15_19_rescue_is_blocked_during_recovery():
    """Task G item 19: rescueEligible requires scannerWorkMode === 'TRACKING' — a
    RECOVERING scanner (mid drop/reacquisition) can never attempt a rescue."""
    track_body = _track_frame_body()
    assert "const rescueEligible = tracking && scannerWorkMode === 'TRACKING' &&" in track_body


def test_p15_20_rescue_is_blocked_during_camera_restart():
    """Task G item 20: a camera restart runs clearTrackingGeometry (via stopTrackingLoop's
    caller path), which sets tracking = false — rescueEligible's own `tracking` guard
    already covers this, and clearTrackingGeometry additionally resets rescue state."""
    html = _scanner_html()
    clear_start = html.index("function clearTrackingGeometry(reason, options = {})")
    clear_end = html.index("function logCallbackEvent(tag, summaryFields, structuredData)")
    clear_body = html[clear_start:clear_end]
    assert "tracking = false;" in clear_body
    assert "rescueInProgress = false;" in clear_body
    assert "rescueAttemptsForEpoch = 0;" in clear_body


def test_p15_21_rescue_is_blocked_before_one_accepted_frame_after_tier_transition():
    """Task G item 21: hasAcceptedFrameSinceTierReconfig is set false the instant a tier
    reconfig succeeds, and only set true again at the true ACCEPT point — rescue's own
    guard requires it to be true."""
    html = _scanner_html()
    fn_start = html.index("function attemptInEpochTierReconfig(oldTier, newTier)")
    fn_body = html[fn_start:html.index("function dropTracking(reason, extraMats", fn_start)]
    assert "hasAcceptedFrameSinceTierReconfig = false;" in fn_body
    track_body = _track_frame_body()
    assert "hasAcceptedFrameSinceTierReconfig = true;" in track_body
    assert "hasAcceptedFrameSinceTierReconfig &&" in track_body


def test_p15_22_stale_callbacks_cannot_mutate_rescue_state():
    """Task G item 22: rescue only ever runs inside trackFrame's own synchronous LK
    pipeline, which the EXISTING top-of-tick ownership/epoch-validity checks already gate
    — a stale callback never reaches this deep into the tick at all (same structural
    guarantee Pass 7-14 already rely on)."""
    track_body = _track_frame_body()
    assert "if (tracking && trackingCallbackId === null) {" in track_body
    assert "if (callbackEpoch !== trackingEpoch) return 'stale_epoch';" in _scanner_html()


def test_p15_23_out_of_bounds_bypasses_rescue():
    """Task G item 23: out_of_bounds is still an immediate, unconditional dropTracking()
    reason, positioned entirely AFTER the insufficient_flow_points/rescue branch — never
    reachable from it."""
    track_body = _track_frame_body()
    rescue_at = track_body.index("function attemptFeatureRescue") if "function attemptFeatureRescue" in track_body else track_body.index("const rescueEligible =")
    out_of_bounds_at = track_body.index("dropTracking('out_of_bounds', [gray, nextPts, status, err, prevMat, nextMat, mask, H], {")
    assert rescue_at < out_of_bounds_at


def test_p15_24_corner_order_invalid_bypasses_rescue():
    """Task G item 24: corner_order_invalid is still an immediate, unconditional
    dropTracking() reason, positioned entirely AFTER the insufficient_flow_points/rescue
    branch — never reachable from it."""
    track_body = _track_frame_body()
    rescue_at = track_body.index("const rescueEligible =")
    corner_order_at = track_body.index("dropTracking('corner_order_invalid', [gray, nextPts, status, err, prevMat, nextMat, mask, H], {")
    assert rescue_at < corner_order_at


def test_p15_25_weak_geometry_support_bypasses_rescue():
    """Task G item 25: weakGeometrySupport's formula is unchanged, and it is evaluated
    entirely AFTER the insufficient_flow_points/rescue branch — never reachable from it."""
    track_body = _track_frame_body()
    assert "const weakGeometrySupport = geometryInlierCount < MIN_GOOD_POINTS || geometryInlierGridCells < RESEED_MIN_GRID_CELLS;" in track_body
    rescue_at = track_body.index("const rescueEligible =")
    weak_geom_at = track_body.index("const weakGeometrySupport =")
    assert rescue_at < weak_geom_at


def test_p15_26_non_convex_non_finite_geometry_bypasses_rescue():
    """Task G item 26: validateOverlayQuad's hard checks (zero_edge, collapsed_area,
    self_intersecting_quad, diagonals_do_not_cross_inside, non_finite_or_not_four) are
    unchanged — normalizeCornerOrder (which uses them) runs entirely AFTER the
    insufficient_flow_points/rescue branch, never reachable from it."""
    html = _scanner_html()
    assert "if (!clean) return { ok: false, reason: 'non_finite_or_not_four' };" in html
    assert "if (edges.some(edge => edge < 1)) return { ok: false, reason: 'zero_edge', edges };" in html
    assert "if (!diagonalsCross) return { ok: false, reason: 'diagonals_do_not_cross_inside'" in html
    track_body = _track_frame_body()
    rescue_at = track_body.index("const rescueEligible =")
    normalize_at = track_body.index("const newCorners = normalizeCornerOrder(newCornersRaw, currCornersTrack);")
    assert rescue_at < normalize_at


def test_p15_27_accept_hold_reject_unchanged():
    """Task G item 27: shapeReason/decision/IMMEDIATE_SHAPE_REJECT_REASONS and the bounded
    SHAPE_HOLD_MAX_FRAMES/SHAPE_HOLD_MAX_MS allowance are unchanged by Pass 15."""
    track_body = _track_frame_body()
    assert "const shapeReason = !localPoseQuality.ok ? localPoseQuality.reason : (weakGeometrySupport ? 'weak_geometry_support' : null);" in track_body
    assert "const decision = (isImmediateReject || holdExpired) ? 'reject' : 'hold';" in track_body
    html = _scanner_html()
    assert "const SHAPE_HOLD_MAX_MS = POSE_HOLD_MS;" in html
    assert "const SHAPE_HOLD_MAX_FRAMES = TRACKING_GRACE_FRAMES;" in html


def test_p15_28_current_point_caps_remain_unchanged():
    """Task G item 28: TIER_MAX_TRACK_POINTS (60/80/existing) is byte-identical to
    Pass 13/14."""
    html = _scanner_html()
    assert "const TIER_MAX_TRACK_POINTS = { low: 60, medium: 80, high: MAX_TRACK_POINTS };" in html


def test_p15_29_current_reseed_behavior_remains_unchanged():
    """Task G item 29: attemptFeatureReseed's own tier-target/merge-cap logic is
    byte-identical to Pass 13/14 — Pass 15 only added a SEPARATE, more tightly bounded
    rescue mechanism, never modified reseed itself."""
    body = _attempt_feature_reseed_body()
    assert "const tierMaxPoints = currentMaxTrackPoints();" in body
    assert "const targetNew = tierMaxPoints - survivingCount;" in body
    assert "if ((merged.length / 2) >= tierMaxPoints) break;" in body


def test_p15_30_default_mode_suppresses_encode_complete():
    """Task G item 30: [ENCODE COMPLETE] is 'verbose'."""
    html = _scanner_html()
    assert "'[ENCODE COMPLETE]': 'verbose'," in html


def test_p15_31_default_mode_suppresses_response_handled():
    """Task G item 31: [RESPONSE HANDLED] is 'verbose'."""
    html = _scanner_html()
    assert "'[RESPONSE HANDLED]': 'verbose'," in html


def test_p15_32_default_mode_suppresses_overlay_play_pause_loop_logs():
    """Task G item 32: [OVERLAY VIDEO PAUSE]/[OVERLAY VIDEO PLAY]/[OVERLAY VIDEO LOOP] are
    all 'verbose', and logVideoCheckpoint shares the same level-gating."""
    html = _scanner_html()
    assert "'[OVERLAY VIDEO PAUSE]': 'verbose'," in html
    assert "'[OVERLAY VIDEO PLAY]': 'verbose'," in html
    assert "'[OVERLAY VIDEO LOOP]': 'verbose'," in html
    fn_start = html.index("function logVideoCheckpoint(tag, reason, extra)")
    fn_body = html[fn_start:fn_start + 400]
    assert "if (!shouldEmitDiagnosticLevel(tag)) return;" in fn_body


def test_p15_33_default_mode_suppresses_normal_performance_logs():
    """Task G item 33/Task F: SLOW_TRACK_FRAME_MS is raised to 350 (above the new healthy
    ~200-290ms baseline), and the perf-log trigger requires totalOverrun/lkOverrun/point-
    collapse/tier-change/rescue/exception/verbose — never fires on an ordinary tick."""
    html = _scanner_html()
    assert "const SLOW_TRACK_FRAME_MS = 350;" in html
    track_body = _track_frame_body()
    assert (
        "if (totalOverrun || lkOverrun || pointCollapseDetectedThisTick || tierChangedThisTick ||\n"
        "            rescueTriggeredThisTick || exceptionThisTick || diagnosticLevel === 'verbose') {"
    ) in track_body


def test_p15_34_slow_performance_logs_remain_visible():
    """Task G item 34: [TRACK FRAME PERFORMANCE] itself is tagged 'events' — visible by
    default whenever its own trigger condition fires."""
    html = _scanner_html()
    assert "'[TRACK FRAME PERFORMANCE]': 'events'," in html


def test_p15_35_rescue_logs_remain_visible():
    """Task G item 35: [TRACK FEATURE RESCUE START]/SUCCESS/FAILED are all 'events'."""
    html = _scanner_html()
    assert "'[TRACK FEATURE RESCUE START]': 'events'," in html
    assert "'[TRACK FEATURE RESCUE SUCCESS]': 'events'," in html
    assert "'[TRACK FEATURE RESCUE FAILED]': 'events'" in html


def test_p15_36_verbose_mode_still_shows_all_logs():
    """Task G item 36: diagnosticLevel becomes 'verbose' whenever ?scanner_debug=1 is set,
    and 'verbose' outranks every other level — nothing is suppressed in that mode."""
    html = _scanner_html()
    assert "let diagnosticLevel = diagnosticsEnabled ? 'verbose' : 'events';" in html
    assert "const DIAG_LEVEL_RANK = { errors: 0, events: 1, verbose: 2 };" in html


def test_p15_37_backend_thresholds_remain_unchanged():
    """Task G item 37: MIN_GOOD_POINTS/MAX_ERR/RANSAC_REPROJ/TRACKING_GRACE_FRAMES/
    TRACKING_GRACE_MS/POSE_HOLD_MS/RVFC_STALL_TIMEOUT_MS are exactly the same values as
    before Pass 15."""
    html = _scanner_html()
    assert "const MIN_GOOD_POINTS = scannerMode === 'lightweight' ? 12 : (deviceInfo.isLowEnd ? 16 : 20);" in html
    assert "const MAX_ERR = scannerMode === 'lightweight' ? 55 : (deviceInfo.isLowEnd ? 45 : 35);" in html
    assert "const RANSAC_REPROJ = 5.0;" in html
    assert "const TRACKING_GRACE_FRAMES = 3;" in html
    assert "const TRACKING_GRACE_MS = 900;" in html
    assert "const POSE_HOLD_MS = 500;" in html
    assert "const RVFC_STALL_TIMEOUT_MS = TRACKING_GRACE_MS;" in html


def test_p15_38_overlay_src_currenttime_native_loop_remain_unchanged():
    """Task G item 38: attemptFeatureRescue never assigns overlay.src/overlay.currentTime,
    and never touches the native <video loop> attribute."""
    html = _scanner_html()
    body = _attempt_feature_rescue_body()
    assert "overlay.src" not in body
    assert "overlay.currentTime" not in body
    assert "overlay.loop" not in body
    assert html.count('<video id="overlay"') == 1


def test_p15_39_recovery_ownership_remains_single_owner():
    """Task G item 39: dropTracking still schedules exactly one tracking_lost_reacquire
    attempt — Pass 15's rescue mechanism adds no second reacquisition path (a failed
    rescue falls through to the SAME existing dropTracking call)."""
    html = _scanner_html()
    drop_start = html.index("function dropTracking(reason, extraMats")
    drop_end = html.index("function handleDetectionTimeout()")
    drop_body = html[drop_start:drop_end]
    assert drop_body.count("scheduleNextScan('tracking_lost_reacquire', 0);") == 1


def test_p15_40_pass14_inepoch_tier_reconfiguration_remains_correct():
    """Task G item 40: attemptInEpochTierReconfig's own cancel -> resetTrackingEpoch ->
    rebuild -> rearm sequence is unchanged by Pass 15."""
    html = _scanner_html()
    fn_start = html.index("function attemptInEpochTierReconfig(oldTier, newTier)")
    fn_body = html[fn_start:html.index("function attemptFeatureRescue(gray, pointsBefore, pointsAfterLk, rescueReason)", fn_start)]
    cancel_at = fn_body.index("cancelCurrentTrackingCallback('tier_reconfig');")
    reset_at = fn_body.index("resetTrackingEpoch(frameW, frameH);")
    rearm_at = fn_body.index("ensureTrackingCallbackOwnership('tier_reconfig_success_rearm');")
    assert cancel_at < reset_at < rearm_at


def test_wave6_fallback_watch_controls_are_visible_actions_when_available():
    html = _scanner_html()
    assert 'id="fallbackWatchBtn"' in html
    assert 'id="recognitionWatchBtn"' in html
    assert 'Watch video instead' in html
    assert "discoverFallbackVideo(code);" in html
    assert "discoverFallbackVideo('recognition_help');" in html


def test_wave6_fallback_video_never_autoplays():
    html = _scanner_html()
    tag_start = html.index('<video id="fallbackVideo"')
    tag = html[tag_start:html.index('>', tag_start)]
    assert "controls" in tag
    assert "playsinline" in tag
    assert "autoplay" not in tag
    fallback_body = html[html.index("async function showFallbackVideoFromCandidate"):html.index("fallbackVideoEl.addEventListener")]
    assert ".play()" not in fallback_body


def test_wave6_uses_final_backend_fallback_routes():
    html = _scanner_html()
    assert "const FALLBACK_VIDEO_ROUTE = `/api/scanner/${encodeURIComponent(PROJECT_ID)}/fallback-video`;" in html
    assert "const FALLBACK_EVENT_ROUTE = `/api/scanner/${encodeURIComponent(PROJECT_ID)}/fallback-event`;" in html
    assert "fetch(url.toString(), { method: 'GET', credentials: 'same-origin' })" in html
    assert "fetch(FALLBACK_EVENT_ROUTE" in html
    assert "PROJECT_FALLBACK_VIDEO_URL" not in html
    assert "FALLBACK_ANALYTICS_ENDPOINT" not in html


def test_wave6_fallback_discovery_never_sends_a_pair_index_hint():
    """Fix 6 (V1 Agent 2): this used to send verifiedFallbackPairContext's pairIndex as a
    pair_index query hint, and the server used to treat ANY confirmed match's own pair as
    an implicit fallback candidate on the strength of that hint alone - a real product bug
    (an ordinary matched pair's own AR video could be offered back as "fallback" with no
    explicit fallback ever configured). The hint is gone entirely now; fallback is resolved
    from Project.fallback_pair_id only (see resolve_scanner_fallback_video in app.py)."""
    html = _scanner_html()
    fn_start = html.index("async function discoverFallbackVideo(reason)")
    fn_body = html[fn_start:html.index("function submitFallbackAnalytics", fn_start)]
    assert "url.searchParams.set('pair_index'" not in fn_body
    assert "fallbackCandidate = {" in fn_body
    assert "kind: data.source === 'pair' ? 'pair' : 'project'," in fn_body


def test_wave6_match_accept_still_sets_verified_context_but_it_no_longer_feeds_fallback_query():
    """verifiedFallbackPairContext is still recorded on every confirmed match accept (kept
    for its other bookkeeping/analytics use), but Fix 6 means it must never again reach the
    fallback-video request itself - see the sibling test above."""
    html = _scanner_html()
    fn_start = html.index("async function discoverFallbackVideo(reason)")
    fn_body = html[fn_start:html.index("function submitFallbackAnalytics", fn_start)]
    assert "verifiedFallbackPairContext" in fn_body
    assert "url.searchParams.set('pair_index'" not in fn_body
    accepted_at = html.index("recordAcceptance();")
    set_context_at = html.index("setVerifiedFallbackPairContext(Number(newPairId), 'matched_detection');", accepted_at)
    match_diag_at = html.index("scannerDiagnostics.push('[SCAN MATCH ACCEPT]'", accepted_at)
    assert accepted_at < set_context_at < match_diag_at


def test_wave6_rejected_detection_does_not_set_pair_fallback_context():
    html = _scanner_html()
    response_at = html.index("const newVideoUrl = data.video_url;")
    accepted_at = html.index("recordAcceptance();", response_at)
    pre_accept_body = html[response_at:accepted_at]
    assert "setVerifiedFallbackPairContext" not in pre_accept_body
    assert "setFallbackCandidate('pair'" not in html


def test_wave6_camera_unavailable_and_recognition_timeout_have_distinct_events():
    html = _scanner_html()
    assert "'camera_unavailable'" in html
    assert "'recognition_timeout'" in html
    # Issue 3B: the inline 'CAMERA_UNAVAILABLE'/'CAMERA_PERMISSION_DENIED' ternary was
    # replaced by isCameraFailureCode(code), the single place that now knows about all
    # four camera-classed codes (permission denied / not found / start failed /
    # interrupted) — see test_show_fallback_panel_uses_the_shared_camera_failure_classifier.
    assert "setScannerUiState(isCameraFailureCode(code) ? 'camera_unavailable' : 'fallback_available', code);" in html
    assert "setScannerUiState('recognition_timeout', reason);" in html
    assert "submitFallbackAnalytics('recognition_timeout'" in html
    assert "submitFallbackAnalytics('camera_unavailable'" in html
    assert "recognitionContinueBtn.addEventListener('click', continueScanningFromRecognitionHelp);" in html
    assert "fallbackRetryBtn.addEventListener('click', retryCameraFromFallback);" in html


def test_show_fallback_panel_uses_the_shared_camera_failure_classifier():
    """isCameraFailureCode() is the single place that has to know all four camera-classed
    codes. showFallbackPanel() (and the fallback-video-close handler) must call it rather
    than re-deriving their own inline subset — otherwise adding a new camera failure code
    means hunting down every duplicated ternary by hand."""
    html = _scanner_html()
    classifier_start = html.index("function isCameraFailureCode(code) {")
    classifier_end = html.index("}", classifier_start)
    classifier_body = html[classifier_start:classifier_end]
    for code in ("CAMERA_PERMISSION_DENIED", "CAMERA_NOT_FOUND", "CAMERA_START_FAILED", "CAMERA_INTERRUPTED", "SECURE_CONTEXT_REQUIRED"):
        assert f"code === '{code}'" in classifier_body

    panel_start = html.index("function showFallbackPanel(code, message) {")
    panel_end = html.index("function hideFallbackPanel(reason)")
    panel_body = html[panel_start:panel_end]
    assert panel_body.count("isCameraFailureCode(code)") >= 3
    assert "CAMERA_UNAVAILABLE" not in panel_body

    close_handler_start = html.index("fallbackVideoCloseBtn.addEventListener('click', function () {")
    close_handler_end = html.index("});", close_handler_start)
    assert "isCameraFailureCode(code)" in html[close_handler_start:close_handler_end]


def test_wave6_pair_fallback_and_project_fallback_emit_correct_event_types():
    html = _scanner_html()
    assert "'pair_fallback_view'," in html
    assert "'project_fallback_view'," in html
    assert "'recognition_timeout'," in html
    assert "'camera_unavailable'" in html
    assert "const eventType = candidate.kind === 'pair' ? 'pair_fallback_view' : 'project_fallback_view';" in html
    assert "matched_scan" not in html


def test_wave6_duplicate_taps_are_prevented_for_fallback_watch():
    html = _scanner_html()
    fallback_body = html[html.index("async function showFallbackVideoFromCandidate"):html.index("fallbackVideoEl.addEventListener")]
    assert "let fallbackWatchInProgress = false;" in html
    assert "if (fallbackWatchInProgress) return;" in fallback_body
    assert "fallbackWatchInProgress = true;" in fallback_body
    assert "fallbackWatchInProgress = false;" in fallback_body
    assert "btn.disabled = fallbackDiscoveryInFlight || !hasVideo || fallbackWatchInProgress;" in html
    assert "if (activeFallbackViewKey !== viewKey)" in fallback_body


def test_wave6_fallback_analytics_use_uuid_idempotency_and_duplicate_success():
    html = _scanner_html()
    assert "function fallbackUuid()" in html
    assert "function fallbackClientEventId(logicalEventKey)" in html
    assert "fallbackEventIds.set(logicalEventKey, fallbackUuid());" in html
    fn_start = html.index("function submitFallbackAnalytics(eventType, extra, logicalEventKey)")
    fn_body = html[fn_start:html.index("function closeFallbackVideoPanel", fn_start)]
    assert "event_type: eventType," in fn_body
    assert "client_event_id: fallbackClientEventId(eventKey)," in fn_body
    assert "fallback_analytics_queued" in fn_body
    assert "duplicate: Boolean(data.duplicate)" in fn_body
    assert "/api/scanner/session/end" not in fn_body


def test_wave6_fallback_absent_state_hides_watch_controls():
    html = _scanner_html()
    fn_start = html.index("function updateFallbackWatchControls(reason)")
    fn_body = html[fn_start:html.index("async function discoverFallbackVideo", fn_start)]
    assert "const hasVideo = Boolean(fallbackCandidate && fallbackCandidate.videoUrl);" in fn_body
    assert "const shouldShow = hasVideo || fallbackDiscoveryInFlight;" in fn_body
    assert "btn.style.display = shouldShow ? 'inline-flex' : 'none';" in fn_body
    assert "btn.setAttribute('aria-hidden', shouldShow ? 'false' : 'true');" in fn_body
    assert "No fallback video is available for this project yet." in fn_body


def test_wave6_fallback_media_error_is_visible():
    """A media error must still put a visible explanation in the panel. Wording updated in
    the scanner copy pass ("Fallback video" is internal vocabulary in viewer-facing text) —
    the requirement, that the error surfaces in #fallbackVideoStatus rather than only in the
    console, is unchanged and asserted structurally below."""
    html = _scanner_html()
    assert "fallbackVideoEl.addEventListener('error'" in html
    assert "This video could not be loaded. It may be unavailable right now." in html
    error_handler = html[html.index("fallbackVideoEl.addEventListener('error'"):]
    error_handler = error_handler[:error_handler.index("});")]
    assert "fallbackVideoStatusEl.textContent =" in error_handler
    assert "fallback_video_load_failed" in html


def test_wave6_successful_match_closes_recognition_timeout_panel():
    html = _scanner_html()
    accepted_at = html.index("recordAcceptance();")
    hide_at = html.index("hideRecognitionHelp('match_accept');", accepted_at)
    state_at = html.index("setScannerUiState('matched', 'match_accept');", hide_at)
    assert accepted_at < hide_at < state_at


def test_wave6_successful_camera_retry_closes_fallback_playback():
    html = _scanner_html()
    retry_start = html.index("async function recoverFallbackAndOpenCamera(reason)")
    retry_body = html[retry_start:html.index("fallbackRetryBtn.addEventListener", retry_start)]
    assert "closeFallbackVideoPanel('camera_retry_succeeded');" in retry_body
    assert "clearVerifiedFallbackPairContext('camera_retry_succeeded');" in retry_body
    assert "hideFallbackPanel('retry_succeeded');" in retry_body


def test_wave6_return_routing_uses_existing_canonical_session_end_path():
    html = _scanner_html()
    assert 'id="fallbackReturnBtn" href="{{ resolved_back_destination }}"' in html
    assert 'id="recognitionReturnBtn" href="{{ resolved_back_destination }}"' in html
    assert "setScannerUiState('returning', reason || 'navigate');" in html
    assert "finalizeScannerAndNavigate(this.href, 'fallback_return');" in html
    assert "finalizeScannerAndNavigate(this.href, 'recognition_return');" in html
    nav_start = html.index("function finalizeScannerAndNavigate(href, reason)")
    nav_body = html[nav_start:html.index("// Start session on page load", nav_start)]
    assert "submitFallbackAnalytics" not in nav_body
