# Dual Read Compatibility

Future dual-read shape:

```text
legacy Project ID -> Project -> optional mapped Experience -> preserve legacy response
```

Internal compatibility shape:

```text
Experience -> legacy Project when needed -> preserve scanner and storage behavior
```

Gate C adds resolver utilities but does not route production scanner requests through them.
