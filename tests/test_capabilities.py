"""clif's fwd capability model + `clif spec` capability-request (ADR-0001 §3/§4)."""

import json

from clif.config import capabilities, Settings

_RECIP = "0x" + "ab" * 20


def _settings(**kw) -> Settings:
    base = dict(
        _env_file=None,
        network="songbird",
        claim_recipient_address=_RECIP,
        fwd_wallet_name="claimer-songbird",
        fsp_signing_wallet_name="fsp-sign-songbird",
        fsp_sender_wallet_name="fsp-sender-songbird",
    )
    base.update(kw)
    return Settings(**base)


def test_five_capabilities_with_namespaced_ids():
    caps = capabilities(_settings())
    ids = [c.capability_id for c in caps]
    assert ids == [
        "claim/songbird/ftso-reward-claim",
        "claim/songbird/uptime-vote-sign",
        "claim/songbird/reward-distribution-sign",
        "claim/songbird/uptime-vote-submit",
        "claim/songbird/reward-distribution-submit",
    ]
    assert len(set(ids)) == 5  # immutable, unique join keys


def test_capability_id_is_namespaced_per_network():
    assert capabilities(_settings(network="flare"))[0].capability_id == "claim/flare/ftso-reward-claim"


def test_claim_pins_recipient_zero_value_rewardmanager():
    claim = {c.role: c for c in capabilities(_settings())}["ftso-reward-claim"]
    assert claim.endpoint == "/v1/sign-transaction"
    assert claim.value_wei == "0"
    assert claim.recipient_pinned == _RECIP
    assert claim.contract_name == "RewardManager"
    assert claim.contract  # address resolved from the network table


def test_fsp_sign_is_detached_signature_no_contract():
    fs = {c.role: c for c in capabilities(_settings())}["uptime-vote-sign"]
    assert fs.endpoint == "/v1/sign-fsp-message"
    assert fs.contract is None
    assert fs.value_wei is None


def test_fsp_submit_targets_flare_systems_manager():
    fs = {c.role: c for c in capabilities(_settings())}["uptime-vote-submit"]
    assert fs.endpoint == "/v1/sign-transaction"
    assert fs.contract_name == "FlareSystemsManager"
    assert fs.value_wei == "0"


def test_capability_carries_names_never_token_values():
    # A caller TOKEN is a secret; the capability references only the env var NAME.
    caps = capabilities(_settings(fwd_caller_token="SUPER-SECRET-TOKEN"))
    assert "SUPER-SECRET-TOKEN" not in repr(caps)
    assert {c.caller_token_env for c in caps} == {
        "FWD_CALLER_TOKEN",
        "FSP_UPTIME_SIGN_CALLER_TOKEN",
        "FSP_UPTIME_SUBMIT_CALLER_TOKEN",
        "FSP_REWARD_SIGN_CALLER_TOKEN",
        "FSP_REWARD_SUBMIT_CALLER_TOKEN",
    }


def test_spec_json_payload_shape_no_secret_leak(monkeypatch):
    from typer.testing import CliRunner

    from clif.cli import app

    monkeypatch.setattr(
        "clif.cli.load_settings",
        lambda: _settings(fwd_caller_token="SUPER-SECRET-TOKEN"),
    )
    result = CliRunner().invoke(app, ["spec", "--json"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.output)
    assert payload["consumer"] == "claim"
    assert payload["network"] == "songbird"
    assert set(payload["compat"]) == {"fwd_contract_expected", "fwd_client", "claim"}
    assert [c["capability_id"] for c in payload["capabilities"]] == [
        "claim/songbird/ftso-reward-claim",
        "claim/songbird/uptime-vote-sign",
        "claim/songbird/reward-distribution-sign",
        "claim/songbird/uptime-vote-submit",
        "claim/songbird/reward-distribution-submit",
    ]
    assert "SUPER-SECRET-TOKEN" not in result.output  # no secret in the emitted artifact
    assert "[bold" not in result.output  # clean JSON, no rich markup
