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
    (422, "transaction_rejected"),  # fwd v1.0.0: node deterministically refused
])
def test_terminal_statuses(code, err):
    def h(_req):
        return httpx.Response(code, json={"error": err, "message": "nope"})

    with _client(h) as fwd:
        with pytest.raises(FwdTerminalError) as ei:
            fwd.sign_and_send("w", 114, "0x" + "00" * 20, "0x")
    assert ei.value.status == code
    assert ei.value.error_code == err


@pytest.mark.parametrize("code,err", [
    (502, "rpc_unreachable"),
    (503, "vault_unavailable"),  # fwd v1.0.0: sealed-master is now RETRYABLE
])
def test_retryable_statuses(code, err):
    def h(_req):
        return httpx.Response(code, json={"error": err, "message": "down"})

    with _client(h) as fwd:
        with pytest.raises(FwdRetryableError) as ei:
            fwd.sign_and_send("w", 114, "0x" + "00" * 20, "0x")
    assert ei.value.status == code


def _raising_client(exc) -> FwdClient:
    def h(req):
        raise exc

    fwd = FwdClient("http://fwd:8080", "fwd_live_test")
    fwd._client = httpx.Client(transport=httpx.MockTransport(h))
    return fwd


@pytest.mark.parametrize("exc", [
    httpx.ConnectError("down"),
    httpx.ReadTimeout("slow"),
    httpx.PoolTimeout("pool"),
])
def test_transport_error_is_retryable_sign_path(exc):
    """A down/restarting fwd must NOT propagate a raw httpx error — it would
    crash `clif auto`. Converted to FwdRetryableError in the sign path."""
    with _raising_client(exc) as fwd:
        with pytest.raises(FwdRetryableError) as ei:
            fwd.sign_and_send("w", 114, "0x" + "00" * 20, "0x")
    assert ei.value.error_code == "transport_error"


def test_transport_error_is_retryable_status_path():
    with _raising_client(httpx.ConnectError("down")) as fwd:
        with pytest.raises(FwdRetryableError):
            fwd.get_transaction("019e-abc")


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


def test_idempotency_retry_discriminator_both_directions():
    """STOP-SHIP #2 safety property — must not regress.

    - retry=None is byte-identical to the legacy positional key.
    - SAME logical attempt + SAME retry ⇒ SAME key (a network retry /
      crash-rerun still dedups at fwd — no double-claim).
    - A NEW retry value ⇒ a fresh key (a DELIBERATE post-failure re-attempt).
    """
    legacy = make_idempotency_key("flare", 1, "0xABC", 100)
    assert make_idempotency_key("flare", 1, "0xABC", 100, retry=None) == legacy
    # same-attempt dedup: identical inputs incl. retry collide
    assert (
        make_idempotency_key("flare", 1, "0xABC", 100, retry="op-2")
        == make_idempotency_key("flare", 1, "0xabc", 100, retry="op-2")
    )
    # deliberate retry: a new discriminator yields a distinct key
    k1 = make_idempotency_key("flare", 1, "0xABC", 100, retry="op-1")
    k2 = make_idempotency_key("flare", 1, "0xABC", 100, retry="op-2")
    assert k1 != k2 != legacy and k1 != legacy
    assert len(k2) <= 128
