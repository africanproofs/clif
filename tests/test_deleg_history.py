"""Delegation 24h / reward-epoch deltas + the render line."""

from __future__ import annotations

import re

from clif.observe.deleg_history import DelegSnap, compute_deltas, load_snaps, prune_snaps
from clif.observe.health import ObserveHealth, render_protocol_report


def _plain(lines):
    return "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in lines)


def test_deltas_24h_and_epoch():
    now = 1_000_000
    hist = [
        DelegSnap(ts=now - 90_000, epoch=424, val_delegated=100.0, val_dels=50, ftso_vp=200.0),  # >24h
        DelegSnap(ts=now - 3_600, epoch=424, val_delegated=110.0, val_dels=52, ftso_vp=210.0),   # 1h
    ]
    cur = DelegSnap(ts=now, epoch=424, val_delegated=115.0, val_dels=53, ftso_vp=205.0)
    d = compute_deltas(hist, now=now, epoch=424, current=cur)
    assert d["val_delegated"]["h24"] == 15.0 and d["val_dels"]["h24"] == 3 and d["ftso_vp"]["h24"] == 5.0
    assert d["val_delegated"]["epoch"] == 15.0  # earliest this-epoch snap = hist[0]


def test_epoch_base_excludes_prior_epoch():
    now = 1_000_000
    hist = [
        DelegSnap(ts=now - 200_000, epoch=423, val_delegated=90.0, val_dels=48, ftso_vp=190.0),   # prior epoch
        DelegSnap(ts=now - 100_000, epoch=424, val_delegated=100.0, val_dels=50, ftso_vp=200.0),  # this epoch
    ]
    cur = DelegSnap(ts=now, epoch=424, val_delegated=112.0, val_dels=51, ftso_vp=208.0)
    d = compute_deltas(hist, now=now, epoch=424, current=cur)
    assert d["val_delegated"]["epoch"] == 12.0   # vs earliest THIS epoch (100), not the 423 snap
    assert d["val_delegated"]["h24"] == 12.0


def test_no_baseline_is_none():
    d = compute_deltas([], now=1_000_000, epoch=424, current=DelegSnap(1_000_000, 424, 100.0, 50, 200.0))
    assert d["val_delegated"]["h24"] is None and d["ftso_vp"]["epoch"] is None


def test_snap_persistence_prunes_old(tmp_path):
    from clif.observe.deleg_history import append_snap

    p = tmp_path / "d.jsonl"
    now = 2_000_000
    append_snap(p, DelegSnap(ts=now - 100, epoch=424, val_delegated=1.0, val_dels=1, ftso_vp=1.0))
    append_snap(p, DelegSnap(ts=now - 9 * 86_400, epoch=420, val_delegated=1.0, val_dels=1, ftso_vp=1.0))  # >8d
    assert len(load_snaps(p, now=now)) == 1
    prune_snaps(p, now=now)
    assert len(p.read_text().splitlines()) == 1


def test_render_delta_lines():
    deleg = {
        "validator": {"total": 140_206_326, "self_bond": 10_000_000, "delegated": 130_206_326,
                      "delegators": 60, "fee_pct": 20.0, "uptime": 100.0},
        "ftso": {"vote_power": 132_646_887.0},
        "deltas": {
            "val_delegated": {"h24": 500_000.0, "epoch": 2_100_000.0},
            "val_dels": {"h24": 2, "epoch": 5},
            "ftso_vp": {"h24": -300_000.0, "epoch": 1_800_000.0},
        },
    }
    out = _plain(render_protocol_report(ObserveHealth(
        network="flare", enabled=True, window_rounds=40, complete=40, signatures_seen=40,
        registered=True, last_block=100, head=101, delegation=deleg,
    )))
    assert "Δ 24h      : validator +500.0K (+2 dels) · FTSO -300.0K" in out
    assert "Δ epoch    : validator +2.1M (+5 dels) · FTSO +1.8M" in out
