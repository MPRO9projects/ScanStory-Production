# AWS Sizing Recommendation

## Current Answer

Do not choose final instance size from "4 GB RAM and 2 cores" alone. Current evidence points to frontend/assets and CPU-heavy scanner design as major causes. AWS migration alone will not fix uncached 57 MB media, Tailwind CDN runtime, or OpenCV request CPU.

## Initial Shortlist

| Option | vCPU | RAM | Use |
|---|---:|---:|---|
| Lightsail 4 GB | 2 | 4 GB | cheapest comparable starting point |
| EC2 t4g.small | 2 | 2 GB | app only if DB/media are separated |
| EC2 t4g.medium | 2 | 4 GB | safer app start if traffic modest |
| EC2 c7g.large | 2 | 4 GB | better if scanner CPU is dominant |
| ECS Fargate 1-2 vCPU | 2 | 4-8 GB | cleaner split for web/worker |

## Graviton Compatibility

Likely compatible because Python packages generally support Linux ARM64, but verify OpenCV, NumPy, Pillow, cryptography, ffmpeg binary availability, and any deployed wheel constraints in staging.

## Recommended Start

Before measurements: EC2 t4g.medium or Lightsail 4 GB only if DB and media load are low. Better production shape: t4g.medium app, S3/CloudFront for media, managed MySQL, and separate worker once upload/scanner processing grows.

## Scaling Triggers

- CPU > 65 percent for 10 minutes.
- p95 API latency above target.
- memory > 75 percent.
- scanner route p95 spikes during concurrent users.
- worker queue age above acceptable threshold.

