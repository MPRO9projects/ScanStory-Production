# Trigger Processing Workflow

New Trigger processing schedules:

- validate reference image
- probe video
- extract recognition artifact
- test marker robustness
- verify processing readiness

Missing reference image or video is reported without scheduling impossible work.

Duplicate requests reuse existing idempotent jobs.
