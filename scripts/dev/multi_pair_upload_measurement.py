"""Larger-file / multi-content-set resumable upload measurement (V1.1 Phase 2, P1).

Dev-only. Drives the REAL /api/uploads routes of a throwaway isolated app
in-process, with the REAL shipped adaptive-chunk policy, over a SIMULATED link.
No Chrome, no CDP (Phase 1 documented that 2 MiB destabilised the CDP socket in
this environment), and no giant fixtures committed to git - every byte is
generated at runtime.

WHAT IS MEASURED AND WHAT IS NOT. The link is a simulated clock, not a wire:
each chunk's transfer time is computed as size/rate plus a fixed per-request
latency, and the client's adaptive sizer is fed those computed samples exactly
as it would be fed real ones. So the CHUNK-SIZE EVOLUTION, RETRY COUNT,
RETRANSMITTED BYTES, SERVER-OFFSET CORRECTNESS, SESSION EXPIRATION, PEAK MEMORY
and FINAL BYTE INTEGRITY are all real behaviour of the shipped code. The
DURATION is analytic. It is reported so the shape of a 20 MB transfer at 0.3
Mbps is visible, and it must never be quoted as a measured production number -
see the report's limitations section.

Usage (dev only):
    python scripts/dev/multi_pair_upload_measurement.py --out measurements.json
    python scripts/dev/multi_pair_upload_measurement.py --quick
"""
import argparse
import json
import os
import sys
import tempfile
import tracemalloc
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# The shipped client policy, mirrored in Python for the simulation. These are
# the constants from templates/user/user_create_project.html; the assertion
# below fails the run if the template drifts from them, so this can never
# silently measure a policy the product no longer ships.
CHUNK_MIN_BYTES = 128 * 1024
CHUNK_MAX_BYTES = 5 * 1024 * 1024
CHUNK_STEP_BYTES = 64 * 1024
CHUNK_TARGET_SECONDS = 8
THROUGHPUT_SMOOTHING = 0.25
CHUNK_RESIZE_RATIO = 1.5

TEMPLATE = REPO_ROOT / "templates" / "user" / "user_create_project.html"
LATENCY_SECONDS = 0.15  # one round trip on a weak mobile link


def _assert_policy_constants_still_match():
    source = TEMPLATE.read_text(encoding="utf-8", errors="ignore")
    expected = {
        "RESUMABLE_CHUNK_MIN_BYTES = 128 * 1024": CHUNK_MIN_BYTES,
        "RESUMABLE_CHUNK_MAX_BYTES = 5 * 1024 * 1024": CHUNK_MAX_BYTES,
        "RESUMABLE_CHUNK_STEP_BYTES = 64 * 1024": CHUNK_STEP_BYTES,
        "RESUMABLE_CHUNK_TARGET_SECONDS = 8": CHUNK_TARGET_SECONDS,
        "RESUMABLE_THROUGHPUT_SMOOTHING = 0.25": THROUGHPUT_SMOOTHING,
        "RESUMABLE_CHUNK_RESIZE_RATIO = 1.5": CHUNK_RESIZE_RATIO,
    }
    missing = [needle for needle in expected if needle not in source]
    if missing:
        raise SystemExit(
            "Shipped adaptive-chunk constants drifted from this harness; "
            f"not found in the template: {missing}"
        )


def _round_chunk(value, server_cap):
    cap = max(CHUNK_MIN_BYTES, min(server_cap, CHUNK_MAX_BYTES))
    stepped = round((value or 0) / CHUNK_STEP_BYTES) * CHUNK_STEP_BYTES
    return int(max(CHUNK_MIN_BYTES, min(stepped or CHUNK_MIN_BYTES, cap)))


def _next_chunk(current, smoothed, server_cap):
    if not smoothed or smoothed <= 0:
        return current
    target = _round_chunk(smoothed * CHUNK_TARGET_SECONDS, server_cap)
    if target == current:
        return current
    ratio = max(target, current) / max(1, min(target, current))
    if ratio < CHUNK_RESIZE_RATIO:
        return current
    return target


def _boot_app():
    workdir = Path(tempfile.mkdtemp(prefix="scanstory_phase2_measure_"))
    os.environ["SCANSTORY_TESTING"] = "1"
    os.environ["TEST_DATABASE_URL"] = f"sqlite:///{(workdir / 'measure.db').as_posix()}"
    os.environ["SCANSTORY_DATA_DIR"] = str(workdir / "data")
    os.environ["SCANSTORY_ADMIN_DATA_DIR"] = str(workdir / "data_admin")
    os.environ["SCANSTORY_STATIC_UPLOADS_DIR"] = str(workdir / "static_uploads")
    os.environ["FLASK_SECRET_KEY"] = "phase2-measurement-secret"
    os.environ.pop("DATABASE_URL", None)

    import app as app_module  # noqa: E402  (env must be set first)

    # Same as tests/conftest.py's isolated_app: this harness exercises the
    # upload protocol, not the CSRF layer (which has its own suite).
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    ctx = app_module.app.app_context()
    ctx.push()
    app_module.db.create_all()

    from werkzeug.security import generate_password_hash
    from datetime import datetime, timedelta

    plan = app_module.SubscriptionPlan.query.filter_by(is_trial_plan=True).first()
    if plan is None:
        plan = app_module.SubscriptionPlan(
            name="Measurement", price=0, duration_days=30, total_project_limit=50,
            total_scan_limit=1000, max_pairs_per_project=10, is_trial_plan=True, is_active=True,
        )
        app_module.db.session.add(plan)
        app_module.db.session.commit()
    plan.max_pairs_per_project = 10
    user = app_module.User(
        email="measure@example.com", first_name="Measure", last_name="Run",
        password_hash=generate_password_hash("password123"), is_verified=True,
        subscription_id=plan.id, subscription_status="trial",
        subscription_taken_at=datetime.utcnow(),
        subscribed_project_limit=50, subscribed_scan_limit=1000,
        projects_used=0, scans_used=0,
    )
    app_module.db.session.add(user)
    app_module.db.session.commit()
    # Same trial row tests/conftest.py seeds; without it the account reads as
    # over its project limit and every session create is refused.
    app_module.db.session.add(app_module.TrialDetails(
        user_id=user.id,
        trial_start=datetime.utcnow(),
        trial_end=datetime.utcnow() + timedelta(days=7),
        trial_project_limit=50,
        trial_scan_limit=1000,
    ))
    app_module.db.session.commit()

    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = user.id
    return app_module, client, workdir


def _payload_bytes(total_bytes):
    """Deterministic, non-compressible-ish filler. Not a decodable video: this
    harness measures the TRANSFER, and finalize's decode path is covered by
    tests/integration/test_multi_pair_resumable_upload.py at real media sizes."""
    block = bytes((i * 37 + 11) % 256 for i in range(4096))
    reps = total_bytes // len(block)
    return block * reps + block[: total_bytes - reps * len(block)]


def measure_one_set(app_module, client, size_bytes, mbps, purpose="project_content_set"):
    server_cap = app_module.RESUMABLE_UPLOAD_CHUNK_MAX_BYTES
    rate = mbps * 1_000_000 / 8.0
    blob = _payload_bytes(size_bytes)

    resp = client.post("/api/uploads/sessions", json={
        "image_size": 0, "video_size": len(blob),
        "experience_type": "direct_qr", "playback_mode": "direct",
        "project_name": "Measurement", "purpose": purpose,
    })
    if resp.status_code != 201:
        raise SystemExit(f"session create failed: {resp.status_code} {resp.get_json()}")
    session = resp.get_json()["session"]
    session_id = session["id"]
    row = app_module.UploadSession.query.get(session_id)
    initial_expiry = row.expires_at

    chunk_bytes = CHUNK_MIN_BYTES
    smoothed = None
    offset = 0
    simulated_seconds = 0.0
    retransmitted = 0
    retries = 0
    requests = 0
    chunk_sizes = [chunk_bytes]

    tracemalloc.start()
    baseline = tracemalloc.get_traced_memory()[0]
    while offset < len(blob):
        piece = blob[offset:offset + chunk_bytes]
        r = client.post(
            f"/api/uploads/sessions/{session_id}/chunk",
            data=piece, headers={"X-Chunk-Offset": str(offset)},
            content_type="application/octet-stream",
        )
        requests += 1
        if r.status_code != 200:
            # A rejection costs the bytes that were in flight; the server's
            # authoritative offset travels back in the same response.
            retries += 1
            retransmitted += len(piece)
            body = r.get_json() or {}
            new_offset = body.get("current_offset")
            if new_offset is None:
                raise SystemExit(f"unrecoverable chunk rejection: {r.status_code} {body}")
            offset = new_offset
            continue
        simulated_seconds += LATENCY_SECONDS + (len(piece) / rate)
        sample = len(piece) / max(LATENCY_SECONDS + (len(piece) / rate), 1e-6)
        smoothed = sample if smoothed is None else (
            smoothed * (1 - THROUGHPUT_SMOOTHING) + sample * THROUGHPUT_SMOOTHING
        )
        resized = _next_chunk(chunk_bytes, smoothed, server_cap)
        if resized != chunk_bytes:
            chunk_bytes = resized
            chunk_sizes.append(chunk_bytes)
        offset = r.get_json()["current_offset"]
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    row = app_module.UploadSession.query.get(session_id)
    temp_path = app_module._upload_session_temp_path(row.storage_token)
    with open(temp_path, "rb") as fh:
        assembled = fh.read()

    return {
        "size_bytes": size_bytes,
        "size_mb": round(size_bytes / (1024 * 1024), 2),
        "mbps": mbps,
        "completed": offset == len(blob),
        "server_offset_correct": row.current_offset == len(blob),
        "final_bytes_identical": assembled == blob,
        "requests": requests,
        "retries": retries,
        "retransmitted_bytes": retransmitted,
        "chunk_bytes_first": chunk_sizes[0],
        "chunk_bytes_final": chunk_sizes[-1],
        "chunk_size_steps": len(chunk_sizes),
        "chunk_evolution": chunk_sizes[:12],
        "analytic_duration_seconds": round(simulated_seconds, 1),
        "analytic_duration_minutes": round(simulated_seconds / 60, 1),
        "peak_traced_memory_kb": round((peak - baseline) / 1024, 1),
        "session_still_active": row.status == "active",
        "inactivity_deadline_extended": row.expires_at >= initial_expiry,
        "session_id": session_id,
    }


def measure_multi_set(app_module, client, set_size_bytes, set_count, mbps):
    """The Phase 2 shape: N independently resumable content sets, one atomic
    project finalize. Asserts the property the whole pass exists for - a
    completed set is never re-sent."""
    per_set = [measure_one_set(app_module, client, set_size_bytes, mbps) for _ in range(set_count)]
    ids = [item["session_id"] for item in per_set]

    # Re-drive every set as a client resuming after a drop would: it asks the
    # server, is told the set is complete, and sends nothing.
    resent = 0
    for session_id in ids:
        state = client.get(f"/api/uploads/sessions/{session_id}").get_json()["session"]
        if state["current_offset"] < state["expected_total_size"]:
            resent += 1
    finalize = client.post("/api/uploads/projects/finalize", json={"session_ids": ids})

    # Failure isolation, measured at size. The filler bytes are not a decodable
    # video, so finalize rejects the FIRST set - which means every sibling must
    # come back 'active' with its assembled bytes untouched. That is the whole
    # point of the pass, observed here at 20 MB rather than at fixture scale.
    survivors = []
    for index, session_id in enumerate(ids):
        row = app_module.UploadSession.query.get(session_id)
        path = app_module._upload_session_temp_path(row.storage_token)
        survivors.append({
            "set_index": index,
            "status": row.status,
            "assembled_bytes_on_disk": os.path.getsize(path) if os.path.exists(path) else 0,
            "expected_total_size": row.expected_total_size,
        })

    return {
        "post_finalize_sets": survivors,
        "post_finalize_siblings_preserved": all(
            item["assembled_bytes_on_disk"] == item["expected_total_size"]
            for item in survivors[1:]
        ) if len(survivors) > 1 else None,
        "set_count": set_count,
        "set_size_mb": round(set_size_bytes / (1024 * 1024), 2),
        "mbps": mbps,
        "sets": per_set,
        "total_mb": round(set_size_bytes * set_count / (1024 * 1024), 2),
        "analytic_total_minutes": round(sum(i["analytic_duration_seconds"] for i in per_set) / 60, 1),
        "sets_needing_resend_after_reconcile": resent,
        "all_sets_intact": all(i["final_bytes_identical"] for i in per_set),
        "finalize_status": finalize.status_code,
        "finalize_code": (finalize.get_json() or {}).get("code"),
    }


MB = 1024 * 1024
PRIORITY_RUNS = [(5 * MB, 0.6), (20 * MB, 0.6), (20 * MB, 0.3)]
EXTRA_RUNS = [(5 * MB, 1.0), (5 * MB, 0.3), (50 * MB, 0.6)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=None)
    parser.add_argument("--quick", action="store_true", help="priority runs only, skip the 50 MB stability run")
    args = parser.parse_args()

    _assert_policy_constants_still_match()
    app_module, client, workdir = _boot_app()
    runs = list(PRIORITY_RUNS) + ([] if args.quick else list(EXTRA_RUNS))

    results = {"single_set": [], "multi_set": [], "workdir": str(workdir)}
    for size_bytes, mbps in runs:
        print(f"[measure] {size_bytes // MB} MB @ {mbps} Mbps ...", flush=True)
        outcome = measure_one_set(app_module, client, size_bytes, mbps)
        print(f"          done: {outcome['analytic_duration_minutes']} min analytic, "
              f"chunk {outcome['chunk_bytes_first']} -> {outcome['chunk_bytes_final']}, "
              f"retransmitted {outcome['retransmitted_bytes']} B, "
              f"integrity {outcome['final_bytes_identical']}", flush=True)
        results["single_set"].append(outcome)

    for set_size, count, mbps in ((5 * MB, 3, 0.6), (20 * MB, 2, 0.3)):
        print(f"[measure] multi-set {count} x {set_size // MB} MB @ {mbps} Mbps ...", flush=True)
        outcome = measure_multi_set(app_module, client, set_size, count, mbps)
        print(f"          done: {outcome['analytic_total_minutes']} min analytic, "
              f"resend-needed {outcome['sets_needing_resend_after_reconcile']}, "
              f"finalize {outcome['finalize_status']}", flush=True)
        results["multi_set"].append(outcome)

    text = json.dumps(results, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"[measure] wrote {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
