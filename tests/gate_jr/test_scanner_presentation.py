"""Public scanner presentation contracts.

Companion to test_scanner_lifecycle.py / test_gate_jr_scanner_recovery.py, which own the
scanner's state machine, camera lifecycle and geometry. This file owns only the things a
viewer can see and hear on templates/user/scanner.html:

  * copy hygiene — no engine/infrastructure/release vocabulary in RENDERED VISIBLE text
    (developer instrumentation in comments, console.log and diagnostics payloads is
    deliberately in scope for the codebase and out of scope for this check),
  * one ARIA announcement channel, not two,
  * touch targets, focus-visible and reduced-motion,
  * the motion budget (one infinite animation visible during an active state),
  * the Recognition Quad being a presentation-only aiming guide that never carries
    detection geometry and is never drawn over the tracked video,
  * Direct QR never showing image-target guidance.

The byte-identity guard for static/js/scanner-runtime.js and scanner_runtime.py is NOT
duplicated here — it already lives in
test_scanner_lifecycle.py::test_scanner_runtime_js_and_scanner_runtime_py_are_byte_identical_to_baseline
and that is the single place it should be asserted.
"""
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

pytestmark = pytest.mark.scanner_robustness


def _scanner_html():
    return Path("templates/user/scanner.html").read_text(encoding="utf-8", errors="ignore")


def render_scanner(app_module, **overrides):
    """Render scanner.html directly. Same context shape as
    test_v11_experience_ux.render_scanner (tests/ is not an importable package, so it cannot
    be imported); Direct QR Video is still only reachable through the template.

    Note the intro screen sits in front of everything until Start Camera is pressed, so a
    rendered-markup check is the only way to see the scanning chrome's initial state."""
    context = {
        "project_id": 1,
        "project_name": "Demo Story",
        "qr_code_url": "qr.png",
        "creator_type": "user",
        "creator_name": "Creator",
        "scanner_diagnostics_enabled": False,
        "scanner_entry_context": "public_viewer",
        "resolved_back_destination": "/",
        "back_destination_reason": "public_viewer",
        "entry_route_type": "public_scanner_route",
        "entry_authorization_result": "n/a_public",
        "experience_type": "image_video",
        "playback_mode": "tracked_overlay",
        "targets": [{"index": 0, "image_url": "/image/1/0", "video_url": "/video/1/0", "label": "Target 1"}],
    }
    context.update(overrides)
    if context["experience_type"] == "direct_qr" and "playback_mode" not in overrides:
        context["playback_mode"] = "direct"
    with app_module.app.test_request_context("/scanner/1"):
        return app_module.app.jinja_env.get_template("user/scanner.html").render(**context)


# Vocabulary that must never reach a public viewer's eyes. Split into two groups because
# "worker"/"queue"/"pair" are only wrong as VISIBLE words — they appear legitimately in
# identifiers and comments all over the file.
FORBIDDEN_VISIBLE_TERMS = (
    "opencv",
    "vision engine",
    "marker engine",
    "tracking engine",
    "homography",
    "feature extraction",
    "feature matching",
    "orb",
    "wasm",
    "webassembly",
    "redis",
    "rq worker",
    "worker",
    "queue",
    "ar engine",
    "ar marker",
    "marker",
    " v1.1",
    "wave 7",
    "getusermedia",
    "sessionstorage",
    "stack trace",
)


class VisibleText(HTMLParser):
    """Collects only text a viewer could actually read: no <script>, no <style>, no comments,
    plus the attributes that are spoken or shown (aria-label, alt, title, placeholder)."""

    SKIP = {"script", "style", "template"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
            return
        for name, value in attrs:
            if name in {"aria-label", "alt", "title", "placeholder"} and value:
                self.chunks.append(value)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            self.chunks.append(data)

    # handle_comment is intentionally not implemented: comments are not visible.


def _visible_text(html):
    parser = VisibleText()
    parser.feed(html)
    return re.sub(r"\s+", " ", " ".join(parser.chunks)).lower()


def _without_comments(html):
    """Strips every comment form this template uses — HTML, Jinja, JS block and JS line — so a
    "this used to be X, and X is now gone" comment cannot fail the check it documents. Author
    notes explaining a removal are exactly what these files are full of."""
    html = re.sub(r"<!--.*?-->", " ", html, flags=re.DOTALL)
    html = re.sub(r"\{#.*?#\}", " ", html, flags=re.DOTALL)
    html = re.sub(r"/\*.*?\*/", " ", html, flags=re.DOTALL)
    html = re.sub(r"(?m)^\s*//.*$", " ", html)
    return re.sub(r"(?<![:'\"`])//[^\n'\"`]*$", " ", html, flags=re.MULTILINE)


def _js_string_literals(html):
    """Every single/double-quoted and backtick literal inside the inline scanner <script>.
    A string that is only ever passed to console.log / scannerDiagnostics.push is developer
    instrumentation; a string assigned to .textContent / .innerHTML / an aria-* attribute is
    shipped copy. This returns the shipped-copy ones only."""
    literals = []
    pattern = re.compile(
        r"""(?:\.textContent|\.innerHTML|\.innerText|\.value|\.placeholder|setAttribute\(\s*'aria-label'\s*,)\s*=?\s*"""
        r"""(?P<expr>(?:[^;\n]|\n\s*[?:])*)""",
    )
    for match in pattern.finditer(html):
        expr = match.group("expr")
        literals.extend(re.findall(r"""'([^'\n]*)'|"([^"\n]*)"|`([^`\n]*)`""", expr))
    # `${...}` inside a template literal is an expression, not viewer copy — strip it so a
    # variable NAME (opencvLoadAttempts) is never mistaken for a word shown to a viewer.
    return [
        re.sub(r"\$\{[^}]*\}", "", part)
        for group in literals
        for part in group
        if part
    ]


# --------------------------------------------------------------------------------------
# Copy hygiene
# --------------------------------------------------------------------------------------

def test_rendered_public_scanner_shows_no_engine_or_infrastructure_vocabulary(app_module):
    for experience_type in ("image_video", "direct_qr"):
        text = _visible_text(render_scanner(app_module, experience_type=experience_type))
        for term in FORBIDDEN_VISIBLE_TERMS:
            assert term not in text, f"{experience_type}: visible copy exposes {term!r}"


def test_status_strings_assigned_into_the_dom_use_viewer_language():
    """The rendered-markup check above cannot see text that JS writes later, so the shipped
    copy assignments are checked at the source too. Only strings that actually land in the
    DOM are inspected — console.log and scannerDiagnostics payloads are developer
    instrumentation and stay exactly as they are."""
    html = _scanner_html()
    for literal in _js_string_literals(html):
        lowered = literal.lower()
        for term in ("opencv", "vision engine", "ar marker", "marker", "homography", "wasm", "redis", "queue"):
            assert term not in lowered, f"DOM-bound string exposes {term!r}: {literal!r}"


def test_no_release_or_wave_terminology_in_rendered_output(app_module):
    text = _visible_text(render_scanner(app_module))
    for term in ("v1.1", "wave ", "gate jr", "phase 3", "pass 13"):
        assert term not in text, term


# --------------------------------------------------------------------------------------
# ARIA / accessibility
# --------------------------------------------------------------------------------------

def test_scanner_guidance_is_the_single_aria_live_status_channel():
    """#scannerGuidance is the one live region for scanner state. #scannerStatusOverlay is
    its visual echo and must be aria-hidden, or assistive tech announces every meaningful
    transition twice."""
    html = _scanner_html()
    assert 'id="scannerGuidance"' in html
    assert 'role="status"' in html and 'aria-live="polite"' in html and 'aria-atomic="true"' in html
    assert '<div id="scannerStatusOverlay" aria-hidden="true">' in html
    assert '<div id="recognitionQuad" aria-hidden="true">' in html
    assert '<div class="info-badge" id="infoBadge" aria-hidden="true"></div>' in html


def test_announcements_are_driven_by_state_changes_not_by_every_frame():
    """setScannerGuidance is the only writer of the live region, it debounces lower-priority
    changes, and updateGuidanceDom skips the DOM write when the text has not actually
    changed — so a per-frame detection callback cannot produce a stream of announcements."""
    html = _scanner_html()
    assert "function updateGuidanceDom(state, text)" in html
    assert "if (guidanceEl.textContent !== text) guidanceEl.textContent = text;" in html
    assert "const GUIDANCE_DEBOUNCE_MS = 900" in html
    assert "scanner_guidance_debounced" in html
    # Exactly one place writes the live region's text.
    assert html.count("guidanceEl.textContent = text") == 1


def test_a_hard_startup_failure_is_announced_in_the_live_region():
    """#scannerStatusOverlay is aria-hidden, so the startup safety net must speak through
    #scannerGuidance or a screen-reader user is told nothing about a fatal failure."""
    html = _scanner_html()
    handler = html[html.index("window.addEventListener('error'"):html.index("markStartupCheckpoint('scanner_script_entered')")]
    assert "getElementById('scannerGuidance')" in handler
    assert "ScanStory could not start. Tap Retry to try again." in handler
    assert "startupRetryBtn" in handler and "min-height:44px" in handler


def test_report_dialog_keeps_its_semantics_and_safe_default():
    html = _scanner_html()
    assert 'id="reportSheet" hidden role="dialog" aria-modal="true" aria-labelledby="reportSheetTitle"' in html
    assert 'aria-haspopup="dialog" aria-controls="reportSheet"' in html
    # Submit stays off until a reason is chosen, and Cancel is always available.
    assert 'id="reportSubmitBtn" disabled>Submit report</button>' in html
    assert 'id="reportCancelBtn">Cancel</button>' in html
    # Truthful submitted state — never a promise of automatic removal or a ban.
    assert "Report submitted for review." in html
    assert "nothing is removed automatically" in html
    code = _without_comments(html)
    for overclaim in ("will be removed", "has been removed", "banned", "taken down"):
        assert overclaim not in code, overclaim


def test_first_screen_touch_targets_meet_the_minimum():
    html = _scanner_html()
    intro_btn_css = html[html.index(".intro-btn {"):html.index(".intro-btn:hover")]
    assert "min-height: 52px;" in intro_btn_css
    back_btn_css = html[html.index(".back-btn {"):html.index(".back-btn:hover")]
    assert "min-height: 44px;" in back_btn_css
    # The report trigger and every in-lens recovery button use the shared 44px button roles.
    # Issue 3E-E's "Replay all" completion button reuses this same role rather than
    # inventing a new one, hence 3 not 2.
    assert 'class="ss-report-trigger"' in html
    assert "min-height: 44px;" in Path("static/css/design-system.css").read_text(encoding="utf-8")
    assert html.count("ss-scan-btn ss-scan-btn-primary") == 3


def test_intro_dialog_moves_focus_to_its_one_primary_action():
    html = _scanner_html()
    body = html[html.index("function showExperienceIntro()"):html.index("function selectedPlaybackMode()")]
    assert "const primaryAction = startCameraBtn || directQrPlayBtn;" in body
    assert "primaryAction.focus({ preventScroll: true })" in body


def test_focus_visible_is_styled_for_the_scanner_own_controls():
    html = _scanner_html()
    assert ".intro-btn:focus-visible" in html
    assert ".intro-link:focus-visible" in html


# --------------------------------------------------------------------------------------
# Motion budget / reduced motion
# --------------------------------------------------------------------------------------

def test_reduced_motion_is_honoured_by_the_scanner_page_itself():
    html = _scanner_html()
    block_start = html.index("@media (prefers-reduced-motion: reduce)")
    block = html[block_start:html.index("</style>", block_start)]
    assert ".scanner-dot {" in block and "animation: none;" in block
    assert "#recognitionQuad," in block
    assert "#scannerStatusWord {" in block
    assert "#arCrosshair svg {" in block


def test_motion_budget_allows_one_infinite_animation_per_visible_state():
    """The audit found ~14 infinite animations on this page. What is left is exactly two
    declarations, and they can never be on screen at the same time: the opening screen's ring
    (#arCrosshair, hidden the moment the intro appears) and the scanning progress dots."""
    code = _without_comments(_scanner_html())
    infinite = re.findall(r"animation:[^;\"']*infinite", code)
    assert len(infinite) == 2, infinite
    assert any("scannerDotBounce" in decl for decl in infinite)
    assert any("arSpin" in decl for decl in infinite)
    # The specific loops the audit called out are gone for good.
    for removed in ("mistDrift", "mistFlow", "mistWave", "gradientShift", "arSpinReverse", "arPing", "dotBounce"):
        assert removed not in code, removed


def test_no_large_blurred_moving_layers_remain_over_the_camera_feed():
    code = _without_comments(_scanner_html())
    assert "scanner-mist" not in code
    # The two 340/380px blur(100px) colour blobs on the opening screen are gone as well.
    for heavy in ("blur(100px)", "blur(60px)", "blur(40px)"):
        assert heavy not in code, heavy
    # The only blur left is the 6px scrim behind an in-lens recovery panel: small, static, and
    # only ever on screen once the camera has already stopped or recognition has given up.
    assert code.count("blur(6px)") == 2


def test_the_lock_on_moment_is_one_short_settle_not_a_celebration_loop():
    html = _scanner_html()
    assert "@keyframes lockOnSettle" in html
    settle = html[html.index(".match-indicator {"):html.index("@keyframes lockOnSettle")]
    assert "animation: lockOnSettle 200ms" in settle
    assert "infinite" not in settle
    # Driven by the state the scanner already emits on an accepted detection.
    assert 'html[data-scanner-ui-state="matched"] #recognitionQuad {' in html
    quad_css = html[html.index("#recognitionQuad {"):html.index("html[data-scanner-ui-state=\"matched\"] #status,")]
    assert "transition: opacity 200ms ease, transform 200ms cubic-bezier(0.22, 1, 0.36, 1);" in quad_css


# --------------------------------------------------------------------------------------
# Recognition Quad: presentation only
# --------------------------------------------------------------------------------------

def test_recognition_quad_is_static_css_and_carries_no_detection_geometry():
    """It must never look like, or be mistaken for, the real detection overlay: no JS touches
    it at all, so it cannot be fed real or fabricated corner coordinates."""
    html = _scanner_html()
    assert 'id="recognitionQuad"' in html
    assert "recognitionQuad" not in html[html.index("<script>", html.index("scanner-runtime.js")):]
    quad_markup = html[html.index('<div id="recognitionQuad"'):]
    quad_markup = quad_markup[:quad_markup.index("</div>")]
    for forbidden in ("corner", "matrix3d", "transform:", "data-", "{{"):
        assert forbidden not in quad_markup, forbidden


def test_recognition_quad_is_a_separate_element_from_the_tracked_overlay():
    html = _scanner_html()
    overlay_start = html.index('<div id="overlayWrap">')
    overlay_end = html.index("</div>", overlay_start)
    assert 'id="recognitionQuad"' not in html[overlay_start:overlay_end]
    assert html.index('id="recognitionQuad"') > overlay_end


def test_nothing_decorative_is_drawn_over_the_playing_video():
    """The quad shows only while the viewer is still looking for the image. 'matched' and
    'fallback_playing' — the two states where content is on screen — never display it, and
    the pair-index chip is no longer shown over the video at all."""
    html = _scanner_html()
    show_rule = html[html.index('html[data-scanner-ui-state="scanning"] #recognitionQuad,'):]
    show_rule = show_rule[:show_rule.index("}")]
    assert "recognition_timeout" in show_rule
    assert "matched" not in show_rule
    assert "fallback_playing" not in show_rule
    match_fn = html[html.index("function showMatchIndicator(pairId)"):html.index("function updateProgress(ready, total)")]
    assert "infoBadge.style.display" not in match_fn
    assert 'matchIndicator.textContent = "✨ Found it";' in match_fn


# --------------------------------------------------------------------------------------
# Direct QR
# --------------------------------------------------------------------------------------

def test_direct_qr_never_shows_image_target_guidance(app_module):
    html = render_scanner(app_module, experience_type="direct_qr")
    assert 'id="recognitionQuad"' not in html
    assert 'id="targetGuide"' not in html
    assert 'id="startCameraBtn"' not in html
    text = _visible_text(html)
    for aiming_phrase in ("point your camera", "point at the", "point back at", "looking for image", "start camera"):
        assert aiming_phrase not in text, aiming_phrase
    # Direct QR viewer UX upgrade (local Creator Integrity pass): copy now
    # reads "story" rather than "video" for the single-video case, and states
    # the video count for a multi-video playlist - see scanner.html's
    # direct_qr_playlist-gated lede.
    assert "your story is ready. no camera needed." in text


def test_image_recognition_scanner_still_shows_the_aiming_guidance(app_module):
    html = render_scanner(app_module, experience_type="image_video")
    assert 'id="recognitionQuad"' in html
    text = _visible_text(html)
    assert "point your camera at this to start the experience." in text
    assert "start camera" in text


# --------------------------------------------------------------------------------------
# Failure presentation
# --------------------------------------------------------------------------------------

def test_each_failure_kind_reads_differently_and_never_blames_the_wrong_thing():
    """A vision-engine load timeout is not a missing camera. The four failure kinds must be
    four distinguishable sentences, and the not-ready case must not claim the camera failed
    (behaviourally that confusion is what the first-camera-start fix untangled — it must not
    come back as copy)."""
    html = _scanner_html()
    assert "const VIEWER_ERROR_MESSAGES = Object.freeze({" in html
    assert "function isCameraFailureCode(code)" in html
    assert "function viewerErrorMessage(code)" in html
    # enterFallback renders the viewer message, not runtime.ERRORS' engine wording.
    fallback_fn = html[html.index("function enterFallback(code)"):html.index("async function recoverFallbackAndOpenCamera(reason)")]
    assert "const safe = viewerErrorMessage(code);" in fallback_fn
    assert "setStatusWord(isCameraFailureCode(code) ? 'Camera Unavailable' : 'Not Quite Ready', safe);" in fallback_fn

    messages = html[html.index("const VIEWER_ERROR_MESSAGES = Object.freeze({"):]
    messages = messages[:messages.index("});")]
    assert "vision engine" not in messages.lower()
    not_ready = messages[messages.index("OPENCV_LOAD_FAILED:"):messages.index("WASM_LOAD_FAILED:")]
    assert "camera could not" not in not_ready.lower()
    assert "no camera" not in not_ready.lower()
    assert "could not finish getting ready" in not_ready


def test_every_failure_state_offers_one_primary_recovery_action():
    html = _scanner_html()
    panel = html[html.index('id="fallbackPanel"'):html.index('id="recognitionHelpPanel"')]
    assert "ss-scan-btn ss-scan-btn-primary" in panel and "Retry Camera" in panel
    help_panel = html[html.index('id="recognitionHelpPanel"'):html.index('</div>\n  </div>\n\n  <div id="soundGate">')]
    assert "ss-scan-btn ss-scan-btn-primary" in help_panel and "Continue Scanning" in help_panel
    # And a way out that agrees with Back on where "away from the scanner" goes.
    assert html.count('href="{{ resolved_back_destination }}"') == 3


# --------------------------------------------------------------------------------------
# Mobile layout
# --------------------------------------------------------------------------------------

def test_mobile_layout_respects_safe_areas_and_never_scrolls_sideways():
    html = _scanner_html()
    body_css = html[html.index("    body {"):html.index("    .wrap {")]
    assert "calc(12px + env(safe-area-inset-bottom, 0px))" in body_css
    assert "overflow-x: hidden;" in body_css
    # The fixed video panel clears the home indicator too.
    assert "bottom: max(12px, env(safe-area-inset-bottom));" in html
    assert "padding: 24px 18px calc(24px + env(safe-area-inset-bottom, 0px));" in html


def test_status_text_can_wrap_instead_of_clipping_at_the_lens_edge():
    """#scannerStatusWord sits inside .wrap (overflow:hidden). A fixed min-width plus
    white-space:nowrap clipped longer words on a 320/360px phone."""
    code = _without_comments(_scanner_html())
    word_css = code[code.index("    #scannerStatusWord {"):code.index("    @keyframes statusWordPulse")]
    assert "min-width" not in word_css
    assert "white-space: nowrap" not in word_css
    assert "font-size: clamp(15px, 4.4vw, 18px);" in word_css
    assert "max-width: min(280px, 78vw);" in word_css


def test_device_diagnostics_are_not_rendered_to_public_viewers(app_module):
    """#deviceInfo prints "mobile 4GB 4cores standard" — real diagnostics, not viewer copy.
    The element and its textContent write are unchanged; it is simply not shown unless the
    server enabled the diagnostics build."""
    html = _scanner_html()
    device_css = html[html.index("    .device-info {"):html.index("    .progress-info {")]
    assert "display: none;" in device_css
    assert "{% if scanner_diagnostics_enabled %}\n    .device-info { display: block; }" in html
    assert "document.getElementById('deviceInfo').textContent = " in html
    public = render_scanner(app_module)
    assert ".device-info { display: block; }" not in public
    debug = render_scanner(app_module, scanner_diagnostics_enabled=True)
    assert ".device-info { display: block; }" in debug
