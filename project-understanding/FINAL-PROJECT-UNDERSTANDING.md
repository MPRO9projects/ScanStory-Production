# Final Project Understanding

## 1. What Scan Story Is

Scan Story is a Flask web app that lets users create scan-based AR video experiences. A creator uploads reference images and matching videos. The app generates a QR code. A viewer scans the QR, opens a browser camera scanner, points at the reference image, and the matching video appears over the camera view.

## 2. Main Users

- Public visitors: view marketing/blog/pricing/contact pages and can open scanner links.
- Registered users: create and manage their own projects.
- Trial users: limited by trial plan.
- Paid users: limited by purchased plan.
- Admins: manage users, plans, payments, projects, scans and settings.
- Superadmins: admin-account management where enforced.
- Scanner viewers: anyone with a scanner QR/link.

## 3. Primary User Journey

Visitor registers, verifies email by OTP, logs in, creates a project, uploads equal numbers of images and videos, receives a QR code, shares/prints it, and viewers open the scanner from QR to see the video overlay after recognition.

## 4. Main Functional Modules

- Public website and blog.
- Auth and sessions.
- Subscription plans and Razorpay payments.
- Project/story creation.
- QR generation.
- Computer-vision feature extraction.
- Browser scanner and server detection.
- Admin management.
- SMTP email.
- Local filesystem media persistence.

## 5. Scanner Workflow

`/scanner/<project_id>` renders `templates/user/scanner.html`. Browser starts a scanner session, requests camera, loads OpenCV.js, captures frames to canvas, posts JPEG frames to `/detect_init`, receives matched pair/corners/video URL, then uses OpenCV.js optical flow to keep tracking and warps the overlay video with CSS matrix transforms. On unload it calls `/api/scanner/session/end`.

## 6. Story/Content Workflow

Source model name is `Project`; each story-like experience contains `ProjectPair` rows. Each pair has one uploaded reference image and one video. QR codes point to scanner URLs. Feature files connect uploaded image targets to scanner detection.

## 7. Database Structure

Major tables: `users`, `admins`, `subscription_plans`, `trial_details`, `payment_orders`, `otp_codes`, `projects`, `project_pairs`, `scan_logs`, `user_login_activities`, `admin_activities`, `system_configs`.

## 8. Files And Persistence

Essential runtime files include uploaded images, videos, generated QR PNGs, and `.npz` feature files under `data/` and `data_admin/`. Bundled files include static assets, landing videos, OpenCV.js and WASM.

## 9. Payment Flow

Razorpay is used for subscription plans. `/create-razorpay-order` creates a Razorpay order and pending `PaymentOrder`; `/verify-payment` verifies the signature, marks payment success, activates the plan, resets usage counters, and sends payment success email.

## 10. Authentication And Permissions

Users register with reCAPTCHA and email OTP. Passwords use Werkzeug hashes. Flask sessions store user/admin ids. Decorators enforce login/admin access. Subscription decorators enforce project/scan limits. Admin and user sessions are separate.

## 11. Frontend Structure

Frontend is mostly Jinja templates with inline CSS/JS. Landing/blog/dashboard load CDN Tailwind and animation libraries. Scanner is standalone and contains camera, OpenCV.js, detection, tracking and overlay logic.

## 12. Backend Structure

`app.py` is a large monolith containing app config, DB bootstrap, routes, auth, email, payment, QR generation, upload processing, OpenCV feature extraction, scanner detection, admin pages and file serving. `models.py` defines all database models.

## 13. Current Deployment Model

Confirmed only: Flask app object exists, `gunicorn` dependency exists, Flask debug `app.run` entry exists, `.env` is loaded, and local filesystem folders are created at startup. Actual production command/proxy/DB placement are unknown.

## 14. Critical Business Behaviours

- Register and verify email.
- Log in and enforce blocked/expired/limit states.
- Create project with image/video pairs.
- Generate and download QR.
- Extract and load feature files.
- Open scanner, detect target, play correct video overlay.
- Count scan once per successful session.
- Pay for subscription and activate limits.
- Admin manage users/plans/projects/payments/scans/settings.

## 15. High-Risk Code Areas

- Scanner JS and `/detect_init` contract.
- Feature file naming and cache invalidation.
- User/admin media directory split.
- Subscription limit checks and scan counting.
- Payment verification and user subscription update.

## 16. Confirmed Performance-Related Observations

Confirmed from source and prior audit: large bundled videos, OpenCV.js/WASM, Tailwind CDN runtime, repeated animation loops, OpenCV processing inside Flask request handlers, local media filesystem dependence, `db.create_all()` startup paths, and unbounded admin list queries.

## 17. Unknowns

Production start command, reverse proxy, DB hosting, live traffic, database size, production media volume, actual scanner p95 latency, recognition accuracy expectations, CPU/RAM usage, backup setup and deployment process remain unknown.

## 18. Readiness For Optimization Discussion

Sufficiently understood for optimization planning. Core architecture, flows, data model, scanner contract, persistence needs, auth/payment/email workflows, and critical regression boundaries are documented. Production metrics are still required before choosing exact fixes or AWS sizing.

