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
        secret_by_role = {"ftso-reward": SECRET_A, "fsp-sign": SECRET_B, "fsp-submit": SECRET_C}
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
        "consumer": "claim",
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
    assert "claim/songbird/ftso-reward" in result.output


def test_no_token_value_appears_in_json_output(monkeypatch, tmp_path):
    env_dir = tmp_path / "clifdir"
    bundle_path = _write_bundle(tmp_path, _bundle())
    result = _invoke(monkeypatch, bundle_path, env_dir, "--json")
    assert result.exit_code == 0, result.output
    for secret in (SECRET_A, SECRET_B, SECRET_C):
        assert secret not in result.output
    payload = json.loads(result.output)
    assert payload["consumer"] == "claim"
    assert payload["network"] == "songbird"
    assert payload["imported"] == 3
    assert payload["capability_ids"] == [
        "claim/songbird/ftso-reward",
        "claim/songbird/fsp-sign",
        "claim/songbird/fsp-submit",
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
            "caller_token": rotated if c.role == "ftso-reward" else "x",
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
                "capability_id": "claim/songbird/rogue",
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


def test_unsupported_version_rejected(monkeypatch, tmp_path):
    # v1 + v2 are supported; an unknown version (3) is terminal.
    env_dir = tmp_path / "clifdir"
    bundle_path = _write_bundle(tmp_path, _bundle(version=3))
    result = _invoke(monkeypatch, bundle_path, env_dir)
    assert result.exit_code == 2, result.output
    assert "version" in result.output


def test_caller_token_env_mismatch_rejected(monkeypatch, tmp_path):
    env_dir = tmp_path / "clifdir"
    s = _settings()
    claim = {c.role: c for c in capabilities(s)}["ftso-reward"]
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
    claim = {c.role: c for c in capabilities(_settings())}["ftso-reward"]
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
    claim = {c.role: c for c in capabilities(_settings())}["ftso-reward"]
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
    bundle_path = _write_bundle(tmp_path, _bundle(version=99))
    result = _invoke(monkeypatch, bundle_path, env_dir, "--json")
    assert result.exit_code == 2, result.output
    payload = json.loads(result.output)  # parseable JSON on the error path
    assert payload["ok"] is False
    assert payload["consumer"] == "claim"
    assert "[bold" not in result.output


# ===== v2 — the COMPLETE handoff (tokens + wallet-envs + config) ==================
# ADR-0003 Unit 4b: the bundle sources clif's ENTIRE .env.<net>. v1 (tokens only)
# stays accepted for back-compat (covered above).

CONFIG_OK = {
    "NETWORK": "songbird",
    "FWD_ENDPOINT": "http://fwd:8080",
    "CLIF_STATE_DIR": ".clif-state/songbird",
    "CLAIM_RECIPIENT_ADDRESS": "0x7c3579aB3E647395c96a1EfC98aF9A31C5Ecc294",
    "IDENTITY_ADDRESS": "0xcf3A3e5797A960C67e0E4B23D4594246ffB9d935",
    "WRAP_REWARDS": "true",
    "FSP_AUTO_ENABLED": "true",
    "SIGNING_POLICY_ADDRESS": "",  # a blank config value is legitimate (no DIRECT rewards)
}


def _v2_caps(network="songbird"):
    s = _settings(network=network)
    secret_by_role = {"ftso-reward": SECRET_A, "fsp-sign": SECRET_B, "fsp-submit": SECRET_C}
    return [
        {
            "capability_id": c.capability_id,
            "caller_token_env": c.caller_token_env,
            "caller_token": secret_by_role[c.role],
            "wallet_name": c.wallet_name,
        }
        for c in capabilities(s)
    ]


def _v2_bundle(network="songbird", caps=None, config=None, **overrides) -> dict:
    """A valid v2 bundle: per-cap tokens + wallet_name, plus a config section."""
    b = {
        "version": 2,
        "consumer": "claim",
        "network": network,
        "issued_at": _iso(datetime.now(timezone.utc) - timedelta(minutes=1)),
        "expires_at": _iso(datetime.now(timezone.utc) + timedelta(minutes=10)),
        "config": CONFIG_OK.copy() if config is None else config,
        "capabilities": _v2_caps(network) if caps is None else caps,
    }
    b.update(overrides)
    return b


def test_v2_writes_tokens_wallet_envs_and_config(monkeypatch, tmp_path):
    env_dir = tmp_path / "clifdir"
    bundle_path = _write_bundle(tmp_path, _v2_bundle())
    result = _invoke(monkeypatch, bundle_path, env_dir)
    assert result.exit_code == 0, result.output

    written = _env_lines(env_dir)
    # tokens (per-cap)
    assert written["FWD_CALLER_TOKEN"] == SECRET_A
    assert written["FSP_SIGN_CALLER_TOKEN"] == SECRET_B
    assert written["FSP_SUBMIT_CALLER_TOKEN"] == SECRET_C
    # wallet-envs (per-cap; the NAME is clif's, the value is the bundle's wallet_name)
    assert written["FWD_WALLET_NAME"] == "claimer-songbird"
    assert written["FSP_SIGNING_WALLET_NAME"] == "fsp-sign-songbird"
    assert written["FSP_SENDER_WALLET_NAME"] == "fsp-sender-songbird"
    # config section — the whole .env is now sourced from the bundle
    assert written["NETWORK"] == "songbird"
    assert written["FWD_ENDPOINT"] == "http://fwd:8080"
    assert written["WRAP_REWARDS"] == "true"
    assert written["FSP_AUTO_ENABLED"] == "true"
    assert written["SIGNING_POLICY_ADDRESS"] == ""  # blank config value preserved
    assert not bundle_path.exists()  # one-shot consumed


def test_v2_json_reports_version_wallet_and_config_names_never_values(monkeypatch, tmp_path):
    env_dir = tmp_path / "clifdir"
    bundle_path = _write_bundle(tmp_path, _v2_bundle())
    result = _invoke(monkeypatch, bundle_path, env_dir, "--json")
    assert result.exit_code == 0, result.output
    for secret in (SECRET_A, SECRET_B, SECRET_C):
        assert secret not in result.output  # never a token value
    payload = json.loads(result.output)
    assert payload["bundle_version"] == 2
    assert payload["env_vars_written"] == [
        "FWD_CALLER_TOKEN",
        "FSP_SIGN_CALLER_TOKEN",
        "FSP_SUBMIT_CALLER_TOKEN",
    ]
    assert payload["wallet_envs_written"] == [
        "FWD_WALLET_NAME",
        "FSP_SIGNING_WALLET_NAME",
        "FSP_SENDER_WALLET_NAME",
    ]
    # config key NAMES, sorted (deterministic); never values
    assert payload["config_keys_written"] == sorted(CONFIG_OK)
    assert payload["bundle_consumed"] is True


def test_v2_no_token_value_in_console_output(monkeypatch, tmp_path):
    env_dir = tmp_path / "clifdir"
    bundle_path = _write_bundle(tmp_path, _v2_bundle())
    result = _invoke(monkeypatch, bundle_path, env_dir)
    assert result.exit_code == 0, result.output
    for secret in (SECRET_A, SECRET_B, SECRET_C):
        assert secret not in result.output
    # NAMES + capability_ids ARE reported
    assert "FWD_WALLET_NAME" in result.output
    assert "claim/songbird/ftso-reward" in result.output
    assert "config: wrote" in result.output


def test_v2_unknown_config_key_rejected(monkeypatch, tmp_path):
    # The bundle MUST NOT inject an arbitrary env var — only clif's own config keys.
    env_dir = tmp_path / "clifdir"
    bad = _v2_bundle(config={**CONFIG_OK, "SOME_RANDOM_KEY": "x"})
    bundle_path = _write_bundle(tmp_path, bad)
    result = _invoke(monkeypatch, bundle_path, env_dir)
    assert result.exit_code == 2, result.output
    assert "ungoverned" in result.output and "SOME_RANDOM_KEY" in result.output
    assert not (env_dir / ".env.songbird").exists()  # nothing written
    assert bundle_path.is_file()  # not consumed on rejection


def test_v2_config_cannot_carry_a_caller_token_env(monkeypatch, tmp_path):
    # The secret token env-vars are EXCLUDED from the config allowlist — a token
    # may travel only via the guarded per-cap path, never the config section.
    env_dir = tmp_path / "clifdir"
    bad = _v2_bundle(config={**CONFIG_OK, "FWD_CALLER_TOKEN": "sneaky_token"})
    bundle_path = _write_bundle(tmp_path, bad)
    result = _invoke(monkeypatch, bundle_path, env_dir)
    assert result.exit_code == 2, result.output
    assert "ungoverned" in result.output
    assert "sneaky_token" not in result.output  # never echo the value


def test_v2_config_value_with_control_char_rejected(monkeypatch, tmp_path):
    # A newline in a config value would inject an extra .env assignment.
    env_dir = tmp_path / "clifdir"
    bad = _v2_bundle(config={**CONFIG_OK, "WRAP_REWARDS": "true\nIDENTITY_ADDRESS=0xATTACKER"})
    bundle_path = _write_bundle(tmp_path, bad)
    result = _invoke(monkeypatch, bundle_path, env_dir)
    assert result.exit_code == 2, result.output
    assert "illegal characters" in " ".join(result.output.split())  # tolerate rich line-wrap
    assert not (env_dir / ".env.songbird").exists()


def test_v2_config_private_key_name_refused(monkeypatch, tmp_path):
    env_dir = tmp_path / "clifdir"
    bad = _v2_bundle(config={**CONFIG_OK, "CLAIM_EXECUTOR_PRIVATE_KEY": "0xdead"})
    bundle_path = _write_bundle(tmp_path, bad)
    result = _invoke(monkeypatch, bundle_path, env_dir)
    assert result.exit_code == 2, result.output
    assert "PRIVATE_KEY" in result.output
    assert not (env_dir / ".env.songbird").exists()


def test_v2_config_private_key_value_refused(monkeypatch, tmp_path):
    # Even under a legit key, a PRIVATE_KEY-looking VALUE is refused (assert_keyless
    # only sees names; this guards the value too).
    env_dir = tmp_path / "clifdir"
    bad = _v2_bundle(config={**CONFIG_OK, "IDENTITY_ADDRESS": "MY_PRIVATE_KEY_0xdead"})
    bundle_path = _write_bundle(tmp_path, bad)
    result = _invoke(monkeypatch, bundle_path, env_dir)
    assert result.exit_code == 2, result.output
    assert "PRIVATE_KEY" in result.output


def test_v2_non_string_config_value_rejected(monkeypatch, tmp_path):
    env_dir = tmp_path / "clifdir"
    bad = _v2_bundle(config={**CONFIG_OK, "WRAP_REWARDS": True})  # JSON bool, not a string
    bundle_path = _write_bundle(tmp_path, bad)
    result = _invoke(monkeypatch, bundle_path, env_dir)
    assert result.exit_code == 2, result.output
    assert "must be a string" in result.output


def test_v2_missing_config_section_rejected(monkeypatch, tmp_path):
    env_dir = tmp_path / "clifdir"
    b = _v2_bundle()
    del b["config"]
    bundle_path = _write_bundle(tmp_path, b)
    result = _invoke(monkeypatch, bundle_path, env_dir)
    assert result.exit_code == 2, result.output
    assert "config" in result.output


def test_v2_capability_missing_wallet_name_rejected(monkeypatch, tmp_path):
    # The COMPLETE handoff must carry a wallet_name per cap (clif writes the wallet-env).
    env_dir = tmp_path / "clifdir"
    caps = _v2_caps()
    caps[0] = {**caps[0], "wallet_name": ""}  # blank
    bundle_path = _write_bundle(tmp_path, _v2_bundle(caps=caps))
    result = _invoke(monkeypatch, bundle_path, env_dir)
    assert result.exit_code == 2, result.output
    assert "wallet_name" in result.output
    assert not (env_dir / ".env.songbird").exists()


def test_v2_rotation_replaces_tokens_wallet_envs_and_config_in_place(monkeypatch, tmp_path):
    # Re-import is the rotation channel — for tokens, wallet-envs AND config.
    env_dir = tmp_path / "clifdir"
    b1 = _write_bundle(tmp_path, _v2_bundle(), name="b1.json")
    assert _invoke(monkeypatch, b1, env_dir).exit_code == 0

    rotated_caps = _v2_caps()
    rotated_caps[0] = {**rotated_caps[0], "caller_token": "fwd_live_claim_ROTATED"}
    b2 = _write_bundle(
        tmp_path,
        _v2_bundle(caps=rotated_caps, config={**CONFIG_OK, "WRAP_REWARDS": "false"}),
        name="b2.json",
    )
    assert _invoke(monkeypatch, b2, env_dir).exit_code == 0

    text = (env_dir / ".env.songbird").read_text()
    written = _env_lines(env_dir)
    assert written["FWD_CALLER_TOKEN"] == "fwd_live_claim_ROTATED"
    assert written["WRAP_REWARDS"] == "false"  # config rotated in place
    assert text.count("FWD_CALLER_TOKEN=") == 1  # no duplication
    assert text.count("FWD_WALLET_NAME=") == 1
    assert text.count("WRAP_REWARDS=") == 1


def test_v2_back_compat_v1_still_imports_tokens_only(monkeypatch, tmp_path):
    # A v1 bundle (tokens only, no config / no wallet-env write) still imports.
    env_dir = tmp_path / "clifdir"
    bundle_path = _write_bundle(tmp_path, _bundle())  # version=1
    result = _invoke(monkeypatch, bundle_path, env_dir, "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["bundle_version"] == 1
    assert payload["wallet_envs_written"] == []  # v1 does not write wallet-envs
    # v1 force-writes the NETWORK selector (the import is the authoritative env
    # writer; absent NETWORK silently defaults to flare — wrong-chain risk).
    assert payload["config_keys_written"] == ["NETWORK"]
    written = _env_lines(env_dir)
    assert written["FWD_CALLER_TOKEN"] == SECRET_A
    assert written["NETWORK"] == "songbird"  # pinned to the bundle's validated network
    assert "FWD_WALLET_NAME" not in written  # v1: fwd's host-side env-write supplied it


# ===== NETWORK enforcement — the import is the authoritative chain selector =======
# clif's only chain selector is NETWORK; absent it silently defaults to flare
# (cli.py resolver; D20). The import force-pins NETWORK to the bundle's validated
# network and rejects a config that contradicts it.


def test_v2_config_network_contradicting_bundle_network_rejected(monkeypatch, tmp_path):
    # config says flare but the bundle is a songbird bundle → terminal contradiction.
    env_dir = tmp_path / "clifdir"
    bad = _v2_bundle(network="songbird", config={**CONFIG_OK, "NETWORK": "flare"})
    bundle_path = _write_bundle(tmp_path, bad)
    result = _invoke(monkeypatch, bundle_path, env_dir)
    assert result.exit_code == 2, result.output
    assert "contradicts" in result.output
    assert not (env_dir / ".env.songbird").exists()  # nothing written
    assert bundle_path.is_file()  # not consumed on rejection


def test_v2_config_omitting_network_still_pins_it(monkeypatch, tmp_path):
    # A v2 config that omits NETWORK imports cleanly; the force-write pins it.
    env_dir = tmp_path / "clifdir"
    config = {k: v for k, v in CONFIG_OK.items() if k != "NETWORK"}
    bundle_path = _write_bundle(tmp_path, _v2_bundle(config=config))
    result = _invoke(monkeypatch, bundle_path, env_dir, "--json")
    assert result.exit_code == 0, result.output
    assert _env_lines(env_dir)["NETWORK"] == "songbird"
    assert "NETWORK" in json.loads(result.output)["config_keys_written"]


def test_v1_import_pins_network_in_config_keys(monkeypatch, tmp_path):
    env_dir = tmp_path / "clifdir"
    bundle_path = _write_bundle(tmp_path, _bundle(network="songbird"))
    result = _invoke(monkeypatch, bundle_path, env_dir, "--json")
    assert result.exit_code == 0, result.output
    assert _env_lines(env_dir)["NETWORK"] == "songbird"
    assert "NETWORK" in json.loads(result.output)["config_keys_written"]
