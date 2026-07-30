# Upload And Processing Flow

Uploads are paired by list order. Mismatched image/video counts are rejected.

Files are persisted to local Gate G storage, Asset and TriggerAsset rows are created, and Gate F orchestration queues durable processing jobs. Recognition work is not run synchronously in the web request.
