"""IQR reward-band scoring for AP — inner (primary/IQR) + outer (secondary/PCT) hit rates.

Native + keyless: computes the consensus median/quartiles from ALL registered voters' reveals
(`py-flare-common calculate_median` IS the Foundation's reference algorithm), then scores AP's
own submitted values against the reward bands (`reward_rule.classify_bands`). OBSERVE-only.

Works even while AP is EXCLUDED (RE423): it scores AP's on-chain values against the registered
voters' consensus — i.e. AP's would-be reward quality — so it is not blocked like fast-updates.

Values from `parse_submit2_tx(...).ftso.payload.values` are ALREADY raw integer ticks — scored
directly (no `to_raw`; that is only for converting decimal-string sources).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from py_flare_common.fsp.messaging import parse_submit2_tx
from py_flare_common.ftso.median import FtsoVote, calculate_median

from clif.observe.reward_rule import BandClass, OfferParams, _find_block_for_ts, classify_bands
from clif.rpc import RpcClient, RpcError

_SUBMIT2_SELECTOR = "9d00c9fd"


def build_voter_weight_map(
    rpc: RpcClient, *, voter_registry: str, entity_manager: str, epoch: int
) -> dict[str, int]:
    """{submit_address_lc: registration_weight} for the epoch's registered voters — used to
    weight the consensus median. Best-effort per voter (a bad entry is skipped)."""
    m: dict[str, int] = {}
    for identity in rpc.get_registered_voters(voter_registry, epoch):
        try:
            submit = rpc.get_voter_addresses(entity_manager, identity)[0].lower()
            weight = rpc.voter_registration_weight(voter_registry, identity, epoch)
            if weight > 0:
                m[submit] = weight
        except RpcError:
            continue
    return m


@dataclass
class FeedScore:
    feed: str
    rounds: int = 0
    inside: int = 0
    boundary: int = 0
    outside: int = 0
    pct_hit: int = 0
    capped: int = 0  # rounds where Q3-Q1 <= 1 tick (structural ~50% inner ceiling)

    @property
    def expected_inner_pct(self) -> float | None:
        # closed-form expected primary rate: inside + 0.5*boundary (the coin flip averages 0.5)
        return None if self.rounds == 0 else round(100.0 * (self.inside + 0.5 * self.boundary) / self.rounds, 1)

    @property
    def outer_pct(self) -> float | None:
        return None if self.rounds == 0 else round(100.0 * self.pct_hit / self.rounds, 1)

    def to_dict(self) -> dict:
        return {
            "feed": self.feed, "rounds": self.rounds,
            "inner_pct": self.expected_inner_pct, "outer_pct": self.outer_pct,
            "inside": self.inside, "boundary": self.boundary, "outside": self.outside,
            "pct_hit": self.pct_hit, "capped": self.capped,
        }


def score_ap(
    rpc: RpcClient,
    *,
    network: str,
    submission: str,
    ap_submit: str,
    voter_registry: str,
    entity_manager: str,
    offer: OfferParams,
    weight_map: dict[str, int],
    factory,
    rounds: int = 10,
    up_to_round: int | None = None,
    log=None,
) -> tuple[dict[str, FeedScore], int]:
    """Score AP's inner/outer band hits over the last `rounds` fully-revealed rounds. ONE
    contiguous scan of the reveal windows; each submit2's payload voting_round_id keys the round.
    Returns ({feed: FeedScore}, scored_rounds)."""
    cur = up_to_round if up_to_round is not None else factory.now_id()
    newest = cur - 2  # a round is fully revealed once its next round has passed
    oldest = newest - rounds + 1
    ap_sub = ap_submit.lower()
    sub = submission.lower()

    scan_lo = factory.make_epoch(oldest).next.start_s
    scan_hi = factory.make_epoch(newest).next.reveal_deadline()
    round_votes: dict[int, dict[int, list]] = defaultdict(lambda: defaultdict(list))
    round_ap: dict[int, list] = {}

    b = _find_block_for_ts(rpc, scan_lo)
    while True:
        blk = rpc.get_block(b, full_transactions=True)
        if blk is None:
            break
        ts = int(str(blk.get("timestamp", "0x0")), 16)
        if ts > scan_hi:
            break
        for tx in blk.get("transactions", []):
            if (tx.get("to") or "").lower() != sub:
                continue
            inp = tx.get("input", "0x")
            if inp[2:10] != _SUBMIT2_SELECTOR:
                continue
            try:
                pm = parse_submit2_tx(inp[10:])
            except Exception:  # noqa: BLE001
                continue
            if pm.ftso is None:
                continue
            r = pm.ftso.voting_round_id
            if not (oldest <= r <= newest):
                continue
            vals = pm.ftso.payload.values
            frm = (tx.get("from") or "").lower()
            if frm == ap_sub:
                round_ap[r] = vals
            if frm in weight_map:
                for i, v in enumerate(vals):
                    if v is not None:
                        round_votes[r][i].append(FtsoVote(value=int(v), weight=weight_map[frm]))
        b += 1

    scores = {name: FeedScore(name) for name in offer.feeds}
    scored = 0
    for r in range(oldest, newest + 1):
        ap_vals = round_ap.get(r)
        if ap_vals is None:  # AP didn't reveal this round → not scored
            continue
        scored += 1
        for i, name in enumerate(offer.feeds):
            votes = round_votes[r].get(i)
            if not votes or i >= len(ap_vals) or ap_vals[i] is None:
                continue
            m = calculate_median(votes)
            cls = classify_bands(
                value_raw=int(ap_vals[i]),
                q1_raw=m.first_quartile, q3_raw=m.third_quartile, median_raw=m.value,
                secondary_band_width_ppm=offer.secondary_band_width_ppm.get(name, 0),
            )
            fs = scores[name]
            fs.rounds += 1
            if cls["band_ticks"] <= 1:
                fs.capped += 1
            if cls["band_class"] == BandClass.INSIDE:
                fs.inside += 1
            elif cls["band_class"] == BandClass.BOUNDARY:
                fs.boundary += 1
            else:
                fs.outside += 1
            if cls["pct_hit"]:
                fs.pct_hit += 1
        if log:
            log.info("iqr round %s scored", r)
    return scores, scored


def overall(scores: dict[str, FeedScore]) -> dict:
    """Weighted-by-round overall inner/outer across feeds that had scored rounds."""
    tot = sum(s.rounds for s in scores.values())
    if tot == 0:
        return {"inner_pct": None, "outer_pct": None, "feed_rounds": 0}
    inner = sum(s.inside + 0.5 * s.boundary for s in scores.values())
    outer = sum(s.pct_hit for s in scores.values())
    return {
        "inner_pct": round(100.0 * inner / tot, 1),
        "outer_pct": round(100.0 * outer / tot, 1),
        "feed_rounds": tot,
    }
