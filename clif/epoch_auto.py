"""Epoch-anchored sign→claim state machine (the `clif epoch` daemon core).

Replaces the two always-on 15-min pollers (`fsp auto` + `auto`) with one
epoch-driven flow per the operator spec:

    epoch ends → (optional) sign uptime → wait until epoch_end + initial_delay,
    poll for reward publication → when published & unsigned, sign rewards →
    poll for >threshold finalization → claim ONLY that epoch → done → next epoch.

Idempotency is **chain-derived** (no durable phase state needed for correctness):
  - "have WE signed rewards/uptime for N" = FlareSystemsManager
    getVoterRewardsSignInfo / getVoterUptimeVoteSignInfo (ts != 0).
  - "is N finalized (>threshold weight signed)" = rewardsHash(N) != 0.
  - claim readiness + already-claimed = run_claim's on-chain pre-flight.
So a crash/restart re-derives each epoch's phase from chain and resumes.

Signing stays behind the existing FSP_AUTO_ENABLED hard-off gate (D15: a valid
signature over wrong data is irreversible). The UPTIME phase is additionally
gated OFF by default (UPTIME_AUTO_ENABLED). Threshold = the binary finalized
rewardsHash flip; the chain exposes no live signing-weight %.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from clif.autostate import AutoState, _ts_iso
from clif.claimer import OutcomeStatus, run_claim
from clif.config import ZERO_BYTES32, Settings
from clif.fsp import run_sign_rewards, run_sign_uptime
from clif.fwd_client import FwdClient
from clif.models import ClaimType
from clif.reward_data import get_reward_distribution_data
from clif.rpc import RpcClient, RpcError

log = logging.getLogger("clif")

# Bound catch-up after downtime: never reach back more than this many epochs
# (≈ a month at 3.5d/epoch). Older unhandled epochs are logged and skipped.
_MAX_CATCHUP = 8

# Claim-stream outcomes that mean "nothing left to do for this epoch+stream".
_CLAIM_DONE = {
    OutcomeStatus.SUBMITTED_MINED,
    OutcomeStatus.MINED_NOOP,
    OutcomeStatus.NOTHING_CLAIMABLE,
}


class Phase(str, Enum):
    REWARD_WAIT = "reward-wait"  # too early, or rewards not yet published
    REWARD_SIGN = "reward-sign"  # signing this cycle (or sign just attempted)
    CLAIM_WAIT = "claim-wait"  # signed; waiting for network finalization (>threshold)
    CLAIM = "claim"  # finalized; claiming this epoch
    DONE = "done"  # claimed (only this epoch)


@dataclass
class EpochObs:
    epoch: int
    phase: Phase
    detail: str
    done: bool = False
    terminal: bool = False
    # When set (REWARD_WAIT-too-early), the precise UNIX time this epoch next
    # becomes actionable (epoch_end + initial_delay) — lets the daemon sleep
    # exactly until the window opens instead of polling blindly.
    wait_until: float | None = None
    actions: list[tuple[str, str, str]] = field(default_factory=list)  # (leg, status, detail)


def resolve_voter(settings: Settings, rpc: RpcClient) -> str | None:
    """The FSP voter/signing-policy address used for getVoter*SignInfo reads.

    Prefer the explicit SIGNING_POLICY_ADDRESS; else resolve it from
    EntityManager.getVoterAddresses(identity). Returns None if neither is
    available (the SM cannot then tell "have we signed" — caller errors out).
    """
    if settings.signing_policy_address:
        return settings.signing_policy_address
    em = settings.net.entity_manager
    if em and settings.identity_address:
        try:
            _sa, _ssa, spa = rpc.get_voter_addresses(em, settings.identity_address)
            return spa or None
        except RpcError as exc:
            log.warning("epoch: could not resolve voter address from EntityManager: %s", exc)
    return None


def drive_epoch(
    settings: Settings,
    rpc: RpcClient,
    fwd: FwdClient,
    voter: str,
    claimers: list[tuple[int, str]],
    epoch: int,
    now: float,
    *,
    uptime_enabled: bool,
    initial_delay: int,
    epoch_end_ts: Callable[[int], int],
) -> EpochObs:
    """Advance a single closed reward epoch through its phases (one cycle)."""
    fsm = settings.net.flare_systems_manager
    actions: list[tuple[str, str, str]] = []

    # --- UPTIME phase (gated OFF by default; independent of rewards/claim) ---
    if uptime_enabled:
        if (
            rpc.uptime_vote_hash(fsm, epoch) == ZERO_BYTES32
            and rpc.voter_uptime_vote_sign_info(fsm, epoch, voter)[0] == 0
        ):
            uo = run_sign_uptime(settings, epoch, wait=True, rpc=rpc)
            actions.append(("uptime", uo.status.value, uo.detail))

    # --- REWARD phase ---
    # rewardsHash(epoch) REVERTS with "not signed yet" before the epoch is finalized (Songbird
    # FlareSystemsManager behaviour); treat that specific revert as finalized=False so we can
    # proceed to the sign check below.  Any other RpcError propagates as before.
    try:
        finalized = rpc.rewards_hash(fsm, epoch) != ZERO_BYTES32
    except RpcError as exc:
        if "not signed yet" in str(exc).lower():
            finalized = False
        else:
            raise
    signed_rewards = rpc.voter_rewards_sign_info(fsm, epoch, voter)[0] != 0

    if not finalized and not signed_rewards:
        end_ts = epoch_end_ts(epoch)
        if now < end_ts + initial_delay:
            return EpochObs(
                epoch,
                Phase.REWARD_WAIT,
                f"holding until epoch_end+{initial_delay}s (end_ts={end_ts})",
                wait_until=float(end_ts + initial_delay),
                actions=actions,
            )
        if get_reward_distribution_data(settings, epoch) is None:
            return EpochObs(epoch, Phase.REWARD_WAIT, "rewards not yet published", actions=actions)
        ro = run_sign_rewards(settings, epoch, wait=True, rpc=rpc)
        actions.append(("rewards", ro.status.value, ro.detail))
        if ro.status == OutcomeStatus.FAILED_TERMINAL:
            return EpochObs(
                epoch,
                Phase.REWARD_SIGN,
                f"reward sign TERMINAL: {ro.detail}",
                terminal=True,
                actions=actions,
            )
        if ro.status == OutcomeStatus.ALREADY_FINALIZED:
            finalized = True  # network finalized before us → fall through to claim
        else:
            # signed / pending / transient → wait for the network to finalize
            return EpochObs(
                epoch,
                Phase.REWARD_SIGN,
                f"reward sign {ro.status.value}; awaiting finalization",
                actions=actions,
            )
    elif not finalized and signed_rewards:
        return EpochObs(
            epoch,
            Phase.CLAIM_WAIT,
            "signed; awaiting network finalization (>threshold)",
            actions=actions,
        )

    # --- CLAIM phase (finalized) — only this epoch ---
    all_done = True
    any_terminal = False
    for ct, benef in claimers:
        co = run_claim(settings, rpc, fwd, int(ct), benef, only_epoch=epoch, wait=True)
        actions.append((f"claim:{ClaimType(int(ct)).name}", co.status.value, co.detail))
        if co.status not in _CLAIM_DONE:
            all_done = False
            if co.status == OutcomeStatus.FAILED_TERMINAL:
                any_terminal = True

    if all_done:
        return EpochObs(epoch, Phase.DONE, "claimed (only this epoch)", done=True, actions=actions)
    return EpochObs(
        epoch,
        Phase.CLAIM,
        "claim incomplete; retry next cycle",
        terminal=any_terminal,
        actions=actions,
    )


def run_cycle(
    settings: Settings,
    rpc: RpcClient,
    fwd: FwdClient,
    voter: str,
    claimers: list[tuple[int, str]],
    state: AutoState,
    last_done_epoch: int | None,
    now: float,
    *,
    uptime_enabled: bool,
    initial_delay: int,
    terminal_cooldown: int,
    epoch_end_ts: Callable[[int], int],
) -> tuple[int | None, int, list[EpochObs]]:
    """One poll cycle: process all closed-but-unhandled epochs (oldest first).

    Returns (new_last_done_epoch, current_epoch, observations). last_done_epoch
    advances contiguously from the bottom as epochs reach DONE; newer epochs are
    still processed even if an older one is stuck (so a stuck epoch never blocks
    a fresh one — it just stays loud/degraded).
    """
    fsm = settings.net.flare_systems_manager
    current = rpc.get_current_reward_epoch_id(fsm)
    key = f"{settings.network}:epoch"

    if last_done_epoch is None:
        # Fresh start: only handle epochs that close while we run (no retro-claim
        # of history). Operator backfills explicitly via --from-epoch.
        last_done_epoch = current - 1

    start = max(last_done_epoch + 1, current - _MAX_CATCHUP, 0)
    if start > last_done_epoch + 1:
        log.warning(
            "epoch: catch-up capped — skipping epochs %s..%s (>%s behind)",
            last_done_epoch + 1,
            start - 1,
            _MAX_CATCHUP,
        )

    observations: list[EpochObs] = []
    new_last_done = last_done_epoch
    advancing = True  # contiguous low-watermark advance
    for epoch in range(start, current):  # closed epochs only (< current)
        if state.in_cooldown(key, epoch, now):
            observations.append(
                EpochObs(epoch, Phase.CLAIM, "in terminal cooldown — skipping", terminal=True)
            )
            advancing = False
            continue
        obs = drive_epoch(
            settings,
            rpc,
            fwd,
            voter,
            claimers,
            epoch,
            now,
            uptime_enabled=uptime_enabled,
            initial_delay=initial_delay,
            epoch_end_ts=epoch_end_ts,
        )
        observations.append(obs)
        if obs.terminal:
            state.record_terminal(key, epoch, now, terminal_cooldown)
        if obs.done and advancing:
            new_last_done = epoch
        else:
            advancing = False

    # Track active (non-done) epochs for staleness/degraded evaluation.
    active = [o.epoch for o in observations if not o.done]
    state.observe(key, active, now)
    if observations:
        state.record_attempt(key, now, observations[-1].phase.value)
        if any(o.done for o in observations):
            state.record_success(key, now)
    return new_last_done, current, observations


def build_epoch_report(
    state: AutoState,
    network: str,
    poll_interval_sec: int,
    stale_after_sec: int,
    last_done_epoch: int | None,
    current_epoch: int | None,
    observations: list[EpochObs],
    now: float,
) -> dict:
    """Status snapshot compatible with autostate.status_exit_code (staleness +
    degraded), plus the per-epoch phase view."""
    degraded, reasons = state.evaluate(now, stale_after_sec)
    return {
        "updated_at": _ts_iso(now),
        "updated_at_ts": now,
        "network": network,
        "poll_interval_sec": poll_interval_sec,
        "stale_after_sec": stale_after_sec,
        "last_done_epoch": last_done_epoch,
        "current_epoch": current_epoch,
        "degraded": degraded,
        "reasons": reasons,
        "epochs": [
            {
                "epoch": o.epoch,
                "phase": o.phase.value,
                "detail": o.detail,
                "done": o.done,
                "actions": [{"leg": leg, "status": st, "detail": d} for (leg, st, d) in o.actions],
            }
            for o in observations
        ],
    }


def make_epoch_end_ts(first_reward_epoch_start_ts: int, reward_epoch_duration_sec: int):
    """Closure: epoch_end_ts(N) = first + (N+1)*duration — the EXPECTED end of any
    reward epoch (apgateway's constant-derived model). Works for the current/next
    (not-yet-closed) epoch, unlike a per-epoch getRewardEpochStartInfo read."""

    def _end(epoch: int) -> int:
        return first_reward_epoch_start_ts + (epoch + 1) * reward_epoch_duration_sec

    return _end


def next_sleep_seconds(
    observations: list[EpochObs],
    current_epoch: int | None,
    epoch_end_ts: Callable[[int], int],
    now: float,
    *,
    poll_interval: int,
    initial_delay: int,
) -> float:
    """How long to sleep before the next cycle — precise, epoch-anchored.

    - all caught up (no active epoch) → sleep until the CURRENT epoch's
      reward window opens (epoch_end + initial_delay) — true idle, not a poll;
    - an active epoch waiting too-early → sleep until the earliest wait_until;
    - an active epoch polling for publication / finalization / claim → poll_interval.
    Clamped to [60s, max(poll_interval, 3600s)]: the ceiling keeps the status
    file fresh for monitoring and re-checks epoch advance ≥ hourly; the floor
    avoids a busy loop.
    """
    floor = 60.0
    ceiling = float(max(poll_interval, 3600))
    active = [o for o in observations if not o.done]
    if not active:
        # Nothing to do until the current epoch ends and its window opens.
        wake = (
            (epoch_end_ts(current_epoch) + initial_delay)
            if current_epoch is not None
            else now + poll_interval
        )
    else:
        candidates: list[float] = []
        for o in active:
            candidates.append(o.wait_until if o.wait_until is not None else now + poll_interval)
        wake = min(candidates)
    return max(floor, min(wake - now, ceiling))


def _fmt_ts(ts: float) -> str:
    """UNIX ts → 'YYYY-MM-DDTHH:MM:SSZ' (UTC, second precision) — matches the log clock."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_dur(seconds: float) -> str:
    """Seconds → compact human countdown: 'NdNh' / 'NhNm' / 'NmNs' / 'Ns'."""
    s = int(max(0, seconds))
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m, sec = divmod(r, 60)
    if d:
        return f"{d}d{h}h"
    if h:
        return f"{h}h{m}m"
    if m:
        return f"{m}m{sec}s"
    return f"{sec}s"


def schedule_line(
    observations: list[EpochObs],
    current_epoch: int | None,
    epoch_end_ts: Callable[[int], int],
    now: float,
    *,
    poll_interval: int,
    initial_delay: int,
) -> str:
    """One-line 'what to expect and when' for the daemon log: what each active epoch is
    waiting for + the ABSOLUTE next-action time + a countdown. Mirrors next_sleep_seconds'
    wake logic so the narrative and the actual sleep agree."""
    active = [o for o in observations if not o.done]
    if not active:
        if current_epoch is None:
            return f"idle — no current epoch resolved yet; re-checking in {_fmt_dur(poll_interval)}"
        wake = float(epoch_end_ts(current_epoch) + initial_delay)
        return (
            f"idle — caught up; next reward window (epoch {current_epoch} end +{initial_delay}s) "
            f"opens {_fmt_ts(wake)} (in {_fmt_dur(wake - now)})"
        )
    parts: list[str] = []
    for o in active:
        if o.wait_until is not None:
            parts.append(
                f"epoch {o.epoch} {o.phase.value} — {o.detail}; actionable {_fmt_ts(o.wait_until)} "
                f"(in {_fmt_dur(o.wait_until - now)})"
            )
        else:
            nxt = now + poll_interval
            parts.append(
                f"epoch {o.epoch} {o.phase.value} — {o.detail}; polling, next check "
                f"{_fmt_ts(nxt)} (in {_fmt_dur(poll_interval)})"
            )
    return " | ".join(parts)


def build_disabled_report(network: str, poll_interval_sec: int, now: float) -> dict:
    """Status snapshot for a daemon idling because FSP_AUTO_ENABLED!=true. `disabled` is
    treated as HEALTHY by autostate.status_exit_code (intentionally off, not broken) and
    bypasses the staleness check, so an idle daemon's docker healthcheck stays green."""
    return {
        "updated_at": _ts_iso(now),
        "updated_at_ts": now,
        "network": network,
        "poll_interval_sec": poll_interval_sec,
        "disabled": True,
        "degraded": False,
        "reasons": [],
        "epochs": [],
    }
