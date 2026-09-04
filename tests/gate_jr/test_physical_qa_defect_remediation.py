"""Physical QA defect remediation pass — focused regression tests (SCANSTORY V1.1).

Covers the fixes made in response to real mobile/browser/network defects a human
tester found. Most of these are structural (string/regex-on-rendered-source)
checks in the same DOM-free idiom as test_scanner_frame_quality_guidance.py /
test_scanner_cold_start_js.py — runtime DOM/network behavior needs a real browser
or a real interrupted connection, neither of which pytest can honestly claim (see
the separate browser QA pass and the final report's "physical retest required"
list).

Run only this pack:
    python -m pytest tests/gate_jr/test_physical_qa_defect_remediation.py -q
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")


def _read(path):
    return Path(path).read_text(encoding="utf-8", errors="ignore")


def _scanner_html():
    return _read("templates/user/scanner.html")


def _creator_html():
    return _read("templates/user/user_create_project.html")


def _edit_html():
    return _read("templates/user/edit_project.html")


def _preview_html():
    return _read("templates/user/project_preview.html")


def _ownership_html():
    return _read("templates/user/ownership.html")


def _login_html():
    return _read("templates/user/login.html")


def _register_html():
    return _read("templates/user/register.html")


def _blog_html():
    return _read("templates/user/blog.html")


def _profile_html():
    return _read("templates/user/profile.html")


def _subscribe_html():
    return _read("templates/user/subscribe.html")


def _creator_nav_html():
    return _read("templates/user/_creator_nav.html")


# ===========================================================================
# Master consolidated stabilization pass — TC-04 pair resolution, section 33
# desktop viewport clipping, and public QA F1-F10.
# ===========================================================================

def test_login_desktop_form_no_longer_uses_unreachable_centering():
    """Section 33: .right-panel used justify-content: center inside a
    scrollable (overflow-y: auto) container - the classic flexbox trap where
    scrollTop can never go negative, so a form taller than a short desktop
    viewport had its top fields pushed above y=0 with no way to scroll up to
    them. Fixed with flex-start + a desktop-only auto-margin on .form-section
    that collapses to 0 (never negative) when content doesn't fit."""
    html = _login_html()
    panel_idx = html.index(".right-panel {")
    panel_block = html[panel_idx:html.index("}", panel_idx)]
    assert "justify-content: flex-start;" in panel_block
    assert "justify-content: center;" not in panel_block
    assert "@media (min-width: 1025px)" in html
    assert html.index("@media (min-width: 1025px)") < html.index(".form-section {\n                margin: auto 0;")


def test_register_desktop_form_no_longer_uses_unreachable_centering():
    html = _register_html()
    panel_idx = html.index(".left-panel {")
    panel_block = html[panel_idx:html.index("}", panel_idx)]
    assert "justify-content: flex-start;" in panel_block
    assert "justify-content: center;" not in panel_block
    assert "@media (min-width: 1025px)" in html
    assert html.index("@media (min-width: 1025px)") < html.index(".form-section {\n                margin: auto 0;")


def test_login_canonical_points_to_itself_not_the_homepage():
    """F10: was <link rel="canonical" href="https://myscanstory.com/"> - the
    homepage's own canonical, copy-pasted onto the login page. noindex/nofollow
    stays untouched."""
    html = _login_html()
    assert 'href="https://myscanstory.com/login/"' in html
    assert 'name="robots" content="noindex, nofollow"' in html


def test_blog_cancellation_refund_link_points_to_the_real_destination():
    """F2: blog.html's footer linked to /cancellation-refund, a route that does
    not exist (dead link) - article.html already correctly links to
    /terms#cancellation-refund, which terms.html actually has a matching
    heading for. blog.html now matches."""
    html = _blog_html()
    assert 'href="/cancellation-refund"' not in html
    assert 'href="/terms#cancellation-refund"' in html


def test_blog_topic_pills_are_no_longer_fake_navigation():
    """F9: every topic-cloud pill was <a href="/blog"> on the /blog page
    itself - clickable-looking navigation to nowhere. No archive/filter route
    exists, so these are now non-link chips (same idiom as the existing
    .blog-chip spans a few sections up on the same page)."""
    html = _blog_html()
    cloud_idx = html.index('class="topic-cloud"')
    cloud_block = html[cloud_idx:html.index("</div>", cloud_idx)]
    assert "<a " not in cloud_block
    assert cloud_block.count("<span class=\"topic-pill\">") == 10


def test_shared_marker_editor_module_exists_and_exports_the_expected_engine():
    """Section 1: creation and replacement must share the SAME marker-
    preparation engine, not two diverging implementations. Pins the exact
    exported surface both consumers depend on."""
    js = _read("static/js/marker-editor.js")
    assert "global.ScanStoryMarkerEditor" in js
    for fn in (
        "defaultCrop", "sanitizeCrop", "computeDrawRect", "cropHandles",
        "hitTestDragMode", "applyDragDelta", "drawCanvas",
        "drawCroppedToOutputCanvas", "exportDataUrl", "exportBlob",
        "evaluateQuality", "createController",
        "cameraSupported", "requestCameraStream", "stopCameraStream", "captureFrameAsBlob",
    ):
        assert (fn + ":") in js, fn


def test_creator_wizard_delegates_crop_math_to_the_shared_engine():
    """The wizard's own crop functions must call into MarkerEditor rather
    than re-implementing the geometry/draw/export math inline - otherwise
    this is a second, diverging ROI implementation in spirit even if it
    still works. Does not touch the wizard's own pointer-event wiring/debug
    logging, which stays page-owned by design (see marker-editor.js's
    header comment)."""
    html = _creator_html()
    assert "js/marker-editor.js" in html
    assert "const MarkerEditor = window.ScanStoryMarkerEditor;" in html
    for call in (
        "MarkerEditor.defaultCrop()", "MarkerEditor.sanitizeCrop(",
        "MarkerEditor.computeDrawRect(", "MarkerEditor.cropHandles(",
        "MarkerEditor.hitTestDragMode(", "MarkerEditor.applyDragDelta(",
        "MarkerEditor.drawCanvas(", "MarkerEditor.drawCroppedToOutputCanvas(",
        "MarkerEditor.evaluateQuality(",
    ):
        assert call in html, call


def test_replacement_flow_uses_the_shared_engine_and_explicit_image_source_choice():
    """Sections 1/2: Edit page's target-replacement flow must offer explicit
    Take a photo / Choose from device actions, both funnelling into the SAME
    shared crop editor (MarkerEditor.createController - full interaction
    parity with the wizard, not a hand-rolled simplified version), and must
    release the camera stream on capture/cancel/close."""
    html = _edit_html()
    assert "js/marker-editor.js" in html
    assert "startCameraCapture()" in html
    assert "chooseFromDevice()" in html
    assert "Camera is unavailable. Choose a photo from your device instead." in html
    assert "MarkerEditor.createController(" in html
    assert "MarkerEditor.exportBlob(" in html
    # Stream cleanup: every exit path calls stopCameraStream (capture, cancel,
    # and the modal's own close handler all route through it). The actual
    # track.stop() lifecycle is shared (marker-editor.js's stopCameraStream),
    # not reimplemented per page.
    stop_calls = html.count("stopCameraStream()")
    assert stop_calls >= 3, stop_calls
    assert "MarkerEditor.stopCameraStream(cameraStream)" in html
    assert "MarkerEditor.requestCameraStream()" in html
    assert "MarkerEditor.captureFrameAsBlob(" in html


def test_replacement_marker_writes_into_the_existing_image_input_not_a_new_endpoint():
    """The cropped File must land on the SAME image_{index} input the
    existing user_edit_project route already reads (via DataTransfer) -
    replacement ROI must not require a new server endpoint or duplicate the
    feature-regeneration/cache-busting logic that route already owns.

    Target-identity remediation pass (2026-08-29): confirmReplacementMarker() now
    also serves Edit -> Add another target (shared ROI pipeline), so the literal
    `document.getElementById('image_' + replacementPairIndex)` call became a computed
    inputId that resolves to 'new_pair_image' for a new target or 'image_<index>' for
    a replacement - the underlying invariant (Replace Target still lands on the
    existing image_<index> input) is unchanged, just expressed via that variable."""
    html = _edit_html()
    assert "new DataTransfer()" in html
    assert "input.files = dataTransfer.files" in html
    idx = html.index("async function confirmReplacementMarker()")
    block = html[idx:html.index("\n    function ", idx)]
    assert "const inputId = isNewTarget ? 'new_pair_image' : 'image_' + replacementPairIndex;" in block
    assert "document.getElementById(inputId)" in block
    # The real input this ultimately submits through is still the one
    # user_edit_project reads - only hidden now, not replaced by anything new.
    assert 'name="image_{{ pair.pair_index }}"' in html
    assert 'class="ss-visually-hidden-input"' in html


def test_creator_wizard_offers_explicit_take_a_photo_alongside_choose_from_device():
    """Section 2: creation must also offer explicit source choice, not just
    replacement. Take a photo shares the camera-stream lifecycle
    (MarkerEditor.requestCameraStream/stopCameraStream/captureFrameAsBlob)
    and, on capture, feeds the SAME handleFileSelect() path as choosing a
    file - so validation and the crop modal never fork per source."""
    html = _creator_html()
    assert "startWizardCameraCapture(" in html
    assert "MarkerEditor.requestCameraStream()" in html
    assert "MarkerEditor.stopCameraStream(wizardCameraStream)" in html
    assert "MarkerEditor.captureFrameAsBlob(" in html
    assert "Camera is unavailable. Choose a photo from your device instead." in html
    assert "handleFileSelect(input, pairId, 'image')" in html
    assert "Choose from device" in html


def test_latency_report_is_derived_from_existing_diagnostics_not_new_hot_path_work():
    """Section 3 (TC-09/TC-11): computeLatencyReport() must derive every
    named metric from the EXISTING scannerDiagnostics history/diagState -
    no new per-frame logging, no new work in the detect loop itself. QA/
    debug-mode only, gated the same way the rest of the diagnostics panel
    already is (scanner_diagnostics_enabled(), never true in production)."""
    html = _scanner_html()
    idx = html.index("function computeLatencyReport()")
    body = html[idx:html.index("\n      document.getElementById('diagExportBtn')", idx)]
    for field in (
        "camera_ready_ms", "detect_request_start_ms", "detect_request_end_ms",
        "network_request_ms", "server_match_ms", "matched_pair_id",
        "target_locked_ms", "video_source_change_ms", "playback_started_ms",
        "target_to_playback_ms",
    ):
        assert field in body, field
    assert "scannerDiagnostics.snapshot()" in body
    # Additive only - defined inside the existing diagPanelActive-gated block,
    # never touches the detect loop/scanLoop functions.
    assert "function startDetectLoop()" not in body


def test_diagnostics_export_now_includes_media_and_latency_state():
    """The pre-existing export button's payload was missing exactly the
    fields section 3 asks for: which pair/media is active, the playlist
    shape, and the current video source."""
    html = _scanner_html()
    export_idx = html.index("document.getElementById('diagExportBtn')")
    export_body = html[export_idx:html.index("URL.revokeObjectURL(url);", export_idx)]
    assert "latency: computeLatencyReport()" in export_body
    assert "playlistLength:" in export_body
    assert "activeMediaIndex:" in export_body
    assert "currentSrc:" in export_body
    assert "currentPairId:" in export_body


def test_scanner_debug_flag_survives_the_canonical_redirect():
    """Real bug found while verifying section 3 live: /scanner/<id> redirects
    to the canonical /s/<public_key> URL and used to forward ONLY test_token -
    ?scanner_debug=1 silently vanished, making the QA diagnostics panel
    unreachable through the documented entry point. Purely a client-
    visibility toggle to forward - the panel itself stays gated server-side
    by scanner_diagnostics_enabled(), never true in production regardless."""
    source = _read("app.py")
    idx = source.index("def scanner(project_id):")
    body = source[idx:source.index("\n\n\n", idx)]
    assert 'scanner_debug = request.args.get("scanner_debug")' in body
    assert 'query["scanner_debug"] = scanner_debug' in body


def test_healthy_tracking_suppression_is_bounded_by_force_redetect_ms():
    """Blocker audit (2026-08-27), TC-04 root cause: isHealthyLocalTracking()
    used to gate the scan-tick's early return UNCONDITIONALLY - pure local
    state flags (tracking/currCorners/prevGray/prevPts), never whether the
    server has confirmed those points still belong to the target in view.
    lastLockTs refreshes on every successful LOCAL optical-flow frame (not
    just real server detections), so the bounded re-anchor check that
    already existed a few lines below (sinceLastDetect > FORCE_REDETECT_MS)
    was provably unreachable - once local tracking latched onto anything
    that kept producing plausible frame-to-frame motion, no future detect
    request could ever be sent, even while pointed at a genuinely different,
    valid target. This is a scan-loop scheduling fix only - no recognition
    threshold, geometry check, or duplicate-guard logic changed."""
    html = _scanner_html()
    idx = html.index("if (isHealthyLocalTracking() && (performance.now() - lastDetectTs) <= FORCE_REDETECT_MS)")
    # The old unconditional form must not exist anywhere (a partial revert
    # would silently restore the bug).
    assert "if (isHealthyLocalTracking()) {\n          allowScanReschedule" not in html
    block = html[idx:html.index("return;", idx) + len("return;")]
    assert "allowScanReschedule = false;" in block
    assert "healthy_tracking_no_capture" in block
    # The bounded re-anchor this gate must not permanently shadow is still
    # reachable right after it.
    after = html[idx + 1:]
    assert "sinceLastDetect > FORCE_REDETECT_MS" in after


def test_finalize_retry_surfaces_real_rejections_instead_of_bare_transport_errors():
    """Blocker audit (2026-08-27), TC-05/07 device-inconsistent duplicate-
    target messaging: a real server rejection (e.g. DUPLICATE_TARGET_IMAGE)
    can still lose its HTTP response in transit on a flaky mobile
    connection - the client then only sees a transport-level error with no
    usable code, even though the server's decision was already made. If the
    reconciled session comes back 'active' (safe to retry - duplicate group
    finalizes are safe by contract), the client now retries once more so a
    real rejection can surface with its actual message instead of a bare
    connection error."""
    html = _creator_html()
    idx = html.index("async function finalizeResumableProjectWithBoundedRetry")
    body = html[idx:html.index("\n    /* Per-content-set progress", idx)]
    assert "recovered.session?.status === 'active'" in body
    assert "attempt < 2" in body


def test_client_duplicate_target_precheck_runs_at_marker_confirmation():
    """Section 3A: canonical duplicate check alongside the server's
    authoritative guard. Runs at "Use this marker" time against the
    canonical finalized marker state (crop/rotation/mode already applied -
    section 3B), not the raw pre-crop upload.

    Admin stabilization pass (2026-09-02): the client-only exact-hash
    pre-check (checkForDuplicateTargetAmongPairs) was replaced by a real
    server round-trip (validateTargetCandidateAgainstSiblings ->
    /create/validate-target -> canonical_target_identity_check) so a
    slightly different ROI on the same underlying photo is caught here too,
    not just at finalize time - see test_v11_admin_stabilization_part_a.py
    for the functional proof."""
    html = _creator_html()
    assert "async function validateTargetCandidateAgainstSiblings(candidateBlob, pairId)" in html
    idx = html.index("async function useCurrentMarker()")
    body = html[idx:html.index("\n    function drawCroppedMarkerToCanvas", idx)]
    # Narrow duplicate-handling fix (2026-09-01): activeCropPairId is now
    # captured into a local (rejectedPairId) at function entry before the
    # await, so a modal reopen for a DIFFERENT pair mid-check can never cause
    # clearRejectedTargetCandidate() to act on the wrong pair afterward.
    assert "const rejectedPairId = activeCropPairId;" in body
    assert "validateTargetCandidateAgainstSiblings(candidateBlob, rejectedPairId)" in body
    assert "clearRejectedTargetCandidate(rejectedPairId)" in body
    assert "validation === null" in body, "must have a controlled failure path, not silent fail-open"
    assert "This is the same photo already used for" in body
    assert "content set ${label}" in body


def test_ss_disclosure_chevron_is_shared_and_matches_the_marker_disclosure_convention():
    """Section 31: .ss-disclosure previously had no visible open/close
    affordance at all - a plain clickable line of text. Uses the SAME
    down-collapsed/up-expanded unicode-glyph-swap convention the Creator
    wizard's .marker-disclosure chevron already established, rather than
    introducing a second visual chevron style."""
    css = _read("static/css/design-system.css")
    block_start = css.index(".ss-disclosure > summary::after")
    block = css[block_start:css.index("}", css.index("}", block_start) + 1)]
    assert '\\25be' in block  # down chevron, collapsed
    assert '\\25b4' in block  # up chevron, [open]
    assert ".ss-disclosure[open] > summary::after" in css


def test_creator_nav_partial_reaches_every_canonical_destination():
    """Creator UI consistency pass (2026-08-27): the per-page links this test used
    to check on each page individually were centralized into one shared partial,
    templates/user/_creator_nav.html, included via {% include %} on every
    authenticated Creator page instead of six-plus divergent per-page navs. This
    is now the single source of truth for reaching Dashboard/My Stories/
    Ownership/Plans/Profile/Logout - pin it here once."""
    html = _creator_nav_html()
    for route in ("dashboard", "projects_page", "ownership_center", "user_profile", "subscribe_page", "logout"):
        assert f"url_for('{route}')" in html, route


def test_edit_project_and_project_preview_pages_now_reach_profile_and_logout():
    """Section 29/30: these two pages previously had ONLY a "Back to My
    Stories" link - no way to reach Dashboard, Ownership, Profile, Plans, or
    Logout at all. Creator UI consistency pass replaced the ad-hoc per-page
    fix with the shared _creator_nav.html partial (see
    test_creator_nav_partial_reaches_every_canonical_destination for its
    content) - this pins that both pages actually include it."""
    for html, label in [(_edit_html(), "edit_project.html"), (_preview_html(), "project_preview.html")]:
        assert '{% include "user/_creator_nav.html" %}' in html, label


def test_ownership_page_now_reaches_plans_and_logout():
    html = _ownership_html()
    assert '{% include "user/_creator_nav.html" %}' in html


def test_profile_page_now_reaches_my_stories():
    html = _profile_html()
    assert '{% include "user/_creator_nav.html" %}' in html


def test_subscribe_page_now_reaches_my_stories_and_ownership():
    """Authenticated visitors get the shared Creator nav partial (My Stories/
    Ownership included); anonymous visitors get the separate public marketing
    nav, which correctly has neither link - there is no account for them to see
    "My Stories" in. See test_creator_nav_partial_reaches_every_canonical_
    destination for the shared partial's own content check."""
    html = _subscribe_html()
    assert "{% if user %}" in html
    include_pos = html.index('{% include "user/_creator_nav.html" %}')
    if_pos = html.rindex("{% if user %}", 0, include_pos)
    assert if_pos < include_pos < html.index("{% else %}", if_pos)


# ===========================================================================
# Issue 1: Creator intro/review UX — collapsible content-requirements box,
# consistent icon alignment.
# ===========================================================================

def test_content_requirements_box_is_now_a_collapsed_disclosure():
    html = _creator_html()
    assert '<details class="ss-content-reqs ss-disclosure">' in html
    assert "<summary>What makes good content</summary>" in html
    # All three original bullets survive verbatim — content was never removed,
    # only made collapsible.
    for text in ("Photo:", "Video:", "After you create:"):
        assert text in html
    # No `open` attribute — collapsed by default, matching every sibling
    # disclosure box on this page (.ss-explainer, "Learn about playback modes").
    assert '<details class="ss-content-reqs ss-disclosure" open' not in html


def test_content_requirements_icon_alignment_uses_fixed_flex_box():
    html = _creator_html()
    idx = html.index(".ss-content-reqs-list i {")
    block = html[idx:idx + 300]
    assert "display: inline-flex" in block
    assert "width: 18px" in block and "height: 18px" in block


# ===========================================================================
# Issue 2 (release blocker): Creator "Next" skips required-media validation.
# ===========================================================================

def test_wizard_step_validation_gates_step_2_on_media_completeness():
    html = _creator_html()
    idx = html.index("function validateWizardStep(step) {")
    block = html[idx:idx + 1200]
    assert "if (step === 2) {" in block
    assert "Object.values(currentFiles).some(pairIsComplete)" in block
    assert "showContentStepError" in block


def test_wizard_content_step_error_element_exists_for_focus_and_scroll():
    html = _creator_html()
    assert 'id="contentStepError"' in html
    assert "function showContentStepError()" in html
    assert "scrollIntoView" in html[html.index("function showContentStepError()"):html.index("function showContentStepError()") + 600]


def test_wizard_step_2_validation_reuses_the_same_completeness_rule_as_submission():
    """Must not diverge into a stricter/looser rule than what the Create button
    and the server already enforce — reuse pairIsComplete(), not new logic."""
    html = _creator_html()
    assert html.count("function pairIsComplete(pairData)") == 1
    # Both the Create-button gate and the new step-2 gate call the SAME function.
    assert html.count("pairIsComplete)") + html.count("pairIsComplete(") >= 3


# ===========================================================================
# Issue 3 (release blocker): resumable upload hangs forever on a dead
# connection — the stall watch only labeled the problem, never acted on it.
# ===========================================================================

def test_fetch_upload_json_supports_a_stall_timeout_that_aborts_and_retries():
    html = _creator_html()
    idx = html.index("async function fetchUploadJson(url, options = {}) {")
    block = html[idx:idx + 2200]
    assert "stallTimeoutMs" in block
    assert "AbortController()" in block
    assert "UPLOAD_CHUNK_STALLED" in block
    # A stall must never surface as a bare AbortError — the caller already
    # treats err.name === 'AbortError' as a user Cancel (SESSION_CANCELLED).
    assert "new UploadApiError('UPLOAD_CHUNK_STALLED'" in block


def test_upload_resumable_chunk_wires_the_stall_timeout_using_the_existing_threshold():
    html = _creator_html()
    idx = html.index("async function uploadResumableChunk(sessionId, offset, chunk, signal) {")
    block = html[idx:idx + 500]
    assert "stallTimeoutMs: UPLOAD_STALL_MS" in block


def test_stalled_chunk_is_classified_as_a_retryable_transport_failure():
    """UPLOAD_CHUNK_STALLED must fall into the SAME retry/backoff/pause
    machinery as any other dead-connection error, not a new/different path -
    it must not be in UPLOAD_TERMINAL_CODES."""
    html = _creator_html()
    terminal_start = html.index("const UPLOAD_TERMINAL_CODES = new Set([")
    terminal_block = html[terminal_start:html.index("]);", terminal_start)]
    assert "UPLOAD_CHUNK_STALLED" not in terminal_block


# ===========================================================================
# Issue 5: replace target image + video together — stale-media/cache-busting.
# ===========================================================================

def test_detect_init_matched_video_url_is_cache_busted_by_pair_updated_at():
    source = _read("app.py")
    idx = source.index("_video_cache_bust = int(matched_pair.updated_at.timestamp())")
    block = source[idx:idx + 900]
    assert 'url_for("serve_video", project_id=project_id, image_id=best_match_id, v=_video_cache_bust' in block
    assert 'url_for("serve_admin_video", project_id=project_id, image_id=best_match_id, v=_video_cache_bust' in block


def test_pair_media_payload_video_urls_are_cache_busted():
    source = _read("app.py")
    idx = source.index("def _pair_media_payload(pair, media_endpoint, external=False):")
    block = source[idx:idx + 1400]
    assert "v=media.video_size or 0" in block


def test_detect_init_response_includes_cache_busted_video_url(client, app_module, login_user, feature_artifact, project_with_pair, blank_wall_image_bytes):
    """End-to-end: even a no-match response doesn't exercise matched_video_url,
    so this only proves the route still returns 200 with the fix in place -
    real url content is covered by the source-level tests above (the exact
    query value depends on a real accepted match, exercised in gate_jr's
    scanner robustness/recovery packs)."""
    from io import BytesIO
    project, _pair = project_with_pair
    response = client.post(
        "/detect_init",
        data={"project_id": str(project.id), "test_image": (BytesIO(blank_wall_image_bytes), "frame.jpg")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200


# ===========================================================================
# Issue 6: edit page must show CURRENT media before replacing it.
# ===========================================================================

def test_edit_page_shows_current_target_image_preview():
    html = _edit_html()
    assert 'class="current-target-preview"' in html
    idx = html.index('class="current-target-preview"')
    block = html[max(0, idx - 400):idx + 200]
    assert "serve_image" in block or "serve_admin_image" in block
    assert "pair.updated_at" in block  # cache-busted, matching issue 5's fix


def test_edit_page_video_previews_have_no_autoplay():
    html = _edit_html()
    assert "media-card-video" in html
    idx = html.index('class="media-card-preview">')
    block = html[idx:idx + 400]
    assert "autoplay" not in block
    assert 'preload="metadata"' in block


def test_edit_page_renders_current_target_preview_for_a_real_project(client, app_module, login_user, project_with_pair):
    project, _pair = project_with_pair
    response = client.get(f"/projects/{project.id}/edit")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "current-target-preview" in html


# ===========================================================================
# Issue 9: 2 different targets sharing identical video CONTENT must never
# collide on filename/pair_index/idempotency — source-level invariant checks
# (live end-to-end reproduction is in a separate QA pass; see the final report).
# ===========================================================================

def test_pair_and_video_filenames_are_index_based_not_content_based():
    """The exact class of bug this issue warns against: a filename/grouping
    scheme keyed by video CONTENT (hash/size) instead of pair index would
    silently collide two different targets that happen to share video bytes."""
    source = _read("app.py")
    idx = source.index('img_filename = f"{project.id}_{index}.jpg" if experience_type == "image_video" else None')
    block = source[idx:idx + 2500]
    assert 'vid_filename = f"{project.id}_{index}{item[\'video_ext\']}"' in block


def test_project_delete_detaches_processing_job_fks_before_cascade(app_module, db_session, project_with_pair):
    """Incidental finding from the live-Postgres repro pass for this issue:
    ProcessingJob.project_id/pair_id/pair_media_id have no ondelete clause,
    so deleting a project with ANY processing history hits a real FK
    violation on Postgres (SQLite doesn't enforce it by default, which is
    why this was never caught before). Fixed in _delete_project_files_and_rows
    by detaching (never deleting) job history, same philosophy as the
    UploadSession detach right above it. This SQLite-backed test documents
    the intended detach behavior; the actual FK-violation reproduction and
    fix verification happened live against real Postgres (see final report)."""
    import processing_queue

    project, pair = project_with_pair
    media = app_module.PairMedia(
        pair=pair, video_filename=pair.video_filename, original_video_name="orig.mp4",
        video_size=100, sort_order=0, is_default=True,
    )
    db_session.add(media)
    db_session.commit()

    job = app_module.ProcessingJob(
        public_key=processing_queue.generate_unique_public_key(db_session, app_module.ProcessingJob, "job"),
        project_id=project.id, pair_id=pair.id, pair_media_id=media.id,
        job_type="optimize_pair_media", status="completed",
        idempotency_key=f"optimize_pair_media:pair_media:{media.id}:initial",
    )
    db_session.add(job)
    db_session.commit()
    job_id = job.id

    app_module._delete_project_files_and_rows(project)

    refreshed_job = app_module.ProcessingJob.query.get(job_id)
    assert refreshed_job is not None, "job history must be preserved, not deleted"
    assert refreshed_job.project_id is None
    assert refreshed_job.pair_id is None
    assert refreshed_job.pair_media_id is None
    assert app_module.Project.query.get(project.id) is None


def test_content_set_ids_reject_duplicates_rather_than_silently_collapsing():
    source = _read("app.py")
    idx = source.index("def _parse_content_set_ids(payload):")
    block = source[idx:idx + 1600]
    assert "DUPLICATE_SESSION_IDS" in block
    assert "len(set(ids)) != len(ids)" in block


# ===========================================================================
# Issue 10: Back navigation must go to a logical parent, not always Dashboard.
# ===========================================================================

def test_project_preview_back_link_goes_to_projects_not_dashboard():
    html = _preview_html()
    idx = html.index("ss-btn ss-btn-tertiary ss-btn-sm inline-flex items-center gap-2")
    block = html[max(0, idx - 200):idx + 50]
    assert "url_for('projects_page')" in block
    assert "url_for('dashboard')" not in block


def test_creator_wizard_back_to_your_stories_label_matches_its_destination():
    html = _creator_html()
    idx = html.index('<i class="fas fa-chevron-left"></i> Back to Your Stories')
    block = html[max(0, idx - 300):idx]
    assert "url_for('projects_page')" in block


def test_edit_project_back_link_still_goes_to_projects_list():
    """Regression guard: this one was already correct before this pass —
    must not regress while fixing the two that were wrong."""
    html = _edit_html()
    assert "url_for('projects_page')" in html


# ===========================================================================
# Issue 11: Ownership page — targeted stabilization only.
# ===========================================================================

def test_ownership_nav_uses_shared_button_styling_and_correct_touch_target():
    """Creator UI consistency pass: ownership.html's own bespoke .top-links
    styling (a deliberately minimal Wave-4-era patch, per its own removed
    comment) was replaced by the shared _creator_nav.html partial - the
    ss-btn-styled Logout link and the 44px touch target both live there now,
    not in a page-local rule."""
    nav_html = _creator_nav_html()
    assert 'class="ss-btn ss-btn-tertiary ss-btn-sm"' in nav_html
    css = _read("static/css/design-system.css")
    idx = css.index(".ss-mobile-menu-panel .ss-nav-link {")
    block = css[idx:idx + 200]
    assert "min-height: 44px" in block


def test_ownership_page_renders_for_a_real_user(client, app_module, login_user):
    response = client.get("/ownership")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "My Stories" in html


# ===========================================================================
# Issue 12: Project View text density — secondary explanations collapsed.
# ===========================================================================

def test_renewal_explanation_is_collapsed_by_default():
    html = _preview_html()
    assert '<details class="ss-disclosure"' in html
    assert "<summary>How renewal works</summary>" in html


def test_disclosure_summary_styling_is_shared_not_duplicated():
    css = _read("static/css/design-system.css")
    assert ".ss-disclosure > summary {" in css
    assert "min-height: 44px" in css[css.index(".ss-disclosure > summary {"):css.index(".ss-disclosure > summary {") + 300]


# ===========================================================================
# Issue 13 (release blocker): scanner guidance not visible without scrolling.
# ===========================================================================

def test_scanner_guidance_is_now_a_child_of_wrap_not_a_sibling():
    html = _scanner_html()
    wrap_start = html.index('<div class="wrap" id="wrap">')
    # Find .wrap's matching close by locating the guidance element and the
    # next sibling-level close - simplest robust check: guidance must appear
    # BEFORE the block comment marking the recognitionHelpPanel (still inside
    # .wrap) and AFTER #overlayWrap opens (both firmly inside .wrap).
    overlay_wrap_idx = html.index('<div id="overlayWrap">', wrap_start)
    guidance_idx = html.index('id="scannerGuidance"', wrap_start)
    recognition_help_idx = html.index('id="recognitionHelpPanel"', wrap_start)
    assert overlay_wrap_idx < guidance_idx < recognition_help_idx


def test_scanner_guidance_is_absolutely_positioned_and_readable_over_video():
    html = _scanner_html()
    idx = html.index("#scannerGuidance {")
    block = html[idx:idx + 1200]
    assert "position: absolute" in block
    assert "z-index: 45" in block
    assert "background: rgba(5, 5, 8, 0.82)" in block  # solid contrast, no blur (see below)
    # Deliberately NOT blurred - unlike .ss-lens-panel's rare recovery scrim,
    # this sits over the LIVE camera feed continuously during normal scanning.
    # A permanent blur layer there would violate the existing
    # test_no_large_blurred_moving_layers_remain_over_the_camera_feed invariant
    # (test_scanner_presentation.py) and cost mobile GPU for no real benefit.
    assert "backdrop-filter" not in block
    assert "pointer-events: none" in block  # never blocks camera tap-to-focus


def test_scanner_guidance_top_anchored_respects_safe_area():
    html = _scanner_html()
    idx = html.index("#scannerGuidance {")
    block = html[idx:idx + 500]
    assert "env(safe-area-inset-top" in block


def test_status_element_unchanged_scanner_guidance_only_moved():
    """#status stays exactly where it was - only #scannerGuidance moved
    in-lens. Regression guard against accidentally moving/removing #status."""
    html = _scanner_html()
    assert '<div id="status" class="status scan">Getting ready…</div>' in html


@pytest.mark.skipif(not NODE, reason="node not available in this environment")
def test_scanner_inline_js_still_parses_after_guidance_reposition(client, project_with_pair):
    project, _pair = project_with_pair
    response = client.get(f"/scanner/{project.id}", follow_redirects=True)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    assert scripts
    combined = "\n;\n".join(s for s in scripts if s.strip())
    result = subprocess.run(
        [NODE, "--check"], input=combined, capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr


def test_scanner_page_renders_scannerguidance_exactly_once(client, project_with_pair):
    project, _pair = project_with_pair
    response = client.get(f"/scanner/{project.id}", follow_redirects=True)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert html.count('id="scannerGuidance"') == 1


# ===========================================================================
# AGENT 2 — unresolved-defect remediation pass
# ===========================================================================

# --- Duplicate Replace UI consolidation (section 6) ---------------------------------------

def test_classic_video_replace_slot_only_shows_for_a_truly_legacy_pair():
    """Root cause of the reported "two Replace systems": the classic slot's
    old `or pair.media_items|length <= 1` condition also matched a pair with
    exactly ONE real PairMedia row - which the "Videos (N)" manager below
    already renders a real, working Replace button for. Now the classic slot
    is the fallback ONLY for a pair with zero PairMedia rows at all."""
    html = _edit_html()
    idx = html.index('{% if not pair.media_items %}')
    assert idx > 0
    # The old, broader (bug-causing) condition must be gone.
    assert '{% if not pair.media_items or pair.media_items|length <= 1 %}' not in html


def test_media_manager_still_documents_the_legacy_fallback_hint():
    html = _edit_html()
    assert "Use &ldquo;Replace video&rdquo; above to update this one." in html


def test_edit_page_single_video_pair_shows_exactly_one_video_replace_control(client, app_module, login_user, project_with_pair):
    """The actual regression proof: a pair already backfilled with one real
    PairMedia row (via _ensure_default_pair_media, e.g. after any prior
    add/replace/remove action) must show only ONE way to replace its video,
    not two competing forms hitting two different routes."""
    project, pair = project_with_pair
    app_module._ensure_default_pair_media(pair)
    app_module.db.session.commit()

    response = client.get(f"/projects/{project.id}/edit")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert f'id="video_{pair.pair_index}"' not in html
    assert f'aria-label="Replace video 1"' in html


def test_edit_page_fully_legacy_pair_keeps_the_classic_slot_as_fallback(client, app_module, login_user, project_with_pair):
    """A pair with ZERO PairMedia rows has nothing for the manager's per-media
    loop to render a control against yet - the classic slot must still work."""
    project, pair = project_with_pair
    assert not pair.media_items  # project_with_pair never calls _ensure_default_pair_media
    response = client.get(f"/projects/{project.id}/edit")
    html = response.get_data(as_text=True)
    assert f'id="video_{pair.pair_index}"' in html


# --- Exact-duplicate-target guard (section 9) ----------------------------------------------

def test_find_duplicate_target_image_detects_identical_bytes(app_module, tmp_path):
    a = tmp_path / "a.jpg"
    b = tmp_path / "b.jpg"
    c = tmp_path / "c.jpg"
    a.write_bytes(b"same content")
    b.write_bytes(b"same content")
    c.write_bytes(b"different content")
    assert app_module._find_duplicate_target_image([("0", str(a)), ("1", str(b))]) == ("0", "1")
    assert app_module._find_duplicate_target_image([("0", str(a)), ("1", str(c))]) is None


def test_duplicate_target_guard_is_wired_into_finalize_before_temp_files_are_consumed():
    """Must run BEFORE the assembled-temp-file cleanup, using
    revert_all_to_active (sessions stay genuinely resumable) - never
    fail_group (which implies bytes were already irreversibly consumed)."""
    source = _read("app.py")
    # Canonical cross-device identity pass (2026-09-01): the old exact-hash-
    # only guard was replaced by canonical_target_identity_check() (same
    # revert_all_to_active/DUPLICATE_TARGET_IMAGE contract, now backed by the
    # same two-layer exact+ORB/homography algorithm Edit's Add/Replace Target
    # already use, plus in-batch sibling comparison) - see
    # test_v11_canonical_target_identity_fix.py for the functional proof.
    guard_idx = source.index("Canonical target-identity guard (canonical cross-device")
    consumed_idx = source.index("the assembled temp files are no\n    # longer needed.")
    assert guard_idx < consumed_idx
    guard_block = source[guard_idx:consumed_idx]
    assert "canonical_target_identity_check(" in guard_block
    assert "revert_all_to_active(" in guard_block
    assert '"DUPLICATE_TARGET_IMAGE"' in guard_block
    assert "This target is already part of this story. Add the new video to the existing target instead." in guard_block


def test_duplicate_target_error_code_reaches_the_client_message_map():
    html = _creator_html()
    assert "DUPLICATE_TARGET_IMAGE: 'This target is already part of this story." in html
    assert "'DUPLICATE_TARGET_IMAGE'" in html[html.index("UPLOAD_TERMINAL_CODES"):html.index("UPLOAD_TERMINAL_CODES") + 600]


# --- Exact-duplicate-video-within-target guard (final physical QA stabilization pass) ------

def test_duplicate_video_guard_is_wired_into_finalize_before_temp_files_are_consumed():
    """Scoped WITHIN one target's own video set (primary + extras), never
    across different targets - must run before the assembled-temp-file
    cleanup, using revert_all_to_active."""
    source = _read("app.py")
    guard_idx = source.index("Exact-duplicate-video guard (physical QA fix)")
    consumed_idx = source.index("the assembled temp files are no\n    # longer needed.")
    assert guard_idx < consumed_idx
    guard_block = source[guard_idx:consumed_idx]
    assert "revert_all_to_active(" in guard_block
    assert '"DUPLICATE_TARGET_VIDEO"' in guard_block
    assert "This video is already added to this target." in guard_block


def test_duplicate_video_error_code_reaches_the_client_message_map():
    html = _creator_html()
    assert "DUPLICATE_TARGET_VIDEO: 'This video is already added to this target.'" in html
    assert "'DUPLICATE_TARGET_VIDEO'" in html[html.index("UPLOAD_TERMINAL_CODES"):html.index("UPLOAD_TERMINAL_CODES") + 700]


def test_add_pair_media_route_blocks_exact_duplicate_video_content(app_module, tmp_path):
    """user_add_pair_media (adding a video to an ALREADY-CREATED target) has
    its own guard, separate from the creation-time one - it must hash the
    new upload against every existing PairMedia file on disk for that pair
    before touching storage/quota."""
    source = _read("app.py")
    idx = source.index("def user_add_pair_media")
    next_idx = source.index("\ndef ", idx + 10)
    route_body = source[idx:next_idx]
    # Pre-existing stale assertion (unrelated to this pass): the comment was
    # reworded to "...fix, centralized in the video duplicate/Direct QR
    # parity pass): ..." during an earlier session pass; this route's own
    # flash text is also the "This video is already part of this target"
    # phrasing (test_b_add_different_video_to_same_pair_is_allowed-adjacent),
    # not "already added" - fixed to match the real, current source.
    assert "Exact-duplicate-video guard (physical QA fix, centralized" in route_body
    assert "_sha256_of_file(temp_path)" in route_body
    assert "This video is already part of this target." in route_body
    # Must run before the storage/quota check so a rejected duplicate never
    # consumes account storage allowance.
    assert route_body.index("Exact-duplicate-video guard") < route_body.index("can_consume(")


# --- Mixed video aspect-ratio robustness (section 11) --------------------------------------

def test_overlay_video_object_fit_is_deliberate_not_accidental():
    """object-fit: fill on #overlay is documented, intentional AR-mapping
    behavior (the video is stretched to fill the camera-shaped overlayWrap
    BEFORE the homography transform warps the whole box onto the target's
    quad - #overlayWrap's own size tracks the camera feed, never any
    individual video's own dimensions, so no per-video aspect-ratio
    dependency exists in this path). Must stay untouched per the brief's
    "unless existing product design deliberately specifies otherwise"."""
    html = _scanner_html()
    idx = html.index("#overlay {")
    block = html[idx:idx + 300]
    assert "object-fit: fill;" in block


def test_sequence_advance_and_marker_switch_both_call_load_after_src_change():
    """Robustness fix: setting .src alone does not guarantee a stale decoded
    frame/dimension from the PREVIOUS video is flushed before the next one
    paints on WebKit. load() is presentation-only - it does not touch
    #overlayWrap's transform/homography geometry."""
    html = _scanner_html()
    seq_idx = html.index("function playSequenceMediaAtIndex(index) {")
    seq_block = html[seq_idx:seq_idx + 1400]
    assert "overlay.src = media.video_url;" in seq_block
    assert "overlay.load();" in seq_block

    switch_idx = html.index("startSequenceForTarget(newMedia);")
    switch_block = html[switch_idx:switch_idx + 200]
    assert "overlay.src = newVideoUrl;" in switch_block
    assert "overlay.load();" in switch_block


def test_overlay_has_exactly_one_ended_listener_registered_once():
    """A listener re-attached per video-load would accumulate and fire the
    sequence-advance handler multiple times per real 'ended' event - the
    exact 'duplicate ended events' failure mode section 8 warns against."""
    html = _scanner_html()
    assert html.count('overlay.addEventListener("ended"') == 1


# --- Multi-pair + multi-video isolation (sections 7-8, live-repro-backed) ------------------

def test_pair_media_relationship_is_scoped_by_pair_id_never_flattened_across_pairs():
    """Structural guard matching the live-Postgres proof (see final report):
    _pair_media_payload only ever iterates ONE pair's own media_items -
    there is no code path that merges media across pairs."""
    source = _read("app.py")
    idx = source.index("def _pair_media_payload(pair, media_endpoint, external=False):")
    block = source[idx:idx + 1500]
    assert "for media in pair.media_items" in block
    assert "ProjectPair.query" not in block  # never re-queries other pairs
