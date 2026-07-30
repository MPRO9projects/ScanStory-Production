# Route And Page Inventory

Full route details are in `route-inventory.csv`.

## Route Groups

- Public website: `/`, `/blog`, `/blog/<slug>`, `/pricing`, `/contact`, `/terms`, `/privacy`, `/faqs`, SEO files.
- Authentication: `/register`, `/verify-email/`, `/resend-otp/`, `/login/`, `/logout/`, `/forgot-password/`, `/reset-password/`.
- User account: `/dashboard`, `/profile`, `/projects`.
- Story/project: `/create-project`, `/upload`, `/project/<id>`, `/success/<id>`, edit/delete/reprocess routes.
- Scanner/media: `/scanner/<id>`, `/detect_init`, `/detect_track`, `/api/scanner/session/end`, `/image/...`, `/video/...`, `/qr/...`, `/media/<name>`.
- Payment: `/subscribe`, `/create-razorpay-order`, `/verify-payment`, `/payment-success`, `/payment-failed`.
- Admin: `/admin/login`, `/admin/dashboard`, users, plans, subscriptions, payments, projects, scans, settings, admins, activity logs.

## Major Page Map

| Page | Route | Template | Main JS/API |
|---|---|---|---|
| Landing | `/` | `templates/user/landing.html` | AOS, Vanilla Tilt, video playback, scroll handlers |
| Scanner | `/scanner/<project_id>` | `templates/user/scanner.html` | OpenCV.js, camera, `/detect_init`, `/api/scanner/session/end` |
| Dashboard | `/dashboard` | `templates/user/dashboard.html` | AOS, scroll animation |
| Create Project | `/create-project` | `templates/user/user_create_project.html` | file upload form to `/upload` |
| Success | `/success/<project_id>` | `templates/user/success.html` | QR/project result display |
| Subscribe/Pricing | `/pricing`, `/subscribe` | `templates/user/subscribe.html` | Razorpay order/verify flow |
| Admin Dashboard | `/admin/dashboard` | `templates/admin/dashboard.html` | admin navigation/actions |
| Admin Create Project | `/admin/projects/create` | `templates/user/user_create_project.html` | file upload form to `/admin/projects/upload` |

## API And Side Effects

- `/send-contact-email`: sends SMTP email to contact address.
- `/create-razorpay-order`: creates Razorpay order and `PaymentOrder`.
- `/verify-payment`: verifies signature, activates subscription, sends email.
- `/upload`: creates project, stores files, generates QR, starts feature extraction thread.
- `/detect_init`: creates/updates scan log, performs matching, returns video URL and tracking points.
- `/api/scanner/session/end`: counts successful scan once per scanner session.

