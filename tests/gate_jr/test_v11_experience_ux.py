"""V1.1 creator / viewer experience UX.

These are structural checks over the rendered creator page and the scanner template:
the wizard is a client-side view over the existing form DOM, so the invariants worth
locking down are "the step chrome exists", "the panes are wired to the right steps",
"the markup still parses", and "Direct QR Video never reaches the recognition stack".
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


def scanner_html():
    return Path("templates/user/scanner.html").read_text(encoding="utf-8", errors="ignore")


def test_creator_page_renders_and_markup_is_balanced(client, login_user):
    response = client.get("/create-project")
    assert response.status_code == 200

    parser = TagBalance()
    parser.feed(response.get_data(as_text=True))
    assert not parser.errors, parser.errors[:5]
    assert not [tag for tag, _pos in parser.stack if tag not in {"html", "body"}]


def test_mobile_wizard_has_three_steps_with_progress_and_sticky_actions():
    html = creator_html()
    assert 'id="wizardProgress"' in html
    for label in ("Details", "Content", "Review"):
        assert f'<span class="wizard-progress-label">{label}</span>' in html
    assert 'data-wizard-pane="1 2"' in html
    assert 'data-wizard-pane="1"' in html
    assert 'data-wizard-pane="2"' in html
    assert 'data-wizard-pane="3"' in html
    assert 'id="wizardBackBtn"' in html
    assert 'id="wizardNextBtn"' in html
    assert 'id="wizardCreateBtn"' in html
    # Step switching is CSS-only over the same DOM, which is what preserves typed values
    # and already-selected files when the user walks backwards through the wizard.
    assert 'body[data-wizard-step="1"] [data-wizard-pane]:not([data-wizard-pane~="1"])' in html


def test_desktop_layout_is_untouched_by_the_wizard_wrappers():
    html = creator_html()
    # The wrappers are display:contents so the .panel elements stay the grid children.
    assert ".wizard-pane-wrap {\n      display: contents;\n    }" in html
    # None of the wizard chrome is visible outside the existing mobile breakpoint.
    assert ".wizard-progress,\n    .wizard-actions,\n    .wizard-recap," in html
    assert "@media (max-width: 640px)" in html


def test_missing_story_name_shows_the_specific_message_and_focuses_the_field():
    html = creator_html()
    assert 'id="storyNameError" role="alert">Please give your ScanStory a name.</div>' in html
    assert "function showStoryNameError()" in html
    assert "nameField.scrollIntoView({ block: 'center', behavior: 'smooth' })" in html
    assert "nameField.focus({ preventScroll: true })" in html
    # The message is also wired to the field for assistive tech, and only while it shows.
    assert "nameField.setAttribute('aria-describedby', 'storyNameError')" in html
    assert "nameField.removeAttribute('aria-describedby')" in html
    # The generic browser bubble must not be the only feedback on submit.
    assert "alert('Please enter a Story name.')" not in html


def test_only_two_experience_types_exist_and_direct_qr_hides_the_target_upload():
    html = creator_html()
    assert 'id="experienceTypeImageVideo"' in html
    assert 'id="experienceTypeDirectQr"' in html
    assert "Image &rarr; Video" in html
    assert "Direct QR Video" in html
    # No pic-to-pic in V1.1.
    assert "Image → Image" not in html
    assert "image_image" not in html
    assert 'body[data-experience-type="direct_qr"] .pair-image-block' in html
    assert "input.required = value === 'image_video'" in html


def test_creator_explains_what_scanstory_is_and_how_it_works():
    html = creator_html()
    assert "ScanStory connects real-world images to digital video experiences through QR and visual recognition." in html
    assert 'id="scanStoryExplainer"' in html
    for step in (
        "Choose the target photo",
        "Add the video",
        "We generate a QR code",
        "scans the QR code",
        "points the camera at the target",
        "The video plays over the target",
    ):
        assert step in html, step


def test_creator_test_control_is_named_test_experience_everywhere():
    for path in (
        "templates/user/user_create_project.html",
        "templates/user/project_preview.html",
        "templates/user/success.html",
        "templates/user/dashboard.html",
    ):
        html = Path(path).read_text(encoding="utf-8", errors="ignore")
        assert "Test Experience" in html, path
        assert "AR Test Scanner" not in html, path


def test_upload_exposes_every_slow_connection_state():
    html = creator_html()
    assert 'id="uploadStateNote"' in html
    assert "function setUploadState(state)" in html
    for state in ("preparing", "uploading", "slow", "retrying", "interrupted", "resuming", "uploaded", "processing", "ready", "failed"):
        assert f"'{state}'" in html, state
    # Resume is real, not a placeholder: it is driven by the existing chunked session API.
    assert "getUploadSessionStatus(sessionId" in html
    assert "RESUMABLE CLIENT RESUME" in html
    # The browser's own connectivity events drive interrupted/resuming, no polling.
    assert "window.addEventListener('offline'" in html
    assert "window.addEventListener('online'" in html


def test_slow_connection_copy_never_claims_a_bandwidth_number():
    html = creator_html()
    assert "Slow connection detected." in html
    for claim in ("Mbps", "mbps", "Kbps", "kbps", "Mb/s", "kb/s"):
        assert claim not in html, claim


def test_completed_pairs_collapse_into_summary_rows():
    html = creator_html()
    assert 'id="pair-summary-${pairId}"' in html
    assert "function updatePairSummary(pairId)" in html
    assert "item.classList.toggle('is-collapsed', complete && !expanded)" in html
    assert "function expandPair(pairId)" in html
    assert ".pair-item.is-collapsed .upload-grid," in html


def test_creator_calls_the_thing_a_scanstory_made_of_pairs():
    """The creator used to call one thing four names on one screen: a Memory in the title,
    a moment on the upload tiles, a project in the plan messages and a Story in the name
    field. A first-time user has to be able to tell they are all the same thing."""
    html = creator_html()
    assert "<title>Create ScanStory | SCANSTORY</title>" in html
    assert "<h1>Create ScanStory</h1>" in html
    for stale in ("Create Your Memory", "Create a Memory", "Moment #", "Add Another Moment", "memoty"):
        assert stale not in html, stale
    # Plan/limit messaging talks about pairs in a ScanStory, not pairs in a project.
    assert "image-video pair(s) per project" not in html
    assert "pair(s) per ScanStory" in html


def test_creator_errors_say_what_to_do_next():
    html = creator_html()
    for shouty in ("Invalid image type!", "Invalid video type!", "size exceeds"):
        assert shouty not in html, shouty
    assert "That photo format is not supported. Please choose a JPG or PNG." in html
    assert "That video format is not supported. Please choose an MP4." in html
    # One recovery phrasing, not "retry" in some places and "try again" in others.
    assert "Please retry." not in html


def test_mobile_wizard_submit_is_not_blocked_by_hidden_required_fields():
    """A browser silently refuses to report an invalid required control it cannot focus,
    and the wizard hides two of its three panes on mobile."""
    html = creator_html()
    assert 'id="projectForm" novalidate' in html
    assert "if (activeWizardCreateBtn) activeWizardCreateBtn.disabled = true;" in html


# --- Viewer: target guide, camera guidance, detect once, direct QR ---------------------


def render_scanner(app_module, **overrides):
    """Render scanner.html directly. Direct QR Video cannot be reached through /scanner yet
    (no persisted experience type), so the template is the only place its branch can be
    exercised today."""
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
        "targets": [
            {"index": i, "image_url": f"/image/1/{i}", "video_url": f"/video/1/{i}", "label": f"Target {i + 1}"}
            for i in range(1)
        ],
    }
    context.update(overrides)
    if context["experience_type"] == "direct_qr" and "playback_mode" not in overrides:
        context["playback_mode"] = "direct"
    # A request context (not just an app context) so url_for in the template can build.
    with app_module.app.test_request_context("/scanner/1"):
        return app_module.app.jinja_env.get_template("user/scanner.html").render(**context)


def test_camera_is_not_requested_until_the_viewer_presses_start_camera():
    html = scanner_html()
    # The only top-level thing that happens now is showing the guide.
    assert "markStartupCheckpoint('camera_setup_requested');\n    showExperienceIntro();" in html
    assert "\n    setupCamera();\n" not in html
    assert "function startCameraFromIntro()" in html
    assert "startCameraBtn.addEventListener('click', startCameraFromIntro)" in html


def test_target_guide_explains_the_target_and_the_camera_before_starting():
    html = scanner_html()
    assert 'id="targetGuide"' in html
    assert "This ScanStory uses your camera to recognize the target shown above." in html
    assert 'id="startCameraBtn">Start Camera</button>' in html
    # 1 / 2-6 / 7+ layouts.
    assert "{% if targets | length == 1 %}single{% elif targets | length > 6 %}many{% else %}gallery{% endif %}" in html
    assert 'id="viewAllTargetsBtn"' in html
    assert "#targetGuide.many .target-extra" in html


def test_target_guide_is_a_preview_layer_and_never_filters_detection(app_module):
    """The guide only renders images. It must not pass a chosen target into detection."""
    html = scanner_html()
    intro_start = html.index('<div id="experienceIntro"')
    intro_end = html.index('<div class="wrap" id="wrap">')
    intro = html[intro_start:intro_end]
    for forbidden in ("pair_index", "detect_init", "expected_pair", "target_index"):
        assert forbidden not in intro, forbidden


def test_detect_once_is_a_lifecycle_change_not_a_detection_change():
    html = scanner_html()
    assert "function lockDetectOnceExperience(reason)" in html
    assert "let scannerPlaybackMode = SERVER_PLAYBACK_MODE === 'detect_once' ? 'detect_once' : 'tracked_overlay';" in html
    assert "function selectedPlaybackMode()" in html
    assert "input[name=\"playbackMode\"]" not in html
    # The lock only suppresses the two functions that could stop playback.
    assert "function stopOverlayImmediate() {\n      if (detectOnceLocked) return;" in html
    assert "function requestPoseHold(reason) {\n      if (detectOnceLocked) return;" in html
    # It must not reach into recognition at all.
    lock_start = html.index("function lockDetectOnceExperience(reason)")
    lock_end = html.index("function stopOverlayImmediate()")
    lock_body = html[lock_start:lock_end]
    for forbidden in ("detect_init", "setupCamera(", "recoverScanner(", "MIN_", "RATIO", "THRESHOLD"):
        assert forbidden not in lock_body, forbidden


def test_direct_qr_never_loads_opencv_or_asks_for_the_camera(app_module):
    html = render_scanner(app_module, experience_type="direct_qr")
    # No recognition entry points are rendered at all on this branch.
    assert 'id="startCameraBtn"' not in html
    assert 'id="targetGuide"' not in html
    assert 'id="directQrVideo"' in html
    assert 'id="directQrPlayBtn"' in html
    assert 'data-experience-type="direct_qr"' in html
    # opencv.js is still referenced in the loader source, but the call is gated.
    assert "if (USES_IMAGE_RECOGNITION) {\n        markStartupCheckpoint('opencv_load_requested');\n        loadOpenCV();\n      }" in html
    # startCameraFromIntro is the only caller of setupCamera outside camera recovery, and
    # its button does not exist in this branch, so getUserMedia is unreachable here.
    assert "if (startCameraBtn) startCameraBtn.addEventListener('click', startCameraFromIntro);" in html


def test_direct_qr_player_is_separate_from_the_ar_overlay_element(app_module):
    html = render_scanner(app_module, experience_type="direct_qr")
    direct_start = html.index("function startDirectQrPlayback()")
    direct_end = html.index("if (startCameraBtn)", direct_start)
    body = html[direct_start:direct_end]
    assert "overlay." not in body
    assert "overlayWrap" not in body
    assert "getElementById('directQrVideo')" in body


def test_image_video_scanner_page_renders_the_guide(app_module):
    html = render_scanner(app_module)
    assert 'data-experience-type="image_video"' in html
    assert 'data-playback-mode="tracked_overlay"' in html
    assert 'id="targetGuide"' in html and 'class="single"' in html
    assert "Point your camera at this to start the experience." in html
    assert 'id="directQrVideo"' not in html


def test_detect_once_scanner_page_uses_persisted_playback_mode(app_module):
    html = render_scanner(app_module, playback_mode="detect_once")
    assert 'data-playback-mode="detect_once"' in html
    assert "Play once detected" in html


def test_scanner_markup_is_balanced_in_both_experience_types(app_module):
    for experience_type in ("image_video", "direct_qr"):
        parser = TagBalance()
        parser.feed(render_scanner(app_module, experience_type=experience_type))
        assert not parser.errors, (experience_type, parser.errors[:5])
        assert not [tag for tag, _pos in parser.stack if tag not in {"html", "body"}], experience_type


def test_many_targets_collapse_behind_view_all(app_module):
    targets = [
        {"index": i, "image_url": f"/image/1/{i}", "video_url": f"/video/1/{i}", "label": f"Target {i + 1}"}
        for i in range(9)
    ]
    html = render_scanner(app_module, targets=targets)
    assert 'id="targetGuide"' in html and 'class="many"' in html
    assert html.count("target-card target-extra") == 3
    assert "View all 9 targets" in html


# --------------------------------------------------------------------------
# Playback Style — creator side
#
# The creator now chooses Project.playback_mode at creation time, so these lock
# down the two things that make an invalid combination unreachable from the form:
# the radios are native (so the browser submits them) and they are DISABLED for
# Direct QR (so nothing is submitted and the server applies its own 'direct').
# --------------------------------------------------------------------------


def test_playback_style_is_a_native_radio_group_in_wizard_step_one():
    html = creator_html()
    assert 'id="playbackModeGroup"' in html
    assert 'role="radiogroup"' in html and 'aria-labelledby="playbackModeLabel"' in html
    assert 'name="playback_mode" value="tracked_overlay" checked' in html
    assert 'name="playback_mode" value="detect_once"' in html
    # Step 1 is the Details pane; the group must sit inside it so Back/Next keeps it.
    step_one = html.split('<div data-wizard-pane="1">')[1].split('<div data-wizard-pane="2">')[0]
    assert 'id="playbackModeGroup"' in step_one


def test_direct_qr_hides_and_disables_the_playback_radios():
    html = creator_html()
    assert 'body[data-experience-type="direct_qr"] #playbackModeGroup' in html
    # Disabled controls are not submitted, which is what makes direct_qr+tracked_overlay
    # unsendable rather than merely discouraged.
    assert "input.disabled = value !== 'image_video';" in html


def test_review_step_recaps_the_chosen_playback_style():
    html = creator_html()
    assert 'id="recapPlaybackMode"' in html
    assert "<dt>Playback style</dt>" in html
    assert "function playbackModeLabel()" in html
    # The recap reads the same radios the form posts, so the two cannot disagree.
    assert "document.querySelector('input[name=\"playback_mode\"]:checked')" in html
    assert "if (recapPlayback) recapPlayback.textContent = playbackModeLabel();" in html


def test_creator_upload_paths_send_experience_and_playback_contract():
    html = creator_html()
    assert "function creatorExperiencePayload()" in html
    assert "playback_mode: type === 'direct_qr' ? 'direct'" in html
    assert "fd.append('experience_type', experiencePayload.experience_type);" in html
    assert "fd.append('playback_mode', experiencePayload.playback_mode);" in html
    assert "...experiencePayload" in html
    assert "createResumableSession(resumableMarkerFile, pair.video, projectName, experiencePayload" in html


def test_resumable_recovery_matches_experience_and_playback_contract():
    html = creator_html()
    matcher = html[html.index("function storedSessionMatchesFiles("):html.index("function sequentialUploadSlice(")]
    # Early returns rather than one && chain since the V1.1 low-bandwidth
    # pass added the file fingerprint, but the same four facts must still be
    # compared before a stored session is ever resumed.
    assert "stored.experience_type !== experiencePayload.experience_type" in matcher
    assert "stored.playback_mode !== experiencePayload.playback_mode" in matcher
    assert "stored.imageName !== (markerFile?.name || null)" in matcher
    assert "stored.imageSize !== (markerFile?.size || 0)" in matcher
    # And the fingerprint is what actually proves file identity: metadata
    # alone cannot tell two exports of the same clip apart.
    assert "fingerprintsMatch(stored.videoFingerprint, fingerprints?.video)" in matcher
    saved_start = html.index("activeResumableUpload = {", html.index("const session = sessionPayload.session;"))
    saved_state = html[saved_start:html.index("saveResumableUploadState(activeResumableUpload);", saved_start)]
    assert "experience_type: experiencePayload.experience_type" in saved_state
    assert "playback_mode: experiencePayload.playback_mode" in saved_state


def test_direct_qr_resumable_upload_sends_video_only_not_fabricated_marker():
    html = creator_html()
    assert "const totalBytes = (resumableMarkerFile?.size || 0) + pair.video.size;" in html
    assert "image_size: markerFile?.size || 0" in html
    assert "original_image_name: markerFile?.name || null" in html
    assert "if (experiencePayload.experience_type === 'image_video')" in html


def test_project_creation_submit_buttons_share_the_same_wording():
    html = creator_html()
    assert '<i class="fas fa-qrcode"></i> Create ScanStory' in html
    assert "Get My Scan Code" not in html
    # The genuinely separate QR retrieval action lives on the success page and keeps
    # its own wording.
    success = Path("templates/user/success.html").read_text(encoding="utf-8", errors="ignore")
    assert "Download QR Code" in success


def test_nav_back_meets_the_minimum_touch_target():
    assert "min-height: 44px;" in creator_html()


# --------------------------------------------------------------------------
# Playback Style — viewer side
# --------------------------------------------------------------------------


def test_viewer_has_no_operable_playback_choice():
    html = scanner_html()
    # No radios, no fieldset, no legend: the viewer states the creator's choice.
    assert 'name="playbackMode"' not in html
    assert "<fieldset" not in html
    assert "#playbackModeChoice legend" not in html
    assert "selectedPlaybackMode()" in html


def test_viewer_fails_safe_on_an_unrecognized_persisted_mode():
    html = scanner_html()
    assert "const PLAYBACK_MODES = ['tracked_overlay', 'detect_once', 'direct'];" in html
    assert "console.warn('[scanner] unrecognized persisted playback mode'" in html
    # The recovery is a safe default, never a choice UI.
    assert "PLAYBACK_MODES.indexOf(RAW_PLAYBACK_MODE) === -1 ? 'tracked_overlay' : RAW_PLAYBACK_MODE" in html
    # And no client-side override channel was introduced alongside it.
    assert "localStorage.getItem('playback" not in html


def test_fallback_playback_never_writes_the_persisted_playback_mode():
    html = scanner_html()
    # SERVER_PLAYBACK_MODE is a const read once from the server-rendered dataset, and
    # scannerPlaybackMode is only ever assigned from selectedPlaybackMode().
    assert "const SERVER_PLAYBACK_MODE" in html
    assert html.count("scannerPlaybackMode =") == 2  # the let-initializer and the intro assignment
    assert "scannerPlaybackMode = selectedPlaybackMode();" in html
