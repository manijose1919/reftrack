"""Optional shared-password authentication.

Set REFTRACK_PASSWORD to require login. Unset = open (localhost mode).
Session cookie is an HMAC over a server nonce derived from the password, so
changing the password invalidates all sessions. No user table needed —
this is a single-shop, shared-password model by design.
"""

import hashlib
import hmac
import os

COOKIE_NAME = "reftrack_session"
_EXEMPT_PREFIXES = ("/login", "/static/", "/health")


def password() -> str | None:
    return os.environ.get("REFTRACK_PASSWORD") or None


def enabled() -> bool:
    return password() is not None


def _expected_token() -> str:
    key = hashlib.sha256(("reftrack::" + (password() or "")).encode()).digest()
    return hmac.new(key, b"authenticated", hashlib.sha256).hexdigest()


def make_token(submitted_password: str) -> str | None:
    """Return a session token if the password is correct, else None."""
    real = password()
    if real is None or not hmac.compare_digest(submitted_password, real):
        return None
    return _expected_token()


def is_valid(token: str | None) -> bool:
    if token is None:
        return False
    return hmac.compare_digest(token, _expected_token())


def path_is_exempt(path: str) -> bool:
    return path == "/health" or any(
        path == p or path.startswith(p) for p in _EXEMPT_PREFIXES
    )
