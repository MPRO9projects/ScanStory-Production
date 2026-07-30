# Processing History And Audit

Gate F adds additive `ProcessingEvent` records for:

- processing requested
- job created
- manual retry requested
- source replaced
- artifact regenerated
- QR asset regenerated
- processing cancelled

Events are append-only in normal service flow.
