# Auth And Password Reset Coverage

Added password-reset tests:

- forgot-password page
- valid reset request
- unknown email
- OTP generation
- expired OTP
- invalid OTP
- valid password reset
- password hash update
- OTP consumption

Old password rejection is represented by hash assertion; login path is already covered.

