"""fwd transport: status -> terminal/retryable classification + envelope parse.

Updated for fwd v1.1.0a9+ sign-only API:
  sign_and_send -> sign_transaction (fwd signs; clif broadcasts)
  New: report_broadcast_result, report_receipt
  Removed: wait_until_mined (clif polls rpc.py directly)
  Status taxonomy: 502 gone (fwd no longer does RPC), 409 added (terminal).
"""

import httpx
import pytest

from clif.fwd_client import (
    FwdClient,
    FwdRetryableError,
    FwdTerminalError,
    make_idempotency_key,
)
from clif.models import BroadcastResultResponse, ReceiptResponse, SignTransactionResponse


def _client(handler) -> FwdClient:
    fwd = FwdClient("http://fwd:8080", "fwd_live_test")
    fwd._client = httpx.Client(transport=httpx.MockTransport(handler))
    return fwd


# ---- sign_transaction ----


def test_sign_transaction_success_200():
    def h(_req):
        return httpx.Response(
            200,
            json={
                "tx_id": "019e-abc",
                "hash": "0xdead",
                "signed_raw_tx": "0xf86c",
                "nonce": 7,
            },
        )

    with _client(h) as fwd:
        r = fwd.sign_transaction(
            "w",
            114,
            "0x" + "00" * 20,
            gas=500_000,
            max_fee_per_gas=100_000_000_000,
            max_priority_fee_per_gas=1_000_000_000,
            data="0x",
        )
    assert r.tx_id == "019e-abc"
    assert r.nonce == 7
    assert r.signed_raw_tx == "0xf86c"
    assert isinstance(r, SignTransactionResponse)


def test_sign_transaction_request_body():
    """Request must include gas, max_fee_per_gas, max_priority_fee_per_gas."""
    import json

    captured: list[dict] = []

    def h(req: httpx.Request) -> httpx.Response:
        captured.append(json.loads(req.content))
        return httpx.Response(
            200,
            json={"tx_id": "t1", "hash": "0x1", "signed_raw_tx": "0xabc", "nonce": 0},
        )

    with _client(h) as fwd:
        fwd.sign_transaction(
            wallet="claim-wallet",
            chain=114,
            to="0x" + "cc" * 20,
            gas=200_000,
            max_fee_per_gas=50_000_000_000,
            max_priority_fee_per_gas=1_000_000_000,
            data="0xcafe",
            value_wei="0",
            idempotency_key="idem-123",
        )

    body = captured[0]
    assert body["wallet"] == "claim-wallet"
    assert body["chain"] == 114
    assert body["gas"] == 200_000
    assert body["max_fee_per_gas"] == 50_000_000_000
    assert body["max_priority_fee_per_gas"] == 1_000_000_000
    assert body["data"] == "0xcafe"
    assert body["value_wei"] == "0"


def test_sign_transaction_idempotency_key_header():
    def h(req: httpx.Request) -> httpx.Response:
        assert req.headers.get("Idempotency-Key") == "idem-abc"
        return httpx.Response(
            200,
            json={"tx_id": "t", "hash": "0x1", "signed_raw_tx": "0x2", "nonce": 0},
        )

    with _client(h) as fwd:
        fwd.sign_transaction(
            "w",
            114,
            "0x" + "00" * 20,
            gas=100_000,
            max_fee_per_gas=10_000_000_000,
            max_priority_fee_per_gas=1_000_000_000,
            idempotency_key="idem-abc",
        )


@pytest.mark.parametrize(
    "code,err",
    [
        (400, "tx_params_rejected"),
        (401, "unauthorized"),
        (403, "policy_denied"),
        (404, "wallet_not_found"),
        (409, "nonce_not_initialized"),  # fwd v1.1.0a9+: operator must run nonce-init
        (422, "transaction_rejected"),
    ],
)
def test_sign_transaction_terminal_statuses(code, err):
    def h(_req):
        return httpx.Response(code, json={"error": err, "message": "nope"})

    with _client(h) as fwd:
        with pytest.raises(FwdTerminalError) as ei:
            fwd.sign_transaction(
                "w",
                114,
                "0x" + "00" * 20,
                gas=100_000,
                max_fee_per_gas=10_000_000_000,
                max_priority_fee_per_gas=1_000_000_000,
            )
    assert ei.value.status == code
    assert ei.value.error_code == err


def test_sign_transaction_503_retryable():
    """503 (vault_unavailable) is the only retryable fwd status in v1.1.0a9+."""

    def h(_req):
        return httpx.Response(503, json={"error": "vault_unavailable", "message": "down"})

    with _client(h) as fwd:
        with pytest.raises(FwdRetryableError) as ei:
            fwd.sign_transaction(
                "w",
                114,
                "0x" + "00" * 20,
                gas=100_000,
                max_fee_per_gas=10_000_000_000,
                max_priority_fee_per_gas=1_000_000_000,
            )
    assert ei.value.status == 503


def test_sign_transaction_502_is_now_terminal():
    """502 is GONE in fwd v1.1.0a9+ (fwd no longer does RPC). Unmapped statuses
    fall through to the terminal catch-all in _raise_for_error."""

    def h(_req):
        return httpx.Response(502, json={"error": "old_rpc_unreachable", "message": "gone"})

    with _client(h) as fwd:
        with pytest.raises(FwdTerminalError):
            fwd.sign_transaction(
                "w",
                114,
                "0x" + "00" * 20,
                gas=100_000,
                max_fee_per_gas=10_000_000_000,
                max_priority_fee_per_gas=1_000_000_000,
            )


def _raising_client(exc) -> FwdClient:
    def h(req):
        raise exc

    fwd = FwdClient("http://fwd:8080", "fwd_live_test")
    fwd._client = httpx.Client(transport=httpx.MockTransport(h))
    return fwd


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("down"),
        httpx.ReadTimeout("slow"),
        httpx.PoolTimeout("pool"),
    ],
)
def test_transport_error_is_retryable_sign_path(exc):
    """A down/restarting fwd must NOT propagate a raw httpx error — it would
    crash `clif auto`. Converted to FwdRetryableError in the sign path."""
    with _raising_client(exc) as fwd:
        with pytest.raises(FwdRetryableError) as ei:
            fwd.sign_transaction(
                "w",
                114,
                "0x" + "00" * 20,
                gas=100_000,
                max_fee_per_gas=10_000_000_000,
                max_priority_fee_per_gas=1_000_000_000,
            )
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
            fwd.sign_transaction(
                "w",
                114,
                "0x" + "00" * 20,
                gas=100_000,
                max_fee_per_gas=10_000_000_000,
                max_priority_fee_per_gas=1_000_000_000,
            )


# ---- report_broadcast_result ----


def test_report_broadcast_result_accepted():
    def h(_req):
        return httpx.Response(200, json={"tx_id": "tx-1", "status": "broadcast_accepted"})

    with _client(h) as fwd:
        r = fwd.report_broadcast_result("tx-1", "0xhash", "accepted")
    assert r.tx_id == "tx-1"
    assert isinstance(r, BroadcastResultResponse)


def test_report_broadcast_result_rejected_releaseable():
    import json

    captured: list[dict] = []

    def h(req: httpx.Request) -> httpx.Response:
        captured.append(json.loads(req.content))
        return httpx.Response(200, json={"tx_id": "tx-2", "status": "nonce_released"})

    with _client(h) as fwd:
        fwd.report_broadcast_result("tx-2", "0xhash", "rejected_releaseable", "RpcError")

    body = captured[0]
    assert body["outcome"] == "rejected_releaseable"
    assert body["error_class"] == "RpcError"
    assert body["tx_hash"] == "0xhash"


def test_report_broadcast_result_no_error_class():
    import json

    captured: list[dict] = []

    def h(req: httpx.Request) -> httpx.Response:
        captured.append(json.loads(req.content))
        return httpx.Response(200, json={"tx_id": "tx-3", "status": "broadcast_accepted"})

    with _client(h) as fwd:
        fwd.report_broadcast_result("tx-3", "0xhash", "accepted")

    body = captured[0]
    assert "error_class" not in body


def test_report_broadcast_result_terminal_on_409():
    def h(_req):
        return httpx.Response(409, json={"error": "tx_hash_mismatch", "message": "bad"})

    with _client(h) as fwd:
        with pytest.raises(FwdTerminalError) as ei:
            fwd.report_broadcast_result("tx-x", "0xh", "accepted")
    assert ei.value.status == 409


def test_report_broadcast_result_transport_error_retryable():
    with _raising_client(httpx.ConnectError("down")) as fwd:
        with pytest.raises(FwdRetryableError):
            fwd.report_broadcast_result("tx-x", "0xh", "accepted")


# ---- report_receipt ----


def test_report_receipt_mined_success():
    import json

    captured: list[dict] = []

    def h(req: httpx.Request) -> httpx.Response:
        captured.append(json.loads(req.content))
        return httpx.Response(200, json={"tx_id": "tx-r", "status": "receipt_recorded"})

    with _client(h) as fwd:
        r = fwd.report_receipt("tx-r", "0xhash", "mined_success", 999)

    assert r.tx_id == "tx-r"
    assert isinstance(r, ReceiptResponse)
    body = captured[0]
    assert body["outcome"] == "mined_success"
    assert body["block_number"] == 999
    assert body["tx_hash"] == "0xhash"


def test_report_receipt_mined_reverted():
    def h(_req):
        return httpx.Response(200, json={"tx_id": "tx-rv", "status": "receipt_recorded"})

    with _client(h) as fwd:
        r = fwd.report_receipt("tx-rv", "0xhash", "mined_reverted", 1000)
    assert r.tx_id == "tx-rv"


def test_report_receipt_transport_error_retryable():
    with _raising_client(httpx.ConnectError("down")) as fwd:
        with pytest.raises(FwdRetryableError):
            fwd.report_receipt("tx-x", "0xh", "mined_success", 1)


# ---- idempotency key helpers ----


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
    assert make_idempotency_key("flare", 1, "0xABC", 100, retry="op-2") == make_idempotency_key(
        "flare", 1, "0xabc", 100, retry="op-2"
    )
    # deliberate retry: a new discriminator yields a distinct key
    k1 = make_idempotency_key("flare", 1, "0xABC", 100, retry="op-1")
    k2 = make_idempotency_key("flare", 1, "0xABC", 100, retry="op-2")
    assert k1 != k2 != legacy and k1 != legacy
    assert len(k2) <= 128


# ---- 409 nonce_not_initialized surface test ----


def test_nonce_not_initialized_surfaces_clearly():
    """409 from fwd must surface as FwdTerminalError with nonce_not_initialized code.
    Callers (claimer) must distinguish this from policy_denied to give the
    operator the right action (run `clifwd nonce-init`, not check policy)."""

    def h(_req):
        return httpx.Response(
            409,
            json={
                "error": "nonce_not_initialized",
                "message": "run clifwd nonce-init for this wallet+chain",
            },
        )

    with _client(h) as fwd:
        with pytest.raises(FwdTerminalError) as ei:
            fwd.sign_transaction(
                "w",
                14,
                "0x" + "00" * 20,
                gas=100_000,
                max_fee_per_gas=10_000_000_000,
                max_priority_fee_per_gas=1_000_000_000,
            )
    assert ei.value.status == 409
    assert ei.value.error_code == "nonce_not_initialized"
