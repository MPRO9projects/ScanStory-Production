# Recognition And Confidence Flow

Recognition still uses the legacy `/detect_init` contract.

Responses are accepted only when a valid success payload includes bounding coordinates. Invalid payloads are rejected and counted toward fallback recovery.

