"""Fast Video Phase 1: real ffmpeg/ffprobe transcode behavior.

Uses REAL ffmpeg/ffprobe (via media_optimization.resolve_ffmpeg_binary() /
resolve_ffprobe_binary(), which respect SCANSTORY_FFMPEG_BINARY /
SCANSTORY_FFPROBE_BINARY) - skipped entirely when neither is resolvable,
mirroring the existing `NODE = shutil.which("node")` /
`@pytest.mark.skipif(not NODE, ...)` convention already used in
tests/gate_jr/test_scanner_cold_start_js.py. Fixture videos are generated
with ffmpeg's own `lavfi` test sources - no binary asset files needed.
"""
import os
import subprocess

import pytest

import media_optimization as mo

FFMPEG = mo.resolve_ffmpeg_binary()
FFPROBE = mo.resolve_ffprobe_binary()
pytestmark = pytest.mark.skipif(
    not FFMPEG or not FFPROBE,
    reason="ffmpeg/ffprobe not resolvable (set SCANSTORY_FFMPEG_BINARY / SCANSTORY_FFPROBE_BINARY or add to PATH)",
)


def _make_source(path, width=1280, height=720, duration=2, with_audio=True):
    args = [FFMPEG, "-y", "-f", "lavfi", "-i", f"testsrc=duration={duration}:size={width}x{height}:rate=24"]
    if with_audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=1000:duration={duration}"]
    args += ["-c:v", "libx264", "-preset", "veryfast"]
    args += ["-c:a", "aac"] if with_audio else ["-an"]
    args.append(path)
    subprocess.run(args, check=True, capture_output=True)


@pytest.fixture()
def source_video(tmp_path):
    path = str(tmp_path / "source.mp4")
    _make_source(path, width=1280, height=720, duration=2, with_audio=True)
    return path


@pytest.fixture()
def silent_small_source_video(tmp_path):
    path = str(tmp_path / "small_silent.mp4")
    _make_source(path, width=320, height=240, duration=1, with_audio=False)
    return path


# ===========================================================================
# 10: output H.264 MP4
# ===========================================================================
def test_output_is_h264_mp4(tmp_path, source_video):
    out = str(tmp_path / "out.mp4")
    probe = mo.probe_video(FFPROBE, source_video)
    mo.transcode_video(FFMPEG, source_video, out, has_audio=probe["has_audio"])
    result = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name",
         "-of", "csv=p=0", out],
        capture_output=True, check=True,
    )
    assert result.stdout.decode().strip() == "h264"


# ===========================================================================
# 11: AAC preserved if audio exists
# ===========================================================================
def test_aac_audio_preserved_when_source_has_audio(tmp_path, source_video):
    out = str(tmp_path / "out.mp4")
    probe = mo.probe_video(FFPROBE, source_video)
    assert probe["has_audio"] is True
    mo.transcode_video(FFMPEG, source_video, out, has_audio=True)
    out_probe = mo.probe_video(FFPROBE, out)
    assert out_probe["has_audio"] is True
    codec = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name",
         "-of", "csv=p=0", out],
        capture_output=True, check=True,
    ).stdout.decode().strip()
    assert codec == "aac"


def test_no_audio_track_when_source_has_none(tmp_path, silent_small_source_video):
    out = str(tmp_path / "out.mp4")
    probe = mo.probe_video(FFPROBE, silent_small_source_video)
    assert probe["has_audio"] is False
    mo.transcode_video(FFMPEG, silent_small_source_video, out, has_audio=False)
    out_probe = mo.probe_video(FFPROBE, out)
    assert out_probe["has_audio"] is False


# ===========================================================================
# 12/13: no upscale, max height respected
# ===========================================================================
def test_downscales_to_max_height_preserving_aspect_ratio(tmp_path, source_video):
    out = str(tmp_path / "out.mp4")
    probe = mo.probe_video(FFPROBE, source_video)
    mo.transcode_video(FFMPEG, source_video, out, has_audio=probe["has_audio"], max_height=540)
    out_probe = mo.probe_video(FFPROBE, out)
    assert out_probe["height"] == 540
    # 1280x720 -> height 540 keeps 16:9: width should be 960 (even, per -2).
    assert out_probe["width"] == 960
    assert out_probe["width"] % 2 == 0


def test_never_upscales_a_source_already_below_max_height(tmp_path, silent_small_source_video):
    out = str(tmp_path / "out.mp4")
    mo.transcode_video(FFMPEG, silent_small_source_video, out, has_audio=False, max_height=540)
    out_probe = mo.probe_video(FFPROBE, out)
    assert out_probe["height"] == 240
    assert out_probe["width"] == 320


# ===========================================================================
# 14: faststart output
# ===========================================================================
def test_output_has_faststart_moov_atom_near_the_front(tmp_path, source_video):
    out = str(tmp_path / "out.mp4")
    probe = mo.probe_video(FFPROBE, source_video)
    mo.transcode_video(FFMPEG, source_video, out, has_audio=probe["has_audio"])
    # A faststart MP4 has its moov atom before mdat, near the start of the
    # file - the non-faststart default puts moov at the end. Checking the
    # first ~64KB for the 'moov' marker is the standard cheap proxy (ffprobe
    # exposes no direct "is faststart" flag).
    with open(out, "rb") as f:
        head = f.read(65536)
    assert b"moov" in head


# ===========================================================================
# 15: failed ffmpeg -> raises TranscodeError safely
# ===========================================================================
def test_transcode_of_a_nonexistent_input_raises_transcode_error(tmp_path):
    out = str(tmp_path / "out.mp4")
    with pytest.raises(mo.TranscodeError):
        mo.transcode_video(FFMPEG, str(tmp_path / "does_not_exist.mp4"), out, has_audio=False)
    assert not os.path.exists(out)


def test_transcode_error_message_is_sanitized_and_bounded(tmp_path):
    out = str(tmp_path / "out.mp4")
    deep_missing = str(tmp_path / "a" / "b" / "c" / "missing_source_file.mp4")
    try:
        mo.transcode_video(FFMPEG, deep_missing, out, has_audio=False)
        assert False, "expected TranscodeError"
    except mo.TranscodeError as exc:
        message = str(exc)
        assert len(message) <= 500
        assert "missing_source_file.mp4" not in message  # path redacted, not leaked raw
