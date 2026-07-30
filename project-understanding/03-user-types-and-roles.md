# User Types And Roles

## Confirmed Roles

| Role/User Type | Login Required | Pages Accessible | Actions Allowed | Data Visible | Restrictions |
|---|---|---|---|---|---|
| Public visitor | No | `/`, `/blog`, `/pricing`, `/contact`, legal, scanner links | View marketing pages, submit contact, open scanner link | Public plans/content/scanner route | Cannot create projects or subscribe without login |
| Registered trial user | Yes | dashboard, profile, projects, create/edit/delete project, subscribe | Create projects within trial limits, scan, download QR | Own user account, own projects/payments/scans | `login_required`, `enforce_subscription`, trial limits |
| Paid user | Yes | same as trial plus active plan access | Create/scan within paid plan limits | Own account/projects/payments | active subscription limits |
| Blocked user | Yes attempted | login/dashboard redirects | None after block | none | `login_required` logs out blocked users |
| Scanner viewer | No app login required through QR | `/scanner/<project_id>` | Grant camera, scan marker, view video overlay | Project scanner only | Scan count tied to URL/session user context if present |
| Admin | Yes | admin dashboard, users, plans, projects, payments, scans, settings | Manage users, plans, subscriptions, projects, scans, settings | Broad admin data | `admin_required` |
| Superadmin | Yes | admin management routes | Manage admin accounts | Admin records | `super_admin_required` where applied |

## Authorization Enforcement

- Flask route decorators enforce most protected user/admin pages: `login_required` at `app.py:524`, `admin_required` at `app.py:553`, `super_admin_required` at `app.py:562`.
- Subscription checks are backend-side in `check_user_limits` and `enforce_subscription`: `app.py:606`, `app.py:665`.
- Project owner checks appear in routes before serving project data, downloads, edits and deletes, for example user project routes around `app.py:2056`, `app.py:2072`, `app.py:2090`, `app.py:2102`.
- Admin file serving validates admin ownership type before serving admin files: `app.py:5571`, `app.py:5589`, `app.py:5602`.

## Critical Permission Boundaries

- User project files must not be confused with admin project files.
- Users must only edit/delete/download their own projects.
- Scanner links can set `session["user_id"]` from query parameters in `app.py:3229`; future changes must preserve intended scan counting and avoid widening access.
- Admin and superadmin roles must remain separate for admin-account management.

