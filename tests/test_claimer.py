"""claimer outcome classification + idempotency passthrough (fwd faked)."""

import httpx

import clif.claimer as claimer_mod

from clif.claimer import OutcomeStatus, run_claim, submit_claims
from clif.config import Settings
from clif.fwd_client import (
    FwdClient,
    FwdRetryableError,
    FwdTerminalError,
    make_idempotency_key,
)
from clif.models import RewardClaimBody, RewardClaimWithProof, SignAndSendResponse, TxStatus
from clif.rpc import RpcError

RECIP = "0x" + "22" * 20
BENEF = "0x" + "11" * 20


def _settings(**over):
    base = dict(
        network="coston2",
        claim_recipient_address=RECIP,
        fwd_wallet_name="claim-wallet",
        fwd_caller_token="fwd_live_x",
        wrap_rewards=True,
    )
    base.update(over)
    return Settings(_env_file=None, **base)


def _claims(*epochs):
    return [
        RewardClaimWithProof(
            merkle_proof=["0x" + "ab" * 32],
            body=RewardClaimBody(
                reward_epoch_id=e, beneficiary=BENEF, amount=1, claim_type=1
            ),
        )
        for e in epochs
    ]


class FakeFwd:
    def __init__(self, send=None, send_exc=None, wait=None, wait_exc=None):
        self._send = send
        self._send_exc = send_exc
        self._wait = wait
        self._wait_exc = wait_exc
        self.sent_kwargs = None

    def sign_and_send(self, **kw):
        self.sent_kwargs = kw
        if self._send_exc:
            raise self._send_exc
        return self._send

    def wait_until_mined(self, tx_id, timeout=600.0, poll=5.0):
        if self._wait_exc:
            raise self._wait_exc
        return self._wait


def test_missing_fwd_config_is_terminal():
    s = _settings(fwd_wallet_name=None)
    o = submit_claims(s, FakeFwd(), 1, BENEF, _claims(10))
    assert o.status == OutcomeStatus.FAILED_TERMINAL


def test_missing_recipient_is_terminal():
    s = _settings(claim_recipient_address=None)
    o = submit_claims(s, FakeFwd(), 1, BENEF, _claims(10))
    assert o.status == OutcomeStatus.FAILED_TERMINAL


def test_no_claims_is_nothing_claimable():
    o = submit_claims(_settings(), FakeFwd(), 1, BENEF, [])
    assert o.status == OutcomeStatus.NOTHING_CLAIMABLE
    assert o.epochs == []


def test_happy_mined_and_idempotency_key_passed():
    fwd = FakeFwd(
        send=SignAndSendResponse(tx_id="tx-1", hash="0xabc", nonce=4),
        wait=TxStatus(status="mined"),
    )
    o = submit_claims(_settings(), fwd, 1, BENEF, _claims(10, 11), wait=True)
    assert o.status == OutcomeStatus.SUBMITTED_MINED
    assert o.epochs == [10, 11] and o.last_epoch == 11
    assert o.tx_id == "tx-1" and o.tx_hash == "0xabc"
    # deterministic idempotency key bound to the last epoch
    assert fwd.sent_kwargs["idempotency_key"] == make_idempotency_key(
        "coston2", 1, BENEF, 11
    )
    assert fwd.sent_kwargs["wallet"] == "claim-wallet"
    assert fwd.sent_kwargs["value_wei"] == "0"


def test_no_wait_is_pending():
    fwd = FakeFwd(send=SignAndSendResponse(tx_id="tx", hash="0x1", nonce=1))
    o = submit_claims(_settings(), fwd, 1, BENEF, _claims(10), wait=False)
    assert o.status == OutcomeStatus.SUBMITTED_PENDING
    assert o.tx_id == "tx"


def test_send_terminal_is_failed_terminal():
    fwd = FakeFwd(send_exc=FwdTerminalError(403, "policy_denied", "no"))
    o = submit_claims(_settings(), fwd, 1, BENEF, _claims(10))
    assert o.status == OutcomeStatus.FAILED_TERMINAL
    assert o.last_epoch == 10


def test_send_retryable_is_failed_retryable():
    fwd = FakeFwd(send_exc=FwdRetryableError(502, "rpc_unreachable", "down"))
    o = submit_claims(_settings(), fwd, 1, BENEF, _claims(10))
    assert o.status == OutcomeStatus.FAILED_RETRYABLE


def test_wait_timeout_is_pending():
    fwd = FakeFwd(
        send=SignAndSendResponse(tx_id="tx", hash="0x1", nonce=1),
        wait_exc=TimeoutError("not mined"),
    )
    o = submit_claims(_settings(), fwd, 1, BENEF, _claims(10), wait=True)
    assert o.status == OutcomeStatus.SUBMITTED_PENDING


def test_wait_onchain_failed_is_terminal():
    fwd = FakeFwd(
        send=SignAndSendResponse(tx_id="tx", hash="0x1", nonce=1),
        wait=TxStatus(status="failed"),
    )
    o = submit_claims(_settings(), fwd, 1, BENEF, _claims(10), wait=True)
    assert o.status == OutcomeStatus.FAILED_TERMINAL


def test_run_claim_discovery_rpc_error_is_retryable(monkeypatch):
    def boom(*_a, **_k):
        raise RpcError("rpc down")

    monkeypatch.setattr(claimer_mod, "collect_reward_claims", boom)
    o = run_claim(_settings(), object(), FakeFwd(), 1, BENEF)
    assert o.status == OutcomeStatus.FAILED_RETRYABLE


def test_run_claim_delegates_to_submit(monkeypatch):
    monkeypatch.setattr(
        claimer_mod, "collect_reward_claims", lambda *_a, **_k: _claims(7)
    )
    fwd = FakeFwd(
        send=SignAndSendResponse(tx_id="t", hash="0xh", nonce=0),
        wait=TxStatus(status="mined"),
    )
    o = run_claim(_settings(), object(), fwd, 1, BENEF)
    assert o.status == OutcomeStatus.SUBMITTED_MINED and o.epochs == [7]


# ---- STOP-SHIP #2: production idempotency retry discriminator ----


def test_default_idempotency_key_is_legacy_no_regression():
    """No retry set anywhere ⇒ byte-identical to the legacy key, so a
    same-attempt network retry / crash-rerun still dedups at fwd."""
    fwd = FakeFwd(send=SignAndSendResponse(tx_id="t", hash="0x1", nonce=0))
    submit_claims(_settings(), fwd, 1, BENEF, _claims(10), wait=False)
    assert fwd.sent_kwargs["idempotency_key"] == make_idempotency_key(
        "coston2", 1, BENEF, 10
    )


def test_explicit_retry_param_overrides_settings_for_deliberate_reattempt():
    fwd = FakeFwd(send=SignAndSendResponse(tx_id="t", hash="0x1", nonce=0))
    s = _settings(idempotency_retry="env-1")
    submit_claims(s, fwd, 1, BENEF, _claims(10), wait=False, retry="cli-2")
    assert fwd.sent_kwargs["idempotency_key"] == make_idempotency_key(
        "coston2", 1, BENEF, 10, retry="cli-2"
    )
    assert fwd.sent_kwargs["idempotency_key"] != make_idempotency_key(
        "coston2", 1, BENEF, 10
    )


def test_auto_uses_settings_idempotency_retry_when_no_explicit():
    """`clif auto` passes no explicit retry ⇒ the operator-controlled
    IDEMPOTENCY_RETRY (settings) is used; stable within the run (dedups),
    fresh only when the operator bumps it."""
    fwd = FakeFwd(send=SignAndSendResponse(tx_id="t", hash="0x1", nonce=0))
    s = _settings(idempotency_retry="op-bump-3")
    submit_claims(s, fwd, 1, BENEF, _claims(10), wait=False)
    assert fwd.sent_kwargs["idempotency_key"] == make_idempotency_key(
        "coston2", 1, BENEF, 10, retry="op-bump-3"
    )


# ---- STOP-SHIP #3: a down fwd must NOT crash `clif auto` ----


def test_down_fwd_yields_retryable_not_raise():
    """Real FwdClient whose transport is down: submit_claims must RETURN a
    FAILED_RETRYABLE outcome (so the `auto` loop records degraded and keeps
    running), never propagate a raw httpx error that terminates the daemon."""
    def boom(req):
        raise httpx.ConnectError("fwd down", request=req)

    fwd = FwdClient("http://fwd:8080", "fwd_live_x")
    fwd._client = httpx.Client(transport=httpx.MockTransport(boom))
    o = submit_claims(_settings(), fwd, 1, BENEF, _claims(10), wait=False)
    assert o.status == OutcomeStatus.FAILED_RETRYABLE
    assert o.last_epoch == 10
