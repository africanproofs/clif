"""clif must refuse to run if any *PRIVATE_KEY* is in the environment
**or** in the configured .env source file (STOP-SHIP #1)."""

import pytest

from clif.config import KeylessViolation, assert_keyless, load_settings


def test_keyless_ok_when_no_private_key(monkeypatch):
    for k in list(__import__("os").environ):
        if "PRIVATE_KEY" in k.upper():
            monkeypatch.delenv(k, raising=False)
    assert_keyless(env_file=None)  # clean env, no file → no raise


def test_keyless_ok_clean_env_and_clean_file(monkeypatch, tmp_path):
    for k in list(__import__("os").environ):
        if "PRIVATE_KEY" in k.upper():
            monkeypatch.delenv(k, raising=False)
    p = tmp_path / ".env"
    p.write_text(
        "NETWORK=coston2\n"
        "FWD_CALLER_TOKEN=fwd_live_x\n"
        "# COMMENTED_PRIVATE_KEY=ignored\n"  # a comment is not an offender
    )
    assert_keyless(env_file=str(p))  # clean env + clean file → no raise


def test_keyless_raises_on_private_key_in_env_FILE(tmp_path, monkeypatch):
    """STOP-SHIP #1: a .env-resident *PRIVATE_KEY* MUST refuse — pydantic
    would otherwise silently ignore the unknown key and run green."""
    for k in list(__import__("os").environ):
        if "PRIVATE_KEY" in k.upper():
            monkeypatch.delenv(k, raising=False)
    p = tmp_path / ".env"
    p.write_text("NETWORK=coston2\nCLAIM_EXECUTOR_PRIVATE_KEY=dummy\n")
    with pytest.raises(KeylessViolation) as ei:
        assert_keyless(env_file=str(p))
    msg = str(ei.value)
    assert "CLAIM_EXECUTOR_PRIVATE_KEY" in msg and "file" in msg


def test_keyless_file_scan_handles_export_prefix(tmp_path, monkeypatch):
    for k in list(__import__("os").environ):
        if "PRIVATE_KEY" in k.upper():
            monkeypatch.delenv(k, raising=False)
    p = tmp_path / ".env"
    p.write_text("export SIGNING_POLICY_PRIVATE_KEY=0xabc\n")
    with pytest.raises(KeylessViolation):
        assert_keyless(env_file=str(p))


def test_load_settings_refuses_env_file_private_key(tmp_path, monkeypatch):
    """End-to-end: load_settings() (CLI entrypoint) fails closed on a
    .env-resident key — clif exits non-zero, never runs green."""
    for k in list(__import__("os").environ):
        if "PRIVATE_KEY" in k.upper():
            monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("CLAIM_EXECUTOR_PRIVATE_KEY=dummy\n")
    with pytest.raises(KeylessViolation):
        load_settings()


def test_keyless_raises_on_executor_private_key(monkeypatch):
    monkeypatch.setenv("CLAIM_EXECUTOR_PRIVATE_KEY", "0xdeadbeef")
    with pytest.raises(KeylessViolation) as ei:
        assert_keyless()
    assert "CLAIM_EXECUTOR_PRIVATE_KEY" in str(ei.value)


def test_keyless_raises_on_any_private_key_variant(monkeypatch):
    monkeypatch.setenv("SOME_SIGNING_PRIVATE_KEY", "x")
    with pytest.raises(KeylessViolation):
        assert_keyless()
