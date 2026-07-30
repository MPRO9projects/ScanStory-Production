# Upload And Filesystem Baseline

Covered:

- upload mismatch redirect baseline
- ProjectPair image/video/feature path helpers
- QR file serving from isolated QR directory
- feature artifact lookup and missing artifact behavior

Test filesystem roots are temporary:

- data
- data_admin
- static uploads
- QR
- images
- videos
- features

Known gaps:

- full file signature validation is xfailed as a security gap
- large upload and failed-write scenarios are documented but not fully automated

