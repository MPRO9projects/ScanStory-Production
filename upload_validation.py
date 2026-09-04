"""Server-side content validation for project media uploads (P0D).

Deliberately independent from media_processing.py, which backs the
disabled (feature-flagged) Experience Creator pipeline - hardening the
live /upload path should never risk perturbing that separate pipeline.

Every accepted upload is validated from its actual bytes (magic-byte
signature + real decode/probe) before it is treated as trusted project
media. Nothing here trusts the client-supplied filename, Content-Type
header, or FileStorage.mimetype for anything beyond a first-pass hint.
"""
import os
import tempfile

import cv2
from PIL import Image, ImageFile


class UploadValidationError(Exception):
    """Raised for any rejected upload.

    `safe_message` is the only thing ever shown to a client. `detail` is
    for server-side logging only and may contain decoder/library detail
    that must never reach a response body. `code` is an optional stable,
    machine-readable identifier (e.g. "VIDEO_LIMIT_REACHED") for admin-facing
    diagnostics/logging - never itself shown to a client either, since a raw
    code plus safe_message is still more than some callers need; existing
    callers that omit it are unaffected (defaults to None).
    """

    def __init__(self, safe_message, detail=None, code=None):
        super().__init__(safe_message)
        self.safe_message = safe_message
        self.detail = detail or safe_message
        self.code = code


IMAGE_SIGNATURES = {
    "JPEG": (b"\xff\xd8\xff",),
    "PNG": (b"\x89PNG\r\n\x1a\n",),
}
ALLOWED_IMAGE_EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png"}

# MP4/ISO-BMFF: 4-byte box size, then the box type "ftyp" at offset 4.
VIDEO_BOX_TYPE_OFFSET = 4
VIDEO_BOX_TYPE = b"ftyp"
ALLOWED_VIDEO_EXTENSION = ".mp4"


def _safe_remove(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def save_to_temp(file_storage, tmp_dir, suffix):
    """Persist an upload stream to a server-generated path under tmp_dir.

    The filename is never derived from the client-supplied name - this is
    the only thing that ever touches the filesystem before content
    validation has run.
    """
    os.makedirs(tmp_dir, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="upload_", suffix=suffix, dir=tmp_dir)
    os.close(fd)
    file_storage.stream.seek(0)
    file_storage.save(path)
    return path


def validate_image(file_storage, tmp_dir, max_bytes, max_dimension_px, max_pixels):
    """Validate an uploaded image from its actual content.

    Returns (temp_path, extension) on success. Raises UploadValidationError
    on any rejection; the temp file is removed before raising.
    """
    if not file_storage or not file_storage.filename:
        raise UploadValidationError("An image file is required.", "missing_file")

    path = save_to_temp(file_storage, tmp_dir, ".upload_img")
    ok = False
    try:
        size = os.path.getsize(path)
        if size == 0:
            raise UploadValidationError("Uploaded image is empty.", "zero_byte_image")
        if size > max_bytes:
            raise UploadValidationError(
                "Image file exceeds allowed size limit.", f"image_too_large:{size}"
            )

        with open(path, "rb") as fh:
            header = fh.read(16)
        detected_format = next(
            (fmt for fmt, sigs in IMAGE_SIGNATURES.items() if any(header.startswith(s) for s in sigs)),
            None,
        )
        if detected_format is None:
            raise UploadValidationError(
                "Unsupported image format.", f"bad_image_signature:{header[:8]!r}"
            )

        # verify() detects structural corruption but leaves the Image object
        # unusable afterwards - Pillow requires a fresh re-open for anything
        # further (dimensions, frame count, full decode).
        try:
            with Image.open(path) as probe:
                probe.verify()
        except Exception as exc:
            raise UploadValidationError("Invalid or corrupted image.", f"verify_failed:{exc}") from exc

        # The application globally sets ImageFile.LOAD_TRUNCATED_IMAGES = True
        # (for lenient handling of already-trusted media elsewhere) - that
        # would silently defeat truncated-file detection here, so validation
        # forces strict decoding regardless, then restores the app-wide setting.
        previous_truncated_setting = ImageFile.LOAD_TRUNCATED_IMAGES
        ImageFile.LOAD_TRUNCATED_IMAGES = False
        try:
            with Image.open(path) as img:
                if img.format != detected_format:
                    raise UploadValidationError(
                        "Unsupported image format.", f"format_mismatch:{img.format}"
                    )
                frame_count = getattr(img, "n_frames", 1)
                if frame_count and frame_count > 1:
                    raise UploadValidationError(
                        "Animated images are not supported.", f"animated:{frame_count}_frames"
                    )
                width, height = img.size
                if width <= 0 or height <= 0:
                    raise UploadValidationError("Invalid or corrupted image.", "zero_dimension")
                if width > max_dimension_px or height > max_dimension_px:
                    raise UploadValidationError(
                        "Image dimensions exceed the allowed limit.",
                        f"dimension_too_large:{width}x{height}",
                    )
                if width * height > max_pixels:
                    raise UploadValidationError(
                        "Image dimensions exceed the allowed limit.",
                        f"pixel_count_too_large:{width * height}",
                    )
                img.load()  # force full decode - catches truncated pixel data
        except UploadValidationError:
            raise
        except Exception as exc:
            raise UploadValidationError("Invalid or corrupted image.", f"decode_failed:{exc}") from exc
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = previous_truncated_setting

        ok = True
        return path, ALLOWED_IMAGE_EXTENSIONS[detected_format]
    finally:
        if not ok:
            _safe_remove(path)


def validate_video(file_storage, tmp_dir, max_bytes, max_duration_seconds=None):
    """Validate an uploaded video from its actual content.

    Returns (temp_path, extension) on success. Raises UploadValidationError
    on any rejection; the temp file is removed before raising.

    Uses cv2.VideoCapture (an always-available hard dependency of this
    application, unlike an ffprobe CLI binary) to confirm a real,
    decodable video stream exists - not just a container with the right
    magic bytes.
    """
    if not file_storage or not file_storage.filename:
        raise UploadValidationError("A video file is required.", "missing_file")

    path = save_to_temp(file_storage, tmp_dir, ".upload_vid")
    ok = False
    try:
        size = os.path.getsize(path)
        if size == 0:
            raise UploadValidationError("Uploaded video is empty.", "zero_byte_video")
        if size > max_bytes:
            raise UploadValidationError(
                "Video file exceeds allowed size limit.", f"video_too_large:{size}"
            )

        with open(path, "rb") as fh:
            header = fh.read(12)
        if len(header) < 8 or header[VIDEO_BOX_TYPE_OFFSET:VIDEO_BOX_TYPE_OFFSET + 4] != VIDEO_BOX_TYPE:
            raise UploadValidationError(
                "Unsupported video format.", f"bad_video_signature:{header[:8]!r}"
            )

        cap = cv2.VideoCapture(path)
        try:
            if not cap.isOpened():
                raise UploadValidationError("Invalid or corrupted video.", "videocapture_open_failed")
            frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
            readable, frame = cap.read()
            if not readable or frame is None:
                raise UploadValidationError(
                    "Invalid or corrupted video.", "no_readable_video_frame"
                )
            if max_duration_seconds:
                fps = cap.get(cv2.CAP_PROP_FPS) or 0
                if fps > 0 and frame_count > 0:
                    duration = frame_count / fps
                    if duration > max_duration_seconds:
                        raise UploadValidationError(
                            "Video exceeds the allowed duration.", f"duration_too_long:{duration:.1f}s"
                        )
        finally:
            cap.release()

        ok = True
        return path, ALLOWED_VIDEO_EXTENSION
    finally:
        if not ok:
            _safe_remove(path)
