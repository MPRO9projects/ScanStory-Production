# 2 vCPU 4 GB Profile

Initial recommendation for a 2-vCPU, 4-GB, 100-GB SSD deployment:

- Gunicorn web workers: 2
- Gunicorn threads: 2 to start, measure before increasing
- Request timeout: 30 seconds for web, scanner request timeout bounded by runtime mode
- Processing concurrency: 1
- Recognition request rate: full 250 ms, standard 350 ms, lightweight 650 ms client-side bounds
- Upload limit: keep current configured limit until media profiling is done
- Video-duration limit: keep conservative release limit
- Swap: enable modest swap for burst protection
- Log rotation: required
- Temporary-file cleanup: required
- Database placement: managed external DB preferred for production
- Object storage: recommended for production media, not added in Gate J
- Monitoring thresholds: CPU sustained above 70 percent, RAM above 75 percent, 5xx above 1 percent, scanner p95 above 2 seconds

This is a starting posture, not production proof, because no real server load run was available.

