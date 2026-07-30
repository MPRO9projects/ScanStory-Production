# OpenCV WASM Loading

OpenCV initialization is represented as a bounded startup phase.

OpenCV load failures enter fallback with a safe `OPENCV_LOAD_FAILED` viewer message. WASM availability is included in capability detection and mode selection.

