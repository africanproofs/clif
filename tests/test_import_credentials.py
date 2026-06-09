"""clif import-credentials — consumer side of the fwd credential handoff (ADR-0001).

Validated against the pinned v1 bundle SHAPE via hand-built fixtures. End-to-end
verification against a REAL fwd-emitted bundle is PENDING (fwd bundle-emission
unbuilt)."""

import json
import stat
from datetime import datetime, timedelta, timezone

from typer.testing import CliRunner

from clif.cli import app
from clif.config import Settings, capabilities

SECRET_A = "fwd_live_claim_SECRET_VALUE_A"
SECRET_B = "fwd_live_fspsign_SECRET_VALUE_B"
SECRET_C = "fwd_live_fspsubmit_SECRET_VALUE_C"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _settings(**kw) -> Settings:
    base = dict(
        _env_file=None,
        network="songbird",
        fwd_wallet_name="claimer-songbird",
        fsp_signing_wallet_name="fsp-sign-songbird",
        fsp_sender_wallet_name="fsp-sender-songbird",
    )
    base.update(kw)
    return Settings(**base)


def _patch_settings(monkeypatch, **kw):
    monkeypatch.setattr("clif.cli.load_settings", lambda: _settings(**kw))


def _bundle(network="songbird", caps=None, **overrides) -> dict:
    """A valid v1 bundle for `network`, granting all of clif's governed capabilities."""
    if caps is None:
        s = _settings(network=network)
        secret_by_role = {"claim": SECRET_A, "fsp-sign": SECRET_B, "fsp-submit": SECRET_C}
        caps = [
            {
                "capability_id": c.capability_id,
                "caller_token_env": c.caller_token_env,
                "caller_token": secret_by_role[c.role],
                "wallet_name": c.wallet_name,
            }
            for c in capabilities(s)
        ]
    b = {
        "version": 1,
        "consumer": "clif",
        "network": network,
        "issued_at": _iso(datetime.now(timezone.utc) - timedelta(minutes=1)),
        "expires_at": _iso(datetime.now(timezone.utc) + timedelta(minutes=10)),
        "capabilities": caps,
    }
    b.update(overrides)
    return b


def _write_bundle(tmp_path, bundle, name="bundle.json", mode=0o600):
    p = tmp_path / name
    p.write_text(json.dumps(bundle))
    p.chmod(mode)  # the bundle MUST be 0600 (consumer-contract-v1 §4.2)
    return p


def _invoke(monkeypatch, bundle_path, env_dir, *extra, **settings_kw):
    _patch_settings(monkeypatch, **settings_kw)
    return CliRunner().invoke(
        app,
        ["import-credentials", "--bundle", str(bundle_path), "--env-dir", str(env_dir), *extra],
    )


def _env_lines(env_dir, network="songbird") -> dict[str, str]:
    text = (env_dir / f".env.{network}").read_text()
    out = {}
    for line in text.splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v
    return out


def test_valid_bundle_writes_the_right_env_vars_by_name(monkeypatch, tmp_path):
    env_dir = tmp_path / "clifdir"
    bundle_path = _write_bundle(tmp_path, _bundle())
    result = _invoke(monkeypatch, bundle_path, env_dir)
    assert result.exit_code == 0, result.output

    written = _env_lines(env_dir)
    # The three governed env var NAMES are written with the bundle's secret values.
    assert written["FWD_CALLER_TOKEN"] == SECRET_A
    assert written["FSP_SIGN_CALLER_TOKEN"] == SECRET_B
    assert written["FSP_SUBMIT_CALLER_TOKEN"] == SECRET_C


def test_no_token_value_appears_in_output(monkeypatch, tmp_path):
    env_dir = tmp_path / "clifdir"
    bundle_path = _write_bundle(tmp_path, _bundle())
    result = _invoke(monkeypatch, bundle_path, env_dir)
    assert result.exit_code == 0, result.output
    for secret in (SECRET_A, SECRET_B, SECRET_C):
        assert secret not in result.output  # never log/print a token value
    # but the env var NAMES and capability_ids ARE reported
    assert "FWD_CALLER_TOKEN" in result.output
    assert "clif/songbird/claim" in result.output


def test_no_token_value_appears_in_json_output(monkeypatch, tmp_path):
    env_dir = tmp_path / "clifdir"
    bundle_path = _write_bundle(tmp_path, _bundle())
    result = _invoke(monkeypatch, bundle_path, env_dir, "--json")
    assert result.exit_code == 0, result.output
    for secret in (SECRET_A, SECRET_B, SECRET_C):
        assert secret not in result.output
    payload = json.loads(result.output)
    assert payload["consumer"] == "clif"
    assert payload["network"] == "songbird"
    assert payload["imported"] == 3
    assert payload["capability_ids"] == [
        "clif/songbird/claim",
        "clif/songbird/fsp-sign",
        "clif/songbird/fsp-submit",
    ]
    assert payload["env_vars_written"] == [
        "FWD_CALLER_TOKEN",
        "FSP_SIGN_CALLER_TOKEN",
        "FSP_SUBMIT_CALLER_TOKEN",
    ]
    assert "[bold" not in result.output  # clean JSON, no rich markup


def test_bundle_is_consumed_deleted_after_import(monkeypatch, tmp_path):
    env_dir = tmp_path / "clifdir"
    bundle_path = _write_bundle(tmp_path, _bundle())
    assert bundle_path.is_file()
    result = _invoke(monkeypatch, bundle_path, env_dir, "--json")
    assert result.exit_code == 0, result.output
    assert not bundle_path.exists()  # one-shot: consumed
    assert json.loads(result.output)["bundle_consumed"] is True


def test_idempotent_rerun_replaces_in_place(monkeypatch, tmp_path):
    env_dir = tmp_path / "clifdir"
    # First import.
    b1 = _write_bundle(tmp_path, _bundle(), name="b1.json")
    assert _invoke(monkeypatch, b1, env_dir).exit_code == 0

    # Re-import (rotation): same id, a NEW token value. Re-runnable = the rotation channel.
    rotated = "fwd_live_claim_ROTATED_VALUE"
    s = _settings()
    caps = [
        {
            "capability_id": c.capability_id,
            "caller_token_env": c.caller_token_env,
            "caller_token": rotated if c.role == "claim" else "x",
            "wallet_name": c.wallet_name,
        }
        for c in capabilities(s)
    ]
    b2 = _write_bundle(tmp_path, _bundle(caps=caps), name="b2.json")
    assert _invoke(monkeypatch, b2, env_dir).exit_code == 0

    written = _env_lines(env_dir)
    assert written["FWD_CALLER_TOKEN"] == rotated  # replaced in place, not duplicated
    # exactly one FWD_CALLER_TOKEN line (no append-duplication)
    text = (env_dir / ".env.songbird").read_text()
    assert text.count("FWD_CALLER_TOKEN=") == 1


def test_ungoverned_capability_id_rejected(monkeypatch, tmp_path):
    env_dir = tmp_path / "clifdir"
    bad = _bundle(
        caps=[
            {
                "capability_id": "clif/songbird/rogue",
                "caller_token_env": "FWD_CALLER_TOKEN",
                "caller_token": SECRET_A,
                "wallet_name": "x",
            }
        ]
    )
    bundle_path = _write_bundle(tmp_path, bad)
    result = _invoke(monkeypatch, bundle_path, env_dir)
    assert result.exit_code == 2, result.output
    assert "ungoverned" in result.output
    assert not (env_dir / ".env.songbird").exists()  # nothing written
    assert bundle_path.is_file()  # not consumed on rejection


def test_expired_bundle_rejected(monkeypatch, tmp_path):
    env_dir = tmp_path / "clifdir"
    expired = _bundle(expires_at=_iso(datetime.now(timezone.utc) - timedelta(minutes=1)))
    bundle_path = _write_bundle(tmp_path, expired)
    result = _invoke(monkeypatch, bundle_path, env_dir)
    assert result.exit_code == 2, result.output
    assert "expired" in result.output
    assert bundle_path.is_file()  # one-shot bundle preserved for re-issue/retry


def test_wrong_consumer_rejected(monkeypatch, tmp_path):
    env_dir = tmp_path / "clifdir"
    bundle_path = _write_bundle(tmp_path, _bundle(consumer="someone-else"))
    result = _invoke(monkeypatch, bundle_path, env_dir)
    assert result.exit_code == 2, result.output
    assert "consumer" in result.output


def test_wrong_version_rejected(monkeypatch, tmp_path):
    env_dir = tmp_path / "clifdir"
    bundle_path = _write_bundle(tmp_path, _bundle(version=2))
    result = _invoke(monkeypatch, bundle_path, env_dir)
    assert result.exit_code == 2, result.output
    assert "version" in result.output


def test_caller_token_env_mismatch_rejected(monkeypatch, tmp_path):
    env_dir = tmp_path / "clifdir"
    s = _settings()
    claim = {c.role: c for c in capabilities(s)}["claim"]
    bad = _bundle(
        caps=[
            {
                "capability_id": claim.capability_id,
                "caller_token_env": "WRONG_ENV_NAME",  # not what clif reads
                "caller_token": SECRET_A,
                "wallet_name": claim.wallet_name,
            }
        ]
    )
    bundle_path = _write_bundle(tmp_path, bad)
    result = _invoke(monkeypatch, bundle_path, env_dir)
    assert result.exit_code == 2, result.output
    assert "caller_token_env" in result.output


def test_missing_bundle_file_exits_1(monkeypatch, tmp_path):
    _patch_settings(monkeypatch)
    result = CliRunner().invoke(
        app,
        [
            "import-credentials",
            "--bundle",
            str(tmp_path / "does-not-exist.json"),
            "--env-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 1, result.output
    assert "not found" in result.output


def test_governed_env_var_writes_into_network_specific_file(monkeypatch, tmp_path):
    # A flare bundle writes .env.flare (not .env.songbird), keyed by the bundle network.
    env_dir = tmp_path / "clifdir"
    bundle_path = _write_bundle(tmp_path, _bundle(network="flare"))
    result = _invoke(monkeypatch, bundle_path, env_dir, network="flare")
    assert result.exit_code == 0, result.output
    assert (env_dir / ".env.flare").is_file()
    assert _env_lines(env_dir, "flare")["FWD_CALLER_TOKEN"] == SECRET_A


# ---- regression tests for the confirmed review findings ----


def test_non_0600_bundle_rejected(monkeypatch, tmp_path):
    # The bundle carries plaintext token values; a group/world-readable bundle is refused.
    env_dir = tmp_path / "clifdir"
    bundle_path = _write_bundle(tmp_path, _bundle(), mode=0o644)
    result = _invoke(monkeypatch, bundle_path, env_dir)
    assert result.exit_code == 2, result.output
    assert "0600" in result.output
    assert bundle_path.is_file()  # not consumed on rejection
    assert not (env_dir / ".env.songbird").exists()


def test_newline_in_token_value_rejected_no_env_injection(monkeypatch, tmp_path):
    # A token value with a newline would inject an extra .env assignment; refuse it.
    env_dir = tmp_path / "clifdir"
    claim = {c.role: c for c in capabilities(_settings())}["claim"]
    bad = _bundle(
        caps=[
            {
                "capability_id": claim.capability_id,
                "caller_token_env": claim.caller_token_env,
                "caller_token": "fwd_live_ok\nCLAIM_RECIPIENT_ADDRESS=0xATTACKER",
                "wallet_name": claim.wallet_name,
            }
        ]
    )
    bundle_path = _write_bundle(tmp_path, bad)
    result = _invoke(monkeypatch, bundle_path, env_dir)
    assert result.exit_code == 2, result.output
    assert "illegal characters" in result.output
    assert bundle_path.is_file()
    assert not (env_dir / ".env.songbird").exists()  # nothing injected


def test_rotation_collapses_preexisting_duplicate(monkeypatch, tmp_path):
    # A pre-existing DUPLICATE assignment must collapse to one canonical line with the
    # NEW value — pydantic reads the last, so a stale dup would keep a revoked token live.
    env_dir = tmp_path / "clifdir"
    env_dir.mkdir()
    (env_dir / ".env.songbird").write_text(
        "FWD_CALLER_TOKEN=STALE_FIRST\nOTHER=keep\nFWD_CALLER_TOKEN=STALE_LAST\n"
    )
    bundle_path = _write_bundle(tmp_path, _bundle())
    result = _invoke(monkeypatch, bundle_path, env_dir)
    assert result.exit_code == 0, result.output
    text = (env_dir / ".env.songbird").read_text()
    assert text.count("FWD_CALLER_TOKEN=") == 1  # collapsed
    assert _env_lines(env_dir)["FWD_CALLER_TOKEN"] == SECRET_A  # the new value
    assert _env_lines(env_dir)["OTHER"] == "keep"  # unrelated lines preserved


def test_written_env_is_mode_0600(monkeypatch, tmp_path):
    env_dir = tmp_path / "clifdir"
    bundle_path = _write_bundle(tmp_path, _bundle())
    assert _invoke(monkeypatch, bundle_path, env_dir).exit_code == 0
    assert stat.S_IMODE((env_dir / ".env.songbird").stat().st_mode) == 0o600


def test_bundle_preserved_on_env_write_failure(monkeypatch, tmp_path):
    # If the env write fails after validation, the one-shot bundle is left intact.
    env_dir = tmp_path / "clifdir"
    bundle_path = _write_bundle(tmp_path, _bundle())

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("clif.cli.import_credentials", _boom)
    result = _invoke(monkeypatch, bundle_path, env_dir)
    assert result.exit_code == 1, result.output
    assert bundle_path.is_file()  # NOT consumed — retry possible


def test_missing_caller_token_rejected(monkeypatch, tmp_path):
    env_dir = tmp_path / "clifdir"
    claim = {c.role: c for c in capabilities(_settings())}["claim"]
    bad = _bundle(
        caps=[
            {
                "capability_id": claim.capability_id,
                "caller_token_env": claim.caller_token_env,
                "caller_token": "",  # empty
                "wallet_name": claim.wallet_name,
            }
        ]
    )
    bundle_path = _write_bundle(tmp_path, bad)
    result = _invoke(monkeypatch, bundle_path, env_dir)
    assert result.exit_code == 2, result.output
    assert "caller_token" in result.output
    assert bundle_path.is_file()


def test_empty_capabilities_rejected(monkeypatch, tmp_path):
    env_dir = tmp_path / "clifdir"
    bundle_path = _write_bundle(tmp_path, _bundle(caps=[]))
    result = _invoke(monkeypatch, bundle_path, env_dir)
    assert result.exit_code == 2, result.output
    assert "capabilities" in result.output


def test_malformed_expires_at_rejected(monkeypatch, tmp_path):
    env_dir = tmp_path / "clifdir"
    bundle_path = _write_bundle(tmp_path, _bundle(expires_at="not-a-date"))
    result = _invoke(monkeypatch, bundle_path, env_dir)
    assert result.exit_code == 2, result.output
    assert "ISO8601" in result.output


def test_json_error_on_rejection_is_machine_readable(monkeypatch, tmp_path):
    env_dir = tmp_path / "clifdir"
    bundle_path = _write_bundle(tmp_path, _bundle(version=2))
    result = _invoke(monkeypatch, bundle_path, env_dir, "--json")
    assert result.exit_code == 2, result.output
    payload = json.loads(result.output)  # parseable JSON on the error path
    assert payload["ok"] is False
    assert payload["consumer"] == "clif"
    assert "[bold" not in result.output
