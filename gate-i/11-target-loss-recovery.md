# Target Loss Recovery

Target loss now has an explicit state path:

- `tracking` to `target_lost`
- `target_lost` to `recovering`
- `recovering` to `detecting`
- `detecting` to `tracking` on a fresh valid response

Repeated invalid or empty recognition results move the viewer to fallback.

