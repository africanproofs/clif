"""Observer: real-tx decode, per-round FTSO checks, boundary guard, health severity."""

from __future__ import annotations

import json
import time

from py_flare_common.ftso.commit import commit_hash

from clif.observe.decode import Decoded, decode_submit
from clif.observe.health import ObserveHealth, build_status, read_observe_status
from clif.observe.state import ObserverState
from clif.observe.timing import voting_factory

# A real AP submit1 tx from Songbird (selector + FTSO commit payload).
_REAL_SUBMIT1 = "0x6c532fae640015b8a800203e3d6426c952c515d0d43338ceaf4ed873086dc98efff296e8e0a6ca80d197b8"
_SUBMIT = "0xa8BBBA017b2ce496bBED4CFC0FB5D1aFd4F23772"
_SIG = "0xf417294C5a65535d4a33259c73eeA39373977C65"


# ---- decode (real fixture) -----------------------------------------------------


def test_decode_real_submit1():
    d = decode_submit(_REAL_SUBMIT1)
    assert d is not None and d.kind == "submit1" and d.round_id == 1423528
    assert d.commit_hash.hex() == "3e3d6426c952c515d0d43338ceaf4ed873086dc98efff296e8e0a6ca80d197b8"


def test_decode_ignores_non_submit():
    assert decode_submit("0xdeadbeef1234") is None
    assert decode_submit("0x") is None


# ---- per-round checks (real timing factory) ------------------------------------

_F = voting_factory("songbird")
_RID = 1423528
_EP = _F.make_epoch(_RID)
_S1_LO, _S1_HI = _EP.start_s, _EP.end_s
_S2_LO = _EP.next.start_s


def _state(**kw):
    return ObserverState("songbird", _SUBMIT, _SIG, window_rounds=40, **kw)


def _finalize_one(st):
    # advance past the round's next-round end so it finalizes
    st.finalize_due(_EP.next.end_s + 1, _F)


def test_clean_round_is_clean():
    st = _state(observe_start_ts=_S1_LO - 10)
    # submit1 on-time with a commit that MATCHES the reveal we feed
    random, feed = 12345, b"\x01\x02\x03\x04"
    commit = bytes.fromhex(commit_hash(_SUBMIT, _RID, random, feed))
    st.record(Decoded("submit1", _RID, commit_hash=commit), _SUBMIT, _S1_LO + 1, _F)
    st.record(Decoded("submit2", _RID, reveal_random=random, reveal_feed_bytes=feed), _SUBMIT, _S2_LO + 1, _F)
    _finalize_one(st)
    agg = st.aggregates()
    assert agg["window_rounds"] == 1 and agg["complete"] == 1 and agg["reveal_offences"] == 0


def test_missing_reveal_after_commit_is_offence():
    st = _state(observe_start_ts=_S1_LO - 10)
    st.record(Decoded("submit1", _RID, commit_hash=b"\x00" * 32), _SUBMIT, _S1_LO + 1, _F)
    _finalize_one(st)
    agg = st.aggregates()
    assert agg["reveal_offences"] == 1 and agg["complete"] == 0


def test_commit_reveal_mismatch_is_offence():
    st = _state(observe_start_ts=_S1_LO - 10)
    st.record(Decoded("submit1", _RID, commit_hash=b"\xaa" * 32), _SUBMIT, _S1_LO + 1, _F)
    st.record(Decoded("submit2", _RID, reveal_random=1, reveal_feed_bytes=b"\x09"), _SUBMIT, _S2_LO + 1, _F)
    _finalize_one(st)
    assert st.aggregates()["reveal_offences"] == 1


def test_boundary_round_dropped_not_counted():
    # observe started AFTER this round's window opened → we couldn't have seen the commit.
    st = _state(observe_start_ts=_S1_HI + 1)
    st.record(Decoded("submit2", _RID, reveal_random=1, reveal_feed_bytes=b"\x09"), _SUBMIT, _S2_LO + 1, _F)
    _finalize_one(st)
    assert st.aggregates()["window_rounds"] == 0  # dropped, no false alarm


def test_late_submit1_is_off_window():
    st = _state(observe_start_ts=_S1_LO - 10)
    random, feed = 7, b"\x01"
    commit = bytes.fromhex(commit_hash(_SUBMIT, _RID, random, feed))
    st.record(Decoded("submit1", _RID, commit_hash=commit), _SUBMIT, _S1_HI + 5, _F)  # after window
    st.record(Decoded("submit2", _RID, reveal_random=random, reveal_feed_bytes=feed), _SUBMIT, _S2_LO + 1, _F)
    _finalize_one(st)
    assert st.aggregates()["off_window"] == 1


# ---- health severity -----------------------------------------------------------


def _health(**kw) -> ObserveHealth:
    base = dict(network="songbird", enabled=True, written_at=int(time.time()), window_rounds=40, complete=40)
    base.update(kw)
    return ObserveHealth(**base)


def test_health_all_clean_ok():
    assert _health().severity == "OK"


def test_health_reveal_offence_crit():
    assert _health(reveal_offences=1).severity == "CRIT"


def test_health_low_participation_crit():
    assert _health(complete=30).severity == "CRIT"  # 75% < 90


def test_health_isolated_miss_warn():
    assert _health(complete=39, missing_submit1=1).severity == "WARN"  # 97.5%


def test_health_stale_is_crit():
    assert _health(written_at=int(time.time()) - 999).severity == "CRIT"


def test_health_warming_up_is_warn():
    assert _health(window_rounds=0, complete=0).severity == "WARN"


def test_health_disabled_is_ok():
    assert _health(enabled=False).severity == "OK"


def test_read_status_missing_file_enabled_is_crit(tmp_path):
    h = read_observe_status(tmp_path / "nope.json", enabled=True)
    assert h.severity == "CRIT" and h.error is not None


def test_read_status_missing_file_disabled_is_ok(tmp_path):
    assert read_observe_status(tmp_path / "nope.json", enabled=False).severity == "OK"


def test_build_status_roundtrips(tmp_path):
    st = ObserverState("songbird", _SUBMIT, _SIG, observe_start_ts=0)
    st.last_block = 123
    d = build_status(st, network="songbird", enabled=True)
    p = tmp_path / "s.json"
    p.write_text(json.dumps(d))
    h = read_observe_status(p, enabled=True)
    assert h.network == "songbird" and h.last_block == 123
