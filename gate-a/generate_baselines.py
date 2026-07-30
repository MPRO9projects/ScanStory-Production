import csv
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "gate-a"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def configure_test_env(tmp: Path):
    os.environ["SCANSTORY_TESTING"] = "1"
    os.environ["TEST_DATABASE_URL"] = f"sqlite:///{(tmp / 'baseline.db').as_posix()}"
    os.environ["SCANSTORY_DATA_DIR"] = str(tmp / "data")
    os.environ["SCANSTORY_ADMIN_DATA_DIR"] = str(tmp / "data_admin")
    os.environ["SCANSTORY_STATIC_UPLOADS_DIR"] = str(tmp / "static_uploads")
    os.environ["FLASK_SECRET_KEY"] = "gate-a-baseline-secret"
    os.environ.pop("DATABASE_URL", None)
    os.environ.pop("RAZORPAY_KEY_ID", None)
    os.environ.pop("RAZORPAY_KEY_SECRET", None)


def write_route_baseline(app_module):
    rows = []
    for rule in sorted(app_module.app.url_map.iter_rules(), key=lambda r: r.rule):
        methods = sorted(m for m in rule.methods if m not in {"HEAD", "OPTIONS"})
        route = rule.rule
        endpoint = rule.endpoint
        auth = "admin" if route.startswith("/admin") else ("user" if endpoint in {
            "dashboard", "projects_page", "user_create_project_page", "handle_upload",
            "subscribe_page", "create_razorpay_order", "verify_payment"
        } else "public")
        filesystem = any(part in route for part in ["/image", "/video", "/qr", "/media", "/static"])
        external = endpoint in {"create_razorpay_order", "verify_payment", "register", "forgot_password"}
        writes = any(m in methods for m in ["POST", "PUT", "DELETE", "PATCH"])
        rows.append({
            "route": route,
            "endpoint": endpoint,
            "methods": "|".join(methods),
            "authentication_requirement": auth,
            "user_admin_scope": auth,
            "response_type": "html/json/file/redirect",
            "current_expected_success_status": "route-specific",
            "expected_failure_status": "route-specific",
            "external_services": "yes" if external else "no",
            "filesystem": "yes" if filesystem else "no",
            "database_writes": "yes" if writes else "no",
            "test_coverage_status": "covered-critical" if route in {
                "/register", "/login/", "/projects", "/scanner/<int:project_id>",
                "/detect_init", "/detect_track", "/api/scanner/session/end",
                "/create-razorpay-order", "/verify-payment", "/admin/login"
            } else "inventory-only",
        })
    write_csv(OUT / "current-route-baseline.csv", rows)


def write_model_baseline(app_module):
    models = [
        app_module.User, app_module.Admin, app_module.SubscriptionPlan, app_module.TrialDetails,
        app_module.PaymentOrder, app_module.OTPCode, app_module.Project, app_module.ProjectPair,
        app_module.ScanLog, app_module.UserLoginActivity, app_module.AdminActivity, app_module.SystemConfig,
    ]
    rows = []
    for model in models:
        rows.append({
            "model": model.__name__,
            "table": model.__tablename__,
            "columns": "|".join(c.name for c in model.__table__.columns),
            "relationships": "|".join(model.__mapper__.relationships.keys()),
            "baseline_status": "documented",
        })
    write_csv(OUT / "current-model-baseline.csv", rows)


def write_scanner_contract():
    contract = {
        "legacy_contract": True,
        "scanner_route": "/scanner/<project_id>",
        "detect_init": {
            "method": "POST",
            "content_type": "multipart/form-data",
            "required_fields": ["project_id", "test_image"],
            "optional_fields": ["scan_session_id"],
            "success_fields": [
                "detected", "matched_pair_id", "video_url", "corners", "init_points",
                "frame_width", "frame_height", "variant", "inliers", "top_checked",
                "scan_session_id", "ready_pairs", "total_pairs", "is_admin_project"
            ],
            "invalid_input": {"status": 400, "fields": ["detected", "reason"]},
            "missing_project": {"status": 404, "fields": ["detected", "reason"]},
        },
        "detect_track": {
            "method": "POST",
            "content_type": "multipart/form-data",
            "required_fields": ["project_id", "pair_id", "test_image"],
            "optional_fields": ["scan_session_id"],
            "success_fields": ["ok", "corners", "frame_width", "frame_height", "variant", "inliers"],
            "invalid_input": {"status": 400, "fields": ["ok", "reason"]},
        },
        "session_end": {
            "route": "/api/scanner/session/end",
            "method": "POST",
            "required_fields": ["project_id", "session_id"],
            "success_fields": ["ok", "counted"],
        },
    }
    (OUT / "scanner-contract-baseline.json").write_text(json.dumps(contract, indent=2), encoding="utf-8")


def write_coverage_and_registers():
    write_csv(OUT / "test-coverage-map.csv", [
        row("registration", "tests/integration/test_auth_baseline.py", "covered"),
        row("verification", "tests/integration/test_auth_baseline.py", "covered"),
        row("login", "tests/integration/test_auth_baseline.py", "covered"),
        row("password reset", "tests/integration/test_auth_baseline.py", "partial"),
        row("user authorization", "tests/security/test_security_baseline.py", "covered"),
        row("admin authorization", "tests/security/test_security_baseline.py", "covered"),
        row("Project creation", "tests/integration/test_project_qr_scanner_baseline.py", "partial"),
        row("ProjectPair persistence", "tests/integration/test_project_qr_scanner_baseline.py", "covered"),
        row("plan/trial limits", "tests/integration/test_payment_and_admin_baseline.py", "partial"),
        row("QR generation/serving", "tests/integration/test_project_qr_scanner_baseline.py", "partial"),
        row("scanner page", "tests/integration/test_project_qr_scanner_baseline.py", "covered"),
        row("detect endpoint contract", "tests/contracts/test_scanner_contract.py", "covered"),
        row("scan logging", "tests/contracts/test_scanner_contract.py", "covered"),
        row("payment verification", "tests/integration/test_payment_and_admin_baseline.py", "covered"),
        row("upload validation", "tests/integration/test_project_qr_scanner_baseline.py", "partial"),
        row("file path isolation", "tests/unit/test_models_and_paths.py", "covered"),
    ])
    write_csv(OUT / "security-baseline-register.csv", [
        sev("CSRF disabled globally", "High", "xfail", "Gate A"),
        sev("Security headers helper not registered", "Medium", "xfail", "Gate A"),
        sev("Upload file-signature validation incomplete", "High", "xfail", "before public staging"),
        sev("OTP brute-force throttling absent", "High", "xfail", "before public staging"),
        sev("Tenant isolation is legacy user/admin ownership only", "Medium", "documented", "Gate C"),
    ])
    write_csv(OUT / "known-gaps.csv", [
        {"gap": "Full browser camera automation not executed", "type": "manual", "status": "documented"},
        {"gap": "Actual recognition accuracy not measured", "type": "cv", "status": "future cv gate"},
        {"gap": "Password reset coverage partial", "type": "test coverage", "status": "documented"},
        {"gap": "Route inventory auth classification is heuristic", "type": "inventory", "status": "documented"},
    ])
    browsers = ["Chrome Android", "Samsung Internet", "Safari iPhone", "Chrome iPhone", "Chrome Windows", "Edge Windows", "Safari macOS"]
    checks = ["QR opening", "scanner shell", "camera permission", "rear-camera selection", "OpenCV load", "WASM load", "recognition", "video overlay", "target loss", "re-acquisition", "rotation", "low light", "slow network", "denied camera", "refresh", "background/foreground", "autoplay"]
    write_csv(OUT / "manual-browser-device-matrix.csv", [
        {"browser_device": b, "check": c, "status": "Not yet executed", "notes": ""} for b in browsers for c in checks
    ])


def row(flow, file, status):
    return {"critical_flow": flow, "test_file": file, "coverage_status": status}


def sev(finding, severity, status, gate):
    return {"finding": finding, "severity": severity, "status": status, "future_gate": gate}


def write_performance_baseline(app_module):
    client = app_module.app.test_client()
    routes = [("/", "landing"), ("/login/", "login"), ("/scanner/999999", "scanner_invalid")]
    rows = []
    for path, name in routes:
        samples = []
        sizes = []
        for _ in range(5):
            start = time.perf_counter()
            response = client.get(path)
            samples.append((time.perf_counter() - start) * 1000)
            sizes.append(len(response.data or b""))
        rows.append({
            "measurement": name,
            "environment": "local test client",
            "dataset": "isolated bootstrap DB",
            "warm_cold": "warm",
            "median_ms": round(statistics.median(samples), 2),
            "p95_ms": round(max(samples), 2),
            "response_size_bytes": max(sizes),
            "notes": f"status {response.status_code}; local only",
        })
    for asset in ["static/js/opencv.js", "static/js/opencv_js.wasm", "static/videos/demo.mp4"]:
        p = ROOT / asset
        rows.append({
            "measurement": asset,
            "environment": "filesystem",
            "dataset": "repo asset",
            "warm_cold": "n/a",
            "median_ms": "",
            "p95_ms": "",
            "response_size_bytes": p.stat().st_size if p.exists() else "missing",
            "notes": "local file size",
        })
    write_csv(OUT / "performance-baseline.csv", rows)


def write_csv(path: Path, rows):
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main():
    OUT.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="scanstory_gate_a_") as td:
        configure_test_env(Path(td))
        sys.path.insert(0, str(ROOT))
        import app as app_module

        write_route_baseline(app_module)
        write_model_baseline(app_module)
        write_scanner_contract()
        write_coverage_and_registers()
        write_performance_baseline(app_module)
        with app_module.app.app_context():
            app_module.db.session.remove()
            app_module.db.engine.dispose()


if __name__ == "__main__":
    main()
