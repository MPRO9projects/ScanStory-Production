import secrets
import string


PUBLIC_KEY_ALPHABET = set(string.ascii_letters + string.digits + "-_")


def generate_public_key(prefix, token_bytes=16):
    token = secrets.token_urlsafe(token_bytes).rstrip("=")
    return f"{prefix}_{token}"


def is_url_safe_public_key(value):
    return bool(value) and all(ch in PUBLIC_KEY_ALPHABET for ch in value)


def generate_unique_public_key(session, model, prefix, field_name="public_key", max_attempts=10):
    for _ in range(max_attempts):
        public_key = generate_public_key(prefix)
        exists = session.query(model).filter(getattr(model, field_name) == public_key).first()
        if not exists:
            return public_key
    raise RuntimeError(f"Could not generate unique {field_name} for {model.__name__}")
