# Orientation And Lifecycle Recovery

The scanner handles page lifecycle events by pausing active tracking on page hide and showing a recovery overlay when visibility changes back to visible.

This keeps camera and tracking state from silently drifting after tab switches, app backgrounding, and mobile browser interruptions.

