# Processing Orchestration Design

Gate F adds `processing_orchestration.py` as a service layer over Gate E primitives.

It schedules idempotent jobs for Trigger and Experience processing, records append-only events, calculates creator-safe status, supports selective source-change workflows, and keeps technical diagnostics separate from creator messages.

No scanner routes, QR routes, billing routes, templates, or upload flows are wired into orchestration.
