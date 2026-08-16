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

from clif.observe.iqr_history import IqrTally
from clif.observe.reward_rule import VRS_PER_REWARD_EPOCH, BandClass, classify_bands
from clif.observe.timing import submit1_window, submit2_window

# Bounds on the in-flight rounds dict (leak prevention on indefinite runtime):
_FUTURE_ROUND_MARGIN = 4  # accept rounds up to this many ahead of current (clock skew / boundary)
_ROUNDS_CAP = 512  # hard ceiling; normal in-flight is ~1–3, so this only trips on orphan accretion


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
    iqr_tally: object | None = None  # the compact IqrTally built at finalize (for persistence)
    iqr_tally_new: bool = False  # True ⇒ this rid wasn't already in history (persist it)
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
        observe_start_ts: int = 0, factory=None,
    ) -> None:
        self.network = network
        self.our_submit = our_submit  # checksum form used for commit_hash
        self._submit_lc = our_submit.lower()
        self._sig_lc = our_sig.lower()
        # Rounds whose submit1 window opened before we started streaming are incomplete by
        # construction (we couldn't have seen their commit) — dropped, never counted, so a
        # fresh start / restart doesn't false-alarm on the boundary round.
        self.observe_start_ts = observe_start_ts
        self.factory = factory  # timing factory — lets _round() reject implausibly-far rounds
        self.rounds: dict[int, RoundState] = {}
        self.finalized: deque[RoundState] = deque(maxlen=window_rounds)
        self.last_block: int | None = None
        self.last_ts: int | None = None
        self.head: int | None = None  # latest chain head seen — for the lag / LIVE-vs-CATCHING-UP signal
        self.last_round_finalized: int | None = None
        # IQR context (set per reward epoch by the engine; None ⇒ IQR scoring off this session).
        self.iqr_offer = None  # reward_rule.OfferParams | None
        self.iqr_weight_map: dict[str, int] = {}  # {registered submit_addr_lc: weight}
        # Per-round IQR tallies for the multi-horizon rates (1h/6h/24h/since-epoch). Bounded to
        # a hair over one reward epoch; the engine seeds this from the persisted log on start.
        self.iqr_history: deque[IqrTally] = deque(maxlen=VRS_PER_REWARD_EPOCH + 240)
        self._iqr_rids: set[int] = set()  # dedup guard (a restart re-finalizes recent rounds)
        # Fast-updates (protocol 255): AP's FastUpdateFeedsSubmitted event timestamps — a
        # per-BLOCK signal (sortition-weighted), so a time-windowed count, not round-based.
        self.fu_events: deque[int] = deque(maxlen=4000)  # ~24h+ of AP updates

    def set_iqr_context(self, offer, weight_map: dict[str, int]) -> None:
        """Install the per-reward-epoch band params + voter→weight map that turn on IQR scoring."""
        self.iqr_offer = offer
        self.iqr_weight_map = weight_map

    def _remember_tally(self, t: IqrTally) -> bool:
        """Add a tally to the multi-horizon history, deduped by round id. Returns True if new."""
        if t.rid in self._iqr_rids:
            return False
        self._iqr_rids.add(t.rid)
        self.iqr_history.append(t)
        if len(self._iqr_rids) > (self.iqr_history.maxlen or 0):
            self._iqr_rids = {x.rid for x in self.iqr_history}  # resync after deque eviction
        return True

    def seed_iqr_history(self, tallies: list[IqrTally]) -> None:
        """Seed the history from the persisted log on start (deduped)."""
        for t in tallies:
            self._remember_tally(t)

    def _current_round(self) -> int | None:
        """The voting round of the last-processed block (None until we have a factory + last_ts)."""
        if self.factory is None or self.last_ts is None:
            return None
        try:
            return self.factory.from_timestamp(self.last_ts).id
        except Exception:  # noqa: BLE001 — timing math never breaks the engine
            return None

    def _round(self, rid: int) -> RoundState:
        rs = self.rounds.get(rid)
        if rs is not None:
            return rs
        # Future/orphan guard: a reveal/FDC tx carrying an implausibly-far-ahead voting_round_id
        # would create a RoundState that never finalizes (finalize needs its next round to end) —
        # an unbounded leak vector. Return a THROWAWAY (unstored) for such ids so the caller can
        # write to it harmlessly; it's discarded when the call returns.
        cur = self._current_round()
        if cur is not None and rid > cur + _FUTURE_ROUND_MARGIN:
            return RoundState(rid)
        rs = RoundState(rid)
        self.rounds[rid] = rs
        # Hard cap (defense-in-depth): normal in-flight is ~1–3 rounds; exceeding the cap means
        # orphans accreted — evict the stalest (lowest) rid, which would have finalized by now.
        if len(self.rounds) > _ROUNDS_CAP:
            del self.rounds[min(self.rounds)]
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

    def record_fast_update(self, ts: int) -> None:
        """One AP FastUpdateFeedsSubmitted event observed at block time `ts` (protocol 255)."""
        self.fu_events.append(ts)

    def windowed_fastupdates(self, now_ts: int) -> dict:
        """AP fast-update counts over 1h / 6h / 24h from the rolling event log."""
        ev = list(self.fu_events)
        return {
            "1h": sum(1 for t in ev if now_ts and t >= now_ts - 3_600),
            "6h": sum(1 for t in ev if now_ts and t >= now_ts - 21_600),
            "24h": sum(1 for t in ev if now_ts and t >= now_ts - 86_400),
            "total_tracked": len(ev),
        }

    def record_fdc_request(self, round_id: int) -> None:
        """One FdcHub AttestationRequest observed for `round_id` (the round of the block it
        was emitted in). Rounds accumulate a request count that gates FDC-participation checks."""
        self._round(round_id).fdc_request_count += 1

    def _finalize(self, rs: RoundState, ts: int = 0) -> None:
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
        if rs.iqr_results:  # one compact tally per scored round → the multi-horizon history
            ins = sum(1 for band, _p, _c in rs.iqr_results.values() if band == "I")
            bnd = sum(1 for band, _p, _c in rs.iqr_results.values() if band == "B")
            pct = sum(1 for _b, p, _c in rs.iqr_results.values() if p)
            cap = sum(1 for _b, _p, c in rs.iqr_results.values() if c)
            rs.iqr_tally = IqrTally(
                rid=rs.round_id, ts=ts, fr=len(rs.iqr_results), ins=ins, bnd=bnd, pct=pct, cap=cap
            )
            rs.iqr_tally_new = self._remember_tally(rs.iqr_tally)
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
                self._finalize(rs, now_ts)
                done.append(rs)
        return done

    def windowed_iqr(self, now_ts: int, reward_epoch: int | None) -> dict:
        """IQR inner/outer rates over 1h / 6h / 24h / since-epoch, from the retained tallies.
        `now_ts` is chain time (state.last_ts); since-epoch = tallies whose round is in
        `reward_epoch`. Each horizon: {inner_pct, outer_pct, feed_rounds, rounds, capped}."""
        hist = list(self.iqr_history)

        def _agg(rows: list) -> dict:
            fr = sum(r.fr for r in rows)
            if fr == 0:
                return {"inner_pct": None, "outer_pct": None, "feed_rounds": 0, "rounds": len(rows), "capped": 0}
            ins = sum(r.ins for r in rows)
            bnd = sum(r.bnd for r in rows)
            return {
                "inner_pct": round(100.0 * (ins + 0.5 * bnd) / fr, 1),
                "outer_pct": round(100.0 * sum(r.pct for r in rows) / fr, 1),
                "feed_rounds": fr,
                "rounds": len(rows),
                "capped": sum(r.cap for r in rows),
            }

        return {
            "1h": _agg([r for r in hist if now_ts and r.ts >= now_ts - 3_600]),
            "6h": _agg([r for r in hist if now_ts and r.ts >= now_ts - 21_600]),
            "24h": _agg([r for r in hist if now_ts and r.ts >= now_ts - 86_400]),
            "epoch": _agg(
                [r for r in hist if reward_epoch is not None and r.rid // VRS_PER_REWARD_EPOCH == reward_epoch]
            ),
        }

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
            "signatures_seen": sum(1 for r in w if r.sig_seen),
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
