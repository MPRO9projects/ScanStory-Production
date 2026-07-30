# Dependency Resolution

Dependencies are respected by scheduling only the work whose source inputs exist:

- reference image present: image validation, artifact extraction, robustness
- video present: video probe
- any Trigger: readiness verification
- excluded Trigger: no jobs

Failed dependencies surface as creator-safe needs-attention states.
