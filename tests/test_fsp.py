"""FSP orchestrator: outcome mapping, rdd-guard, epoch-bind, config-guard, terminal/retryable paths."""

from clif.claimer import OutcomeStatus
from clif.config import Settings
from clif.fsp import FspOutcome, run_sign_rewards, run_sign_uptime
from clif.fwd_client import FwdRetryableError, FwdTerminalError
from clif.models import RewardDistributionData, SignAndSendResponse, SignFspMessageResponse

REWARDS_HASH = "0x" + "ab" * 32
RDD = RewardDistributionData(rewardEpochId=3, merkleRoot=REWARDS_HASH, noOfWeightBasedClaims=56)

SIG = SignFspMessageResponse(
    message_hash="0x" + "bb" * 32,
    v=27,
    r="0x" + "aa" * 32,
    s="0x" + "cc" * 32,
    signature="0x" + "dd" * 65,
)
SEND_RESP = SignAndSendResponse(tx_id="tx-fsp-1", hash="0x" + "ef" * 32, nonce=5)


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
    """Minimal FwdClient fake supporting sign_fsp_message + sign_and_send + wait_until_mined."""

    def __init__(
        self,
        sign_fsp=None, sign_fsp_exc=None,
        send=None, send_exc=None,
        wait=None, wait_exc=None,
    ):
        self._sign_fsp = sign_fsp
        self._sign_fsp_exc = sign_fsp_exc
        self._send = send
        self._send_exc = send_exc
        self._wait = wait
        self._wait_exc = wait_exc
        self.sign_fsp_kwargs = None
        self.send_kwargs = None
        self.wait_calls = 0

    def sign_fsp_message(self, **kw):
        self.sign_fsp_kwargs = kw
        if self._sign_fsp_exc:
            raise self._sign_fsp_exc
        return self._sign_fsp

    def sign_and_send(self, **kw):
        self.send_kwargs = kw
        if self._send_exc:
            raise self._send_exc
        return self._send

    def wait_until_mined(self, tx_id, timeout=600.0, poll=5.0):
        self.wait_calls += 1
        if self._wait_exc:
            raise self._wait_exc
        return self._wait

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


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
    sign_fwd = FakeFwdFsp(sign_fsp=SIG, send=SEND_RESP)
    submit_fwd = FakeFwdFsp(sign_fsp=SIG, send=SEND_RESP)
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
    assert submit_fwd.send_kwargs is None


def test_rdd_matching_epoch_proceeds(monkeypatch):
    """File rewardEpochId==signing epoch → proceeds to Leg-1."""
    from clif.models import TxStatus
    sign_fwd = FakeFwdFsp(sign_fsp=SIG)
    submit_fwd = FakeFwdFsp(send=SEND_RESP, wait=TxStatus(status="mined"))
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    monkeypatch.setattr("clif.fsp.get_reward_distribution_data", lambda *_: RDD)
    o = run_sign_rewards(_settings(), 3)  # RDD.reward_epoch_id == 3
    assert o.ok


def test_rdd_present_proceeds(monkeypatch):
    monkeypatch.setattr("clif.fsp.get_reward_distribution_data", lambda *_: RDD)
    from clif.models import TxStatus
    sign_fwd = FakeFwdFsp(sign_fsp=SIG)
    submit_fwd = FakeFwdFsp(send=SEND_RESP, wait=TxStatus(status="mined"))
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    o = run_sign_rewards(_settings(), 3)
    assert o.ok


# ---- UPTIME happy path ----

def test_uptime_mined(monkeypatch):
    from clif.models import TxStatus
    sign_fwd = FakeFwdFsp(sign_fsp=SIG)
    submit_fwd = FakeFwdFsp(send=SEND_RESP, wait=TxStatus(status="mined"))
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    o = run_sign_uptime(_settings(), 0)
    assert o.status == OutcomeStatus.SUBMITTED_MINED
    assert o.message_hash is not None
    assert o.tx_hash is not None


def test_uptime_no_wait(monkeypatch):
    sign_fwd = FakeFwdFsp(sign_fsp=SIG)
    submit_fwd = FakeFwdFsp(send=SEND_RESP)
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    o = run_sign_uptime(_settings(), 0, wait=False)
    assert o.status == OutcomeStatus.SUBMITTED_PENDING


# ---- two-caller topology ----

def test_leg1_uses_sign_caller_leg2_and_poll_use_submit_caller(monkeypatch):
    """Leg-1 sign call goes to sign_fwd; Leg-2 submit + wait_until_mined go to submit_fwd."""
    from clif.models import TxStatus
    sign_fwd = FakeFwdFsp(sign_fsp=SIG)
    submit_fwd = FakeFwdFsp(send=SEND_RESP, wait=TxStatus(status="mined"))
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    o = run_sign_uptime(_settings(), 0)
    assert o.ok
    # Leg-1 went to sign_fwd
    assert sign_fwd.sign_fsp_kwargs is not None
    assert sign_fwd.send_kwargs is None
    # Leg-2 + poll went to submit_fwd
    assert submit_fwd.send_kwargs is not None
    assert submit_fwd.wait_calls == 1
    assert sign_fwd.wait_calls == 0


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
    sign_fwd = FakeFwdFsp(sign_fsp_exc=FwdRetryableError(502, "rpc_unreachable", "down"))
    submit_fwd = FakeFwdFsp()
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    o = run_sign_uptime(_settings(), 0)
    assert o.status == OutcomeStatus.FAILED_RETRYABLE


def test_leg2_terminal_is_failed_terminal(monkeypatch):
    sign_fwd = FakeFwdFsp(sign_fsp=SIG)
    submit_fwd = FakeFwdFsp(send_exc=FwdTerminalError(403, "policy_denied", "no"))
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    o = run_sign_uptime(_settings(), 0)
    assert o.status == OutcomeStatus.FAILED_TERMINAL


def test_leg2_retryable_is_failed_retryable(monkeypatch):
    sign_fwd = FakeFwdFsp(sign_fsp=SIG)
    submit_fwd = FakeFwdFsp(send_exc=FwdRetryableError(502, "rpc_unreachable", "down"))
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    o = run_sign_uptime(_settings(), 0)
    assert o.status == OutcomeStatus.FAILED_RETRYABLE


def test_on_chain_failed_is_terminal(monkeypatch):
    from clif.models import TxStatus
    sign_fwd = FakeFwdFsp(sign_fsp=SIG)
    submit_fwd = FakeFwdFsp(send=SEND_RESP, wait=TxStatus(status="failed"))
    _patch_fwd_factory(monkeypatch, sign_fwd=sign_fwd, submit_fwd=submit_fwd)
    o = run_sign_uptime(_settings(), 0)
    assert o.status == OutcomeStatus.FAILED_TERMINAL


def test_fsp_outcome_ok_property():
    o_ok = FspOutcome("UPTIME", 0, OutcomeStatus.SUBMITTED_MINED, "ok")
    o_fail = FspOutcome("UPTIME", 0, OutcomeStatus.FAILED_TERMINAL, "fail")
    assert o_ok.ok is True
    assert o_fail.ok is False


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
