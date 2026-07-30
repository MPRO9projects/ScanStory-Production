# Target Compatibility Model

Gate C adds future ownership, publishing, trigger, asset, artifact, processing, and migration checkpoint structures next to the existing legacy tables.

The target model maps:

- `Project.id` to `Experience.legacy_project_id`.
- `ProjectPair.id` to `Trigger.legacy_project_pair_id`.
- Existing image/video filenames to `Asset.storage_key`.
- Existing `.npz` feature filenames to `RecognitionArtifact.storage_key`.

The legacy system remains the live runtime system during Gate C.
