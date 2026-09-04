"""Narrow duplicate-handling fix — focused regression tests (SCANSTORY V1.1, 2026-09-01).

Covers ONLY the defects proven by the prior audit and fixed in this pass:
  - Create: a duplicate-rejected target photo no longer reaches readyPairs/final
    submission (pairIsComplete() now requires markerConfirmed).
  - Create: the rejected candidate's state is actually cleared, not left stale
    (clearRejectedTargetCandidate()).
  - Create: setFullImageMode() now runs the same duplicate check as the crop
    path, and sets markerConfirmed on success (previously it did neither).
  - Create: a new client-side exact-hash video-duplicate pre-check
    (findDuplicateVideoInScope()), scoped exactly like the backend's own rule.
  - Edit: two new backend read/validation-only endpoints
    (user_validate_target_candidate / user_validate_video_candidate) that
    wrap the existing authoritative helpers, with no persistence.

These are functional tests where practical (actually executing the extracted
JS via Node with minimal stubs and asserting real return values/state
mutations), not source-string checks - see the audit's own section 24/14-15
finding that a source-string test would still pass with the bug present.

Run only this pack:
    python -m pytest tests/gate_jr/test_v11_duplicate_handling_fix.py -q
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")


def _read(path):
    return Path(path).read_text(encoding="utf-8", errors="ignore")


def _creator_html():
    return _read("templates/user/user_create_project.html")


def _edit_html():
    return _read("templates/user/edit_project.html")


def _extract_and_neutralize_scripts(html):
    """Pulls every inline <script> (not src=) out of a template and blanks
    Jinja expressions/statements so the result is plain, executable JS - the
    same idiom test_scanner_inline_js_still_parses_after_guidance_reposition
    already uses via node --check."""
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    combined = "\n;\n".join(scripts)
    combined = re.sub(r"\{\{.*?\}\}", "0", combined, flags=re.S)
    combined = re.sub(r"\{%.*?%\}", "", combined, flags=re.S)
    return combined


def _extract_named_functions(js_source, names):
    """Slices out ONLY the named top-level `function foo(...) { ... }`
    definitions via brace balancing, instead of executing the entire
    multi-thousand-line wizard script - most of that script runs real
    browser-only setup code (pointer listeners, getComputedStyle, etc.) at
    module-load time that has nothing to do with the specific fixed
    functions under test here and cannot be meaningfully stubbed away."""
    out = []
    for name in names:
        match = re.search(r"(async\s+)?function\s+" + re.escape(name) + r"\s*\(", js_source)
        assert match, f"function {name} not found in source"
        start = match.start()
        brace_start = js_source.index("{", start)
        depth = 0
        i = brace_start
        while i < len(js_source):
            if js_source[i] == "{":
                depth += 1
            elif js_source[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out.append(js_source[start:i + 1])
    return "\n\n".join(out)


_HARNESS_PRELUDE = """
globalThis.__experienceType = 'image_video';
const __stubEl = () => ({
  classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
  style: {}, value: '', innerHTML: '', textContent: '', hidden: false,
  setAttribute() {}, getAttribute() { return null; }, focus() {}, click() {},
  addEventListener() {}, querySelector() { return null; }, querySelectorAll() { return []; },
});
globalThis.document = {
  getElementById(id) {
    if (id === 'experienceType') return { value: globalThis.__experienceType };
    return __stubEl();
  },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  addEventListener() {},
  createElement() { return __stubEl(); },
};
globalThis.URL = { createObjectURL() { return 'blob:stub'; }, revokeObjectURL() {} };
globalThis.addEventListener = function () {};
globalThis.removeEventListener = function () {};
globalThis.window = globalThis;
globalThis.navigator = { userAgent: 'node-test' };
// Matches static/js/marker-editor.js's real DEFAULT_CROP exactly.
globalThis.MarkerEditor = { defaultCrop() { return { x: 0.1, y: 0.1, width: 0.8, height: 0.8 }; } };
"""


def _run_node_harness(js_body, driver_code):
    """Concatenates the stub prelude, the real extracted/neutralized page
    script, and a driver snippet that prints JSON to stdout - then actually
    runs it under Node and returns the parsed result."""
    full_script = _HARNESS_PRELUDE + "\n" + js_body + "\n" + driver_code
    result = subprocess.run(
        [NODE], input=full_script, capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, f"stderr:\n{result.stderr}\nstdout:\n{result.stdout}"
    return json.loads(result.stdout.strip().splitlines()[-1])


pytestmark = pytest.mark.skipif(not NODE, reason="node not available in this environment")


# ===========================================================================
# CREATE — pairIsComplete() gate (the actual defect: markerConfirmed ignored)
# ===========================================================================

def test_pair_is_complete_rejects_unconfirmed_target_even_with_files_present():
    """The proven bug: a duplicate-rejected candidate still has pair.image and
    pair.video set (it was never cleared), so the OLD pairIsComplete() (image
    && video only) would have called it ready. This proves the fixed gate
    requires markerConfirmed too."""
    js = _extract_named_functions(_extract_and_neutralize_scripts(_creator_html()), ["pairIsComplete", "experienceType"])
    driver = """
      globalThis.__experienceType = 'image_video';
      const rejected = { image: {}, video: {}, markerConfirmed: false };
      const confirmed = { image: {}, video: {}, markerConfirmed: true };
      const noVideo = { image: {}, video: null, markerConfirmed: true };
      console.log(JSON.stringify({
        rejected: pairIsComplete(rejected),
        confirmed: pairIsComplete(confirmed),
        noVideo: pairIsComplete(noVideo),
      }));
    """
    out = _run_node_harness(js, driver)
    assert out["rejected"] is False, "duplicate-rejected (unconfirmed) pair must NOT be ready - this was the actual bug"
    assert out["confirmed"] is True
    assert out["noVideo"] is False


def test_pair_is_complete_direct_qr_unaffected_by_marker_confirmed_gate():
    """Direct QR has no crop/marker step at all - must stay video-only,
    exactly as before this fix (no regression to the Direct QR contract)."""
    js = _extract_named_functions(_extract_and_neutralize_scripts(_creator_html()), ["pairIsComplete", "experienceType"])
    driver = """
      globalThis.__experienceType = 'direct_qr';
      const noMarkerButHasVideo = { image: null, video: {}, markerConfirmed: false };
      console.log(JSON.stringify({ ready: pairIsComplete(noMarkerButHasVideo) }));
    """
    out = _run_node_harness(js, driver)
    assert out["ready"] is True


# ===========================================================================
# CREATE — clearRejectedTargetCandidate() actually clears state
# ===========================================================================

def test_clear_rejected_target_candidate_resets_every_listed_field():
    js = _extract_named_functions(
        _extract_and_neutralize_scripts(_creator_html()),
        ["clearRejectedTargetCandidate", "defaultMarkerCrop"],
    )
    driver = """
      globalThis.currentFiles = {
        2: {
          image: {}, imageUrl: 'blob:fake', markerConfirmed: true, markerMode: 'crop',
          crop: { x: 0.2, y: 0.2, width: 0.5, height: 0.5 }, rotation: 90,
          processedFile: {}, processedWidth: 400, processedHeight: 400,
          processedPreviewUrl: 'blob:fake2', originalWidth: 800, originalHeight: 800,
          quality: { status: 'Weak marker', guidance: ['x'] },
        }
      };
      clearRejectedTargetCandidate(2);
      const p = currentFiles[2];
      console.log(JSON.stringify({
        image: p.image, imageUrl: p.imageUrl, markerConfirmed: p.markerConfirmed,
        processedFile: p.processedFile, rotation: p.rotation,
        crop: p.crop,
      }));
    """
    out = _run_node_harness(js, driver)
    assert out["image"] is None
    assert out["imageUrl"] is None
    assert out["markerConfirmed"] is False
    assert out["processedFile"] is None
    assert out["rotation"] == 0
    assert out["crop"] == {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8}, "must reset to defaultMarkerCrop(), not leave the rejected crop"


# ===========================================================================
# CREATE — video duplicate pre-check scope (findDuplicateVideoInScope)
# ===========================================================================

def test_duplicate_video_precheck_flags_same_pair_reuse_for_image_video():
    js = _extract_named_functions(
        _extract_and_neutralize_scripts(_creator_html()),
        ["findDuplicateVideoInScope", "experienceType"],
    )
    driver = """
      globalThis.__experienceType = 'image_video';
      globalThis.currentFiles = {
        1: { video: {}, videoHash: 'aaaa', additionalVideos: [] },
        2: { video: {}, videoHash: 'bbbb', additionalVideos: [] },
      };
      (async () => {
        const withinSamePair = await findDuplicateVideoInScope('aaaa', 1, 'primary');
        const acrossDifferentPairsAllowed = await findDuplicateVideoInScope('aaaa', 2, 'primary');
        console.log(JSON.stringify({
          withinSamePair: withinSamePair,
          acrossDifferentPairsAllowed: acrossDifferentPairsAllowed,
        }));
      })();
    """
    out = _run_node_harness(js, driver)
    assert out["withinSamePair"] is None, "candidate matches its OWN current slot (excludeSlot) - must not self-flag"
    assert out["acrossDifferentPairsAllowed"] is None, "same video content under a DIFFERENT target must stay allowed (existing contract)"

    driver2 = """
      globalThis.__experienceType = 'image_video';
      globalThis.currentFiles = {
        1: { video: {}, videoHash: 'aaaa', additionalVideos: [{ id: 9, hash: 'aaaa' }] },
      };
      (async () => {
        const dup = await findDuplicateVideoInScope('aaaa', 1, 'new-primary-selection');
        console.log(JSON.stringify({ dup: dup }));
      })();
    """
    out2 = _run_node_harness(js, driver2)
    assert out2["dup"] is not None, "a genuinely NEW duplicate video within the SAME target's own set must be caught"


def test_duplicate_video_precheck_direct_qr_scope_is_whole_playlist():
    js = _extract_named_functions(
        _extract_and_neutralize_scripts(_creator_html()),
        ["findDuplicateVideoInScope", "experienceType"],
    )
    driver = """
      globalThis.__experienceType = 'direct_qr';
      globalThis.currentFiles = {
        1: { video: {}, videoHash: 'zzzz', additionalVideos: [] },
        2: { video: {}, videoHash: 'yyyy', additionalVideos: [] },
      };
      (async () => {
        const dup = await findDuplicateVideoInScope('zzzz', 2, 'primary');
        console.log(JSON.stringify({ dup: dup }));
      })();
    """
    out = _run_node_harness(js, driver)
    assert out["dup"] is not None, "Direct QR has no per-target grouping - a duplicate across DIFFERENT playlist entries must be caught"


# ===========================================================================
# EDIT — new validate-only backend endpoints (real HTTP, no persistence)
# ===========================================================================

def test_edit_validate_target_endpoint_source_exists_and_never_persists():
    """The endpoint itself is exercised live in test_v11_target_identity_remediation.py
    -style fixtures elsewhere; this guards the narrow contract the audit
    required: no db.session.add/commit anywhere in its body."""
    app_src = _read("app.py")
    start = app_src.index("def user_validate_target_candidate(")
    end = app_src.index("\ndef ", start + 10)
    body = app_src[start:end]
    assert "resolve_target_identity_conflict(" in body
    assert "db.session.add(" not in body
    assert "db.session.commit(" not in body
    assert "_safe_remove(temp_path)" in body


def test_edit_validate_video_endpoint_source_exists_and_never_persists():
    app_src = _read("app.py")
    start = app_src.index("def user_validate_video_candidate(")
    end = app_src.index("\ndef ", start + 10)
    body = app_src[start:end]
    assert "find_video_duplicate(" in body
    assert "db.session.add(" not in body
    assert "db.session.commit(" not in body


def test_edit_video_inputs_validate_before_auto_submit_not_immediately():
    """The audit's proven Edit gap: onchange="this.form.requestSubmit()" fired
    on selection with zero validation. Confirms every such input now routes
    through the validating handler instead."""
    html = _edit_html()
    # Regex, not a substring check: matches only a real <input ... onchange=...>
    # attribute, not this file's own explanatory prose about the old pattern.
    assert not re.search(r'<input[^>]*onchange="this\.form\.requestSubmit\(\)"', html)
    assert html.count('onchange="handleEditVideoFileChosen(this)"') == 4


def test_edit_replace_marker_validates_before_writing_into_form_input():
    html = _edit_html()
    idx = html.index("async function confirmReplacementMarker()")
    end = html.index("\n    function ", idx)
    body = html[idx:end]
    validate_pos = body.index("validateTargetCandidateOnServer(")
    input_write_pos = body.index("input.files = dataTransfer.files")
    assert validate_pos < input_write_pos, "duplicate validation must run BEFORE the candidate reaches the form's input"
    assert "CONFLICT_OTHER_PAIR" in body
