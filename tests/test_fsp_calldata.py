"""FSP calldata: ABI-derived selectors anchored, UPTIME_VOTE_HASH, round-trip decode."""

from eth_abi import decode as abi_decode

from clif.fsp_calldata import (
    EXPECTED_SIGN_REWARDS_SELECTOR,
    EXPECTED_SIGN_UPTIME_SELECTOR,
    SIGN_REWARDS_SELECTOR,
    SIGN_REWARDS_SIGNATURE,
    SIGN_UPTIME_SELECTOR,
    SIGN_UPTIME_SIGNATURE,
    UPTIME_VOTE_HASH,
    build_sign_rewards_calldata,
    build_sign_uptime_calldata,
)


def test_sign_uptime_signature_from_abi():
    assert SIGN_UPTIME_SIGNATURE == "signUptimeVote(uint24,bytes32,(uint8,bytes32,bytes32))"


def test_sign_rewards_signature_from_abi():
    assert SIGN_REWARDS_SIGNATURE == "signRewards(uint24,(uint256,uint256)[],bytes32,(uint8,bytes32,bytes32))"


def test_sign_uptime_selector_matches_anchor():
    assert SIGN_UPTIME_SELECTOR.hex() == EXPECTED_SIGN_UPTIME_SELECTOR == "dc5a4225"


def test_sign_rewards_selector_matches_anchor():
    assert SIGN_REWARDS_SELECTOR.hex() == EXPECTED_SIGN_REWARDS_SELECTOR == "c00a1a97"


def test_uptime_vote_hash_is_keccak_zero_bytes32():
    assert UPTIME_VOTE_HASH == (
        "0x290decd9548b62a8d60345a988386fc84ba6bc95484008f6362f93160ef3e563"
    )


def test_build_sign_uptime_calldata_prefix():
    v = 27
    r = "0x" + "aa" * 32
    s = "0x" + "bb" * 32
    data = build_sign_uptime_calldata(0, v, r, s)
    assert data.startswith("0x" + EXPECTED_SIGN_UPTIME_SELECTOR)


def test_build_sign_uptime_calldata_round_trip():
    """ABI-decode the calldata and verify all fields match."""
    v, r, s = 27, "0x" + "9a" * 32, "0x" + "1e" * 32
    epoch = 42
    data = build_sign_uptime_calldata(epoch, v, r, s)
    payload = bytes.fromhex(data[10:])  # strip 0x + 4-byte selector
    d_epoch, d_vote_hash, d_sig = abi_decode(
        ["uint24", "bytes32", "(uint8,bytes32,bytes32)"], payload
    )
    assert d_epoch == epoch
    assert "0x" + d_vote_hash.hex() == UPTIME_VOTE_HASH
    d_v, d_r, d_s = d_sig
    assert d_v == v
    assert d_r == bytes.fromhex("9a" * 32)
    assert d_s == bytes.fromhex("1e" * 32)


def test_build_sign_rewards_calldata_prefix():
    v = 27
    r = "0x" + "64" * 32
    s = "0x" + "53" * 32
    data = build_sign_rewards_calldata(3, 114, 56, "0x" + "ab" * 32, v, r, s)
    assert data.startswith("0x" + EXPECTED_SIGN_REWARDS_SELECTOR)


def test_build_sign_rewards_calldata_round_trip():
    """ABI-decode the rewards calldata and verify all fields match."""
    epoch = 3
    rm_id = 114
    n = 56
    rh = "0x" + "ab" * 32
    v, r, s = 27, "0x" + "64" * 32, "0x" + "53" * 32
    data = build_sign_rewards_calldata(epoch, rm_id, n, rh, v, r, s)
    payload = bytes.fromhex(data[10:])
    d_epoch, d_n_claims, d_rh, d_sig = abi_decode(
        ["uint24", "(uint256,uint256)[]", "bytes32", "(uint8,bytes32,bytes32)"],
        payload,
    )
    assert d_epoch == epoch
    assert len(d_n_claims) == 1
    assert d_n_claims[0][0] == rm_id  # rewardManagerId
    assert d_n_claims[0][1] == n      # noOfWeightBasedClaims
    assert "0x" + d_rh.hex() == rh
    d_v, d_r, d_s = d_sig
    assert d_v == v
    assert d_r == bytes.fromhex("64" * 32)
    assert d_s == bytes.fromhex("53" * 32)


def test_oracle_frozen_uptime_vector():
    """§4 oracle UPTIME epoch 0 — shape check (message_hash not re-derived here;
    that is fwd's job). Selector prefix and UPTIME_VOTE_HASH are the anchors."""
    data = build_sign_uptime_calldata(0, 27, "0x" + "99" * 32, "0x" + "1e" * 32)
    assert data[2:10] == EXPECTED_SIGN_UPTIME_SELECTOR
    # Decode and verify vote hash is the fakeVoteHash (not the sig r/s).
    payload = bytes.fromhex(data[10:])
    _, vote_hash, _ = abi_decode(["uint24", "bytes32", "(uint8,bytes32,bytes32)"], payload)
    assert "0x" + vote_hash.hex() == UPTIME_VOTE_HASH


def test_oracle_frozen_rewards_vector():
    """§4 oracle REWARD_DISTRIBUTION epoch 3, chain_id 114, n 56, rewards_hash 0xab*32."""
    rh = "0x" + "ab" * 32
    data = build_sign_rewards_calldata(3, 114, 56, rh, 27, "0x" + "64" * 32, "0x" + "53" * 32)
    assert data[2:10] == EXPECTED_SIGN_REWARDS_SELECTOR
    payload = bytes.fromhex(data[10:])
    d_epoch, d_n, d_rh, _ = abi_decode(
        ["uint24", "(uint256,uint256)[]", "bytes32", "(uint8,bytes32,bytes32)"],
        payload,
    )
    assert d_epoch == 3
    assert d_n[0][0] == 114
    assert d_n[0][1] == 56
    assert "0x" + d_rh.hex() == rh
