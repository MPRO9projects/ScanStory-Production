"""V1.1 Creator Studio UI pass.

Structural guards for what the studio pass ADDED. Everything the pass had to
leave alone is already pinned by tests/gate_jr/test_v11_experience_ux.py (the
mobile wizard, the experience/playback contract, the upload states) and
tests/gate_jr/test_marker_selection_upload.py (the crop interaction, the upload
pipeline, the phase labels). This file only covers the new surfaces, so a future
change that quietly deletes one of them fails here instead of silently shipping.
"""

from html.parser import HTMLParser
from pathlib import Path


VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


class TagBalance(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.errors = []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID_ELEMENTS:
            self.stack.append((tag, self.getpos()))

    def handle_endtag(self, tag):
        if tag in VOID_ELEMENTS:
            return
        if not self.stack:
            self.errors.append(f"stray </{tag}> at {self.getpos()}")
            return
        open_tag, open_pos = self.stack[-1]
        self.stack.pop()
        if open_tag != tag:
            self.errors.append(f"</{tag}> at {self.getpos()} closes <{open_tag}> opened at {open_pos}")


def creator_html():
    return Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")


def edit_html():
    return Path("templates/user/edit_project.html").read_text(encoding="utf-8", errors="ignore")


# --- Desktop studio ---------------------------------------------------------


def test_desktop_stage_rail_exists_and_is_not_a_fourth_wizard_step():
    html = creator_html()
    assert 'id="studioRail"' in html
    for stage in ("story", "experience", "playback", "media", "review", "create"):
        assert f'data-studio-stage="{stage}"' in html, stage
    # The rail is desktop chrome, not the wizard: it must never grow the mobile
    # wizard a fourth step or claim a pane.
    assert 'data-wizard-progress="4"' not in html
    rail = html[html.index('id="studioRail"'):html.index("</ol>", html.index('id="studioRail"'))]
    assert "data-wizard-pane" not in rail
    assert "data-wizard-progress" not in rail
    # Hidden by default, shown only from the tablet breakpoint up.
    assert ".studio-rail {\n      display: none;" in html
    assert "@media (min-width: 641px)" in html


def test_stage_numbers_come_from_a_counter_so_direct_qr_renumbers():
    """Direct QR has no playback stage; a hard-coded "4" would leave a gap."""
    html = creator_html()
    assert "counter-reset: studio-stage;" in html
    assert "counter-increment: studio-stage;" in html
    assert 'body[data-experience-type="direct_qr"] .studio-stage-playback' in html


def test_desktop_reuses_the_one_recap_rather_than_a_second_summary():
    html = creator_html()
    # Same element, same updateWizardRecap(): `.wizard-recap` stays wizard chrome,
    # `.studio-recap` is what makes it visible on desktop.
    assert '<dl class="wizard-recap studio-recap" id="wizardRecap">' in html
    assert ".studio-recap {\n        display: block;" in html
    assert html.count('id="wizardRecap"') == 1


# --- Review step -----------------------------------------------------------


def test_review_shows_presence_of_media_and_the_content_set_count():
    html = creator_html()
    for row in ("recapTargetImage", "recapVideo", "recapPairCount"):
        assert f'id="{row}"' in html, row
    assert "<dt>Target image</dt>" in html
    assert "<dt>Video</dt>" in html
    assert "<dt>Content sets</dt>" in html
    # Target-image presence is meaningless for Direct QR, so that row is hidden by
    # the same rule that hides the other target-only affordances.
    assert '<div class="wizard-recap-row recap-target">' in html
    assert 'body[data-experience-type="direct_qr"] .recap-target' in html


def test_review_states_what_happens_after_create_per_experience_type():
    html = creator_html()
    assert 'id="recapNextSteps"' in html
    after = html[html.index('id="recapNextSteps"'):html.index('id="recapNextSteps"') + 1600]
    # Image -> Video honestly includes a preparation step; Direct QR must not
    # imply an analysis step it does not have.
    assert '<ol class="next-image-video">' in after
    assert '<ol class="next-direct-qr">' in after
    assert "recognise your photo" in after
    assert "nothing has to be analysed" in after
    assert 'body[data-experience-type="direct_qr"] .next-image-video' in html
    assert 'body:not([data-experience-type="direct_qr"]) .next-direct-qr' in html


def test_desktop_says_why_create_is_disabled_using_the_same_reason_text():
    html = creator_html()
    assert 'id="submitBlockedHint"' in html
    hint = html[html.index("function updateWizardCreateHint()"):html.index("/* ===================== Experience type")]
    assert "const reason = wizardCreateBlockedReason();" in hint
    assert "document.getElementById('submitBlockedHint')" in hint
    # One sentence for both viewports, so it cannot name a step the desktop
    # viewer has never seen.
    assert "in step 1" not in html
    assert "in step 2" not in html


# --- Recognition preview (explanation only, never new CV) ------------------


def test_recognition_preview_is_a_static_explanation_not_a_measurement():
    html = creator_html()
    assert 'id="recognitionFlow"' in html
    assert '<span class="recognition-frame">' in html
    assert "This is what your viewer's camera will look for." in html
    # The quad is CSS corners on the existing crop preview. No CV entry point,
    # no confidence number, nothing read from the recognition pipeline.
    frame = html[html.index(".recognition-frame {"):html.index(".recognition-caption {")]
    assert "border: 2px solid var(--accent)" in frame
    # No CV entry point and no invented score anywhere on the creator page.
    for forbidden in ("confidence", "loadOpenCV", "opencv", "OpenCV", "cv.matchTemplate", "descriptor", "keypoint"):
        assert forbidden not in html, forbidden


# --- Target image / video tile states --------------------------------------


def test_selected_media_tiles_state_the_filename_and_the_replace_action():
    """The only "Replace" affordance used to be a :hover overlay, which no touch
    device ever shows, and the chosen filename was never on the tile at all."""
    html = creator_html()
    assert "function uploadTileMarkup(type, url, file)" in html
    assert 'class="ua-caption"' in html
    assert "tap to replace" in html
    assert "uploadTileMarkup('image', fileUrl, file)" in html
    assert "uploadTileMarkup('video', fileUrl, file)" in html
    assert "uploadTileMarkup('image', pairData.imageUrl, pairData.image)" in html
    assert "uploadTileMarkup('video', pairData.videoUrl, pairData.video)" in html
    # A creator can name a local file `<img onerror=...>`, and the name is now
    # printed into innerHTML.
    assert "function escapeHtml(value)" in html
    assert "const name = escapeHtml(file?.name" in html


# --- Crop modal ------------------------------------------------------------


def test_crop_modal_is_a_real_dialog_with_escape_focus_trap_and_return_focus():
    html = creator_html()
    modal = html[html.index('<div class="crop-modal" id="cropModal"'):html.index('<div class="crop-shell">')]
    assert 'role="dialog"' in modal
    assert 'aria-modal="true"' in modal
    assert 'aria-labelledby="cropModalTitle"' in modal
    assert 'aria-describedby="cropModalDesc"' in modal
    assert 'id="cropModalTitle"' in html
    assert "function onCropModalKeydown(event)" in html
    assert "if (event.key === 'Escape')" in html
    assert "function cropFocusables()" in html
    assert "document.addEventListener('keydown', onCropModalKeydown, true);" in html
    assert "document.removeEventListener('keydown', onCropModalKeydown, true);" in html
    assert "if (cropReturnFocus && cropReturnFocus.focus) cropReturnFocus.focus({ preventScroll: true });" in html


def test_keyboard_crop_nudge_reuses_sanitizecrop_and_adds_no_crop_maths():
    """Arrow keys are the keyboard route into a canvas-only interaction. They must
    write through the SAME clamp the pointer path uses, so a keyboard edit can
    never reach a crop a drag could not."""
    html = creator_html()
    start = html.index("function nudgeCropFromKeyboard(event)")
    block = html[start:html.index("async function useCurrentMarker()", start)]
    assert "if (!canvas || document.activeElement !== canvas) return;" in block
    assert "pair.markerMode !== 'crop'" in block
    assert "sanitizeCrop(pair, activeCropImage);" in block
    assert "MIN_MARKER_CROP_FRACTION" in block
    # No second implementation of the bounds maths.
    for forbidden in ("cropDrawRect", "naturalWidth", "getBoundingClientRect"):
        assert forbidden not in block, forbidden
    assert 'aria-label="Marker crop area.' in html


# --- Confirm / toast foundation -------------------------------------------


def test_creator_uses_the_shared_toast_and_confirm_not_native_dialogs():
    html = creator_html()
    assert "js/ss-ui.js" in html
    assert "function notify(message, tone)" in html
    assert "function askConfirm(options)" in html
    # Every gate a window.confirm used to hold is still held, by ssConfirm.
    assert "await askConfirm({" in html
    assert "confirmLabel: 'Remove content set'" in html
    assert "confirmLabel: 'Clear everything'" in html
    # The only remaining native calls are the fallbacks inside those two
    # wrappers, so a blocked ss-ui.js can never swallow a refusal silently.
    assert html.count("window.alert(message)") == 1
    assert html.count("window.confirm(options.body") == 1
    assert "alert('" not in html
    assert "confirm('" not in html


# --- Processing / copy hygiene --------------------------------------------


def test_creator_copy_never_exposes_the_internal_pipeline():
    """Copy hygiene is about what a USER can actually see rendered on the
    page - a developer-only // line comment inside <script> (e.g. explaining
    why a validation rule mirrors the same "exact + ORB/homography" check
    Edit uses) is never rendered by any browser and reaches no user. Strip
    JS line comments before scanning so this test can't false-positive on
    engineering commentary while still catching real user-facing strings."""
    import re
    html = creator_html()
    html_without_js_comments = re.sub(r"^\s*//.*$", "", html, flags=re.MULTILINE)
    for term in ("Redis", " RQ ", "queued", "Queue", "worker", "enqueue", "ORB", "homography", "feature extraction"):
        assert term not in html_without_js_comments, term
    # The truthful state vocabulary instead.
    assert "setUploadProgress('Preparing your ScanStory'" in html


def test_refresh_recovery_notice_is_actually_visible():
    """.upload-state-note is display:none by default and only setUploadState()
    un-hid it, so this message was written into the DOM and never shown."""
    html = creator_html()
    start = html.index("function announceRecoverableResumableUpload()")
    block = html[start:html.index("/* Resolve ONE content set", start)]
    assert "Re-select the same video to continue your upload." in block
    assert "note.style.display = 'block';" in block


# --- Responsive / motion --------------------------------------------------


def test_studio_is_responsive_and_respects_reduced_motion():
    html = creator_html()
    for query in (
        "@media (max-width: 430px)",
        "@media (min-width: 641px)",
        "@media (min-width: 641px) and (max-width: 1024px)",
        "@media (min-width: 1025px)",
        "@media (prefers-reduced-motion: reduce)",
    ):
        assert query in html, query
    # Long filenames must not widen the document.
    assert "overflow-wrap: anywhere;" in html
    # Sticky stage needs a non-scrolling overflow on body; `hidden` stays first so
    # a browser without `clip` keeps today's behaviour.
    assert "overflow-x: hidden;\n      overflow-x: clip;" in html


# --- Progressive disclosure ------------------------------------------------


def test_optional_explanations_are_collapsed_by_default():
    """Every block here is a second explanation of something already stated, or an
    output rather than a control. Open by default they cost roughly half a 900px
    desktop viewport before the creator types anything."""
    html = creator_html()
    # <details> with no `open`: native disclosure semantics, keyboard focusable
    # summary, real aria-expanded, and no JS that could force it open on a step
    # change.
    # ss-disclosure added (Creator UI consistency pass): gives these two the same
    # chevron affordance every other disclosure on the page already has, instead
    # of relying on the browser's inconsistent default <summary> marker.
    assert '<details class="ss-explainer ss-disclosure" id="scanStoryExplainer">' in html
    assert '<details class="recognition-flow ss-disclosure" id="recognitionFlow">' in html
    assert "<summary>What&rsquo;s the difference?</summary>" in html
    assert "<summary>Learn about playback modes</summary>" in html
    assert '<details class="qr-preview-section ss-disclosure">' in html
    assert '<details class="marker-disclosure" id="marker-disclosure-${pairId}">' in html
    # None of them may carry `open`.
    for opened in (
        'id="scanStoryExplainer" open',
        'id="recognitionFlow" open',
        'class="marker-disclosure" id="marker-disclosure-${pairId}" open',
    ):
        assert opened not in html, opened
    # Hiding the marker CONTROLS must not change what gets submitted: crop stays
    # the default marker mode.
    assert "markerMode: 'crop'" in html
    # Direct QR has no marker step at all, so the wrapper is hidden with the rest.
    assert 'body[data-experience-type="direct_qr"] .marker-disclosure' in html
    assert ".pair-item.is-collapsed .marker-disclosure," in html
    # Visible focus on every new disclosure trigger.
    assert ".marker-disclosure > summary:focus-visible {" in html
    assert ".ss-disclosure > summary:focus-visible" in html


def test_runtime_injected_content_set_fragment_is_balanced():
    """addPair() builds its markup as a template literal, so the server-rendered
    page's TagBalance check never sees it. Nesting the marker controls inside a
    <details> is exactly the kind of edit that can leave it unbalanced."""
    html = creator_html()
    start = html.index("const pairHTML = `")
    fragment = html[start + len("const pairHTML = `"):html.index("`;", start)]
    fragment = fragment.replace("${pairId}", "1")

    parser = TagBalance()
    parser.feed(fragment)
    assert not parser.errors, parser.errors[:5]
    assert not parser.stack, parser.stack[:5]


# --- Manage media (edit_project.html) ------------------------------------


def test_edit_project_keeps_the_truthful_replacement_consequences():
    """Only an IMAGE replacement flips pair.is_processed=False and reprocesses; a
    video-only swap is live immediately. This copy is correct and must survive
    every visual pass."""
    html = edit_html()
    assert "<strong>Replacing the target image</strong> means we analyse it again" in html
    assert "<strong>Replacing only the video</strong> takes effect straight away" in html
    assert "nothing is re-analysed" in html
    assert "Either way your QR code and share link never change." in html
    # Master stabilization pass (section 1/2): the image slot's copy now leads
    # with the explicit Take a photo / Choose from device + marker-preparation
    # flow rather than a bare file-format hint - the "goes back to Processing"
    # consequence this test exists to pin is still there, just later in the
    # sentence.
    assert "Take a photo or choose one from your device, then crop the marker area." in html
    assert "Goes back to" in html and "Processing while we analyse it." in html
    assert "MP4. Live as soon as it is saved" in html


def test_edit_project_visual_pass_separates_consequence_from_caveat():
    html = edit_html()
    # Two identical cyan banners read as one paragraph; the storage caveat is
    # explicitly the quieter of the two now.
    assert '<div class="info-banner is-quiet">' in html
    assert ".info-banner.is-quiet {" in html
    # Selected-file state moved from two inline style writes to a class, and a
    # cleared picker no longer leaves the zone looking selected.
    assert ".file-zone.has-selection {" in html
    assert "zone.classList.add('has-selection');" in html
    assert "zone.classList.remove('has-selection');" in html
    assert "js/ss-ui.js" in html
    # Touch targets and focus.
    assert "min-height: 44px;" in html
    assert ":focus-visible" in html
    assert "@media (prefers-reduced-motion: reduce)" in html


def test_admin_project_creation_route_renders_the_same_studio(client, login_admin):
    """user_create_project.html serves BOTH /create-project and
    /admin/projects/create, and the admin context passes neither
    dev_test_entitled nor viewer_is_business_vendor. Every studio addition has to
    survive those being undefined."""
    response = client.get("/admin/projects/create")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'id="studioRail"' in body
    assert 'id="recapNextSteps"' in body
    assert 'id="submitBlockedHint"' in body
    # Vendor-only block stays absent for an admin.
    assert 'id="createdForGroup"' not in body

    parser = TagBalance()
    parser.feed(body)
    assert not parser.errors, parser.errors[:5]
    assert not [tag for tag, _pos in parser.stack if tag not in {"html", "body"}]


def test_edit_project_renders_and_markup_is_balanced(client, login_user, project_with_pair):
    project, _pair = project_with_pair
    response = client.get(f"/projects/{project.id}/edit")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Manage media" in body

    parser = TagBalance()
    parser.feed(body)
    assert not parser.errors, parser.errors[:5]
    assert not [tag for tag, _pos in parser.stack if tag not in {"html", "body"}]
