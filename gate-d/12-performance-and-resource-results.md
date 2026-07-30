# Performance And Resource Results

Local synthetic rehearsal sizes:

- small: 10 users, 20 projects, 50 pairs
- medium: 30 users, 90 projects, 250 pairs
- large: 60 users, 180 projects, 500 pairs

Durations:

- small build: 19.1s
- small dry-run: 3.8s
- small apply: 4.7s
- medium build: 37.7s
- medium dry-run: 4.1s
- medium apply: 7.1s
- large build: 51.8s
- large dry-run: 4.8s
- large apply: 10.2s

Peak memory was not measured by a dedicated profiler. No obvious unbounded memory behavior appeared at tested local scale.
