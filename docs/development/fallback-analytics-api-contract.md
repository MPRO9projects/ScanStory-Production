# Fallback Video + Fallback Analytics API Contract (V1 Wave 6)

Backend-only contract. This document is written so the scanner-frontend
implementer (a parallel wave building the actual "Watch video instead" /
recognition-timeout / camera-unavailable UI hooks) can build against it
without reading `app.py`.

## Scope

Two independent public, unauthenticated JSON APIs (same trust model as the
existing `/detect_init`, `/detect_track`, `/api/scanner/session/end`
routes - no login, no CSRF token, rate-limited by IP):

1. **Fallback video resolution** - "does this project (or this specific
   pair) have a video I can offer as a fallback, and what's its URL?"
2. **Fallback/analytics event recording** - "record that a fallback view /
   recognition timeout / camera-unavailable event happened" - **never**
   a successful scan.

Plus two normal authenticated (logged-in) creator routes for designating a
project's default fallback video.

## Event type vocabulary

| `event_type` | Meaning |
|---|---|
| `pair_fallback_view` | The scanner showed the fallback video for a SPECIFIC pair it was tracking toward. |
| `project_fallback_view` | The scanner showed the project-level default fallback video (no specific pair context). |
| `recognition_timeout` | A healthy camera never found a marker after repeated tries (the scanner gave up without ever matching). |
| `camera_unavailable` | A device/permission-level failure before any detection was possible (camera denied, no camera, secure-context required, etc). |

`matched_scan` (a real successful detection+overlay) is **not** an
accepted value on the event-recording route below - a real match is
recorded exclusively by the existing `detect_track`/`detect_init` path via
`ScanLog.is_successful=True`, never by a client POST. Submitting
`event_type: "matched_scan"` to the event route always fails with
`400 INVALID_EVENT_TYPE`.

**Fallback events are never successful scans.** They are stored in a
dedicated `scan_events` table, never `ScanLog` - they can never be counted
by `/api/scanner/session/end`, never appear in `project.scan_count`, and
never inflate any admin "total scans" counter.

## Routes

### 1. Resolve fallback video

`GET /api/scanner/<project_id>/fallback-video`

Optional query param: `pair_index` (integer) - the pair-context hint, i.e.
the specific pair the scanner was tracking toward before falling back
(e.g. after a partial detection that never fully confirmed). Omit it to
resolve the project-level default only.

Resolution order:
1. If `pair_index` is given AND that pair belongs to this project AND its
   video file actually exists on disk -> that pair's own video (`"source":
   "pair"`). This wins even if a project-level default is also configured.
2. Else, if the project has a project-level default fallback configured
   (see route 4) and its video file exists -> that pair's video
   (`"source": "project_default"`).
3. Else -> `{"available": false}`.

Success (`200`), a video is available:

```json
{"available": true, "source": "pair", "pair_index": 2, "video_url": "/video/17/2"}
```

`source` is `"pair"` or `"project_default"`. `video_url` is the same URL
shape `serve_video()` already uses elsewhere in this app (never a raw
filesystem path).

No video available (still `200`, this is a normal outcome, not an error):

```json
{"available": false}
```

Errors:
- Project doesn't exist -> `404 {"available": false, "error": "NOT_FOUND"}`
- Project suspended/inactive (same `_project_is_available()` check
  `serve_video`/`serve_image`/`serve_qr`/`detect_init` already use) ->
  `404 {"available": false, "error": "PROJECT_UNAVAILABLE"}`
- Rate limited -> `429` (see Rate limits below)

A `pair_index` that doesn't exist under **this** project (including one
that exists under a *different* project) is always treated as "no
pair-specific video" and falls through to the project-level default (or
`available: false`) - it is structurally impossible for this route to
resolve to another project's pair or video.

### 2. Record a fallback/analytics event

`POST /api/scanner/<project_id>/fallback-event`

Request body (JSON or form - same dual handling as
`/api/scanner/session/end`):

```json
{
  "event_type": "recognition_timeout",
  "client_event_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "scan_session_id": "optional-existing-scan-session-id",
  "pair_index": 2
}
```

- `event_type`: required, one of the four values above.
- `client_event_id`: required, a client-generated UUID (v4 recommended,
  max 36 chars). **This is the idempotency key** - see below.
- `scan_session_id`: optional. If you have one (the scanner already
  generates one for `detect_init`/`detect_track`), send it - it lets this
  event be correlated with the rest of that scan session's activity.
- `pair_index`: optional. Send it when the event has pair context
  (required in practice for `pair_fallback_view`, meaningless for
  `camera_unavailable`). An index that doesn't resolve to a real pair on
  this project is silently ignored (the event is still recorded, just
  without a pair reference) - this is a best-effort analytics event, not a
  media lookup, so a stale/racy pair_index never blocks recording that the
  fallback genuinely happened.

Success, first time this `client_event_id` is seen (`201`):

```json
{"success": true, "duplicate": false, "event": {"id": 501, "event_type": "recognition_timeout", "created_at": "2026-08-06T12:00:00"}}
```

Success, this `client_event_id` was already recorded (`200`) - **the safe
retry response**, not an error:

```json
{"success": true, "duplicate": true, "event": {"id": 501, "event_type": "recognition_timeout", "created_at": "2026-08-06T12:00:00"}}
```

Errors (all `{"success": false, "code": "...", "error": "..."}`):
- Project not found -> `404 NOT_FOUND`
- Project suspended/inactive -> `404 PROJECT_UNAVAILABLE`
- Missing body -> `400 INVALID_REQUEST`
- `event_type` missing/invalid (including `"matched_scan"`) -> `400 INVALID_EVENT_TYPE`
- `client_event_id` missing or over 36 chars -> `400 MISSING_CLIENT_EVENT_ID`
- Rate limited -> `429`

#### Idempotency: how to generate `client_event_id`

Generate **one fresh UUID per real, distinct event**, client-side, the
first time you decide to fire it (e.g. the moment the fallback panel is
shown, or the moment the recognition-timeout prompt appears). If the
network request fails or times out, retry with the **exact same**
`client_event_id` - the server will detect the duplicate (a database-level
UNIQUE constraint on `client_event_id`, not just an in-app check) and
return `"duplicate": true` instead of creating a second row. Never reuse a
`client_event_id` across two genuinely different events (a second
recognition-timeout later in the same session is a new event -> a new
UUID).

### 3. Designate the project-level default fallback (creator-only)

`POST /project/<project_id>/fallback-pair` (logged-in user, must own the
project) or `POST /admin/project/<project_id>/fallback-pair` (logged-in
admin, must own the project) - normal authenticated routes, **not**
CSRF-exempt (send the CSRF token the same way every other authenticated
POST in this app does).

Request body: `{"pair_index": 2}` to designate pair 2's own video as this
project's default fallback, or `{"pair_index": null}` to clear it.

Success: `200 {"success": true, "fallback_pair_index": 2}` (or `null` if
cleared).

Errors:
- Not logged in -> standard `login_required`/`admin_required` redirect/401.
- Project doesn't belong to the calling user/admin -> `404` (never `403` -
  existence is never leaked to a non-owner, same convention as every other
  ownership-checked route in this app).
- `pair_index` isn't an integer/null -> `400 INVALID_PAIR_INDEX`.
- No such pair on **this** project (including a pair_index that's valid on
  a *different* project) -> `404 PAIR_NOT_FOUND`. It is structurally
  impossible to designate another project's pair as this project's
  fallback.

There is no new upload flow here: this only ever references one of the
project's own already-uploaded/processed pairs.

## Rate limits

Both public routes are rate-limited per source IP (`request_limiter`, the
same in-process limiter every other scanner route uses - not Redis):
`scanner_fallback` (60 requests / 60s) for the GET route, and
`scanner_fallback_event` (60 requests / 60s) for the POST route. A
rate-limited request gets the same `429` shape as `detect_init`/
`detect_track`/`session/end`: `{"error": true, "code": "RATE_LIMITED",
"reason": "...", "retry_after_seconds": N}` with a `Retry-After` header.

## Error code vocabulary (fallback-video + fallback-event routes)

`NOT_FOUND`, `PROJECT_UNAVAILABLE`, `INVALID_REQUEST`, `INVALID_EVENT_TYPE`,
`MISSING_CLIENT_EVENT_ID`, `RATE_LIMITED`.

## Error code vocabulary (fallback-pair designation routes)

`INVALID_PAIR_INDEX`, `PAIR_NOT_FOUND`.

## Known V1 limitations

- No new fallback-video *upload* flow - the project-level default and
  every pair-level fallback are always one of the project's own existing
  pairs' videos, uploaded through the normal (or resumable) upload path.
- `scan_events` has no admin-facing dashboard/report view yet - the data
  is recorded and query-able (`ScanEvent` model) but there is no UI
  surfacing it. Out of scope for this wave (data/API layer only).
- The fallback-video route resolves at most one pair-level candidate (the
  exact `pair_index` hint given) - it does not search other pairs if that
  one has no video file on disk; it falls through directly to the
  project-level default instead.
