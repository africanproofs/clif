"""Fee-cap behaviour of RpcClient.suggest_fees (keyless read path)."""

from __future__ import annotations

import pytest

from clif.rpc import RpcClient, RpcError

_GWEI = 1_000_000_000


def _client(base_fee_wei: int) -> RpcClient:
    """RpcClient whose eth_feeHistory reports a fixed base fee."""
    client = RpcClient("http://rpc.invalid")
    client._call = lambda method, params: {  # type: ignore[method-assign]
        "baseFeePerGas": [hex(base_fee_wei)] * 4
    }
    return client


def test_suggest_fees_doubles_base_plus_tip(monkeypatch):
    monkeypatch.delenv("CLIF_MAX_FEE_PER_GAS_WEI", raising=False)
    max_fee, max_priority = _client(19).suggest_fees()
    assert max_fee == 19 * 2 + _GWEI
    assert max_priority == _GWEI


def test_suggest_fees_caps_at_default(monkeypatch):
    monkeypatch.delenv("CLIF_MAX_FEE_PER_GAS_WEI", raising=False)
    # 200 gwei base would want 401 gwei; the 300 gwei default clamps it.
    max_fee, _ = _client(200 * _GWEI).suggest_fees()
    assert max_fee == 300 * _GWEI


def test_suggest_fees_honours_env_cap(monkeypatch):
    monkeypatch.setenv("CLIF_MAX_FEE_PER_GAS_WEI", str(1500 * _GWEI))
    # Songbird's post-2026-07-07 floor: 500 gwei base -> 1001 gwei, under the cap.
    max_fee, max_priority = _client(500 * _GWEI).suggest_fees()
    assert max_fee == 1001 * _GWEI
    assert max_priority == _GWEI


def test_suggest_fees_raises_when_cap_below_base_fee(monkeypatch):
    """The Songbird 2026-07-07 regression: a 300 gwei cap under a 500 gwei floor."""
    monkeypatch.setenv("CLIF_MAX_FEE_PER_GAS_WEI", str(300 * _GWEI))
    with pytest.raises(RpcError) as exc:
        _client(500 * _GWEI).suggest_fees()
    message = str(exc.value)
    assert "underpriced" in message
    assert "CLIF_MAX_FEE_PER_GAS_WEI" in message
    assert str(1001 * _GWEI) in message  # the value that would work


def test_suggest_fees_rejects_nonpositive_env_cap(monkeypatch):
    monkeypatch.setenv("CLIF_MAX_FEE_PER_GAS_WEI", "0")
    with pytest.raises(ValueError, match="must be positive"):
        _client(19).suggest_fees()
