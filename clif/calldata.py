"""Build `RewardManager.claim(...)` calldata from the vendored ABI.

Every on-wire value is derived from the real producing path, never a
hand-authored shape. The function signature is reconstructed from the
*registered ABI* and the selector is computed at runtime, then asserted equal
to the independently-verified anchor `0x8e33aba5`. A mismatch (wrong ABI, or a
vendored-keccak bug) fails loudly at import.
"""

from __future__ import annotations

import json
from importlib import resources

from eth_abi import encode as abi_encode

from clif._keccak import keccak256
from clif.models import RewardClaimWithProof

# Independently-verified anchor: keccak4 of the canonical claim signature.
EXPECTED_CLAIM_SELECTOR = "8e33aba5"


def _load_abi(filename: str) -> list[dict]:
    raw = resources.files("clif.abi").joinpath(filename).read_text()
    doc = json.loads(raw)
    return doc if isinstance(doc, list) else doc["abi"]


_IRM_ABI = _load_abi("IRewardManager.json")


def _canonical_type(item: dict) -> str:
    """ABI input/component -> canonical type string (recurses tuples)."""
    t = item["type"]
    if t.startswith("tuple"):
        inner = ",".join(_canonical_type(c) for c in item["components"])
        return f"({inner}){t[len('tuple'):]}"  # preserve any [] / [N] suffix
    return t


def _find_function(abi: list[dict], name: str) -> dict:
    for e in abi:
        if e.get("type") == "function" and e.get("name") == name:
            return e
    raise KeyError(f"function {name!r} not in ABI")


def canonical_signature(abi: list[dict], name: str) -> str:
    fn = _find_function(abi, name)
    return f"{name}({','.join(_canonical_type(i) for i in fn['inputs'])})"


def selector(signature: str) -> bytes:
    """First 4 bytes of keccak-256 of a canonical function signature."""
    return keccak256(signature.encode())[:4]


CLAIM_SIGNATURE = canonical_signature(_IRM_ABI, "claim")
CLAIM_SELECTOR = selector(CLAIM_SIGNATURE)

# Fail-loud anchor: derived-from-ABI must equal the verified constant.
assert CLAIM_SELECTOR.hex() == EXPECTED_CLAIM_SELECTOR, (
    f"claim selector drift: ABI-derived {CLAIM_SELECTOR.hex()} "
    f"({CLAIM_SIGNATURE}) != verified {EXPECTED_CLAIM_SELECTOR}"
)

# eth-abi type list for the claim arguments (B1: only the scalar args are
# policy-gateable in fwd; `_proofs` is decoded but not predicated).
_CLAIM_ARG_TYPES = [
    "address",
    "address",
    "uint24",
    "bool",
    "(bytes32[],(uint24,bytes20,uint120,uint8))[]",
]


def _fixed_bytes(hexstr: str, n: int) -> bytes:
    b = bytes.fromhex(hexstr[2:] if hexstr.startswith(("0x", "0X")) else hexstr)
    if len(b) != n:
        raise ValueError(f"expected {n} bytes, got {len(b)} from {hexstr!r}")
    return b


def build_claim_calldata(
    reward_owner: str,
    recipient: str,
    last_epoch_id: int,
    wrap: bool,
    proofs: list[RewardClaimWithProof],
) -> str:
    """Return `0x` + selector + ABI-encoded args for `RewardManager.claim`."""
    encoded_proofs = [
        (
            [_fixed_bytes(p, 32) for p in proof.merkle_proof],
            (
                proof.body.reward_epoch_id,
                _fixed_bytes(proof.body.beneficiary, 20),
                proof.body.amount,
                proof.body.claim_type,
            ),
        )
        for proof in proofs
    ]
    args = abi_encode(
        _CLAIM_ARG_TYPES,
        [reward_owner, recipient, last_epoch_id, wrap, encoded_proofs],
    )
    return "0x" + CLAIM_SELECTOR.hex() + args.hex()
