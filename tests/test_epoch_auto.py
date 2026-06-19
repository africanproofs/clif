"""Epoch state-machine phase transitions + watermark/catch-up — mocked RpcClient.

The reward-sign / claim primitives (run_sign_rewards, run_sign_uptime, run_claim)
and the publication fetch (get_reward_distribution_data) are stubbed; these tests
exercise epoch_auto's PHASE logic and run_cycle's contiguous-watermark advance.
"""

from __future__ import annotations

import clif.epoch_auto as ea
from clif.claimer import ClaimOutcome, OutcomeStatus
from clif.config import ZERO_BYTES32, Settings
from clif.epoch_auto import (
    EpochObs,
    Phase,
    drive_epoch,
    make_epoch_end_ts,
    next_sleep_seconds,
    run_cycle,
)
from clif.fsp import FspOutcome
from clif.rpc import RpcError

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
                 signed_rewards=False, uptime_hash=ZERO_BYTES32, signed_uptime=False,
                 revert=False):
        self.current = current
        self.end_ts = end_ts
        self._rh = rewards_hash
        self._sr = signed_rewards
        self._uh = uptime_hash
        self._su = signed_uptime
        # revert=True simulates the pre-finalization FSM behaviour: rewardsHash AND
        # getVoterRewardsSignInfo both revert with "rewards hash not signed yet".
        self._revert = revert

    def get_current_reward_epoch_id(self, _fsm):
        return self.current

    def reward_epoch_end_ts(self, _fsm, _epoch):
        return self.end_ts

    def rewards_hash(self, _fsm, _epoch):
        if self._revert:
            raise RpcError("execution reverted: rewards hash not signed yet")
        return self._rh

    def voter_rewards_sign_info(self, _fsm, _epoch, _voter):
        if self._revert:
            raise RpcError("execution reverted: rewards hash not signed yet")
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
        epoch_end_ts=kw.pop("epoch_end_ts", lambda _e: rpc.end_ts),
        our_signed_fn=kw.pop("our_signed_fn", None),
        retry_counts=kw.pop("retry_counts", None),
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


# --- pre-finalization revert: event-based "already signed" (no spurious re-sign) ---

def test_revert_with_event_signed_goes_to_claim_wait(monkeypatch):
    # View reverts pre-finalization; the RewardsSigned event check says WE already
    # signed → CLAIM_WAIT, NOT a re-sign (which would hit fwd's idempotency_conflict
    # → a false TERMINAL). run_sign_rewards must NOT be called.
    calls = []
    monkeypatch.setattr(
        ea, "run_sign_rewards",
        lambda *a, **k: calls.append(1) or _fsp(OutcomeStatus.SUBMITTED_MINED),
    )
    obs = _drive(FakeRpc(revert=True, end_ts=1_000), now=10_000, our_signed_fn=lambda _e: True)
    assert obs.phase is Phase.CLAIM_WAIT and not obs.terminal and not obs.done
    assert calls == []  # never re-attempted the sign


def test_revert_with_event_not_signed_attempts_sign(monkeypatch):
    # View reverts; event check says NOT signed → proceeds to sign (current behaviour).
    _patch(monkeypatch, sign=_fsp(OutcomeStatus.SUBMITTED_MINED))
    obs = _drive(FakeRpc(revert=True, end_ts=1_000), now=10_000, our_signed_fn=lambda _e: False)
    assert obs.phase is Phase.REWARD_SIGN and not obs.done
    assert any(a[0] == "rewards" for a in obs.actions)


def test_revert_no_signed_fn_attempts_sign(monkeypatch):
    # No event check available (None) → assume not-signed → sign (no regression).
    _patch(monkeypatch, sign=_fsp(OutcomeStatus.SUBMITTED_MINED))
    obs = _drive(FakeRpc(revert=True, end_ts=1_000), now=10_000)  # our_signed_fn defaults None
    assert obs.phase is Phase.REWARD_SIGN and not obs.done


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
        epoch_end_ts=lambda _e: 0,
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
        epoch_end_ts=lambda _e: 0,
    )
    assert seen == [7, 8]  # newer epoch still processed
    assert new_last == 6  # watermark did NOT advance past the stuck 7


def test_run_cycle_threads_our_signed_fn(monkeypatch):
    seen = {}

    def fake_drive(*a, **k):
        seen["fn"] = k.get("our_signed_fn")
        return EpochObs(a[5], Phase.CLAIM_WAIT, "x", done=False)

    monkeypatch.setattr(ea, "drive_epoch", fake_drive)

    def sentinel(_e):
        return True

    run_cycle(
        _settings(), FakeRpc(current=8), object(), VOTER, CLAIMERS, ea.AutoState(), 6, 100.0,
        uptime_enabled=False, initial_delay=3600, terminal_cooldown=3600,
        epoch_end_ts=lambda _e: 0, our_signed_fn=sentinel,
    )
    assert seen["fn"] is sentinel


# --- apgateway-informed timing + smart sleep --------------------------------

def test_make_epoch_end_ts_math():
    end = make_epoch_end_ts(1_000_000, 302_400)
    assert end(0) == 1_302_400  # first + 1*duration
    assert end(5) == 1_000_000 + 6 * 302_400


def test_sleep_all_done_waits_for_next_window():
    # caught up → wake at current epoch's end + initial_delay (precise, within ceiling)
    obs = [EpochObs(7, Phase.DONE, "done", done=True)]
    end = make_epoch_end_ts(0, 1000)  # end(0)=1000
    s = next_sleep_seconds(obs, 0, end, now=600.0, poll_interval=1800, initial_delay=0)
    assert s == 400.0  # 1000 - 600


def test_sleep_capped_at_ceiling():
    obs = [EpochObs(7, Phase.DONE, "done", done=True)]
    end = make_epoch_end_ts(0, 1000)
    s = next_sleep_seconds(obs, 0, end, now=0.0, poll_interval=1800, initial_delay=100_000)
    assert s == 3600.0  # max(poll_interval, 3600) ceiling


def test_sleep_too_early_uses_wait_until():
    obs = [EpochObs(5, Phase.REWARD_WAIT, "holding", wait_until=10_500.0)]
    s = next_sleep_seconds(obs, 6, lambda _e: 0, now=10_000.0, poll_interval=1800, initial_delay=3600)
    assert s == 500.0  # exactly until the window opens


def test_sleep_active_wait_uses_poll_interval():
    obs = [EpochObs(5, Phase.CLAIM_WAIT, "awaiting finalization")]  # wait_until None
    s = next_sleep_seconds(obs, 6, lambda _e: 0, now=10_000.0, poll_interval=1800, initial_delay=3600)
    assert s == 1800.0


def test_sleep_floor_when_overdue():
    obs = [EpochObs(5, Phase.REWARD_WAIT, "holding", wait_until=9_900.0)]  # already past
    s = next_sleep_seconds(obs, 6, lambda _e: 0, now=10_000.0, poll_interval=1800, initial_delay=3600)
    assert s == 60.0  # floor


# ---- daemon log narrative helpers (v0.5.22) ----

def test_fmt_ts_utc():
    assert ea._fmt_ts(0) == "1970-01-01T00:00:00Z"
    assert ea._fmt_ts(3661) == "1970-01-01T01:01:01Z"


def test_fmt_dur_buckets():
    assert ea._fmt_dur(0) == "0s"
    assert ea._fmt_dur(45) == "45s"
    assert ea._fmt_dur(90) == "1m30s"
    assert ea._fmt_dur(3600 + 13 * 60) == "1h13m"
    assert ea._fmt_dur(2 * 86400 + 3 * 3600) == "2d3h"
    assert ea._fmt_dur(-5) == "0s"  # negatives clamp to 0


def test_schedule_line_idle_names_next_window():
    end = make_epoch_end_ts(0, 1000)  # end(7) = 8000
    obs = [EpochObs(7, Phase.DONE, "done", done=True)]
    line = ea.schedule_line(obs, 7, end, now=1000.0, poll_interval=1800, initial_delay=100)
    assert "idle — caught up" in line
    assert ea._fmt_ts(8100.0) in line  # end(7)=8000 + initial_delay 100
    assert "(in " in line


def test_schedule_line_too_early_uses_wait_until():
    obs = [EpochObs(5, Phase.REWARD_WAIT, "holding", wait_until=10_500.0)]
    line = ea.schedule_line(obs, 5, lambda _e: 0, now=10_000.0, poll_interval=1800, initial_delay=3600)
    assert "epoch 5 reward-wait" in line
    assert "actionable " + ea._fmt_ts(10_500.0) in line


def test_schedule_line_polling_uses_poll_interval():
    obs = [EpochObs(5, Phase.CLAIM_WAIT, "awaiting finalization")]  # wait_until None
    line = ea.schedule_line(obs, 5, lambda _e: 0, now=10_000.0, poll_interval=1800, initial_delay=3600)
    assert "next check " + ea._fmt_ts(11_800.0) in line  # now + poll_interval


def test_build_disabled_report_shape():
    rep = ea.build_disabled_report("songbird", 1800, now=42.0)
    assert rep["disabled"] is True
    assert rep["degraded"] is False
    assert rep["network"] == "songbird"
    assert rep["updated_at_ts"] == 42.0
    assert rep["epochs"] == []


# --- reward-sign idempotency retry-token bump (durable nonce-wedge fix) -------

def test_sign_retry_token_helper():
    from clif.epoch_auto import _sign_retry_token
    assert _sign_retry_token(None, 0) is None
    assert _sign_retry_token("base", 0) == "base"
    assert _sign_retry_token(None, 1) == "r1"
    assert _sign_retry_token("base", 2) == "base-r2"


def test_retryable_reward_sign_bumps_retry_count_and_token(monkeypatch):
    """A FAILED_RETRYABLE reward-sign bumps the per-epoch count, and the NEXT
    attempt re-signs under a fresh idempotency key (the wedge self-heals)."""
    seen_retry = []
    seq = [
        _fsp(OutcomeStatus.FAILED_RETRYABLE, "broadcast rejected (nonce too low)"),
        _fsp(OutcomeStatus.SUBMITTED_MINED),
    ]

    def fake_sign(*_a, **kw):
        seen_retry.append(kw.get("retry"))
        return seq.pop(0)

    monkeypatch.setattr(ea, "get_reward_distribution_data", lambda *_a, **_k: object())
    monkeypatch.setattr(ea, "run_sign_rewards", fake_sign)
    rpc = FakeRpc(end_ts=1_000)
    rc: dict[int, int] = {}

    # cycle 1: retryable → count bumped to 1, still awaiting finalization (not terminal)
    obs1 = _drive(rpc, now=10_000, retry_counts=rc)
    assert obs1.phase is Phase.REWARD_SIGN and not obs1.done and not obs1.terminal
    assert rc[5] == 1
    assert seen_retry[0] is None  # first attempt used the base (default) token

    # cycle 2: re-signs under the bumped token, succeeds
    obs2 = _drive(rpc, now=10_000, retry_counts=rc)
    assert obs2.phase is Phase.REWARD_SIGN and not obs2.done
    assert seen_retry[1] == "r1"  # fresh key on the re-attempt


def test_persistent_retryable_reward_sign_goes_terminal(monkeypatch):
    """If fresh keys don't help (persistent fwd-nonce drift), it surfaces terminal
    with a nonce-sync hint rather than looping silently forever."""
    _patch(monkeypatch, sign=_fsp(OutcomeStatus.FAILED_RETRYABLE, "nonce too low"))
    rpc = FakeRpc(end_ts=1_000)
    rc: dict[int, int] = {}
    obs = None
    for _ in range(3):
        obs = _drive(rpc, now=10_000, retry_counts=rc)
    assert obs.terminal and obs.phase is Phase.REWARD_SIGN
    assert "nonce-sync" in obs.detail
