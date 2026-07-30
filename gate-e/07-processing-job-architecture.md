# Processing Job Architecture

Gate E uses the additive `ProcessingJob` model from Gate C and extends it with lease and retry metadata.

`processing_jobs.py` provides:

- idempotent job creation
- state transitions
- claiming and lease expiry
- progress updates
- retry and terminal failure handling
- sanitized error logging

`processing_worker.py` provides a local command interface and does not start from Flask import.
