"""Physical-evidence-driven remediation pass (2026-08-27): Direct QR multi-video
playlist and Detect Once session completion/reset.

Both defects were confirmed live via physical device testing and root-caused via
code trace before any fix was written (see V1_1_MASTER_CONSOLIDATED_STABILIZATION_
HANDOFF.md, "PLAYBACK LIFECYCLE REMEDIATION" section). These tests assert the
structural fix is present and the two mechanisms that caused each defect
(targets[0]-only rendering; a one-way detectOnceLocked flag with no unlock path)
cannot silently regress. Full interactive verification (real playlist playthrough,
real lock->complete->re-lock lifecycle) was done via live Playwright/Chromium this
pass - see the handoff doc for that evidence; these tests are the codebase's
standing regression guard for it.
"""
from pathlib import Path


def scanner_html():
    return Path("templates/user/scanner.html").read_text(encoding="utf-8", errors="ignore")


def app_py():
    return Path("app.py").read_text(encoding="utf-8", errors="ignore")


# ===========================================================================
# Direct QR playlist
# ===========================================================================

def test_direct_qr_server_builds_flattened_playlist_from_all_targets():
    """Root cause was never the server: _pair_media_payload/targets already carried
    every video. The fix adds one new context var, direct_qr_playlist, flattening
    every target (and each target's own multi-PairMedia list, if present) in
    pair_index order - the same canonical ProjectPair -> ordered PairMedia source
    every other mode reads, not a second playlist model."""
    src = app_py()
    assert "direct_qr_playlist = []" in src
    assert 'if experience_type == "direct_qr":' in src
    start = src.index("direct_qr_playlist = []")
    block = src[start:src.index("return render_template(", start)]
    assert "for target in targets:" in block
    assert 'media_list = target.get("media")' in block
    assert 'direct_qr_playlist.append({"url": media_item["video_url"]' in block
    assert 'direct_qr_playlist.append({"url": target["video_url"]' in block
    assert "direct_qr_playlist=direct_qr_playlist" in src


def test_direct_qr_template_no_longer_hardcodes_first_target_only():
    """The actual bug: targets[0].video_url was the ONLY thing ever rendered,
    regardless of how many videos the project had. Must not regress to this."""
    html = scanner_html()
    assert 'src="{{ targets[0].video_url }}"' not in html
    assert 'id="directQrPlaylistData"' in html
    assert "{{ direct_qr_playlist | tojson }}" in html


def test_direct_qr_video_element_has_no_loop_attribute():
    """A looping single video and a playlist that never advances are two different
    bugs with the same symptom (video 2 unreachable) - confirm the fix didn't
    introduce the loop-based version of the same defect."""
    html = scanner_html()
    start = html.index('<video id="directQrVideo"')
    tag = html[start:html.index(">", start)]
    assert " loop" not in tag
    assert "loop=" not in tag


def test_direct_qr_client_has_real_playlist_state_and_ended_handler():
    html = scanner_html()
    assert "let directQrPlaylist = []" in html
    assert "let directQrIndex = 0" in html
    assert "function playDirectQrAtIndex(index)" in html
    assert "player.addEventListener('ended'" in html


def test_direct_qr_advances_to_next_index_before_reaching_the_end():
    html = scanner_html()
    start = html.index("function wireDirectQrEnded")
    block = html[start:html.index("})();", start)]
    assert "if (directQrIndex < directQrPlaylist.length - 1)" in block
    assert "playDirectQrAtIndex(directQrIndex + 1)" in block


def test_direct_qr_stops_and_shows_replay_after_last_video_not_loop():
    html = scanner_html()
    start = html.index("function wireDirectQrEnded")
    block = html[start:html.index("})();", start)]
    assert "player.classList.add('is-hidden')" in block
    assert "directQrCompletion" in block
    assert "playDirectQrAtIndex(0)" not in block  # must not auto-restart from the ended handler itself


def test_direct_qr_replay_all_restarts_from_first_video():
    html = scanner_html()
    assert "directQrReplayBtn" in html
    start = html.index("directQrReplayBtn.addEventListener")
    end = html.index("});", start)
    block = html[start:end]
    assert "playDirectQrAtIndex(0)" in block


def test_direct_qr_falls_back_to_legacy_single_video_when_no_pairmedia():
    """A target with no PairMedia rows (legacy single-video mirror only) still
    contributes exactly one playlist entry, from target["video_url"]."""
    src = app_py()
    start = src.index("direct_qr_playlist = []")
    block = src[start:src.index("return render_template(", start)]
    assert "else:" in block
    assert 'direct_qr_playlist.append({"url": target["video_url"], "label": target["label"]})' in block


# ===========================================================================
# Detect Once completion/reset
# ===========================================================================

def test_detect_once_lock_now_has_a_real_unlock_function():
    """Root cause: detectOnceLocked went false->true exactly once per page load and
    NOTHING ever set it back to false - a permanent one-way lock. This is the
    single most important assertion in this file: without it, every other Detect
    Once fix is meaningless (nothing could ever call them again)."""
    html = scanner_html()
    assert "function completeDetectOnceSession(reason)" in html
    start = html.index("function completeDetectOnceSession(reason)")
    block = html[start:html.index("\n    function stopOverlayImmediate()", start)]
    assert "detectOnceLocked = false" in block


def test_detect_once_completion_restarts_camera_and_recognition_loops():
    """Must reuse the existing recoverScanner machinery (bounded retry, FSM-safe
    transition, startDetectLoop/startTrackingLoop) rather than re-implement camera
    reacquisition - the same path visibility/orientation/stream-interruption
    recovery already uses."""
    html = scanner_html()
    start = html.index("function completeDetectOnceSession(reason)")
    block = html[start:html.index("\n    function stopOverlayImmediate()", start)]
    assert "recoverScanner(reason, true)" in block


def test_detect_once_completion_clears_playlist_and_pair_state():
    html = scanner_html()
    start = html.index("function completeDetectOnceSession(reason)")
    block = html[start:html.index("\n    function stopOverlayImmediate()", start)]
    assert "resetSequencePlaybackState()" in block
    assert "currentPairId = -1" in block


def test_detect_once_completion_bumps_generation_against_stale_late_responses():
    html = scanner_html()
    start = html.index("function completeDetectOnceSession(reason)")
    block = html[start:html.index("\n    function stopOverlayImmediate()", start)]
    assert "scannerGeneration++" in block


def test_detect_once_forces_loop_off_so_a_single_video_target_can_reach_ended():
    """The other half of the root cause: overlay.loop stayed true for a
    single-video target (the normal tracked_overlay behavior), so 'ended' never
    fired at all in Detect Once - the video played forever regardless of whether
    a completion handler existed. Detect Once must force loop off unconditionally,
    not just for 2+-video targets."""
    html = scanner_html()
    start = html.index("function startSequenceForTarget(media)")
    block = html[start:html.index("\n    }", start)]
    assert "overlay.loop = scannerPlaybackMode === 'detect_once' ? false : !isMultiVideoTarget();" in block


def test_detect_once_ended_handler_completes_session_on_last_or_only_video():
    html = scanner_html()
    start = html.index('overlay.addEventListener("ended"')
    block = html[start:html.index("// Audit note (issue 3", start)]
    assert "scannerPlaybackMode === 'detect_once' && detectOnceLocked" in block
    assert "completeDetectOnceSession('playlist_ended')" in block


def test_detect_once_ended_handler_advances_multi_video_playlist_before_completing():
    html = scanner_html()
    start = html.index('overlay.addEventListener("ended"')
    block = html[start:html.index("// Audit note (issue 3", start)]
    # The if-branch (more items remain) advances; only the else-branch (no next item)
    # completes the session - so completion never fires while a next item exists.
    if_start = block.index("if (availableMedia && availableMedia.length > 1")
    if_else_split = block.index("} else {", if_start)
    if_branch = block[if_start:if_else_split]
    else_branch = block[if_else_split:]
    assert "playSequenceMediaAtIndex(currentMediaIndex + 1)" in if_branch
    assert "completeDetectOnceSession('playlist_ended')" in else_branch


def test_detect_once_pose_hold_and_stop_overlay_still_survive_target_loss():
    """Regression guard: the fix must not remove the existing camera-away survival
    behavior (stopOverlayImmediate/requestPoseHold neutralised while locked) -
    only add the missing unlock path on natural completion."""
    html = scanner_html()
    assert "if (detectOnceLocked) return;" in html
    start = html.index("function stopOverlayImmediate()")
    assert "if (detectOnceLocked) return;" in html[start:start + 200]
    start2 = html.index("function requestPoseHold(reason)")
    assert "if (detectOnceLocked) return;" in html[start2:start2 + 200]


def test_detect_once_recover_scanner_still_guards_against_mid_lock_recovery():
    """Regression guard: recoverScanner's existing detectOnceLocked guard (skip
    camera recovery while genuinely still locked) must remain - completion clears
    the flag itself before calling recoverScanner, it does not remove the guard."""
    html = scanner_html()
    assert "recover_skipped_detect_once_locked" in html
    start = html.index("function recoverScanner(reason, restartCamera)")
    block = html[start:html.index("\n    }", start)]
    assert "if (detectOnceLocked) {" in block
