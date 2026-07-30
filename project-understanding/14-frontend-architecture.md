# Frontend Architecture

## Template Structure

- User pages mostly standalone templates with inline CSS and page scripts.
- Admin pages have `templates/admin/base.html` plus many admin child pages.
- Scanner is a standalone full-page template with all CSS and JS inline.

## CDN And Global Libraries

Confirmed template loads:

- Tailwind CDN: landing/blog/dashboard and other user pages.
- Font Awesome CDN: landing/blog/dashboard.
- Vanilla Tilt CDN: landing/blog.
- AOS CDN: landing/blog/dashboard.
- Google reCAPTCHA script: register/contact.
- OpenCV.js: scanner only.

## Template Dependency Map

| Base/Page | Child/Template | Scripts | API routes |
|---|---|---|---|
| standalone | `user/landing.html` | Tailwind CDN, Font Awesome, Vanilla Tilt, AOS, inline scroll/video JS | none primary |
| standalone | `user/scanner.html` | OpenCV.js, camera JS, optical flow JS | `/detect_init`, `/api/scanner/session/end` |
| standalone | `user/subscribe.html` | Razorpay checkout JS implied by route responses | `/create-razorpay-order`, `/verify-payment` |
| standalone | `user/contact.html` | reCAPTCHA, fetch form submit | `/send-contact-email` |
| admin/base | admin pages | admin page-specific forms/scripts | admin POST routes |

Full map: `template-script-map.csv`.

## Frontend Responsibilities

- Render marketing and forms.
- Collect uploads.
- Start scanner session.
- Request camera permission.
- Load OpenCV.js.
- Capture camera frames to canvas.
- Send detection frames to Flask.
- Track detected target locally using optical flow.
- Warp overlay video.
- End scanner session for scan counting.

