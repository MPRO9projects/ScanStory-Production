import time
import uuid
from dataclasses import dataclass


SCANNER_STATES = {
    "idle",
    "loading_shell",
    "checking_capabilities",
    "requesting_camera",
    "initializing_camera",
    "loading_opencv",
    "loading_wasm",
    "initializing_scanner",
    "ready_to_scan",
    "detecting",
    "tracking",
    "target_lost",
    "recovering",
    "fallback",
    "paused",
    "failed",
}

VALID_TRANSITIONS = {
    "idle": {"loading_shell"},
    "loading_shell": {"checking_capabilities", "failed"},
    "checking_capabilities": {"requesting_camera", "fallback", "failed"},
    "requesting_camera": {"initializing_camera", "fallback", "failed"},
    "initializing_camera": {"loading_opencv", "fallback", "failed"},
    "loading_opencv": {"loading_wasm", "fallback", "failed"},
    "loading_wasm": {"initializing_scanner", "fallback", "failed"},
    "initializing_scanner": {"ready_to_scan", "fallback", "failed"},
    "ready_to_scan": {"detecting", "paused", "fallback"},
    "detecting": {"tracking", "ready_to_scan", "target_lost", "fallback", "paused", "failed"},
    "tracking": {"target_lost", "detecting", "paused", "fallback"},
    "target_lost": {"recovering", "fallback", "paused"},
    "recovering": {"tracking", "detecting", "fallback", "failed"},
    "fallback": {"requesting_camera", "paused"},
    "paused": {"ready_to_scan", "requesting_camera", "fallback", "failed"},
    "failed": {"fallback"},
}

STATE_TIMEOUT_MS = {
    "loading_shell": 5000,
    "checking_capabilities": 3000,
    "requesting_camera": 15000,
    "initializing_camera": 10000,
    "loading_opencv": 15000,
    "loading_wasm": 15000,
    "initializing_scanner": 8000,
    "detecting": 8000,
    "target_lost": 3000,
    "recovering": 7000,
}

RUNTIME_MODES = {
    "full": {"frame_width": 960, "detect_interval_ms": 250, "tracking_points": 260, "request_timeout_ms": 7000},
    "standard": {"frame_width": 720, "detect_interval_ms": 350, "tracking_points": 180, "request_timeout_ms": 8000},
    "lightweight": {"frame_width": 480, "detect_interval_ms": 650, "tracking_points": 90, "request_timeout_ms": 9000},
    "fallback": {"frame_width": 0, "detect_interval_ms": 0, "tracking_points": 0, "request_timeout_ms": 0},
}

VIEWER_ERRORS = {
    "CAMERA_PERMISSION_DENIED": ("Camera access is needed to recognize the image. Allow camera access, then tap Try Again.", True, True),
    "CAMERA_UNAVAILABLE": ("Camera is unavailable on this device. You can still view the fallback video.", True, True),
    "SECURE_CONTEXT_REQUIRED": ("Camera scanning needs a secure browser connection.", False, True),
    "OPENCV_LOAD_FAILED": ("The vision engine did not load. Try again or use fallback.", True, True),
    "WASM_LOAD_FAILED": ("The scanner engine is not supported by this browser.", False, True),
    "SCANNER_INIT_TIMEOUT": ("Scanner setup took too long. Try again or use fallback.", True, True),
    "DETECTION_TIMEOUT": ("Recognition is taking too long. Move closer or use fallback.", True, True),
    "INVALID_DETECTION_RESPONSE": ("The scanner could not read this response safely.", True, True),
    "TARGET_LOST_TIMEOUT": ("The image marker was lost. Point the camera at the image again.", True, True),
    "VIDEO_LOAD_FAILED": ("The video could not play on this browser. Try fallback playback.", True, True),
    "UNSUPPORTED_DEVICE": ("This device cannot run live tracking. Use fallback playback.", False, True),
    "EXPERIENCE_PAUSED": ("This Experience is paused.", False, False),
    "EXPERIENCE_ARCHIVED": ("This Experience is archived.", False, False),
    "PUBLISHED_MEDIA_MISSING": ("This Experience is temporarily unavailable.", False, False),
}


class ScannerStateError(ValueError):
    pass


@dataclass
class ScannerStateMachine:
    state: str = "idle"
    entered_at_ms: int = 0
    initializations: int = 0

    def transition(self, new_state, now_ms=0):
        if new_state not in SCANNER_STATES:
            raise ScannerStateError("unknown scanner state")
        if new_state not in VALID_TRANSITIONS.get(self.state, set()):
            raise ScannerStateError(f"invalid transition {self.state}->{new_state}")
        if self.state == "idle" and new_state == "loading_shell":
            self.initializations += 1
        elif new_state == "loading_shell" and self.initializations:
            raise ScannerStateError("duplicate initialization blocked")
        self.state = new_state
        self.entered_at_ms = now_ms
        return self.state

    def timed_out(self, now_ms):
        timeout = STATE_TIMEOUT_MS.get(self.state)
        return bool(timeout and now_ms - self.entered_at_ms >= timeout)

    def timeout_target(self):
        return "fallback" if self.state != "failed" else "fallback"


def select_runtime_mode(capabilities, override=None, prior_failure=False):
    if override in RUNTIME_MODES:
        return override
    if prior_failure:
        return "fallback"
    if not capabilities.get("secure_context", True):
        return "fallback"
    if not capabilities.get("camera_api", False):
        return "fallback"
    if not capabilities.get("webassembly", False):
        return "fallback"
    if not capabilities.get("canvas", True):
        return "fallback"
    memory = float(capabilities.get("device_memory") or 4)
    cores = int(capabilities.get("hardware_concurrency") or 2)
    width = int(capabilities.get("screen_width") or 360)
    reduced_motion = bool(capabilities.get("reduced_motion"))
    if memory <= 2 or cores <= 2 or width < 360 or reduced_motion:
        return "lightweight"
    if memory >= 6 and cores >= 6 and width >= 720 and capabilities.get("webgl", True):
        return "full"
    return "standard"


def mode_config(mode):
    if mode not in RUNTIME_MODES:
        raise ValueError("unknown runtime mode")
    return dict(RUNTIME_MODES[mode])


def viewer_error(code):
    message, retry, fallback = VIEWER_ERRORS.get(code, ("Scanner unavailable.", False, True))
    return {"code": code, "message": message, "retry_allowed": retry, "fallback_allowed": fallback}


# Wave 7 (429-vs-cadence fix, see docs/development/wave-7-detection-overlay-audit.md §7): every
# RATE_LIMITS scope this policy actually serves (app.py) uses a 60s window, so the server's own
# retry_after_seconds can never exceed that in real operation. This cap is defensive only.
MAX_BACKOFF_MS = 60000


class RecognitionRequestPolicy:
    def __init__(self, mode="standard"):
        cfg = mode_config(mode)
        self.interval_ms = cfg["detect_interval_ms"]
        self.timeout_ms = cfg["request_timeout_ms"]
        self.in_flight_id = None
        self.last_started_ms = -10**9
        self.latest_sequence = 0
        self.backoff_until_ms = -10**9

    def can_start(self, now_ms, page_visible=True, camera_active=True, tracking=False):
        if not page_visible or not camera_active or self.in_flight_id:
            return False
        if now_ms < self.backoff_until_ms:
            return False
        interval = self.interval_ms * (2 if tracking else 1)
        return now_ms - self.last_started_ms >= interval

    def start(self, now_ms):
        if self.in_flight_id:
            raise RuntimeError("recognition request already in flight")
        self.latest_sequence += 1
        self.in_flight_id = self.latest_sequence
        self.last_started_ms = now_ms
        return self.in_flight_id

    def finish(self, request_id):
        if request_id != self.in_flight_id:
            return "stale"
        self.in_flight_id = None
        return "accepted"

    def timed_out(self, now_ms):
        return bool(self.in_flight_id and now_ms - self.last_started_ms >= self.timeout_ms)

    def note_rate_limited(self, now_ms, retry_after_ms):
        """Called when a response comes back 429/RATE_LIMITED. retry_after_ms is the server's
        own advertised wait — never a client-guessed value. Only ever extends the deadline
        forward, so an earlier/smaller retry-after can't shorten an already-set later one."""
        bounded = max(0, min(retry_after_ms or 0, MAX_BACKOFF_MS))
        self.backoff_until_ms = max(self.backoff_until_ms, now_ms + bounded)

    def reset_backoff(self):
        """Explicit clean-slate reset — called on Continue Scanning / Retry Camera so neither
        carries over a stale backoff deadline from the attempt that led to that panel."""
        self.backoff_until_ms = -10**9

    def backoff_remaining_ms(self, now_ms):
        return max(0, self.backoff_until_ms - now_ms)


def is_rate_limited_response(status_code, payload):
    """Must be checked BEFORE validate_detection_response ever sees the payload — the 429 body
    shape ({error:true, code:"RATE_LIMITED", ...}, no "detected" key) would otherwise be reported
    as (True, "NO_MATCH"), indistinguishable from a genuine no-marker-found response. That
    misclassification is what let 429s silently inflate the client's failure streak and
    manufacture a false "recognition timed out" prompt (see the Wave 7 audit, §7)."""
    if status_code == 429:
        return True
    return bool(isinstance(payload, dict) and payload.get("code") == "RATE_LIMITED")


def resolve_retry_after_ms(payload, header_value=None):
    """Prefers the JSON body's retry_after_seconds (set from the same limiter state as the
    Retry-After header, see app.py's _scanner_rate_limited_response) with the header as a
    fallback. Returns milliseconds, never negative."""
    body_seconds = None
    if isinstance(payload, dict):
        try:
            candidate = float(payload.get("retry_after_seconds"))
            if candidate >= 0:
                body_seconds = candidate
        except (TypeError, ValueError):
            body_seconds = None
    if body_seconds is None:
        try:
            header_seconds = float(header_value)
            body_seconds = header_seconds if header_seconds >= 0 else 1.0
        except (TypeError, ValueError):
            body_seconds = 1.0
    return max(0, round(body_seconds * 1000))


def validate_detection_response(payload):
    if not isinstance(payload, dict):
        return False, "INVALID_DETECTION_RESPONSE"
    if not payload.get("detected"):
        return True, "NO_MATCH"
    corners = payload.get("corners")
    if not isinstance(corners, list) or len(corners) != 4:
        return False, "INVALID_DETECTION_RESPONSE"
    for point in corners:
        if not isinstance(point, dict):
            return False, "INVALID_DETECTION_RESPONSE"
        try:
            float(point["x"])
            float(point["y"])
        except (KeyError, TypeError, ValueError):
            return False, "INVALID_DETECTION_RESPONSE"
    if not payload.get("video_url"):
        return False, "PUBLISHED_MEDIA_MISSING"
    return True, "MATCH"


def create_viewer_session_id():
    return uuid.uuid4().hex


def now_ms():
    return int(time.time() * 1000)
