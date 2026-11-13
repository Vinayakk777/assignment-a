from __future__ import annotations

import base64
import os

import pytest

from au_ledger.config import (
    ENV_ENC_KEY,
    ENV_HMAC_KEY,
    ConfigError,
    KeyConfig,
    generate_keypair,
    load_env_file,
    write_env_file,
)
from au_ledger.crypto import (
    CryptoError,
    PhoneHasher,
    PiiCipher,
    canonical_phone,
    mask_phone,
)


@pytest.mark.parametrize(
    "raw",
    [
        "9876543210",
        "+91 98765 43210",
        "09876543210",
        "91-98765-43210",
        "(+91) 9876543210",
        "+919876543210",
    ],
)
def test_canonical_phone_variants(raw):
    assert canonical_phone(raw) == "+919876543210"


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "987654321", "5876543210", "022-1234-5678", "N/A", "+1 415 555 0134", "9" * 13],
)
def test_canonical_phone_rejects_invalid(raw):
    with pytest.raises(ValueError):
        canonical_phone(raw)


def test_mask_phone():
    assert mask_phone("+91 98765 43210") == "+91******3210"


def test_hmac_is_deterministic_and_keyed(hasher):
    a = hasher.blind_index("9876543210")
    b = hasher.blind_index("+91 98765 43210")
    assert a == b
    assert len(a) == 64
    assert "9876543210" not in a
    other = PhoneHasher(b"X" * 32)
    assert other.blind_index("9876543210") != a


def test_aes_gcm_round_trip_and_randomisation(cipher):
    token1 = cipher.encrypt("Asha Rao", field_name="customer_name")
    token2 = cipher.encrypt("Asha Rao", field_name="customer_name")
    assert token1.startswith("v1:")
    assert token1 != token2
    assert cipher.decrypt(token1, field_name="customer_name") == "Asha Rao"
    assert cipher.decrypt(token2, field_name="customer_name") == "Asha Rao"


def test_aes_gcm_rejects_wrong_field_and_tampering(cipher):
    token = cipher.encrypt("+919876543210", field_name="phone")
    with pytest.raises(CryptoError):
        cipher.decrypt(token, field_name="customer_name")
    tampered = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    with pytest.raises(CryptoError):
        cipher.decrypt(tampered, field_name="phone")


def test_key_config_from_environment(monkeypatch):
    enc, hmac = generate_keypair()
    monkeypatch.setenv(ENV_ENC_KEY, enc)
    monkeypatch.setenv(ENV_HMAC_KEY, hmac)
    keys = KeyConfig.from_env()
    assert len(keys.enc_key) == 32
    assert len(keys.hmac_key) == 32
    assert keys.enc_key != keys.hmac_key


def test_missing_or_invalid_keys_are_rejected(monkeypatch):
    monkeypatch.delenv(ENV_ENC_KEY, raising=False)
    monkeypatch.delenv(ENV_HMAC_KEY, raising=False)
    with pytest.raises(ConfigError):
        KeyConfig.from_env()
    monkeypatch.setenv(ENV_ENC_KEY, base64.b64encode(b"short").decode())
    monkeypatch.setenv(ENV_HMAC_KEY, base64.b64encode(b"x" * 32).decode())
    with pytest.raises(ConfigError):
        KeyConfig.from_env()


def test_env_file_round_trip_and_no_overwrite(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_ENC_KEY, raising=False)
    monkeypatch.delenv(ENV_HMAC_KEY, raising=False)
    enc, hmac = generate_keypair()
    target = write_env_file(tmp_path / ".env", enc, hmac)
    assert target.exists()
    assert target.read_text(encoding="utf-8").count("AU_PII_") == 2
    with pytest.raises(ConfigError):
        write_env_file(target, enc, hmac)
    load_env_file(target)
    keys = KeyConfig.from_env()
    assert len(keys.enc_key) == 32
    assert len(keys.hmac_key) == 32
