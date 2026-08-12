"""Voting-epoch timing — a thin wrapper over py-flare-common's per-network factories.

We never hand-compute round boundaries; py-flare-common's `voting_epoch_factory` owns
firstVotingRoundStartTs / votingEpochDurationSeconds / reveal-deadline math.
"""

from __future__ import annotations

from py_flare_common.fsp.epoch.timing import coston, coston2, flare, songbird

_NET = {"flare": flare, "songbird": songbird, "coston2": coston2, "coston": coston}


def voting_factory(network: str):
    """The py-flare-common VotingEpochFactory for a network (has make_epoch / from_timestamp / now)."""
    mod = _NET.get(network)
    if mod is None:
        raise KeyError(f"no voting-epoch timing for network {network!r}")
    return mod.voting_epoch_factory


def submit1_window(epoch) -> tuple[int, int]:
    """[start, end] a submit1 (commit) for `epoch` should land in — the round's own window."""
    return epoch.start_s, epoch.end_s


def submit2_window(epoch) -> tuple[int, int]:
    """[start, deadline] a submit2 (reveal) for `epoch` should land in — the FIRST HALF of the
    NEXT round (reveal happens the round after the commit)."""
    nxt = epoch.next
    return nxt.start_s, nxt.reveal_deadline()


def signature_deadline(epoch, finalization_ts: int | None = None) -> int:
    """The grace deadline for submitSignatures of `epoch`: max(next round start + 60s, the
    on-chain finalization timestamp) — later of the two (ftso.py:271)."""
    return max(epoch.next.start_s + 60, finalization_ts or 0)
