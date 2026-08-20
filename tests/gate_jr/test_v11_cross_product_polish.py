"""Cross-product polish guards (final pre-release-gate pass).

These are structural, source-level invariants that hold across the whole
product surface. They are deliberately NOT style-string assertions: each one
guards a defect class that actually shipped at least once on this branch and
would silently regress again.

Nothing here touches backend behaviour, auth, payment or scanner logic.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = REPO_ROOT / "templates"
STATIC = REPO_ROOT / "static"

# --------------------------------------------------------------------------
# Protected scanner files. The recognition runtime is explicitly out of scope
# for every UI pass; these hashes are the tripwire that proves a presentation
# change did not reach into it. LF-normalised so the check is not defeated by
# a CRLF checkout.
# --------------------------------------------------------------------------
PROTECTED_FILE_HASHES = {
    "static/js/scanner-runtime.js": (
        "05badbd03e00c22715edbdba168db8721ae621493acab8a211a54dbf76acc5b2"
    ),
    "scanner_runtime.py": (
        "eda140bf24f534e160d365c863c618469d68bbcf9619273d499674590324cec0"
    ),
}


def _lf_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _user_templates() -> list[Path]:
    return sorted((TEMPLATES / "user").rglob("*.html"))


def _all_templates() -> list[Path]:
    return sorted(TEMPLATES.rglob("*.html"))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
# Deliberately does not match "https://" etc: a "//" preceded by ":" is a URL
# scheme, not a line comment. No protocol-relative ("//host/path") URLs exist
# in this template set (checked), so this stays a safe line-comment strip.
_JS_LINE_COMMENT = re.compile(r"(?<!:)//[^\n]*")


def _strip_comments(source: str) -> str:
    """Remove authoring commentary so we only assert on what the page does.

    Every template on this branch documents its own layout decisions in
    {# #} blocks, CSS /* */ blocks and JS // lines, and those blocks
    legitimately name the very things these tests forbid in live markup (the
    old duplicate <link>s, "backend", the release labels). Asserting against
    raw source would fail on the docs, not on anything a viewer ever sees.
    """
    source = _JINJA_COMMENT.sub(" ", source)
    source = _HTML_COMMENT.sub(" ", source)
    source = _CSS_COMMENT.sub(" ", source)
    source = _JS_LINE_COMMENT.sub(" ", source)
    return source


@pytest.mark.parametrize("rel_path,expected_sha", sorted(PROTECTED_FILE_HASHES.items()))
def test_protected_scanner_files_are_byte_identical(rel_path, expected_sha):
    """The scanner recognition runtime must survive every UI pass untouched."""
    target = REPO_ROOT / rel_path
    assert target.is_file(), f"protected file missing: {rel_path}"
    assert _lf_sha256(target) == expected_sha, (
        f"{rel_path} changed. This file is protected: revert the edit rather "
        f"than updating this hash."
    )


def test_data_aos_attributes_never_appear_without_the_aos_library():
    """AOS's stylesheet sets [data-aos] { opacity: 0 }.

    A template carrying data-aos attributes but not loading aos.js therefore
    renders permanently invisible content if the stylesheet is ever present,
    and carries dead attributes if it is not. Either way the attribute has no
    business existing without the library that consumes it.
    """
    offenders = []
    for template in _user_templates():
        source = _strip_comments(_read(template))
        if "data-aos" not in source:
            continue
        if "aos.js" not in source or "AOS.init" not in source:
            offenders.append(template.relative_to(REPO_ROOT).as_posix())
    assert not offenders, (
        "templates use data-aos attributes without initialising AOS "
        f"(dead attributes / invisible content): {offenders}"
    )


def test_aos_stylesheet_is_never_loaded_without_its_script():
    """The inverse failure: aos.css alone hides every annotated element."""
    offenders = []
    for template in _user_templates():
        source = _strip_comments(_read(template))
        if "aos.css" in source and "aos.js" not in source:
            offenders.append(template.relative_to(REPO_ROOT).as_posix())
    assert not offenders, f"aos.css loaded without aos.js: {offenders}"


def test_head_assets_partial_is_not_duplicated_by_direct_links():
    """templates/user/_head_assets.html owns fonts + icons + design system.

    Pages drifted to hand-copied <link> tags before that partial existed, which
    produced two Font Awesome versions and a design-system.css that loaded
    before Tailwind and lost every token override. Any page that includes the
    partial must not also link those assets itself.
    """
    partial = "_head_assets.html"
    offenders = []
    for template in _user_templates():
        source = _strip_comments(_read(template))
        if partial not in source:
            continue
        for asset in ("font-awesome", "fonts.googleapis.com", "design-system.css"):
            if asset in source:
                offenders.append(
                    f"{template.relative_to(REPO_ROOT).as_posix()} duplicates {asset}"
                )
    assert not offenders, offenders


def test_only_one_font_awesome_version_across_creator_and_public_pages():
    """Icon version drift produced missing glyphs on individual pages.

    Scoped to templates/user/** - this lane's actual jurisdiction. Admin
    (templates/admin/**) still pins 6.4.0 in admin/base.html and two
    standalone pages; that is a real, separate drift but out of bounds here
    (Admin files are a hard boundary for this pass) - it belongs to whoever
    next touches admin/base.html's shared head.
    """
    versions = set()
    for template in _user_templates():
        source = _strip_comments(_read(template))
        versions.update(re.findall(r"font-awesome/(\d+\.\d+\.\d+)/", source))
    assert len(versions) <= 1, f"multiple Font Awesome versions in use: {sorted(versions)}"


def test_social_preview_images_resolve_to_files_that_exist():
    """og:image pointed at /static/uploads/og/ on six pages; nothing is there.

    Every social and SEO crawler got a 404 for the preview image. The asset
    actually lives under /static/assets/og/. This guards the whole class:
    any absolute /static/... reference in a meta/JSON-LD image field must
    resolve to a file on disk.
    """
    pattern = re.compile(r"/static/(assets|uploads)/og/[A-Za-z0-9._\-]+")
    missing = []
    for template in _all_templates():
        source = _read(template)
        for ref in set(pattern.findall(source) or []):
            pass
        for ref in set(pattern.finditer(source)):
            rel = ref.group(0)[len("/static/"):]
            if not (STATIC / rel).is_file():
                missing.append(
                    f"{template.relative_to(REPO_ROOT).as_posix()} -> {ref.group(0)}"
                )
    assert not missing, f"social preview image references do not exist: {sorted(set(missing))}"


def test_creator_ownership_copy_avoids_backend_jargon():
    """"the backend says..." leaked into creator-facing ownership copy.

    Ownership is the surface where a creator is most anxious about what is
    happening to their project; naming our server tiers there explains
    nothing. The capacity/eligibility vocabulary is what the page already
    uses everywhere else.
    """
    offenders = []
    for name in ("ownership.html", "project_preview.html", "profile.html"):
        template = TEMPLATES / "user" / name
        source = _strip_comments(_read(template))
        for match in re.finditer(r"\bbackend\b", source, re.IGNORECASE):
            start = max(0, match.start() - 70)
            offenders.append(f"{name}: ...{source[start:match.end() + 40].strip()}...")
    assert not offenders, offenders


def test_tracked_overlay_is_never_called_object_tracking():
    """One product name for the playback mode, everywhere."""
    offenders = []
    for template in _all_templates():
        if re.search(r"object\s+tracking", _read(template), re.IGNORECASE):
            offenders.append(template.relative_to(REPO_ROOT).as_posix())
    assert not offenders, (
        f'"Object Tracking" must be "Tracked Overlay": {offenders}'
    )


def test_creator_share_links_use_the_canonical_public_identity():
    """Share surfaces must never render the stale project.scanner_url column.

    That column was written at creation time and holds a legacy /scanner/<id>
    address. The canonical public entry point is /s/<public_key>, resolved by
    the route into share_url / project.public_share_url.
    """
    expected = {
        "dashboard.html": "project.public_share_url",
        "projects.html": "project.public_share_url",
        "success.html": "share_url",
    }
    for name, token in expected.items():
        source = _strip_comments(_read(TEMPLATES / "user" / name))
        assert token in source, f"{name} lost its canonical share-url binding ({token})"
        assert "project.scanner_url" not in source, (
            f"{name} renders the stale project.scanner_url column"
        )


def test_admin_superadmin_only_nav_items_stay_permission_guarded():
    """A normal Admin must not see Super-Admin-only destinations.

    This guard was added once and then survived a later admin-shell
    convergence; it is cheap to keep asserting.
    """
    source = _read(TEMPLATES / "admin" / "_sidebar_links.html")
    superadmin_endpoints = [
        "admin_plans",
        "admin_manage_admins",
        "admin_capacity",
        "admin_settings",
        "admin_activity_logs",
        "admin_operations",
    ]
    for endpoint in superadmin_endpoints:
        index = source.find(f"url_for('{endpoint}')")
        assert index != -1, f"{endpoint} nav link disappeared from the admin sidebar"
        preceding = source[:index]
        assert "admin_can(" in preceding, f"{endpoint} nav link is not permission guarded"
        # the nearest preceding guard must be a superadmin.* capability
        last_guard = preceding.rfind("admin_can(")
        guard = source[last_guard:index]
        assert "superadmin." in guard, (
            f"{endpoint} is guarded by {guard.strip()!r}, not a superadmin capability"
        )


def test_creator_studio_mobile_wizard_contracts_are_present():
    """The mobile create flow is driven entirely by these hooks."""
    source = _read(TEMPLATES / "user" / "user_create_project.html")
    for hook in (
        'id="wizardProgress"',
        'id="wizardBackBtn"',
        'id="wizardNextBtn"',
        'id="wizardCreateBtn"',
        "data-wizard-pane",
    ):
        assert hook in source, f"Creator Studio wizard lost {hook}"


def test_scanner_first_start_recovery_helper_is_shared_by_both_entry_points():
    """First Start Camera and Retry Camera must go through one recovery path.

    They diverged once, and the intro button silently failed to recover a
    fallback state that the retry button did recover.
    """
    source = _read(TEMPLATES / "user" / "scanner.html")
    assert "async function recoverFallbackAndOpenCamera(" in source
    for caller in ("retryCameraFromFallback", "startCameraFromIntro"):
        index = source.find(f"function {caller}(")
        assert index != -1, f"{caller} is gone"
        body = source[index:index + 1600]
        assert "recoverFallbackAndOpenCamera(" in body, (
            f"{caller} no longer routes through the shared recovery helper"
        )


def test_auth_forms_retain_their_exact_submission_hooks():
    """Field names and CSRF tokens are the contract the routes parse."""
    required = {
        "login.html": ['name="csrf_token"', 'name="email"', 'name="password"'],
        "register.html": [
            'name="csrf_token"',
            'name="email"',
            'name="g-recaptcha-response"',
        ],
        "forgot_password.html": ['name="csrf_token"', 'name="email"'],
        "reset_password.html": ['name="csrf_token"', 'name="otp"'],
        "verify_email.html": ['name="csrf_token"', 'name="otp"'],
    }
    for name, hooks in required.items():
        source = _read(TEMPLATES / "user" / name)
        for hook in hooks:
            assert hook in source, f"{name} lost {hook}"


def test_razorpay_checkout_script_only_loads_where_checkout_can_run():
    """Don't ship a third-party checkout script to pages that never call it.

    subscribe.html instantiates Razorpay directly; profile.html and
    project_preview.html load static/js/ss-addons.js, which instantiates it
    for add-on purchases. Any other page linking checkout.js is dead weight.
    """
    addons_js = (STATIC / "js" / "ss-addons.js").read_text(
        encoding="utf-8", errors="replace"
    )
    assert "Razorpay" in addons_js, (
        "ss-addons.js no longer uses Razorpay; the pages that load "
        "checkout.js only for it should drop the script"
    )
    allowed = {"subscribe.html", "profile.html", "project_preview.html"}
    for template in _all_templates():
        source = _strip_comments(_read(template))
        if "checkout.razorpay.com" in source:
            assert template.name in allowed, (
                f"{template.relative_to(REPO_ROOT).as_posix()} loads the Razorpay "
                f"checkout script but has no checkout flow"
            )
