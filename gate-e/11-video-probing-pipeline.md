# Video Probing Pipeline

`probe_video()` shells to `ffprobe` using argument lists, not shell interpolation.

It captures container, codec, duration, dimensions, frame rate, audio presence, size, and warning conditions.

If `ffprobe` is unavailable, Gate E returns a degraded adapter result.

No transcoding is performed.
