import os
import tempfile
from io import BytesIO
from pathlib import Path
import re

import cv2
import numpy as np
import pytest
from PIL import Image
from werkzeug.security import generate_password_hash


class NoopThread:
    def __init__(self, target=None, args=(), kwargs=None, **_ignored):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        return None


def _jpeg_bytes(width=640, height=480, color=(160, 80, 40)):
    out = BytesIO()
    Image.new("RGB", (width, height), color).save(out, format="JPEG", quality=88)
    out.seek(0)
    return out


_MP4_BYTES = None


def _mp4_bytes():
    """Smallest deterministic valid MP4 this test environment can produce
    (cv2's bundled MP4 backend works with no system ffmpeg/ffprobe CLI)."""
    global _MP4_BYTES
    if _MP4_BYTES is None:
        fd, path = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        try:
            writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (64, 64))
            for _ in range(5):
                writer.write(np.zeros((64, 64, 3), dtype=np.uint8))
            writer.release()
            with open(path, "rb") as fh:
                _MP4_BYTES = fh.read()
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
    return BytesIO(_MP4_BYTES)


def _upload_data(name="Marker Project", modes=("crop",), widths=(640,)):
    data = {"name": name, "upload_id": f"upload-{name}"}
    images = []
    videos = []
    for index, mode in enumerate(modes):
        width = widths[index] if index < len(widths) else widths[-1]
        images.append((_jpeg_bytes(width, 480), f"marker-{index}.jpg"))
        videos.append((_mp4_bytes(), f"clip-{index}.mp4"))
        data[f"marker_{index}_mode"] = mode
        data[f"marker_{index}_crop_x"] = "0.1" if mode == "crop" else "0"
        data[f"marker_{index}_crop_y"] = "0.2" if mode == "crop" else "0"
        data[f"marker_{index}_crop_width"] = "0.5" if mode == "crop" else "1"
        data[f"marker_{index}_crop_height"] = "0.4" if mode == "crop" else "1"
        data[f"marker_{index}_rotation"] = "90" if index == 0 else "0"
        data[f"marker_{index}_original_width"] = str(width)
        data[f"marker_{index}_original_height"] = "480"
        data[f"marker_{index}_processed_width"] = "520"
        data[f"marker_{index}_processed_height"] = "420"
        data[f"marker_{index}_source_size_bytes"] = "1000000"
        data[f"marker_{index}_processed_size_bytes"] = "120000"
        data[f"marker_{index}_display_orientation"] = "landscape" if width > 480 else "portrait"
    data["images"] = images
    data["videos"] = videos
    return data


def _patch_upload_processing(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "standardize_uploaded_image", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "make_feature_working_jpeg", lambda *args, **kwargs: Path(args[1]).write_bytes(b"work"))
    monkeypatch.setattr(app_module, "extract_features_multi", lambda *args, **kwargs: Path(args[1]).write_bytes(b"npz"))
    monkeypatch.setattr(app_module, "generate_custom_qr", lambda *args, **kwargs: False)
    monkeypatch.setattr(app_module, "generate_basic_qr", lambda *args, **kwargs: Path(args[3]).write_bytes(b"qr") if len(args) > 3 else None)
    monkeypatch.setattr(app_module.threading, "Thread", NoopThread)


def test_create_project_ui_defaults_to_crop_and_allows_full_image():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    assert "markerMode: 'crop'" in html
    assert "Crop marker area" in html
    assert "Use full image as marker" in html
    assert "This area will become the marker" in html
    assert "CLIENT PREP START" in html
    assert "CLIENT IMAGE COMPRESS DONE" in html
    assert "CLIENT UPLOAD PROGRESS" in html
    assert "video-meta-${pairId}" in html
    assert "VIDEO_WARNING_CONFIG" in html
    assert "VIDEO CLIENT SELECTED" in html
    assert "VIDEO METADATA READY" in html
    assert "VIDEO CLIENT UPLOAD START" in html
    assert "VIDEO CLIENT PROGRESS" in html
    assert "VIDEO CLIENT UPLOAD RESPONSE" in html
    assert "UPLOAD CLIENT SUBMIT" in html
    assert "UPLOAD CLIENT VALIDATION DONE" in html
    assert "UPLOAD CLIENT FORM DATA READY" in html
    assert "UPLOAD CLIENT REQUEST OPEN" in html
    assert "UPLOAD CLIENT FIRST PROGRESS" in html
    assert "UPLOAD CLIENT TRANSFER COMPLETE" in html
    assert "UPLOAD CLIENT SERVER RESPONSE" in html
    assert "UPLOAD CLIENT COMPLETE" in html
    assert "Metadata unavailable" in html
    assert "Large file" in html
    assert "Very large file" in html
    assert "Unsupported type" in html
    assert "uploadActive = true" in html
    assert "if (uploadActive) return" in html
    assert "estimated_remaining_seconds" in html
    assert "marker_${index}_crop_x" in html
    assert "MAX_PAIRS_PER_PROJECT = (IS_ADMIN || IS_DEV_TEST) ? Infinity" in html


def test_upload_phase_labels_are_visible_and_sequential():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    for label in (
        "Preparing project",
        "Preparing marker",
        "Uploading video",
        "Saving project",
        "Processing marker",
        "Project ready",
    ):
        assert label in html
    assert "setUploadProgress('Processing marker...', 98" in html
    assert "setUploadProgress('Saving project...', 92" in html
    assert "setUploadProgress('Project ready', 100" in html


def test_real_upload_progress_calculates_percentage_speed_and_eta():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    progress_start = html.index("xhr.upload.onprogress")
    progress_block = html[progress_start:html.index("xhr.upload.onload", progress_start)]
    assert "if (!event.lengthComputable) return" in progress_block
    assert "const percent = event.total ? (event.loaded / event.total) * 100 : 0" in progress_block
    assert "const avgBytesPerSecond = event.loaded / elapsedSeconds" in progress_block
    assert "const etaSeconds = enoughForEta ? remainingBytes / avgBytesPerSecond : null" in progress_block
    assert "UPLOAD CLIENT FIRST PROGRESS" in progress_block
    assert "UPLOAD CLIENT PROGRESS" in progress_block
    assert "Uploading video — ${Math.round(percent)}%" in progress_block
    assert "${formatBytes(event.loaded)} of ${formatBytes(event.total)}" in progress_block
    assert "${formatRate(avgBytesPerSecond)} · ${etaText}" in progress_block


def test_upload_never_shows_fake_100_before_server_completion():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    assert "setUploadProgress('Uploaded. Processing...', 100)" not in html
    assert "Math.min(90, 35 + (event.loaded / event.total) * 55)" in html
    assert "setUploadProgress('Processing marker...', 98" in html


def test_duplicate_submit_prevention_and_one_active_request():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    assert "let uploadActive = false" in html
    assert "let activeUploadXhr = null" in html
    assert "let activeUploadId = null" in html
    assert "if (uploadActive) return" in html
    assert "uploadActive = true" in html
    assert "activeUploadXhr = xhr" in html
    assert "activeUploadId = uploadId" in html


def test_upload_failure_and_abort_restore_controls():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    assert "function resetUploadSubmit(submitBtn)" in html
    assert "xhr.onerror = () =>" in html
    assert "xhr.onabort = () =>" in html
    assert "UPLOAD CLIENT FAILED" in html
    assert "UPLOAD CLIENT ABORTED" in html
    assert "resetUploadSubmit(submitBtn)" in html
    assert "submitBtn.innerHTML = '<i class=\"fas fa-qrcode\"></i> Create ScanStory'" in html


def test_beforeunload_only_active_during_upload_and_removed_after_completion():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    assert "function enableUploadNavigationWarning()" in html
    assert "function disableUploadNavigationWarning()" in html
    assert "function markResumableUploadCompleteForRedirect()" in html
    assert "window.addEventListener('beforeunload', warnDuringUpload)" in html
    assert "window.removeEventListener('beforeunload', warnDuringUpload)" in html
    assert "if (!uploadActive || (!activeUploadXhr && !activeResumableUpload)) return" in html
    assert "enableUploadNavigationWarning();" in html
    assert "disableUploadNavigationWarning();" in html


def test_resumable_success_clears_beforeunload_state_before_redirect():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    helper_start = html.index("function markResumableUploadCompleteForRedirect()")
    helper = html[helper_start:html.index("function formatRate", helper_start)]
    assert "uploadActive = false" in helper
    assert "activeResumableUpload = null" in helper
    assert "activeUploadId = null" in helper
    assert "setCancelUploadVisible(false)" in helper
    assert "disableUploadNavigationWarning();" in helper

    finalize_start = html.index("let finalized = await finalizeResumableWithBoundedRetry")
    success_block = html[finalize_start:html.index("} catch (err)", finalize_start)]
    redirect_index = success_block.index("window.location.href = finalSession.project_id")
    cleanup_index = success_block.index("markResumableUploadCompleteForRedirect();")
    assert cleanup_index < redirect_index
    assert "clearResumableUploadState();" in success_block


def test_resumable_active_upload_keeps_navigation_protection():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    start = html.index("async function submitResumableSinglePair")
    block = html[start:html.index("let uploadActive = false", start)]
    assert "activeResumableUpload = { controller, cancelled: false, sessionId: null, projectName }" in block
    assert "setCancelUploadVisible(true)" in block
    assert "enableUploadNavigationWarning();" in block
    assert "if (!uploadActive || (!activeUploadXhr && !activeResumableUpload)) return" in html


def test_cancel_and_clear_all_leave_navigation_warning_state_safe():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    cancel_start = html.index("async function cancelActiveUpload")
    cancel_block = html[cancel_start:html.index("async function uploadResumableStream", cancel_start)]
    assert "clearResumableUploadState()" in cancel_block
    assert "resetUploadSubmit(document.getElementById('submitBtn'))" in cancel_block

    reset_start = html.index("function resetUploadSubmit(submitBtn)")
    reset_block = html[reset_start:html.index("function enableUploadNavigationWarning", reset_start)]
    assert "activeResumableUpload = null" in reset_block
    assert "disableUploadNavigationWarning();" in reset_block

    clear_start = html.index("function clearAll()")
    clear_block = html[clear_start:html.index("function logClientStage", clear_start)]
    assert "if (!uploadActive)" in clear_block
    assert "disableUploadNavigationWarning();" in clear_block


def test_success_redirect_happens_once():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    success_start = html.index("if (xhr.status >= 200 && xhr.status < 400)")
    success_block = html[success_start:html.index("} else {", success_start)]
    assert "if (!uploadRedirected)" in success_block
    assert "uploadRedirected = true" in success_block
    assert "UPLOAD CLIENT REDIRECT" in success_block
    assert "window.location.href = xhr.responseURL || '/dashboard'" in success_block


def test_production_does_not_emit_upload_client_diagnostics():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    app_source = Path("app.py").read_text(encoding="utf-8", errors="ignore")
    assert "const UPLOAD_CLIENT_DEBUG_ENABLED = {{ 'true' if upload_debug_enabled else 'false' }}" in html
    assert "if (uploadLog && !UPLOAD_CLIENT_DEBUG_ENABLED) return" in html
    assert "upload_debug_enabled=(app.config.get(\"TESTING\") or os.environ.get(\"FLASK_ENV\") == \"development\")" in app_source


def test_dedicated_upload_progress_container_is_above_submit_button():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    progress_markup = """
          <div class="upload-progress" id="uploadProgress" aria-live="polite" aria-atomic="true">
            <div class="upload-progress-phase" id="uploadProgressText">Preparing upload...</div>
            <div class="upload-progress-amount" id="uploadProgressAmount"></div>
            <div class="upload-progress-detail" id="uploadProgressDetail"></div>
            <div class="upload-progress-bar" aria-hidden="true"><span id="uploadProgressFill"></span></div>
          </div>
"""
    assert progress_markup in html
    right_panel_start = html.index('<div class="panel">\n          <div class="panel-title"><i class="fas fa-eye"></i> Story Preview</div>')
    right_panel = html[right_panel_start:html.index("</form>", right_panel_start)]
    assert right_panel.index('id="uploadProgress"') < right_panel.index('id="submitBtn"')
    assert right_panel.index('id="uploadProgress"') < right_panel.index('<div class="action-buttons">')
    assert "progress.scrollIntoView({ block: 'nearest', inline: 'nearest' })" in html


def test_upload_progress_dom_update_logs_visibility_in_development():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    assert "function uploadProgressDomSnapshot(selector = '#uploadProgress')" in html
    assert "console.log('[UPLOAD CLIENT DOM UPDATE]'" in html
    assert "console.log('[UPLOAD CLIENT PROGRESS EVENT]'" in html
    assert "phase_text: document.querySelector('#uploadProgressText')?.textContent || ''" in html
    assert "amount_text: document.querySelector('#uploadProgressAmount')?.textContent || ''" in html
    assert "detail_text: document.querySelector('#uploadProgressDetail')?.textContent || ''" in html
    assert "in_viewport: rect.top < window.innerHeight && rect.bottom > 0" in html


def test_upload_client_diagnostics_use_safe_metrics_only():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    start = html.index("function uploadClientLog(stage, state, extra = {})")
    block = html[start:html.index("function resetUploadSubmit", start)]
    for key in (
        "upload_id",
        "pair_count",
        "video_size_bytes",
        "bytes_uploaded",
        "percentage",
        "elapsed_ms",
        "estimated_upload_speed",
        "estimated_remaining_time",
        "http_status",
        "current_phase",
    ):
        assert key in block


def test_resumable_upload_frontend_uses_documented_routes_and_csrf():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    # Phase 2 added a trailing `purpose` argument so a multi-content-set
    # session can be marked as one at creation; the four facts this ever cared
    # about are the files, the project name and the experience payload.
    assert "createResumableSession(markerFile, videoFile, projectName, experiencePayload, signal" in html
    assert "fetchUploadJson('/api/uploads/sessions'" in html
    assert "...experiencePayload" in html
    assert "`/api/uploads/sessions/${sessionId}/chunk`" in html
    assert "`/api/uploads/sessions/${sessionId}`" in html
    assert "`/api/uploads/sessions/${sessionId}/finalize`" in html
    assert "`/api/uploads/sessions/${sessionId}/cancel`" in html
    # The multi-content-set project finalize is the one new documented route.
    assert "fetchUploadJson('/api/uploads/projects/finalize'" in html
    assert "'X-CSRFToken': csrfHeader()" in html
    assert "'X-Chunk-Offset': String(offset)" in html


def test_every_pair_count_uploads_resumably_and_legacy_multipart_is_browser_fallback_only():
    """V1.1 Phase 2 re-point. This used to assert the opposite - that a
    multi-pair project deliberately fell back to the monolithic multipart
    uploader. That was the largest remaining low-bandwidth gap (a two-set
    project on a weak link was all-or-nothing), and Phase 2 closed it, so the
    guard now pins the architecture that replaced it: one content set or many,
    the resumable path handles it, and the multipart XHR survives ONLY for a
    browser that cannot run the resumable path at all.
    """
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    dispatch_start = html.index("if (resumableUploadSupported())")
    dispatch = html[dispatch_start:html.index("// Last resort only", dispatch_start)]
    assert "if (readyPairs.length === 1)" in dispatch
    assert "await submitResumableSinglePair" in dispatch
    assert "await submitResumableMultiPair" in dispatch
    # The fallback is reachable, gated on capability rather than pair count,
    # and no longer describes itself as the multi-pair uploader.
    assert "Using the legacy uploader for multi-pair projects." not in html
    assert "Using the direct uploader for this browser." in html
    assert "xhr.open('POST', this.action, true)" in html
    support = html[html.index("function resumableUploadSupported()"):html.index("async function finalizeResumableProject(")]
    for capability in ("fetch", "AbortController", "Blob.prototype.slice"):
        assert capability in support


def test_multi_content_set_upload_is_one_session_per_set_and_one_atomic_finalize():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    start = html.index("async function submitResumableMultiPair")
    block = html[start:html.index("    let uploadActive = false;", start)]
    # One session per content set, created/recovered before any byte is sent.
    assert "resolveContentSetSession(" in block
    # A set the server already has is SKIPPED, never re-sent.
    assert "if (record.complete)" in block
    assert "RESUMABLE CLIENT SET SKIPPED" in block
    # The chunk loop is the single-pair one, reused rather than reimplemented.
    assert "await uploadResumableStream(" in block
    # Exactly one project finalize for the whole group.
    assert "finalizeResumableProjectWithBoundedRetry(sessionIds" in block
    assert "session_ids: sessionIds" in html
    # Server state wins for every set, not just the active one.
    assert "err.code === 'INCOMPLETE_UPLOAD'" in block
    # Per-set failure names the set and preserves the others.
    assert "failed_set_index" in block


def test_resumable_upload_streams_marker_then_video_sequentially():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    start = html.index("function sequentialUploadSlice")
    block = html[start:html.index("async function fetchUploadJson", start)]
    assert "const markerSize = markerFile?.size || 0" in block
    assert "const markerEnd = markerSize" in block
    assert "markerFile.slice(offset, end)" in block
    assert "videoFile.slice(offset - markerEnd, end - markerEnd)" in block
    assert "markerFile.slice(offset, markerEnd)" in block
    assert "videoFile.slice(0, end - markerEnd)" in block
    # Chunk size is measured, not fixed (V1.1 extreme-low-bandwidth
    # hardening), so what matters here is that the slice length comes from
    # the adaptive size and stays inside the floor/ceiling the sizer honours.
    assert "sequentialUploadSlice(markerFile, videoFile, offset, chunkBytes)" in html
    assert "RESUMABLE_CHUNK_MIN_BYTES = 128 * 1024" in html
    assert "RESUMABLE_CHUNK_MAX_BYTES = 5 * 1024 * 1024" in html
    assert "initialChunkBytes(serverMaxBytes)" in html


def test_resumable_upload_progress_bytes_percent_speed_eta_are_visible():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    start = html.index("async function uploadResumableStream")
    block = html[start:html.index("async function finalizeResumableWithBoundedRetry", start)]
    assert "`Uploading video — ${Math.round(percent)}%`" in block
    assert "`${formatBytes(offset)} of ${formatBytes(expectedTotal)}`" in block
    # The rate quoted to the creator is the SMOOTHED one: a link that just
    # recovered must stop quoting the stall it recovered from.
    assert "`${formatRate(smoothed)} · ${etaText}`" in block
    assert "setUploadProgress('Finalizing upload'" in html
    # Creator-facing phase copy must not expose the internal pipeline vocabulary
    # ("queue"/"queued"/"worker"/"RQ"). This phase is the same phase it always
    # was - the same 98%, the same 'processing' state, the same call site - it is
    # now named in the product's own words.
    assert "setUploadProgress('Preparing your ScanStory'" in html
    assert "Processing queued" not in html
    assert "setUploadProgress('Project ready'" in html


def test_resumable_upload_resume_and_offset_mismatch_sync_to_server_offset():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    start = html.index("async function uploadResumableStream")
    block = html[start:html.index("async function finalizeResumableWithBoundedRetry", start)]
    # Failures are now CLASSIFIED rather than pattern-matched inline; the
    # classifier itself is exercised for real in
    # tests/integration/test_extreme_low_bandwidth_upload.py. What this
    # guards is that the stream loop still routes a mismatch back to the
    # server's authoritative offset and never to a local counter.
    assert "uploadRetryDecision(err, attempt)" in block
    assert "decision.action === 'resync'" in block
    assert "err.payload?.current_offset" in block
    assert "getUploadSessionStatus(sessionId" in block
    assert ".session.current_offset" in block
    assert "RESUMABLE CLIENT RESUME" in block
    assert "response.note === 'duplicate_chunk_ignored'" in block
    # A spent retry budget must pause, never destroy the session.
    assert "decision.action === 'pause'" in block
    assert "UPLOAD_PAUSED_NETWORK" in block


def test_resumable_upload_cancellation_aborts_and_calls_cancel_route():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    start = html.index("async function cancelActiveUpload")
    block = html[start:html.index("async function uploadResumableStream", start)]
    assert "activeResumableUpload.cancelled = true" in block
    assert "activeResumableUpload.controller?.abort()" in block
    assert "await cancelResumableSession(sessionId)" in block
    assert "clearResumableUploadState()" in block
    assert "id=\"cancelUploadBtn\"" in html


def test_resumable_upload_finalize_once_and_handles_incomplete_queue_failures():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    start = html.index("async function finalizeResumableWithBoundedRetry")
    block = html[start:html.index("async function submitResumableSinglePair", start)]
    assert "if (activeResumableUpload.finalizeStarted)" in block
    assert "activeResumableUpload.finalizeStarted = true" in block
    assert "err.code === 'ALREADY_FINALIZED'" in block
    assert "reconcileUploadSessionFromServer(sessionId" in block
    assert "normalizedCompletedUploadPayload(recovered)" in block
    assert "err.code === 'INCOMPLETE_UPLOAD'" in block
    assert "recovered.session?.status === 'assembled'" in block
    assert "err.code !== 'QUEUE_ENQUEUE_FAILED'" in block
    assert "UPLOAD CLIENT FINALIZE RETRY" in block


def test_resumable_upload_refresh_recovery_uses_authoritative_session_status():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    start = html.index("if (storedSessionMatchesFiles")
    block = html[start:html.index("if (!sessionPayload)", start)]
    assert "getUploadSessionStatus(stored.sessionId, controller.signal)" in block
    assert "sessionPayload.session?.status === 'completed' && sessionPayload.session.project_id" in block
    assert "window.location.href = `/success/${sessionPayload.session.project_id}`" in block
    assert "sessionPayload.session?.status === 'assembled'" in block
    assert "sessionPayload.session?.status === 'active'" in block
    # Terminality comes from the server's own is_terminal flag (see _upload_session_payload
    # in app.py), not a duplicated client-side status set that nothing kept in step with it.
    assert "sessionPayload.session?.is_terminal" in block
    assert "RESUMABLE_TERMINAL_STATUSES" not in html
    assert "clearResumableUploadState()" in block


def test_resumable_upload_processing_status_polling_is_bounded_and_optional():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    start = html.index("async function pollProcessingJobIfAvailable")
    block = html[start:html.index("async function cancelActiveUpload", start)]
    assert "if (!jobId) return null" in block
    assert "RESUMABLE_POLL_MAX_ATTEMPTS" in block
    assert "`/api/processing/jobs/${jobId}`" in block
    assert "status === 'completed' || status === 'failed'" in block


def test_resumable_upload_refresh_recovery_stores_only_non_secret_metadata():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    start = html.index("function saveResumableUploadState")
    block = html[start:html.index("function loadResumableUploadState", start)]
    for key in ("sessionId", "projectName", "imageName", "imageSize", "videoName", "videoSize", "expectedTotalSize", "currentOffset"):
        assert key in block
    assert "localStorage.setItem(RESUMABLE_UPLOAD_STORAGE_KEY" in block
    assert "fileBytes" not in block
    assert "markerFile" not in block
    assert "videoFile" not in block
    assert "storedSessionMatchesFiles" in html


def test_resumable_upload_error_messages_are_documented_safe_codes():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    start = html.index("function safeUploadErrorMessage")
    block = html[start:html.index("function setCancelUploadVisible", start)]
    for code in (
        "OFFSET_MISMATCH",
        "INCOMPLETE_UPLOAD",
        "SESSION_EXPIRED",
        "SESSION_CANCELLED",
        "QUEUE_ENQUEUE_FAILED",
        "IMAGE_VALIDATION_FAILED",
        "VIDEO_VALIDATION_FAILED",
        "PROJECT_LIMIT_REACHED",
    ):
        assert code in block
    assert "stack" not in block.lower()
    assert "traceback" not in block.lower()
    assert "user_agent" not in block
    assert "filename" not in block
    assert "email" not in block
    assert "token" not in block


def test_new_image_initializes_visible_crop_after_dimensions_load():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    assert "const DEFAULT_MARKER_CROP = Object.freeze({ x: 0.1, y: 0.1, width: 0.8, height: 0.8 })" in html
    assert "function defaultMarkerCrop()" in html
    assert "const image = await loadImageForPair(pairId)" in html
    assert "sanitizeCrop(currentFiles[pairId], image, true)" in html
    assert "await openCropModal(pairId)" in html


def test_replacing_image_resets_invalid_prior_crop_and_dimensions():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    assert "currentFiles[pairId].crop = defaultMarkerCrop()" in html
    assert "currentFiles[pairId].rotation = 0" in html
    assert "currentFiles[pairId].originalWidth = 0" in html
    assert "currentFiles[pairId].originalHeight = 0" in html
    assert "currentFiles[pairId].processedWidth = 0" in html
    assert "currentFiles[pairId].processedHeight = 0" in html


def test_minimum_crop_dimensions_are_enforced_client_side():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    assert "const MIN_MARKER_CROP_FRACTION = 0.08" in html
    assert "const minWidth = Math.min(0.5, Math.max(MIN_MARKER_CROP_FRACTION, 1 / naturalWidth))" in html
    assert "const minHeight = Math.min(0.5, Math.max(MIN_MARKER_CROP_FRACTION, 1 / naturalHeight))" in html
    assert "width < minWidth || height < minHeight" in html
    assert "width = Math.min(1, Math.max(minWidth, width))" in html
    assert "height = Math.min(1, Math.max(minHeight, height))" in html


def test_rotation_keeps_crop_valid():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    rotate_start = html.index("function rotateCropImage()")
    rotate_block = html[rotate_start:html.index("function cropDrawRect()", rotate_start)]
    assert "pair.rotation = (pair.rotation + 90) % 360" in rotate_block
    assert "sanitizeCrop(pair, activeCropImage)" in rotate_block


def test_zero_size_canvas_or_image_layout_does_not_persist_invalid_crop():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    assert "if (!image.naturalWidth || !image.naturalHeight || !canvas.width || !canvas.height)" in html
    assert "reason: 'missing image or canvas dimensions'" in html
    assert "if (!Number.isFinite(scale) || scale <= 0)" in html
    assert "reason: 'invalid display scale'" in html
    assert "if (!rect.width || !rect.height)" in html
    assert "reason: 'zero canvas bounding rect'" in html
    assert "if (!point) return" in html


def test_mobile_touch_interaction_cannot_collapse_crop_to_point():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    assert "addEventListener('pointerdown'" in html
    assert "addEventListener('pointermove'" in html
    assert "const minSize = MIN_MARKER_CROP_FRACTION" in html
    assert "x2 = Math.max(x1 + minSize, Math.min(x2, 1))" in html
    assert "y2 = Math.max(y1 + minSize, Math.min(y2, 1))" in html
    assert "sanitizeCrop(pair, activeCropImage)" in html


def test_desktop_move_drag_changes_crop_xy():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    start = html.index("if (cropDrag.mode === 'move')")
    block = html[start:html.index("} else {", start)]
    assert "cropDrag.start.x + dx" in block
    assert "cropDrag.start.y + dy" in block
    assert "x2 = x1 + width" in block
    assert "y2 = y1 + height" in block


def test_desktop_corner_resize_changes_width_and_height():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    assert "{ name: 'nw', x: r.x, y: r.y }" in html
    assert "{ name: 'se', x: r.x + r.w, y: r.y + r.h }" in html
    assert "if (cropDrag.mode.includes('w')) x1 += dx" in html
    assert "if (cropDrag.mode.includes('s')) y2 += dy" in html


def test_desktop_edge_resize_changes_one_axis():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    assert "{ name: 'e', x: r.x + r.w, y: r.y + r.h / 2 }" in html
    assert "{ name: 'n', x: r.x + r.w / 2, y: r.y }" in html
    assert "if (cropDrag.mode.includes('e')) x2 += dx" in html
    assert "if (cropDrag.mode.includes('n')) y1 += dy" in html


def test_mobile_pointer_drag_uses_unified_pointer_events():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    assert "#cropCanvas" in html and "touch-action: none" in html
    assert "function cropPointer(event)" in html
    assert "event.clientX" in html
    assert "event.clientY" in html
    assert "pointerId: event.pointerId" in html


def test_pointer_capture_survives_leaving_crop_box():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    assert "setPointerCapture(event.pointerId)" in html
    assert "window.addEventListener('pointermove'" in html
    assert "cropDrag.pointerId !== event.pointerId" in html


def test_pointerup_stops_crop_movement():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    assert "addEventListener('pointerup'" in html
    assert "function stopCropDrag(event = null)" in html
    assert "cropDrag = null" in html


def test_pointercancel_cleans_crop_drag_state():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    assert "addEventListener('pointercancel'" in html
    assert "releasePointerCapture(event.pointerId)" in html
    assert "stopCropDrag(event)" in html


def test_sanitization_preserves_valid_drag_updates():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    update_start = html.index("function updateCropFromDrag(point)")
    update_block = html[update_start:html.index("async function updateCropPreview()", update_start)]
    assert "pair.crop = { x: x1, y: y1, width: x2 - x1, height: y2 - y1 }" in update_block
    assert "sanitizeCrop(pair, activeCropImage)" in update_block
    assert "forceDefault" not in update_block
    assert "cropDrag.start.width" in update_block
    assert "cropDrag.start.height" in update_block
    assert not re.search(r"cropDrag\.start\.w(?!idth)", update_block)
    assert not re.search(r"cropDrag\.start\.h(?!eight)", update_block)


def test_crop_drag_stays_within_bounds():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    assert "x1 = Math.max(0, Math.min(x1, 1 - minSize))" in html
    assert "y1 = Math.max(0, Math.min(y1, 1 - minSize))" in html
    assert "x2 = Math.max(x1 + minSize, Math.min(x2, 1))" in html
    assert "y2 = Math.max(y1 + minSize, Math.min(y2, 1))" in html


def test_minimum_size_remains_enforced_during_drag():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    assert "const minSize = MIN_MARKER_CROP_FRACTION" in html
    assert "if (cropDrag.mode.includes('w') && x2 - x1 < minSize) x1 = x2 - minSize" in html
    assert "if (cropDrag.mode.includes('e') && x2 - x1 < minSize) x2 = x1 + minSize" in html
    assert "if (cropDrag.mode.includes('n') && y2 - y1 < minSize) y1 = y2 - minSize" in html
    assert "if (cropDrag.mode.includes('s') && y2 - y1 < minSize) y2 = y1 + minSize" in html


def test_crop_debug_diagnostics_are_development_only_and_visible():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    assert "const CROP_DEBUG_ENABLED = {{ 'true' if crop_debug_enabled else 'false' }}" in html
    assert 'id="cropDebugPanel"' in html
    assert "[CROP INIT]" in html
    assert "[CROP POINTER DOWN]" in html
    assert "[CROP POINTER MOVE]" in html
    assert "[CROP POINTER UP]" in html
    assert "[CROP HIT TEST]" in html
    assert "[CROP UPDATE]" in html
    assert "[CROP BLOCKED]" in html
    assert "canvas_count: document.querySelectorAll('#cropCanvas').length" in html
    assert "document.elementFromPoint" in html


def test_crop_pointer_listeners_attach_to_existing_single_canvas():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    assert html.count('id="cropCanvas"') == 1
    assert "function attachCropPointerListeners()" in html
    assert "cropAttachedCanvas = canvas" in html
    assert "cropAttachedCanvas !== canvas" in html
    assert "attachCropPointerListeners();" in html
    assert "attachCropPointerListeners();" in html[html.index("async function openCropModal"):html.index("function closeCropModal")]


def test_mobile_crop_navbar_has_always_visible_cancel_and_confirm():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    assert ".crop-toolbar {" in html
    assert "position: sticky" in html
    assert "env(safe-area-inset-top" in html
    assert '<span class="mobile-label">Cancel</span>' in html
    assert 'class="crop-btn primary mobile-confirm"' in html
    assert ".crop-toolbar .mobile-confirm" in html
    assert "display: inline-flex" in html


def test_mobile_confirm_does_not_require_scrolling_and_bottom_confirm_hidden_only_mobile():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    mobile_block = html[html.index("@media (max-width: 780px)"):html.index("@keyframes slideIn")]
    assert ".crop-actions > .crop-btn.primary" in mobile_block
    assert "display: none" in mobile_block
    assert ".crop-toolbar .mobile-confirm" in mobile_block
    assert "position: sticky" in mobile_block


def test_bottom_crop_controls_remain_unchanged():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    actions_start = html.index('<div class="crop-actions">')
    actions_block = html[actions_start:html.index("</div>\n    </div>\n  </div>", actions_start)]
    assert 'onclick="resetCrop()">Reset</button>' in actions_block
    assert 'onclick="rotateCropImage()">Rotate 90 degrees</button>' in actions_block
    assert 'onclick="setFullImageMode(activeCropPairId)">Use full image as marker</button>' in actions_block
    assert 'onclick="useCurrentMarker()">Use this marker</button>' in actions_block


def test_desktop_crop_modal_layout_remains_default():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    before_mobile = html[:html.index("@media (max-width: 780px)")]
    assert "grid-template-rows: auto 1fr auto" in before_mobile
    assert ".crop-toolbar,\n    .crop-actions" in before_mobile
    assert ".crop-toolbar .mobile-confirm {\n      display: none;" in before_mobile
    assert '<span class="desktop-label">Close</span>' in html


def test_crop_modal_locks_and_restores_background_scroll():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    assert "let cropScrollLock = null" in html
    assert "function lockBackgroundScroll()" in html
    assert "function unlockBackgroundScroll()" in html
    assert "document.body.style.overflow = 'hidden'" in html
    assert "document.documentElement.style.overflow = 'hidden'" in html
    assert "window.scrollTo(0, cropScrollLock.scrollY)" in html
    assert "lockBackgroundScroll();" in html[html.index("async function openCropModal"):html.index("function closeCropModal")]
    assert "unlockBackgroundScroll();" in html[html.index("function closeCropModal"):html.index("function lockBackgroundScroll")]


def test_crop_debug_panel_requires_query_flag():
    html = Path("templates/user/user_create_project.html").read_text(encoding="utf-8", errors="ignore")
    app_source = Path("app.py").read_text(encoding="utf-8", errors="ignore")
    assert 'request.args.get("crop_debug") == "1"' in app_source
    assert 'id="cropDebugPanel"' in html
    assert ".crop-debug-panel {\n      display: none;" in html
    assert "{% if crop_debug_enabled %} active{% endif %}" in html


def test_browser_level_crop_pointer_drag_when_playwright_available(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api", reason="browser-level crop test requires Playwright")
    html = """
    <!doctype html>
    <meta charset="utf-8">
    <style>
      #cropCanvas { width: 500px; height: 400px; touch-action: none; display:block; }
    </style>
    <canvas id="cropCanvas"></canvas>
    <script>
      let activeCropPairId = 1;
      let activeCropImage = { naturalWidth: 1000, naturalHeight: 800 };
      let cropDrag = null;
      const currentFiles = { 1: { markerMode: 'crop', crop: { x: .1, y: .1, width: .8, height: .8 }, originalWidth: 1000, originalHeight: 800 } };
      const MIN_MARKER_CROP_FRACTION = 0.08;
      const cropCanvas = () => document.getElementById('cropCanvas');
      const canvas = cropCanvas();
      canvas.width = 500;
      canvas.height = 400;
      function sanitizeCrop(pair) {
        const minWidth = MIN_MARKER_CROP_FRACTION;
        const minHeight = MIN_MARKER_CROP_FRACTION;
        let { x, y, width, height } = pair.crop;
        width = Math.min(1, Math.max(minWidth, width));
        height = Math.min(1, Math.max(minHeight, height));
        x = Math.min(Math.max(0, x), 1 - width);
        y = Math.min(Math.max(0, y), 1 - height);
        pair.crop = { x, y, width, height };
      }
      function cropDrawRect() {
        const pair = currentFiles[activeCropPairId];
        sanitizeCrop(pair);
        return { imageX: 0, imageY: 0, imageW: 500, imageH: 400, x: pair.crop.x * 500, y: pair.crop.y * 400, w: pair.crop.width * 500, h: pair.crop.height * 400 };
      }
      function cropHandles(r) {
        return [
          { name: 'nw', x: r.x, y: r.y }, { name: 'n', x: r.x + r.w / 2, y: r.y },
          { name: 'ne', x: r.x + r.w, y: r.y }, { name: 'e', x: r.x + r.w, y: r.y + r.h / 2 },
          { name: 'se', x: r.x + r.w, y: r.y + r.h }, { name: 's', x: r.x + r.w / 2, y: r.y + r.h },
          { name: 'sw', x: r.x, y: r.y + r.h }, { name: 'w', x: r.x, y: r.y + r.h / 2 }
        ];
      }
      function cropPointer(event) {
        const rect = canvas.getBoundingClientRect();
        return { x: (event.clientX - rect.left) * (canvas.width / rect.width), y: (event.clientY - rect.top) * (canvas.height / rect.height) };
      }
      function cropDragMode(point, r) {
        const handle = cropHandles(r).find(h => Math.abs(point.x - h.x) <= 24 && Math.abs(point.y - h.y) <= 24);
        if (handle) return handle.name;
        return point.x >= r.x && point.x <= r.x + r.w && point.y >= r.y && point.y <= r.y + r.h ? 'move' : null;
      }
      function updateCropFromDrag(point) {
        const pair = currentFiles[activeCropPairId];
        const r = cropDrawRect();
        const minSize = MIN_MARKER_CROP_FRACTION;
        let x1 = cropDrag.start.x, y1 = cropDrag.start.y, x2 = cropDrag.start.x + cropDrag.start.width, y2 = cropDrag.start.y + cropDrag.start.height;
        const dx = (point.x - cropDrag.point.x) / r.imageW;
        const dy = (point.y - cropDrag.point.y) / r.imageH;
        if (cropDrag.mode === 'move') {
          const width = Math.max(minSize, cropDrag.start.width);
          const height = Math.max(minSize, cropDrag.start.height);
          x1 = Math.max(0, Math.min(cropDrag.start.x + dx, 1 - width));
          y1 = Math.max(0, Math.min(cropDrag.start.y + dy, 1 - height));
          x2 = x1 + width; y2 = y1 + height;
        } else {
          if (cropDrag.mode.includes('e')) x2 += dx;
          if (cropDrag.mode.includes('s')) y2 += dy;
        }
        x2 = Math.max(x1 + minSize, Math.min(x2, 1));
        y2 = Math.max(y1 + minSize, Math.min(y2, 1));
        pair.crop = { x: x1, y: y1, width: x2 - x1, height: y2 - y1 };
        sanitizeCrop(pair);
      }
      canvas.addEventListener('pointerdown', event => {
        const point = cropPointer(event);
        const mode = cropDragMode(point, cropDrawRect());
        cropDrag = { pointerId: event.pointerId, mode, point, start: { ...currentFiles[1].crop } };
        canvas.setPointerCapture(event.pointerId);
      });
      canvas.addEventListener('pointermove', event => {
        if (!cropDrag || cropDrag.pointerId !== event.pointerId) return;
        updateCropFromDrag(cropPointer(event));
      });
      canvas.addEventListener('pointerup', event => { cropDrag = null; });
      window.__crop = currentFiles[1].crop;
    </script>
    """
    path = tmp_path / "crop-pointer-browser.html"
    path.write_text(html, encoding="utf-8")
    with playwright.sync_playwright() as p:
      browser = p.chromium.launch()
      page = browser.new_page(viewport={"width": 800, "height": 600})
      page.goto(path.as_uri())
      before = page.evaluate("currentFiles[1].crop")
      page.mouse.move(250, 200)
      page.mouse.down()
      page.mouse.move(300, 240)
      page.mouse.up()
      after_move = page.evaluate("currentFiles[1].crop")
      page.mouse.move(450, 360)
      page.mouse.down()
      page.mouse.move(490, 390)
      page.mouse.up()
      after_resize = page.evaluate("currentFiles[1].crop")
      browser.close()
    assert after_move["x"] > before["x"]
    assert after_move["y"] > before["y"]
    assert after_resize["width"] >= after_move["width"]
    assert after_resize["height"] >= after_move["height"]


def test_marker_mode_is_stored_per_pair_and_mixed_modes_work(client, app_module, login_user, monkeypatch):
    _patch_upload_processing(app_module, monkeypatch)

    response = client.post(
        "/upload",
        data=_upload_data(modes=("crop", "full_image"), widths=(480, 800)),
        content_type="multipart/form-data",
        follow_redirects=False,
    )

    assert response.status_code == 302
    pairs = app_module.ProjectPair.query.order_by(app_module.ProjectPair.pair_index).all()
    assert [pair.marker_mode for pair in pairs] == ["crop", "full_image"]
    assert pairs[0].marker_crop_x == 0.1
    assert pairs[0].marker_crop_y == 0.2
    assert pairs[0].marker_crop_width == 0.5
    assert pairs[0].marker_crop_height == 0.4
    assert pairs[0].marker_rotation == 90
    assert pairs[0].marker_original_width == 480
    assert pairs[0].marker_processed_width == 520
    assert pairs[0].marker_processed_size_bytes == 120000
    assert pairs[1].marker_crop_x == 0
    assert pairs[1].marker_crop_y == 0
    assert pairs[1].marker_crop_width == 1
    assert pairs[1].marker_crop_height == 1


def test_standard_upload_persists_image_video_detect_once(client, app_module, login_user, monkeypatch):
    _patch_upload_processing(app_module, monkeypatch)
    data = _upload_data(name="detect-once", modes=("crop",))
    data["experience_type"] = "image_video"
    data["playback_mode"] = "detect_once"

    response = client.post("/upload", data=data, content_type="multipart/form-data", follow_redirects=False)

    assert response.status_code == 302
    project = app_module.Project.query.one()
    assert project.experience_type == "image_video"
    assert project.playback_mode == "detect_once"


def test_standard_upload_persists_direct_qr_without_marker_image(client, app_module, login_user, monkeypatch):
    _patch_upload_processing(app_module, monkeypatch)
    response = client.post(
        "/upload",
        data={
            "name": "Direct QR",
            "upload_id": "upload-direct",
            "experience_type": "direct_qr",
            "playback_mode": "direct",
            "videos": [(_mp4_bytes(), "direct.mp4")],
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )

    assert response.status_code == 302
    project = app_module.Project.query.one()
    pair = app_module.ProjectPair.query.one()
    assert project.experience_type == "direct_qr"
    assert project.playback_mode == "direct"
    assert pair.image_filename is None
    assert pair.is_processed is True
    assert pair.feature_extraction_status == "not_required"


@pytest.mark.parametrize(
    ("experience_type", "playback_mode"),
    [
        ("direct_qr", "tracked_overlay"),
        ("direct_qr", "detect_once"),
        ("image_video", "direct"),
    ],
)
def test_standard_upload_rejects_invalid_experience_playback_mapping(
    client, app_module, login_user, monkeypatch, experience_type, playback_mode
):
    _patch_upload_processing(app_module, monkeypatch)
    data = _upload_data(name=f"bad-{experience_type}-{playback_mode}", modes=("crop",))
    data["experience_type"] = experience_type
    data["playback_mode"] = playback_mode

    response = client.post("/upload", data=data, content_type="multipart/form-data", follow_redirects=False)

    assert response.status_code == 302
    assert app_module.Project.query.count() == 0


def test_video_server_timing_logs_use_same_upload_id(client, app_module, login_user, monkeypatch):
    logs = []
    _patch_upload_processing(app_module, monkeypatch)
    monkeypatch.setattr(app_module, "_upload_log", lambda stage, upload_id, **fields: logs.append((stage, upload_id, fields)))

    response = client.post(
        "/upload",
        data=_upload_data(name="video-timing", modes=("crop",)),
        content_type="multipart/form-data",
        follow_redirects=False,
    )

    assert response.status_code == 302
    stages = [stage for stage, _upload_id, _fields in logs]
    assert "VIDEO SERVER REQUEST ENTER" in stages
    assert "VIDEO SERVER BODY READY" in stages
    assert "VIDEO SERVER PERSIST START" in stages
    assert "VIDEO SERVER PERSIST DONE" in stages
    assert {upload_id for _stage, upload_id, _fields in logs} == {"upload-video-timing"}

    body_ready = next(fields for stage, _upload_id, fields in logs if stage == "VIDEO SERVER BODY READY")
    persist_done = next(fields for stage, _upload_id, fields in logs if stage == "VIDEO SERVER PERSIST DONE")
    assert body_ready["content_length"] is not None
    assert body_ready["body_duration_ms"] >= 0
    assert persist_done["video_size"] == len(_MP4_BYTES)
    assert persist_done["persistence_duration_ms"] >= 0
    source = Path("app.py").read_text(encoding="utf-8", errors="ignore")
    assert "VIDEO BG START" in source
    assert "VIDEO BG DONE" in source
    assert "background_duration_ms" in source


def test_one_two_and_three_pair_uploads_remain_compatible(client, app_module, login_user, monkeypatch):
    login_user.subscribed_project_limit = 10
    app_module.db.session.commit()
    monkeypatch.setattr(app_module, "get_plan_pairs_limit", lambda _user: 10)

    for pair_count in (1, 2, 3):
        _patch_upload_processing(app_module, monkeypatch)
        response = client.post(
            "/upload",
            data=_upload_data(name=f"pairs-{pair_count}", modes=tuple(["crop"] * pair_count)),
            content_type="multipart/form-data",
            follow_redirects=False,
        )

        assert response.status_code == 302
        project = app_module.Project.query.order_by(app_module.Project.id.desc()).first()
        pairs = app_module.ProjectPair.query.filter_by(project_id=project.id).all()
        assert len(pairs) == pair_count


def test_legacy_upload_without_marker_metadata_behaves_as_full_image(client, app_module, login_user, monkeypatch):
    _patch_upload_processing(app_module, monkeypatch)

    response = client.post(
        "/upload",
        data={
            "name": "Legacy Upload",
            "images": [(_jpeg_bytes(), "legacy.jpg")],
            "videos": [(_mp4_bytes(), "legacy.mp4")],
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )

    assert response.status_code == 302
    pair = app_module.ProjectPair.query.one()
    assert pair.marker_mode == "full_image"
    assert pair.marker_crop_x == 0
    assert pair.marker_crop_width == 1
    assert pair.marker_display_orientation == "legacy"


def test_invalid_crop_bounds_and_minimum_size_are_rejected(client, app_module, login_user, monkeypatch):
    _patch_upload_processing(app_module, monkeypatch)
    bad_bounds = _upload_data()
    bad_bounds["marker_0_crop_x"] = "0.8"
    bad_bounds["marker_0_crop_width"] = "0.4"

    response = client.post("/upload", data=bad_bounds, content_type="multipart/form-data", follow_redirects=False)
    assert response.status_code == 302
    assert app_module.Project.query.count() == 0

    too_small = _upload_data()
    too_small["marker_0_processed_width"] = "120"
    response = client.post("/upload", data=too_small, content_type="multipart/form-data", follow_redirects=False)
    assert response.status_code == 302
    assert app_module.Project.query.count() == 0


def test_roi_coverage_is_separate_from_full_frame_position(app_module):
    marker_w, marker_h = 500, 300
    frame_w, frame_h = 1200, 900
    src = np.array(
        [[40, 40], [250, 35], [460, 45], [55, 150], [250, 150], [445, 155], [35, 260], [250, 265], [465, 255]],
        dtype=np.float32,
    )
    small_roi = np.array([[80, 70], [260, 75], [255, 180], [85, 175]], dtype=np.float32)
    h_matrix = cv2.getPerspectiveTransform(
        np.array([[0, 0], [marker_w, 0], [marker_w, marker_h], [0, marker_h]], dtype=np.float32),
        small_roi,
    )
    dst = cv2.perspectiveTransform(src.reshape(-1, 1, 2), h_matrix).reshape(-1, 2)
    mask = np.ones((len(src), 1), dtype=np.uint8)

    ok, quality = app_module.evaluate_homography_quality(src, dst, h_matrix, mask, marker_w, marker_h, frame_w, frame_h, scale=1.0)

    assert ok
    assert quality["projected_roi_grid_cells"] >= 3
    assert quality["frame_grid_cells"] <= quality["projected_roi_grid_cells"]


def test_dev_test_payment_route_is_blocked_without_payment_order(client, app_module, monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setenv("SCANSTORY_DEV_TESTING", "1")
    app_module._seed_dev_test_users()
    user = app_module.User.query.filter_by(email="scanstorytest01@gmail.com").one()
    paid_plan = app_module.SubscriptionPlan.query.filter_by(is_trial_plan=False).first()
    with client.session_transaction() as sess:
        sess["user_id"] = user.id

    response = client.post("/create-razorpay-order", data={"plan_id": paid_plan.id})

    assert response.status_code == 403
    payload = response.get_json()
    assert payload["success"] is False
    assert "Payment is disabled" in payload["error"]
    assert app_module.PaymentOrder.query.count() == 0


def test_two_test_user_uploads_remain_isolated_with_unique_upload_ids(app_module, monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setenv("SCANSTORY_DEV_TESTING", "1")
    _patch_upload_processing(app_module, monkeypatch)
    app_module._seed_dev_test_users()
    users = [
        app_module.User.query.filter_by(email="scanstorytest01@gmail.com").one(),
        app_module.User.query.filter_by(email="scanstorytest02@gmail.com").one(),
    ]

    def submit(index):
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = users[index].id
        response = client.post(
            "/upload",
            data=_upload_data(name=f"Concurrent {index}", modes=("crop", "full_image")),
            content_type="multipart/form-data",
            follow_redirects=False,
        )
        return response.status_code

    statuses = [submit(0), submit(1)]

    assert statuses == [302, 302]
    projects = app_module.Project.query.order_by(app_module.Project.id).all()
    assert len(projects) == 2
    assert {project.owner_user_id for project in projects} == {user.id for user in users}
    for project in projects:
        pairs = app_module.ProjectPair.query.filter_by(project_id=project.id).order_by(app_module.ProjectPair.pair_index).all()
        assert len(pairs) == 2
        assert [pair.marker_mode for pair in pairs] == ["crop", "full_image"]
        for pair in pairs:
          assert pair.image_filename.startswith(f"{project.id}_")
          assert pair.processing_status == "uploaded"
