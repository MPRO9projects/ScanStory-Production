# Public Key Strategy

`public_keys.py` generates opaque URL-safe public keys with server-side randomness.

Keys are independent from database IDs, indexed as unique values, and protected from mutation by SQLAlchemy update events.

Gate C does not replace existing scanner links with public keys.
