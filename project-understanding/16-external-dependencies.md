# External Dependencies

See `external-dependencies.csv`.

## Key Services

- MySQL-compatible database via `DATABASE_URL`.
- Razorpay for subscription payment.
- SMTP for OTP/payment/contact emails.
- Google reCAPTCHA v3.
- CDN libraries: Tailwind, Font Awesome, Vanilla Tilt, AOS, reCAPTCHA.
- Browser camera permission and secure context.

## Native/System Dependencies

- `ffmpeg-python` requires an ffmpeg executable available at runtime.
- `opencv-python`, NumPy, Pillow and cryptography rely on compatible native wheels/libraries.
- `opencv.js` and `opencv_js.wasm` must be served with correct MIME (`app.py:82`).

## AWS/ARM64 Compatibility Considerations

OpenCV, NumPy, Pillow, cryptography, ffmpeg, and MySQL client wheels should be verified on the chosen Linux architecture. This is a compatibility note only, not an infrastructure recommendation.

