# Project And Pair Baseline

Protected legacy behavior:

- `Project` remains the production model name.
- `ProjectPair` remains the production pair/trigger-like model name.
- user-owned Project list works for authenticated owner.
- ProjectPair persists `project_id`, `pair_index`, image path, video filename, and processing state.
- image/video/feature path helpers point at isolated storage in tests.
- mismatched upload request is rejected through current redirect behavior.

Not changed:

- no Experience model added
- no Trigger model added
- no schema migration

