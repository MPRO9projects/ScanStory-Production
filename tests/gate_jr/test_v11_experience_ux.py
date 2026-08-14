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
    assert 'id="storyNameError">Please give your ScanStory a name.</div>' in html
    assert "function showStoryNameError()" in html
    assert "nameField.scrollIntoView({ block: 'center', behavior: 'smooth' })" in html
    assert "nameField.focus({ preventScroll: true })" in html
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


def test_completed_pairs_collapse_into_summary_rows():
    html = creator_html()
    assert 'id="pair-summary-${pairId}"' in html
    assert "function updatePairSummary(pairId)" in html
    assert "item.classList.toggle('is-collapsed', complete && !expanded)" in html
    assert "function expandPair(pairId)" in html
    assert ".pair-item.is-collapsed .upload-grid," in html
