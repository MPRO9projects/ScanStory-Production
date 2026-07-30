# User Navigation Flow

```mermaid
flowchart TD
  A[Visitor opens landing /] --> B{Register or login?}
  B -->|Register| C[/register]
  C --> D[Email OTP verification]
  D --> E[/login]
  B -->|Login| E
  E --> F[/dashboard]
  F --> G[/create-project]
  G --> H[POST /upload]
  H --> I[/success/project_id]
  I --> J[Download/share QR]
  J --> K[/scanner/project_id]
  K --> L[Camera permission]
  L --> M[detect_init recognition]
  M -->|success| N[Video overlay plays]
  M -->|failure| O[Keep scanning]
  F --> P[/subscribe]
  P --> Q[Razorpay checkout]
  Q --> R[/verify-payment]
  R --> F
```

## Public Visitor

Public visitors can read marketing/blog/pricing/contact/legal content. They can open scanner URLs if they have a QR/link.

## Creator User

Registered users verify email, log in, create projects by uploading matching numbers of images and videos, receive generated QR codes, and monitor their projects.

## Admin

Admins log in separately, manage users/plans/payments/subscriptions/scans/settings, and can create unlimited admin projects through the same project creation template with admin-specific flags and storage paths.

