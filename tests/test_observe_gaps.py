"""Outage/backfill ledger + the LIVE-vs-CATCHING-UP stream signal."""

from __future__ import annotations

import re

from clif.observe.gaps import Gap, append_gap, hms, load_gaps, prune_gaps
from clif.observe.health import ObserveHealth, render_protocol_report


def _plain(lines):
    return "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in lines)


# ---- ledger persistence ----------------------------------------------------------


def test_hms():
    assert hms(3 * 3600 + 16 * 60) == "3h16m"
    assert hms(45 * 60 + 3) == "45m03s"
    assert hms(8) == "8s"


def test_gap_backfilled_by_last_block():
    g = Gap(start=0, end=100, dur=100, fails=50, from_block=1000, to_block=1600)
    assert g.backfilled(1599) is False
    assert g.backfilled(1600) is True


def test_load_append_prune_roundtrip(tmp_path):
    p = tmp_path / "gaps.jsonl"
    now = 2_000_000
    append_gap(p, Gap(start=now - 100, end=now - 50, dur=50, fails=25, from_block=10, to_block=40))
    append_gap(p, Gap(start=now - 10_000_000, end=now - 9_999_000, dur=1000, fails=500, from_block=1, to_block=9))  # >7d
    got = load_gaps(p, now=now)
    assert len(got) == 1 and got[0].from_block == 10  # old one filtered out
    prune_gaps(p, now=now)
    assert len(p.read_text().splitlines()) == 1


# ---- stream state + render -------------------------------------------------------


def _h(**kw):
    base = dict(network="flare", enabled=True, window_rounds=40, complete=40, reward_epoch=424,
                signatures_seen=40, registered=True, last_block=1000, head=1002, live_lag_blocks=8)
    base.update(kw)
    return ObserveHealth(**base)


def test_stream_live_vs_catching_up():
    assert _h(last_block=1000, head=1002).stream_state == "live"       # lag 2 ≤ 8
    assert _h(last_block=1000, head=9000).stream_state == "catching_up"  # lag 8000 > 8
    assert _h(head=None).stream_state == "unknown"


def test_report_shows_live_stream():
    out = _plain(render_protocol_report(_h(last_block=1000, head=1003)))
    assert "stream       : ✓ LIVE (at head, lag 3 blk)" in out


def test_report_shows_catching_up_and_outage():
    gap = {"start": 1_000_000, "end": 1_011_733, "dur": 11733, "fails": 5858,
           "from_block": 67_495_000, "to_block": 67_511_000}
    h = _h(last_block=67_502_000, head=67_511_690, lag_sec=17000, gaps=[gap])
    out = _plain(render_protocol_report(h))
    assert "CATCHING UP — 9690 blk" in out and "REPLAYED, not live" in out
    assert "replaying 1 outage" in out
    assert "outages (7d) : 1 —" in out and "(3h15m)" in out
    assert h.open_gaps  # to_block 67,511,000 > last_block 67,502,000 → still open


def test_report_outage_marked_backfilled_once_past():
    gap = {"start": 1_000_000, "end": 1_001_000, "dur": 1000, "fails": 500,
           "from_block": 100, "to_block": 200}
    h = _h(last_block=67_000_000, head=67_000_002, gaps=[gap])  # long past → backfilled
    out = _plain(render_protocol_report(h))
    assert "✓ LIVE" in out and not h.open_gaps
    assert "outages (7d) : 1 —" in out and "✓" in out
