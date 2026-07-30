# Monitoring And Reliability Plan

## Must Add

- Health endpoint.
- Structured app logs.
- CloudWatch or equivalent logs with retention.
- CPU, RAM, disk, and latency alarms.
- Error-rate alert.
- Uptime monitor.
- DB backup schedule and restore drill.
- SSL renewal monitoring.
- Deploy rollback procedure.

## Scanner-Specific Metrics

- `/detect_init` p50/p95/p99.
- `/detect_track` p50/p95/p99.
- OpenCV processing time buckets.
- request size.
- successful detection ratio.
- active scanner sessions.

## Upload/Worker Metrics

- queue age once worker exists.
- job success/failure count.
- feature extraction duration.
- media storage growth.

