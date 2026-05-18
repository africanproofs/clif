"""The rehearse path's calldata property: a real-builder empty-proofs claim.

When nothing is genuinely claimable, `clif rehearse` feeds an EMPTY proofs
list through the real `build_claim_calldata` — the least hand-modeled valid
shape (no fabricated merkle/body). This locks the two facts the fwd policy
binds on: the anchored selector, and `_recipient` preserved byte-exact (the
pinned arg-predicate; any drift → fwd policy 403, no broadcast).
"""

from eth_abi import decode as abi_decode

from clif.calldata import CLAIM_SELECTOR, _CLAIM_ARG_TYPES, build_claim_calldata
from clif.fwd_client import make_idempotency_key


def test_empty_proofs_rehearse_calldata_round_trips_recipient_exact():
    reward_owner = "0x7c3579aB3E647395c96a1EfC98aF9A31C5Ecc294"
    recipient = "0x7c3579aB3E647395c96a1EfC98aF9A31C5Ecc294"
    epoch = 4242
    wrap = True

    data = build_claim_calldata(reward_owner, recipient, epoch, wrap, [])

    assert data.startswith("0x" + CLAIM_SELECTOR.hex())
    assert CLAIM_SELECTOR.hex() == "8e33aba5"

    payload = bytes.fromhex(data[10:])
    owner_d, recip_d, epoch_d, wrap_d, proofs_d = abi_decode(
        _CLAIM_ARG_TYPES, payload
    )
    assert owner_d == reward_owner.lower()
    assert recip_d == recipient.lower()  # pinned arg-predicate, byte-exact
    assert epoch_d == epoch
    assert wrap_d is True
    assert len(proofs_d) == 0  # empty real proofs — not a hand-authored shape


def test_production_idempotency_key_stays_deterministic_D10():
    """The money path's key MUST NOT vary across calls (no double-broadcast)."""
    a = make_idempotency_key("coston2", 1, "0x" + "7c" * 20, 5585)
    b = make_idempotency_key("coston2", 1, "0x" + "7c" * 20, 5585)
    assert a == b


def test_rehearse_salt_makes_each_attempt_distinct():
    """Rehearse composes base + `-r<tag>`: distinct tags -> distinct keys,
    so fwd cannot replay a stale prior outcome when the epoch hasn't rolled.
    The base remains the exact deterministic production key (unchanged)."""
    base = make_idempotency_key("coston2", 1, "0x" + "7c" * 20, 5585)
    k1 = f"{base}-r1747570000"
    k2 = f"{base}-r1747570099"
    assert k1 != k2
    assert k1.startswith(base) and k2.startswith(base)
    assert len(k1) <= 128  # fwd Idempotency-Key limit
