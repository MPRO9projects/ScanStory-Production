# Fixture Architecture

Fixture dependency diagram:

```text
isolated_app
  -> app_module
  -> app
  -> client
  -> db_session
  -> plan
  -> normal_user / expired_user
  -> admin / secondary_admin
  -> project_with_pair
      -> multiple_pairs
      -> feature_artifact
  -> login_user / login_admin
```

Fixtures are deterministic, use temp storage, and do not use production data.

