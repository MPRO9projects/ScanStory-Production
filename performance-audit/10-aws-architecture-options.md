# AWS Architecture Options

Region assumption: `ap-south-1` Mumbai for Indian users. Pricing assumption: Linux, on-demand, 730 hours/month, low-to-moderate traffic, 50-100 GB storage, bandwidth not fully known. AWS pricing is variable; validate with AWS Pricing Calculator before purchase.

Sources checked: AWS EC2 On-Demand pricing page, Amazon Lightsail bundles page, Amazon RDS for MySQL pricing page.

## Option A - Cost-Conscious

```text
CloudFront + S3 for static/video
-> Lightsail 4 GB or EC2 t4g.small/t4g.medium for Flask
-> MySQL on same host only for very early stage, or small RDS if budget allows
-> CloudWatch basic logs/metrics
```

- Cost range: roughly $25-$80/month before high bandwidth.
- Pros: cheap and simple.
- Cons: limited HA, app and CPU-heavy scanner still compete for compute if single host.

## Option B - Recommended Reliable Production

```text
Route 53 + ACM
-> CloudFront
-> ALB
-> EC2 Auto Scaling or ECS service for Flask
-> S3 for media/static uploads
-> RDS MySQL Single-AZ or Multi-AZ depending SLA
-> CloudWatch logs/alarms
```

- Cost range: roughly $100-$250/month before high bandwidth.
- Pros: separates assets, app, and database.
- Cons: more setup than Lightsail.

## Option C - Scalable

```text
Route 53 + ACM + WAF
-> CloudFront
-> ALB
-> ECS Fargate services: web + worker
-> SQS for processing jobs
-> S3 media/features/QR
-> RDS MySQL Multi-AZ
-> ElastiCache only if measured need
-> CloudWatch + alarms + dashboard
```

- Cost range: roughly $250-$700+/month depending traffic and DB size.
- Pros: better isolation, worker scaling, safer deploys.
- Cons: not justified until measurements or business requirements demand it.

