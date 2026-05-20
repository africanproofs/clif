"""FSP fwd_client: sign_fsp_message cross-field validation, HTTP contract, error mapping."""

import httpx
import pytest

from clif.fwd_client import (
    FwdClient,
    FwdRetryableError,
    FwdTerminalError,
    make_fsp_idempotency_key,
)
from clif.models import SignFspMessageResponse

FAKE_RESPONSE = {
    "message_hash": "0xb7e97e6b4b2c7cd5fb9b51a86ad7eae441872b770b5953443024cb1e0bc6f67d",
    "v": 27,
    "r": "0x9938afc59dae94cb20e0c5982e00c6a88afc01f6ff8c058024f999857a32e785",
    "s": "0x1e926390fbdece399aa1c56dbcbc66d128d43fba246b9459d5018d0c2de9b4b5",
    "signature": "0x" + "ab" * 65,
}

REWARDS_HASH = "0x" + "ab" * 32


def _client(handler) -> FwdClient:
    fwd = FwdClient("http://fwd:8080", "fwd_live_test")
    fwd._client = httpx.Client(transport=httpx.MockTransport(handler))
    return fwd


# ---- cross-field validation fires BEFORE any HTTP ----

def test_uptime_rejects_chain_id():
    with _client(lambda _: httpx.Response(200, json=FAKE_RESPONSE)) as fwd:
        with pytest.raises(ValueError, match="all be None"):
            fwd.sign_fsp_message("w", "UPTIME", 0, chain_id=114)


def test_uptime_rejects_no_of_claims():
    with _client(lambda _: httpx.Response(200, json=FAKE_RESPONSE)) as fwd:
        with pytest.raises(ValueError, match="all be None"):
            fwd.sign_fsp_message("w", "UPTIME", 0, no_of_weight_based_claims=5)


def test_uptime_rejects_rewards_hash():
    with _client(lambda _: httpx.Response(200, json=FAKE_RESPONSE)) as fwd:
        with pytest.raises(ValueError, match="all be None"):
            fwd.sign_fsp_message("w", "UPTIME", 0, rewards_hash=REWARDS_HASH)


def test_reward_distribution_requires_all_three():
    with _client(lambda _: httpx.Response(200, json=FAKE_RESPONSE)) as fwd:
        with pytest.raises(ValueError, match="all required"):
            fwd.sign_fsp_message("w", "REWARD_DISTRIBUTION", 3)


def test_reward_distribution_requires_rewards_hash_present():
    with _client(lambda _: httpx.Response(200, json=FAKE_RESPONSE)) as fwd:
        with pytest.raises(ValueError, match="all required"):
            fwd.sign_fsp_message("w", "REWARD_DISTRIBUTION", 3, chain_id=114, no_of_weight_based_claims=56)


def test_reward_distribution_bad_rewards_hash_format():
    with _client(lambda _: httpx.Response(200, json=FAKE_RESPONSE)) as fwd:
        with pytest.raises(ValueError, match="rewards_hash must match"):
            fwd.sign_fsp_message(
                "w", "REWARD_DISTRIBUTION", 3,
                chain_id=114, no_of_weight_based_claims=56,
                rewards_hash="not-a-hash",
            )


def test_unknown_message_type_raises():
    with _client(lambda _: httpx.Response(200, json=FAKE_RESPONSE)) as fwd:
        with pytest.raises(ValueError, match="Unknown message_type"):
            fwd.sign_fsp_message("w", "BOGUS", 0)


# Verify no HTTP was made by the cross-field checks (counter trick).
def test_cross_field_check_fires_before_http():
    calls: list[int] = []

    def h(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=FAKE_RESPONSE)

    with _client(h) as fwd:
        with pytest.raises(ValueError):
            fwd.sign_fsp_message("w", "UPTIME", 0, chain_id=99)
    assert calls == [], "HTTP was called before the cross-field check fired"


# ---- happy-path: UPTIME ----

def test_sign_fsp_uptime_success():
    def h(req: httpx.Request) -> httpx.Response:
        body = req.content
        import json
        payload = json.loads(body)
        assert payload["message_type"] == "UPTIME"
        assert payload["reward_epoch_id"] == 0
        # Must NOT include reward-distribution fields.
        assert "chain_id" not in payload
        assert "no_of_weight_based_claims" not in payload
        assert "rewards_hash" not in payload
        # Must NOT include a digest/hash field.
        assert "digest" not in payload
        assert "hash" not in payload
        return httpx.Response(200, json=FAKE_RESPONSE)

    with _client(h) as fwd:
        r = fwd.sign_fsp_message("signing-wallet", "UPTIME", 0)
    assert r.v == 27
    assert r.message_hash == FAKE_RESPONSE["message_hash"]
    assert isinstance(r, SignFspMessageResponse)


# ---- happy-path: REWARD_DISTRIBUTION ----

def test_sign_fsp_rewards_success():
    def h(req: httpx.Request) -> httpx.Response:
        import json
        payload = json.loads(req.content)
        assert payload["message_type"] == "REWARD_DISTRIBUTION"
        assert payload["chain_id"] == 114
        assert payload["no_of_weight_based_claims"] == 56
        assert payload["rewards_hash"] == REWARDS_HASH
        # Must NOT include a digest/hash field.
        assert "digest" not in payload
        return httpx.Response(200, json=FAKE_RESPONSE)

    with _client(h) as fwd:
        r = fwd.sign_fsp_message(
            "signing-wallet", "REWARD_DISTRIBUTION", 3,
            chain_id=114, no_of_weight_based_claims=56,
            rewards_hash=REWARDS_HASH,
        )
    assert r.v == 27


# ---- idempotency key header ----

def test_idempotency_key_header_sent():
    def h(req: httpx.Request) -> httpx.Response:
        assert req.headers.get("Idempotency-Key") == "test-key-123"
        return httpx.Response(200, json=FAKE_RESPONSE)

    with _client(h) as fwd:
        fwd.sign_fsp_message("w", "UPTIME", 0, idempotency_key="test-key-123")


def test_no_idempotency_key_no_header():
    def h(req: httpx.Request) -> httpx.Response:
        assert "Idempotency-Key" not in req.headers
        return httpx.Response(200, json=FAKE_RESPONSE)

    with _client(h) as fwd:
        fwd.sign_fsp_message("w", "UPTIME", 0)


# ---- error status mapping ----

@pytest.mark.parametrize("code,err_code", [
    (403, "policy_denied"),
    (404, "wallet_not_found"),
    (422, "transaction_rejected"),
])
def test_terminal_statuses_fsp(code, err_code):
    def h(_req):
        return httpx.Response(code, json={"error": err_code, "message": "no"})

    with _client(h) as fwd:
        with pytest.raises(FwdTerminalError) as ei:
            fwd.sign_fsp_message("w", "UPTIME", 0)
    assert ei.value.status == code


@pytest.mark.parametrize("code,err_code", [
    (502, "rpc_unreachable"),
    (503, "vault_unavailable"),
])
def test_retryable_statuses_fsp(code, err_code):
    def h(_req):
        return httpx.Response(code, json={"error": err_code, "message": "down"})

    with _client(h) as fwd:
        with pytest.raises(FwdRetryableError):
            fwd.sign_fsp_message("w", "UPTIME", 0)


def test_transport_error_is_retryable_fsp():
    def h(req):
        raise httpx.ConnectError("down")

    fwd = FwdClient("http://fwd:8080", "fwd_live_test")
    fwd._client = httpx.Client(transport=httpx.MockTransport(h))
    with pytest.raises(FwdRetryableError) as ei:
        fwd.sign_fsp_message("w", "UPTIME", 0)
    assert ei.value.error_code == "transport_error"
    fwd.close()


# ---- make_fsp_idempotency_key ----

def test_fsp_idempotency_key_deterministic():
    a = make_fsp_idempotency_key("flare", "UPTIME", 42, "sign")
    b = make_fsp_idempotency_key("flare", "UPTIME", 42, "sign")
    assert a == b


def test_fsp_idempotency_key_bounded():
    k = make_fsp_idempotency_key("flare", "REWARD_DISTRIBUTION", 999, "submit", retry="r1")
    assert len(k) <= 128


def test_fsp_idempotency_key_distinct_legs():
    a = make_fsp_idempotency_key("flare", "UPTIME", 0, "sign")
    b = make_fsp_idempotency_key("flare", "UPTIME", 0, "submit")
    assert a != b


def test_fsp_idempotency_key_distinct_message_types():
    a = make_fsp_idempotency_key("flare", "UPTIME", 0, "sign")
    b = make_fsp_idempotency_key("flare", "REWARD_DISTRIBUTION", 0, "sign")
    assert a != b


def test_fsp_idempotency_key_retry_discriminator():
    base = make_fsp_idempotency_key("flare", "UPTIME", 0, "sign")
    r1 = make_fsp_idempotency_key("flare", "UPTIME", 0, "sign", retry="r1")
    r2 = make_fsp_idempotency_key("flare", "UPTIME", 0, "sign", retry="r2")
    assert base != r1 and r1 != r2
    # Same retry = same key (dedup).
    assert r1 == make_fsp_idempotency_key("flare", "UPTIME", 0, "sign", retry="r1")
