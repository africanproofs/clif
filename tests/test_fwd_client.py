"""fwd transport: status -> terminal/retryable classification + envelope parse."""

import httpx
import pytest

from clif.fwd_client import (
    FwdClient,
    FwdRetryableError,
    FwdTerminalError,
    make_idempotency_key,
)


def _client(handler) -> FwdClient:
    fwd = FwdClient("http://fwd:8080", "fwd_live_test")
    fwd._client = httpx.Client(transport=httpx.MockTransport(handler))
    return fwd


def test_success_200():
    def h(_req):
        return httpx.Response(
            200, json={"tx_id": "019e-abc", "hash": "0xdead", "nonce": 7}
        )

    with _client(h) as fwd:
        r = fwd.sign_and_send("w", 114, "0x" + "00" * 20, "0x")
    assert r.tx_id == "019e-abc"
    assert r.nonce == 7


@pytest.mark.parametrize("code,err", [
    (400, "bad_request"),
    (401, "unauthorized"),
    (403, "policy_denied"),
    (404, "wallet_not_found"),
    (503, "vault_unavailable"),
])
def test_terminal_statuses(code, err):
    def h(_req):
        return httpx.Response(code, json={"error": err, "message": "nope"})

    with _client(h) as fwd:
        with pytest.raises(FwdTerminalError) as ei:
            fwd.sign_and_send("w", 114, "0x" + "00" * 20, "0x")
    assert ei.value.status == code
    assert ei.value.error_code == err


def test_retryable_502():
    def h(_req):
        return httpx.Response(502, json={"error": "rpc_unreachable", "message": "down"})

    with _client(h) as fwd:
        with pytest.raises(FwdRetryableError) as ei:
            fwd.sign_and_send("w", 114, "0x" + "00" * 20, "0x")
    assert ei.value.status == 502


def test_unmapped_status_fails_closed_terminal():
    def h(_req):
        return httpx.Response(418, text="teapot")

    with _client(h) as fwd:
        with pytest.raises(FwdTerminalError):
            fwd.sign_and_send("w", 114, "0x" + "00" * 20, "0x")


def test_idempotency_key_deterministic_distinct_bounded():
    a = make_idempotency_key("flare", 1, "0xABC", 100)
    b = make_idempotency_key("flare", 1, "0xabc", 100)  # case-insensitive
    c = make_idempotency_key("flare", 1, "0xABC", 101)  # different epoch
    d = make_idempotency_key("songbird", 1, "0xABC", 100)  # different net
    assert a == b
    assert a != c and a != d
    assert len(a) <= 128
