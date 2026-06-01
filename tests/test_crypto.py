"""AES-GCM token encryption round-trip + failure modes."""

from __future__ import annotations

import pytest

from hermes_inbox_organizer import crypto


def test_roundtrip() -> None:
    key = crypto.generate_key()
    blob = crypto.encrypt("hello refresh-token", key)
    assert blob != "hello refresh-token"
    assert crypto.decrypt(blob, key) == "hello refresh-token"


def test_wrong_key_cannot_decrypt() -> None:
    blob = crypto.encrypt("secret", crypto.generate_key())
    with pytest.raises(Exception):
        crypto.decrypt(blob, crypto.generate_key())


def test_bad_key_length_rejected() -> None:
    with pytest.raises(ValueError):
        crypto.encrypt("x", "deadbeef")  # not 32 bytes


def test_each_encrypt_uses_fresh_nonce() -> None:
    key = crypto.generate_key()
    assert crypto.encrypt("x", key) != crypto.encrypt("x", key)
