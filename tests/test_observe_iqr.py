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
