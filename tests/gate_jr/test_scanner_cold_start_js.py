"""Render the real scanner page, extract every inline <script>, and hand each to
`node --check`. This is the only automated guard in the repo that would catch a
syntax error introduced into scanner.html's inline JS - a Python test can render
the template happily while the browser refuses to parse the script."""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

NODE = shutil.which("node")

_JS_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_JS_LINE_COMMENT = re.compile(r"(?<!:)//[^\n]*")


def _strip_js_comments(source):
    """Comments legitimately narrate what a fix used to do — including the
    literal names of the calls it removed — so a bare substring check on raw
    source reads a code comment as live code. Strip both comment forms before
    asserting what the code actually DOES."""
    return _JS_LINE_COMMENT.sub(" ", _JS_BLOCK_COMMENT.sub(" ", source))


@pytest.mark.skipif(not NODE, reason="node not available")
def test_scanner_inline_js_parses(client, project_with_pair):
    project, _pair = project_with_pair
    response = client.get(f"/scanner/{project.id}", follow_redirects=True)
    assert response.status_code == 200
    html = response.get_data(as_text=True)

    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    assert blocks, "scanner page rendered no inline script blocks"

    checked = 0
    for i, body in enumerate(blocks):
        if not body.strip():
            continue
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / f"block_{i}.js"
            f.write_text(body, encoding="utf-8")
            proc = subprocess.run(
                [NODE, "--check", str(f)], capture_output=True, text=True
            )
        assert proc.returncode == 0, (
            f"inline script block {i} failed node --check:\n{proc.stderr}"
        )
        checked += 1
    assert checked, "no non-empty inline script blocks were checked"


@pytest.mark.skipif(not NODE, reason="node not available")
def test_scanner_cold_start_states_are_honest(client, project_with_pair):
    """The cold-start contract: readiness is claimed in exactly one place, and
    the recognition-load stages name what is actually pending."""
    project, _pair = project_with_pair
    html = client.get(
        f"/scanner/{project.id}", follow_redirects=True
    ).get_data(as_text=True)

    assert "function markScannerReadyIfPossible(reason)" in html
    assert "if (!cvReady || !isCameraHealthy()) return false;" in html
    # The old lie: ready_to_scan asserted straight off camera_ready.
    assert "safeTransition('ready_to_scan', 'camera_ready')" not in html
    assert "markScannerReadyIfPossible('camera_ready')" in html
    assert "markScannerReadyIfPossible('opencv_ready')" in html
    # Sequential, engine-free copy.
    assert "initializing_camera: 'Starting your camera…'" in html
    assert "loading_opencv: 'Preparing image recognition…'" in html
    assert "preparing: 'Preparing image recognition…'" in html
    # The stale copy the sequence replaced. Both recognition-load stages used to
    # render the same vague phrase as the shell-loading stage, which is what left
    # the cold-start wait unexplained.
    assert "loading_opencv: 'Getting ready…'" not in html
    assert 'loading_opencv: ["Preparing Camera"' not in html


@pytest.mark.skipif(not NODE, reason="node not available")
def test_recovered_camera_never_reports_false_failure(client, project_with_pair):
    """The other half of the cold-start ticket: a stream that recoverScannerInner
    just confirmed alive (isStreamDead() false -> recovered=true) must never be
    reported as a camera failure merely because the FOLLOW-UP ready_to_scan
    transition was rejected - transitionScannerState() rejects both a genuine
    illegal move (e.g. some concurrent path already pushed the FSM into
    'fallback' while restartCameraStream() was being awaited) and a same-state
    no-op, and neither is evidence the camera itself failed. The old code
    called enterFallback('CAMERA_UNAVAILABLE') on that rejection regardless,
    overwriting a real state with a wrong "camera not found" one."""
    project, _pair = project_with_pair
    html = client.get(
        f"/scanner/{project.id}", follow_redirects=True
    ).get_data(as_text=True)

    recover_start = html.index("async function recoverScannerInner(reason, restartCamera)")
    recover_end = html.index("async function restartCameraStream(reason)", recover_start)
    body = html[recover_start:recover_end]

    ready_for_scan_at = body.index("const readyForScan = safeTransition('ready_to_scan'")
    rejected_branch = _strip_js_comments(body[ready_for_scan_at:body.index("startDetectLoop();", ready_for_scan_at)])

    # The exact bug: claiming camera failure from a rejected (not necessarily
    # illegal - possibly just redundant) transition, over a stream `recovered`
    # already proved alive one line above. Comments legitimately narrate the
    # removed call by name, so this must run against the comment-stripped code.
    assert "enterFallback(" not in rejected_branch
    assert "automatic_recovery_transition_rejected" in rejected_branch

    # The genuinely-dead-stream path (recovery itself failed after the bounded
    # retry budget) is a real camera problem and must still raise it - this
    # test would also catch a fix that went too far and silenced that too.
    failed_recovery_at = body.index("if (!recovered) {")
    failed_branch = _strip_js_comments(body[failed_recovery_at:ready_for_scan_at])
    assert "enterFallback('CAMERA_INTERRUPTED')" in failed_branch

    # The three-way split this fix depends on: a device with no usable camera,
    # a getUserMedia() failure that is not a permission denial, and a stream
    # that stopped mid-session must stay distinguishable from each other and
    # from a permission denial - not collapsed back into one generic code.
    assert "CAMERA_NOT_FOUND: " in html
    assert "CAMERA_START_FAILED: " in html
    assert "CAMERA_INTERRUPTED: " in html
    # Comments narrate the removed code by name (e.g. "used to call
    # enterFallback('CAMERA_UNAVAILABLE')"), so this has to run comment-stripped
    # too, or every one of those explanations fails it.
    assert "'CAMERA_UNAVAILABLE'" not in _strip_js_comments(html)
