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


def test_run_engine_startup_writes_status_without_crashing(tmp_path):
    """Regression: `_status()` reads `gap_list`, which must be bound BEFORE the startup status
    write (a v0.5.69 NameError crash-looped the observer). Exercises run_engine's startup path."""
    from clif.observe.engine import run_engine

    class _FakeRpc:
        def block_number(self):
            return 2000

        def get_block(self, num, full_transactions=False):
            return {"timestamp": hex(1_700_000_000 + int(num)), "transactions": []}

    statuses: list = []
    run_engine(
        rpc=_FakeRpc(), network="flare", submission_address="0x" + "1" * 40,
        our_submit="0x" + "a" * 40, our_sig="0x" + "b" * 40,
        status_writer=statuses.append, lookback_blocks=5, poll_sec=0.01,
        gaps_file=str(tmp_path / "g.jsonl"),
        mincond_history_file=str(tmp_path / "mc.jsonl"),  # exercise the ledger seed + _status path
        live_lag_blocks=8, iqr_enabled=False, _max_blocks=3, log=None,
    )
    assert statuses and "gaps" in statuses[-1] and "head" in statuses[-1]
    assert "mincond" in statuses[-1]  # the per-epoch tracker field is wired into the status


def test_skipped_gap_not_open_and_rendered_as_skipped():
    from clif.observe.gaps import Gap

    g = Gap(start=0, end=100, dur=100, fails=50, from_block=1000, to_block=99_000, skipped=True)
    assert g.backfilled(99_000) is False  # a skipped gap is never 'backfilled'
    gap = {"start": 1_000_000, "end": 1_020_000, "dur": 20_000, "fails": 9000,
           "from_block": 1000, "to_block": 99_000, "skipped": True}
    h = _h(last_block=1500, head=1502, gaps=[gap])  # last_block < to_block, but skipped
    assert not h.open_gaps  # skipped ⇒ resolved-by-skip, not open
    assert h.severity != "CRIT"  # a skipped (resolved) gap doesn't hold the stream in CATCHING UP
    out = _plain(render_protocol_report(h))
    assert "⏭ skipped" in out


def test_report_outage_marked_backfilled_once_past():
    gap = {"start": 1_000_000, "end": 1_001_000, "dur": 1000, "fails": 500,
           "from_block": 100, "to_block": 200}
    h = _h(last_block=67_000_000, head=67_000_002, gaps=[gap])  # long past → backfilled
    out = _plain(render_protocol_report(h))
    assert "✓ LIVE" in out and not h.open_gaps
    assert "outages (7d) : 1 —" in out and "✓" in out


# ---- adaptive report cadence: tighten to 5 min while degraded --------------------


def test_report_interval_tightens_on_degradation():
    from clif.observe.engine import report_interval

    # Healthy → the relaxed hourly cadence; any non-OK severity → the tight (5 min) cadence,
    # so a degradation is re-reported every few minutes until it clears back to OK.
    assert report_interval("OK", healthy_sec=3600, degraded_sec=300) == 3600
    assert report_interval("WARN", healthy_sec=3600, degraded_sec=300) == 300
    assert report_interval("CRIT", healthy_sec=3600, degraded_sec=300) == 300


def test_report_interval_relaxes_when_recovering():
    from clif.observe.engine import report_interval

    # A recovering WARN (stale isolated miss, recent rounds clean) relaxes back to hourly;
    # a fresh/active WARN or CRIT keeps the tight cadence.
    assert report_interval("WARN", healthy_sec=3600, degraded_sec=300, recovering=True) == 3600
    assert report_interval("WARN", healthy_sec=3600, degraded_sec=300, recovering=False) == 300
    # CRIT stays tight (the `recovering` property is only ever True for a WARN, never a CRIT).
    assert report_interval("CRIT", healthy_sec=3600, degraded_sec=300, recovering=False) == 300


def test_resume_cursor_gap_free_across_restarts():
    from clif.observe.engine import resume_cursor

    h, lb = 1_000_000, 900
    assert resume_cursor(h, lookback_blocks=lb, prior_last_block=None) == h - lb          # fresh start
    assert resume_cursor(h, lookback_blocks=lb, prior_last_block=h - 50) == h - lb - 50    # quick restart: re-covers
    assert resume_cursor(h, lookback_blocks=lb, prior_last_block=h - 50_000) == h - 50_000 - lb  # long: fills the gap
    # capped so a pathological downtime doesn't backfill unbounded history
    assert resume_cursor(h, lookback_blocks=lb, prior_last_block=h - 500_000, max_blocks=200_000) == h - 200_000
    assert resume_cursor(h, lookback_blocks=lb, prior_last_block=h - 500_000, max_blocks=0) == h - 500_000 - lb
    assert resume_cursor(h, lookback_blocks=lb, prior_last_block=0) == h - lb              # bad/zero prior ⇒ fresh


# ---- reward-epoch rollover expires the epoch-scoped overlays ---------------------


def _overlays(epoch=427, registered=True, checked=9_000.0):
    """The five epoch-scoped overlay caches as run_engine holds them."""
    reg = {"registered": registered, "epoch": epoch, "checked": checked}
    bud = {"data": {"epoch": epoch}, "checked": checked}
    deleg = {"data": {}, "checked": checked}
    up = {"pct": 99.99, "connected": True, "checked": checked}
    iqr = {"epoch": epoch, "checked": checked, "ready": True}
    return reg, bud, deleg, up, iqr


def test_epoch_rollover_expires_every_epoch_scoped_overlay():
    """RE427→428: without this the report stayed keyed to the CLOSED epoch for up to an hour
    (stale header + min-cond + reveal-offence penalty). Every overlay must re-probe at once."""
    from clif.observe.engine import expire_epoch_overlays

    reg, bud, deleg, up, iqr = _overlays(epoch=427)
    expire_epoch_overlays(428, reg, bud, deleg, up, iqr)

    assert reg["epoch"] == 428  # header/tally flip immediately, without waiting for the RPC probe
    assert reg["registered"] is None  # never carry the old epoch's YES across the boundary
    now, refresh = 10_000.0, 3600.0
    # the exact guards inside _refresh_* must now fall through to a real probe
    assert not (reg["epoch"] is not None and now - reg["checked"] < refresh)
    for st in (bud, deleg, up, iqr):
        assert not (st["checked"] and now - st["checked"] < refresh)


def test_epoch_rollover_never_rewinds_on_a_backfilled_old_round():
    """A backfill replaying a CLOSED epoch must not rewind the header or blank a good
    registration answer — the tracked epoch only ever moves forward."""
    from clif.observe.engine import expire_epoch_overlays

    reg, bud, deleg, up, iqr = _overlays(epoch=428)
    expire_epoch_overlays(426, reg, bud, deleg, up, iqr)  # replaying an old epoch

    assert reg["epoch"] == 428 and reg["registered"] is True  # untouched
