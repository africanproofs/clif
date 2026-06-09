"""FSP orchestrator: outcome mapping, rdd-guard, epoch-bind, config-guard, terminal/retryable paths."""

from clif.claimer import OutcomeStatus
from clif.config import Settings
from clif.fsp import FspOutcome, run_sign_rewards, run_sign_uptime
from clif.fwd_client import FwdRetryableError, FwdTerminalError
from clif.models import RewardDistributionData, SignFspMessageResponse, SignTransactionResponse
from clif.rpc import RpcError

REWARDS_HASH = "0x" + "ab" * 32
RDD = RewardDistributionData(rewardEpochId=3, merkleRoot=REWARDS_HASH, noOfWeightBasedClaims=56)

SIG = SignFspMessageResponse(
    message_hash="0x" + "bb" * 32,
    v=27,
    r="0x" + "aa" * 32,
    s="0x" + "cc" * 32,
    signature="0x" + "dd" * 65,
)
SIGN_TX_RESP = SignTransactionResponse(
    tx_id="tx-fsp-1", hash="0x" + "ef" * 32, signed_raw_tx="0xf86c", nonce=5
)
SIGNED_RAW = "0xf86c"


def _settings(**over):
    base = dict(
        network="coston2",
        fsp_sign_caller_token="fwd_live_fsp_sign",
        fsp_submit_caller_token="fwd_live_fsp_submit",
        fsp_signing_wallet_name="fsp-signing-wallet",
        fsp_sender_wallet_name="fsp-sender-wallet",
        fsp_submit_gas=500_000,
    )
    base.update(over)
    return Settings(_env_file=None, **base)


class FakeFwdFsp:
    """Minimal FwdClient fake for the new sign-only FSP API."""

    def __init__(
        self,
        sign_fsp=None,
        sign_fsp_exc=None,
        sign_tx=None,
        sign_tx_exc=None,
        broadcast_result_exc=None,
        receipt_exc=None,
    ):
        self._sign_fsp = sign_fsp
        self._sign_fsp_exc = sign_fsp_exc
        self._sign_tx = sign_tx
        self._sign_tx_exc = sign_tx_exc
        self._broadcast_result_exc = broadcast_result_exc
        self._receipt_exc = receipt_exc
        self.sign_fsp_kwargs = None
        self.sign_tx_kwargs = None
        self.broadcast_calls: list[dict] = []
        self.receipt_calls: list[dict] = []

    def sign_fsp_message(self, **kw):
        self.sign_fsp_kwargs = kw
        if self._sign_fsp_exc:
            raise self._sign_fsp_exc
        return self._sign_fsp

    def sign_transaction(self, **kw):
        self.sign_tx_kwargs = kw
        if self._sign_tx_exc:
            raise self._sign_tx_exc
        return self._sign_tx

    def report_broadcast_result(self, tx_id, tx_hash, outcome, error_class=None):
        self.broadcast_calls.append({"tx_id": tx_id, "tx_hash": tx_hash, "outcome": outcome})
        if self._broadcast_result_exc:
            raise self._broadcast_result_exc

    def report_receipt(self, tx_id, tx_hash, outcome, block_number):
        self.receipt_calls.append(
            {"tx_id": tx_id, "tx_hash": tx_hash, "outcome": outcome, "block_number": block_number}
        )
        if self._receipt_exc:
            raise self._receipt_exc

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class FakeRpc:
    """Minimal RpcClient stub for FSP tests: fee estimation + broadcast + receipt."""

    def __init__(
        self,
        *,
        send_raw_raises=None,
        poll_receipt_result=None,  # None = timeout; dict = the receipt
        revert_reason=None,  # str returned by get_revert_reason (None = unknown)
        rewards_hash_result=None,  # str returned by rewards_hash (None = ZERO_BYTES32)
        uptime_vote_hash_result=None,  # str returned by uptime_vote_hash (None = ZERO_BYTES32)
    ):
        from clif.config import ZERO_BYTES32

        self._send_raw_raises = send_raw_raises
        self._poll_receipt_result = poll_receipt_result
        self._revert_reason = revert_reason
        self._rewards_hash = rewards_hash_result or ZERO_BYTES32
        self._uptime_vote_hash = uptime_vote_hash_result or ZERO_BYTES32
        self.send_raw_calls: list[str] = []

    def estimate_gas(self, _from, _to, _data, _value_wei=0):
        return 500_000

    def suggest_fees(self):
        return 100_000_000_000, 1_000_000_000

    def send_raw_transaction(self, signed_raw_tx: str) -> str:
        self.send_raw_calls.append(signed_raw_tx)
        if self._send_raw_raises:
            raise self._send_raw_raises
        return "0x" + "bc" * 32

    def poll_receipt(self, tx_hash: str, timeout: float = 600.0, poll: float = 5.0):
        return self._poll_receipt_result

    def get_revert_reason(self, tx_hash: str):
        return self._revert_reason

    def rewards_hash(self, _fsm: str, _epoch: int) -> str:
        return self._rewards_hash

    def uptime_vote_hash(self, _fsm: str, _epoch: int) -> str:
        return self._uptime_vote_hash


def _ok_receipt() -> dict:
    return {"status": "0x1", "blockNumber": "0x3e8", "logs": []}


def _patch_fwd_factory(monkeypatch, *, sign_fwd, submit_fwd):
    """Patch FwdClient in fsp module to a factory dispatching by caller_token."""
    import clif.fsp as fsp_mod

    def factory(endpoint, caller_token, *a, **kw):
        if caller_token == "fwd_live_fsp_sign":
            return sign_fwd
        if caller_token == "fwd_live_fsp_submit":
            return submit_fwd
        raise AssertionError(f"unexpected caller_token={caller_token!r}")

    monkeypatch.setattr(fsp_mod, "FwdClient", factory)


# ---- config guards (terminal before any HTTP) ----


def test_missing_fsp_sign_caller_token_is_terminal(monkeypatch):
    s = _settings(fsp_sign_caller_token=None)
    sign_fwd = FakeFwdFsp()
    submit_fwd = FakeFwdFsp()
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    o = run_sign_uptime(s, 0)
    assert o.status == OutcomeStatus.FAILED_TERMINAL
    assert "FSP_SIGN_CALLER_TOKEN" in o.detail


def test_missing_fsp_submit_caller_token_is_terminal(monkeypatch):
    s = _settings(fsp_submit_caller_token=None)
    sign_fwd = FakeFwdFsp()
    submit_fwd = FakeFwdFsp()
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    o = run_sign_uptime(s, 0)
    assert o.status == OutcomeStatus.FAILED_TERMINAL
    assert "FSP_SUBMIT_CALLER_TOKEN" in o.detail


def test_missing_fsp_signing_wallet_is_terminal(monkeypatch):
    s = _settings(fsp_signing_wallet_name=None)
    sign_fwd = FakeFwdFsp()
    submit_fwd = FakeFwdFsp()
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    o = run_sign_uptime(s, 0)
    assert o.status == OutcomeStatus.FAILED_TERMINAL
    assert "FSP_SIGNING_WALLET_NAME" in o.detail


def test_missing_fsp_sender_wallet_is_terminal(monkeypatch):
    s = _settings(fsp_sender_wallet_name=None)
    sign_fwd = FakeFwdFsp()
    submit_fwd = FakeFwdFsp()
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    o = run_sign_uptime(s, 0)
    assert o.status == OutcomeStatus.FAILED_TERMINAL
    assert "FSP_SENDER_WALLET_NAME" in o.detail


# ---- rdd guard for rewards ----


def test_rdd_none_is_terminal(monkeypatch):
    sign_fwd = FakeFwdFsp()
    submit_fwd = FakeFwdFsp()
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    monkeypatch.setattr("clif.fsp.get_reward_distribution_data", lambda *_: None)
    o = run_sign_rewards(_settings(), 3)
    assert o.status == OutcomeStatus.FAILED_TERMINAL
    assert "unverified rewardsHash" in o.detail


# ---- MAJOR-1 epoch-bind tests ----


def test_rdd_wrong_epoch_is_terminal_no_sign_call(monkeypatch):
    """File rewardEpochId=99 but signing epoch=3 → FAILED_TERMINAL, no Leg-1 call."""
    sign_fwd = FakeFwdFsp(sign_fsp=SIG, sign_tx=SIGN_TX_RESP)
    submit_fwd = FakeFwdFsp(sign_fsp=SIG, sign_tx=SIGN_TX_RESP)
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    wrong_epoch_rdd = RewardDistributionData(
        rewardEpochId=99, merkleRoot=REWARDS_HASH, noOfWeightBasedClaims=56
    )
    monkeypatch.setattr("clif.fsp.get_reward_distribution_data", lambda *_: wrong_epoch_rdd)
    o = run_sign_rewards(_settings(), 3)
    assert o.status == OutcomeStatus.FAILED_TERMINAL
    assert "epoch mismatch" in o.detail
    # No sign call must have been made
    assert sign_fwd.sign_fsp_kwargs is None
    assert submit_fwd.sign_tx_kwargs is None


def test_rdd_matching_epoch_proceeds(monkeypatch):
    """File rewardEpochId==signing epoch → proceeds to Leg-1 + Leg-2."""
    rpc = FakeRpc(poll_receipt_result=_ok_receipt())
    sign_fwd = FakeFwdFsp(sign_fsp=SIG)
    submit_fwd = FakeFwdFsp(sign_tx=SIGN_TX_RESP)
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    monkeypatch.setattr("clif.fsp.get_reward_distribution_data", lambda *_: RDD)
    o = run_sign_rewards(_settings(), 3, rpc=rpc)
    assert o.ok


def test_rdd_present_proceeds(monkeypatch):
    monkeypatch.setattr("clif.fsp.get_reward_distribution_data", lambda *_: RDD)
    rpc = FakeRpc(poll_receipt_result=_ok_receipt())
    sign_fwd = FakeFwdFsp(sign_fsp=SIG)
    submit_fwd = FakeFwdFsp(sign_tx=SIGN_TX_RESP)
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    o = run_sign_rewards(_settings(), 3, rpc=rpc)
    assert o.ok


# ---- UPTIME happy path ----


def test_uptime_mined(monkeypatch):
    rpc = FakeRpc(poll_receipt_result=_ok_receipt())
    sign_fwd = FakeFwdFsp(sign_fsp=SIG)
    submit_fwd = FakeFwdFsp(sign_tx=SIGN_TX_RESP)
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    o = run_sign_uptime(_settings(), 0, rpc=rpc)
    assert o.status == OutcomeStatus.SUBMITTED_MINED
    assert o.message_hash is not None
    assert o.tx_hash is not None
    # fwd was notified of broadcast + receipt
    assert len(submit_fwd.broadcast_calls) == 1
    assert submit_fwd.broadcast_calls[0]["outcome"] == "accepted"
    assert len(submit_fwd.receipt_calls) == 1
    assert submit_fwd.receipt_calls[0]["outcome"] == "mined_success"


def test_uptime_no_wait(monkeypatch):
    rpc = FakeRpc()
    sign_fwd = FakeFwdFsp(sign_fsp=SIG)
    submit_fwd = FakeFwdFsp(sign_tx=SIGN_TX_RESP)
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    o = run_sign_uptime(_settings(), 0, wait=False, rpc=rpc)
    assert o.status == OutcomeStatus.SUBMITTED_PENDING
    # No broadcast happened
    assert len(rpc.send_raw_calls) == 0


def test_uptime_no_rpc_returns_pending_signed(monkeypatch):
    """Without rpc, clif can't broadcast — returns pending after sign."""
    sign_fwd = FakeFwdFsp(sign_fsp=SIG)
    submit_fwd = FakeFwdFsp(sign_tx=SIGN_TX_RESP)
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    o = run_sign_uptime(_settings(), 0, rpc=None)
    assert o.status == OutcomeStatus.SUBMITTED_PENDING
    assert "cannot broadcast" in o.detail


# ---- two-caller topology ----


def test_leg1_uses_sign_caller_leg2_uses_submit_caller(monkeypatch):
    """Leg-1 sign call goes to sign_fwd; Leg-2 sign_transaction goes to submit_fwd."""
    rpc = FakeRpc(poll_receipt_result=_ok_receipt())
    sign_fwd = FakeFwdFsp(sign_fsp=SIG)
    submit_fwd = FakeFwdFsp(sign_tx=SIGN_TX_RESP)
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    o = run_sign_uptime(_settings(), 0, rpc=rpc)
    assert o.ok
    # Leg-1 went to sign_fwd
    assert sign_fwd.sign_fsp_kwargs is not None
    assert sign_fwd.sign_tx_kwargs is None
    # Leg-2 went to submit_fwd
    assert submit_fwd.sign_tx_kwargs is not None
    assert submit_fwd.sign_fsp_kwargs is None
    # broadcast + receipt reported to submit_fwd
    assert len(submit_fwd.broadcast_calls) == 1
    assert len(submit_fwd.receipt_calls) == 1


# ---- error classification ----


def test_leg1_terminal_is_failed_terminal(monkeypatch):
    sign_fwd = FakeFwdFsp(sign_fsp_exc=FwdTerminalError(403, "policy_denied", "no"))
    submit_fwd = FakeFwdFsp()
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    o = run_sign_uptime(_settings(), 0)
    assert o.status == OutcomeStatus.FAILED_TERMINAL
    assert "operator must provision" in o.detail


def test_leg1_403_404_adds_operator_hint(monkeypatch):
    sign_fwd = FakeFwdFsp(sign_fsp_exc=FwdTerminalError(404, "wallet_not_found", "no"))
    submit_fwd = FakeFwdFsp()
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    o = run_sign_uptime(_settings(), 0)
    assert "operator must provision" in o.detail


def test_leg1_retryable_is_failed_retryable(monkeypatch):
    sign_fwd = FakeFwdFsp(sign_fsp_exc=FwdRetryableError(503, "vault_unavailable", "down"))
    submit_fwd = FakeFwdFsp()
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    o = run_sign_uptime(_settings(), 0)
    assert o.status == OutcomeStatus.FAILED_RETRYABLE


def test_leg2_terminal_is_failed_terminal(monkeypatch):
    sign_fwd = FakeFwdFsp(sign_fsp=SIG)
    submit_fwd = FakeFwdFsp(sign_tx_exc=FwdTerminalError(403, "policy_denied", "no"))
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    o = run_sign_uptime(_settings(), 0)
    assert o.status == OutcomeStatus.FAILED_TERMINAL


def test_leg2_retryable_is_failed_retryable(monkeypatch):
    sign_fwd = FakeFwdFsp(sign_fsp=SIG)
    submit_fwd = FakeFwdFsp(sign_tx_exc=FwdRetryableError(503, "vault_unavailable", "down"))
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    o = run_sign_uptime(_settings(), 0)
    assert o.status == OutcomeStatus.FAILED_RETRYABLE


def test_leg2_idempotency_conflict_is_retryable_not_terminal(monkeypatch):
    """A 409 idempotency_conflict (we already submitted this epoch's sign) must be
    RETRYABLE, not TERMINAL — else a restart-before-finalization wedges the epoch in a
    false TERMINAL + cooldown. Relies on the v0.1.2 reliable error_code."""
    sign_fwd = FakeFwdFsp(sign_fsp=SIG)
    submit_fwd = FakeFwdFsp(
        sign_tx_exc=FwdTerminalError(409, "idempotency_conflict", "reused with different body")
    )
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    o = run_sign_uptime(_settings(), 0)
    assert o.status == OutcomeStatus.FAILED_RETRYABLE


def test_leg2_other_409_still_terminal(monkeypatch):
    """A non-idempotency_conflict 409 (e.g. nonce_not_initialized) stays TERMINAL."""
    sign_fwd = FakeFwdFsp(sign_fsp=SIG)
    submit_fwd = FakeFwdFsp(
        sign_tx_exc=FwdTerminalError(409, "nonce_not_initialized", "run clifwd nonce-init")
    )
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    o = run_sign_uptime(_settings(), 0)
    assert o.status == OutcomeStatus.FAILED_TERMINAL


def test_leg2_rewards_idempotency_conflict_is_retryable(monkeypatch):
    """The REWARDS leg-2 path (run_sign_rewards) maps a 409 idempotency_conflict to
    RETRYABLE too — same branch as uptime, exercised on the rewards path."""
    sign_fwd = FakeFwdFsp(sign_fsp=SIG)
    submit_fwd = FakeFwdFsp(
        sign_tx_exc=FwdTerminalError(409, "idempotency_conflict", "reused with different body")
    )
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    monkeypatch.setattr("clif.fsp.get_reward_distribution_data", lambda *_: RDD)
    o = run_sign_rewards(_settings(), 3)
    assert o.status == OutcomeStatus.FAILED_RETRYABLE


def test_broadcast_nonce_too_low_is_retryable(monkeypatch):
    """Broadcast rejection with 'nonce too low' → FAILED_RETRYABLE for FSP Leg-2."""
    rpc = FakeRpc(send_raw_raises=RpcError("nonce too low: next 5, tx 4"))
    sign_fwd = FakeFwdFsp(sign_fsp=SIG)
    submit_fwd = FakeFwdFsp(sign_tx=SIGN_TX_RESP)
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    o = run_sign_uptime(_settings(), 0, rpc=rpc)
    assert o.status == OutcomeStatus.FAILED_RETRYABLE
    assert "nonce too low" in o.detail
    assert submit_fwd.broadcast_calls[0]["outcome"] == "rejected_nonce_too_low"


def test_broadcast_insufficient_funds_is_terminal(monkeypatch):
    """Broadcast rejection with insufficient funds → FAILED_TERMINAL."""
    rpc = FakeRpc(send_raw_raises=RpcError("insufficient funds for gas * price + value"))
    sign_fwd = FakeFwdFsp(sign_fsp=SIG)
    submit_fwd = FakeFwdFsp(sign_tx=SIGN_TX_RESP)
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    o = run_sign_uptime(_settings(), 0, rpc=rpc)
    assert o.status == OutcomeStatus.FAILED_TERMINAL
    assert submit_fwd.broadcast_calls[0]["outcome"] == "rejected_releaseable"


def test_receipt_poll_timeout_is_pending(monkeypatch):
    """Receipt poll timeout → SUBMITTED_PENDING."""
    rpc = FakeRpc(poll_receipt_result=None)
    sign_fwd = FakeFwdFsp(sign_fsp=SIG)
    submit_fwd = FakeFwdFsp(sign_tx=SIGN_TX_RESP)
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    o = run_sign_uptime(_settings(), 0, rpc=rpc)
    assert o.status == OutcomeStatus.SUBMITTED_PENDING
    assert "timed out" in o.detail


def test_on_chain_reverted_is_terminal(monkeypatch):
    """status=0x0 in receipt + no recognized revert reason → FAILED_TERMINAL."""
    rpc = FakeRpc(
        poll_receipt_result={"status": "0x0", "blockNumber": "0x1", "logs": []},
        revert_reason=None,  # unknown revert → still FAILED_TERMINAL
    )
    sign_fwd = FakeFwdFsp(sign_fsp=SIG)
    submit_fwd = FakeFwdFsp(sign_tx=SIGN_TX_RESP)
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    o = run_sign_uptime(_settings(), 0, rpc=rpc)
    assert o.status == OutcomeStatus.FAILED_TERMINAL
    assert "reverted" in o.detail
    assert submit_fwd.receipt_calls[0]["outcome"] == "mined_reverted"


def test_fsp_outcome_ok_property():
    o_ok = FspOutcome("UPTIME", 0, OutcomeStatus.SUBMITTED_MINED, "ok")
    o_fail = FspOutcome("UPTIME", 0, OutcomeStatus.FAILED_TERMINAL, "fail")
    assert o_ok.ok is True
    assert o_fail.ok is False


# ---- ALREADY_FINALIZED: post-revert classification ----


def test_rewards_already_signed_revert_is_already_finalized(monkeypatch):
    """status=0x0 + get_revert_reason='rewards hash already signed'
    → ALREADY_FINALIZED (benign, in _OK)."""
    rpc = FakeRpc(
        poll_receipt_result={"status": "0x0", "blockNumber": "0x1", "logs": []},
        revert_reason="rewards hash already signed",
    )
    sign_fwd = FakeFwdFsp(sign_fsp=SIG)
    submit_fwd = FakeFwdFsp(sign_tx=SIGN_TX_RESP)
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    monkeypatch.setattr("clif.fsp.get_reward_distribution_data", lambda *_: RDD)
    o = run_sign_rewards(_settings(), 3, rpc=rpc)
    assert o.status == OutcomeStatus.ALREADY_FINALIZED
    assert o.ok is True
    assert "finalized by the network" in o.detail
    assert "not a fault" in o.detail


def test_rewards_already_signed_in_ok_set():
    """ALREADY_FINALIZED is in the _OK set — exit-0 outcome."""
    from clif.claimer import _OK

    assert OutcomeStatus.ALREADY_FINALIZED in _OK


def test_uptime_already_signed_revert_is_already_finalized(monkeypatch):
    """status=0x0 + get_revert_reason='uptime vote hash already signed'
    → ALREADY_FINALIZED (benign, in _OK)."""
    rpc = FakeRpc(
        poll_receipt_result={"status": "0x0", "blockNumber": "0x1", "logs": []},
        revert_reason="uptime vote hash already signed",
    )
    sign_fwd = FakeFwdFsp(sign_fsp=SIG)
    submit_fwd = FakeFwdFsp(sign_tx=SIGN_TX_RESP)
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    o = run_sign_uptime(_settings(), 0, rpc=rpc)
    assert o.status == OutcomeStatus.ALREADY_FINALIZED
    assert o.ok is True
    assert "finalized by the network" in o.detail
    assert "not a fault" in o.detail


def test_other_revert_reason_stays_failed_terminal(monkeypatch):
    """status=0x0 + unrecognized revert reason → FAILED_TERMINAL with reason appended."""
    rpc = FakeRpc(
        poll_receipt_result={"status": "0x0", "blockNumber": "0x1", "logs": []},
        revert_reason="some unexpected contract error",
    )
    sign_fwd = FakeFwdFsp(sign_fsp=SIG)
    submit_fwd = FakeFwdFsp(sign_tx=SIGN_TX_RESP)
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    o = run_sign_uptime(_settings(), 0, rpc=rpc)
    assert o.status == OutcomeStatus.FAILED_TERMINAL
    assert "some unexpected contract error" in o.detail


def test_none_revert_reason_stays_failed_terminal_unknown(monkeypatch):
    """status=0x0 + get_revert_reason=None → FAILED_TERMINAL with 'unknown' appended."""
    rpc = FakeRpc(
        poll_receipt_result={"status": "0x0", "blockNumber": "0x1", "logs": []},
        revert_reason=None,
    )
    sign_fwd = FakeFwdFsp(sign_fsp=SIG)
    submit_fwd = FakeFwdFsp(sign_tx=SIGN_TX_RESP)
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    o = run_sign_uptime(_settings(), 0, rpc=rpc)
    assert o.status == OutcomeStatus.FAILED_TERMINAL
    assert "unknown" in o.detail


# ---- ALREADY_FINALIZED: pre-flight finalization skip ----


def test_rewards_pre_flight_already_finalized_skips_signing(monkeypatch):
    """rewardsHash != ZERO_BYTES32 → ALREADY_FINALIZED before any leg-1/leg-2."""
    from clif.config import ZERO_BYTES32

    non_zero_hash = "0x" + "ab" * 32
    assert non_zero_hash != ZERO_BYTES32
    rpc = FakeRpc(rewards_hash_result=non_zero_hash)
    sign_fwd = FakeFwdFsp(sign_fsp_exc=AssertionError("must not call leg-1 when finalized"))
    submit_fwd = FakeFwdFsp(sign_tx_exc=AssertionError("must not call leg-2 when finalized"))
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    monkeypatch.setattr("clif.fsp.get_reward_distribution_data", lambda *_: RDD)
    o = run_sign_rewards(_settings(), 3, rpc=rpc)
    assert o.status == OutcomeStatus.ALREADY_FINALIZED
    assert o.ok is True
    assert sign_fwd.sign_fsp_kwargs is None
    assert submit_fwd.sign_tx_kwargs is None


def test_uptime_pre_flight_already_finalized_skips_signing(monkeypatch):
    """uptimeVoteHash != ZERO_BYTES32 → ALREADY_FINALIZED before any leg-1/leg-2."""
    from clif.config import ZERO_BYTES32

    non_zero_hash = "0x" + "cd" * 32
    assert non_zero_hash != ZERO_BYTES32
    rpc = FakeRpc(uptime_vote_hash_result=non_zero_hash)
    sign_fwd = FakeFwdFsp(sign_fsp_exc=AssertionError("must not call leg-1 when finalized"))
    submit_fwd = FakeFwdFsp(sign_tx_exc=AssertionError("must not call leg-2 when finalized"))
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    o = run_sign_uptime(_settings(), 0, rpc=rpc)
    assert o.status == OutcomeStatus.ALREADY_FINALIZED
    assert o.ok is True
    assert sign_fwd.sign_fsp_kwargs is None
    assert submit_fwd.sign_tx_kwargs is None


def test_rewards_pre_flight_not_finalized_proceeds(monkeypatch):
    """rewardsHash == ZERO_BYTES32 → pre-flight passes, proceeds to leg-1/leg-2."""
    from clif.config import ZERO_BYTES32

    rpc = FakeRpc(
        rewards_hash_result=ZERO_BYTES32,
        poll_receipt_result=_ok_receipt(),
    )
    sign_fwd = FakeFwdFsp(sign_fsp=SIG)
    submit_fwd = FakeFwdFsp(sign_tx=SIGN_TX_RESP)
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    monkeypatch.setattr("clif.fsp.get_reward_distribution_data", lambda *_: RDD)
    o = run_sign_rewards(_settings(), 3, rpc=rpc)
    assert o.ok
    assert o.status == OutcomeStatus.SUBMITTED_MINED


# ---- fsp auto hard-off gate ----


def test_fsp_auto_disabled_by_default(monkeypatch):
    """clif fsp auto exits 2 with a D15 message unless FSP_AUTO_ENABLED=true."""
    from typer.testing import CliRunner
    from clif.cli import app

    # Patch load_settings to return a settings object with fsp_auto_enabled=False
    monkeypatch.setattr(
        "clif.cli.load_settings",
        lambda: Settings(
            _env_file=None,
            network="coston2",
            fsp_auto_enabled=False,
            fsp_sign_caller_token="tok",
            fsp_submit_caller_token="tok2",
        ),
    )
    runner = CliRunner()
    result = runner.invoke(app, ["fsp", "auto"])
    assert result.exit_code == 2
    assert "D15" in result.output or "D15" in (result.stdout or "")
