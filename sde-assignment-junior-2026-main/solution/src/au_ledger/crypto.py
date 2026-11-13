"""PII protection primitives.

- Phone matching uses deterministic HMAC-SHA256 over the canonical Indian
  mobile number. HMAC hides the number from offline enumeration and allows
  equality matching without storing plaintext.
- All fields marked ``pii: true`` use AES-256-GCM with a random 96-bit nonce.
  The token is self-describing (``v1:`` prefix) and authenticated with the
  field name as associated data. Ciphertext is randomized, so two encryptions
  of the same value differ; identity matching never relies on ciphertext.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re
from typing import Final

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .errors import CryptoError

KEY_BYTES: Final = 32
NONCE_BYTES: Final = 12
TOKEN_PREFIX: Final = "v1:"
TOKEN_VERSION: Final = b"\x01"
_FIELD_NAME_RE: Final = re.compile(r"^[A-Za-z0-9_.-]+$")


def canonical_phone(raw: str) -> str:
    """Return ``+91XXXXXXXXXX`` or raise ``ValueError``.

    Rules from the assignment: strip every non-digit; drop a leading ``91``
    from a 12-digit value or a leading ``0`` from an 11-digit value; require
    exactly 10 digits starting with 6, 7, 8 or 9.
    """
    if not isinstance(raw, str):
        raise ValueError("phone value must be a string")
    digits = re.sub(r"[^0-9]", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] not in "6789":
        # Never include the supplied value in the exception.
        raise ValueError("invalid Indian mobile number")
    return "+91" + digits


def mask_phone(raw: str) -> str:
    """Return the report-safe ``+91******`` plus last four digits."""
    canonical = canonical_phone(raw)
    return "+91******" + canonical[-4:]


def redact_phone(raw: str) -> str:
    """Alias used by logging code; never log an unmasked phone."""
    return mask_phone(raw)


class PhoneHasher:
    """Deterministic blind index for phone matching."""

    DOMAIN: Final = b"au-ledger:phone:v1:"

    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_BYTES:
            raise CryptoError(f"HMAC key must be exactly {KEY_BYTES} bytes")
        self._key = bytes(key)

    def blind_index(self, phone: str) -> str:
        canonical = canonical_phone(phone)
        digest = hmac.new(
            self._key,
            self.DOMAIN + canonical.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        return digest


class PiiCipher:
    """AES-256-GCM encryption for PII columns."""

    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_BYTES:
            raise CryptoError(f"encryption key must be exactly {KEY_BYTES} bytes")
        self._aead = AESGCM(bytes(key))

    @staticmethod
    def _aad(field_name: str) -> bytes:
        if not isinstance(field_name, str) or not _FIELD_NAME_RE.fullmatch(field_name):
            raise ValueError("field name must be a simple identifier")
        return f"au-ledger|{field_name}|v1".encode("utf-8")

    def encrypt(self, plaintext: str, *, field_name: str) -> str:
        if not isinstance(plaintext, str):
            raise TypeError("PII value must be a string")
        aad = self._aad(field_name)
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = self._aead.encrypt(nonce, plaintext.encode("utf-8"), aad)
        payload = base64.b64encode(TOKEN_VERSION + nonce + ciphertext).decode("ascii")
        return TOKEN_PREFIX + payload

    def decrypt(self, token: str, *, field_name: str) -> str:
        if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX):
            raise CryptoError("unsupported PII token format")
        aad = self._aad(field_name)
        try:
            raw = base64.b64decode(token[len(TOKEN_PREFIX) :], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise CryptoError("invalid PII token encoding") from exc
        if len(raw) < 1 + NONCE_BYTES + 16:
            raise CryptoError("invalid PII token length")
        if raw[:1] != TOKEN_VERSION:
            raise CryptoError("unsupported PII token version")
        nonce = raw[1 : 1 + NONCE_BYTES]
        ciphertext = raw[1 + NONCE_BYTES :]
        try:
            plaintext = self._aead.decrypt(nonce, ciphertext, aad)
        except Exception as exc:  # cryptography raises InvalidTag
            raise CryptoError("PII authentication failed") from exc
        try:
            return plaintext.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CryptoError("PII token is not valid UTF-8") from exc
