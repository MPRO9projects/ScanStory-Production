# Local Storage Adapter

`LocalFilesystemStorage` writes under one allowed root and verifies every resolved path remains under that root.

Writes use temporary files plus atomic replacement.

Gate E supports local storage for originals, derived files, recognition artifacts, QR assets, and temporary processing files.
