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
    assert "uptime       : 100% · connected" in out
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


def test_prune_offer_cache_keeps_current_and_prev(tmp_path):
    from clif.observe.reward_rule import prune_offer_cache

    for ep in (420, 421, 422, 423):
        (tmp_path / f"offer-params-flare-{ep}.json").write_text("{}")
    (tmp_path / "offer-params-songbird-100.json").write_text("{}")  # other net untouched
    prune_offer_cache(str(tmp_path), "flare", keep_epoch=423)
    remaining = sorted(p.name for p in tmp_path.glob("offer-params-flare-*.json"))
    assert remaining == ["offer-params-flare-422.json", "offer-params-flare-423.json"]
    assert (tmp_path / "offer-params-songbird-100.json").exists()
