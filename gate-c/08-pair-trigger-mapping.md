# Pair Trigger Mapping

`map_pairs_to_triggers()` creates one `Trigger` for each unmapped `ProjectPair` whose parent project has a mapped experience.

Mapping rule:

```text
project_pairs.id -> triggers.legacy_project_pair_id
```

The migration can represent legacy image/video files as `Asset` rows and legacy `.npz` feature files as `RecognitionArtifact` rows without moving or regenerating files.

Missing mapped projects, media, or artifacts are reported.
