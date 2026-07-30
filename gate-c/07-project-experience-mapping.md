# Project Experience Mapping

`map_projects_to_experiences()` creates one `Experience` for each unmapped user-owned `Project` whose owner has a personal workspace.

Mapping rule:

```text
projects.id -> experiences.legacy_project_id
```

Admin-owned projects are not guessed. They are reported as migration failures requiring explicit workspace resolution.

Legacy project records are not modified.
