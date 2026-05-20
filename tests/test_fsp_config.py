"""FSP config fields: distinct from fwd_* claim fields, correct types and defaults."""

from clif.config import Settings


def _s(**over) -> Settings:
    return Settings(_env_file=None, **over)


def test_fsp_fields_default_none_or_false():
    s = _s()
    assert s.fsp_sign_caller_token is None
    assert s.fsp_submit_caller_token is None
    assert s.fsp_auto_enabled is False
    assert s.fsp_signing_wallet_name is None
    assert s.fsp_sender_wallet_name is None
    assert s.fsp_idempotency_retry is None


def test_fsp_submit_gas_default():
    s = _s()
    assert s.fsp_submit_gas == 500_000


def test_fsp_poll_interval_default():
    s = _s()
    assert s.fsp_poll_interval_sec == 900


def test_fsp_stale_after_default():
    s = _s()
    assert s.fsp_stale_after_sec == 86_400


def test_fsp_terminal_cooldown_default():
    s = _s()
    assert s.fsp_terminal_cooldown_sec == 3_600


def test_fsp_fields_distinct_from_claim_fields():
    """FSP caller tokens and wallet names are separate from the claim path (D14/D15)."""
    s = _s(
        fwd_caller_token="claim-token",
        fwd_wallet_name="claim-wallet",
        fsp_sign_caller_token="fsp-sign-token",
        fsp_submit_caller_token="fsp-submit-token",
        fsp_signing_wallet_name="fsp-signing",
        fsp_sender_wallet_name="fsp-sender",
    )
    assert s.fwd_caller_token == "claim-token"
    assert s.fwd_wallet_name == "claim-wallet"
    assert s.fsp_sign_caller_token == "fsp-sign-token"
    assert s.fsp_submit_caller_token == "fsp-submit-token"
    assert s.fsp_signing_wallet_name == "fsp-signing"
    assert s.fsp_sender_wallet_name == "fsp-sender"
    # Both FSP tokens are distinct from the claim token (cross-domain rule, D15)
    assert s.fsp_sign_caller_token != s.fwd_caller_token
    assert s.fsp_submit_caller_token != s.fwd_caller_token
    # The two FSP tokens are distinct from each other
    assert s.fsp_sign_caller_token != s.fsp_submit_caller_token


def test_fsp_auto_enabled_default_false():
    """fsp_auto_enabled defaults False — unattended REWARDS auto-signer hard-off (D15)."""
    s = _s()
    assert s.fsp_auto_enabled is False


def test_fsp_auto_enabled_settable():
    s = _s(fsp_auto_enabled=True)
    assert s.fsp_auto_enabled is True


def test_fsp_status_file_path():
    s = _s(clif_state_dir="/tmp/clif-state", network="flare")
    # Network-scoped filename (multichain cutover: parallel NETWORK= processes
    # sharing a CLIF_STATE_DIR must not clobber each other's status files).
    assert str(s.fsp_status_file).endswith("fsp-auto-status-flare.json")
    # Distinct from the claim status file.
    assert s.fsp_status_file != s.status_file


def test_status_files_are_network_scoped():
    """status_file / fsp_status_file embed the network token and are pairwise
    distinct across flare / songbird / coston2 (the clobber this fix prevents)."""
    nets = ("flare", "songbird", "coston2")
    auto = {n: _s(clif_state_dir="/x", network=n).status_file for n in nets}
    fsp = {n: _s(clif_state_dir="/x", network=n).fsp_status_file for n in nets}
    for n in nets:
        assert n in auto[n].name and n in fsp[n].name
    assert len({str(p) for p in auto.values()}) == 3
    assert len({str(p) for p in fsp.values()}) == 3


def test_parallel_networks_do_not_collide_under_one_state_dir():
    """The regression this exists to prevent: two NETWORK= processes sharing
    one CLIF_STATE_DIR get non-colliding status-file paths."""
    flr = _s(clif_state_dir="/shared", network="flare")
    sgb = _s(clif_state_dir="/shared", network="songbird")
    assert flr.status_file != sgb.status_file
    assert flr.fsp_status_file != sgb.fsp_status_file
    # ...and they still share the one state dir (only the filename differs).
    assert flr.status_file.parent == sgb.status_file.parent


def test_reward_distribution_url_flare():
    s = _s(network="flare")
    url = s.reward_distribution_url(42)
    assert "reward-distribution-data.json" in url
    assert "42" in url
    assert "reward-distribution-data-tuples.json" not in url


def test_reward_distribution_url_coston2():
    s = _s(network="coston2")
    url = s.reward_distribution_url(7)
    assert "reward-distribution-data.json" in url
    assert "7" in url


def test_fsp_idempotency_retry_settable():
    s = _s(fsp_idempotency_retry="op-fsp-1")
    assert s.fsp_idempotency_retry == "op-fsp-1"
