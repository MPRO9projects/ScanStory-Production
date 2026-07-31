# Test Environment Inventory

- OS: Microsoft Windows 11 Pro 10.0.26200
- CPU: Intel Core i5-8350U, 4 cores, 8 logical processors
- RAM: 8 GB installed class, 8,265,192 KB visible to OS
- Python: 3.10.11
- Chrome: 150.0.7871.187
- Edge: 150.0.4078.105
- Firefox: not installed
- Android device: not available
- iPhone device: not available
- Network: local loopback and current workstation network only
- Server: local Flask test client and local static server for browser probe
- Gunicorn: not executed locally
- Processing worker: not executed as a long-running worker
- Database: isolated SQLite test databases through pytest fixtures
- Scanner runtime modes: full, standard, lightweight, fallback via automated runtime policy tests
- Camera used: headless fake-media attempt only; no physical camera certification
- Test marker: synthetic JPEG frame for API rehearsal; no physical printed/displayed marker certification
- Video codec/resolution: metadata-only disposable MP4 asset names in tests; no real codec playback certification

