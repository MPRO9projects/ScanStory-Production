# Regression Protection Checklist

## Authentication

- Registration creates user, trial details, OTP, and pending verification session.
- Email verification marks user verified and clears session.
- Login tracks failed attempts and blocks after threshold.
- Blocked users cannot access protected routes.
- Admin and user sessions remain separate.

## Project/Story

- Upload requires equal image/video count.
- Plan max pairs is enforced for users.
- Project owner id is set correctly.
- `ProjectPair` rows match filesystem filenames.
- QR code path and scanner URL are saved.
- Feature extraction eventually marks pairs processed.

## Scanner

- Camera permission flow works on HTTPS.
- OpenCV.js initializes.
- `/detect_init` returns a matched pair, video URL, corners and points for a valid target.
- Failed detection keeps scanning.
- Optical-flow tracking keeps overlay aligned.
- Session end counts only once.

## Payment

- Active non-trial plans can create Razorpay orders.
- Signature verification is required.
- Successful payment activates plan and resets counters.
- Payment success email failure does not roll back the paid state.

## Files

- User and admin media paths stay distinct.
- `.npz` features align with project id and pair index.
- QR downloads use correct folder.
- Delete routes remove expected files and rows.

