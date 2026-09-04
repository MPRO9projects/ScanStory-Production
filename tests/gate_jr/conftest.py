"""Reusable scanner-robustness fixtures — generated in-process, no committed binary assets.

Every fixture here builds synthetic point clouds/homographies or tiny in-memory images via
numpy/cv2. Nothing is written to disk and nothing is a real marker/camera photo — these
exercise the classification *rules* (evaluate_homography_quality, resolve_candidate_margin,
the /detect_init keypoint gate), not real-device camera behavior. See
gate-jr/scanner-quality-matrix.md for what still needs a physical phone.
"""
import cv2
import numpy as np
import pytest


def _encode_jpeg(img):
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


# --- Reference point sets ---------------------------------------------------------------

@pytest.fixture
def marker_grid_points():
    """9-point grid spread across a canonical 500x300 marker — the base reference set most
    homography-quality scenarios below build on."""
    return np.array(
        [
            [40, 40], [250, 35], [460, 45],
            [55, 150], [250, 150], [445, 155],
            [35, 260], [250, 265], [465, 255],
        ],
        dtype=np.float32,
    )


@pytest.fixture
def marker_dense_grid_points():
    """20-point 5x4 grid across a 500x300 marker — dense enough to model partial occlusion
    (dropping a region) while still leaving a legitimate spread for the un-occluded case."""
    xs = np.linspace(30, 470, 5)
    ys = np.linspace(30, 270, 4)
    return np.array([[x, y] for y in ys for x in xs], dtype=np.float32)


# --- Homography-quality case builder -----------------------------------------------------

@pytest.fixture
def homography_case():
    """Factory: (h_matrix, ...) -> kwargs for app_module.evaluate_homography_quality.
    Each scenario test states only what's different about it instead of repeating the
    src/dst/mask/marker/frame plumbing."""
    def build(h_matrix, src, marker_w=500, marker_h=300, frame_w=1200, frame_h=1200,
              scale=1.0, inlier_mask=None):
        h_matrix = np.asarray(h_matrix, dtype=np.float64)
        dst = cv2.perspectiveTransform(src.reshape(-1, 1, 2), h_matrix).reshape(-1, 2)
        n = len(src)
        mask = (inlier_mask.astype(np.uint8).reshape(-1, 1)
                if inlier_mask is not None else np.ones((n, 1), dtype=np.uint8))
        return dict(src_arr=src, dst_arr=dst, homography=h_matrix, mask=mask,
                    marker_w=marker_w, marker_h=marker_h, frame_w=frame_w, frame_h=frame_h,
                    scale=scale)
    return build


@pytest.fixture
def rotated_marker_homography():
    """Factory: an in-plane rotation (+ mild scale/translate) at the given angle."""
    def build(angle_degrees, s=0.9, tx=120, ty=250):
        theta = np.radians(angle_degrees)
        c, sn = np.cos(theta), np.sin(theta)
        return np.array([[s * c, -s * sn, tx], [s * sn, s * c, ty], [0, 0, 1]], dtype=np.float64)
    return build


@pytest.fixture
def perspective_trapezoid_homographies():
    """A moderate vs. an excessive keystone/perspective tilt, both mapping the same 500x300
    canonical marker rect onto a trapezoid in a 900x900-ish frame. Values were picked by
    direct measurement against the real edge_ratio/diagonal_ratio thresholds (8.0/6.0) —
    moderate stays comfortably under, excessive is well over."""
    canon = np.array([[0, 0], [500, 0], [500, 300], [0, 300]], dtype=np.float32)
    moderate_dst = np.array([[300, 100], [500, 100], [600, 700], [200, 700]], dtype=np.float32)
    excessive_dst = np.array([[380, 100], [420, 100], [750, 700], [50, 700]], dtype=np.float32)
    return {
        "moderate": cv2.getPerspectiveTransform(canon, moderate_dst),
        "excessive": cv2.getPerspectiveTransform(canon, excessive_dst),
    }


@pytest.fixture
def invalid_quad_homography():
    """Wildly out-of-bounds/degenerate projection — must fail valid_corners (code: invalid_quad)."""
    return np.array([[2.2, 0, -150], [0, 4.2, -250], [0, 0, 1]], dtype=np.float64)


@pytest.fixture
def wrong_marker_many_matches_case(marker_grid_points):
    """Descriptor-rich but geometrically invalid: 18 points tightly clustered in one small
    patch of the reference image — plenty of raw matches, but the geometry can't be
    trusted (clustered_reference_points)."""
    src = np.array([[100 + i * 2, 100 + (i % 3) * 2] for i in range(18)], dtype=np.float32)
    h = np.array([[1, 0, 80], [0, 1, 160], [0, 0, 1]], dtype=np.float64)
    return src, h


@pytest.fixture
def two_candidate_margin_cases():
    """(best_good, second_good) pairs for the ambiguous-vs-clear-winner candidate matcher."""
    return {
        "ambiguous": (20, 18),
        "clear_winner": (40, 5),
        "weak_runner_up": (10, 5),
    }


@pytest.fixture
def legacy_full_image_marker_homography():
    """A full-image (uncropped) 500x300 marker, scaled/perspective-projected into a real
    frame position — the pre-marker-crop-feature baseline shape."""
    canon = np.array([[0, 0], [500, 0], [500, 300], [0, 300]], dtype=np.float32)
    dst = np.array([[80, 200], [560, 220], [535, 1000], [105, 980]], dtype=np.float32) * 0.8
    return cv2.getPerspectiveTransform(canon, dst)


@pytest.fixture
def small_cropped_marker():
    """A small (120x80) cropped marker with its own 9-point spread, plus a homography
    placing it realistically in a big frame — marker_w/marker_h are intentionally NOT
    500x300 here, unlike every other fixture in this file."""
    marker_w, marker_h = 120, 80
    src = np.array(
        [
            [10, 8], [60, 6], [110, 9],
            [12, 40], [60, 40], [108, 41],
            [9, 72], [60, 74], [111, 70],
        ],
        dtype=np.float32,
    )
    canon = np.array([[0, 0], [marker_w, 0], [marker_w, marker_h], [0, marker_h]], dtype=np.float32)
    dst = np.array([[80, 200], [560, 220], [535, 1000], [105, 980]], dtype=np.float32) * 0.8
    h = cv2.getPerspectiveTransform(canon, dst)
    return src, h, marker_w, marker_h


# --- Image-level fixtures (real /detect_init HTTP path, no stored marker matches) --------

@pytest.fixture
def blank_wall_image_bytes():
    """Flat mid-grey frame — zero texture, fails the keypoint-count gate before matching."""
    return _encode_jpeg(np.full((480, 360, 3), 128, dtype=np.uint8))


@pytest.fixture
def low_texture_image_bytes():
    """Smooth gradient — some structure, but far below a real marker's texture."""
    grad = np.tile(np.linspace(80, 160, 360, dtype=np.uint8), (480, 1))
    return _encode_jpeg(cv2.cvtColor(grad, cv2.COLOR_GRAY2BGR))


@pytest.fixture
def high_noise_image_bytes():
    """Busy random-texture background — plenty of keypoints, but nothing that matches a
    stored marker."""
    rng = np.random.default_rng(1234)
    return _encode_jpeg(rng.integers(0, 255, (480, 360, 3), dtype=np.uint8))


@pytest.fixture
def repeated_pattern_image_bytes():
    """Tiled checkerboard — lots of self-similar keypoints (the 'many descriptor matches'
    shape), no stored marker registered to match against."""
    tile = np.zeros((20, 20), dtype=np.uint8)
    tile[:10, :10] = 255
    tile[10:, 10:] = 255
    board = np.tile(tile, (24, 18))[:480, :360]
    return _encode_jpeg(cv2.cvtColor(board, cv2.COLOR_GRAY2BGR))


@pytest.fixture
def motion_blurred_image_bytes():
    """Heavy directional blur over noise — approximates a fast-moving-camera frame where
    ORB has nothing stable to key off."""
    rng = np.random.default_rng(99)
    img = rng.integers(0, 255, (480, 360, 3), dtype=np.uint8)
    kernel_size = 25
    kernel = np.zeros((kernel_size, kernel_size))
    kernel[kernel_size // 2, :] = 1.0 / kernel_size
    return _encode_jpeg(cv2.filter2D(img, -1, kernel))


# --- Scanner resilience pass: frame-quality diagnostic fixtures ---------------------------
# Same "real /detect_init HTTP path, no stored marker matches" convention as the four
# fixtures above - these exist to prove the NEW diagnostic fields (likely_overexposed/
# likely_underexposed/likely_localized_glare/likely_low_contrast) fire correctly AND that
# none of them ever flip `detected` to True (frame-quality signals are diagnostic-only,
# never a matching shortcut).

@pytest.fixture
def bright_overexposed_image_bytes():
    """Near-white across the whole frame — global overexposure, not a hotspot."""
    rng = np.random.default_rng(11)
    base = np.full((480, 360, 3), 250, dtype=np.uint8)
    noise = rng.integers(0, 5, (480, 360, 3), dtype=np.uint8)
    return _encode_jpeg(np.clip(base.astype(np.int16) + noise, 0, 255).astype(np.uint8))


@pytest.fixture
def dark_underexposed_image_bytes():
    """Near-black across the whole frame — global underexposure."""
    rng = np.random.default_rng(12)
    base = np.full((480, 360, 3), 8, dtype=np.uint8)
    noise = rng.integers(0, 5, (480, 360, 3), dtype=np.uint8)
    return _encode_jpeg(np.clip(base.astype(np.int16) + noise, 0, 255).astype(np.uint8))


@pytest.fixture
def localized_glare_image_bytes():
    """Mid-brightness textured frame with ONE small blown-out hotspot in a corner - a
    concentrated reflection, not the whole frame overexposed. Overall mean brightness
    stays well under the global-overexposure threshold."""
    rng = np.random.default_rng(13)
    img = rng.integers(60, 140, (480, 360, 3), dtype=np.uint8)
    img[20:100, 20:100] = 255  # a small, fully blown-out hotspot in one corner only
    return _encode_jpeg(img)


@pytest.fixture
def low_contrast_image_bytes():
    """Everything squeezed into a narrow mid-grey band - flat, low-information frame that
    is neither over- nor under-exposed."""
    rng = np.random.default_rng(14)
    img = rng.integers(118, 138, (480, 360, 3), dtype=np.uint8)
    return _encode_jpeg(img)
