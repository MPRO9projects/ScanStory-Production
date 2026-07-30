# Known Gaps

- Admin-owned project mappings were tested through the mechanism but not populated with real customer decisions.
- Performance rehearsal was local SQLite up to 60 users, 180 projects, and 500 pairs.
- Peak memory was not measured with a profiler.
- Missing media checks are policy-visible but do not inspect actual real media.
- Workspace billing, Experience UI, publishing, queues, AWS, and new scanner APIs remain out of scope.
