"""Phase 2b/2c: fast-updates (255) + P-chain uptime + the explicit per-protocol FSP report."""

from __future__ import annotations

from clif.observe.health import ObserveHealth, render_protocol_report
from clif.observe.state import ObserverState


# ---- fast-updates rolling counts -------------------------------------------------


def test_windowed_fastupdates_counts_by_horizon():
    st = ObserverState("flare", "0x" + "a" * 40, "0x" + "b" * 40)
    now = 1_000_000
    for dt in (100, 1000, 5 * 3600, 20 * 3600):  # 1 within 1h→ actually 2 within 1h, etc.
        st.record_fast_update(now - dt)
    w = st.windowed_fastupdates(now)
    assert w["1h"] == 2 and w["6h"] == 3 and w["24h"] == 4 and w["total_tracked"] == 4


def test_signatures_seen_aggregate():
    st = ObserverState("flare", "0x" + "a" * 40, "0x" + "b" * 40)
    r = st._round(5)
    r.submit1_seen = r.submit2_seen = r.sig_seen = True
    st._finalize(r, 1000)
    assert st.aggregates()["signatures_seen"] == 1


# ---- P-chain uptime (URL derivation + parse) ------------------------------------


class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class _FakeClient:
    def __init__(self, payload):
        self._p = payload
        self.last_url = None

    def post(self, url, json=None):  # noqa: A002
        self.last_url = url
        return _FakeResp(self._p)


def test_validator_uptime_derives_pchain_url_and_parses():
    from clif.rpc import RpcClient

    rpc = RpcClient.__new__(RpcClient)
    rpc._url = "http://51.159.197.64:9650/ext/bc/C/rpc"
    rpc._id = 0
    rpc._client = _FakeClient({"result": {"validators": [{"uptime": "99.9500", "connected": True}]}})
    res = rpc.validator_uptime("NodeID-X")
    assert res == (99.95, True)
    assert rpc._client.last_url == "http://51.159.197.64:9650/ext/bc/P"


def test_validator_uptime_none_when_not_in_set():
    from clif.rpc import RpcClient

    rpc = RpcClient.__new__(RpcClient)
    rpc._url = "http://h/ext/bc/C/rpc"
    rpc._id = 0
    rpc._client = _FakeClient({"result": {"validators": []}})
    assert rpc.validator_uptime("NodeID-X") is None


# ---- the explicit report ---------------------------------------------------------


def _health(**kw):
    base = dict(
        network="flare", enabled=True, window_rounds=40, complete=39,
        missing_submit1=0, missing_submit2=0, off_window=1, reveal_offences=0,
        signatures_seen=40, fdc_request_rounds=12, fdc_participated=12,
        registered=True, reward_epoch=424,
        iqr_windows={k: {"inner_pct": 50.0, "outer_pct": 95.0, "feed_rounds": 630, "rounds": 10, "capped": 1}
                     for k in ("1h", "6h", "24h", "epoch")},
        fu_windows={"1h": 33, "6h": 190, "24h": 760, "total_tracked": 760},
        uptime_pct=100.0, uptime_connected=True, validator_node="NodeID-FLPF99",
    )
    base.update(kw)
    return ObserveHealth(**base)


def _plain(lines):
    import re

    return "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in lines)


def test_report_has_a_line_per_protocol_when_registered():
    out = _plain(render_protocol_report(_health()))
    assert "FSP protocol health — flare RE424" in out
    assert "registration : ✓ registered" in out
    assert "FTSO (100)   : commit 40/40 · reveal 40/40 · sigs 40/40" in out
    assert "FDC (200)    : 12/12 bitvoted" in out
    assert "FastUpd (255): 1h 33 · 6h 190 · 24h 760" in out
    assert "uptime       : our-node 100% · connected" in out
    assert "IQR quality" in out


def test_report_reflects_exclusion_and_no_validator():
    out = _plain(render_protocol_report(
        _health(network="songbird", registered=False, reward_epoch=423, validator_node=None,
                fdc_request_rounds=0, fu_windows={"1h": 0, "6h": 0, "24h": 0, "total_tracked": 0})
    ))
    assert "NOT REGISTERED — submissions earn ZERO" in out
    assert "no attestation requests" in out
    assert "not registered ⇒ no sortition weight" in out
    assert "n/a (no validator on this net)" in out
    assert "would-be IQR" in out


def test_report_flags_reveal_offence():
    out = _plain(render_protocol_report(_health(reveal_offences=2)))
    assert "2 REVEAL OFFENCE" in out


def test_report_includes_resource_gauge():
    out = _plain(render_protocol_report(
        _health(resources={"in_flight_rounds": 2, "iqr_hist": 120, "fu_events": 33, "rss_mib": 84.2})
    ))
    assert "resources    : in-flight-rounds 2 · iqr-hist 120 · fu-events 33 · rss 84.2 MiB" in out


# ---- leak prevention: bounded in-flight rounds -----------------------------------


class _FakeEpoch:
    def __init__(self, rid):
        self.id = rid


class _FakeFactory:
    """Minimal timing factory: round id = ts // 90 (matches from_timestamp(ts).id contract)."""

    def from_timestamp(self, ts):
        return _FakeEpoch(ts // 90)


def test_round_future_orphan_guard_not_retained():
    st = ObserverState("flare", "0x" + "a" * 40, "0x" + "b" * 40, factory=_FakeFactory())
    st.last_ts = 90_000  # current round = 1000
    st._round(1002)  # within margin (≤ cur+4) → retained
    st._round(9999)  # far future → throwaway, NOT stored
    assert 1002 in st.rounds and 9999 not in st.rounds


def test_round_hard_cap_evicts_stalest():
    from clif.observe.state import _ROUNDS_CAP

    st = ObserverState("flare", "0x" + "a" * 40, "0x" + "b" * 40)  # no factory ⇒ guard off, cap on
    for rid in range(_ROUNDS_CAP + 50):
        st._round(rid)
    assert len(st.rounds) <= _ROUNDS_CAP
    assert 0 not in st.rounds  # stalest (lowest) evicted


# ---- leak prevention: offer-params cache prune -----------------------------------


# ---- trustlessness: independent-RPC quorum ---------------------------------------


class _StubRpc:
    def __init__(self, val=None, raise_exc=False):
        self._val = val
        self._raise = raise_exc

    def read(self):
        from clif.rpc import RpcError

        if self._raise:
            raise RpcError("verify node down")
        return self._val


def test_verifier_agree_dispute_unavailable():
    from clif.observe.verify import CrossVerifier, quorum_overall

    v_true = CrossVerifier(_StubRpc(True), "verify.example")
    assert v_true.compare(True, lambda r: r.read())["status"] == "agree"
    assert v_true.compare(False, lambda r: r.read())["status"] == "dispute"

    v_down = CrossVerifier(_StubRpc(raise_exc=True), "verify.example")
    assert v_down.compare(True, lambda r: r.read())["status"] == "unavailable"

    assert quorum_overall({}) == "off"
    assert quorum_overall({"a": {"status": "agree"}, "b": {"status": "unavailable"}}) == "agree"
    assert quorum_overall({"a": {"status": "agree"}, "b": {"status": "dispute"}}) == "dispute"


def test_verifier_voter_set_order_insensitive():
    from clif.observe.verify import CrossVerifier

    v = CrossVerifier(_StubRpc(["0xBB", "0xaa"]), "verify.example")
    res = v.compare(["0xAA", "0xbb"], lambda r: r.read(), key=lambda x: sorted(a.lower() for a in x))
    assert res["status"] == "agree"  # same set, different order/case


def test_report_quorum_lines_and_severity():
    q_agree = {"registration": {"status": "agree"}, "reward_epoch": {"status": "agree"}}
    out = _plain(render_protocol_report(_health(quorum=q_agree, verify_host="flare-api.flare.network")))
    assert "quorum       : ✓ 2 gating facts agree (verify: flare-api.flare.network)" in out

    q_disp = {"registration": {"status": "dispute", "primary": "True", "verify": "False"}}
    hd = _health(quorum=q_disp, verify_host="flare-api.flare.network")
    out = _plain(render_protocol_report(hd))
    assert "DISPUTED — registration our=True verify=False" in out
    assert hd.severity == "WARN"  # dispute ⇒ WARN by default
    assert _health(quorum=q_disp, quorum_crit=True).severity == "CRIT"  # escalatable


def test_report_dual_uptime():
    out = _plain(render_protocol_report(_health(uptime_verify=[99.98, True])))
    assert "our-node 100% · " in out and "verify-node 99.98%" in out


def test_prune_offer_cache_keeps_current_and_prev(tmp_path):
    from clif.observe.reward_rule import prune_offer_cache

    for ep in (420, 421, 422, 423):
        (tmp_path / f"offer-params-flare-{ep}.json").write_text("{}")
    (tmp_path / "offer-params-songbird-100.json").write_text("{}")  # other net untouched
    prune_offer_cache(str(tmp_path), "flare", keep_epoch=423)
    remaining = sorted(p.name for p in tmp_path.glob("offer-params-flare-*.json"))
    assert remaining == ["offer-params-flare-422.json", "offer-params-flare-423.json"]
    assert (tmp_path / "offer-params-songbird-100.json").exists()


# ---- the bottom-line verdict (proclamation + call to action) --------------------


def test_verdict_healthy_proclaims_no_action():
    h = _health(off_window=0)  # base has an off-window miss; clear it for the clean case
    level, headline, actions = h.verdict()
    assert level == "OK"
    assert "HEALTHY" in headline and actions == []
    out = _plain(render_protocol_report(h))
    assert "VERDICT      : ✅ SYSTEM HEALTHY" in out
    assert "→ ACTION" not in out  # nothing to do


def test_verdict_not_registered_is_critical_with_action():
    h = _health(registered=False)
    level, headline, actions = h.verdict()
    assert level == "CRIT"
    assert "CRITICAL" in headline and "NOT REGISTERED" in headline
    assert any("registerVoter" in a for a in actions)
    out = _plain(render_protocol_report(h))
    assert "VERDICT      : 🔴 SYSTEM CRITICAL" in out
    assert "→ ACTION" in out


def test_verdict_warn_isolated_miss_watches():
    h = _health(off_window=1)  # base default: one off-window round → WARN
    level, headline, actions = h.verdict()
    assert level == "WARN"
    assert "DEGRADED" in headline and "off-window" in headline
    assert any("watch" in a.lower() for a in actions)


def test_verdict_in_json_surface():
    v = _health(off_window=0).to_dict()["verdict"]
    assert v["level"] == "OK" and "HEALTHY" in v["headline"] and v["actions"] == []


# ---- recovery-aware verdict + cadence (a stale isolated miss self-clears) --------


def test_recovering_after_enough_clean_rounds():
    # Fresh isolated off-window miss, no clean rounds yet → active DEGRADED.
    fresh = _health(off_window=1, trailing_clean=0)
    assert fresh.severity == "WARN" and fresh.recovering is False
    # Same miss still in the window, but the last 3 rounds are clean → RECOVERING.
    recov = _health(off_window=1, trailing_clean=3)
    assert recov.severity == "WARN" and recov.recovering is True
    out = _plain(render_protocol_report(recov))
    assert "VERDICT      : ✓ RECOVERING" in out and "self-clearing" in out
    assert "→ ACTION" not in out  # nothing to do — it's aging out on its own


def test_reveal_offence_never_recovering():
    h = _health(off_window=0, reveal_offences=1, trailing_clean=10)
    assert h.severity == "CRIT" and h.recovering is False


def test_recovering_needs_isolated_miss_not_just_clean_rounds():
    # A clean, fully-OK window is not "recovering" — there is nothing to recover from.
    h = _health(off_window=0, trailing_clean=40)
    assert h.severity == "OK" and h.recovering is False


# ---- minimal-conditions panel (FTSO / FDC / uptime gates in one line) -----------

_MC_BUDGET = {
    "rate_pct": 99.8, "threshold_pct": 80, "budget_left": 667, "miss_budget": 672,
    "budget_left_pct": 99.3, "projected_final_pct": 99.8, "eta_rounds_to_breach": None,
    "rounds_elapsed": 2992, "rounds_total": 3360, "severity": "OK",
}


def _mc_line(h):
    return next((ln for ln in _plain(render_protocol_report(h)).splitlines() if "min-cond" in ln), "")


def test_min_conditions_panel_shows_all_three_gates():
    # No per-epoch ledger yet ⇒ FDC falls back to the rolling window (`1h obs`), no FU epoch line.
    line = _mc_line(_health(budget=_MC_BUDGET, off_window=0))
    assert "FTSO 99.8% (≥80" in line and "667/672 budget" in line
    assert "FDC" in line and "(≥60 · 1h obs)" in line
    assert "uptime" in line and "(≥80)" in line
    assert "[ep 2992/3360]" in line


_MC_EPOCH = {"epoch": 426, "rounds_recorded": 900, "fdc_expected": 225,
             "fdc_participated": 224, "fdc_pct": 99.6, "fu_updates": 1200}


def test_min_conditions_uses_exact_epoch_ledger_for_fdc_and_fu():
    line = _mc_line(_health(budget=_MC_BUDGET, off_window=0, mincond=_MC_EPOCH))
    assert "FDC 99.6% (≥60 · 224/225 epoch)" in line  # EXACT full-epoch, not the 1h window
    assert "FU 1200 (epoch)" in line                   # cumulative epoch fast-updates
    assert "1h obs" not in line                         # the window fallback is not used


def test_exact_epoch_fdc_breach_is_critical():
    breach = {**_MC_EPOCH, "fdc_expected": 100, "fdc_participated": 55, "fdc_pct": 55.0}  # < 60 floor
    assert _health(off_window=0, mincond=breach).severity == "CRIT"
    # …but a small sample (<10 expected) does not trip it (noise guard).
    tiny = {**_MC_EPOCH, "fdc_expected": 3, "fdc_participated": 0, "fdc_pct": 0.0}
    assert _health(off_window=0, mincond=tiny).severity == "OK"


def test_uptime_breach_below_floor_is_critical():
    h = _health(off_window=0, uptime_pct=70.0)  # < 80 minimal-condition floor
    assert h.severity == "CRIT"
    assert _health(off_window=0, uptime_pct=99.99).severity == "OK"  # healthy uptime does not


def test_min_conditions_omits_uptime_gate_without_a_validator():
    line = _mc_line(_health(budget=_MC_BUDGET, off_window=0, validator_node=None))
    assert "FTSO" in line and "FDC" in line and "uptime" not in line  # songbird: no validator gate


def test_fast_updates_shown_as_volume_not_a_pass_fail_floor():
    # Fast-updates is tracked per-epoch (cumulative count) but has no ≥floor — a volume metric.
    line = _mc_line(_health(budget=_MC_BUDGET, off_window=0, mincond=_MC_EPOCH))
    assert "FU 1200 (epoch)" in line and "FU 1200 (epoch) (≥" not in line
