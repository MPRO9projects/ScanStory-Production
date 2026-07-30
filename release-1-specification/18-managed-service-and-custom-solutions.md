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

## Revision 1 Managed-Service Ownership

Decision status: Approved Release 1 rule.

M Pro9/Scan Story staff may create an Experience on behalf of a customer. The Experience must belong to either the customer Workspace or a managed-service Workspace with explicit contractual ownership. Publishing authority, transfer, and support access must be explicit and audited. No customer content may remain ambiguously owned by an administrator account. Customer content ownership is contractual; reusable platform IP remains owned by Scan Story/M Pro9.
