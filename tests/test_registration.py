"""Registration readiness: the severity matrix (the RE423 detector), never-green-on-
error, and the revert-tolerant on-chain reads."""

from __future__ import annotations

import pytest

from clif import registration
from clif.registration import RegistrationReadiness, read_readiness, render_readiness
from clif.rpc import RpcClient, RpcError

_WEI = 10**18
_FLARE_IDENTITY = "0x26534aC74153E3257dDD3471f96faA33D5D3B575"
_FLARE_SUBMIT = "0x366BCb8c23490327A1C63880875a35de41705876"
_NONZERO3 = ("0x1", "0x2", "0x3")  # entity addresses present


# ---- severity matrix (construct the readiness directly) ------------------------


def _base(**kw) -> RegistrationReadiness:
    """A fully-healthy readiness; override fields per case."""
    defaults = dict(
        network="flare",
        current_epoch=423,
        next_epoch=424,
        current_registered=True,
        next_window_enabled=False,
        next_registered=False,
        next_weight=0,
        sender_balance=440.0,
        gas_floor=10.0,
        entity_ok=True,
        time_to_boundary_sec=100000.0,
    )
    defaults.update(kw)
    return RegistrationReadiness(**defaults)


def test_current_not_registered_is_crit():
    # The RE423 state: excluded from the current epoch's voter set.
    assert _base(current_registered=False).severity == "CRIT"


def test_gas_below_floor_is_crit():
    assert _base(sender_balance=5.0).severity == "CRIT"  # < 10 floor


def test_entity_gap_is_crit():
    assert _base(entity_ok=False).severity == "CRIT"


def test_window_open_zero_weight_is_crit():
    # Window open, not registered, and 0 vote power ⇒ excluded even if the tx lands.
    r = _base(next_window_enabled=True, next_registered=False, next_weight=0)
    assert r.votepower_ok is False and r.severity == "CRIT"


def test_window_open_prereqs_green_is_warn():
    # Window open, not yet registered, but weight/gas/entity fine ⇒ client is retrying.
    r = _base(next_window_enabled=True, next_registered=False, next_weight=5 * _WEI)
    assert r.severity == "WARN"


def test_gas_nearing_is_warn():
    assert _base(sender_balance=15.0).severity == "WARN"  # 10 <= 15 < 20


def test_registered_current_and_next_is_ok():
    assert _base(next_window_enabled=True, next_registered=True).severity == "OK"


def test_registered_current_window_closed_is_ok():
    assert _base().severity == "OK"


def test_read_error_is_crit_not_green():
    assert _base(error="node down").severity == "CRIT"


def test_unsupported_network_is_ok_not_crit():
    # coston2 has no VoterRegistry pinned — untracked, not a false alarm.
    r = RegistrationReadiness(network="coston2", supported=False)
    assert r.severity == "OK"


# ---- read_readiness (FakeRpc) --------------------------------------------------


class FakeRpc:
    def __init__(self, *, current=423, registered=None, window=None, weight=None,
                 balance=440.0, entity=_NONZERO3, raise_on=None):
        self.current = current
        self.registered = registered or {}
        self.window = window or {}
        self.weight = weight or {}
        self.balance = balance
        self.entity = entity
        self.raise_on = raise_on  # method name that raises RpcError

    def _maybe_raise(self, name):
        if self.raise_on == name:
            raise RpcError("node down")

    def get_current_reward_epoch_id(self, _fsm):
        self._maybe_raise("get_current_reward_epoch_id")
        return self.current

    def reward_epoch_timing(self, _fsm):
        return (1_700_000_000, 302400)  # arbitrary first_ts + ~3.5d duration

    def is_voter_registered(self, _vr, _voter, epoch):
        self._maybe_raise("is_voter_registered")
        return bool(self.registered.get(epoch, False))

    def voter_registration_data(self, _fsm, epoch):
        return self.window.get(epoch, (0, False))

    def voter_registration_weight(self, _vr, _voter, epoch):
        return self.weight.get(epoch, 0)

    def get_balance(self, _addr):
        return int(self.balance * _WEI)

    def get_voter_addresses(self, _em, _voter):
        return self.entity


def _read(fake, **kw):
    return read_readiness(
        fake, "flare",
        flare_systems_manager="0xFSM", voter_registry="0xVR", entity_manager="0xEM",
        **kw,
    )


def test_read_readiness_registered_is_ok():
    fake = FakeRpc(current=423, registered={423: True}, balance=440.0)
    r = _read(fake)
    assert r.error is None and r.current_registered is True
    assert r.current_epoch == 423 and r.next_epoch == 424 and r.severity == "OK"


def test_read_readiness_current_excluded_is_crit():
    # The live RE423 shape: registered for 422 but NOT 423.
    fake = FakeRpc(current=423, registered={422: True, 423: False})
    r = _read(fake)
    assert r.current_registered is False and r.severity == "CRIT"


def test_read_readiness_rpc_error_is_crit_never_green():
    fake = FakeRpc(raise_on="is_voter_registered")
    r = _read(fake)
    assert r.error is not None and r.severity == "CRIT"


def test_read_readiness_window_open_not_registered_is_warn():
    fake = FakeRpc(
        current=423, registered={423: True, 424: False},
        window={424: (12345, True)}, weight={424: 3 * _WEI},
    )
    r = _read(fake)
    assert r.next_window_enabled is True and r.next_registered is False
    assert r.severity == "WARN"


def test_read_readiness_gas_floor_targets_sender():
    fake = FakeRpc(current=423, registered={423: True}, balance=2.0)
    r = _read(fake, gas_floor=10.0)
    assert r.gas_ok is False and r.severity == "CRIT"


# ---- revert-tolerant on-chain reads (rpc.py) -----------------------------------


def _rpc_with_call(raiser):
    rpc = RpcClient.__new__(RpcClient)  # no network; we patch eth_call
    rpc.eth_call = raiser  # type: ignore[assignment]
    return rpc


def test_is_voter_registered_treats_voter_not_registered_as_false():
    def boom(_to, _data):
        raise RpcError("eth_call rpc error: execution reverted: voter not registered")
    rpc = _rpc_with_call(boom)
    assert rpc.is_voter_registered("0xVR", _FLARE_IDENTITY, 424) is False


def test_voter_registration_weight_treats_not_supported_as_zero():
    def boom(_to, _data):
        raise RpcError("execution reverted: reward epoch id not supported")
    rpc = _rpc_with_call(boom)
    assert rpc.voter_registration_weight("0xVR", _FLARE_IDENTITY, 999) == 0


def test_voter_registration_weight_reraises_real_error():
    def boom(_to, _data):
        raise RpcError("transport failure: connection refused")
    rpc = _rpc_with_call(boom)
    with pytest.raises(RpcError, match="connection refused"):
        rpc.voter_registration_weight("0xVR", _FLARE_IDENTITY, 424)


def test_voter_registration_data_revert_is_window_closed():
    def boom(_to, _data):
        raise RpcError("execution reverted: reward epoch id not supported")
    rpc = _rpc_with_call(boom)
    assert rpc.voter_registration_data("0xFSM", 999) == (0, False)


# ---- render (color + the RE423 headline) ---------------------------------------


def test_render_live_exclusion_is_loud_red():
    r = _base(current_registered=False)
    line = render_readiness(r, active=True)
    assert registration._RED in line and "NOT REGISTERED" in line and "LIVE EXCLUSION" in line


def test_render_registered_is_green():
    line = render_readiness(_base(next_registered=True, next_window_enabled=True), active=False)
    assert registration._GREEN in line and "registered" in line
