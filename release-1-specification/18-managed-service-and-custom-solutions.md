---
title: Managed Service And Custom Solutions
tags:
  - scan-story/release-1
  - services
status: draft
---

# Managed Service And Custom Solutions

Managed service lets Scan Story staff create, test, publish, and monitor Experiences for clients.

```mermaid
flowchart LR
  A[Client brief] --> B[Staff workspace setup]
  B --> C[Asset collection]
  C --> D[Experience build]
  D --> E[Quality review]
  E --> F[Client approval]
  F --> G[Publish]
  G --> H[Report]
```

## Custom Request Workflow

```mermaid
flowchart LR
  A[Custom request] --> B[Triage]
  B --> C{Fits Release 1?}
  C -- yes --> D[Estimate managed work]
  C -- no --> E[Future add-on backlog]
  D --> F[Contract and schedule]
  F --> G[Delivery]
```

Managed access is entitlement-gated and auditable.

