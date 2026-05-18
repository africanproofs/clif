"""clif must refuse to run if any *PRIVATE_KEY* is in the environment."""

import pytest

from clif.config import KeylessViolation, assert_keyless


def test_keyless_ok_when_no_private_key(monkeypatch):
    for k in list(__import__("os").environ):
        if "PRIVATE_KEY" in k.upper():
            monkeypatch.delenv(k, raising=False)
    assert_keyless()  # no raise


def test_keyless_raises_on_executor_private_key(monkeypatch):
    monkeypatch.setenv("CLAIM_EXECUTOR_PRIVATE_KEY", "0xdeadbeef")
    with pytest.raises(KeylessViolation) as ei:
        assert_keyless()
    assert "CLAIM_EXECUTOR_PRIVATE_KEY" in str(ei.value)


def test_keyless_raises_on_any_private_key_variant(monkeypatch):
    monkeypatch.setenv("SOME_SIGNING_PRIVATE_KEY", "x")
    with pytest.raises(KeylessViolation):
        assert_keyless()
