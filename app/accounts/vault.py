"""Encryption at rest for broker credentials and access tokens.

AES-256-GCM with the key in AUTOTRADE_CREDENTIALS_KEY (never in the database). Each value is
stored as "v1:<key id>:<base64 nonce + ciphertext>" and is bound to where it lives (e.g.
"broker_account:7:credentials") as associated data: a ciphertext copied onto another account
or field fails to decrypt instead of handing that account someone else's credentials.

Keys:
    python -m app.accounts.vault generate-key     # a new random key for .env
    python -m app.accounts.vault rotate           # re-encrypt everything with the current key
To rotate: move the old key into AUTOTRADE_CREDENTIALS_OLD_KEYS, put the new one in
AUTOTRADE_CREDENTIALS_KEY, run `rotate`, then drop the old key.
"""

import base64
import hashlib
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings

VERSION = "v1"
NONCE_BYTES = 12


class VaultError(Exception):
    """A stored secret can't be read (no key configured, wrong key, or tampered with)."""


class VaultNotConfigured(VaultError):
    pass


def generate_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")


def _decode_key(text: str) -> bytes:
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except ValueError:
        raw = b""
    if len(raw) != 32:
        raise ValueError("A credentials key is 32 random bytes, base64url-encoded "
                         "(python -m app.accounts.vault generate-key)")
    return raw


def _key_id(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:8]


class Vault:
    def __init__(self, key: str, old_keys: list[str] = ()) -> None:
        self._keys: dict[str, AESGCM] = {}
        self.primary: str | None = None
        for i, text in enumerate([key, *old_keys]):
            if not text:
                continue
            raw = _decode_key(text)
            kid = _key_id(raw)
            self._keys[kid] = AESGCM(raw)
            if i == 0:
                self.primary = kid

    @property
    def configured(self) -> bool:
        return self.primary is not None

    def encrypt(self, plaintext: bytes, context: str) -> str:
        if self.primary is None:
            raise VaultNotConfigured(
                "Broker credentials can't be stored: the server has no AUTOTRADE_CREDENTIALS_KEY")
        nonce = os.urandom(NONCE_BYTES)
        sealed = self._keys[self.primary].encrypt(nonce, plaintext, context.encode())
        return f"{VERSION}:{self.primary}:{base64.urlsafe_b64encode(nonce + sealed).decode()}"

    def decrypt(self, token: str, context: str) -> bytes:
        try:
            version, kid, body = token.split(":", 2)
            blob = base64.urlsafe_b64decode(body)
        except ValueError:
            raise VaultError("Stored secret is malformed") from None
        if version != VERSION:
            raise VaultError(f"Stored secret has unknown format {version!r}")
        key = self._keys.get(kid)
        if key is None:
            if not self.configured:
                raise VaultNotConfigured("The server has no AUTOTRADE_CREDENTIALS_KEY to read stored credentials")
            raise VaultError(f"Stored secret was encrypted with key {kid}, which this server "
                             "doesn't have (add it to AUTOTRADE_CREDENTIALS_OLD_KEYS)")
        try:
            return key.decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], context.encode())
        except InvalidTag:
            raise VaultError("Stored secret could not be decrypted (wrong key or tampered with)") from None

    def encrypt_json(self, value: dict, context: str) -> str:
        return self.encrypt(json.dumps(value, separators=(",", ":")).encode(), context)

    def decrypt_json(self, token: str, context: str) -> dict:
        return json.loads(self.decrypt(token, context))

    def is_current(self, token: str) -> bool:
        """Encrypted with the primary key (nothing to rotate)."""
        return token.split(":", 2)[1:2] == [self.primary]


def vault() -> Vault:
    """The vault for the current settings."""
    return Vault(settings.credentials_key, settings.credentials_old_keys)


def _main() -> None:
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(prog="python -m app.accounts.vault")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("generate-key", help="print a new random AUTOTRADE_CREDENTIALS_KEY")
    sub.add_parser("rotate", help="re-encrypt every stored secret with AUTOTRADE_CREDENTIALS_KEY")
    args = parser.parse_args()

    if args.command == "generate-key":
        print(generate_key())
        return

    async def rotate() -> int:
        from app.accounts.store import reencrypt_all
        from app.core import db

        await db.init(settings.database_url)
        try:
            return await reencrypt_all()
        finally:
            await db.close()

    print(f"Re-encrypted {asyncio.run(rotate())} secret(s) with the current key.")


if __name__ == "__main__":
    _main()
