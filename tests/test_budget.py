"""Per-epoch minimal-conditions miss-budget math + the delegation/budget report lines."""

from __future__ import annotations

import re

from clif.observe.budget import FTSO_MIN, budget_status
from clif.observe.health import ObserveHealth, render_protocol_report


def _plain(lines):
    return "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in lines)


# ---- miss-budget math ------------------------------------------------------------


def test_healthy_full_budget():
    b = budget_status(1.0, elapsed=2348, total=3360, threshold=FTSO_MIN)
    assert b["severity"] == "OK"
    assert b["miss_budget"] == 672 and b["missed"] == 0 and b["budget_left"] == 672
    assert b["projected_final_pct"] == 100.0


def test_on_track_to_breach_is_crit():
    # 75% so far → miss rate 25% → projected 75% < 80% floor ⇒ CRIT even though budget not yet 0
    b = budget_status(0.75, elapsed=1000, total=3360, threshold=FTSO_MIN)
    assert b["severity"] == "CRIT" and b["projected_final_pct"] == 75.0
    assert b["eta_rounds_to_breach"] is not None


def test_warn_when_budget_low_but_projection_ok():
    # 85% → miss rate 15% → projected 85% ≥ 80% (not CRIT), but only 222 of 672 budget left ⇒ WARN
    b = budget_status(0.85, elapsed=3000, total=3360, threshold=FTSO_MIN)
    assert b["severity"] == "WARN" and b["projected_final_pct"] == 85.0
    assert b["budget_left"] == 672 - 450


def test_already_breached_is_crit():
    b = budget_status(0.70, elapsed=3360, total=3360, threshold=FTSO_MIN)
    assert b["severity"] == "CRIT" and b["budget_left"] < 0


def test_unknown_before_data():
    assert budget_status(None, 0, 3360, FTSO_MIN)["severity"] == "unknown"


# ---- report lines ----------------------------------------------------------------


def _h(**kw):
    base = dict(network="flare", enabled=True, window_rounds=40, complete=40, reward_epoch=424,
                signatures_seen=40, registered=True, last_block=100, head=101)
    base.update(kw)
    return ObserveHealth(**base)


def test_budget_line_and_severity():
    healthy = {"epoch": 424, "rounds_elapsed": 2348, "rounds_total": 3360, "ftso_submitted": 2348,
               **budget_status(1.0, 2348, 3360, FTSO_MIN)}
    out = _plain(render_protocol_report(_h(budget=healthy)))
    assert "budget       : FTSO 100.0% (≥80) · 672/672 miss-budget left (100.0%)" in out

    breaching = {"epoch": 424, "rounds_elapsed": 1000, "rounds_total": 3360, "ftso_submitted": 750,
                 **budget_status(0.75, 1000, 3360, FTSO_MIN)}
    h = _h(budget=breaching)
    assert h.severity == "CRIT"  # budget CRIT drives the report CRIT
    assert "projected 75.0%" in _plain(render_protocol_report(h))


def test_delegation_line():
    deleg = {"validator": {"total": 140_206_326, "self_bond": 10_000_000, "delegated": 130_206_326,
                           "delegators": 60, "fee_pct": 20.0, "uptime": 100.0},
             "ftso": {"vote_power": 132_646_887.0}}
    out = _plain(render_protocol_report(_h(delegation=deleg)))
    assert "delegation   : validator 140.2M (10.0M self + 130.2M by 60 dels, 20% fee)" in out
    assert "FTSO 132.6M vote power" in out
