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
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.scanner_robustness


def _scanner_html():
    return Path("templates/user/scanner.html").read_text(encoding="utf-8", errors="ignore")


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
    the <script src="...scanner-runtime.js"> tag right before it."""
    for match in re.finditer(r"<script\b([^>]*)>(.*?)</script>", html, re.DOTALL):
        attrs, body = match.group(1), match.group(2)
        if "src=" not in attrs and body.strip():
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
    drop_start = html.index("function dropTracking(reason, extraMats)")
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
    must read as marker/detection language, not camera-starting language."""
    html = _scanner_html()
    assert 'target_lost: ["Marker Lost"' in html
    assert 'recovering: ["Reacquiring Marker"' in html
    assert '"Preparing Camera"' in html  # still exists, just not attached to these states


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
    assert "} finally {" in html
    assert "detectInFlight = false;" in html


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


def test_scan_request_is_always_rescheduled_after_any_outcome():
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
    assert "scheduleNextScan('after_tick');" in body[finally_idx:]


def test_scan_in_flight_cleared_after_every_outcome():
    """detectInFlight (this codebase's scanInFlight) clears in detectOnceFromServer's own
    finally block — independently of scanTick's reschedule guarantee above."""
    html = _scanner_html()
    detect_start = html.index("async function detectOnceFromServer(triggeredByWatchdog)")
    detect_end = html.index("async function scanTick(token)")
    detect_body = html[detect_start:detect_end]
    assert "} finally {" in detect_body
    assert "detectInFlight = false;" in detect_body


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
    (loading_opencv -> loading_wasm -> initializing_scanner -> ready_to_scan), so all four
    steps must be walked through once metadata actually loads."""
    html = _scanner_html()
    onloadedmetadata_start = html.index("cam.onloadedmetadata = async () => {")
    onloadedmetadata_end = html.index("} catch (err) {", onloadedmetadata_start)
    body = html[onloadedmetadata_start:onloadedmetadata_end]
    assert "safeTransition('loading_opencv', 'camera_ready');" in body
    assert "safeTransition('loading_wasm', 'camera_ready');" in body
    assert "safeTransition('initializing_scanner', 'camera_ready');" in body
    assert "safeTransition('ready_to_scan', 'camera_ready');" in body
    # ordering matches the only valid TRANSITIONS chain — index the actual calls, not the
    # bare state names (which also appear earlier, in this function's own explanatory
    # comment about why the chain is needed)
    assert (body.index("safeTransition('loading_opencv'") < body.index("safeTransition('loading_wasm'")
            < body.index("safeTransition('initializing_scanner'") < body.index("safeTransition('ready_to_scan'"))


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
    camera were healthy."""
    html = _scanner_html()
    recover_start = html.index("async function recoverScannerInner(reason, restartCamera)")
    recover_end = html.index("function stopCameraStream(reason)")
    body = html[recover_start:recover_end]
    not_recovered_idx = body.index("if (!recovered) {")
    enter_fallback_idx = body.index("enterFallback('CAMERA_UNAVAILABLE');", not_recovered_idx)
    return_idx = body.index("return;", enter_fallback_idx)
    ready_to_scan_idx = body.index("safeTransition('ready_to_scan', reason);")
    assert not_recovered_idx < enter_fallback_idx < return_idx < ready_to_scan_idx
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


def test_retry_camera_invokes_guarded_recovery_and_avoids_concurrent_starts():
    html = _scanner_html()
    assert "async function retryCameraFromFallback()" in html
    assert "if (fallbackRetryInProgress || diagState.cameraStartInProgress) return;" in html
    assert "fallbackRetryBtn.addEventListener('click', retryCameraFromFallback);" in html
    assert "await setupCamera();" in html


def test_successful_retry_hides_fallback_panel():
    html = _scanner_html()
    assert "function hideFallbackPanel(reason)" in html
    assert "hideFallbackPanel('retry_succeeded');" in html
    retry_start = html.index("async function retryCameraFromFallback()")
    retry_end = html.index("fallbackRetryBtn.addEventListener('click', retryCameraFromFallback);")
    body = html[retry_start:retry_end]
    assert "if (!isStreamDead()) {" in body
    assert "hideFallbackPanel('retry_succeeded');" in body


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
    assert "project.owner_user_id == session_user_id" in resolver_body
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
    assert "project.owner_user_id != user.id" in creator_body
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


def test_watchdog_does_not_create_concurrent_requests():
    html = _scanner_html()
    watchdog_start = html.index("function watchdogTick(token)")
    watchdog_end = html.index("function startDetectLoop()")
    body = html[watchdog_start:watchdog_end]
    assert "if (!detectInFlight && !isStreamDead()) {" in body


def test_watchdog_respects_hidden_state():
    html = _scanner_html()
    watchdog_start = html.index("function watchdogTick(token)")
    watchdog_end = html.index("function startDetectLoop()")
    body = html[watchdog_start:watchdog_end]
    assert "if (document.hidden || recoverScannerPromise || diagState.cameraStartInProgress) {" in body


def test_watchdog_respects_recovery_state():
    html = _scanner_html()
    watchdog_start = html.index("function watchdogTick(token)")
    watchdog_end = html.index("function startDetectLoop()")
    body = html[watchdog_start:watchdog_end]
    assert "recoverScannerPromise" in body


def test_watchdog_uses_current_scan_loop_token():
    html = _scanner_html()
    assert "function scheduleWatchdog(token)" in html
    watchdog_start = html.index("function watchdogTick(token)")
    watchdog_end = html.index("function startDetectLoop()")
    body = html[watchdog_start:watchdog_end]
    assert "if (sessionEnding || token !== scanLoopToken) return;" in body
    assert "scheduleWatchdog(scanLoopToken);" in html  # started by startDetectLoop with the CURRENT token


def test_watchdog_never_restarts_the_camera():
    html = _scanner_html()
    watchdog_start = html.index("function watchdogTick(token)")
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
    start = html.index("function watchdogTick(token)")
    end = html.index("function skipTick(reason, extra)")
    return html[start:end]


def test_watchdog_aborts_a_request_stuck_past_its_own_deadline():
    body = _watchdog_tick_body()
    assert "detectInFlight && diagState.lastRequestStartAt && elapsed > WATCHDOG_TIMEOUT_MS" in body
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
    assert "project_owner_id = project.owner_user_id" in body  # ownership resolved from the DB record
    assert 'request.args.get("user_id"' not in body  # no code path left for a tampered param to reach


def test_project_owner_is_resolved_from_the_database_record():
    body = _scanner_view_body()
    assert "project_owner_id = project.owner_user_id" in body
    owner_line_idx = body.index("project_owner_id = project.owner_user_id")
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
    assert "scan_attribution_owner_id = project.owner_user_id" in body
    assert 'session.get("user_id")' not in body
    assert "ScanLog(" in body
    assert "user_id=scan_attribution_owner_id," in body


def test_scanner_session_end_attributes_to_project_owner_not_session():
    app_src = _app_py()
    start = app_src.index("def scanner_session_end():")
    end = app_src.index('@app.route("/detect_track"')
    body = app_src[start:end]
    assert "scan_attribution_owner_id = project.owner_user_id if project else None" in body
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
    assert "Test Scanner" in html


def test_project_preview_exposes_test_scanner():
    html = _project_preview_html()
    assert "url_for('scanner_test_entry', project_id=project.id)" in html
    assert "Test Scanner" in html


def test_success_page_exposes_test_scanner():
    html = _success_html()
    assert "{{ test_scanner_url }}" in html
    assert "Test Scanner" in html
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
    independently re-checks project.owner_user_id != user.id server-side regardless of which
    template linked to it. This test confirms the templates never bypass that route with
    their own inline token/context construction."""
    for html in (_dashboard_html(), _project_preview_html()):
        assert "_issue_scanner_test_token" not in html
        assert "test_token=" not in html
    app_src = _app_py()
    creator_start = app_src.index('@app.route("/project/<int:project_id>/scanner-test")')
    creator_end = app_src.index('@app.route("/admin/project/<int:project_id>/scanner-test")')
    assert "project.owner_user_id != user.id" in app_src[creator_start:creator_end]


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
    end = html.index("const durationMs = Math.round")
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
    drop_tracking_start = html.index("function dropTracking(reason, extraMats)")
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
    real_abort_start = body.index("} else if (detectInFlight && diagState.lastRequestStartAt && activeDetectionController) {")
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


def test_frame_capture_runs_before_active_detection_controller_is_assigned():
    """Documents the actual root cause: frame capture (drawImage/toBlob) has no
    AbortController of its own and runs BEFORE activeDetectionController is assigned —
    confirmed directly from source ordering, not assumed."""
    html = _scanner_html()
    start = html.index("async function detectOnceFromServer(triggeredByWatchdog)")
    end = html.index("async function scanTick(token)")
    body = html[start:end]
    capture_idx = body.index("ctx.drawImage(cam, 0, 0, cap.width, cap.height);")
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
    present — no change made to the video element."""
    html = _scanner_html()
    assert '<video id="overlay" autoplay playsinline loop preload="auto"></video>' in html


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
    """The only legitimate source reassignment is the marker-switch branch in the accept
    path — confirms no other code path (loop detection, ended handler, pause/stop) recreates
    or reloads the video element's source, matching 'preserve a single long-lived video
    element' from earlier passes."""
    html = _scanner_html()
    assert html.count("overlay.src = ") == 1
    assign_idx = html.index("overlay.src = ")
    nearby = html[max(0, assign_idx - 300):assign_idx]
    assert "[MARKER SWITCH]" in nearby


def test_marker_loss_still_performs_cleanup_and_logs_playback_state():
    html = _scanner_html()
    start = html.index("function dropTracking(reason, extraMats)")
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
        body.index("ctx.drawImage(cam, 0, 0, cap.width, cap.height);"),
        body.index("logTimingCheckpoint('[DRAW IMAGE END]'"),
        body.index("logTimingCheckpoint('[TOBLOB START]'"),
        body.index("cap.toBlob(res, \"image/jpeg\", 0.85)"),
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
    real_abort_start = watchdog_body.index("} else if (detectInFlight && diagState.lastRequestStartAt && activeDetectionController) {")
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
    real_abort_start = watchdog_body.index("} else if (detectInFlight && diagState.lastRequestStartAt && activeDetectionController) {")
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
    assert "detectInFlight = false;" in guard_body
    assert "return;" in guard_body


def test_marker_loss_reasons_are_all_genuine_local_tracking_failures():
    """Audit 5 / Case E: every dropTracking() caller must be a local optical-flow/geometry
    failure inside trackFrame() — never a network/watchdog/session-related reason. Proves
    [TRACKING LOST DURING PLAYBACK] represents genuine marker loss, not a request-gap
    side effect, and that no change to tracking behaviour is warranted."""
    html = _scanner_html()
    track_start = html.index("function trackFrame()")
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
    assert "dropTracking('pose_rejected_' + localPoseQuality.reason" in track_body
    # none of these are network/session/watchdog-caused
    for forbidden in ("network_timeout", "capture_timeout", "session_ending", "watchdog"):
        assert forbidden not in track_body
    # dropTracking is only ever CALLED from within trackFrame — never from detect_init
    # response handling, watchdog, or session-end code. (dropTracking's own function
    # DEFINITION lives earlier in the file, before trackFrame — that's the "+1" below.)
    assert html.count("dropTracking(") == track_body.count("dropTracking(") + 1


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
    watchdog_start = html.index("function watchdogTick(token)")
    body = html[watchdog_start:watchdog_start + 400]
    assert "if (sessionEnding || token !== scanLoopToken) return;" in body


# ---------------------------------------------------------------------------
# Final conclusive verification: shared canvas / coordinate-space hypothesis.
# These describe CURRENT behaviour only — no production code changed to add
# or pass these tests.
# ---------------------------------------------------------------------------

def _track_frame_body():
    html = _scanner_html()
    start = html.index("function trackFrame()")
    end = html.index("async function detectOnceFromServer(triggeredByWatchdog)")
    return html[start:end]


def test_capture_and_tracking_share_one_canvas_and_context():
    """Proof 1: exactly one <canvas>/2D-context pair exists and is used by both the
    server-capture path (detectOnceFromServer) and the local-tracking path
    (matFromVideoGray, called every trackFrame tick) — not two independent resources."""
    html = _scanner_html()
    assert html.count('getElementById("cap")') == 1
    assert html.count('cap.getContext(') == 1
    gray_start = html.index("function matFromVideoGray()")
    gray_end = html.index("function cornersToMat(corners)")
    gray_body = html[gray_start:gray_end]
    assert "ctx.drawImage(cam, 0, 0, cap.width, cap.height)" in gray_body
    assert "ctx.getImageData(0, 0, cap.width, cap.height)" in gray_body


def test_detect_resizes_shared_canvas_before_network_await():
    """Proof 2/4: detectOnceFromServer mutates cap.width/height to detection dimensions
    SYNCHRONOUSLY, before the toBlob await and before the fetch await — i.e. before any
    point where the event loop could run a queued trackFrame() rAF callback in between."""
    html = _scanner_html()
    detect_start = html.index("async function detectOnceFromServer(triggeredByWatchdog)")
    detect_end = html.index("async function scanTick(token)")
    body = html[detect_start:detect_end]
    resize_at = body.index("cap.width = capW;")
    assert body.index("cap.height = capH;") > resize_at
    to_blob_at = body.index('cap.toBlob(res, "image/jpeg", 0.85)')
    fetch_at = body.index('await fetch("/detect_init"')
    assert resize_at < to_blob_at < fetch_at


def test_capture_dimensions_are_not_restored_before_response_arrives():
    """Proof 2/4: nothing restores cap.width/height back to frameW/frameH between the
    capture-dimension resize and the fetch's await resolving — the only restoration is
    inside the accepted-detection branch, which runs strictly after `await r.json()`.
    A rejected/no-detection/stale response leaves cap at detection dimensions."""
    html = _scanner_html()
    detect_start = html.index("async function detectOnceFromServer(triggeredByWatchdog)")
    detect_end = html.index("async function scanTick(token)")
    body = html[detect_start:detect_end]
    resize_at = body.index("cap.width = capW;")
    json_at = body.index("const data = await r.json();")
    between = body[resize_at:json_at]
    assert "cap.width = frameW" not in between
    restore_at = body.index("cap.width = frameW; cap.height = frameH;")
    assert restore_at > json_at


def test_track_frame_has_no_capture_in_flight_guard():
    """Proof 6: trackFrame() never checks detectInFlight/activeDetectionController/capW/
    capH before calling matFromVideoGray() — nothing pauses or defers local tracking while
    a server-capture cycle currently owns (has resized) the shared canvas."""
    body = _track_frame_body()
    for forbidden in ("detectInFlight", "activeDetectionController", "capW", "capH"):
        assert forbidden not in body
    assert "matFromVideoGray()" in body


def test_matframevideogray_has_no_fixed_dimension_or_size_guard():
    """Proof 5: matFromVideoGray always draws/reads at whatever cap.width/cap.height
    currently are — no parameter, no comparison against frameW/frameH, no assertion that
    the canvas is still sized for tracking before producing a gray Mat. Combined with proof
    2/4 above, prevGray (captured at a previous cap size) and this call's gray Mat (captured
    at whatever size cap currently holds) can genuinely differ in dimensions."""
    html = _scanner_html()
    gray_start = html.index("function matFromVideoGray()")
    gray_end = html.index("function cornersToMat(corners)")
    gray_body = html[gray_start:gray_end]
    assert "frameW" not in gray_body
    assert "frameH" not in gray_body
    assert gray_body.count("cap.width") == 2  # drawImage + getImageData, both current-size


def test_trackframe_catch_block_bypasses_droptracking_and_geometry_clear():
    """Proof 7 (shape-loss connection): a thrown exception inside trackFrame's try block
    (e.g. cv.calcOpticalFlowPyrLK asserting prevGray/gray size equality, which a shared,
    externally-resized canvas can violate) is caught by a bare catch that only flips
    `tracking = false` — it does NOT call dropTracking()/clearTrackingGeometry(), so no
    [TRACK LOST] diagnostic is recorded and the overlay's last-applied transform/visibility
    is left exactly as-is (frozen), rather than explicitly held or hidden."""
    body = _track_frame_body()
    catch_start = body.rindex("} catch (e) {")
    catch_body = body[catch_start:body.index("}", body.index("tracking = false;", catch_start)) + 1]
    assert "tracking = false;" in catch_body
    assert "dropTracking(" not in catch_body
    assert "clearTrackingGeometry(" not in catch_body
    assert "requestPoseHold(" not in catch_body


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


def test_applywarp_and_render_path_use_frameW_frameH_not_cap_dimensions():
    """Scope-limiting proof: applyWarp (the server-response geometry-render path) reads the
    module-level frameW/frameH variables directly, never cap.width/cap.height — so the
    shared-canvas mutation proven above corrupts local OPTICAL-FLOW tracking specifically,
    not the server-response rendering math itself. Keeps the verdict precise rather than
    over-broad."""
    html = _scanner_html()
    warp_start = html.index("function applyWarp(cornersFrame, context = {})")
    warp_end = html.index("function poseCompatibility(nextCorners)")
    warp_body = html[warp_start:warp_end]
    assert "isOverlayFrameQuadRenderable(cornersFrame, frameW, frameH)" in warp_body
    assert "cap.width" not in warp_body
    assert "cap.height" not in warp_body
