"""Creator Identity / Edit Flow / Direct QR remediation pass (2026-08-29),
Phase 6 - Direct QR visual polish only.

Functionality is locked (confirmed passing via real browser testing per the
brief) - these tests assert only the requested visual/copy changes, and that
the underlying playback engine (directQrPlaylist/directQrIndex/
playDirectQrAtIndex/wireDirectQrEnded) is untouched.
"""
from pathlib import Path


def scanner_html():
    return Path("templates/user/scanner.html").read_text(encoding="utf-8", errors="ignore")


# ===========================================================================
# START screen
# ===========================================================================

def test_start_screen_has_start_story_cta():
    # Pre-play landing redesign (human QA blocker pass, 2026-09-03) removed
    # the "Ready when you are." reassurance line - the brief's own audit
    # named it as redundant filler weakening the composition. The element
    # (#directQrReadyNote) is gone from the markup; the JS below still
    # null-guards a lookup for it (harmless, not worth touching further).
    html = scanner_html()
    assert "Ready when you are." not in html
    start = html.index('id="directQrPlayBtn"')
    block = html[max(0, start - 200):start + 250]
    assert "Start story" in block


def test_ready_note_and_play_button_hide_together_on_playback_start():
    html = scanner_html()
    start = html.index("function startDirectQrPlayback")
    block = html[start:html.index("\n    }", start)]
    assert "directQrPlayBtn.style.display = 'none'" in block
    assert "directQrReadyNote" in block


# ===========================================================================
# PLAYBACK NAV: one combined "‹ 1 2 3 ›" row
# ===========================================================================

def test_playback_nav_is_one_combined_row_not_separate_dots():
    html = scanner_html()
    assert 'id="directQrNavNumbers"' in html
    assert 'id="directQrPrevBtn"' in html
    assert 'id="directQrNextBtn"' in html
    # The old separate dot-indicator strip must be gone.
    assert "directQrIndicatorDots" not in html
    assert "sequence-dot" not in html[html.index("directQrIndicator"):html.index("directQrIndicator") + 3000]


def test_nav_numbers_rebuild_marks_active_video_and_disables_at_ends():
    html = scanner_html()
    start = html.index("function updateDirectQrIndicator")
    block = html[start:html.index("\n    }", start)]
    assert "is-active" in block
    assert "aria-current" in block
    assert "prevBtn.disabled = directQrIndex === 0" in block
    assert "nextBtn.disabled = directQrIndex === directQrPlaylist.length - 1" in block


def test_nav_number_click_calls_playdirectqratindex_via_delegation():
    html = scanner_html()
    start = html.index("directQrNavNumbers.addEventListener")
    block = html[start:html.index("});", start)]
    assert "playDirectQrAtIndex(parseInt(btn.dataset.dqrIndex, 10))" in block


# ===========================================================================
# COMPLETION screen
# ===========================================================================

def test_completion_screen_has_checkmark_and_replay_story_copy():
    html = scanner_html()
    assert 'class="dqr-complete-check"' in html
    assert "You watched all" in html
    assert "Replay story" in html


def test_watch_again_list_is_inline_not_behind_a_toggle():
    """Confirmed brief requirement: the per-video list must render the
    moment completion shows, not require an extra 'Choose a video' tap."""
    html = scanner_html()
    assert "directQrChooserToggleBtn" not in html
    start = html.index("function renderDirectQrWatchAgainList")
    block = html[start:html.index("\n    }", start)]
    assert "directQrPlaylist" in block
    # Called from the natural-completion path (the 'ended' handler), not
    # gated behind a separate click.
    ended_start = html.index("function wireDirectQrEnded")
    ended_block = html[ended_start:html.index("})();", ended_start)]
    assert "renderDirectQrWatchAgainList()" in ended_block


# ===========================================================================
# Playback engine untouched
# ===========================================================================

def test_playback_engine_selection_logic_is_unchanged():
    html = scanner_html()
    assert "let directQrPlaylist = []" in html
    assert "let directQrIndex = 0" in html
    start = html.index("function playDirectQrAtIndex(index)")
    block = html[start:html.index("\n    }", start)]
    assert "player.src = directQrPlaylist[index].url" in block
    assert "player.load()" in block


def test_single_video_playlist_hides_all_nav_and_watch_again():
    html = scanner_html()
    # Nav and indicator are still gated behind direct_qr_playlist|length > 1.
    indicator_pos = html.index('id="directQrIndicator"')
    guard_pos = html.rindex("{% if direct_qr_playlist | length > 1 %}", 0, indicator_pos)
    endif_pos = html.index("{% endif %}", guard_pos)
    assert guard_pos < indicator_pos < endif_pos
    watch_again_pos = html.index('class="dqr-watch-again-label"')
    guard_pos2 = html.rindex("{% if direct_qr_playlist | length > 1 %}", 0, watch_again_pos)
    endif_pos2 = html.index("{% endif %}", guard_pos2)
    assert guard_pos2 < watch_again_pos < endif_pos2
