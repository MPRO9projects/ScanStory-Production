# Atomic Publication Design

`publish_experience_version()` validates readiness, marks publication started, supersedes the previous Published Version, marks the new Version published, sets `current_published_version_id`, records audit events, and commits together.

Failed validation leaves the previous Published Version active.
