# Image Validation Pipeline

`validate_reference_image()` checks:

- extension
- file signature
- decodability
- dimensions
- file size
- zero-byte/missing file
- brightness
- blur/blank risk

Outcomes are `valid`, `valid_with_warning`, or `invalid`.
