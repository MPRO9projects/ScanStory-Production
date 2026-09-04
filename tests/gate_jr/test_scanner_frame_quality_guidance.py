"""Scanner resilience pass: structural coverage for the new client-side
frame-quality/network/video-loading guidance additions in scanner.html.

Same DOM-free, string/regex-on-rendered-source idiom as test_wave7_rate_limit_backoff.py
and test_scanner_cold_start_js.py - these assert the JS text itself, not runtime DOM
behavior (that needs a real browser, see the browser QA pass).
"""
import re
import subprocess
import shutil
from pathlib import Path

import pytest


def _scanner_html():
    return Path("templates/user/scanner.html").read_text(encoding="utf-8", errors="ignore")


NODE = shutil.which("node")


def test_new_guidance_keys_are_additive_not_replacements():
    """All pre-existing guidance copy must survive byte-for-byte (test_wave7 already
    pins these) - this test only proves the NEW keys were added alongside them."""
    html = _scanner_html()
    for existing in ("Move closer", "Move farther away", "Hold steady", "Improve lighting", "Reduce glare"):
        assert existing in html
    for new_text in (
        "Too much direct light",
        "Too dark",
        "Connection is slow",
        "Loading experience",
    ):
        assert new_text in html


def test_guidance_priority_has_new_states_correctly_ordered():
    html = _scanner_html()
    block_start = html.index("const GUIDANCE_PRIORITY = Object.freeze({")
    block = html[block_start:html.index("});", block_start)]
    for key in ("overexposed", "underexposed", "low_contrast", "network_slow", "video_loading"):
        assert f"{key}:" in block

    def _priority(name):
        m = re.search(rf"\b{name}:\s*(\d+)", block)
        assert m, f"missing priority for {name}"
        return int(m.group(1))

    # glare > blur(steady) > exposure > low_contrast > network_slow > looking,
    # matching the priority order in the resilience brief (glare, blur, exposure,
    # contrast, recognizing, network, video loading).
    assert _priority("glare") > _priority("steady")
    assert _priority("steady") > _priority("overexposed")
    assert _priority("overexposed") == _priority("underexposed")
    assert _priority("overexposed") > _priority("low_contrast")
    assert _priority("low_contrast") > _priority("network_slow")
    assert _priority("network_slow") > _priority("looking")
    assert _priority("found") > _priority("video_loading")  # a real match still outranks a buffering notice


def test_guidance_from_detection_uses_new_signals_with_correct_precedence():
    html = _scanner_html()
    start = html.index("function guidanceFromDetection(data)")
    body = html[start:html.index("function isFrameQualityClean")]
    assert "guidance.likely_localized_glare" in body
    assert "guidance.likely_overexposed" in body
    assert "guidance.likely_underexposed" in body
    assert "guidance.likely_low_contrast" in body
    # glare must be checked before blur, blur before exposure, exposure before
    # low_contrast - matching GUIDANCE_PRIORITY's ordering so the two never disagree.
    order = [
        body.index("likely_localized_glare"),
        body.index("likely_blurry"),
        body.index("likely_overexposed"),
        body.index("likely_low_contrast"),
    ]
    assert order == sorted(order)


def test_network_vs_camera_distinction_is_wired():
    html = _scanner_html()
    assert "let lastFrameQualityClean" in html
    assert "function isFrameQualityClean(guidance)" in html
    # Updated once per accepted response, before any guidance is set for it.
    assert "lastFrameQualityClean = isFrameQualityClean(data && data.scanner_guidance);" in html
    # Only blamed on the network when the last known frame was clean.
    timeout_start = html.index("function handleDetectionTimeout()")
    timeout_body = html[timeout_start:html.index("if (detectionFailCount >= RECOVERY_RETRY_LIMIT)", timeout_start)]
    assert "if (lastFrameQualityClean)" in timeout_body
    assert "setScannerGuidance('network_slow', 'detect_timeout')" in timeout_body


def test_video_loading_guidance_debounced_and_self_clearing():
    html = _scanner_html()
    assert 'overlay.addEventListener("waiting"' in html
    assert 'overlay.addEventListener("playing"' in html
    assert "VIDEO_LOADING_GUIDANCE_DELAY_MS" in html
    # Debounced: only fires after the delay, and only if still not playing by then.
    waiting_start = html.index('overlay.addEventListener("waiting"')
    waiting_body = html[waiting_start:html.index(");", html.index("setTimeout", waiting_start)) + 2]
    assert "setTimeout(" in waiting_body
    assert "if (overlay.paused || overlay.ended) return;" in waiting_body
    # Clears back to a real state, never leaves stale guidance behind.
    playing_start = html.index('overlay.addEventListener("playing"')
    playing_body = html[playing_start:html.index("});", playing_start)]
    assert "scannerGuidanceState === 'video_loading'" in playing_body


def test_frame_quality_reason_diagnostic_field_present_in_app_source():
    source = Path("app.py").read_text(encoding="utf-8", errors="ignore")
    assert "def _frame_quality_reason():" in source
    assert '"frame_quality_reason"' in source
    # Logged at both the no-match and accepted exit points, not just one.
    assert source.count("frame_quality_reason=_frame_quality_reason()") == 2


@pytest.mark.skipif(not NODE, reason="node not available in this environment")
def test_scanner_inline_js_still_parses_after_resilience_additions(client, project_with_pair):
    # Render the real page (not the raw template file) so Jinja's {{ }}
    # expressions are already substituted with real values, matching
    # test_scanner_cold_start_js.py::test_scanner_inline_js_parses's approach -
    # reading the raw template file directly still contains literal Jinja
    # delimiters, which is not valid JS on its own.
    project, _pair = project_with_pair
    response = client.get(f"/scanner/{project.id}", follow_redirects=True)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    assert scripts, "expected at least one inline <script> block in the rendered scanner page"
    combined = "\n;\n".join(s for s in scripts if s.strip())
    result = subprocess.run(
        [NODE, "--check"], input=combined, capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr
