"""Fast Video Phase 1: PairMedia optimized-derivative transcode helper.

Pure, testable functions only - binary resolution, ffprobe parsing, ffmpeg
argument construction, subprocess invocation, and the size-retention policy.
Orchestration (loading/updating PairMedia rows, job status, file placement)
lives in processing_operations.py; this module never touches the database
or decides a final filename/path, only builds/runs the ffmpeg command and
reports what happened.
"""
import json
import os
import re
import shutil
import subprocess

DEFAULT_MAX_HEIGHT = 540
DEFAULT_TIMEOUT_SECONDS = 300
PROBE_TIMEOUT_SECONDS = 30
# A derivative is only worth keeping if it shrinks the file by at least this
# much - anything smaller (or larger) is discarded and the original stays
# the sole playback source. 10% is a deliberately simple, un-tuned first cut;
# revisit with real-world size data before Fast Video Phase 2.
MIN_SIZE_REDUCTION_RATIO = 0.10


class TranscodeError(RuntimeError):
    """Raised for any ffmpeg/ffprobe failure. Message is already sanitized -
    safe to store directly in PairMedia.optimization_error."""


def resolve_binary(env_var, binary_name):
    """Explicit env var override, else PATH - never a hard-coded path."""
    explicit = (os.environ.get(env_var) or "").strip()
    if explicit:
        return explicit
    return shutil.which(binary_name)


def resolve_ffmpeg_binary():
    return resolve_binary("SCANSTORY_FFMPEG_BINARY", "ffmpeg")


def resolve_ffprobe_binary():
    return resolve_binary("SCANSTORY_FFPROBE_BINARY", "ffprobe")


def derivative_filename(project_id, pair_id, media_id):
    """Deterministic and collision-safe - built only from trusted database
    ids, never from a client-supplied filename. Distinct from ProjectPair's
    older `<name>_fast.mp4` convention (which only ever identified one video
    per pair) since a pair can now have several PairMedia rows."""
    return f"{project_id}_{pair_id}_{media_id}_optimized.mp4"


def _sanitize_output(raw_bytes, limit=500):
    text = (raw_bytes or b"").decode("utf-8", errors="replace")
    text = re.sub(r"[A-Za-z]:[\\/][^\s]+", "[path]", text)
    text = re.sub(r"/(?:[^/\s]+/){2,}[^\s]+", "[path]", text)
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return (lines[-1] if lines else "ffmpeg failed")[:limit]


def probe_video(ffprobe_bin, input_path, timeout=PROBE_TIMEOUT_SECONDS):
    """{"has_audio": bool, "width": int|None, "height": int|None,
    "duration": float|None}. Raises TranscodeError on any failure."""
    if not ffprobe_bin:
        raise TranscodeError("ffprobe binary not available")
    args = [
        ffprobe_bin, "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", input_path,
    ]
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise TranscodeError("ffprobe timed out") from exc
    except OSError as exc:
        raise TranscodeError(_sanitize_output(str(exc).encode())) from exc
    if result.returncode != 0:
        raise TranscodeError(_sanitize_output(result.stderr))
    try:
        data = json.loads(result.stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise TranscodeError("ffprobe returned unreadable output") from exc
    streams = data.get("streams") or []
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    duration = None
    fmt_duration = (data.get("format") or {}).get("duration")
    if fmt_duration:
        try:
            duration = float(fmt_duration)
        except (TypeError, ValueError):
            duration = None
    return {
        "has_audio": has_audio,
        "width": video_stream.get("width") if video_stream else None,
        "height": video_stream.get("height") if video_stream else None,
        "duration": duration,
    }


def build_ffmpeg_args(ffmpeg_bin, input_path, output_path, has_audio, max_height=DEFAULT_MAX_HEIGHT):
    """List form for direct subprocess exec - never shell=True, so the
    comma inside the scale expression is backslash-escaped here rather than
    shell-quoted (there is no shell to strip quote characters).
    scale=-2:min(H,ih) caps height at H, derives width to preserve aspect
    ratio (-2 keeps it even, required for yuv420p), and never upscales:
    when the source is already shorter than H, min() keeps its own height."""
    scale_filter = f"scale=-2:min({int(max_height)}\\,ih)"
    args = [
        ffmpeg_bin, "-y", "-i", input_path,
        "-vf", scale_filter,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
    ]
    args += ["-c:a", "aac", "-b:a", "128k"] if has_audio else ["-an"]
    args.append(output_path)
    return args


def _remove_if_exists(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def transcode_video(ffmpeg_bin, input_path, output_path, has_audio, max_height=DEFAULT_MAX_HEIGHT, timeout=DEFAULT_TIMEOUT_SECONDS):
    """Writes to output_path (the caller's responsibility to pick a temp
    path and atomically os.replace() it onto the final name once this
    returns - this function never decides the final filename). Raises
    TranscodeError on any failure and removes a partial output_path first."""
    if not ffmpeg_bin:
        raise TranscodeError("ffmpeg binary not available")
    args = build_ffmpeg_args(ffmpeg_bin, input_path, output_path, has_audio, max_height)
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        _remove_if_exists(output_path)
        raise TranscodeError("ffmpeg timed out") from exc
    except OSError as exc:
        _remove_if_exists(output_path)
        raise TranscodeError(_sanitize_output(str(exc).encode())) from exc
    if result.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        _remove_if_exists(output_path)
        raise TranscodeError(_sanitize_output(result.stderr))
    return output_path


def should_retain_derivative(original_size, optimized_size, min_reduction_ratio=MIN_SIZE_REDUCTION_RATIO):
    """Only keep a derivative that is smaller than the original by at least
    min_reduction_ratio (default 10%) - never a same-size-or-larger or
    negligibly-smaller one."""
    if not original_size or not optimized_size:
        return False
    return optimized_size <= original_size * (1 - min_reduction_ratio)
