# Scanner Startup State Machine

Startup moves through `idle`, `loading_shell`, `checking_capabilities`, `requesting_camera`, `initializing_camera`, `loading_opencv`, `loading_wasm`, `initializing_scanner`, and `ready_to_scan`.

The state machine rejects invalid transitions, blocks duplicate initialization, and exposes timeout checks so the scanner cannot remain in a permanent loader state without an actionable fallback.

