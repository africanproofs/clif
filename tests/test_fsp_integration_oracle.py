"""FSP integration: §4 oracle frozen vectors (offline shape + parse).

These tests verify that the frozen §4 oracle vectors from the spec prompt
are parse-able by SignFspMessageResponse and that the calldata builders
produce the correct selector prefix + structurally valid output.

Live byte-match upgrade (fwd interaction) is gated by the CLIF_FSP_LIVE_FWD
env var — absent by default, never run in CI without a live fwd instance.
"""

import os

import pytest

from clif.fsp_calldata import (
    EXPECTED_SIGN_REWARDS_SELECTOR,
    EXPECTED_SIGN_UPTIME_SELECTOR,
    UPTIME_VOTE_HASH,
    build_sign_rewards_calldata,
    build_sign_uptime_calldata,
)
from clif.models import SignFspMessageResponse

# §4 oracle UPTIME epoch 0 verified vector.
UPTIME_ORACLE = {
    "message_hash": "0xb7e97e6b4b2c7cd5fb9b51a86ad7eae441872b770b5953443024cb1e0bc6f67d",
    "v": 27,
    "r": "0x9938afc59dae94cb20e0c5982e00c6a88afc01f6ff8c058024f999857a32e785",
    "s": "0x1e926390fbdece399aa1c56dbcbc66d128d43fba246b9459d5018d0c2de9b4b5",
    "signature": "0x" + "00" * 65,  # placeholder — only v/r/s matter for calldata
}

# §4 oracle REWARD_DISTRIBUTION epoch 3, chain_id 114, n 56, rewards_hash 0xab*32.
REWARDS_ORACLE = {
    "message_hash": "0x3f2025e652f0c582e59f6c0f8c7f1fde4fbd80e6f02771d0ab961cbc6ed742c0",
    "v": 27,
    "r": "0x641235a188dac8467dc0e8f3a71073312c4f0dde0f91058db0aca10bee275d5e",
    "s": "0x53c2acf6985b72a9657c57368d9b5f83858f9e988ef52190c0b21410a5acfa7a",
    "signature": "0x" + "00" * 65,
}

REWARDS_HASH = "0x" + "ab" * 32


def test_uptime_oracle_vector_parses():
    """The §4 UPTIME oracle vector is valid for SignFspMessageResponse."""
    r = SignFspMessageResponse.model_validate(UPTIME_ORACLE)
    assert r.v == 27
    assert r.r == UPTIME_ORACLE["r"]
    assert r.s == UPTIME_ORACLE["s"]
    assert r.message_hash == UPTIME_ORACLE["message_hash"]


def test_rewards_oracle_vector_parses():
    """The §4 REWARD_DISTRIBUTION oracle vector is valid for SignFspMessageResponse."""
    r = SignFspMessageResponse.model_validate(REWARDS_ORACLE)
    assert r.v == 27
    assert r.r == REWARDS_ORACLE["r"]
    assert r.message_hash == REWARDS_ORACLE["message_hash"]


def test_uptime_calldata_uses_oracle_signature():
    """Build calldata from the §4 UPTIME oracle v/r/s and verify selector + vote hash."""
    from eth_abi import decode as abi_decode

    data = build_sign_uptime_calldata(
        0, UPTIME_ORACLE["v"], UPTIME_ORACLE["r"], UPTIME_ORACLE["s"]
    )
    assert data[2:10] == EXPECTED_SIGN_UPTIME_SELECTOR
    payload = bytes.fromhex(data[10:])
    epoch, vote_hash, sig = abi_decode(
        ["uint24", "bytes32", "(uint8,bytes32,bytes32)"], payload
    )
    assert epoch == 0
    assert "0x" + vote_hash.hex() == UPTIME_VOTE_HASH
    assert sig[0] == 27


def test_rewards_calldata_uses_oracle_signature():
    """Build calldata from the §4 REWARD_DISTRIBUTION oracle v/r/s and verify structure."""
    from eth_abi import decode as abi_decode

    data = build_sign_rewards_calldata(
        3, 114, 56, REWARDS_HASH,
        REWARDS_ORACLE["v"], REWARDS_ORACLE["r"], REWARDS_ORACLE["s"],
    )
    assert data[2:10] == EXPECTED_SIGN_REWARDS_SELECTOR
    payload = bytes.fromhex(data[10:])
    epoch, n_claims, rh, sig = abi_decode(
        ["uint24", "(uint256,uint256)[]", "bytes32", "(uint8,bytes32,bytes32)"],
        payload,
    )
    assert epoch == 3
    assert n_claims[0][0] == 114   # rewardManagerId = chain_id
    assert n_claims[0][1] == 56    # noOfWeightBasedClaims
    assert "0x" + rh.hex() == REWARDS_HASH
    assert sig[0] == 27


def test_extra_fields_ignored_by_model():
    """extra='ignore' — fwd may add extra fields; model must not reject them."""
    payload = dict(UPTIME_ORACLE)
    payload["extra_field"] = "ignored"
    r = SignFspMessageResponse.model_validate(payload)
    assert r.v == 27


@pytest.mark.skipif(
    not os.environ.get("CLIF_FSP_LIVE_FWD"),
    reason="CLIF_FSP_LIVE_FWD not set — live byte-match skipped (env-deferred)",
)
def test_live_uptime_byte_match():
    """Live upgrade: POST to real fwd and compare returned message_hash to the oracle vector.

    Only runs when CLIF_FSP_LIVE_FWD is set. Requires:
    - FSP_SIGN_CALLER_TOKEN, FSP_SIGNING_WALLET_NAME, FWD_ENDPOINT in env.
    - fwd provisioned with /v1/sign-fsp-message and the FSP signing wallet.
    """
    import os

    from clif.fwd_client import FwdClient

    endpoint = os.environ["FWD_ENDPOINT"]
    token = os.environ["FSP_SIGN_CALLER_TOKEN"]
    wallet = os.environ["FSP_SIGNING_WALLET_NAME"]
    with FwdClient(endpoint, token) as fwd:
        r = fwd.sign_fsp_message(wallet, "UPTIME", 0)
    # The message_hash is deterministic for (network, epoch, message_type)
    # so it should match the oracle vector for epoch=0.
    assert r.message_hash == UPTIME_ORACLE["message_hash"], (
        f"Live message_hash {r.message_hash} != oracle {UPTIME_ORACLE['message_hash']}"
    )
