"""AutoState degraded transitions, cooldown, and `clif status` exit codes."""

from clif.autostate import (
    EXIT_DEGRADED,
    EXIT_HEALTHY,
    EXIT_NO_STATE,
    AutoState,
    build_report,
    read_status,
    status_exit_code,
    stream_key,
    write_status_atomic,
)

KEY = stream_key("flare", 1, "0xABC")


def test_stream_key_lowercases():
    assert KEY == "flare:1:0xabc"


def test_observe_tracks_and_releases_returning_claimed():
    st = AutoState()
    assert st.observe(KEY, [10, 11], now=100.0) == []  # both new
    # epoch 10 disappears (claimed); 11 stays; 12 new
    gone = st.observe(KEY, [11, 12], now=200.0)
    assert gone == [10]
    s = st.streams[KEY]
    assert set(s.first_seen) == {11, 12}
    assert s.first_seen[11] == 100.0  # original first-seen preserved


def test_stale_epoch_makes_degraded():
    st = AutoState()
    st.observe(KEY, [10], now=0.0)
    deg, reasons = st.evaluate(now=100.0, stale_after_sec=3600)
    assert deg is False
    deg, reasons = st.evaluate(now=4000.0, stale_after_sec=3600)
    assert deg is True
    assert "epoch 10 claimable" in reasons[0]


def test_terminal_cooldown_blocks_and_degrades():
    st = AutoState()
    st.observe(KEY, [10], now=0.0)
    st.record_terminal(KEY, 10, now=0.0, cooldown_sec=3600)
    assert st.in_cooldown(KEY, 10, now=1000.0) is True
    assert st.in_cooldown(KEY, 10, now=4000.0) is False
    deg, reasons = st.evaluate(now=1000.0, stale_after_sec=10**9)
    assert deg is True and "terminal fwd failure" in reasons[0]


def test_claimed_epoch_clears_cooldown_and_staleness():
    st = AutoState()
    st.observe(KEY, [10], now=0.0)
    st.record_terminal(KEY, 10, now=0.0, cooldown_sec=3600)
    st.observe(KEY, [], now=10.0)  # epoch 10 claimed/gone
    deg, _ = st.evaluate(now=10_000.0, stale_after_sec=1)
    assert deg is False  # nothing tracked anymore


def test_report_roundtrip_and_exit_codes(tmp_path):
    st = AutoState()
    st.observe(KEY, [10], now=1000.0)
    rep = build_report(st, "flare", poll_interval_sec=900, stale_after_sec=10**9, now=1000.0)
    assert rep["degraded"] is False
    p = tmp_path / "auto-status.json"
    write_status_atomic(p, rep)
    back = read_status(p)
    assert back == rep

    # healthy & fresh
    code, _ = status_exit_code(back, now=1000.0 + 100)
    assert code == EXIT_HEALTHY
    # stale (older than 3x interval) -> degraded/dead
    code, line = status_exit_code(back, now=1000.0 + 3 * 900 + 1)
    assert code == EXIT_DEGRADED and "dead" in line
    # degraded report
    st2 = AutoState()
    st2.observe(KEY, [10], now=0.0)
    rep2 = build_report(st2, "flare", 900, 1, now=10_000.0)
    assert rep2["degraded"] is True
    code, _ = status_exit_code(rep2, now=10_000.0 + 1)
    assert code == EXIT_DEGRADED
    # no state at all
    code, _ = status_exit_code(None)
    assert code == EXIT_NO_STATE


def test_status_exit_code_disabled_is_healthy():
    # A disabled daemon idles intentionally → healthy, and the staleness check is bypassed
    # (the report is ages-stale here, but `disabled` wins).
    rep = {"disabled": True, "network": "songbird", "updated_at_ts": 0.0, "poll_interval_sec": 1800}
    code, line = status_exit_code(rep, now=1.0e12)
    assert code == EXIT_HEALTHY
    assert "DISABLED" in line
