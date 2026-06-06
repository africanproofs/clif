"""Epoch state-machine phase transitions + watermark/catch-up — mocked RpcClient.

The reward-sign / claim primitives (run_sign_rewards, run_sign_uptime, run_claim)
and the publication fetch (get_reward_distribution_data) are stubbed; these tests
exercise epoch_auto's PHASE logic and run_cycle's contiguous-watermark advance.
"""

from __future__ import annotations

import clif.epoch_auto as ea
from clif.claimer import ClaimOutcome, OutcomeStatus
from clif.config import ZERO_BYTES32, Settings
from clif.epoch_auto import EpochObs, Phase, drive_epoch, run_cycle
from clif.fsp import FspOutcome

NONZERO = "0x" + "ab" * 32
VOTER = "0x" + "11" * 20
BENEF = "0x" + "22" * 20
FSM = "0xbC1F76CEB521Eb5484b8943B5462D08ea96617A1"  # coston2 fsm (unused — reads are faked)
CLAIMERS = [(2, BENEF)]  # one DIRECT stream


def _settings(**over):
    base = dict(network="coston2", signing_policy_address=VOTER, identity_address=BENEF)
    base.update(over)
    return Settings(_env_file=None, **base)


class FakeRpc:
    def __init__(self, *, current=10, end_ts=1_000, rewards_hash=ZERO_BYTES32,
                 signed_rewards=False, uptime_hash=ZERO_BYTES32, signed_uptime=False):
        self.current = current
        self.end_ts = end_ts
        self._rh = rewards_hash
        self._sr = signed_rewards
        self._uh = uptime_hash
        self._su = signed_uptime

    def get_current_reward_epoch_id(self, _fsm):
        return self.current

    def reward_epoch_end_ts(self, _fsm, _epoch):
        return self.end_ts

    def rewards_hash(self, _fsm, _epoch):
        return self._rh

    def voter_rewards_sign_info(self, _fsm, _epoch, _voter):
        return (1, 1) if self._sr else (0, 0)

    def uptime_vote_hash(self, _fsm, _epoch):
        return self._uh

    def voter_uptime_vote_sign_info(self, _fsm, _epoch, _voter):
        return (1, 1) if self._su else (0, 0)


def _fsp(status, detail="x", epoch=5):
    return FspOutcome("REWARD_DISTRIBUTION", epoch, status, detail)


def _claim(status, detail="x"):
    return ClaimOutcome(2, "DIRECT", BENEF, status, detail)


def _patch(monkeypatch, *, rdd=object(), sign=None, claim=None, uptime=None):
    monkeypatch.setattr(ea, "get_reward_distribution_data", lambda *_a, **_k: rdd)
    if sign is not None:
        monkeypatch.setattr(ea, "run_sign_rewards", lambda *_a, **_k: sign)
    if claim is not None:
        monkeypatch.setattr(ea, "run_claim", lambda *_a, **_k: claim)
    if uptime is not None:
        monkeypatch.setattr(ea, "run_sign_uptime", lambda *_a, **_k: uptime)


def _drive(rpc, **kw):
    return drive_epoch(
        _settings(), rpc, object(), VOTER, CLAIMERS, 5, kw.pop("now", 10_000),
        uptime_enabled=kw.pop("uptime_enabled", False),
        initial_delay=kw.pop("initial_delay", 3600),
    )


# --- REWARD phase gates ------------------------------------------------------

def test_reward_wait_too_early(monkeypatch):
    _patch(monkeypatch)
    rpc = FakeRpc(end_ts=1_000)
    obs = _drive(rpc, now=1_500, initial_delay=3600)  # 1500 < 1000+3600
    assert obs.phase is Phase.REWARD_WAIT and not obs.done
    assert "holding until" in obs.detail


def test_reward_wait_not_published(monkeypatch):
    _patch(monkeypatch, rdd=None)  # publication fetch returns None
    rpc = FakeRpc(end_ts=1_000)
    obs = _drive(rpc, now=10_000, initial_delay=3600)  # past the delay
    assert obs.phase is Phase.REWARD_WAIT
    assert "not yet published" in obs.detail


def test_reward_sign_then_await_finalization(monkeypatch):
    _patch(monkeypatch, sign=_fsp(OutcomeStatus.SUBMITTED_MINED))
    obs = _drive(FakeRpc(end_ts=1_000), now=10_000)
    assert obs.phase is Phase.REWARD_SIGN and not obs.done
    assert any(a[0] == "rewards" for a in obs.actions)


def test_reward_sign_terminal(monkeypatch):
    _patch(monkeypatch, sign=_fsp(OutcomeStatus.FAILED_TERMINAL, "merkle mismatch"))
    obs = _drive(FakeRpc(end_ts=1_000), now=10_000)
    assert obs.phase is Phase.REWARD_SIGN and obs.terminal


def test_already_finalized_before_sign_goes_to_claim(monkeypatch):
    # sign returns ALREADY_FINALIZED → fall through to claim (which succeeds → DONE)
    _patch(
        monkeypatch,
        sign=_fsp(OutcomeStatus.ALREADY_FINALIZED),
        claim=_claim(OutcomeStatus.SUBMITTED_MINED),
    )
    obs = _drive(FakeRpc(rewards_hash=ZERO_BYTES32, end_ts=1_000), now=10_000)
    assert obs.phase is Phase.DONE and obs.done


def test_signed_awaiting_finalization(monkeypatch):
    _patch(monkeypatch)
    rpc = FakeRpc(rewards_hash=ZERO_BYTES32, signed_rewards=True)
    obs = _drive(rpc)
    assert obs.phase is Phase.CLAIM_WAIT and not obs.done


# --- CLAIM phase (finalized) -------------------------------------------------

def test_finalized_claim_done(monkeypatch):
    _patch(monkeypatch, claim=_claim(OutcomeStatus.SUBMITTED_MINED))
    obs = _drive(FakeRpc(rewards_hash=NONZERO))
    assert obs.phase is Phase.DONE and obs.done


def test_finalized_nothing_claimable_is_done(monkeypatch):
    # no accrual for this beneficiary counts as done (nothing left to do)
    _patch(monkeypatch, claim=_claim(OutcomeStatus.NOTHING_CLAIMABLE))
    obs = _drive(FakeRpc(rewards_hash=NONZERO))
    assert obs.phase is Phase.DONE and obs.done


def test_finalized_claim_retryable_not_done(monkeypatch):
    _patch(monkeypatch, claim=_claim(OutcomeStatus.FAILED_RETRYABLE))
    obs = _drive(FakeRpc(rewards_hash=NONZERO))
    assert obs.phase is Phase.CLAIM and not obs.done


# --- UPTIME phase gate -------------------------------------------------------

def test_uptime_off_does_not_sign(monkeypatch):
    calls = []
    _patch(monkeypatch, claim=_claim(OutcomeStatus.SUBMITTED_MINED))
    monkeypatch.setattr(ea, "run_sign_uptime", lambda *a, **k: calls.append(1) or _fsp(OutcomeStatus.SUBMITTED_MINED))
    _drive(FakeRpc(rewards_hash=NONZERO), uptime_enabled=False)
    assert calls == []  # uptime never attempted when gated off


def test_uptime_on_signs_when_unsigned(monkeypatch):
    calls = []
    _patch(monkeypatch, claim=_claim(OutcomeStatus.SUBMITTED_MINED))
    monkeypatch.setattr(
        ea, "run_sign_uptime",
        lambda *a, **k: calls.append(1) or _fsp(OutcomeStatus.SUBMITTED_MINED),
    )
    _drive(
        FakeRpc(rewards_hash=NONZERO, uptime_hash=ZERO_BYTES32, signed_uptime=False),
        uptime_enabled=True,
    )
    assert calls == [1]  # uptime signed once


# --- run_cycle watermark / catch-up ------------------------------------------

def test_run_cycle_advances_contiguously(monkeypatch):
    # epochs 7,8 closed (current=9); both DONE → last_done advances to 8.
    seen = []
    monkeypatch.setattr(
        ea, "drive_epoch",
        lambda *a, **k: seen.append(a[5]) or EpochObs(a[5], Phase.DONE, "done", done=True),
    )
    rpc = FakeRpc(current=9)
    new_last, current, obs = run_cycle(
        _settings(), rpc, object(), VOTER, CLAIMERS, ea.AutoState(), 6, 100.0,
        uptime_enabled=False, initial_delay=3600, terminal_cooldown=3600,
    )
    assert current == 9 and seen == [7, 8] and new_last == 8
    assert all(o.done for o in obs)


def test_run_cycle_stops_advance_on_stuck(monkeypatch):
    # epoch 7 stuck (not done), 8 done → last_done stays 6 (contiguous from bottom),
    # but BOTH are still processed (8 not blocked by 7).
    seen = []

    def fake_drive(*a, **k):
        e = a[5]
        seen.append(e)
        done = e != 7  # 7 is stuck
        return EpochObs(e, Phase.DONE if done else Phase.CLAIM_WAIT, "x", done=done)

    monkeypatch.setattr(ea, "drive_epoch", fake_drive)
    rpc = FakeRpc(current=9)
    new_last, _current, _obs = run_cycle(
        _settings(), rpc, object(), VOTER, CLAIMERS, ea.AutoState(), 6, 100.0,
        uptime_enabled=False, initial_delay=3600, terminal_cooldown=3600,
    )
    assert seen == [7, 8]  # newer epoch still processed
    assert new_last == 6  # watermark did NOT advance past the stuck 7
