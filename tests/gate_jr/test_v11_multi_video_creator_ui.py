"""Issue 3E-D: Creator multi-video UI + resumable wiring.

Structural guards for the multi-video-per-target Creator UI added on top of
the Issue 3E-D0 resumable transport. DOM-free: string/AST-level checks on the
served template's HTML/JS source, mirroring
tests/gate_jr/test_v11_creator_studio_ui.py's approach, not a browser run.
Real browser QA is still required separately (console errors, actual upload,
mobile viewport rendering) - these tests only pin the source so a future
change cannot silently delete or diverge one of the pieces below.
"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

NODE = shutil.which("node")


def creator_html():
    return Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")


# ===========================================================================
# 1-3: feature-disabled account sees the pre-3E-D experience, byte for byte.
# ===========================================================================
def test_disabled_account_gets_no_add_video_markup_at_all():
    """When allow_multi_video_per_target is false, the Jinja ternary must
    keep the extra-videos block OUT of pairHTML entirely - not hidden with
    CSS, not rendered-then-removed. Grep the JS template literal for the
    guard, not just presence of the feature elsewhere in the file."""
    html = creator_html()
    assert "${ALLOW_MULTI_VIDEO_PER_TARGET ? `" in html
    assert 'class="pair-extra-videos"' in html
    # The ternary's false branch is a bare empty template, so a disabled
    # account's pairHTML never contains the extra-videos-list/add ids.
    guarded = html[html.index('class="pair-extra-videos"') - 400:html.index('class="pair-extra-videos"')]
    assert "${ALLOW_MULTI_VIDEO_PER_TARGET ? `" in guarded


def test_disabled_account_video_title_has_no_default_tag():
    html = creator_html()
    assert "Add your video${ALLOW_MULTI_VIDEO_PER_TARGET ? ' <span class=\"pair-video-default-tag\">" in html


def test_no_disabled_upsell_control_only_a_conditional_block():
    """The brief explicitly forbids showing a disabled Add-Video control to
    upsell a disabled account. There must be exactly one way the button
    renders (inside the entitlement ternary), never a second disabled-by-
    default copy of it outside that guard."""
    html = creator_html()
    assert html.count('id="extra-videos-add-${pairId}"') == 1
    block_start = html.index("${ALLOW_MULTI_VIDEO_PER_TARGET ? `")
    block_end = html.index("` : ``}", block_start)
    block = html[block_start:block_end]
    assert 'id="extra-videos-add-${pairId}"' in block


# ===========================================================================
# 4-5: feature-enabled account sees the Add-Video control and counter.
# ===========================================================================
def test_enabled_account_gets_add_video_button_and_usage_counter():
    html = creator_html()
    assert 'onclick="addAdditionalVideo(${pairId})"' in html
    assert 'id="extra-videos-usage-${pairId}"' in html
    assert "Add another video" in html


def test_initial_counter_text_reflects_one_of_max():
    html = creator_html()
    assert "1 of ${MAX_VIDEOS_PER_TARGET === Infinity ? '∞' : MAX_VIDEOS_PER_TARGET} video" in html


# ===========================================================================
# 6-7: add/remove behavior.
# ===========================================================================
def test_add_additional_video_pushes_state_and_row():
    html = creator_html()
    fn = html[html.index("function addAdditionalVideo("):html.index("function handleAdditionalVideoSelect(")]
    assert "state.additionalVideos.push(" in fn
    assert "extra-videos-list-${pairId}" in fn
    assert "list.insertAdjacentHTML('beforeend', rowHTML)" in fn


def test_remove_additional_video_targets_only_the_selected_entry():
    html = creator_html()
    fn = html[html.index("function removeAdditionalVideo("):html.index("function renumberAdditionalVideos(")]
    assert "state.additionalVideos.findIndex(v => v.id === videoId)" in fn
    assert "state.additionalVideos.splice(index, 1)" in fn


# ===========================================================================
# 8: primary/default video has no remove control.
# ===========================================================================
def test_primary_video_upload_area_has_no_remove_button():
    html = creator_html()
    video_block = html[html.index('id="video-area-${pairId}"'):html.index('id="video-area-${pairId}"') + 900]
    assert "pair-extra-video-remove" not in video_block
    assert "removeAdditionalVideo" not in video_block


# ===========================================================================
# 9: max disables further additions and shows the limit message.
# ===========================================================================
def test_usage_update_disables_add_and_reveals_limit_at_max():
    html = creator_html()
    fn = html[html.index("function updateAdditionalVideoUsage("):html.index("/* ==== end of additional-videos-per-target UI ==== */")]
    assert "atMax" in fn
    # aria-disabled, not the native `disabled` attribute - disabling a
    # focused control blurs it (see the keyboard-focus regression test
    # below), and addAdditionalVideo() already no-ops at max on its own.
    assert "addBtn.setAttribute('aria-disabled'" in fn
    assert "addBtn.disabled" not in fn
    assert "limitEl.hidden = !atMax" in fn


# ===========================================================================
# 10: target isolation - state is keyed per pairId and removePair only
# renumbers, never merges, another pair's additionalVideos.
# ===========================================================================
def test_additional_videos_state_is_per_pair_not_shared():
    html = creator_html()
    assert "additionalVideos: []" in html
    assert "currentFiles[pairId]" in html
    fn = html[html.index("async function removePair("):html.index("/* A local filename is attacker-controllable")]
    # carry-forward line reassigns the WHOLE per-pair state object (including
    # its additionalVideos) under the new id - it never reaches inside one
    # pair's additionalVideos to touch another pair's.
    assert "if (currentFiles[oldId]) newFiles[newId] = currentFiles[oldId];" in fn


def test_remove_pair_renumbers_extra_video_ids_without_touching_other_pairs():
    html = creator_html()
    fn = html[html.index("async function removePair("):html.index("/* A local filename is attacker-controllable")]
    assert "extra-videos-${newId}" in fn or 'extraVideos.id = `extra-videos-${newId}`;' in fn
    assert "extra-videos-usage-${newId}" in fn
    assert "extra-videos-list-${newId}" in fn
    assert "extra-videos-add-${newId}" in fn
    assert "extra-videos-limit-${newId}" in fn


# ===========================================================================
# 11: Direct QR never shows or uses the multi-video controls.
# ===========================================================================
def test_direct_qr_hides_extra_videos_block_via_existing_css_convention():
    html = creator_html()
    assert 'body[data-experience-type="direct_qr"] .pair-extra-videos' in html
    assert 'body[data-experience-type="direct_qr"] .pair-video-default-tag' in html


# ===========================================================================
# 12-13: resumable session creation only for extras, using purpose
# "pair_video" - the primary session is still created the pre-3E-D way.
# ===========================================================================
def test_create_pair_video_session_used_only_for_extras():
    html = creator_html()
    assert "async function createPairVideoSession(" in html
    body = html[html.index("async function createPairVideoSession("):html.index("async function createPairVideoSession(") + 250]
    assert "'pair_video'" in body
    # Only uploadAdditionalVideosForPair calls it - the primary upload path
    # (submitResumableSinglePair/MultiPair) never does.
    assert html.count("createPairVideoSession(") == 2  # definition + the one call site
    enclosing_fn = html[html.index("async function uploadAdditionalVideosForPair("):html.index("/* ==== end of multi-video resumable transport ==== */")]
    assert "await createPairVideoSession(" in enclosing_fn
    assert "filter(entry => entry.file)" in enclosing_fn


# ===========================================================================
# 14: three-video upload flow uploads primary once, then each extra as its
# own independently resumable session, sequentially.
# ===========================================================================
def test_upload_additional_videos_loops_one_session_per_extra():
    html = creator_html()
    fn = html[html.index("async function uploadAdditionalVideosForPair("):html.index("/* ==== end of multi-video resumable transport ==== */")]
    assert "for (let i = 0; i < extras.length; i++)" in fn
    assert "await createPairVideoSession(" in fn
    assert "await uploadPairVideoSession(" in fn
    assert "entry.sessionId = created.session.id;" in fn


# ===========================================================================
# 15: interrupted extra doesn't reupload primary or sibling extras - the
# extra-videos loop runs AFTER the primary is fully uploaded/confirmed, and
# sessionIds already obtained are reused across finalize retries rather than
# re-derived (which would re-run uploads).
# ===========================================================================
def test_extra_session_ids_computed_once_and_reused_across_finalize_retries():
    html = creator_html()
    start = html.index("async function submitResumableSinglePair(")
    end = html.index("async function submitResumableMultiPair(")
    single = html[start:end]
    assert single.count("uploadAdditionalVideosForPair(") == 1
    assert single.count("finalizeResumableWithBoundedRetry(session.id, uploadState, extraSessionIds)") == 2


# ===========================================================================
# 16-17: finalize payload shapes match the 3E-D0 contract exactly.
# ===========================================================================
def test_single_target_finalize_passes_extra_ids_through():
    html = creator_html()
    fn = html[html.index("async function finalizeResumableSession("):html.index("async function finalizeResumableSession(") + 700]
    assert "extra_video_session_ids: extraVideoSessionIds" in fn


def test_multi_target_finalize_builds_and_sends_the_primary_to_extras_map():
    html = creator_html()
    assert "function buildExtraVideoSessionIdMap(" in html
    fn = html[html.index("async function finalizeResumableProject("):html.index("async function finalizeResumableProject(") + 700]
    assert "body.extra_video_session_ids = extraVideoSessionIdMap" in fn
    multi = html[html.index("async function submitResumableMultiPair("):html.index("Last resort only")]
    assert "buildExtraVideoSessionIdMap(extraTargets)" in multi


# ===========================================================================
# 18: classic multipart fallback maps every video (default + extras) to its
# target index, exactly per the already-approved 3E-C contract.
# ===========================================================================
def test_classic_fallback_appends_video_target_indexes_for_every_video():
    html = creator_html()
    assert "fd.append('video_target_indexes', String(index));" in html
    assert "(pair.additionalVideos || []).forEach((extra, extraIndex) => {" in html
    fallback = html[html.index("(pair.additionalVideos || []).forEach((extra, extraIndex) => {"):html.index("(pair.additionalVideos || []).forEach((extra, extraIndex) => {") + 300]
    assert "if (!extra.file) return;" in fallback
    assert "fd.append('videos', extra.file" in fallback


# ===========================================================================
# 20: empty extra-video inputs are never submitted as files.
# ===========================================================================
def test_empty_extra_video_slots_are_filtered_out_of_every_upload_path():
    html = creator_html()
    resumable_fn = html[html.index("async function uploadAdditionalVideosForPair("):html.index("async function uploadAdditionalVideosForPair(") + 400]
    assert "filter(entry => entry.file)" in resumable_fn
    assert "if (!extra.file) return;" in html  # classic fallback guard, checked above too


# ===========================================================================
# 21: mobile - no horizontal overflow, rows stack, filenames truncate.
# ===========================================================================
def test_extra_video_rows_have_mobile_responsive_rules():
    html = creator_html()
    assert ".pair-extra-video-row {" in html
    assert "@media (max-width: 640px) {" in html
    mobile_block = html[html.index(".pair-extra-videos {"):html.index(".video-metadata {")]
    assert "flex-wrap: wrap;" in mobile_block
    assert "max-width: 100%;" in mobile_block


# ===========================================================================
# 22: accessible names for every dynamic control.
# ===========================================================================
def test_dynamic_controls_have_meaningful_aria_labels():
    html = creator_html()
    assert 'aria-label="Video ${slotNumber} for Content set ${pairId}"' in html
    assert 'aria-label="Add another video to Content set ${pairId}"' in html
    assert 'aria-label="Remove Video ${slotNumber} from Content set ${pairId}"' in html


# ===========================================================================
# Review step shows a per-target video count so a mistake is catchable
# before Create, without building a full media manager.
# ===========================================================================
def test_review_preview_shows_video_count_per_target_when_entitled():
    html = creator_html()
    fn = html[html.index("function updatePreview("):html.index("function replaceFile(")]
    assert "ALLOW_MULTI_VIDEO_PER_TARGET" in fn
    assert "preview-video-count" in fn


# ===========================================================================
# Backend: entitlement values reach the template for both user and admin
# create-project pages (admin stays the existing "unlimited & free" shape).
# ===========================================================================
def test_app_exposes_multi_video_entitlements_to_user_create_page():
    app_source = Path("app.py").read_text(encoding="utf-8", errors="ignore")
    user_fn = app_source[app_source.index("def user_create_project_page("):app_source.index("def user_create_project_page(") + 4000]
    assert "allow_multi_video_per_target=_ents[\"allow_multi_video_per_target\"]" in user_fn
    assert "effective_max_videos_per_target=_ents[\"effective_max_videos_per_target\"]" in user_fn


def test_app_exposes_unlimited_multi_video_to_admin_create_page():
    app_source = Path("app.py").read_text(encoding="utf-8", errors="ignore")
    admin_fn = app_source[app_source.index("def admin_create_project_page("):app_source.index("def admin_create_project_page(") + 4000]
    assert "allow_multi_video_per_target=True" in admin_fn
    assert "effective_max_videos_per_target=None" in admin_fn


# ===========================================================================
# Manual browser QA regression: the extra-video file input must NEVER carry
# the shared `.file-input` class. That class is `position:absolute;
# inset:0;opacity:0;z-index:5`, built for the big tile-style upload areas
# (.upload-area is `position:relative` so `inset:0` fills exactly that
# tile) - reused here it has no positioned ancestor of its own, so Chromium
# sized it to ~660px tall and it silently covered (opacity:0) the "Add
# another video" button and everything below it, making the button
# unclickable by a real user. Caught via Playwright's elementFromPoint at
# the Add button's own center during 3E-D manual QA.
# ===========================================================================
def test_extra_video_input_does_not_reuse_the_absolute_tile_overlay_class():
    html = creator_html()
    row_html = html[html.index("function addAdditionalVideo("):html.index("function handleAdditionalVideoSelect(")]
    assert 'class="pair-extra-video-input"' in row_html
    assert "file-input pair-extra-video-input" not in row_html


@pytest.mark.skipif(not NODE, reason="node not available")
def test_creator_inline_js_parses(client, login_user):
    """Render the real page (Jinja already substituted) and hand every inline
    <script> to `node --check` - the only guard that would catch a genuine JS
    syntax error, which a Python string/AST check on the raw template cannot."""
    response = client.get("/create-project", follow_redirects=True)
    assert response.status_code == 200
    html = response.get_data(as_text=True)

    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    assert blocks, "create-project page rendered no inline script blocks"

    checked = 0
    for i, body in enumerate(blocks):
        if not body.strip():
            continue
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / f"block_{i}.js"
            f.write_text(body, encoding="utf-8")
            proc = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True)
        assert proc.returncode == 0, f"inline script block {i} failed node --check:\n{proc.stderr}"
        checked += 1
    assert checked, "no non-empty inline script blocks were checked"


# ===========================================================================
# Manual browser QA regression: the Review step's per-target video count
# must refresh whenever an extra video is added, filled, or removed - not
# only when the primary image/video changes. Caught during 3E-D manual QA:
# after filling 3 videos for one target, the Review step still said "1
# video" because handleAdditionalVideoSelect/removeAdditionalVideo never
# called updatePreview() (only addPair/handleFileSelect/clearAll did).
# ===========================================================================
def test_extra_video_select_and_remove_refresh_the_review_preview():
    html = creator_html()
    select_fn = html[html.index("function handleAdditionalVideoSelect("):html.index("function removeAdditionalVideo(")]
    assert "updatePreview();" in select_fn
    remove_fn = html[html.index("function removeAdditionalVideo("):html.index("function renumberAdditionalVideos(")]
    assert "updatePreview();" in remove_fn


# ===========================================================================
# Manual browser QA regression: reaching the max must not strand keyboard
# focus on <body>. Caught during 3E-D manual QA: focusing "Add another
# video" and pressing Enter to add the video that reaches the limit set the
# button's native `disabled` attribute mid-handler - Chromium (and other
# browsers) blur a focused control the instant it becomes disabled, so a
# keyboard-only user's focus vanished right as they hit the limit.
# ===========================================================================
def test_add_button_uses_aria_disabled_so_focus_survives_reaching_max():
    html = creator_html()
    fn = html[html.index("function updateAdditionalVideoUsage("):html.index("/* ==== end of additional-videos-per-target UI ==== */")]
    assert "addBtn.setAttribute('aria-disabled'" in fn
    assert "addBtn.disabled" not in fn
    assert ".pair-extra-videos-add.is-disabled {" in html
    assert ".pair-extra-videos-add:disabled {" not in html
