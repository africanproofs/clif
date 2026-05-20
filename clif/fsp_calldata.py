"""Build FlareSystemsManager signing calldata (signUptimeVote / signRewards).

Selectors are derived from the vendored ABI and asserted against independently-
verified anchors at import (fail-loud). The `fakeVoteHash` / `UPTIME_VOTE_HASH`
is keccak256(0x00 * 32) — derived here, never hardcoded directly.

No signing primitive is imported. clif holds zero private keys.
"""

from __future__ import annotations

from eth_abi import encode as abi_encode

from clif._keccak import keccak256
from clif.calldata import _load_abi, canonical_signature, selector

# Independently-derived & pinned anchors: ABI-derived selector asserted ==
# this constant (drift guard). (§4 carried KAT v/r/s vectors, not selectors.)
EXPECTED_SIGN_UPTIME_SELECTOR = "dc5a4225"
EXPECTED_SIGN_REWARDS_SELECTOR = "c00a1a97"

# keccak256(0x00 * 32) — the upstream fakeVoteHash for signUptimeVote.
# Extended keccak scope: this derivation proves the vendored keccak covers
# FSM selectors + fakeVoteHash (not only the claim selector).
UPTIME_VOTE_HASH = "0x" + keccak256(b"\x00" * 32).hex()
assert UPTIME_VOTE_HASH == (
    "0x290decd9548b62a8d60345a988386fc84ba6bc95484008f6362f93160ef3e563"
), f"UPTIME_VOTE_HASH drift: got {UPTIME_VOTE_HASH}"

_FSM_ABI = _load_abi("FlareSystemsManager.json")

SIGN_UPTIME_SIGNATURE = canonical_signature(_FSM_ABI, "signUptimeVote")
SIGN_UPTIME_SELECTOR = selector(SIGN_UPTIME_SIGNATURE)
assert SIGN_UPTIME_SELECTOR.hex() == EXPECTED_SIGN_UPTIME_SELECTOR, (
    f"signUptimeVote selector drift: ABI-derived {SIGN_UPTIME_SELECTOR.hex()} "
    f"({SIGN_UPTIME_SIGNATURE}) != verified {EXPECTED_SIGN_UPTIME_SELECTOR}"
)

SIGN_REWARDS_SIGNATURE = canonical_signature(_FSM_ABI, "signRewards")
SIGN_REWARDS_SELECTOR = selector(SIGN_REWARDS_SIGNATURE)
assert SIGN_REWARDS_SELECTOR.hex() == EXPECTED_SIGN_REWARDS_SELECTOR, (
    f"signRewards selector drift: ABI-derived {SIGN_REWARDS_SELECTOR.hex()} "
    f"({SIGN_REWARDS_SIGNATURE}) != verified {EXPECTED_SIGN_REWARDS_SELECTOR}"
)


def _fixed_bytes(hexstr: str, n: int) -> bytes:
    b = bytes.fromhex(hexstr[2:] if hexstr.startswith(("0x", "0X")) else hexstr)
    if len(b) != n:
        raise ValueError(f"expected {n} bytes, got {len(b)} from {hexstr!r}")
    return b


def build_sign_uptime_calldata(
    reward_epoch_id: int,
    v: int,
    r: str,
    s: str,
) -> str:
    """Return `0x` + selector + ABI-encoded args for `signUptimeVote`.

    The uptimeVoteHash is always keccak256(0x00 * 32) (fakeVoteHash) — the
    upstream convention for signing an uptime attestation without a real vote
    hash payload.
    """
    args = abi_encode(
        ["uint24", "bytes32", "(uint8,bytes32,bytes32)"],
        [
            reward_epoch_id,
            _fixed_bytes(UPTIME_VOTE_HASH, 32),
            (v, _fixed_bytes(r, 32), _fixed_bytes(s, 32)),
        ],
    )
    return "0x" + SIGN_UPTIME_SELECTOR.hex() + args.hex()


def build_sign_rewards_calldata(
    reward_epoch_id: int,
    reward_manager_id: int,
    no_of_weight_based_claims: int,
    rewards_hash: str,
    v: int,
    r: str,
    s: str,
) -> str:
    """Return `0x` + selector + ABI-encoded args for `signRewards`.

    `_noOfWeightBasedClaims` is `[(rewardManagerId, noOfWeightBasedClaims)]` —
    a single-element tuple array. The `rewardManagerId` is the chain-numeric ID
    of the RewardManager (static per network — supplied from net.chain_id in the
    orchestrator, D14: static-table chain_id simplification).
    """
    args = abi_encode(
        ["uint24", "(uint256,uint256)[]", "bytes32", "(uint8,bytes32,bytes32)"],
        [
            reward_epoch_id,
            [(reward_manager_id, no_of_weight_based_claims)],
            _fixed_bytes(rewards_hash, 32),
            (v, _fixed_bytes(r, 32), _fixed_bytes(s, 32)),
        ],
    )
    return "0x" + SIGN_REWARDS_SELECTOR.hex() + args.hex()
