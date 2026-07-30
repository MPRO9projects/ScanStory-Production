# QR Generation Pipeline

QR generation is represented as processing work.

`generate_qr_asset()` separates destination identity from the rendered PNG asset.

Regeneration preserves the destination by default and atomically replaces only the rendered asset.

Legacy Project QR routes are not modified.
