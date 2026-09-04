"""Local Creator Integrity pass (2026-08-28) - Phase 4/9 PostgreSQL concurrency
proof.

Unlike every other test in this suite, this one deliberately does NOT use the
Flask test client (which serves requests synchronously, one at a time, on a
single thread - it cannot produce a genuine race). It fires real, concurrent
HTTP requests at a locally-running dev server (threaded=True) backed by the
real QA PostgreSQL database, exactly as the brief requires ("true simultaneous
HTTP requests ... local HTTP ... is sufficient to prove application/DB
atomicity locally").

Requires, already running locally:
  - the Flask dev server on SCANSTORY_LIVE_BASE_URL (default http://127.0.0.1:5000)
    with threaded=True, backed by PostgreSQL (not SQLite/fake queue mode)
  - the seed-dev-test-users CLI already run (scanstorytestNN@gmail.com / 123456)

Skips itself entirely if the server isn't reachable, so it never breaks a
normal focused-regression pytest run with no live server up. Run explicitly:

    python -m pytest tests/gate_jr/test_v11_postgres_concurrency_proof.py -q -s

IDEMP-07 (concurrent finalize of the same resumable upload session) is NOT
re-proven here - it is already a pre-existing, already-atomic conditional
UPDATE (`UploadSession.status` transition, see finalize_upload_session's own
docstring in app.py), verified by code inspection rather than re-built as a
live scenario in this pass.
"""
import io
import os
import re
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np
import psycopg
import pytest
import requests
from PIL import Image

BASE_URL = os.environ.get("SCANSTORY_LIVE_BASE_URL", "http://127.0.0.1:5000")
DB_URL = os.environ.get(
    "SCANSTORY_LIVE_DATABASE_URL",
    "postgresql://scanstory_qa:ScanStoryQA2026!@localhost:55432/scanstory_qa",
)
TEST_EMAIL = "scanstorytest01@gmail.com"
TEST_PASSWORD = "123456"


def _server_reachable():
    try:
        return requests.get(f"{BASE_URL}/ready", timeout=3).status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _server_reachable(), reason="live dev server not reachable on SCANSTORY_LIVE_BASE_URL"
)


def _jpeg_bytes(color):
    out = io.BytesIO()
    Image.new("RGB", (48, 48), color).save(out, format="JPEG", quality=88)
    return out.getvalue()


_MP4_BYTES_CACHE = {}


def _mp4_bytes(fill=0):
    """A real, cv2-encoded MP4 - validate_video (on this live server, not a
    test fixture that monkeypatches it away) genuinely inspects the file, so
    placeholder bytes are rejected. Same recipe test_marker_selection_upload.py
    already uses for the same reason. `fill` selects the frame's pixel value
    so a genuinely DIFFERENT (non-duplicate) video is available on demand -
    every project's own seed video uses fill=0; the add-video race test must
    NOT reuse those exact bytes, or the existing (correct, pre-existing)
    duplicate-video guard rejects it for an unrelated reason before the race
    guard this test is actually trying to prove is ever reached."""
    if fill not in _MP4_BYTES_CACHE:
        fd, path = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        try:
            writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (64, 64))
            for _ in range(5):
                writer.write(np.full((64, 64, 3), fill, dtype=np.uint8))
            writer.release()
            with open(path, "rb") as fh:
                _MP4_BYTES_CACHE[fill] = fh.read()
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
    return _MP4_BYTES_CACHE[fill]


def _db_counts(user_id):
    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM projects WHERE created_by_user_id = %s", (user_id,))
            projects = cur.fetchone()[0]
            cur.execute(
                "SELECT count(*) FROM project_pairs pp JOIN projects p ON p.id = pp.project_id "
                "WHERE p.created_by_user_id = %s",
                (user_id,),
            )
            pairs = cur.fetchone()[0]
            cur.execute(
                "SELECT count(*) FROM pair_media pm JOIN project_pairs pp ON pp.id = pm.pair_id "
                "JOIN projects p ON p.id = pp.project_id WHERE p.created_by_user_id = %s",
                (user_id,),
            )
            media = cur.fetchone()[0]
    return {"projects": projects, "pairs": pairs, "media": media}


def _login():
    session = requests.Session()
    get_resp = session.get(f"{BASE_URL}/login/", timeout=15)
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', get_resp.text)
    assert match, "no csrf_token found on /login/"
    resp = session.post(
        f"{BASE_URL}/login/",
        data={"email": TEST_EMAIL, "password": TEST_PASSWORD, "csrf_token": match.group(1)},
        timeout=15,
        allow_redirects=True,
    )
    assert resp.status_code == 200, f"login failed: {resp.status_code} body={resp.text[:300]}"
    return session


def _csrf_token(session, page_url):
    resp = session.get(f"{BASE_URL}{page_url}", timeout=15)
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text)
    if not match:
        match = re.search(r"X-CSRFToken',\s*'([^']+)'", resp.text)
    assert match, f"no csrf_token found on {page_url}"
    return match.group(1)


def _user_id(session, email=TEST_EMAIL):
    resp = session.get(f"{BASE_URL}/create-project", timeout=15)
    match = re.search(r"user_id\s*[:=]\s*(\d+)", resp.text)
    if match:
        return int(match.group(1))
    # Parallel freeze preparation (2026-09-01): this fallback hardcoded the
    # module-level TEST_EMAIL regardless of which user the passed session
    # actually belongs to - harmless for idemp_01/02 (which always use that
    # same user) but silently resolved every OTHER dedicated test user back
    # to scanstorytest01's id, so their seed projects were created under the
    # real logged-in user while this helper reported a different one, and
    # the later "SELECT ... WHERE created_by_user_id = %s" lookup could never
    # find them. Accepting the real email closes that gap without touching
    # the untouched idemp_01/02 call sites, which don't pass one.
    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE email = %s", (email,))
            return cur.fetchone()[0]


@pytest.fixture(scope="module")
def live_session():
    """One login for the whole module - the login route is itself
    rate-limited (P0-8), and this suite's own job is to prove OTHER routes'
    concurrency safety, not repeatedly exercise the login limiter."""
    return _login()


@pytest.fixture(scope="module")
def live_user_id(live_session):
    return _user_id(live_session)


@pytest.fixture()
def live_csrf(live_session):
    return _csrf_token(live_session, "/create-project")


# ===========================================================================
# Parallel freeze preparation (2026-09-01): idemp_03/04/05/06/08/09 each call
# _create_one_project() (a real /upload POST) as their own setup, all sharing
# ONE test user's "upload": (8, 3600) rate-limit bucket (keyed by user_id,
# see app.py's handle_upload) together with idemp_01/02's own 2+5=7 create-
# idempotency submissions - 13 total /upload calls in one continuous run,
# reliably exceeding the real, correctly-working limit well before the suite
# finishes (confirmed and documented in the prior Track C/pre-freeze passes).
# This is NOT fixed by touching the rate limiter.
#
# Considered and rejected: sharing ONE seed project across 05/06/08/09
# instead of each creating its own. Rejected because 05 replaces the seed
# project's video, 06 replaces its image, and 09 depends on knowing pair 0's
# CURRENT image bytes precisely (its own comment: reusing _create_one_
# project's seed color would trip a real duplicate-target block) - chaining
# mutations across tests that each assume specific prior state is exactly
# the kind of fragility that produces a false test failure later, worse than
# the well-understood rate-limit collision it would replace.
#
# Fix instead: each of these six tests gets its OWN dedicated dev-test user
# (the rate limiter's key includes user_id, so this is a genuinely separate
# budget, not a shared one) via one small login helper - no mutated state is
# ever shared between tests, and no test's assumptions about its own seed
# project change. idemp_01/02 keep the original live_session/live_user_id/
# live_csrf fixtures and test user unchanged.
def _login_as(email):
    session = requests.Session()
    get_resp = session.get(f"{BASE_URL}/login/", timeout=15)
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', get_resp.text)
    assert match, "no csrf_token found on /login/"
    resp = session.post(
        f"{BASE_URL}/login/",
        data={"email": email, "password": TEST_PASSWORD, "csrf_token": match.group(1)},
        timeout=15,
        allow_redirects=True,
    )
    assert resp.status_code == 200, f"login failed for {email}: {resp.status_code} body={resp.text[:300]}"
    return session


def _dedicated_session_user_csrf(email):
    session = _login_as(email)
    user_id = _user_id(session, email=email)
    csrf = _csrf_token(session, "/create-project")
    return session, user_id, csrf


@pytest.fixture()
def idemp03_identity():
    return _dedicated_session_user_csrf("scanstorytest02@gmail.com")


@pytest.fixture()
def idemp04_identity():
    return _dedicated_session_user_csrf("scanstorytest03@gmail.com")


@pytest.fixture()
def idemp05_identity():
    return _dedicated_session_user_csrf("scanstorytest04@gmail.com")


@pytest.fixture()
def idemp06_identity():
    return _dedicated_session_user_csrf("scanstorytest05@gmail.com")


@pytest.fixture()
def idemp08_identity():
    return _dedicated_session_user_csrf("scanstorytest06@gmail.com")


@pytest.fixture()
def idemp09_identity():
    return _dedicated_session_user_csrf("scanstorytest07@gmail.com")


def _concurrent_post(session, url, build_data, count, csrf):
    def _one(_i):
        data, files = build_data()
        data["csrf_token"] = csrf
        return session.post(url, data=data, files=files, timeout=30, allow_redirects=False)

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(_one, range(count)))


# ===========================================================================
# IDEMP-01 / IDEMP-02: concurrent Create Project submissions, same upload_id
# ===========================================================================

def _run_create_idempotency(session, user_id, csrf, n):
    before = _db_counts(user_id)
    upload_id = f"concurrency-proof-{uuid.uuid4()}"

    def build():
        data = {
            "name": f"Concurrency Proof {upload_id}",
            "upload_id": upload_id,
            "experience_type": "image_video",
            "playback_mode": "tracked_overlay",
        }
        files = {
            "images": ("m.jpg", _jpeg_bytes((30, 60, 90)), "image/jpeg"),
            "videos": ("v.mp4", _mp4_bytes(), "video/mp4"),
        }
        return data, files

    responses = _concurrent_post(session, f"{BASE_URL}/upload", build, n, csrf)
    after = _db_counts(user_id)
    delta_projects = after["projects"] - before["projects"]
    print(f"\n[IDEMP n={n}] status codes: {[r.status_code for r in responses]}")
    print(f"[IDEMP n={n}] project delta: {delta_projects} (expected exactly 1)")
    assert delta_projects == 1, (
        f"expected exactly ONE project created from {n} concurrent identical "
        f"submissions (same upload_id), got {delta_projects}"
    )


def test_idemp_01_two_simultaneous_create_project_same_upload_id(live_session, live_user_id, live_csrf):
    _run_create_idempotency(live_session, live_user_id, live_csrf, 2)


def test_idemp_02_five_simultaneous_create_project_same_upload_id(live_session, live_user_id, live_csrf):
    _run_create_idempotency(live_session, live_user_id, live_csrf, 5)


# ===========================================================================
# IDEMP-03 / IDEMP-04: concurrent Add Video to the same target, same content
# ===========================================================================

def _create_one_project(session, csrf, user_id):
    upload_id = f"seed-{uuid.uuid4()}"
    resp = session.post(
        f"{BASE_URL}/upload",
        data={
            "name": f"Seed Project {upload_id}",
            "upload_id": upload_id,
            "experience_type": "image_video",
            "playback_mode": "tracked_overlay",
            "csrf_token": csrf,
        },
        files={
            "images": ("m.jpg", _jpeg_bytes((10, 200, 10)), "image/jpeg"),
            "videos": ("v.mp4", _mp4_bytes(), "video/mp4"),
        },
        timeout=30,
        allow_redirects=True,
    )
    assert resp.status_code == 200, f"seed project creation failed: {resp.status_code} url={resp.url}"
    # Successful creation redirects to /projects (the list page), not the new
    # project directly - the created row's own id is the reliable source.
    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM projects WHERE created_by_user_id = %s AND creation_idempotency_key = %s",
                (user_id, upload_id),
            )
            row = cur.fetchone()
    assert row, f"seed project (upload_id={upload_id}) was not found after creation - body={resp.text[:400]}"
    return row[0]


def _run_add_video_idempotency(session, user_id, csrf, n):
    project_id = _create_one_project(session, csrf, user_id)
    before = _db_counts(user_id)
    video_bytes = _mp4_bytes(fill=200)  # distinct from the seed project's own fill=0 default video

    def build():
        return (
            {"csrf_token": csrf},
            {"new_video": ("dup.mp4", video_bytes, "video/mp4")},
        )

    responses = _concurrent_post(session, f"{BASE_URL}/projects/{project_id}/pair/0/media/add", build, n, csrf)
    after = _db_counts(user_id)
    delta_media = after["media"] - before["media"]
    print(f"\n[IDEMP add-video n={n}] status codes: {[r.status_code for r in responses]}")
    print(f"[IDEMP add-video n={n}] pair_media delta: {delta_media} (expected exactly 1)")
    assert delta_media == 1, (
        f"expected exactly ONE PairMedia row from {n} concurrent identical "
        f"Add Video submissions, got {delta_media}"
    )


def test_idemp_03_two_simultaneous_add_video_same_content(idemp03_identity):
    session, user_id, csrf = idemp03_identity
    _run_add_video_idempotency(session, user_id, csrf, 2)


def test_idemp_04_five_simultaneous_add_video_same_content(idemp04_identity):
    session, user_id, csrf = idemp04_identity
    _run_add_video_idempotency(session, user_id, csrf, 5)


# ===========================================================================
# IDEMP-05 / IDEMP-06: Replace Video / Replace Target are single-row UPDATEs -
# no duplicate-row race is possible by construction (whichever request
# commits last simply wins the one row). Proven here as "no duplicate ever
# appears", not "one logical write wins over another".
# ===========================================================================

def test_idemp_05_two_simultaneous_replace_video_never_duplicates_media_rows(idemp05_identity):
    session, user_id, csrf = idemp05_identity
    project_id = _create_one_project(session, csrf, user_id)
    before = _db_counts(user_id)

    def build():
        return (
            {"csrf_token": csrf},
            {"replacement_video": ("r.mp4", _mp4_bytes(), "video/mp4")},
        )

    # media_id 1 is this freshly-created project's only (default) PairMedia row.
    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pm.id FROM pair_media pm JOIN project_pairs pp ON pp.id = pm.pair_id "
                "WHERE pp.project_id = %s", (project_id,),
            )
            media_id = cur.fetchone()[0]

    responses = _concurrent_post(
        session, f"{BASE_URL}/projects/{project_id}/pair/0/media/{media_id}/replace", build, 2, csrf
    )
    after = _db_counts(user_id)
    print(f"\n[IDEMP replace-video] status codes: {[r.status_code for r in responses]}")
    print(f"[IDEMP replace-video] media row count delta: {after['media'] - before['media']} (expected 0)")
    assert after["media"] == before["media"], "Replace Video must never create an extra row"


def test_idemp_06_two_simultaneous_replace_target_never_duplicates_pair_rows(idemp06_identity):
    session, user_id, csrf = idemp06_identity
    project_id = _create_one_project(session, csrf, user_id)
    before = _db_counts(user_id)
    new_image = _jpeg_bytes((5, 5, 250))

    def build():
        return ({"csrf_token": csrf}, {"image_0": ("newtarget.jpg", new_image, "image/jpeg")})

    responses = _concurrent_post(session, f"{BASE_URL}/projects/{project_id}/edit", build, 2, csrf)
    after = _db_counts(user_id)
    print(f"\n[IDEMP replace-target] status codes: {[r.status_code for r in responses]}")
    print(f"[IDEMP replace-target] pair row count delta: {after['pairs'] - before['pairs']} (expected 0)")
    assert after["pairs"] == before["pairs"], "Replace Target must never create an extra pair row"


# ===========================================================================
# Canonical target identity concurrency (Creator Identity remediation pass,
# 2026-08-29, brief section 36) - two scenarios specific to the new
# standardize-before-hash fix and the new Add-Pair route, on top of the
# generic idempotency proofs above.
# ===========================================================================

def test_idemp_08_two_simultaneous_add_pair_same_new_target_in_same_project(idemp08_identity):
    session, user_id, csrf = idemp08_identity
    project_id = _create_one_project(session, csrf, user_id)
    before = _db_counts(user_id)
    new_target = _jpeg_bytes((222, 111, 33))

    def build():
        return (
            {"csrf_token": csrf},
            {
                "new_pair_image": ("same_new_target.jpg", new_target, "image/jpeg"),
                "new_pair_video": ("v.mp4", _mp4_bytes(fill=201), "video/mp4"),
            },
        )

    responses = _concurrent_post(session, f"{BASE_URL}/projects/{project_id}/pair/add", build, 2, csrf)
    after = _db_counts(user_id)
    delta_pairs = after["pairs"] - before["pairs"]
    print(f"\n[IDEMP add-pair n=2] status codes: {[r.status_code for r in responses]}")
    print(f"[IDEMP add-pair n=2] pair delta: {delta_pairs} (expected exactly 1)")
    assert delta_pairs == 1, (
        f"expected exactly ONE new pair from 2 concurrent Add-Pair submissions "
        f"with the same target image, got {delta_pairs}"
    )


def test_idemp_09_two_simultaneous_replace_target_both_colliding_with_another_pair(idemp09_identity):
    """Two DIFFERENT pairs, both replaced concurrently to point at a THIRD
    pair's existing target - neither request originates from the pair being
    collided with, so this is a genuine race on the duplicate-target check
    (not the single-row-UPDATE-is-inherently-safe case IDEMP-06 already
    covers)."""
    session, user_id, csrf = idemp09_identity
    project_id = _create_one_project(session, csrf, user_id)
    before = _db_counts(user_id)

    # Add two more pairs to this project (sequential, not part of the race)
    # so there are three total: pair A (the collision target) plus two
    # others that will race to steal its image.
    def add_pair(color, video_fill):
        r = session.post(
            f"{BASE_URL}/projects/{project_id}/pair/add",
            data={"csrf_token": csrf},
            files={
                "new_pair_image": (f"seed_{video_fill}.jpg", _jpeg_bytes(color), "image/jpeg"),
                "new_pair_video": ("v.mp4", _mp4_bytes(fill=video_fill), "video/mp4"),
            },
            timeout=30,
        )
        assert r.status_code == 200, f"seed add-pair failed: {r.status_code}"

    # Distinct from _create_one_project's own seed color (10, 200, 10) -
    # reusing it here would trip a REAL (and correct) duplicate-target block
    # against the base pair, which is not what this test is trying to prove.
    add_pair((90, 40, 210), 210)  # pair B
    add_pair((210, 90, 40), 211)  # pair C

    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pair_index FROM project_pairs WHERE project_id = %s ORDER BY pair_index", (project_id,)
            )
            pair_indexes = [r[0] for r in cur.fetchall()]
    assert len(pair_indexes) == 3
    target_index = pair_indexes[0]   # pair A - the one whose image gets stolen
    racer_indexes = pair_indexes[1:]  # pair B and pair C - both race to steal it

    target_image = session.get(f"{BASE_URL}/image/{project_id}/{target_index}", timeout=15).content

    def build_for(pair_index):
        def _build():
            return ({"csrf_token": csrf}, {f"image_{pair_index}": ("stolen.jpg", target_image, "image/jpeg")})
        return _build

    def _one(pair_index):
        data, files = build_for(pair_index)()
        data["csrf_token"] = csrf
        return session.post(f"{BASE_URL}/projects/{project_id}/edit", data=data, files=files, timeout=30)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(_one, racer_indexes))

    print(f"\n[IDEMP replace-target race] status codes: {[r.status_code for r in responses]}")
    assert all(r.status_code == 200 for r in responses), "neither request should 500"

    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pair_index, image_hash FROM project_pairs WHERE project_id = %s ORDER BY pair_index",
                (project_id,),
            )
            rows = cur.fetchall()
    hashes = [h for _, h in rows]
    print(f"[IDEMP replace-target race] final hashes: {rows}")
    assert len(hashes) == len(set(hashes)), (
        "no two pairs in this project may end up sharing the same image_hash - "
        "at most one of the two racing requests may have won"
    )
