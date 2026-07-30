# QR And Scanner Baseline

Protected:

- `/scanner/<project_id>` resolves for a valid Project.
- invalid scanner Project currently returns body `Project not found` with status 200.
- `/qr/<filename>` serves QR files from the QR directory.
- `/image/<project_id>/<image_id>` and `/video/<project_id>/<image_id>` resolve through ProjectPair identity.
- scanner HTML includes OpenCV and `/detect_init` markers.

No QR routes or QR files were changed.

