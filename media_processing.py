import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np
import qrcode
from PIL import Image, ImageStat

from storage import stream_hash


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
IMAGE_SIGNATURES = {
    ".jpg": [b"\xff\xd8\xff"],
    ".jpeg": [b"\xff\xd8\xff"],
    ".png": [b"\x89PNG\r\n\x1a\n"],
    ".webp": [b"RIFF"],
}


def _atomic_replace(source_path, final_path):
    Path(final_path).parent.mkdir(parents=True, exist_ok=True)
    os.replace(source_path, final_path)


def validate_reference_image(path, min_width=64, min_height=64, max_bytes=20 * 1024 * 1024):
    path = Path(path)
    warnings = []
    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        return {"outcome": "invalid", "warnings": ["invalid_extension"]}
    if not path.exists() or path.stat().st_size == 0:
        return {"outcome": "invalid", "warnings": ["zero_byte_or_missing"]}
    if path.stat().st_size > max_bytes:
        return {"outcome": "invalid", "warnings": ["file_too_large"]}
    header = path.read_bytes()[:12]
    if not any(header.startswith(sig) for sig in IMAGE_SIGNATURES.get(path.suffix.lower(), [])):
        return {"outcome": "invalid", "warnings": ["invalid_signature"]}
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
            gray = image.convert("L")
            brightness = ImageStat.Stat(gray).mean[0]
            blur = float(cv2.Laplacian(np.array(gray), cv2.CV_64F).var())
    except Exception:
        return {"outcome": "invalid", "warnings": ["undecodable"]}
    if width < min_width or height < min_height:
        warnings.append("low_resolution")
    if brightness < 25:
        warnings.append("dark_image")
    if brightness > 235:
        warnings.append("bright_image")
    if blur < 10:
        warnings.append("blur_or_blank_risk")
    outcome = "valid_with_warning" if warnings else "valid"
    return {"outcome": outcome, "warnings": warnings, "width": width, "height": height, "blur": blur, "brightness": brightness}


def probe_video(path, ffprobe_path="ffprobe"):
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return {"outcome": "invalid", "warnings": ["zero_byte_or_missing"]}
    cmd = [
        ffprobe_path,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(path),
    ]
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=20)
        payload = json.loads(completed.stdout or "{}")
    except FileNotFoundError:
        return {"outcome": "degraded", "warnings": ["ffprobe_unavailable"], "size": path.stat().st_size}
    except Exception:
        return {"outcome": "invalid", "warnings": ["unreadable_video"], "size": path.stat().st_size}
    video_stream = next((s for s in payload.get("streams", []) if s.get("codec_type") == "video"), {})
    audio_present = any(s.get("codec_type") == "audio" for s in payload.get("streams", []))
    duration = float(payload.get("format", {}).get("duration") or video_stream.get("duration") or 0)
    warnings = []
    if duration <= 0:
        warnings.append("zero_duration")
    codec = video_stream.get("codec_name")
    if codec not in {"h264", "vp9", "av1", "hevc", "mpeg4"}:
        warnings.append("unsupported_codec")
    return {
        "outcome": "valid_with_warning" if warnings else "valid",
        "warnings": warnings,
        "container": payload.get("format", {}).get("format_name"),
        "codec": codec,
        "duration": duration,
        "width": video_stream.get("width"),
        "height": video_stream.get("height"),
        "frame_rate": video_stream.get("r_frame_rate"),
        "audio_present": audio_present,
        "size": path.stat().st_size,
    }


def extract_recognition_artifact(image_path, final_npz_path, algorithm_version="orb-gate-e-v1", min_features=8):
    validation = validate_reference_image(image_path)
    if validation["outcome"] == "invalid":
        return {"outcome": "invalid", "warnings": validation["warnings"]}
    gray = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if gray is None:
        return {"outcome": "invalid", "warnings": ["undecodable"]}
    orb = cv2.ORB_create(nfeatures=600)
    keypoints, descriptors = orb.detectAndCompute(gray, None)
    count = len(keypoints or [])
    warnings = []
    if count < min_features:
        warnings.append("weak_marker_low_features")
    final_path = Path(final_npz_path)
    fd, temp_name = tempfile.mkstemp(prefix=".artifact-", suffix=".npz", dir=str(final_path.parent))
    os.close(fd)
    try:
        points = np.array([kp.pt for kp in keypoints or []], dtype=np.float32)
        np.savez(
            temp_name,
            keypoints=points,
            descriptors=descriptors if descriptors is not None else np.zeros((0, 32), dtype=np.uint8),
            width=gray.shape[1],
            height=gray.shape[0],
            algorithm="orb",
            algorithm_version=algorithm_version,
            input_hash=stream_hash(image_path),
        )
        if not Path(temp_name).exists() and Path(temp_name + ".npz").exists():
            temp_name = temp_name + ".npz"
        _atomic_replace(temp_name, final_path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return {
        "outcome": "valid_with_warning" if warnings else "valid",
        "warnings": warnings,
        "feature_count": count,
        "artifact_hash": stream_hash(final_path),
        "algorithm_version": algorithm_version,
    }


def regenerate_recognition_artifact(image_path, final_npz_path, **kwargs):
    previous = None
    final_path = Path(final_npz_path)
    if final_path.exists():
        fd, previous = tempfile.mkstemp(prefix=".previous-", suffix=".npz", dir=str(final_path.parent))
        os.close(fd)
        shutil.copyfile(final_path, previous)
    result = extract_recognition_artifact(image_path, final_path, **kwargs)
    if result["outcome"] == "invalid" and previous and os.path.exists(previous):
        shutil.copyfile(previous, final_path)
    if previous and os.path.exists(previous):
        os.unlink(previous)
    return result


def generate_qr_asset(destination_url, final_png_path):
    if not destination_url.startswith(("https://", "http://", "/")):
        return {"outcome": "invalid", "warnings": ["invalid_destination"]}
    final_path = Path(final_png_path)
    fd, temp_name = tempfile.mkstemp(prefix=".qr-", suffix=".png", dir=str(final_path.parent))
    os.close(fd)
    try:
        image = qrcode.make(destination_url)
        image.save(temp_name)
        if not Path(temp_name).exists() or Path(temp_name).stat().st_size == 0:
            return {"outcome": "invalid", "warnings": ["missing_output"]}
        _atomic_replace(temp_name, final_path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return {"outcome": "valid", "warnings": [], "destination": destination_url, "asset_hash": stream_hash(final_path)}


def regenerate_qr_asset(destination_url, final_png_path):
    return generate_qr_asset(destination_url, final_png_path)
