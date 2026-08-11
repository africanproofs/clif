"""Funding module: band health/severity + color rendering + the top-up pass."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from clif import funding
from clif.funding import (
    ACCOUNTS,
    _AP_FUNDER_ADDRESS,
    read_health,
    render_health,
    run_funding,
)
from clif.rpc import RpcError

_WEI = 10**18


def _healthy_balances(net: str = "flare") -> dict[str, int]:
    """Every account comfortably in band, funder full."""
    bal: dict[str, int] = {}
    for a in ACCOUNTS[net]:
        bal[a.address.lower()] = int((a.band.upper - 10) * _WEI)  # inside band
    bal[_AP_FUNDER_ADDRESS.lower()] = int(2000 * _WEI)
    return bal


class Chain:
    def __init__(self, balances: dict[str, int]) -> None:
        self.bal = dict(balances)

    def apply(self, to: str, value: int) -> None:
        self.bal[to.lower()] = self.bal.get(to.lower(), 0) + value


class FakeRpc:
    def __init__(self, chain: Chain, *, receipt_status: int = 1, raise_on: str | None = None):
        self.chain = chain
        self.receipt_status = receipt_status
        self.raise_on = raise_on  # address (lower) whose get_balance raises
        self._pending: tuple[str, int] | None = None

    def get_balance(self, addr: str) -> int:
        if self.raise_on == addr.lower():
            raise RpcError("node down")
        return self.chain.bal.get(addr.lower(), 0)

    def suggest_fees(self) -> tuple[int, int]:
        return (10**9, 10**9)

    def send_raw_transaction(self, _raw: str) -> str:
        assert self._pending is not None
        to, val = self._pending
        self.chain.apply(to, val)
        self._pending = None
        return "0x" + "aa" * 32

    def poll_receipt(self, _h: str, timeout: float = 120.0):
        return {"status": hex(self.receipt_status), "blockNumber": "0x10"}


class FakeFwd:
    def __init__(self, rpc: FakeRpc, *, sign_exc: Exception | None = None):
        self.rpc = rpc
        self.sign_exc = sign_exc

    def sign_transaction(self, **kw):
        if self.sign_exc:
            raise self.sign_exc
        self.rpc._pending = (kw["to"], int(kw["value_wei"]))
        return SimpleNamespace(tx_id="t", hash="0x" + "bb" * 32, signed_raw_tx="0x", nonce=0)

    def report_broadcast_result(self, *a, **k):
        pass

    def report_receipt(self, *a, **k):
        pass


# ---- health + severity + color -------------------------------------------------


def test_health_all_in_band_is_ok_green():
    rpc = FakeRpc(Chain(_healthy_balances()))
    h = read_health(rpc, "flare")
    assert h.severity == "OK"
    line = render_health(h, active=False)
    assert "✓" in line and funding._GREEN in line and "in band" in line


def test_account_below_floor_is_crit_red():
    bal = _healthy_balances()
    bal["0x366bcb8c23490327a1c63880875a35de41705876"] = int(180 * _WEI)  # Submit < 250
    h = read_health(FakeRpc(Chain(bal)), "flare")
    assert h.severity == "CRIT"
    assert [a.name for a in h.below] == ["Submit"]
    line = render_health(h, active=True)
    assert funding._RED in line and "BELOW" in line and "Submit" in line
    assert "🔴🔴" in line and "WHILE SIGNING" in line  # louder when active


def test_funder_exhausted_is_crit():
    bal = _healthy_balances()
    bal[_AP_FUNDER_ADDRESS.lower()] = int(50 * _WEI)  # < _FUNDER_CRIT
    h = read_health(FakeRpc(Chain(bal)), "flare")
    assert h.severity == "CRIT" and h.funder_crit
    assert "EXHAUSTED" in render_health(h, active=False)


def test_nearing_floor_is_warn_yellow():
    bal = _healthy_balances()
    bal["0x366bcb8c23490327a1c63880875a35de41705876"] = int(260 * _WEI)  # 250 < 260 < 275
    h = read_health(FakeRpc(Chain(bal)), "flare")
    assert h.severity == "WARN"
    line = render_health(h, active=False)
    assert funding._YELLOW in line and "⚠" in line


def test_rpc_error_is_crit_not_green():
    # RE423 lesson: a read failure must never look healthy.
    rpc = FakeRpc(Chain(_healthy_balances()), raise_on="0x366bcb8c23490327a1c63880875a35de41705876")
    h = read_health(rpc, "flare")
    assert h.severity == "CRIT" and h.error is not None
    assert "STATE UNKNOWN" in render_health(h, active=False)


# ---- enforcement ---------------------------------------------------------------


def test_run_funding_tops_below_accounts_to_upper():
    bal = _healthy_balances()
    bal["0x8af4f6eb1a26516ccb0fe02ad7182f96c0ca5bbb"] = int(100 * _WEI)  # FastUpdates-1 < 250
    rpc = FakeRpc(Chain(bal))
    res = run_funding(rpc=rpc, fwd=FakeFwd(rpc), network="flare", wallet="ap-funder", chain_id=14)
    assert [t.name for t in res.funded] == ["FastUpdates-1"]
    t = res.funded[0]
    assert t.before == pytest.approx(100.0) and t.after == pytest.approx(400.0)
    assert res.failed == []
    assert res.skipped_ok == 7  # the other 7 were in band


def test_run_funding_skips_in_band():
    rpc = FakeRpc(Chain(_healthy_balances()))
    res = run_funding(rpc=rpc, fwd=FakeFwd(rpc), network="flare", wallet="ap-funder", chain_id=14)
    assert res.topups == [] and res.skipped_ok == 8


def test_run_funding_sign_failure_is_failed():
    from clif.fwd_client import FwdTerminalError

    bal = _healthy_balances()
    bal["0x26534ac74153e3257ddd3471f96faa33d5d3b575"] = int(50 * _WEI)  # Identity < 150
    rpc = FakeRpc(Chain(bal))
    fwd = FakeFwd(rpc, sign_exc=FwdTerminalError(403, "policy_denied", "denied"))
    res = run_funding(rpc=rpc, fwd=fwd, network="flare", wallet="ap-funder", chain_id=14)
    assert [t.name for t in res.failed] == ["Identity"] and res.funded == []


def test_run_funding_no_balance_rise_is_failed():
    # mined but reverted (status 0) → not a success (mined ≠ success).
    bal = _healthy_balances()
    bal["0x85ba3af484fe8ee8bace1470aebd88d57a0078f3"] = int(100 * _WEI)  # FastUpdates-3
    rpc = FakeRpc(Chain(bal), receipt_status=0)
    # with status 0 the fake still applied the transfer; force no-rise by not applying:
    rpc.send_raw_transaction = lambda _raw: "0x" + "aa" * 32  # type: ignore[method-assign]
    res = run_funding(rpc=rpc, fwd=FakeFwd(rpc), network="flare", wallet="ap-funder", chain_id=14)
    assert [t.name for t in res.failed] == ["FastUpdates-3"] and res.funded == []
