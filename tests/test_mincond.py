"""Per-epoch minimal-conditions ledger — exact FDC + fast-updates, gap-free across restarts."""

from __future__ import annotations

from dataclasses import dataclass, field

from clif.observe.mincond import (
    RoundRecord, append_record, epoch_gap_ranges, epoch_of, epoch_tally, from_round,
    load_history, prune_history,
)
from clif.observe.reward_rule import VRS_PER_REWARD_EPOCH as VRS


@dataclass
class _FakeRound:
    round_id: int
    submit1_seen: bool = True
    submit2_seen: bool = True
    _clean: bool = True
    fdc_request_count: int = 0
    fdc_bitvote_seen: bool = False
    fdc_gap: bool = False
    fu_count: int = 0

    @property
    def fdc_expected(self) -> bool:
        return self.fdc_request_count > 0

    @property
    def clean(self) -> bool:
        return self._clean


def test_from_round_maps_participation():
    assert from_round(_FakeRound(1, fu_count=3)) == RoundRecord(1, s1=1, s2=1, cl=1, fu=3)  # no FDC req
    assert from_round(_FakeRound(2, fdc_request_count=2, fdc_bitvote_seen=True)) == RoundRecord(2, s1=1, s2=1, cl=1, fexp=1, fok=1)
    assert from_round(_FakeRound(3, fdc_request_count=2, fdc_gap=True)) == RoundRecord(3, s1=1, s2=1, cl=1, fexp=1, fok=0)  # FDC gap
    miss = from_round(_FakeRound(4, submit2_seen=False, _clean=False))
    assert miss.s2 == 0 and miss.cl == 0  # a missed reveal


def test_epoch_tally_exact():
    e = 426
    recs = {}
    for rid in range(e * VRS, e * VRS + 100):
        recs[rid] = RoundRecord(rid=rid, s2=1, cl=1, fexp=1 if rid % 2 == 0 else 0, fok=1 if rid % 2 == 0 else 0, fu=1)
    recs[e * VRS + 2] = RoundRecord(rid=e * VRS + 2, s2=1, cl=1, fexp=1, fok=0, fu=1)  # one FDC miss
    t = epoch_tally(recs, epoch=e)
    assert t["fdc_expected"] == 50 and t["fdc_participated"] == 49
    assert t["fdc_pct"] == round(100 * 49 / 50, 1) and t["fu_updates"] == 100
    assert t["ftso_revealed"] == 100 and t["ftso_pct"] == 100.0  # all 100 rounds revealed
    assert t["rounds_recorded"] == 100 and t["rounds_expected"] == VRS
    # a different epoch is excluded
    assert epoch_tally(recs, epoch=e + 1)["fdc_expected"] == 0


def test_load_dedups_and_scopes_to_current_and_prior_epoch(tmp_path):
    p = tmp_path / "mc.jsonl"
    append_record(p, RoundRecord(424 * VRS + 5, fexp=1, fok=1, fu=1))   # two epochs back → dropped
    append_record(p, RoundRecord(425 * VRS + 5, fexp=1, fok=0, fu=2))   # prior epoch → kept
    append_record(p, RoundRecord(426 * VRS + 5, fexp=1, fok=1, fu=3))   # current → kept
    append_record(p, RoundRecord(426 * VRS + 5, fexp=1, fok=1, fu=9))   # re-finalized dup → last wins
    recs = load_history(p, reward_epoch=426)
    assert set(epoch_of(r) for r in recs) == {425, 426}
    assert recs[426 * VRS + 5].fu == 9  # dedup kept the latest


def test_prune_rewrites_current_and_prior_only(tmp_path):
    p = tmp_path / "mc.jsonl"
    for rid in (423 * VRS, 425 * VRS, 426 * VRS):
        append_record(p, RoundRecord(rid, fexp=1, fok=1, fu=1))
    prune_history(p, reward_epoch=426)
    assert set(epoch_of(r) for r in load_history(p, reward_epoch=426)) == {425, 426}


# ---- epoch ceremony renderers (start banner + close-out report card) ------------


def _plain(lines):
    import re
    return "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in lines)


def test_closeout_all_pass_is_eligible():
    from clif.observe.health import render_epoch_closeout

    t = epoch_tally({rid: RoundRecord(rid, s2=1, cl=1, fexp=1, fok=1, fu=2) for rid in range(426 * VRS, 426 * VRS + 3360)}, epoch=426)
    out = _plain(render_epoch_closeout(t, uptime_pct=99.99, network="flare"))
    assert "REWARD EPOCH 426 CLOSED" in out
    assert "FTSO    100.0% (≥80)  ✓ PASS" in out and "FDC     100.0% (≥60)  ✓ PASS" in out
    assert "ALL MINIMAL CONDITIONS MET — epoch 426 reward-eligible" in out


def test_closeout_fdc_breach_is_not_eligible():
    from clif.observe.health import render_epoch_closeout

    recs = {rid: RoundRecord(rid, s2=1, cl=1, fexp=1, fok=1 if rid % 2 else 0, fu=1) for rid in range(426 * VRS, 426 * VRS + 3360)}
    out = _plain(render_epoch_closeout(epoch_tally(recs, epoch=426), uptime_pct=99.99, network="flare"))
    assert "✗ BREACH" in out and "🔴 BREACHED: FDC — epoch 426 NOT reward-eligible" in out


def test_closeout_flags_partial_coverage():
    from clif.observe.health import render_epoch_closeout

    recs = {rid: RoundRecord(rid, s2=1, cl=1, fexp=1, fok=1, fu=1) for rid in range(426 * VRS, 426 * VRS + 500)}
    out = _plain(render_epoch_closeout(epoch_tally(recs, epoch=426), uptime_pct=99.99, network="flare"))
    assert "coverage" in out and "tracker started mid-epoch" in out


def test_open_ceremony_names_epoch_and_round_range():
    from clif.observe.health import render_epoch_open

    out = _plain(render_epoch_open(427, network="flare"))
    assert "REWARD EPOCH 427 OPEN" in out and "trackers armed" in out
    assert f"{427 * VRS}–{428 * VRS - 1}" in out


def test_closeout_logs_throughput():
    from clif.observe.health import render_epoch_closeout

    t = epoch_tally({rid: RoundRecord(rid, s2=1, cl=1, fexp=0, fok=0, fu=1) for rid in range(426 * VRS, 426 * VRS + 3360)}, epoch=426)
    out = _plain(render_epoch_closeout(t, uptime_pct=99.99, network="flare", blocks_scanned=302761))
    assert "processed 3360 voting rounds · 302761 blocks scanned" in out
    # blocks omitted when not provided
    assert "blocks scanned" not in _plain(render_epoch_closeout(t, uptime_pct=99.99, network="flare"))


def test_round_report_only_shows_full_picture_with_the_problem():
    from clif.observe.health import render_round_report

    line = render_round_report(
        rid=1434295, network="flare", s1=True, s2=False, sig=True,
        fdc_expected=True, fdc_ok=False, fu=0, offence=False, issues=["no submit2 (reveal)"],
    )
    import re
    plain = re.sub(r"\x1b\[[0-9;]*m", "", line)
    assert "ROUND 1434295" in plain and "reveal" in plain and "FDC ✗ gap" in plain
    assert "no submit2 (reveal)" in plain
    # a reveal offence gets the loud marker
    off = render_round_report(rid=1, network="flare", s1=True, s2=True, sig=True,
                              fdc_expected=False, fdc_ok=False, fu=0, offence=True, issues=["mismatch"])
    assert "‼ REVEAL OFFENCE" in re.sub(r"\x1b\[[0-9;]*m", "", off)


def test_epoch_gap_ranges_and_missing_in_span():
    e = 426
    recs = {rid: RoundRecord(rid, s2=1, cl=1, fexp=1, fok=1, fu=1)
            for rid in list(range(e * VRS, e * VRS + 100)) + list(range(e * VRS + 105, e * VRS + 200))}
    t = epoch_tally(recs, epoch=e)
    assert t["span"] == 200 and t["rounds_recorded"] == 195 and t["missing_in_span"] == 5
    ranges, total = epoch_gap_ranges(recs, epoch=e)
    assert total == 1 and ranges == [(e * VRS + 100, e * VRS + 104)]
    # contiguous ⇒ no gaps
    contig = {rid: RoundRecord(rid, s2=1) for rid in range(e * VRS, e * VRS + 50)}
    assert epoch_tally(contig, epoch=e)["missing_in_span"] == 0
    assert epoch_gap_ranges(contig, epoch=e) == ([], 0)


def test_closeout_provisional_when_rounds_unaccounted():
    from clif.observe.health import render_epoch_closeout

    recs = {rid: RoundRecord(rid, s2=1, cl=1, fexp=1, fok=1, fu=1)
            for rid in list(range(426 * VRS, 426 * VRS + 100)) + list(range(426 * VRS + 105, 426 * VRS + 200))}
    t = epoch_tally(recs, epoch=426)
    gr, gt = epoch_gap_ranges(recs, epoch=426)
    out = _plain(render_epoch_closeout(t, uptime_pct=99.99, network="flare", blocks_scanned=40000, gap_ranges=gr, gap_total=gt))
    assert "5 rounds MISSING within the observed span" in out
    assert "PROVISIONAL" in out and "cannot be fully certified" in out
    assert "span" in out  # the observed span is shown


def test_missing_rounds_while_live_is_warn():
    from clif.observe.health import ObserveHealth

    mc = {"fdc_pct": 100.0, "fdc_expected": 195, "fdc_participated": 195, "missing_in_span": 5, "fu_updates": 195}
    h = ObserveHealth(network="flare", enabled=True, window_rounds=40, complete=40, registered=True,
                      last_block=100, head=101, reward_epoch=426, mincond=mc)  # lag 1 ⇒ live
    assert h.severity == "WARN" and h.recovering is False
    # a hole while CATCHING UP (backfill in progress) is not yet flagged
    catching = ObserveHealth(network="flare", enabled=True, window_rounds=40, complete=40, registered=True,
                             last_block=100, head=500, reward_epoch=426, mincond=mc)  # lag 400 ⇒ catching up
    assert catching.stream_state == "catching_up"


def test_resume_reprocessing_never_duplicates(tmp_path):
    # Simulate a restart resume that re-appends rounds already in the ledger: load must dedup to
    # unique rids (last-wins), and the tally must never double-count.
    p = tmp_path / "mc.jsonl"
    e = 426
    for rid in range(e * VRS, e * VRS + 50):  # original run
        append_record(p, RoundRecord(rid, s2=1, cl=1, fexp=1, fok=1, fu=1))
    # resume re-processes the last 10 rounds (overlap) + 10 new ones — the engine guard would skip
    # the overlap, but even if the file gets the lines, load + tally must stay exact.
    for rid in range(e * VRS + 40, e * VRS + 70):
        append_record(p, RoundRecord(rid, s2=1, cl=1, fexp=1, fok=1, fu=1))
    recs = load_history(p, reward_epoch=e)
    rids = [r for r in recs]
    assert len(rids) == len(set(rids)) == 70  # 0..69 unique, no duplicates
    t = epoch_tally(recs, epoch=e)
    assert t["rounds_recorded"] == 70 and t["fdc_expected"] == 70 and t["fu_updates"] == 70
    assert t["missing_in_span"] == 0  # contiguous 0..69
