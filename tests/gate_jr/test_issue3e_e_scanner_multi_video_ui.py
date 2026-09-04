"""Issue 3E-E: scanner sequential multi-video playback (frontend).

DOM-free, source-level assertions against the rendered scanner.html, matching
the established convention in test_scanner_lifecycle.py: no headless browser
in CI, so the guard code's presence/placement is asserted directly. Real
playback behavior (autoplay, video-ended transitions, target loss/regain) is
verified separately via manual browser QA - these tests only pin the source
so a future change cannot silently delete or diverge one of the pieces below.
"""
from pathlib import Path


def creator_html():
    return Path("templates/user/scanner.html").read_text(encoding="utf-8", errors="ignore")


def _fn_body(html, start_marker, end_marker):
    return html[html.index(start_marker):html.index(end_marker)]


# ===========================================================================
# 7-9: single video is unchanged.
# ===========================================================================
def test_single_media_target_keeps_native_loop_and_autoplay():
    html = creator_html()
    overlay_tag = html[html.index('<video id="overlay"'):html.index(">", html.index('<video id="overlay"'))]
    assert "loop" in overlay_tag and "autoplay" in overlay_tag
    fn = _fn_body(html, "function startSequenceForTarget(media) {", "function resetSequencePlaybackState()")
    # Physical QA fix (playback lifecycle remediation): Detect Once forces loop off even for
    # a single-video target (so 'ended' can fire and the session can complete/reset) - a
    # plain tracked_overlay single-video target is unaffected, still !isMultiVideoTarget().
    assert "overlay.loop = scannerPlaybackMode === 'detect_once' ? false : !isMultiVideoTarget();" in fn


def test_single_media_target_never_shows_sequence_ui():
    html = creator_html()
    assert 'id="sequenceIndicator" class="sequence-indicator" aria-hidden="true" hidden' in html
    assert 'id="sequenceControls" class="sequence-controls" hidden' in html
    fn = _fn_body(html, "function isMultiVideoTarget() {", "function renderSequenceIndicator() {")
    assert "availableMedia.length > 1" in fn


def test_ended_handler_preserves_single_video_behavior_first():
    html = creator_html()
    ended_fn = _fn_body(html, 'overlay.addEventListener("ended", () => {', "let lastKnownOverlayTime")
    # The single-video early return happens BEFORE any sequence/manual branching -
    # a target with 0 or 1 media never reaches the new logic at all.
    assert "if (!availableMedia || availableMedia.length <= 1) return;" in ended_fn
    early_return_idx = ended_fn.index("if (!availableMedia || availableMedia.length <= 1) return;")
    assert "playbackMode === 'manual'" not in ended_fn[:early_return_idx]


# ===========================================================================
# 10-15: sequential playback.
# ===========================================================================
def test_start_sequence_resets_index_and_shows_ui_for_multi_media():
    html = creator_html()
    fn = _fn_body(html, "function startSequenceForTarget(media) {", "function resetSequencePlaybackState()")
    assert "currentMediaIndex = 0;" in fn
    assert "playbackMode = 'sequence';" in fn
    assert "showSequenceControls();" in fn
    assert "renderSequenceIndicator();" in fn


def test_ended_handler_advances_to_next_media_in_sequence_mode():
    html = creator_html()
    ended_fn = _fn_body(html, 'overlay.addEventListener("ended", () => {', "let lastKnownOverlayTime")
    assert "if (currentMediaIndex < availableMedia.length - 1) {" in ended_fn
    assert "playSequenceMediaAtIndex(currentMediaIndex + 1);" in ended_fn


def test_last_video_does_not_restart_video_one():
    html = creator_html()
    ended_fn = _fn_body(html, 'overlay.addEventListener("ended", () => {', "let lastKnownOverlayTime")
    else_branch = ended_fn[ended_fn.index("} else {", ended_fn.index("if (currentMediaIndex < availableMedia.length - 1) {")):]
    assert "playSequenceMediaAtIndex(0)" not in else_branch
    assert "overlay.pause();" in else_branch
    assert "showSequenceCompletion();" in else_branch


def test_completion_screen_markup_and_labels():
    html = creator_html()
    assert 'id="sequenceCompletion"' in html
    completion_block = _fn_body(html, 'id="sequenceCompletion"', '<div id="sequenceChooser"')
    assert "Experience complete" in completion_block
    assert 'aria-label="Replay all videos"' in completion_block
    assert 'aria-label="Choose a video"' in completion_block
    assert "Replay all" in completion_block
    assert "Choose a video" in completion_block


def test_replay_all_starts_at_video_one():
    html = creator_html()
    fn = _fn_body(html, "function replaySequenceFromStart() {", "function startSequenceForTarget(")
    assert "playSequenceMediaAtIndex(0);" in fn


def test_replay_all_resumes_full_sequence_mode_not_manual():
    html = creator_html()
    fn = _fn_body(html, "function replaySequenceFromStart() {", "function startSequenceForTarget(")
    assert "playbackMode = 'sequence';" in fn
    assert "hideSequenceCompletion();" in fn
    assert "hideSequenceChooser();" in fn


# ===========================================================================
# 16-22: Previous / Next / All Videos / chooser.
# ===========================================================================
def test_next_button_wired_to_advance_one_media():
    html = creator_html()
    assert "sequenceNextBtn.addEventListener('click', goToNextSequenceMedia);" in html
    fn = _fn_body(html, "function goToNextSequenceMedia() {", "function replaySequenceFromStart(")
    assert "currentMediaIndex >= availableMedia.length - 1) return;" in fn
    assert "playSequenceMediaAtIndex(currentMediaIndex + 1);" in fn


def test_previous_button_wired_to_go_back_one_media():
    html = creator_html()
    assert "sequencePrevBtn.addEventListener('click', goToPreviousSequenceMedia);" in html
    fn = _fn_body(html, "function goToPreviousSequenceMedia() {", "function goToNextSequenceMedia(")
    assert "currentMediaIndex <= 0) return;" in fn
    assert "playSequenceMediaAtIndex(currentMediaIndex - 1);" in fn


def test_all_videos_button_opens_chooser():
    html = creator_html()
    assert "sequenceAllVideosBtn.addEventListener('click', showSequenceChooser);" in html


def test_chooser_lists_one_button_per_media_with_safe_labels():
    html = creator_html()
    fn = _fn_body(html, "function showSequenceChooser() {", "function playSequenceMediaAtIndex(")
    assert "availableMedia" in fn and ".map((media, i) =>" in fn
    assert "videoLabelForIndex(i)" in fn
    label_fn = _fn_body(html, "function videoLabelForIndex(i) {", "function showSequenceChooser(")
    assert "`Video ${i + 1}`" in label_fn
    # Never exposes db ids or PairMedia terminology in the chooser label itself.
    assert "media.id" not in label_fn


def test_choosing_a_video_plays_it_and_sets_manual_mode():
    html = creator_html()
    fn = _fn_body(html, "function selectSequenceMediaManually(index) {", "function goToPreviousSequenceMedia(")
    assert "playbackMode = 'manual';" in fn
    assert "playSequenceMediaAtIndex(index);" in fn


def test_manually_selected_video_returns_to_chooser_on_end():
    html = creator_html()
    ended_fn = _fn_body(html, 'overlay.addEventListener("ended", () => {', "let lastKnownOverlayTime")
    assert "if (playbackMode === 'manual') {" in ended_fn
    manual_branch = ended_fn[ended_fn.index("if (playbackMode === 'manual') {"):]
    manual_branch = manual_branch[:manual_branch.index("}", manual_branch.index("return;"))]
    assert "showSequenceChooser();" in manual_branch


def test_sequence_selected_video_continues_automatically_not_to_chooser():
    html = creator_html()
    ended_fn = _fn_body(html, 'overlay.addEventListener("ended", () => {', "let lastKnownOverlayTime")
    # The manual-mode check comes first and returns; only a NON-manual ended
    # reaches the auto-advance branch below it.
    manual_idx = ended_fn.index("if (playbackMode === 'manual') {")
    advance_idx = ended_fn.index("if (currentMediaIndex < availableMedia.length - 1) {")
    assert manual_idx < advance_idx


# ===========================================================================
# 23-27: state / target-loss / target-switch / close.
# ===========================================================================
def test_temporary_loss_paths_never_touch_sequence_state():
    html = creator_html()
    for fn_start, fn_end in (
        ("function dropTracking(reason, extraMats", "function handleDetectionTimeout()"),
        ("function clearTrackingGeometry(reason, options = {})", "function stopTrackingLoop()"),
        ("function requestPoseHold(reason)", "function playOverlay()"),
    ):
        body = html[html.index(fn_start):html.index(fn_end)]
        assert "availableMedia =" not in body
        assert "currentMediaIndex =" not in body
        assert "hideSequenceCompletion(" not in body
        assert "hideSequenceChooser(" not in body
        assert "resetSequencePlaybackState(" not in body


def test_different_target_calls_start_sequence_for_target():
    html = creator_html()
    switch_block = _fn_body(html, "if (!wasSameTarget) {", "} else if (sequenceCompletionVisible")
    assert "startSequenceForTarget(newMedia);" in switch_block


def test_start_sequence_never_merges_with_prior_media():
    html = creator_html()
    fn = _fn_body(html, "function startSequenceForTarget(media) {", "function resetSequencePlaybackState()")
    assert "availableMedia = (Array.isArray(media) && media.length > 1) ? media.slice() : null;" in fn
    assert "availableMedia.concat(" not in html
    assert "availableMedia.push(" not in html


def test_explicit_close_resets_sequence_playback_state():
    html = creator_html()
    end_session_fn = _fn_body(html, "async function endScannerSession() {", "console.log('🔚 Ending scanner session:'")
    assert "resetSequencePlaybackState();" in end_session_fn
    reset_fn = _fn_body(html, "function resetSequencePlaybackState() {", "if (sequencePrevBtn)")
    assert "availableMedia = null;" in reset_fn
    assert "currentMediaIndex = 0;" in reset_fn
    assert "overlay.loop = true;" in reset_fn
    assert "hideSequenceCompletion();" in reset_fn
    assert "hideSequenceChooser();" in reset_fn


# ===========================================================================
# 28: Direct QR baseline spot-check (full regression lives in the existing
# scanner test suites - test_scanner_lifecycle.py, test_scanner_presentation.py,
# test_gate_jr_scanner_recovery.py, test_gate_j_certification.py, all green).
# ===========================================================================
def test_direct_qr_video_untouched_by_sequence_logic():
    html = creator_html()
    fn = _fn_body(html, "function startDirectQrPlayback()", "}")
    assert "availableMedia" not in fn
    assert "sequence" not in fn.lower()
    guard = "{% if experience_type != 'direct_qr' %}"
    assert html.rindex(guard, 0, html.index('id="sequenceIndicator"')) > 0


# ===========================================================================
# 33-35: mobile safety (structural proxy - real viewport verification is
# manual browser QA at 375/390/430).
# ===========================================================================
def test_sequence_controls_use_relative_flex_layout_not_fixed_widths():
    html = creator_html()
    controls_css = html[html.index(".sequence-controls {"):html.index(".sequence-ctrl-btn {")]
    assert "left: 8px;" in controls_css and "right: 8px;" in controls_css  # anchored, not fixed-width
    assert "display: flex;" in controls_css


def test_sequence_buttons_meet_44px_minimum_tap_target():
    html = creator_html()
    ctrl_btn_css = html[html.index(".sequence-ctrl-btn {"):html.index(".sequence-ctrl-btn-wide {")]
    assert "min-width: 44px;" in ctrl_btn_css
    assert "min-height: 44px;" in ctrl_btn_css
    chooser_btn_css = html[html.index(".sequence-chooser-list .sequence-chooser-btn {"):]
    assert "min-height: 44px;" in chooser_btn_css[:300]


def test_sequence_panels_stay_inside_the_lens_like_existing_recovery_panels():
    html = creator_html()
    assert 'id="sequenceCompletion" class="ss-lens-panel sequence-panel"' in html
    assert 'id="sequenceChooser" class="ss-lens-panel sequence-panel"' in html


# ===========================================================================
# 36: accessibility roles/labels.
# ===========================================================================
def test_accessible_labels_present():
    html = creator_html()
    for label in (
        "Previous video", "Next video", "Show all videos",
        "Replay all videos", "Choose a video",
    ):
        assert f'aria-label="{label}"' in html
    indicator_fn = _fn_body(html, "function renderSequenceIndicator() {", "function updateSequenceControlsState(")
    assert "`Video ${currentMediaIndex + 1} of ${availableMedia.length}`" in indicator_fn
    chooser_fn = _fn_body(html, "function showSequenceChooser() {", "function playSequenceMediaAtIndex(")
    assert 'aria-label="Play ${videoLabelForIndex(i)}"' in chooser_fn
