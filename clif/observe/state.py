"""Per-voting-round participation state + the FTSO checks (ported from fsp-observer ftso.py).

Tracks, per round, whether AP's OWN submit1/submit2/submitSignatures landed, on time, and —
for the reveal — whether it matches the commit (reveal offence). A rolling window of finalized
rounds feeds the health severity. OBSERVE-only.

Deferred to Phase 2 (marked): minimal-conditions value checks (need all-voter medians) and the
signature-vs-finalization recovery (need the Relay finalization event + ecrecover).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from py_flare_common.ftso.commit import commit_hash

from clif.observe.timing import submit1_window, submit2_window


@dataclass
class RoundState:
    round_id: int
    submit1_seen: bool = False
    submit1_ontime: bool = False
    submit1_commit: bytes | None = None
    submit2_seen: bool = False
    submit2_ontime: bool = False
    submit2_random: int | None = None
    submit2_feed_bytes: bytes | None = None
    sig_seen: bool = False
    # verdict (set at finalize)
    reveal_offence: bool = False
    issues: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """A fully-healthy round: on-time commit + on-time reveal, no offence."""
        return (
            self.submit1_seen and self.submit1_ontime
            and self.submit2_seen and self.submit2_ontime
            and not self.reveal_offence
        )


class ObserverState:
    """Accumulates rounds as blocks stream in; finalizes each once its windows close."""

    def __init__(
        self, network: str, our_submit: str, our_sig: str, window_rounds: int = 40,
        observe_start_ts: int = 0,
    ) -> None:
        self.network = network
        self.our_submit = our_submit  # checksum form used for commit_hash
        self._submit_lc = our_submit.lower()
        self._sig_lc = our_sig.lower()
        # Rounds whose submit1 window opened before we started streaming are incomplete by
        # construction (we couldn't have seen their commit) — dropped, never counted, so a
        # fresh start / restart doesn't false-alarm on the boundary round.
        self.observe_start_ts = observe_start_ts
        self.rounds: dict[int, RoundState] = {}
        self.finalized: deque[RoundState] = deque(maxlen=window_rounds)
        self.last_block: int | None = None
        self.last_ts: int | None = None
        self.last_round_finalized: int | None = None

    def _round(self, rid: int) -> RoundState:
        rs = self.rounds.get(rid)
        if rs is None:
            rs = RoundState(rid)
            self.rounds[rid] = rs
        return rs

    def record(self, decoded, from_addr: str, tx_ts: int, factory) -> None:
        """Record one decoded submit tx from one of OUR addresses into its round."""
        frm = from_addr.lower()
        rs = self._round(decoded.round_id)
        ep = factory.make_epoch(decoded.round_id)
        if decoded.kind == "submit1" and frm == self._submit_lc:
            lo, hi = submit1_window(ep)
            rs.submit1_seen = True
            rs.submit1_ontime = lo <= tx_ts <= hi
            rs.submit1_commit = decoded.commit_hash
        elif decoded.kind == "submit2" and frm == self._submit_lc:
            lo, hi = submit2_window(ep)
            rs.submit2_seen = True
            rs.submit2_ontime = lo <= tx_ts <= hi
            rs.submit2_random = decoded.reveal_random
            rs.submit2_feed_bytes = decoded.reveal_feed_bytes
        elif decoded.kind == "signatures" and frm == self._sig_lc:
            rs.sig_seen = True

    def _finalize(self, rs: RoundState) -> None:
        """Run the FTSO checks for a round whose windows have closed (ftso.py logic)."""
        # submit1 (commit)
        if not rs.submit1_seen:
            rs.issues.append("no submit1 (commit)")
        elif not rs.submit1_ontime:
            rs.issues.append("submit1 outside window")
        # submit2 (reveal) — a missing reveal after a commit is a reveal offence
        if not rs.submit2_seen:
            if rs.submit1_seen:
                rs.reveal_offence = True
                rs.issues.append("no submit2 after submit1 — REVEAL OFFENCE")
            else:
                rs.issues.append("no submit2 (reveal)")
        elif not rs.submit2_ontime:
            rs.issues.append("submit2 outside window")
        # commit/reveal consistency
        if rs.submit1_seen and rs.submit2_seen and rs.submit1_commit is not None:
            recon = commit_hash(
                self.our_submit, rs.round_id, rs.submit2_random or 0, rs.submit2_feed_bytes or b""
            )
            if rs.submit1_commit.hex() != recon:
                rs.reveal_offence = True
                rs.issues.append("commit/reveal mismatch — REVEAL OFFENCE")
        self.finalized.append(rs)
        self.last_round_finalized = rs.round_id

    def finalize_due(self, now_ts: int, factory) -> list[RoundState]:
        """Finalize every tracked round whose next round has fully ended (windows closed) —
        mirrors fsp-observer's `round_completed = k.next.end_s < block.timestamp`."""
        done: list[RoundState] = []
        for rid in sorted(self.rounds):
            ep = factory.make_epoch(rid)
            if ep.next.end_s < now_ts:
                rs = self.rounds.pop(rid)
                if ep.start_s < self.observe_start_ts:
                    continue  # boundary round — we couldn't have seen its commit; drop, don't count
                self._finalize(rs)
                done.append(rs)
        return done

    # --- rolling aggregates over the finalized window ---
    def aggregates(self) -> dict:
        w = list(self.finalized)
        n = len(w)
        return {
            "window_rounds": n,
            "complete": sum(1 for r in w if r.clean),
            "missing_submit1": sum(1 for r in w if not r.submit1_seen),
            "missing_submit2": sum(1 for r in w if r.submit1_seen and not r.submit2_seen),
            "reveal_offences": sum(1 for r in w if r.reveal_offence),
            "off_window": sum(1 for r in w if (r.submit1_seen and not r.submit1_ontime) or (r.submit2_seen and not r.submit2_ontime)),
            "recent_issues": [f"RD{r.round_id}: {'; '.join(r.issues)}" for r in w if r.issues][-8:],
        }
