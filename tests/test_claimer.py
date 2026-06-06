"""claimer outcome classification + idempotency passthrough (fwd faked)."""

import httpx

import clif.claimer as claimer_mod

from clif.claimer import OutcomeStatus, run_claim, submit_claims
from clif.config import ZERO_BYTES32, Settings
from clif.discovery import classify_claim_frontier, unclaimable_reason
from clif.fwd_client import (
    FwdClient,
    FwdRetryableError,
    FwdTerminalError,
    make_idempotency_key,
)
from clif.models import RewardClaimBody, RewardClaimWithProof, SignTransactionResponse
from clif.rpc import RpcError

RECIP = "0x" + "22" * 20
BENEF = "0x" + "11" * 20
SIGNED_RAW = "0xf86c01"  # stub raw signed tx


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
            body=RewardClaimBody(reward_epoch_id=e, beneficiary=BENEF, amount=1, claim_type=1),
        )
        for e in epochs
    ]


class FakeFwd:
    """Minimal FwdClient fake for the new sign-only API."""

    def __init__(
        self,
        sign=None,
        sign_exc=None,
        broadcast_result_exc=None,
        receipt_exc=None,
    ):
        self._sign = sign
        self._sign_exc = sign_exc
        self._broadcast_result_exc = broadcast_result_exc
        self._receipt_exc = receipt_exc
        self.signed_kwargs = None
        self.broadcast_calls: list[dict] = []
        self.receipt_calls: list[dict] = []

    def sign_transaction(self, **kw):
        self.signed_kwargs = kw
        if self._sign_exc:
            raise self._sign_exc
        return self._sign

    def report_broadcast_result(self, tx_id, tx_hash, outcome, error_class=None):
        self.broadcast_calls.append(
            {"tx_id": tx_id, "tx_hash": tx_hash, "outcome": outcome, "error_class": error_class}
        )
        if self._broadcast_result_exc:
            raise self._broadcast_result_exc

    def report_receipt(self, tx_id, tx_hash, outcome, block_number):
        self.receipt_calls.append(
            {"tx_id": tx_id, "tx_hash": tx_hash, "outcome": outcome, "block_number": block_number}
        )
        if self._receipt_exc:
            raise self._receipt_exc


class FakeRpc:
    """Keyless-RPC stub: discovery + broadcast + receipt + fee estimation."""

    def __init__(
        self,
        *,
        next_claimable=0,
        end=10**9,
        rewards_hash="0x" + "11" * 32,
        receipt_log_addr=None,
        # Broadcast controls:
        send_raw_raises=None,  # RpcError to raise on send_raw_transaction
        poll_receipt_result=None,  # None = timeout; dict = the receipt
        # Fee estimation defaults (no errors):
        estimate_gas_result=200_000,
        suggest_fees_result=(100_000_000_000, 1_000_000_000),
    ):
        self._nc = next_claimable
        self._end = end
        self._rh = rewards_hash
        self._addr = receipt_log_addr
        self._send_raw_raises = send_raw_raises
        self._poll_receipt_result = poll_receipt_result
        self._estimate_gas_result = estimate_gas_result
        self._suggest_fees_result = suggest_fees_result
        # Track calls
        self.send_raw_calls: list[str] = []

    def next_claimable_reward_epoch_id(self, _rm, _owner):
        return self._nc

    def reward_epoch_id_range(self, _rm):
        return (0, self._end)

    def rewards_hash(self, _fsm, _epoch):
        return self._rh

    def get_transaction_receipt(self, _tx_hash):
        return {"logs": [{"address": self._addr}] if self._addr else []}

    # ---- new broadcast/fee methods ----

    def estimate_gas(self, _from, _to, _data, _value_wei=0):
        return self._estimate_gas_result

    def suggest_fees(self):
        return self._suggest_fees_result

    def send_raw_transaction(self, signed_raw_tx: str) -> str:
        self.send_raw_calls.append(signed_raw_tx)
        if self._send_raw_raises:
            raise self._send_raw_raises
        return "0x" + "aa" * 32  # stub broadcast hash

    def poll_receipt(self, tx_hash: str, timeout: float = 600.0, poll: float = 5.0):
        if self._poll_receipt_result is None:
            return None  # timeout
        return self._poll_receipt_result


def _ok_receipt(addr: str | None = None) -> dict:
    """A mined_success receipt with optional log address."""
    return {
        "status": "0x1",
        "blockNumber": "0x3e8",
        "logs": [{"address": addr}] if addr else [],
    }


def _reverted_receipt() -> dict:
    return {
        "status": "0x0",
        "blockNumber": "0x3e9",
        "logs": [],
    }


# ---- config guards (terminal before any HTTP) ----


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


# ---- happy path: sign → broadcast → receipt → mined ----


def test_happy_mined_and_idempotency_key_passed():
    s = _settings()
    fwd = FakeFwd(
        sign=SignTransactionResponse(tx_id="tx-1", hash="0xfwd", signed_raw_tx=SIGNED_RAW, nonce=4),
    )
    rpc = FakeRpc(
        receipt_log_addr=s.net.reward_manager,
        poll_receipt_result=_ok_receipt(s.net.reward_manager),
    )
    o = submit_claims(s, fwd, 1, BENEF, _claims(10, 11), wait=True, rpc=rpc)
    assert o.status == OutcomeStatus.SUBMITTED_MINED
    assert o.epochs == [10, 11] and o.last_epoch == 11
    assert o.tx_id == "tx-1"
    # hash is the broadcast_hash (from rpc.send_raw_transaction), not fwd's hash
    assert o.tx_hash == "0x" + "aa" * 32
    # deterministic idempotency key bound to the last epoch
    assert fwd.signed_kwargs["idempotency_key"] == make_idempotency_key("coston2", 1, BENEF, 11)
    assert fwd.signed_kwargs["wallet"] == "claim-wallet"
    assert fwd.signed_kwargs["value_wei"] == "0"
    # fwd was notified of broadcast + receipt
    assert len(fwd.broadcast_calls) == 1
    assert fwd.broadcast_calls[0]["outcome"] == "accepted"
    assert len(fwd.receipt_calls) == 1
    assert fwd.receipt_calls[0]["outcome"] == "mined_success"


def test_no_rpc_returns_pending():
    """When rpc is None, we can't broadcast — return SUBMITTED_PENDING after signing."""
    fwd = FakeFwd(
        sign=SignTransactionResponse(tx_id="tx", hash="0x1", signed_raw_tx=SIGNED_RAW, nonce=1),
    )
    o = submit_claims(_settings(), fwd, 1, BENEF, _claims(10), rpc=None)
    assert o.status == OutcomeStatus.SUBMITTED_PENDING
    assert "cannot broadcast" in o.detail


def test_no_wait_is_pending():
    """wait=False: sign and skip broadcast+poll."""
    fwd = FakeFwd(
        sign=SignTransactionResponse(tx_id="tx", hash="0x1", signed_raw_tx=SIGNED_RAW, nonce=1),
    )
    rpc = FakeRpc()
    o = submit_claims(_settings(), fwd, 1, BENEF, _claims(10), wait=False, rpc=rpc)
    assert o.status == OutcomeStatus.SUBMITTED_PENDING
    assert o.tx_id == "tx"
    # No broadcast should have happened
    assert len(rpc.send_raw_calls) == 0


def test_sign_terminal_is_failed_terminal():
    fwd = FakeFwd(sign_exc=FwdTerminalError(403, "policy_denied", "no"))
    o = submit_claims(_settings(), fwd, 1, BENEF, _claims(10))
    assert o.status == OutcomeStatus.FAILED_TERMINAL
    assert o.last_epoch == 10


def test_sign_retryable_is_failed_retryable():
    fwd = FakeFwd(sign_exc=FwdRetryableError(503, "vault_unavailable", "down"))
    o = submit_claims(_settings(), fwd, 1, BENEF, _claims(10))
    assert o.status == OutcomeStatus.FAILED_RETRYABLE


def test_sign_409_nonce_not_initialized_is_terminal():
    """409 nonce_not_initialized must surface as FAILED_TERMINAL with the right detail."""
    fwd = FakeFwd(sign_exc=FwdTerminalError(409, "nonce_not_initialized", "run clifwd nonce-init"))
    o = submit_claims(_settings(), fwd, 1, BENEF, _claims(10))
    assert o.status == OutcomeStatus.FAILED_TERMINAL
    assert "nonce_not_initialized" in o.detail or "fwd denied" in o.detail


# ---- broadcast rejection paths ----


def test_broadcast_rejected_insufficient_funds_is_terminal():
    """Deterministic node rejection → rejected_releaseable → FAILED_TERMINAL."""
    fwd = FakeFwd(
        sign=SignTransactionResponse(tx_id="tx", hash="0xfwd", signed_raw_tx=SIGNED_RAW, nonce=1),
    )
    rpc = FakeRpc(
        send_raw_raises=RpcError("insufficient funds for gas * price + value"),
    )
    o = submit_claims(_settings(), fwd, 1, BENEF, _claims(10), wait=True, rpc=rpc)
    assert o.status == OutcomeStatus.FAILED_TERMINAL
    assert "broadcast rejected" in o.detail
    # fwd must have been notified of the rejection
    assert len(fwd.broadcast_calls) == 1
    assert fwd.broadcast_calls[0]["outcome"] == "rejected_releaseable"


def test_broadcast_rejected_nonce_too_low_is_retryable():
    """'nonce too low' node rejection → rejected_nonce_too_low → FAILED_RETRYABLE."""
    fwd = FakeFwd(
        sign=SignTransactionResponse(tx_id="tx", hash="0xfwd", signed_raw_tx=SIGNED_RAW, nonce=0),
    )
    rpc = FakeRpc(
        send_raw_raises=RpcError("nonce too low: next nonce 5, tx nonce 4"),
    )
    o = submit_claims(_settings(), fwd, 1, BENEF, _claims(10), wait=True, rpc=rpc)
    assert o.status == OutcomeStatus.FAILED_RETRYABLE
    assert "nonce too low" in o.detail
    assert len(fwd.broadcast_calls) == 1
    assert fwd.broadcast_calls[0]["outcome"] == "rejected_nonce_too_low"


# ---- receipt polling ----


def test_receipt_poll_timeout_is_pending():
    """Receipt poll timeout (poll_receipt returns None) → SUBMITTED_PENDING."""
    fwd = FakeFwd(
        sign=SignTransactionResponse(tx_id="tx", hash="0xfwd", signed_raw_tx=SIGNED_RAW, nonce=1),
    )
    rpc = FakeRpc(poll_receipt_result=None)  # timeout
    o = submit_claims(_settings(), fwd, 1, BENEF, _claims(10), wait=True, rpc=rpc)
    assert o.status == OutcomeStatus.SUBMITTED_PENDING
    assert "timed out" in o.detail


def test_mined_reverted_is_terminal():
    """status=0x0 in receipt → mined_reverted → FAILED_TERMINAL."""
    fwd = FakeFwd(
        sign=SignTransactionResponse(tx_id="tx", hash="0xfwd", signed_raw_tx=SIGNED_RAW, nonce=1),
    )
    rpc = FakeRpc(poll_receipt_result=_reverted_receipt())
    o = submit_claims(_settings(), fwd, 1, BENEF, _claims(10), wait=True, rpc=rpc)
    assert o.status == OutcomeStatus.FAILED_TERMINAL
    assert "reverted" in o.detail
    # fwd receipt reported as mined_reverted
    assert fwd.receipt_calls[0]["outcome"] == "mined_reverted"


# ---- run_claim paths ----


def test_run_claim_discovery_rpc_error_is_retryable(monkeypatch):
    def boom(*_a, **_k):
        raise RpcError("rpc down")

    monkeypatch.setattr(claimer_mod, "collect_reward_claims", boom)
    o = run_claim(_settings(), object(), FakeFwd(), 1, BENEF)
    assert o.status == OutcomeStatus.FAILED_RETRYABLE


def test_run_claim_delegates_to_submit(monkeypatch):
    monkeypatch.setattr(claimer_mod, "collect_reward_claims", lambda *_a, **_k: _claims(7))
    s = _settings()
    fwd = FakeFwd(
        sign=SignTransactionResponse(tx_id="t", hash="0xh", signed_raw_tx=SIGNED_RAW, nonce=0),
    )
    rpc = FakeRpc(
        receipt_log_addr=s.net.reward_manager,
        poll_receipt_result=_ok_receipt(s.net.reward_manager),
    )
    o = run_claim(s, rpc, fwd, 1, BENEF)
    assert o.status == OutcomeStatus.SUBMITTED_MINED and o.epochs == [7]


# ---- STOP-SHIP #2: production idempotency retry discriminator ----


def test_default_idempotency_key_is_legacy_no_regression():
    """No retry set anywhere ⇒ byte-identical to the legacy key, so a
    same-attempt network retry / crash-rerun still dedups at fwd."""
    fwd = FakeFwd(
        sign=SignTransactionResponse(tx_id="t", hash="0x1", signed_raw_tx=SIGNED_RAW, nonce=0),
    )
    rpc = FakeRpc()
    submit_claims(_settings(), fwd, 1, BENEF, _claims(10), wait=False, rpc=rpc)
    assert fwd.signed_kwargs["idempotency_key"] == make_idempotency_key("coston2", 1, BENEF, 10)


def test_explicit_retry_param_overrides_settings_for_deliberate_reattempt():
    fwd = FakeFwd(
        sign=SignTransactionResponse(tx_id="t", hash="0x1", signed_raw_tx=SIGNED_RAW, nonce=0),
    )
    rpc = FakeRpc()
    s = _settings(idempotency_retry="env-1")
    submit_claims(s, fwd, 1, BENEF, _claims(10), wait=False, retry="cli-2", rpc=rpc)
    assert fwd.signed_kwargs["idempotency_key"] == make_idempotency_key(
        "coston2", 1, BENEF, 10, retry="cli-2"
    )
    assert fwd.signed_kwargs["idempotency_key"] != make_idempotency_key("coston2", 1, BENEF, 10)


def test_auto_uses_settings_idempotency_retry_when_no_explicit():
    """`clif auto` passes no explicit retry ⇒ the operator-controlled
    IDEMPOTENCY_RETRY (settings) is used; stable within the run (dedups),
    fresh only when the operator bumps it."""
    fwd = FakeFwd(
        sign=SignTransactionResponse(tx_id="t", hash="0x1", signed_raw_tx=SIGNED_RAW, nonce=0),
    )
    rpc = FakeRpc()
    s = _settings(idempotency_retry="op-bump-3")
    submit_claims(s, fwd, 1, BENEF, _claims(10), wait=False, rpc=rpc)
    assert fwd.signed_kwargs["idempotency_key"] == make_idempotency_key(
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


# ---- MINED_NOOP: effect verification (RewardClaimed event check) ----


def test_run_claim_e_already_claimed_is_clear_nothing_claimable():
    """`clif claim -e <claimed epoch>` must NOT submit a no-op; it reports
    NOTHING_CLAIMABLE with the precise reason (no FakeFwd send happens)."""
    fwd = FakeFwd(sign_exc=AssertionError("must not submit an already-claimed epoch"))
    rpc = FakeRpc(next_claimable=401, end=410)  # epoch 400 < 401 ⇒ already claimed
    o = run_claim(_settings(), rpc, fwd, 1, BENEF, only_epoch=400)
    assert o.status == OutcomeStatus.NOTHING_CLAIMABLE
    assert "already claimed" in o.detail and "401" in o.detail
    assert fwd.signed_kwargs is None  # never reached sign_transaction


def test_run_claim_e_not_yet_signed_is_clear():
    rpc = FakeRpc(next_claimable=400, end=410, rewards_hash=ZERO_BYTES32)
    o = run_claim(_settings(), rpc, FakeFwd(), 1, BENEF, only_epoch=400)
    assert o.status == OutcomeStatus.NOTHING_CLAIMABLE
    assert "not yet signed" in o.detail


def test_run_claim_e_out_of_range_is_clear():
    rpc = FakeRpc(next_claimable=0, end=399)  # epoch 400 > 399
    o = run_claim(_settings(), rpc, FakeFwd(), 1, BENEF, only_epoch=400)
    assert o.status == OutcomeStatus.NOTHING_CLAIMABLE
    assert "not in claimable range" in o.detail


# ---- empty auto-discovery path must classify WHY (not bare nothing-claimable) ----


def test_run_claim_no_epoch_already_claimed_reports_frontier():
    """`clif claim` (no -e) on a caught-up owner reports the per-epoch reason
    (already-claimed / not-finalized), not a bare 'nothing-claimable' that a
    reader could mistake for a still-pending state. No submit happens."""
    fwd = FakeFwd(sign_exc=AssertionError("must not submit when nothing claimable"))
    rpc = FakeRpc(next_claimable=402, end=401)  # 401 claimed, 402 not yet finalized
    o = run_claim(_settings(), rpc, fwd, 1, BENEF, only_epoch=None)
    assert o.status == OutcomeStatus.NOTHING_CLAIMABLE
    assert "nothing pending" in o.detail
    assert "already claimed" in o.detail
    assert "401" in o.detail and "402" in o.detail
    assert fwd.signed_kwargs is None


def test_unclaimable_reason_classifies_each_state():
    s = _settings()
    assert "already claimed" in unclaimable_reason(
        FakeRpc(next_claimable=402, end=401), s, BENEF, 401
    )
    assert "not in claimable range" in unclaimable_reason(
        FakeRpc(next_claimable=0, end=399), s, BENEF, 400
    )
    assert "not yet signed" in unclaimable_reason(
        FakeRpc(next_claimable=400, end=410, rewards_hash=ZERO_BYTES32), s, BENEF, 400
    )
    # Passes the on-chain gates → None (claimable iff present in the merkle tree).
    assert unclaimable_reason(FakeRpc(next_claimable=400, end=410), s, BENEF, 405) is None


def test_classify_frontier_no_accrual(monkeypatch):
    """A finalized+signed+unclaimed epoch with no merkle entry is reported as
    'no rewards accrued' (a DONE state), never as pending."""
    monkeypatch.setattr("clif.discovery.reward_claim_for", lambda *a, **k: None)
    rpc = FakeRpc(next_claimable=400, end=405)  # 400..405 finalized + signed
    frontier = classify_claim_frontier(rpc, _settings(), BENEF, 1)
    reasons = "; ".join(r for _e, r in frontier)
    assert "no rewards accrued" in reasons
    assert "already claimed" in reasons  # epoch 399 = next_claimable - 1


def test_submit_claims_mined_noop_when_no_reward_event():
    """status-0x1 mined + NO RewardManager log ⇒ MINED_NOOP, never SUBMITTED_MINED."""
    s = _settings()
    fwd = FakeFwd(
        sign=SignTransactionResponse(tx_id="t", hash="0xfwd", signed_raw_tx=SIGNED_RAW, nonce=0),
    )
    rpc = FakeRpc(
        receipt_log_addr=None,  # no RewardManager log
        poll_receipt_result=_ok_receipt(None),  # status=0x1 but no log
    )
    o = submit_claims(s, fwd, 1, BENEF, _claims(10), wait=True, rpc=rpc)
    assert o.status == OutcomeStatus.MINED_NOOP
    assert "claimed nothing" in o.detail


def test_submit_claims_real_claim_has_reward_event():
    s = _settings()
    fwd = FakeFwd(
        sign=SignTransactionResponse(tx_id="t", hash="0xfwd", signed_raw_tx=SIGNED_RAW, nonce=0),
    )
    rpc = FakeRpc(
        receipt_log_addr=s.net.reward_manager,
        poll_receipt_result=_ok_receipt(s.net.reward_manager),
    )
    o = submit_claims(s, fwd, 1, BENEF, _claims(10), wait=True, rpc=rpc)
    assert o.status == OutcomeStatus.SUBMITTED_MINED


# ---- report-back: fwd notification is best-effort (non-fatal failures) ----


def test_broadcast_report_failure_is_nonfatal():
    """If report_broadcast_result raises, clif continues (warning, not crash)."""
    s = _settings()
    fwd = FakeFwd(
        sign=SignTransactionResponse(tx_id="t", hash="0xfwd", signed_raw_tx=SIGNED_RAW, nonce=0),
        broadcast_result_exc=FwdRetryableError(503, "vault_unavailable", "down"),
    )
    rpc = FakeRpc(
        poll_receipt_result=_ok_receipt(s.net.reward_manager),
        receipt_log_addr=s.net.reward_manager,
    )
    # Should not raise; receipt_calls won't be made since broadcast raises
    # but the tx was accepted by the node; we proceed to poll
    o = submit_claims(s, fwd, 1, BENEF, _claims(10), wait=True, rpc=rpc)
    # The broadcast succeeded (node accepted), so we continue to receipt poll
    assert o.status == OutcomeStatus.SUBMITTED_MINED
