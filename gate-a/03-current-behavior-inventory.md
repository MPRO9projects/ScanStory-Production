# Current Behavior Inventory

Current app remains a Flask monolith with legacy terms `Project` and `ProjectPair`.

Generated inventories:

- `gate-a/current-route-baseline.csv`
- `gate-a/current-model-baseline.csv`
- `gate-a/scanner-contract-baseline.json`

Critical legacy behavior protected by tests:

- Auth and session baseline.
- Project/ProjectPair persistence and ownership.
- Legacy QR file serving.
- `/scanner/<project_id>` compatibility.
- `/detect_init`, `/detect_track`, and scanner session end contracts.
- User-level plan/payment baseline.
- Admin login and dashboard baseline.

