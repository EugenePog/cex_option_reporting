"""Password hashing via the `bcrypt` library directly.

(We use bcrypt directly rather than passlib — passlib 1.7.x is unmaintained and breaks with
bcrypt >= 4.1.) bcrypt only considers the first 72 bytes of a password, and bcrypt 5.x raises on
longer input, so we truncate to 72 bytes on both hash and verify for consistency.
"""
from __future__ import annotations

import bcrypt

_MAX = 72


def hash_password(plain: str) -> str:
    pw = plain.encode("utf-8")[:_MAX]
    return bcrypt.hashpw(pw, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8")[:_MAX], hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False
