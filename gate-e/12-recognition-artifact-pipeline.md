# Recognition Artifact Pipeline

Gate E keeps the ORB algorithm family.

`extract_recognition_artifact()` writes to a temporary `.npz`, validates enough structure for test use, and atomically publishes the artifact.

It records algorithm version, input hash, feature count, and artifact hash in the result.
