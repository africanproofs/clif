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
from py_flare_common.ftso.median import FtsoVote, calculate_median

from clif.observe.reward_rule import BandClass, classify_bands
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
    # FDC (protocol 200) — participation only when the round had attestation requests.
    fdc_request_count: int = 0  # AttestationRequests observed for this round (FdcHub)
    fdc_bitvote_seen: bool = False  # AP submitted an FDC bitvote (submit2.fdc)
    fdc_bitvote_len: int | None = None
    fdc_num_requests_claimed: int | None = None
    fdc_sig_seen: bool = False  # AP submitted an FDC signature (submitSignatures.fdc)
    fdc_gap: bool = False  # set at finalize: requests existed but we didn't bitvote
    # IQR reward-band scoring (Phase 2 — quality, not liveness). Transient per-round: all
    # registered voters' reveals accumulate here, AP's own values kept aside, both consumed at
    # finalize into `iqr_results` (then the raw votes are dropped to bound memory).
    iqr_votes: dict[int, list] = field(default_factory=dict)  # feed_idx -> [FtsoVote]
    iqr_ap_values: list | None = None  # AP's own submitted per-feed values this round
    iqr_results: dict[str, tuple[str, bool, bool]] = field(default_factory=dict)  # feed -> (band, pct_hit, capped)
    # verdict (set at finalize)
    reveal_offence: bool = False
    issues: list[str] = field(default_factory=list)

    @property
    def fdc_expected(self) -> bool:
        return self.fdc_request_count > 0

    @property
    def clean(self) -> bool:
        """A fully-healthy FTSO round: on-time commit + on-time reveal, no offence. FDC is a
        SEPARATE protocol tracked on its own axis (fdc_* aggregates), not folded in here."""
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
        # IQR context (set per reward epoch by the engine; None ⇒ IQR scoring off this session).
        self.iqr_offer = None  # reward_rule.OfferParams | None
        self.iqr_weight_map: dict[str, int] = {}  # {registered submit_addr_lc: weight}

    def set_iqr_context(self, offer, weight_map: dict[str, int]) -> None:
        """Install the per-reward-epoch band params + voter→weight map that turn on IQR scoring."""
        self.iqr_offer = offer
        self.iqr_weight_map = weight_map

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
            if decoded.fdc_present and decoded.fdc_round is not None:
                self._round(decoded.fdc_round).fdc_sig_seen = True
        # FDC bitvote rides in AP's submit2 tx (same sender) — record it on the FDC round.
        if (
            decoded.kind == "submit2" and frm == self._submit_lc
            and decoded.fdc_present and decoded.fdc_round is not None
        ):
            frs = self._round(decoded.fdc_round)
            frs.fdc_bitvote_seen = True
            frs.fdc_bitvote_len = decoded.fdc_bitvote_len
            frs.fdc_num_requests_claimed = decoded.fdc_num_requests

    def record_reveal_values(self, round_id: int, from_lc: str, values: list) -> None:
        """One submit2's per-feed values, from AP or a registered voter (IQR scoring only). AP's
        values are kept aside; a registered voter's are folded into the round's weighted votes."""
        rs = self._round(round_id)
        if from_lc == self._submit_lc:
            rs.iqr_ap_values = values
        w = self.iqr_weight_map.get(from_lc)
        if w:
            for i, v in enumerate(values):
                if v is not None:
                    rs.iqr_votes.setdefault(i, []).append(FtsoVote(value=int(v), weight=w))

    def _score_iqr(self, rs: RoundState) -> None:
        """At finalize: per feed, median/quartiles from ALL registered voters' reveals, then
        classify AP's own value (inner=IQR band, outer=PCT band). Raw votes dropped afterwards."""
        offer = self.iqr_offer
        ap_vals = rs.iqr_ap_values
        if offer is None or ap_vals is None:
            rs.iqr_votes = {}
            return
        for i, name in enumerate(offer.feeds):
            votes = rs.iqr_votes.get(i)
            if not votes or i >= len(ap_vals) or ap_vals[i] is None:
                continue
            m = calculate_median(votes)
            cls = classify_bands(
                value_raw=int(ap_vals[i]),
                q1_raw=m.first_quartile, q3_raw=m.third_quartile, median_raw=m.value,
                secondary_band_width_ppm=offer.secondary_band_width_ppm.get(name, 0),
            )
            band = {BandClass.INSIDE: "I", BandClass.BOUNDARY: "B"}.get(cls["band_class"], "O")
            rs.iqr_results[name] = (band, bool(cls["pct_hit"]), cls["band_ticks"] <= 1)
        rs.iqr_votes = {}  # free the per-round vote buffer

    def record_fdc_request(self, round_id: int) -> None:
        """One FdcHub AttestationRequest observed for `round_id` (the round of the block it
        was emitted in). Rounds accumulate a request count that gates FDC-participation checks."""
        self._round(round_id).fdc_request_count += 1

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
        # FDC participation — only when the round actually had attestation requests. The
        # ONLY robust signal is "requests existed → did AP bitvote at all": AP's own bitvote
        # uses the protocol's exact request set, while our per-block request COUNT is ±1 at
        # round boundaries, so we do NOT compare lengths (that produced false mismatches).
        if rs.fdc_expected and not rs.fdc_bitvote_seen:
            rs.fdc_gap = True
            rs.issues.append(f"FDC: no bitvote (~{rs.fdc_request_count} request(s) this round)")
        # IQR reward-band scoring (informational — never sets an issue or affects `clean`).
        self._score_iqr(rs)
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
        # IQR rollup over the window (feed-rounds): inner = inside + ½·boundary (expected primary
        # rate under the boundary coin-flip); outer = pct-band hits. `capped` = Q3−Q1 ≤ 1 tick.
        iqr_fr = iqr_inside = iqr_boundary = iqr_pct = iqr_capped = 0
        iqr_scored_rounds = 0
        for r in w:
            if r.iqr_results:
                iqr_scored_rounds += 1
            for band, pct, capped in r.iqr_results.values():
                iqr_fr += 1
                if band == "I":
                    iqr_inside += 1
                elif band == "B":
                    iqr_boundary += 1
                if pct:
                    iqr_pct += 1
                if capped:
                    iqr_capped += 1
        return {
            "window_rounds": n,
            "complete": sum(1 for r in w if r.clean),
            "missing_submit1": sum(1 for r in w if not r.submit1_seen),
            "missing_submit2": sum(1 for r in w if r.submit1_seen and not r.submit2_seen),
            "reveal_offences": sum(1 for r in w if r.reveal_offence),
            "off_window": sum(1 for r in w if (r.submit1_seen and not r.submit1_ontime) or (r.submit2_seen and not r.submit2_ontime)),
            "fdc_request_rounds": sum(1 for r in w if r.fdc_expected),
            "fdc_participated": sum(1 for r in w if r.fdc_expected and r.fdc_bitvote_seen and not r.fdc_gap),
            "fdc_missing": sum(1 for r in w if r.fdc_gap),
            "iqr_scored_rounds": iqr_scored_rounds,
            "iqr_feed_rounds": iqr_fr,
            "iqr_inside": iqr_inside,
            "iqr_boundary": iqr_boundary,
            "iqr_pct_hit": iqr_pct,
            "iqr_capped": iqr_capped,
            "recent_issues": [f"RD{r.round_id}: {'; '.join(r.issues)}" for r in w if r.issues][-8:],
        }
