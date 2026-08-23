"""Per-epoch minimal-conditions ledger — exact FDC + fast-updates, gap-free across restarts."""

from __future__ import annotations

from dataclasses import dataclass, field

from clif.observe.mincond import (
    RoundRecord, append_record, epoch_of, epoch_tally, from_round, load_history, prune_history,
)
from clif.observe.reward_rule import VRS_PER_REWARD_EPOCH as VRS


@dataclass
class _FakeRound:
    round_id: int
    fdc_request_count: int = 0
    fdc_bitvote_seen: bool = False
    fdc_gap: bool = False
    fu_count: int = 0

    @property
    def fdc_expected(self) -> bool:
        return self.fdc_request_count > 0


def test_from_round_maps_participation():
    assert from_round(_FakeRound(1, 0, False, False, 3)) == RoundRecord(1, 0, 0, 3)          # no FDC req
    assert from_round(_FakeRound(2, 2, True, False, 1)) == RoundRecord(2, 1, 1, 1)           # FDC ok
    assert from_round(_FakeRound(3, 2, False, True, 0)) == RoundRecord(3, 1, 0, 0)           # FDC gap
    assert from_round(_FakeRound(4, 2, True, True, 0)) == RoundRecord(4, 1, 0, 0)            # gap wins


def test_epoch_tally_exact():
    e = 426
    recs = {}
    for rid in range(e * VRS, e * VRS + 100):
        recs[rid] = RoundRecord(rid=rid, fexp=1 if rid % 2 == 0 else 0, fok=1 if rid % 2 == 0 else 0, fu=1)
    recs[e * VRS + 2] = RoundRecord(rid=e * VRS + 2, fexp=1, fok=0, fu=1)  # one FDC miss
    t = epoch_tally(recs, epoch=e)
    assert t["fdc_expected"] == 50 and t["fdc_participated"] == 49
    assert t["fdc_pct"] == round(100 * 49 / 50, 1) and t["fu_updates"] == 100
    # a different epoch is excluded
    assert epoch_tally(recs, epoch=e + 1)["fdc_expected"] == 0


def test_load_dedups_and_scopes_to_current_and_prior_epoch(tmp_path):
    p = tmp_path / "mc.jsonl"
    append_record(p, RoundRecord(424 * VRS + 5, 1, 1, 1))   # two epochs back → dropped
    append_record(p, RoundRecord(425 * VRS + 5, 1, 0, 2))   # prior epoch → kept
    append_record(p, RoundRecord(426 * VRS + 5, 1, 1, 3))   # current → kept
    append_record(p, RoundRecord(426 * VRS + 5, 1, 1, 9))   # re-finalized dup → last wins
    recs = load_history(p, reward_epoch=426)
    assert set(epoch_of(r) for r in recs) == {425, 426}
    assert recs[426 * VRS + 5].fu == 9  # dedup kept the latest


def test_prune_rewrites_current_and_prior_only(tmp_path):
    p = tmp_path / "mc.jsonl"
    for rid in (423 * VRS, 425 * VRS, 426 * VRS):
        append_record(p, RoundRecord(rid, 1, 1, 1))
    prune_history(p, reward_epoch=426)
    assert set(epoch_of(r) for r in load_history(p, reward_epoch=426)) == {425, 426}
