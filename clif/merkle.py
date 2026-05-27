"""Flare FSP reward-distribution Merkle tree — build and verify.

Implements the exact leaf and internal-node hash scheme used by the Flare
fsp-rewards reference implementation:

  leaf   = keccak256(abi.encode((uint24, bytes20, uint120, uint8)))
           — a SINGLE keccak (NOT the OpenZeppelin double-hash)
  node   = keccak256(abi.encode(sorted_pair))  — lexicographic sort on 0x-hex
  root   = tree[0] of a sorted-unique leaf array, built bottom-up

Byte-exactness verified against Flare epoch 228 (merkle root
0x1f68e0d9e92745c7f636e1917cfb902c51433fb766969935c68988b9b72ea601)
and epoch 400 (0x7939ff9ecce494a899f878f35359209ae27ef57c786ab480075e91ac1621495f).

Uses:
  clif._keccak.keccak256   — vendored Ethereum keccak (no pycryptodome)
  eth_abi.encode            — allowed dep (already in pyproject.toml)
"""

from __future__ import annotations

from typing import Iterable, Protocol

from eth_abi import encode as abi_encode

from clif._keccak import keccak256


# ---------------------------------------------------------------------------
# Low-level primitives
# ---------------------------------------------------------------------------


def compute_leaf(
    reward_epoch_id: int,
    beneficiary: str,
    amount: int,
    claim_type: int,
) -> str:
    """Return the 0x-prefixed keccak256 leaf for one reward claim.

    Encoding: ``keccak256(abi.encode((uint24, bytes20, uint120, uint8)))``
    — a single keccak, NOT the OZ double-hash.

    ``beneficiary`` must be a 0x-prefixed 40-hex address string; it is
    lower-cased and stripped of the 0x prefix before encoding as bytes20.
    """
    beneficiary_bytes20 = bytes.fromhex(beneficiary.lower().removeprefix("0x"))
    encoded = abi_encode(
        ["(uint24,bytes20,uint120,uint8)"],
        [(reward_epoch_id, beneficiary_bytes20, amount, claim_type)],
    )
    return "0x" + keccak256(encoded).hex()


def node(x: str, y: str) -> str:
    """Combine two 0x-prefixed 32-byte hashes into an internal tree node.

    The pair is sorted lexicographically (on the 0x-hex string) so the hash is
    commutative — same result regardless of left/right assignment.
    """
    a, b = (x, y) if x <= y else (y, x)
    encoded = abi_encode(
        ["bytes32", "bytes32"],
        [bytes.fromhex(a[2:]), bytes.fromhex(b[2:])],
    )
    return "0x" + keccak256(encoded).hex()


# ---------------------------------------------------------------------------
# Claim protocol — accepts both RewardClaimWithProof bodies and RawRewardClaim
# ---------------------------------------------------------------------------


class _HasClaimFields(Protocol):
    reward_epoch_id: int
    beneficiary: str
    amount: int
    claim_type: int


def _leaf_from_claim(claim: object) -> str:
    """Extract fields from any claim-like object and compute its leaf hash.

    Accepts objects exposing .reward_epoch_id / .beneficiary / .amount /
    .claim_type (e.g. RewardClaimBody) as well as bare sequence tuples in
    the RawRewardClaim form ``[proof_list, [epochId, beneficiary, amountStr,
    claimType]]``.
    """
    if isinstance(claim, (list, tuple)):
        # RawRewardClaim: (proof_list, (epoch_id, beneficiary, amount_str, claim_type))
        _, body = claim
        epoch_id, beneficiary, amount_str, claim_type = body
        return compute_leaf(int(epoch_id), str(beneficiary), int(amount_str), int(claim_type))
    # Duck-typed object (RewardClaimBody, RewardClaimWithProof.body, etc.)
    body = getattr(claim, "body", claim)
    return compute_leaf(
        int(body.reward_epoch_id),
        str(body.beneficiary),
        int(body.amount),
        int(body.claim_type),
    )


# ---------------------------------------------------------------------------
# Tree builder
# ---------------------------------------------------------------------------


def build_reward_merkle_root(claims: Iterable[object]) -> str:
    """Build the Merkle root for an iterable of reward claims.

    ``claims`` may contain:
    - ``RewardClaimWithProof`` objects (body attribute)
    - ``RewardClaimBody`` objects (direct fields)
    - ``RawRewardClaim`` tuples ``(proof_list, (epochId, beneficiary, amountStr, claimType))``
    - Any dict-like with a ``body`` sub-object exposing those four fields

    Returns the 0x-prefixed 64-hex root string.  An empty claim set returns
    the all-zeros root (``0x`` + ``"0" * 64``).
    """
    leaves = [_leaf_from_claim(c) for c in claims]

    # Sort lexicographically and deduplicate — matches the upstream reference.
    sorted_unique = sorted(set(leaves))
    n = len(sorted_unique)

    if n == 0:
        return "0x" + "0" * 64

    if n == 1:
        return sorted_unique[0]

    # Bottom-up tree over a flat array:
    #   internal nodes: indices 0 … n-2
    #   leaves:         indices n-1 … 2n-2
    tree: list[str] = ["0x" + "0" * 64] * (n - 1) + sorted_unique
    for i in range(n - 2, -1, -1):
        tree[i] = node(tree[2 * i + 1], tree[2 * i + 2])

    return tree[0]


# ---------------------------------------------------------------------------
# Proof verifier
# ---------------------------------------------------------------------------


def verify_proof(leaf: str, proof: list[str], root: str) -> bool:
    """Return True iff ``leaf`` is included in the Merkle tree with ``root``.

    ``leaf``  — 0x-prefixed 64-hex leaf hash (from ``compute_leaf``).
    ``proof`` — ordered list of 0x-prefixed 64-hex sibling hashes.
    ``root``  — 0x-prefixed 64-hex Merkle root.

    Comparison is case-insensitive on the hex digits.
    """
    h = leaf
    for p in proof:
        h = node(p, h)
    return h.lower() == root.lower()
