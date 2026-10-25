"""Password hashing with stdlib scrypt. Manage users with `python -m app.auth.users`."""

import base64
import hashlib
import hmac
import os

# scrypt cost parameters (~16 MB, a few tens of ms per check).
N, R, P = 2**14, 8, 1


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P)
    return f"scrypt:{N}:{R}:{P}:{_b64(salt)}:{_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, n, r, p, salt, digest = encoded.split(":")
        if algo != "scrypt":
            return False
        expected = _unb64(digest)
        actual = hashlib.scrypt(
            password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=len(expected)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Checked when the username is wrong, so a bad username costs the same time as a bad password.
DUMMY_HASH = hash_password(os.urandom(16).hex())

