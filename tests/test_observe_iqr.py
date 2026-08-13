"""IQR reward-rule: band classification (inner/outer) + tick conversion + coin-flip determinism."""

from __future__ import annotations

from clif.observe.reward_rule import (
    BandClass,
    classify_bands,
    random_select,
    reward_epoch_id_for_vr,
    to_raw,
)


def _cls(v, q1, q3, m, ppm=500):
    return classify_bands(value_raw=v, q1_raw=q1, q3_raw=q3, median_raw=m, secondary_band_width_ppm=ppm)


# ---- inner (primary/IQR) band ---------------------------------------------------


def test_inside_is_inside():
    c = _cls(56153, 56150, 56164, 56161)
    assert c["band_class"] == BandClass.INSIDE and c["band_ticks"] == 14


def test_exactly_on_q1_or_q3_is_boundary():
    assert _cls(7898, 7898, 7899, 7898)["band_class"] == BandClass.BOUNDARY  # == Q1
    assert _cls(7899, 7898, 7899, 7898)["band_class"] == BandClass.BOUNDARY  # == Q3


def test_outside_the_iqr():
    assert _cls(100, 56150, 56164, 56161)["band_class"] == BandClass.OUTSIDE
    assert _cls(99999, 56150, 56164, 56161)["band_class"] == BandClass.OUTSIDE


# ---- outer (secondary/PCT) band -------------------------------------------------


def test_pct_hit_within_band():
    # band = |M|*ppm//1e6 = 1000000*500//1e6 = 500; [999500, 1000500) exclusive
    assert _cls(1000000, 0, 0, 1000000, ppm=500)["pct_hit"] is True
    assert _cls(1000400, 0, 0, 1000000, ppm=500)["pct_hit"] is True
    assert _cls(1000500, 0, 0, 1000000, ppm=500)["pct_hit"] is False  # exclusive upper
    assert _cls(999499, 0, 0, 1000000, ppm=500)["pct_hit"] is False


# ---- tick conversion (exact, no float epsilon) ---------------------------------


def test_to_raw_exact_and_half_up():
    assert to_raw("0.56161", 5) == 56161
    assert to_raw("78.985", 2) == 7899  # ROUND_HALF_UP
    assert to_raw("1.0", 8) == 100_000_000


# ---- coin-flip determinism (same inputs → same bit) ----------------------------


def test_random_select_is_deterministic():
    a = random_select("BTC/USD", 1424356, "0xa8BBBA017b2ce496bBED4CFC0FB5D1aFd4F23772")
    b = random_select("BTC/USD", 1424356, "0xa8BBBA017b2ce496bBED4CFC0FB5D1aFd4F23772")
    assert a == b and isinstance(a, bool)


def test_reward_epoch_id_for_vr():
    assert reward_epoch_id_for_vr(1424356) == 423  # 1424356 // 3360
    assert reward_epoch_id_for_vr(417 * 3360) == 417


# ---- score aggregation (FeedScore + overall) -----------------------------------


def test_feed_score_expected_inner_and_outer():
    from clif.observe.iqr import FeedScore

    fs = FeedScore("XRP/USD", rounds=10, inside=4, boundary=2, outside=4, pct_hit=9)
    # expected primary = inside + 0.5*boundary = 4 + 1 = 5 → 50%
    assert fs.expected_inner_pct == 50.0
    assert fs.outer_pct == 90.0


def test_overall_weights_by_feed_rounds():
    from clif.observe.iqr import FeedScore, overall

    a = FeedScore("A", rounds=10, inside=8, boundary=0, pct_hit=10)  # inner 80, outer 100
    b = FeedScore("B", rounds=10, inside=2, boundary=0, pct_hit=6)  # inner 20, outer 60
    ov = overall({"A": a, "B": b})
    assert ov["inner_pct"] == 50.0 and ov["outer_pct"] == 80.0 and ov["feed_rounds"] == 20


def test_overall_empty_is_none():
    from clif.observe.iqr import overall

    ov = overall({})
    assert ov["inner_pct"] is None and ov["feed_rounds"] == 0


# ---- streaming integration (ObserverState IQR path) -----------------------------


def _offer(feeds, ppm=500):
    from clif.observe.reward_rule import OfferParams

    return OfferParams(
        reward_epoch_id=1, network="songbird", block=0, primary_band_reward_share_ppm=400000,
        min_rewarded_turnout_bips=0, feeds=feeds, decimals={f: 2 for f in feeds},
        secondary_band_width_ppm={f: ppm for f in feeds},
    )


def test_state_scores_ap_at_finalize_and_clears_votes():
    from clif.observe.state import ObserverState

    ap = "0x" + "a" * 40
    sig = "0x" + "b" * 40
    v = [("0x" + c * 40) for c in ("1", "2", "3", "4", "5")]  # 5 registered voters
    st = ObserverState("songbird", ap, sig)
    st.set_iqr_context(_offer(["BTC/USD", "XRP/USD"]), {a.lower(): 1 for a in v})

    R = 1_000_000
    for a in v:  # unanimous consensus ⇒ median == Q1 == Q3 (deterministic)
        st.record_reveal_values(R, a.lower(), [102000, 50000])
    st.record_reveal_values(R, ap.lower(), [102000, 99999])  # BTC == median; XRP far outside

    rs = st.rounds[R]
    assert rs.iqr_votes[0] and rs.iqr_ap_values == [102000, 99999]
    st._finalize(rs)

    # BTC: value == Q1==Q3 ⇒ BOUNDARY, capped (Q3−Q1==0), and inside the tiny PCT band.
    assert rs.iqr_results["BTC/USD"] == ("B", True, True)
    # XRP: 99999 is outside the IQR and outside the PCT band around 50000 (unanimous ⇒ capped).
    assert rs.iqr_results["XRP/USD"] == ("O", False, True)
    assert rs.iqr_votes == {}  # per-round vote buffer freed

    agg = st.aggregates()
    assert agg["iqr_feed_rounds"] == 2 and agg["iqr_boundary"] == 1
    assert agg["iqr_pct_hit"] == 1 and agg["iqr_capped"] == 2 and agg["iqr_scored_rounds"] == 1


def test_iqr_off_when_no_context():
    from clif.observe.state import ObserverState

    st = ObserverState("songbird", "0x" + "a" * 40, "0x" + "b" * 40)
    st.record_reveal_values(5, ("0x" + "a" * 40).lower(), [1, 2, 3])  # no context ⇒ AP vals kept, no votes
    rs = st.rounds[5]
    st._finalize(rs)
    assert rs.iqr_results == {} and st.aggregates()["iqr_feed_rounds"] == 0
